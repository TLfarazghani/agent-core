"use strict";

/* Context-window budget helpers — JS port of core/context.py.
 *
 * Line-for-line mirror of the Python contract so the browser target trims the
 * same way Windows does. Input messages are plain objects with the snake_case
 * shape { role, content, function_calls, tool_call_id }.
 *
 * Exported for web/engine.js (the in-browser loop). UMD is not needed here —
 * only the module worker imports this — but keep it free of browser APIs so it
 * stays Node-testable (see test_webgpu_engine.mjs).
 */

function messageText(m) {
  let text = (m && m.content) || "";
  const calls = (m && m.function_calls) || [];
  for (const call of calls) {
    text += (call && call.name) || "";
    text += JSON.stringify((call && call.arguments) || {});
  }
  text += (m && m.tool_call_id) || "";
  return text;
}

function estimateMessageTokens(m) {
  return Math.max(1, Math.ceil(messageText(m).length / 4));
}

function estimateTokens(messages) {
  let total = 0;
  for (const m of messages || []) total += estimateMessageTokens(m);
  return total;
}

function userIndices(messages) {
  const idx = [];
  for (let i = 0; i < messages.length; i++) {
    if (messages[i] && messages[i].role === "user") idx.push(i);
  }
  return idx;
}

/* Mirrors core/context.trim_to_budget. Returns { kept, dropped }. */
function trimToBudget(messages, budgetTokens) {
  if (!budgetTokens || budgetTokens <= 0) {
    return { kept: messages.slice(), dropped: [] };
  }
  const userIdx = userIndices(messages);
  if (!userIdx.length) return { kept: messages.slice(), dropped: [] };

  const prefixEnd = userIdx[0];
  const lastUser = userIdx[userIdx.length - 1];

  const turns = [];
  for (let k = 0; k < userIdx.length; k++) {
    const end = k + 1 < userIdx.length ? userIdx[k + 1] : messages.length;
    turns.push([userIdx[k], end]);
  }

  const alwaysStart = turns[turns.length - 1][0];
  const alwaysEnd = turns[turns.length - 1][1];
  let budgetLeft =
    budgetTokens -
    estimateTokens(messages.slice(0, prefixEnd)) -
    estimateTokens(messages.slice(alwaysStart, alwaysEnd));

  const keptIndices = new Set();
  for (let i = 0; i < prefixEnd; i++) keptIndices.add(i);
  for (let i = alwaysStart; i < alwaysEnd; i++) keptIndices.add(i);

  for (let t = turns.length - 2; t >= 0; t--) {
    const start = turns[t][0];
    const end = turns[t][1];
    const cost = estimateTokens(messages.slice(start, end));
    if (cost > budgetLeft) break;
    budgetLeft -= cost;
    for (let i = start; i < end; i++) keptIndices.add(i);
  }

  const kept = [];
  const dropped = [];
  messages.forEach((m, i) => (keptIndices.has(i) ? kept : dropped).push(m));
  return { kept, dropped };
}

export { estimateMessageTokens, estimateTokens, trimToBudget };