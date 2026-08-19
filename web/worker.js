"use strict";

/* In-browser agent worker (WebGPU / Transformers.js + Pyodide).
 *
 * Runs the agent fully in the tab: the pure loop lives in engine.js (the JS
 * port of core/loop.py) and the tool-call grammar in parser.js (the JS port
 * of core/parser.py) -- nothing here re-implements the loop or the grammar.
 * This file only adapts:
 *   - main-thread messages   -> engine events (chat / approve / reject / reset)
 *   - engine events          -> main-thread messages (token / tool_call /
 *                               tool_result / approval / error / done)
 *   - a provider             -> Transformers.js model.generate + TextStreamer
 *   - the run_code tool      -> Pyodide (WASM python, already tab-sandboxed)
 *
 * MUST be created as a module worker:
 *   new Worker("worker.js", { type: "module" })
 * (Transformers.js v4 is ESM-only from a CDN.)
 *
 * Pinned: @huggingface/transformers@4.2.0 and Pyodide v314.0.5. WebGPU is
 * attempted first with an automatic WASM fallback (Transformers.js picks the
 * backend itself unless device is forced -- we prefer "webgpu" and retry on
 * "wasm" if the GPU adapter is unusable).
 *
 * Protocol (main -> worker):
 *   { type: "init",   model_id, dtype?, device? }
 *   { type: "chat",   text }
 *   { type: "approve" } | { type: "reject" }
 *   { type: "reset" }
 * (worker -> main):
 *   { type: "status", detail }            -- lifecycle / progress
 *   { type: "token",  text }              -- streamed assistant tokens
 *   { type: "tool_call",  id, name, arguments }
 *   { type: "tool_result", call_id, content, error }
 *   { type: "approval", call_id, tool_name, arguments }
 *   { type: "error", message }
 *   { type: "done",   state }             -- serialized AgentState
 */

import "./parser.js";
import { createEngine } from "./engine.js";
import { estimateTokens } from "./context.js";
import { recallBounded, saveMemory as memorySave } from "./memory.js";
import { makePlan as planMake, updatePlan as planUpdate } from "./planner.js";

const { parse_tool_calls } = self.AgentParser;

const TRANSFORMERS_CDN =
  "https://cdn.jsdelivr.net/npm/@huggingface/transformers@4.2.0";
const PYODIDE_CDN = "https://cdn.jsdelivr.net/pyodide/v314.0.5/full/pyodide.mjs";

const AGENT_NAME = "Agent Core";
const MODEL_ID = "LiquidAI/LFM2.5-1.2B-Instruct-ONNX";
const ONNX_CONTEXT_TOKENS = 32768;

const SYSTEM_PROMPT =
  `You are ${AGENT_NAME}, a local, privacy-first personal assistant running ` +
  "entirely in your browser. Your capabilities: web search (web_search), " +
  "fetching pages (fetch_url), running code in a sandbox (run_code), and " +
  "long-term memory across sessions (remember, recall). You can introspect " +
  "yourself (inspect_self) and track multi-step tasks (make_plan, " +
  "update_plan). Your limits: you run locally with no external APIs beyond " +
  "web search, and run_code always asks the user for approval. " +
  "When the user asks you to run or " +
  "execute code, you MUST call the run_code tool (python only). When the user " +
  "asks for news, recent information, facts, or research, call the web_search " +
  "tool (kind='news' for headlines, kind='wikipedia' for encyclopedia entries). " +
  "If a search snippet is not enough and you need the full page content, call " +
  "fetch_url. If the user just asks a question, answer directly without tools. " +
  "Tool calls use this exact format:\n" +
  "<|tool_call_start|>web_search(query=\"...\", kind=\"web\")<|tool_call_end|>";

const BROWSER_TOOLS = {
  echo: {
    requires_approval: false,
    run: async (args) => `echo: ${args.text}`,
  },
  run_code: {
    requires_approval: true,
    run: async (args) => runPyodide(args),
  },
  web_search: {
    requires_approval: false,
    run: async (args) => {
      const params = new URLSearchParams({ q: args.query });
      if (args.kind) params.set("kind", args.kind);
      if (args.max_results) params.set("max_results", String(args.max_results));
      const resp = await fetch(`/api/search?${params}`);
      const data = await resp.json();
      if (data.error) return `error: ${data.error}`;
      return data.result;
    },
  },
  fetch_url: {
    requires_approval: false,
    run: async (args) => {
      const params = new URLSearchParams({ url: args.url });
      if (args.max_chars) params.set("max_chars", String(args.max_chars));
      const resp = await fetch(`/api/fetch?${params}`);
      const data = await resp.json();
      if (data.error) return `error: ${data.error}`;
      return data.result;
    },
  },
  inspect_self: {
    requires_approval: false,
    stateful: true,
    run: async (state, args) => {
      const snapshot = {
        name: AGENT_NAME,
        session_id: state.session_id,
        target: state.target,
        model: state.model,
        turn_count: state.turn_count,
        max_turns: state.max_turns,
        estimated_context_tokens: estimateTokens(state.messages),
        max_context_tokens: ONNX_CONTEXT_TOKENS,
        pending_approval: state.pending_approval,
        plan: state.plan,
        tools: Object.keys(BROWSER_TOOLS).sort(),
      };
      return JSON.stringify(snapshot, null, 2);
    },
  },
  remember: {
    requires_approval: false,
    stateful: true,
    run: async (state, args) => {
      const kind = args.kind || "fact";
      if (!["fact", "preference", "lesson", "session_summary"].includes(kind)) {
        return `error: invalid kind '${kind}'`;
      }
      try {
        const entry = memorySave({
          key: args.key,
          content: args.content,
          kind,
          source_session: state.session_id,
        });
        return `remembered: [${entry.kind}] ${entry.key}: ${entry.content}`;
      } catch (err) {
        return `error: ${err && err.message ? err.message : String(err)}`;
      }
    },
  },
  recall: {
    requires_approval: false,
    stateful: true,
    run: async (state, args) => {
      const topic = args.topic || "";
      const limit = args.limit || 5;
      const text = recallBounded(topic, 512);
      const lines = text.split("\n").filter((l) => l);
      if (!lines.length) return `no memories found for topic '${topic}'`;
      return lines.slice(0, limit).join("\n");
    },
  },
  make_plan: {
    requires_approval: false,
    stateful: true,
    run: async (state, args) => {
      let steps = args.steps;
      if (typeof steps === "string") steps = [steps];
      if (!Array.isArray(steps) || !steps.length || !steps.every((s) => typeof s === "string" && s.trim())) {
        return "error: make_plan requires a non-empty list of step descriptions";
      }
      const plan = planMake(state, args.goal, steps);
      return `plan set: ${JSON.stringify(plan, null, 2)}`;
    },
  },
  update_plan: {
    requires_approval: false,
    stateful: true,
    run: async (state, args) => {
      try {
        const step = planUpdate(state, args.step_id, args.status, args.result);
        return `step ${step.id} -> ${step.status}`;
      } catch (err) {
        return `error: ${err && err.message ? err.message : String(err)}`;
      }
    },
  },
};

/* Tool schemas handed to the model through apply_chat_template, mirroring
 * tools/registry.json (subset available in-browser). */
const TOOL_SCHEMAS = [
  {
    type: "function",
    function: {
      name: "echo",
      description: "Return the given text verbatim.",
      parameters: {
        type: "object",
        properties: { text: { type: "string" } },
        required: ["text"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "run_code",
      description:
        "Execute arbitrary python code in a sandbox. Always requires human approval.",
      parameters: {
        type: "object",
        properties: {
          language: { type: "string", enum: ["python"] },
          code: { type: "string" },
          timeout_seconds: { type: "integer", minimum: 1 },
        },
        required: ["language", "code"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "web_search",
      description:
        "Search the web and return top results. kind='web' for general web search, kind='news' for latest headlines, kind='wikipedia' for encyclopedia entries.",
      parameters: {
        type: "object",
        properties: {
          query: { type: "string", minLength: 1 },
          kind: { type: "string", enum: ["web", "news", "wikipedia"] },
          max_results: { type: "integer", minimum: 1, maximum: 10 },
        },
        required: ["query"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "fetch_url",
      description:
        "Fetch a URL and return its text content as trimmed plain text. Use when a search snippet is not enough and you need the actual page content.",
      parameters: {
        type: "object",
        properties: {
          url: { type: "string", minLength: 1 },
          max_chars: { type: "integer", minimum: 200, maximum: 20000 },
        },
        required: ["url"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "inspect_self",
      description:
        "Return a live snapshot of the agent: name, session id, target, model, turn budget, estimated context usage, pending approval, current plan, and available tools.",
      parameters: { type: "object", properties: {}, required: [] },
    },
  },
  {
    type: "function",
    function: {
      name: "remember",
      description:
        "Save a fact, preference, or lesson to long-term memory so it persists across sessions. kind is one of: fact, preference, lesson, session_summary.",
      parameters: {
        type: "object",
        properties: {
          key: { type: "string", minLength: 1 },
          content: { type: "string", minLength: 1 },
          kind: { type: "string", enum: ["fact", "preference", "lesson", "session_summary"] },
        },
        required: ["key", "content"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "recall",
      description: "Search long-term memory for entries matching a topic. Empty topic recalls the most recent memories.",
      parameters: {
        type: "object",
        properties: {
          topic: { type: "string" },
          limit: { type: "integer", minimum: 1, maximum: 20 },
        },
        required: [],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "make_plan",
      description:
        "Set an explicit multi-step plan for the current task (goal + ordered step descriptions). Each step that calls a tool still requires normal approval.",
      parameters: {
        type: "object",
        properties: {
          goal: { type: "string", minLength: 1 },
          steps: { type: "array", items: { type: "string", minLength: 1 }, minItems: 1 },
        },
        required: ["goal", "steps"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "update_plan",
      description:
        "Mark a plan step as in_progress, done, failed, or skipped (optionally with a short result).",
      parameters: {
        type: "object",
        properties: {
          step_id: { type: "string", minLength: 1 },
          status: { type: "string", enum: ["pending", "in_progress", "done", "failed", "skipped"] },
          result: { type: "string" },
        },
        required: ["step_id", "status"],
      },
    },
  },
];

let modelInstance = null;
let modelPromise = null;
let pyodideInstance = null;
let engine = null;

function post(type, data) {
  self.postMessage(Object.assign({ type }, data || {}));
}

function agentBio(state) {
  const tools = Object.keys(BROWSER_TOOLS).sort().join(", ");
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const iso = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ` +
    `${pad(now.getHours())}:${pad(now.getMinutes())}`;
  return (
    `You are ${AGENT_NAME}.\n` +
    `Current date/time: ${iso}.\n` +
    `Target: ${state.target}. Model: ${state.model}.\n` +
    `Context budget: ${estimateTokens(state.messages)} ~tokens / ${ONNX_CONTEXT_TOKENS}.\n` +
    `Turn budget: ${state.turn_count}/${state.max_turns} used.\n` +
    `Available tools: ${tools}.\n` +
    "You answer questions about yourself from this block; never invent " +
    "capabilities you do not have."
  );
}

function resetSession(maxTurns) {
  engine = createEngine({
    provider: { generate },
    tools: BROWSER_TOOLS,
    parser: { parse_tool_calls },
    model: MODEL_ID,
    max_turns: Number.isInteger(maxTurns) && maxTurns >= 1 ? maxTurns : 8,
    max_context_tokens: ONNX_CONTEXT_TOKENS,
    memory: { saveMemory: memorySave },
  });
  engine.state.messages.push({ role: "system", content: SYSTEM_PROMPT });
  const recall = recallBounded("", 512);
  if (recall) {
    engine.state.messages.push({ role: "system", content: `Prior knowledge:\n${recall}` });
  }
  engine.state.messages.push({ role: "system", content: agentBio(engine.state) });
  return engine;
}

/* ---------- Transformers.js model ---------- */

async function loadModel(device = "webgpu", dtype = "q4") {
  if (modelInstance) return modelInstance;
  if (modelPromise) return modelPromise;

  post("status", { detail: `loading Transformers.js (${dtype} / ${device})...` });
  const { AutoModelForCausalLM, AutoTokenizer, env } = await import(
    TRANSFORMERS_CDN
  );

  /* Prefer a locally-downloaded model: if the ONNX files sit under the
   * repo's models/ dir (served here at /models/), load them from disk --
   * zero network. Only when they are missing do we download from HF.
   * allowLocalModels defaults to false in-browser, so opt back in. */
  env.localModelPath = "/models/";
  env.allowLocalModels = true;

  const build = async (dev, dt, localOnly) => {
    const opts = {
      dtype: dt,
      device: dev,
      local_files_only: localOnly,
      progress_callback: (p) => onProgress(p),
    };
    const tokenizer = await AutoTokenizer.from_pretrained(MODEL_ID, opts);
    const model = await AutoModelForCausalLM.from_pretrained(MODEL_ID, opts);
    return { model, tokenizer };
  };

  modelPromise = (async () => {
    try {
      return await build(device, dtype, true);
    } catch (err) {
      post("status", {
        detail: "model not found in local models/, downloading from Hugging Face...",
      });
      try {
        return await build(device, dtype, false);
      } catch (err2) {
        if (device !== "wasm") {
          post("status", {
            detail: `WebGPU unavailable (${err2 && err2.message ? err2.message : err2}), falling back to WASM...`,
          });
          return build("wasm", "q4", false);
        }
        throw err2;
      }
    }
  })();

  modelInstance = await modelPromise;
  modelPromise = null;
  return modelInstance;
}

function onProgress(p) {
  if (p && (p.status === "progress" || p.status === "progress_total")) {
    const file = p.file || "";
    const loaded = p.loaded || 0;
    const total = p.total || 1;
    post("status", {
      detail: `downloading ${file} ${Math.round((loaded / total) * 100)}%`,
    });
  }
}

/* Chat template -> tokenized input. Assistant/tool messages pass their raw
 * content; tool-call extraction happens back in the engine via parser.js. */
async function generate(messages, onToken) {
  const { model, tokenizer } = await loadModel();
  const chat = messages.map((m) => ({
    role: m.role,
    content: m.content || "",
  }));
  const input = tokenizer.apply_chat_template(chat, {
    tools: TOOL_SCHEMAS,
    add_generation_prompt: true,
    return_dict: true,
  });

  let text = "";
  const { TextStreamer } = await import(TRANSFORMERS_CDN);
  const streamer = new TextStreamer(tokenizer, {
    skip_prompt: true,
    skip_special_tokens: false,
    callback_function: (chunk) => {
      text += chunk;
      if (onToken) onToken(chunk);
    },
  });

  await model.generate({
    ...input,
    max_new_tokens: 512,
    do_sample: false,
    streamer,
  });

  /* strip trailing EOS variants left in the streamed text */
  const content = text.replace(/<\/s>\s*$/, "").replace(/<\|im_end\|>\s*$/, "");
  return { role: "assistant", content };
}

/* ---------- run_code: Pyodide ---------- */

async function getPyodide() {
  if (pyodideInstance) return pyodideInstance;
  post("status", { detail: "loading Pyodide (python for the browser)..." });
  const { loadPyodide } = await import(PYODIDE_CDN);
  pyodideInstance = await loadPyodide({ indexURL: PYODIDE_CDN.replace(/pyodide\.mjs$/, "") });
  post("status", { detail: "Pyodide ready." });
  return pyodideInstance;
}

async function runPyodide(args) {
  const language = args.language || "python";
  if (language !== "python") {
    return `error: in-browser run_code supports python only (Pyodide); got '${language}'`;
  }
  const code = String(args.code || "");
  const timeoutMs = (args.timeout_seconds || 30) * 1000;
  const pyodide = await getPyodide();

  let stdout = "";
  const prevStdout = pyodide.setStdout
    ? pyodide.setStdout({ batched: (text) => (stdout += text) })
    : null;

  const interruptBuffer = new Int32Array([1]);
  pyodide.setInterruptBuffer(interruptBuffer);

  let timer = null;
  const watchdog = new Promise((_, reject) => {
    timer = setTimeout(() => {
      interruptBuffer[0] = 2; // raise KeyboardInterrupt
      reject(new Error(`run_code timed out after ${timeoutMs / 1000}s`));
    }, timeoutMs);
  });

  try {
    const result = await Promise.race([pyodide.runPythonAsync(code), watchdog]);
    if (prevStdout) pyodide.setStdout(prevStdout);
    const out = stdout.trim();
    return out || (result === undefined ? "ok" : String(result));
  } catch (err) {
    if (prevStdout) pyodide.setStdout(prevStdout);
    const detail = stdout.trim() || (err && err.message ? err.message : String(err));
    return `error: ${detail}`;
  } finally {
    if (timer) clearTimeout(timer);
    pyodide.setInterruptBuffer(null);
  }
}

/* ---------- message handling ---------- */

self.onmessage = async (event) => {
  const msg = event.data || {};
  try {
    switch (msg.type) {
      case "init":
        await loadModel(msg.device, msg.dtype);
        post("status", { detail: "model ready" });
        break;
      case "chat": {
        if (!engine) resetSession();
        engine.addUserMessage(String(msg.text));
        await engine.start((name, data) => post(name, data));
        break;
      }
      case "approve":
      case "reject": {
        if (!engine) throw new Error("no active session; start a chat first");
        await engine.resolveApproval(msg.type === "approve");
        break;
      }
      case "reset":
        resetSession(msg.max_turns);
        post("done", { state: JSON.parse(JSON.stringify(engine.state)) });
        break;
      default:
        throw new Error(`unknown message type: ${msg.type}`);
    }
  } catch (err) {
    post("error", {
      message: `error: ${err && err.name ? err.name : "Error"}: ${err && err.message ? err.message : String(err)}`,
    });
  }
};