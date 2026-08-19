"use strict";

/* In-browser agent engine (pure logic, no browser APIs).
 *
 * JS port of core/loop.py (run / step / resolve_approval) for the webgpu
 * target. Transport-agnostic like the Python core: the provider is injected
 * and only produces assistant text; tool-call extraction is done through the
 * injected parser (web/parser.js, the line-for-line port of core/parser.py);
 * dispatch + the approval gate are implemented here, never model-decided.
 * Before each generate call the message history is trimmed to the model's
 * context budget via web/context.js (the JS port of core/context.py).
 *
 * Testable in Node with a fake provider (see test_webgpu_engine.mjs).
 *
 * Emits events with the same shapes the web UI SSE path uses:
 *   "token"      { text }
 *   "tool_call"  { id, name, arguments }
 *   "tool_result"{ call_id, content, error }
 *   "approval"   { call_id, tool_name, arguments }
 *   "error"      { message }
 *   "done"       { state }
 *
 * `tools` is a map: name -> { requires_approval, run(arguments) -> Promise<string> }
 */

import { trimToBudget } from "./context.js";

const DEFAULT_MAX_CONTEXT_TOKENS = 32768;
/* Retry-once: mirrors core/loop.RETRY_ONCE_LIMIT. Never auto-retries a
 * rejected call (resolveApproval(false) stays the only way through). */
const RETRY_ONCE_LIMIT = 1;

function newState({ session_id, model, max_turns = 8 }) {
  return {
    session_id: session_id || "browser-" + Math.random().toString(36).slice(2, 10),
    target: "webgpu",
    model: model || "LFM2.5-1.2B-Instruct-ONNX",
    messages: [],
    max_turns,
    turn_count: 0,
    pending_approval: null,
    pending_calls: [],
    retry_count: 0,
    plan: null,
  };
}

function lastMessage(state) {
  return state.messages.length ? state.messages[state.messages.length - 1] : null;
}

function createEngine({ provider, tools, parser, session_id, model, max_turns = 8, max_context_tokens = DEFAULT_MAX_CONTEXT_TOKENS, events = null, memory = null }) {
  const state = newState({ session_id, model, max_turns });
  let emitting = events;

  function emit(event, data) {
    if (emitting && typeof emitting === "function") emitting(event, data);
  }

  function setEmitter(fn) {
    emitting = fn || false;
  }

  function append(message) {
    state.messages.push(message);
  }

  function isTerminal() {
    const last = lastMessage(state);
    return last !== null && last.role === "assistant" && !(last.function_calls || []).length;
  }

  /* Mirrors core/reflection.lesson_from_state. */
  function lessonFromState() {
    if (state.retry_count > 0) {
      return "Lesson: a tool call failed; retry exactly once with corrected arguments, then give up cleanly.";
    }
    for (let i = state.messages.length - 1; i >= 0; i--) {
      const m = state.messages[i];
      if (m.role === "tool" && m.content === "rejected by user") {
        return "Lesson: the user rejected a tool call; never retry a rejected call.";
      }
      if (m.role === "tool" && typeof m.content === "string" && m.content.startsWith("error")) {
        return "Lesson: a tool call errored; check its arguments and results before calling again.";
      }
    }
    return null;
  }

  /* Mirrors core/loop.finalize_turn. Bounded; never blocks the loop. */
  function finalizeLesson() {
    const lesson = lessonFromState();
    if (!lesson) return;
    if (memory && typeof memory.saveMemory === "function") {
      try {
        memory.saveMemory({
          key: `lesson_${Date.now()}`,
          content: lesson,
          kind: "lesson",
          source_session: state.session_id,
        });
        emit("lesson", { lesson });
      } catch (_) {
        /* reflection must never break the turn */
      }
    }
  }

  async function dispatch(call) {
    const tool = tools[call.name];
    if (!tool) {
      const content = `error: unknown tool '${call.name}'`;
      state.retry_count += 1;
      append({ role: "tool", tool_call_id: call.id, content });
      emit("tool_result", { call_id: call.id, content, error: true });
      return;
    }
    if (tool.requires_approval) {
      state.pending_approval = {
        call_id: call.id,
        tool_name: call.name,
        arguments: call.arguments || {},
      };
      emit("approval", state.pending_approval);
      return;
    }
    await execute(tool, call);
  }

  async function execute(tool, call) {
    let content;
    let error = false;
    try {
      content = tool.stateful
        ? await tool.run(state, call.arguments || {})
        : await tool.run(call.arguments || {});
    } catch (err) {
      content = `error: ${err && err.message ? err.message : String(err)}`;
      error = true;
    }
    if (error || String(content).startsWith("error")) state.retry_count += 1;
    append({ role: "tool", tool_call_id: call.id, content });
    emit("tool_result", { call_id: call.id, content, error });
  }

  /* One generate + parse + dispatch cycle. Mirrors core/loop.step. */
  async function step() {
    if (state.pending_approval) return;
    if (state.turn_count >= state.max_turns) {
      emit("error", { message: "Turn budget reached. Start a new session." });
      return;
    }
    const lastBefore = state.messages[state.messages.length - 1];
    if (lastBefore && lastBefore.role === "user") state.retry_count = 0;
    const trimmed = trimToBudget(state.messages, max_context_tokens);
    const assistant = await provider.generate(trimmed.kept, (text) =>
      emit("token", { text })
    );
    const function_calls = parser.parse_tool_calls(assistant.content || "");
    assistant.function_calls = function_calls;
    append(assistant);
    state.turn_count += 1;
    for (let i = 0; i < function_calls.length; i++) {
      const call = function_calls[i];
      emit("tool_call", { id: call.id, name: call.name, arguments: call.arguments });
      await dispatch(call);
      if (state.pending_approval) {
        /* Keep the calls after this one so resolveApproval can resume them. */
        state.pending_calls = function_calls.slice(i + 1);
        return;
      }
    }
  }

  /* Drive until terminal / approval / turn cap / retry-once give-up.
   * Mirrors core/loop.run plus the web server's turn-cap error event. */
  async function start(events) {
    if (events) setEmitter(events);
    while (true) {
      if (state.pending_approval) return state;
      if (state.turn_count >= state.max_turns) {
        emit("error", { message: "Turn budget reached. Start a new session." });
        return state;
      }
      if (state.retry_count > RETRY_ONCE_LIMIT) {
        finalizeLesson();
        emit("done", { state: JSON.parse(JSON.stringify(state)) });
        return state;
      }
      if (isTerminal()) {
        finalizeLesson();
        emit("done", { state: JSON.parse(JSON.stringify(state)) });
        return state;
      }
      try {
        await step();
      } catch (err) {
        emit("error", {
          message: `error: ${err && err.name ? err.name : "Error"}: ${err && err.message ? err.message : String(err)}`,
        });
        return state;
      }
    }
  }

  /* Resolve a pending approval; only approved=True executes the tool.
   * Mirrors core/loop.resolve_approval. Tool calls parked behind the pending
   * one (state.pending_calls) are resumed afterwards. */
  async function resolveApproval(approved) {
    const pending = state.pending_approval;
    if (!pending) return state;
    state.pending_approval = null;
    if (approved) {
      const tool = tools[pending.tool_name];
      if (tool) {
        await execute(tool, {
          id: pending.call_id,
          name: pending.tool_name,
          arguments: pending.arguments,
        });
      }
    } else {
      const content = "rejected by user";
      append({ role: "tool", tool_call_id: pending.call_id, content });
      emit("tool_result", { call_id: pending.call_id, content, error: false });
    }
    const remaining = state.pending_calls || [];
    state.pending_calls = [];
    for (let i = 0; i < remaining.length; i++) {
      const call = remaining[i];
      emit("tool_call", { id: call.id, name: call.name, arguments: call.arguments });
      await dispatch(call);
      if (state.pending_approval) {
        state.pending_calls = remaining.slice(i + 1);
        break;
      }
    }
    /* Continue the loop until terminal / approval / cap. */
    await start();
    return state;
  }

  function userMessage(content) {
    return { role: "user", content };
  }

  function addUserMessage(content) {
    append(userMessage(content));
    return state;
  }

  return {
    state,
    start,
    resolveApproval,
    addUserMessage,
  };
}

export { createEngine, newState };