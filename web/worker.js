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

const { parse_tool_calls } = self.AgentParser;

const TRANSFORMERS_CDN =
  "https://cdn.jsdelivr.net/npm/@huggingface/transformers@4.2.0";
const PYODIDE_CDN = "https://cdn.jsdelivr.net/pyodide/v314.0.5/full/pyodide.mjs";

const MODEL_ID = "LiquidAI/LFM2.5-1.2B-Instruct-ONNX";

const SYSTEM_PROMPT =
  "You are a helpful local assistant running entirely in your browser. " +
  "You have access to a small set of tools. When the user asks you to run or " +
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
];

let modelInstance = null;
let modelPromise = null;
let pyodideInstance = null;
let engine = null;

function post(type, data) {
  self.postMessage(Object.assign({ type }, data || {}));
}

function resetSession() {
  engine = createEngine({
    provider: { generate },
    tools: BROWSER_TOOLS,
    parser: { parse_tool_calls },
    model: MODEL_ID,
  });
  engine.state.messages.push({ role: "system", content: SYSTEM_PROMPT });
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
        resetSession();
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