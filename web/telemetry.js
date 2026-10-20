"use strict";

/* Session telemetry — JS port of the pure parts of core/telemetry.py (Phase 12).
 *
 * Mirrors the Python summary/format contract so `/cost` (and the sidebar
 * readout) renders identically in the CLI, the web server, and the browser.
 * The browser path has no server-reported usage (Transformers.js does not
 * surface token counts), so state.usage stays empty and /cost reports that;
 * the host (CLI / web server) records real numbers via
 * core.telemetry.record_usage.
 *
 * /doctor is host-only — see web/commands.js.
 */

function asInt(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

function asFloat(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

/* Fold state.usage (array of TurnUsage records) into the summary dict that
 * mirrors core/telemetry.session_usage(state). */
function sessionUsage(state) {
  const records = Array.isArray(state && state.usage) ? state.usage : [];
  let prompt = 0;
  let completion = 0;
  let total = 0;
  let generateSeconds = 0;
  for (const record of records) {
    prompt += asInt(record.prompt_tokens);
    completion += asInt(record.completion_tokens);
    total += asInt(record.total_tokens);
    generateSeconds += asFloat(record.latency_s);
  }
  if (!total && (prompt || completion)) total = prompt + completion;
  const latency = records.length ? asFloat(records[records.length - 1].latency_s) : 0;
  const last = records.length ? asInt(records[records.length - 1].completion_tokens) : 0;
  return {
    turns: records.length,
    prompt_tokens: prompt,
    completion_tokens: completion,
    total_tokens: total,
    generate_seconds: Math.round(generateSeconds * 1000) / 1000,
    tokens_per_second: generateSeconds > 0 ? Math.round((completion / generateSeconds) * 100) / 100 : 0.0,
    last_tokens_per_second: latency > 0 ? Math.round((last / latency) * 100) / 100 : 0.0,
  };
}

/* Thousands-separated count: 1234 -> "1,234" (matches Python f"{n:,}"). */
function formatTokens(value) {
  const text = String(Math.max(0, Math.floor(asInt(value))));
  return text.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/* Render a sessionUsage summary for /cost — the same text the Python side
 * prints (test_webgpu_engine.mjs asserts the parity). */
function formatCost(report) {
  if (!report.turns) {
    return (
      "No usage recorded yet this session.\n" +
      "  Server-reported tokens appear after the first generation.\n" +
      "  Inference is local: nothing is metered (see /context for the budget)."
    );
  }
  const mean = report.tokens_per_second.toFixed(1);
  const last = report.last_tokens_per_second.toFixed(1);
  return (
    `Session usage: ${report.turns} generation(s)\n` +
    `  prompt tokens:      ${formatTokens(report.prompt_tokens)}\n` +
    `  completion tokens:  ${formatTokens(report.completion_tokens)}\n` +
    `  total tokens:       ${formatTokens(report.total_tokens)}\n` +
    `  generation time:    ${report.generate_seconds.toFixed(1)}s (mean ${mean} tok/s, last turn ${last} tok/s)\n` +
    "  inference cost:     $0.00 - local, no metered API calls"
  );
}

export { asInt, asFloat, sessionUsage, formatTokens, formatCost };
