"use strict";

/* In-browser engine tests (JS port of test_smoke.py's loop + approval gate).
 *
 * Runs entirely in Node: the engine is pure logic, the parser is the real
 * web/parser.js, and the provider + tools are fakes -- no WebGPU, no Pyodide.
 * Providers emit raw tool-call blocks, exactly like the browser model does
 * (Transformers.js has no native tool_calls array; the parser extracts them).
 * Run: node test_webgpu_engine.mjs
 */

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { createEngine, newState, requiresApproval, PERMISSION_MODES } from "./web/engine.js";
import { compactToBudget, estimateTokens, shouldCompact, trimToBudget } from "./web/context.js";
import { makePlan, updatePlan } from "./web/planner.js";
import { COMMANDS, helpText, parseCommand, runCommand } from "./web/commands.js";
import { formatTokens, sessionUsage } from "./web/telemetry.js";
import { activateSkill, loadSkills, renderSkills } from "./web/skills.js";
import { createTask, renderTasks, updateTask } from "./web/tasks.js";
import { MAX_CONCURRENT_SUBAGENTS, spawnSubagent } from "./web/subagent.js";

const require = createRequire(import.meta.url);
const Parser = require("./web/parser.js");

const TOOLS = {
  echo: { requires_approval: false, read_only: true, run: async (args) => `echo: ${args.text}` },
  mutate: { requires_approval: false, read_only: false, run: async (args) => `mutated: ${args.text}` },
  risky: { requires_approval: true, read_only: false, run: async (args) => `risky: ${args.code}` },
  run_code: {
    requires_approval: true,
    read_only: false,
    run: async (args) => `ran: ${args.code}`,
  },
};

function makeEngine(provider, opts) {
  const events = [];
  const engine = createEngine({
    provider,
    tools: TOOLS,
    parser: Parser,
    events: (event, data) => events.push([event, data]),
    ...opts,
  });
  return { engine, events };
}

const ECHO_BLOCK = '<|tool_call_start|>echo(text="hello")<|tool_call_end|>';
const RUN_BLOCK =
  '<|tool_call_start|>run_code(language="python", code="print(1)")<|tool_call_end|>';

async function test_ordinary_tool_executes() {
  const blocks = [ECHO_BLOCK, "done."];
  const provider = { generate: async () => ({ role: "assistant", content: blocks.shift() }) };
  const { engine, events } = makeEngine(provider);
  await engine.start();
  const names = events.map(([e]) => e);
  assert.ok(names.includes("tool_call"), "expected a tool_call event");
  assert.ok(names.includes("tool_result"), "expected a tool_result event");
  const result = events.find(([e]) => e === "tool_result");
  assert.equal(result[1].content, "echo: hello");
  assert.equal(result[1].error, false);
  assert.equal(engine.state.turn_count, 2);
}

async function test_run_code_halts_until_approval() {
  const provider = { generate: async () => ({ role: "assistant", content: RUN_BLOCK }) };
  const { engine, events } = makeEngine(provider);
  await engine.start();
  const approval = events.find(([e]) => e === "approval");
  assert.ok(approval, "expected an approval event");
  assert.equal(approval[1].tool_name, "run_code");
  assert.equal(approval[1].arguments.code, "print(1)");
  assert.equal(engine.state.pending_approval.call_id, "call_0001");
  assert.ok(!events.some(([e]) => e === "tool_result"), "tool must not run before approval");
}

async function test_approval_accept_runs_sandbox() {
  const blocks = [RUN_BLOCK, "done."];
  const provider = { generate: async () => ({ role: "assistant", content: blocks.shift() }) };
  const { engine, events } = makeEngine(provider);
  await engine.start();
  assert.ok(engine.state.pending_approval, "must halt at approval");
  await engine.resolveApproval(true);
  const result = events.find(([e]) => e === "tool_result");
  assert.ok(result, "expected tool_result after approval");
  assert.equal(result[1].content, "ran: print(1)");
  assert.equal(engine.state.pending_approval, null);
}

async function test_approval_reject_never_runs() {
  const blocks = [RUN_BLOCK, "done."];
  const provider = { generate: async () => ({ role: "assistant", content: blocks.shift() }) };
  const { engine, events } = makeEngine(provider);
  await engine.start();
  const result = events.find(([e]) => e === "tool_result");
  assert.equal(result, undefined, "tool must not run before approval");
  await engine.resolveApproval(false);
  const result2 = events.find(([e]) => e === "tool_result");
  assert.ok(result2, "expected tool_result after rejection");
  assert.equal(result2[1].content, "rejected by user");
  assert.equal(engine.state.pending_approval, null);
}

async function test_final_answer_terminates() {
  const provider = {
    generate: async () => ({ role: "assistant", content: "done." }),
  };
  const { engine, events } = makeEngine(provider);
  await engine.start();
  const done = events.find(([e]) => e === "done");
  assert.ok(done, "expected done event");
  assert.equal(done[1].state.turn_count, 1);
  assert.equal(engine.state.messages.at(-1).content, "done.");
}

async function test_turn_cap_enforced() {
  const provider = {
    generate: async () => ({ role: "assistant", content: "still looping." }),
  };
  const { engine, events } = makeEngine(provider, { max_turns: 1 });
  await engine.start();
  assert.equal(engine.state.turn_count, 1);
  assert.ok(events.some(([e]) => e === "error"), "expected turn-budget error");
}

async function test_multi_turn_echo_then_answer() {
  const blocks = [ECHO_BLOCK, "done."];
  const provider = { generate: async () => ({ role: "assistant", content: blocks.shift() }) };
  const { engine, events } = makeEngine(provider);
  await engine.start();
  const done = events.find(([e]) => e === "done");
  assert.ok(done, "expected done event");
  assert.equal(done[1].state.turn_count, 2);
  assert.equal(engine.state.messages.at(-1).content, "done.");
}

/* ---------- context-window parity (core/context.py <-> web/context.js) ---------- */

function sys() {
  return { role: "system", content: "You are a helpful assistant." };
}
function usr(text) {
  return { role: "user", content: text };
}
function asst(text) {
  return { role: "assistant", content: text };
}

function test_estimate_matches_python() {
  // estimate_tokens = ceil(chars/4), min 1 per message
  assert.equal(estimateTokens([usr("")]), 1);
  assert.equal(estimateTokens([usr("abcd")]), 1);
  assert.equal(estimateTokens([usr("abcde")]), 2);
  assert.equal(estimateTokens([usr("x".repeat(100))]), 25);
  assert.equal(estimateTokens([]), 0);
}

function test_trim_keeps_system_and_last_turn() {
  const msgs = [sys(), usr("first"), asst("one"), usr("last"), asst("two")];
  const { kept, dropped } = trimToBudget(msgs, 1);
  assert.deepEqual(kept, [sys(), usr("last"), asst("two")]);
  assert.deepEqual(dropped, [usr("first"), asst("one")]);
}

function test_trim_drops_oldest_turns_first() {
  const msgs = [sys(), usr("first"), asst("one"), usr("last"), asst("two")];
  const budget = estimateTokens([sys(), usr("last"), asst("two")]);
  const { kept, dropped } = trimToBudget(msgs, budget);
  assert.deepEqual(kept, [sys(), usr("last"), asst("two")]);
  assert.deepEqual(dropped, [usr("first"), asst("one")]);
}

function test_trim_never_splits_tool_pair() {
  const msgs = [
    sys(),
    usr("first"),
    {
      role: "assistant",
      content: "",
      function_calls: [{ id: "c1", name: "echo", arguments: { text: "x" } }],
    },
    { role: "tool", tool_call_id: "c1", content: "echo: x" },
    usr("last"),
    asst("done"),
  ];
  const budget = estimateTokens([sys(), usr("last"), asst("done")]);
  const { kept, dropped } = trimToBudget(msgs, budget);
  assert.deepEqual(kept, [sys(), usr("last"), asst("done")]);
  assert.equal(dropped.length, 3);
  // the tool pair dropped together, never split across kept/dropped
  const droppedRoles = dropped.map((m) => m.role);
  assert.deepEqual(droppedRoles, ["user", "assistant", "tool"]);
}

function test_engine_trims_before_generate() {
  const seen = [];
  const provider = {
    generate: async (messages, onToken) => {
      seen.push(messages.map((m) => m.content || m.role));
      return { role: "assistant", content: "done." };
    },
  };
  const { engine } = makeEngine(provider, { max_context_tokens: 1 });
  // Seed a long history: system + an old turn that must be trimmed away.
  engine.state.messages.push(sys());
  engine.state.messages.push(usr("old turn " + "x".repeat(500)));
  engine.state.messages.push(asst("old reply"));
  engine.state.messages.push(usr("live question"));
  return (async () => {
    await engine.start();
    assert.ok(seen.length >= 1, "provider should have been called");
    const firstPrompt = seen[0];
    assert.ok(!firstPrompt.some((c) => String(c).includes("old turn")), "old turn must be trimmed");
    assert.ok(firstPrompt.includes("live question"), "last user message must be kept");
    assert.ok(firstPrompt.includes("You are a helpful assistant."), "system prompt must be kept");
  })();
}

/* ---------- Phase 5 parity: stateful tools, planning, retry-once, lesson ---------- */

const STATE_TOOLS = {
  make_plan: {
    requires_approval: false,
    stateful: true,
    run: async (state, args) => {
      let steps = args.steps;
      if (typeof steps === "string") steps = [steps];
      makePlan(state, args.goal, steps);
      return `plan set: ${JSON.stringify(state.plan)}`;
    },
  },
  update_plan: {
    requires_approval: false,
    stateful: true,
    run: async (state, args) => {
      const step = updatePlan(state, args.step_id, args.status, args.result);
      return `step ${step.id} -> ${step.status}`;
    },
  },
};

function flakyTools(calls) {
  return {
    echo: {
      requires_approval: false,
      run: async (args) => {
        calls.push(args.text);
        if (args.text === "boom") return "error: boom failed";
        return `echo: ${args.text}`;
      },
    },
  };
}

async function test_new_state_includes_plan_and_retry() {
  const { engine } = makeEngine({
    generate: async () => ({ role: "assistant", content: "hi" }),
  });
  assert.equal(engine.state.retry_count, 0);
  assert.equal(engine.state.plan, null);
}

async function test_stateful_tools_mutate_state_plan() {
  const blocks = [
    '<|tool_call_start|>make_plan(goal="research", steps=["a", "b"])<|tool_call_end|>',
    '<|tool_call_start|>update_plan(step_id="step_1", status="done", result="ok")<|tool_call_end|>',
    "done.",
  ];
  const provider = { generate: async () => ({ role: "assistant", content: blocks.shift() }) };
  const { engine } = makeEngine(provider, { tools: STATE_TOOLS });
  await engine.start();
  assert.ok(engine.state.plan, "plan must be set");
  assert.equal(engine.state.plan.goal, "research");
  assert.equal(engine.state.plan.steps.length, 2);
  assert.equal(engine.state.plan.steps[0].status, "done");
  assert.equal(engine.state.plan.steps[0].result, "ok");
}

async function test_retry_once_recovers_with_corrected_args() {
  const calls = [];
  const blocks = [
    '<|tool_call_start|>echo(text="boom")<|tool_call_end|>',
    '<|tool_call_start|>echo(text="fixed")<|tool_call_end|>',
    "done.",
  ];
  const provider = { generate: async () => ({ role: "assistant", content: blocks.shift() }) };
  const { engine } = makeEngine(provider, { tools: flakyTools(calls) });
  await engine.start();
  assert.deepEqual(calls, ["boom", "fixed"]);
  assert.equal(engine.state.retry_count, 1, "only the first failure counts");
  assert.equal(engine.state.messages.at(-1).content, "done.");
}

async function test_retry_once_gives_up_after_second_failure() {
  const calls = [];
  const blocks = [
    '<|tool_call_start|>echo(text="boom")<|tool_call_end|>',
    '<|tool_call_start|>echo(text="boom")<|tool_call_end|>',
    "unused third response.",
  ];
  const provider = { generate: async () => ({ role: "assistant", content: blocks.shift() }) };
  const { engine, events } = makeEngine(provider, { tools: flakyTools(calls) });
  await engine.start();
  assert.equal(engine.state.retry_count, 2);
  assert.equal(calls.length, 2, "third response must not be generated");
  assert.ok(events.some(([e]) => e === "done"), "give-up should still emit done");
}

async function test_lesson_emitted_on_terminal_failure() {
  const calls = [];
  const mem = { saved: [], saveMemory: (entry) => mem.saved.push(entry) };
  const blocks = [
    '<|tool_call_start|>echo(text="boom")<|tool_call_end|>',
    "done.",
  ];
  const provider = { generate: async () => ({ role: "assistant", content: blocks.shift() }) };
  const { engine, events } = makeEngine(provider, {
    tools: flakyTools(calls),
    memory: mem,
  });
  await engine.start();
  assert.equal(mem.saved.length, 1, "one lesson expected");
  assert.equal(mem.saved[0].kind, "lesson");
  assert.ok(mem.saved[0].content.includes("retry exactly once"));
  assert.ok(events.some(([e]) => e === "lesson"), "expected a lesson event");
}

/* ---------- Phase 8: permission modes + shared commands ---------- */

function modeEngine(mode, content) {
  const provider = { generate: async () => ({ role: "assistant", content }) };
  return makeEngine(provider, { permission_mode: mode });
}

async function test_mode_default_parks_only_gated_tools() {
  const { engine, events } = modeEngine("default", ECHO_BLOCK);
  await engine.start();
  assert.ok(events.some(([e]) => e === "tool_result"), "echo should run in default mode");

  const blocked = modeEngine("default", '<|tool_call_start|>risky(code="x")<|tool_call_end|>');
  await blocked.engine.start();
  assert.ok(blocked.events.some(([e]) => e === "approval"), "risky should be parked");
}

async function test_mode_plan_runs_only_read_only_tools() {
  const allowed = modeEngine("plan", ECHO_BLOCK);
  await allowed.engine.start();
  assert.ok(allowed.events.some(([e]) => e === "tool_result"), "read-only echo runs in plan mode");

  const parked = modeEngine("plan", '<|tool_call_start|>mutate(text="x")<|tool_call_end|>');
  await parked.engine.start();
  assert.ok(parked.events.some(([e]) => e === "approval"), "mutating tool is parked in plan mode");
  assert.ok(!parked.events.some(([e]) => e === "tool_result"), "mutating tool must not run");
}

async function test_mode_auto_runs_gated_tools_but_never_run_code() {
  const risky = modeEngine("auto", '<|tool_call_start|>risky(code="x")<|tool_call_end|>');
  await risky.engine.start();
  const result = risky.events.find(([e]) => e === "tool_result");
  assert.ok(result, "auto mode should run risky without approval");
  assert.equal(result[1].content, "risky: x");

  const sandbox = modeEngine("auto", RUN_BLOCK);
  await sandbox.engine.start();
  assert.ok(sandbox.events.some(([e]) => e === "approval"), "run_code stays gated in auto mode");
  assert.equal(sandbox.engine.state.pending_approval.tool_name, "run_code");
  await sandbox.engine.resolveApproval(false);
  const results = sandbox.events.filter(([e]) => e === "tool_result");
  assert.equal(results.length, 1, "rejection records exactly one tool result");
  assert.equal(results[0][1].content, "rejected by user", "rejected run_code must not run");
  assert.ok(!results.some(([, d]) => d.content === "ran: print(1)"));
}

async function test_mode_strict_parks_every_tool() {
  for (const block of [ECHO_BLOCK, '<|tool_call_start|>mutate(text="x")<|tool_call_end|>']) {
    const { engine, events } = modeEngine("strict", block);
    await engine.start();
    assert.ok(events.some(([e]) => e === "approval"), "strict parks every call");
    assert.ok(!events.some(([e]) => e === "tool_result"), "nothing runs in strict mode");
    assert.ok(engine.state.pending_approval);
  }
}

async function test_requires_approval_matches_python() {
  for (const mode of PERMISSION_MODES) {
    const state = { permission_mode: mode };
    assert.equal(requiresApproval(TOOLS.run_code, "run_code", state), true, `${mode}: run_code`);
    assert.equal(requiresApproval(TOOLS.echo, "echo", state), mode === "strict", `${mode}: echo`);
    assert.equal(
      requiresApproval(TOOLS.mutate, "mutate", state),
      mode === "strict" || mode === "plan",
      `${mode}: mutate`
    );
    assert.equal(
      requiresApproval(TOOLS.risky, "risky", state),
      mode !== "auto",
      `${mode}: risky`
    );
  }
  assert.equal(requiresApproval(TOOLS.risky, "risky", null), true, "null state = legacy flag");
  assert.equal(requiresApproval(TOOLS.echo, "echo", null), false);
}

async function test_always_confirm_send_tools_in_every_mode() {
  // Mirror of test_permission_modes.test_send_tools_confirm_in_every_mode:
  // send_email / send_message are ALWAYS_CONFIRM_TOOLS -- true in every mode,
  // including auto (external sends are never silently approved).
  const sendTools = {
    send_email: { requires_approval: true, read_only: false },
    send_message: { requires_approval: true, read_only: false },
  };
  for (const mode of PERMISSION_MODES) {
    const state = { permission_mode: mode };
    for (const [name, tool] of Object.entries(sendTools)) {
      assert.equal(requiresApproval(tool, name, state), true, `${mode}: ${name}`);
    }
  }
  // Even a definition flag of false cannot defeat the always-confirm set.
  const state = { permission_mode: "auto" };
  assert.equal(requiresApproval({ requires_approval: false, read_only: false }, "send_email", state), true, "auto: send_email");
  assert.equal(requiresApproval({ requires_approval: false, read_only: false }, "send_message", state), true, "auto: send_message");
}

async function test_phase9_file_tool_flags_parity() {
  const files = {
    read_file: { requires_approval: false, read_only: true, run: async () => "" },
    write_file: { requires_approval: true, read_only: false, run: async () => "" },
  };
  for (const mode of PERMISSION_MODES) {
    const state = { permission_mode: mode };
    assert.equal(
      requiresApproval(files.read_file, "read_file", state),
      mode === "strict",
      `${mode}: read_file`
    );
    assert.equal(
      requiresApproval(files.write_file, "write_file", state),
      mode !== "auto",
      `${mode}: write_file`
    );
  }
}

async function test_new_state_defaults_permission_mode() {
  assert.equal(newState({}).permission_mode, "default");
  assert.equal(newState({ permission_mode: "plan" }).permission_mode, "plan");
  assert.equal(newState({ permission_mode: "yolo" }).permission_mode, "default");
}

async function test_engine_set_permission_mode() {
  const provider = { generate: async () => ({ role: "assistant", content: "ok." }) };
  const { engine } = makeEngine(provider);
  assert.equal(engine.state.permission_mode, "default");
  engine.setPermissionMode("strict");
  assert.equal(engine.state.permission_mode, "strict");
  assert.throws(() => engine.setPermissionMode("yolo"), /invalid permission mode/);
}

async function test_commands_registry_parity() {
  const names = Object.keys(COMMANDS);
  // Browser table mirrors core/commands.py minus host-only commands:
  // /resume //approve /reject /quit run on the host drive; /doctor probes the
  // host's llama-server, Docker daemon, and directories (documented ceiling —
  // the web UI routes /doctor through POST /api/sessions/<id>/command).
  assert.deepEqual(names.sort(), [
    "/context", "/cost", "/help", "/memory", "/mode", "/new", "/plan", "/status", "/tasks", "/tools", "/turns",
  ]);
  assert.ok(!names.includes("/doctor"), "/doctor is host-only in the browser");
  assert.deepEqual(parseCommand("/turns 3"), { name: "/turns", arg: "3" });
  assert.deepEqual(parseCommand("  /mode  plan "), { name: "/mode", arg: "plan" });
  assert.ok(helpText().includes("/mode"));
  assert.equal(runCommand("/nope", newState({})).error, true);
}

async function test_command_mode_and_turns_mutate_state() {
  const state = newState({});
  const set = runCommand("/mode plan", state);
  assert.equal(set.error, false);
  assert.equal(state.permission_mode, "plan");
  assert.equal(runCommand("/mode yolo", state).error, true);
  assert.equal(state.permission_mode, "plan");

  const turns = runCommand("/turns 4", state);
  assert.equal(turns.error, false);
  assert.equal(state.max_turns, 4);
  assert.equal(runCommand("/turns 0", state).error, true);
  assert.equal(state.max_turns, 4);
}

async function test_command_new_requests_clear() {
  const state = newState({});
  const result = runCommand("/new", state);
  assert.equal(result.clear, true);
  assert.equal(result.error, false);
}

async function test_command_plan_and_status_render() {
  const state = newState({});
  assert.ok(runCommand("/plan", state).text.includes("No plan"));
  makePlan(state, "goal", ["one", "two"]);
  assert.ok(runCommand("/plan", state).text.includes("goal"));
  assert.ok(runCommand("/status", state).text.includes("permission_mode"));
  assert.ok(runCommand("/context", state, { max_context_tokens: 2048 }).text.includes("2048"));
  assert.ok(runCommand("/tools", state, { tools: TOOLS }).text.includes("run_code"));
}

/* ---------- Phase 10: skills + sub-agents (core/skills, core/subagent ports) ---------- */

async function test_phase10_skill_allowlist_denies_other_tools() {
  const provider = {
    generate: async () => ({
      role: "assistant",
      content: '<|tool_call_start|>mutate(text="x")<|tool_call_end|>',
    }),
  };
  const { engine, events } = makeEngine(provider);
  engine.state.tool_allowlist = ["echo"];
  await engine.start();
  const result = events.find(([e]) => e === "tool_result");
  assert.ok(result[1].content.includes("not allowed by the active skill"), result[1].content);
  assert.ok(!events.some(([e]) => e === "approval"), "allowlist denial is not an approval");
}

async function test_phase10_skill_allowlist_allows_listed_tool() {
  const blocks = ['<|tool_call_start|>echo(text="hi")<|tool_call_end|>', "done."];
  const provider = { generate: async () => ({ role: "assistant", content: blocks.shift() }) };
  const { engine, events } = makeEngine(provider);
  engine.state.tool_allowlist = ["echo"];
  await engine.start();
  const result = events.find(([e]) => e === "tool_result");
  assert.equal(result[1].content, "echo: hi");
}

async function test_phase10_allowlist_cleared_on_new_user_turn() {
  const provider = { generate: async () => ({ role: "assistant", content: "hi" }) };
  const { engine } = makeEngine(provider);
  engine.state.tool_allowlist = ["echo"];
  engine.addUserMessage("go");
  await engine.start();
  assert.equal(engine.state.tool_allowlist, null);
}

async function test_phase10_skills_module_parity() {
  const skills = loadSkills([
    { name: "research", description: "do research", prompt: "Be thorough", tools: ["echo"] },
  ]);
  const state = newState({});
  const msg = activateSkill(state, "research", skills);
  assert.ok(msg.includes("Allowed tools: echo"));
  assert.deepEqual(state.tool_allowlist, ["echo"]);
  assert.ok(state.messages.at(-1).content.includes("Be thorough"));
  assert.ok(activateSkill(state, "missing", skills).startsWith("error: unknown skill"));
  assert.ok(renderSkills(skills).includes("research"));
}

function fakeChildEngine(assistantTexts) {
  const state = newState({});
  return {
    state,
    start: async () => {
      for (const content of assistantTexts) state.messages.push({ role: "assistant", content });
    },
  };
}

async function test_phase10_subagent_returns_summary() {
  const parent = newState({});
  const child = fakeChildEngine(["working...", "final summary"]);
  const result = await spawnSubagent(parent, "do a thing", { createEngine: () => child });
  assert.equal(result.status, "ok");
  assert.equal(result.summary, "final summary");
  assert.equal(parent.active_subagents, 0, "parent counter restored");
  assert.equal(child.state.active_subagents, MAX_CONCURRENT_SUBAGENTS, "one level deep");
}

async function test_phase10_subagent_bubbles_approval() {
  const parent = newState({});
  const child = fakeChildEngine([]);
  child.state.pending_approval = { call_id: "c1", tool_name: "run_code", arguments: {} };
  const result = await spawnSubagent(parent, "run it", { createEngine: () => child });
  assert.equal(result.status, "needs_approval");
  assert.equal(result.pending.tool_name, "run_code");
}

async function test_phase10_subagent_rejects_nesting_and_empty_goal() {
  const parent = newState({});
  parent.active_subagents = MAX_CONCURRENT_SUBAGENTS;
  const nested = await spawnSubagent(parent, "goal", { createEngine: () => fakeChildEngine([]) });
  assert.equal(nested.status, "rejected");

  const empty = await spawnSubagent(newState({}), "   ", { createEngine: () => fakeChildEngine([]) });
  assert.equal(empty.status, "rejected");
}

/* ---------- Phase 11: compaction + tasks ---------- */

async function test_phase11_compaction_replaces_dropped_with_summary() {
  const msgs = [sys(), usr("first"), asst("one"), usr("last"), asst("two")];
  assert.equal(shouldCompact(msgs, 1), true);
  const { kept, dropped, summary } = compactToBudget(msgs, 1, "they asked about first");
  assert.equal(summary, "they asked about first");
  assert.deepEqual(dropped, [usr("first"), asst("one")]);
  assert.equal(kept[0].role, "system");
  assert.ok(kept[1].content.startsWith("Summary of earlier conversation:"));
  assert.deepEqual(kept.slice(2), [usr("last"), asst("two")]);

  const nothing = compactToBudget(msgs, 100000, "x");
  assert.equal(nothing.summary, null);
  assert.equal(nothing.dropped.length, 0);
  assert.equal(nothing.kept.length, msgs.length);
}

async function test_phase11_compaction_never_splits_tool_pair() {
  const msgs = [
    sys(),
    usr("q1"),
    { role: "assistant", content: "", function_calls: [{ id: "c1", name: "echo", arguments: {} }] },
    { role: "tool", tool_call_id: "c1", content: "echo: hi" },
    asst("a1"),
    usr("q2"),
    asst("a2"),
  ];
  const { kept, dropped } = trimToBudget(msgs, 1);
  assert.deepEqual(kept, [sys(), usr("q2"), asst("a2")]);
  assert.deepEqual(dropped, msgs.slice(1, 5), "the whole earlier turn drops together");
  const { kept: compacted } = compactToBudget(msgs, 1, "summary");
  assert.deepEqual(compacted.slice(2), [usr("q2"), asst("a2")]);
}

async function test_phase11_tasks_module_parity() {
  const state = newState({});
  const t1 = createTask(state, "first task");
  assert.equal(t1.id, "task_1");
  assert.equal(t1.status, "pending");
  assert.equal(createTask(state, "second").id, "task_2");
  updateTask(state, "task_1", "done", "did it");
  assert.equal(state.tasks[0].status, "done");
  assert.equal(state.tasks[0].result, "did it");
  assert.throws(() => updateTask(state, "task_9", "done"), /no task/);
  assert.throws(() => updateTask(state, "task_1", "yolo"), /invalid task status/);
  assert.ok(renderTasks(state).includes("task_1"));
  assert.ok(renderTasks(newState({})).includes("No tasks"));
}

async function test_phase11_command_tasks() {
  const state = newState({});
  assert.ok(runCommand("/tasks", state).text.includes("No tasks"));
  assert.ok(runCommand("/tasks add write docs", state).text.includes("task_1"));
  const updated = runCommand("/tasks task_1 done", state);
  assert.equal(state.tasks[0].status, "done");
  assert.ok(updated.text.includes("[done]"));
  assert.ok(runCommand("/tasks task_99 done", state).error, "unknown task id errors");
}

/* ---------- Phase 12: telemetry (core/telemetry.js port + /cost) ---------- */

function usageRecord(prompt, completion, latency) {
  return {
    prompt_tokens: prompt,
    completion_tokens: completion,
    total_tokens: prompt + completion,
    latency_s: latency,
    at: 1792460000.0,
  };
}

async function test_phase12_new_state_records_usage_list() {
  const state = newState({});
  assert.deepEqual(state.usage, []);
  state.usage.push(usageRecord(100, 10, 0.5));
  assert.deepEqual(state.usage.length, 1);
}

async function test_phase12_session_usage_totals_and_tps() {
  const state = newState({});
  assert.deepEqual(sessionUsage(state), {
    turns: 0,
    prompt_tokens: 0,
    completion_tokens: 0,
    total_tokens: 0,
    generate_seconds: 0,
    tokens_per_second: 0.0,
    last_tokens_per_second: 0.0,
  });
  state.usage.push(usageRecord(2000, 100, 2.0));
  state.usage.push(usageRecord(1500, 200, 4.0));
  const summary = sessionUsage(state);
  assert.equal(summary.turns, 2);
  assert.equal(summary.prompt_tokens, 3500);
  assert.equal(summary.completion_tokens, 300);
  assert.equal(summary.total_tokens, 3800);
  assert.equal(summary.generate_seconds, 6.0);
  assert.equal(summary.tokens_per_second, 50.0, "300 completion tok / 6.0s");
  assert.equal(summary.last_tokens_per_second, 50.0);
}

async function test_phase12_format_tokens_matches_python_commas() {
  assert.equal(formatTokens(0), "0");
  assert.equal(formatTokens(1234), "1,234");
  assert.equal(formatTokens(9182736), "9,182,736");
}

async function test_phase12_cost_command_parity() {
  const state = newState({});
  const empty = runCommand("/cost", state);
  assert.equal(empty.error, false);
  assert.ok(empty.text.startsWith("No usage recorded yet"), empty.text);
  assert.ok(empty.text.includes("local"), empty.text);

  state.usage.push(usageRecord(2014, 412, 6.25));
  const populated = runCommand("/cost", state);
  assert.equal(populated.error, false);
  assert.ok(populated.text.includes("Session usage: 1 generation(s)"), populated.text);
  assert.ok(populated.text.includes("2,014"), populated.text);
  assert.ok(populated.text.includes("2,426"), "total falls back to prompt+completion");
  assert.ok(populated.text.includes("$0.00 - local, no metered API calls"), populated.text);
}

async function test_phase12_context_command_real_usage_line() {
  const state = newState({});
  const base = runCommand("/context", state, { max_context_tokens: 2048 });
  assert.ok(!base.text.includes("Real usage"), base.text);
  state.usage.push(usageRecord(512, 64, 0.8));
  const withUsage = runCommand("/context", state, { max_context_tokens: 2048 });
  assert.ok(withUsage.text.includes("Real usage (server-reported)"), withUsage.text);
  assert.ok(withUsage.text.includes("576"), withUsage.text);
  assert.ok(withUsage.text.includes("tok/s"), withUsage.text);
}

async function main() {
  const tests = [
    test_ordinary_tool_executes,
    test_run_code_halts_until_approval,
    test_approval_accept_runs_sandbox,
    test_approval_reject_never_runs,
    test_final_answer_terminates,
    test_turn_cap_enforced,
    test_multi_turn_echo_then_answer,
    test_estimate_matches_python,
    test_trim_keeps_system_and_last_turn,
    test_trim_drops_oldest_turns_first,
    test_trim_never_splits_tool_pair,
    test_engine_trims_before_generate,
    test_new_state_includes_plan_and_retry,
    test_stateful_tools_mutate_state_plan,
    test_retry_once_recovers_with_corrected_args,
    test_retry_once_gives_up_after_second_failure,
    test_lesson_emitted_on_terminal_failure,
    test_mode_default_parks_only_gated_tools,
    test_mode_plan_runs_only_read_only_tools,
    test_mode_auto_runs_gated_tools_but_never_run_code,
    test_mode_strict_parks_every_tool,
    test_requires_approval_matches_python,
    test_always_confirm_send_tools_in_every_mode,
    test_phase9_file_tool_flags_parity,
    test_new_state_defaults_permission_mode,
    test_engine_set_permission_mode,
    test_commands_registry_parity,
    test_command_mode_and_turns_mutate_state,
    test_command_new_requests_clear,
    test_command_plan_and_status_render,
    test_phase10_skill_allowlist_denies_other_tools,
    test_phase10_skill_allowlist_allows_listed_tool,
    test_phase10_allowlist_cleared_on_new_user_turn,
    test_phase10_skills_module_parity,
    test_phase10_subagent_returns_summary,
    test_phase10_subagent_bubbles_approval,
    test_phase10_subagent_rejects_nesting_and_empty_goal,
    test_phase11_compaction_replaces_dropped_with_summary,
    test_phase11_compaction_never_splits_tool_pair,
    test_phase11_tasks_module_parity,
    test_phase11_command_tasks,
    test_phase12_new_state_records_usage_list,
    test_phase12_session_usage_totals_and_tps,
    test_phase12_format_tokens_matches_python_commas,
    test_phase12_cost_command_parity,
    test_phase12_context_command_real_usage_line,
  ];
  let failures = 0;
  for (const test of tests) {
    try {
      await test();
      console.log(`PASS  ${test.name}`);
    } catch (err) {
      failures += 1;
      console.log(`FAIL  ${test.name}: ${err}`);
    }
  }
  if (failures) {
    console.error(`${failures} test(s) failed`);
    process.exit(1);
  }
  console.log(`\nAll ${tests.length} engine tests passed.`);
}

main();