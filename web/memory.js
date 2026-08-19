"use strict";

/* Long-term memory — JS port of core/memory.py.
 *
 * Same contract as the Python store: entries are
 * { key, content, kind: fact|preference|lesson|session_summary, created_at,
 * source_session }, keys get the same traversal guard, and recall is trimmed
 * to the Phase 6 context budget via web/context.js.
 *
 * Storage is an injected adapter (defaults to the worker's localStorage):
 * anything with getItem/setItem/key/length. Node tests pass a plain
 * in-memory object.
 */

import { estimateMessageTokens } from "./context.js";

const KINDS = ["fact", "preference", "lesson", "session_summary"];
const PREFIX = "agent-core:memory:";

function defaultStorage() {
  if (typeof globalThis !== "undefined" && globalThis.localStorage) {
    return globalThis.localStorage;
  }
  return null;
}

function memoryStorageKey(key) {
  if (!key || key === "." || key === "..") {
    throw new Error(`invalid memory key: ${JSON.stringify(key)}`);
  }
  if (key.includes("/") || key.includes("\\") || key.includes(":")) {
    throw new Error(`invalid memory key: ${JSON.stringify(key)}`);
  }
  const lowered = key.toLowerCase();
  if (lowered.includes("%2f") || lowered.includes("%5c")) {
    throw new Error(`invalid memory key: ${JSON.stringify(key)}`);
  }
  return PREFIX + key;
}

function saveMemory(entry, storage = defaultStorage()) {
  if (!storage) throw new Error("no memory storage available");
  const key = entry.key;
  const kind = entry.kind || "fact";
  if (!KINDS.includes(kind)) throw new Error(`invalid memory kind: ${JSON.stringify(kind)}`);
  const record = {
    key,
    content: entry.content,
    kind,
    created_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
    source_session: entry.source_session || null,
  };
  storage.setItem(memoryStorageKey(key), JSON.stringify(record));
  return record;
}

function listMemories(storage = defaultStorage()) {
  if (!storage) return [];
  const entries = [];
  for (let i = 0; i < storage.length; i++) {
    const k = storage.key(i);
    if (!k || !k.startsWith(PREFIX)) continue;
    try {
      entries.push(JSON.parse(storage.getItem(k)));
    } catch (_) {
      /* skip corrupt entries */
    }
  }
  entries.sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  return entries;
}

function recallMemories(topic = "", limit = 5, storage = defaultStorage()) {
  const topicLower = String(topic || "").toLowerCase();
  const matches = [];
  const entries = listMemories(storage).reverse(); // newest first
  for (const entry of entries) {
    const haystack = `${entry.key} ${entry.content} ${entry.kind}`.toLowerCase();
    if (!topicLower || haystack.includes(topicLower)) {
      matches.push(entry);
      if (matches.length >= limit) break;
    }
  }
  return matches;
}

function recallBounded(topic = "", maxTokens = 512, storage = defaultStorage()) {
  const entries = recallMemories(topic, 20, storage);
  if (!entries.length) return "";
  const lines = [];
  let used = 0;
  for (const entry of entries) {
    const line = `[${entry.kind}] ${entry.key}: ${entry.content}`;
    const cost = estimateMessageTokens({ role: "system", content: line });
    if (lines.length && used + cost > maxTokens) break;
    lines.push(line);
    used += cost;
  }
  return lines.join("\n");
}

export { KINDS, listMemories, recallBounded, recallMemories, saveMemory };