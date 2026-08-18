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
import { createEngine } from "./web/engine.js";

const require = createRequire(import.meta.url);
const Parser = require("./web/parser.js");

const TOOLS = {
  echo: { requires_approval: false, run: async (args) => `echo: ${args.text}` },
  run_code: {
    requires_approval: true,
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

async function main() {
  const tests = [
    test_ordinary_tool_executes,
    test_run_code_halts_until_approval,
    test_approval_accept_runs_sandbox,
    test_approval_reject_never_runs,
    test_final_answer_terminates,
    test_turn_cap_enforced,
    test_multi_turn_echo_then_answer,
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