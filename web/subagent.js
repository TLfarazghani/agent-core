"use strict";

/* Phase 10 sub-agents — JS port of core/subagent.py.
 *
 * Runs a bounded, one-level nested engine with an isolated message list and
 * returns a summary. The approval gate is never bypassed: a gated tool inside
 * the child stops it and returns the pending call for the caller to bubble to
 * the parent/human. One level deep, one at a time. Node-testable with a fake
 * `createEngine` (see test_webgpu_engine.mjs).
 */

import { createEngine } from "./engine.js";

const MAX_SUBAGENT_TURNS = 4;
const MAX_CONCURRENT_SUBAGENTS = 1;

const SUBAGENT_SYSTEM_PROMPT =
  "You are a bounded sub-agent of Agent Core. Work only on the stated goal, " +
  "use the available tools when needed, and finish by replying with a short, " +
  "self-contained summary of what you did and found. You cannot ask the user " +
  "questions; if a tool requires approval, stop and report it. Do not spawn " +
  "further sub-agents.";

const SUBAGENT_OK = "ok";
const SUBAGENT_NEEDS_APPROVAL = "needs_approval";
const SUBAGENT_REJECTED = "rejected";

function lastSummary(child) {
  for (let i = child.messages.length - 1; i >= 0; i--) {
    const m = child.messages[i];
    if (m.role === "assistant" && m.content && !(m.function_calls || []).length) {
      return String(m.content).trim();
    }
  }
  for (let i = child.messages.length - 1; i >= 0; i--) {
    const m = child.messages[i];
    if (m.role === "assistant" && m.content) return String(m.content).trim();
  }
  return "(the sub-agent produced no summary)";
}

async function spawnSubagent(parentState, goal, config, options = {}) {
  const text = String(goal || "").trim();
  if (!text) return { status: SUBAGENT_REJECTED, summary: "error: sub-agent goal is empty", turns: 0, pending: null };
  if ((parentState.active_subagents || 0) >= MAX_CONCURRENT_SUBAGENTS) {
    return {
      status: SUBAGENT_REJECTED,
      summary: "error: a sub-agent is already running (one level deep, one at a time)",
      turns: 0,
      pending: null,
    };
  }

  const maxTurns = options.maxTurns || MAX_SUBAGENT_TURNS;
  const build =
    config.createEngine ||
    ((extra) => createEngine({ ...config, ...extra }));
  const engine = build({
    session_id: parentState.session_id,
    model: parentState.model,
    target: parentState.target,
    max_turns: Math.max(1, maxTurns),
    max_context_tokens: config.max_context_tokens,
    permission_mode: parentState.permission_mode,
  });
  const child = engine.state;
  child.active_subagents = MAX_CONCURRENT_SUBAGENTS; /* one level deep */
  child.messages.push({ role: "system", content: SUBAGENT_SYSTEM_PROMPT });
  child.messages.push({ role: "user", content: text });

  parentState.active_subagents = (parentState.active_subagents || 0) + 1;
  try {
    await engine.start();
  } finally {
    parentState.active_subagents = Math.max(0, parentState.active_subagents - 1);
  }

  if (child.pending_approval) {
    return {
      status: SUBAGENT_NEEDS_APPROVAL,
      summary: `Sub-agent stopped: tool '${child.pending_approval.tool_name}' requires human approval.`,
      turns: child.turn_count,
      pending: child.pending_approval,
    };
  }
  return { status: SUBAGENT_OK, summary: lastSummary(child), turns: child.turn_count, pending: null };
}

export {
  MAX_SUBAGENT_TURNS,
  MAX_CONCURRENT_SUBAGENTS,
  SUBAGENT_SYSTEM_PROMPT,
  SUBAGENT_OK,
  SUBAGENT_NEEDS_APPROVAL,
  SUBAGENT_REJECTED,
  spawnSubagent,
};
