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
import { estimateTokens, trimToBudget } from "./web/context.js";
import { makePlan, updatePlan } from "./web/planner.js";
import { COMMANDS, helpText, parseCommand, runCommand } from "./web/commands.js";

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
  assert.deepEqual(names.sort(), [
    "/context", "/help", "/memory", "/mode", "/new", "/plan", "/status", "/tools", "/turns",
  ]);
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
    test_phase9_file_tool_flags_parity,
    test_new_state_defaults_permission_mode,
    test_engine_set_permission_mode,
    test_commands_registry_parity,
    test_command_mode_and_turns_mutate_state,
    test_command_new_requests_clear,
    test_command_plan_and_status_render,
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