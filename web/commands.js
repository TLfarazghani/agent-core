"use strict";

/* Shared slash-command registry — JS port of core/commands.py (Phase 8).
 *
 * Same contract as the Python table: a command is
 * { name, description, usage, run(state, arg) -> {text, error?, state?, clear?} }
 * and both the browser worker and Node tests consume the same table, so a
 * command behaves identically in the CLI, the web server, and the browser.
 *
 * Commands never bypass the approval gate: /approve and /reject call
 * engine.resolveApproval, which is the only path that executes a parked call.
 * /mode changes state.permission_mode, enforced in web/engine.js.
 */

import { estimateTokens } from "./context.js";
import { recallBounded } from "./memory.js";

const AGENT_NAME = "Agent Core";
const PERMISSION_MODES = ["default", "plan", "auto", "strict"];
const MODE_HELP = [
  "  default = ask for approval-gated tools",
  "  plan    = read-only tools only, everything else is parked",
  "  auto    = auto-approve gated tools except run_code",
  "  strict  = ask before every tool call",
  "run_code always requires explicit approval in every mode.",
];

function result(text, extra) {
  return { text, error: false, ...(extra || {}) };
}

function errorResult(text) {
  return { text, error: true };
}

const COMMANDS = {
  "/help": {
    name: "/help",
    description: "list commands",
    usage: "/help",
    run: () => result(helpText()),
  },
  "/mode": {
    name: "/mode",
    description: "get or set the permission mode",
    usage: "/mode [default|plan|auto|strict]",
    run: (state, arg) => {
      const wanted = String(arg || "").trim().toLowerCase();
      if (!wanted) {
        const listing = PERMISSION_MODES.map((m) =>
          m === state.permission_mode ? `${m}*` : m
        ).join(", ");
        return result(
          `Permission mode: ${state.permission_mode}  (${listing})\n` + MODE_HELP.join("\n")
        );
      }
      if (!PERMISSION_MODES.includes(wanted)) {
        return errorResult(`Usage: /mode [${PERMISSION_MODES.join("|")}]`);
      }
      state.permission_mode = wanted;
      return result(`Permission mode set to ${wanted}.`, { state });
    },
  },
  "/status": {
    name: "/status",
    description: "live self snapshot",
    usage: "/status",
    run: (state) =>
      result(
        JSON.stringify(
          {
            name: AGENT_NAME,
            session_id: state.session_id,
            target: state.target,
            model: state.model,
            turn_count: state.turn_count,
            max_turns: state.max_turns,
            estimated_context_tokens: estimateTokens(state.messages),
            pending_approval: state.pending_approval,
            plan: state.plan,
            permission_mode: state.permission_mode,
          },
          null,
          2
        )
      ),
  },
  "/context": {
    name: "/context",
    description: "token budget usage",
    usage: "/context",
    run: (state, arg, ctx) => {
      const used = estimateTokens(state.messages);
      const budget = (ctx && ctx.max_context_tokens) || 32768;
      const pct = budget ? ((used / budget) * 100).toFixed(1) : "0.0";
      return result(
        `Context: ~${used} tokens / ${budget} (${pct}%)\n` +
          `Messages: ${state.messages.length}  turns: ${state.turn_count}/${state.max_turns}  ` +
          `mode: ${state.permission_mode}`
      );
    },
  },
  "/plan": {
    name: "/plan",
    description: "render the current plan",
    usage: "/plan",
    run: (state) => {
      if (!state.plan) return result("No plan in progress.");
      const markers = {
        done: "\u2713",
        in_progress: "\u25b6",
        failed: "\u2717",
        pending: "\u00b7",
        skipped: "\u2212",
      };
      const lines = [`Plan: ${state.plan.goal}`];
      for (const step of state.plan.steps) {
        const suffix = step.result ? ` \u2014 ${step.result}` : "";
        lines.push(
          `  ${markers[step.status] || "\u00b7"} ${step.id} ${step.description} [${step.status}]${suffix}`
        );
      }
      return result(lines.join("\n"));
    },
  },
  "/memory": {
    name: "/memory",
    description: "recall long-term memory",
    usage: "/memory [topic]",
    run: (state, arg) => {
      const topic = String(arg || "").trim();
      const text = recallBounded(topic, 1024);
      if (!text) return result(`No memories found${topic ? ` for '${topic}'` : ""}.`);
      return result(`Memories:\n${text}`);
    },
  },
  "/turns": {
    name: "/turns",
    description: "set this session's turn budget",
    usage: "/turns <n>",
    run: (state, arg) => {
      const value = Number(String(arg || "").trim());
      if (!Number.isInteger(value) || value < 1) {
        return errorResult("Usage: /turns <n>  (positive integer)");
      }
      state.max_turns = value;
      return result(`Turn budget for this session set to ${value}.`, { state });
    },
  },
  "/new": {
    name: "/new",
    description: "start a fresh session",
    usage: "/new",
    run: () => result("New session started.", { clear: true }),
  },
  "/tools": {
    name: "/tools",
    description: "list registered tools",
    usage: "/tools",
    run: (state, arg, ctx) => {
      const tools = (ctx && ctx.tools) || {};
      const lines = ["Registered tools:"];
      for (const name of Object.keys(tools).sort()) {
        const tool = tools[name] || {};
        const flags = [];
        if (tool.requires_approval) flags.push("approval");
        if (tool.read_only) flags.push("read-only");
        lines.push(`  ${name}${flags.length ? `  [${flags.join(", ")}]` : ""}`);
      }
      return result(lines.join("\n"));
    },
  },
};

function parseCommand(line) {
  const trimmed = String(line || "").trim();
  const space = trimmed.indexOf(" ");
  if (space === -1) return { name: trimmed, arg: "" };
  return { name: trimmed.slice(0, space), arg: trimmed.slice(space + 1).trim() };
}

function helpText() {
  const lines = ["Commands:"];
  for (const command of Object.values(COMMANDS)) {
    lines.push(`  ${command.usage.padEnd(22)} ${command.description}`);
  }
  return lines.join("\n");
}

function runCommand(line, state, ctx) {
  const { name, arg } = parseCommand(line);
  const command = COMMANDS[name];
  if (!command) return errorResult(`Unknown command: ${name}`);
  try {
    return command.run(state, arg, ctx);
  } catch (err) {
    return errorResult(
      `Command failed: ${err && err.name ? err.name : "Error"}: ${err && err.message ? err.message : String(err)}`
    );
  }
}

export { COMMANDS, PERMISSION_MODES, helpText, parseCommand, runCommand };
