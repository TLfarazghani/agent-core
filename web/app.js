"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  sessionId: null,
  agentState: null,
  streaming: false,
  pendingApproval: false,
  liveBubble: null,
  transport: "server",
};

let browserWorker = null;

const els = {
  input: $("input"),
  send: $("send-btn"),
  messages: $("messages"),
  sessionList: $("session-list"),
  modelStatus: $("model-status"),
  newSession: $("new-session"),
  clearSession: $("clear-session"),
  deleteSession: $("delete-session"),
  turnCounter: $("turn-counter"),
  modal: $("approval-modal"),
  approvalTool: $("approval-tool"),
  approvalArgs: $("approval-args"),
  approve: $("approve-btn"),
  reject: $("reject-btn"),
  transportServer: $("transport-server"),
  transportBrowser: $("transport-browser"),
};

/* ---------- HTTP helpers ---------- */

async function fetchJSON(url, options) {
  const resp = await fetch(url, options);
  const body = await resp.json();
  if (!resp.ok) throw new Error(body.error || `HTTP ${resp.status}`);
  return body;
}

/* Parse a `text/event-stream` body from a POST response into events. */
async function consumeSSE(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const eventLine = block.split("\n").find((l) => l.startsWith("event: "));
      const dataLine = block.split("\n").find((l) => l.startsWith("data: "));
      if (!dataLine) continue;
      const event = eventLine ? eventLine.slice(7) : "message";
      let data;
      try { data = JSON.parse(dataLine.slice(6)); } catch { continue; }
      onEvent(event, data);
    }
  }
}

async function streamPost(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || `HTTP ${resp.status}`);
  }
  return consumeSSE(resp, handleEvent);
}

/* ---------- rendering ---------- */

function appendElement(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  els.messages.appendChild(el);
  els.messages.scrollTop = els.messages.scrollHeight;
  return el;
}

function renderMessages(agentState) {
  els.messages.innerHTML = "";
  state.agentState = agentState;
  const pendingCards = new Map();
  for (const message of agentState.messages || []) {
    if (message.role === "system") continue;
    if (message.role === "user") {
      appendElement("div", "msg user", message.content);
    } else if (message.role === "assistant") {
      if (message.content) appendElement("div", "msg assistant", message.content);
      for (const call of message.function_calls || []) {
        const card = renderToolCard(call);
        pendingCards.set(call.id, card);
      }
    } else if (message.role === "tool") {
      const card = pendingCards.get(message.tool_call_id);
      if (card) setToolResult(card, message.content);
      else appendElement("div", "tool-card").appendChild(
        Object.assign(document.createElement("div"), { className: "tool-result", textContent: message.content })
      );
    }
  }
  updateTurnCounter();
}

function renderToolCard(call) {
  const card = appendElement("div", "tool-card");
  const head = document.createElement("div");
  head.className = "tool-head";
  head.textContent = "\u27e6 tool: " + call.name + " \u27e7";
  card.appendChild(head);
  const args = document.createElement("div");
  args.className = "tool-args";
  args.textContent = JSON.stringify(call.arguments || {}, null, 2);
  card.appendChild(args);
  return card;
}

function setToolResult(card, content) {
  const result = document.createElement("div");
  result.className = "tool-result" + (content.startsWith("error") ? " error" : "");
  result.textContent = content;
  card.appendChild(result);
}

function startLiveBubble() {
  state.liveBubble = appendElement("div", "msg assistant live");
  return state.liveBubble;
}

function updateTurnCounter() {
  const s = state.agentState;
  els.turnCounter.textContent = s ? `turn ${s.turn_count}/${s.max_turns}` : "";
}

/* ---------- UI state ---------- */

function setBusy(busy) {
  state.streaming = busy;
  els.input.disabled = busy;
  els.send.disabled = busy;
  els.clearSession.disabled = busy;
  els.deleteSession.disabled = busy;
}

function showApproval(data) {
  state.pendingApproval = true;
  els.approvalTool.textContent = "\u26a0 " + data.tool_name;
  els.approvalArgs.textContent = JSON.stringify(data.arguments || {}, null, 2);
  els.modal.hidden = false;
  els.approve.focus();
}

function hideApproval() {
  state.pendingApproval = false;
  els.modal.hidden = true;
}

/* ---------- SSE event handling ---------- */

async function handleEvent(event, data) {
  switch (event) {
    case "status":
      if (state.transport === "browser" && data.detail) {
        if (data.detail === "model ready") {
          els.modelStatus.textContent = "\u25c9 in-browser \u00b7 ready";
          els.modelStatus.className = "";
        } else {
          els.modelStatus.textContent = "\u25c9 in-browser \u00b7 " + data.detail;
        }
      }
      break;
    case "token":
      if (!state.liveBubble) startLiveBubble();
      state.liveBubble.textContent += data.text;
      els.messages.scrollTop = els.messages.scrollHeight;
      break;
    case "tool_call":
      if (state.liveBubble) { state.liveBubble.classList.remove("live"); state.liveBubble = null; }
      renderToolCard(data);
      break;
    case "tool_result":
      break;
    case "approval":
      if (state.liveBubble) { state.liveBubble.classList.remove("live"); state.liveBubble = null; }
      showApproval(data);
      break;
    case "error":
      if (state.liveBubble) { state.liveBubble.classList.remove("live"); state.liveBubble = null; }
      appendElement("div", "msg error", data.message);
      setBusy(false);
      break;
    case "done":
      if (state.liveBubble) { state.liveBubble.classList.remove("live"); state.liveBubble = null; }
      renderMessages(data.state);
      setBusy(false);
      refreshSessions();
      break;
  }
}

/* ---------- browser (WebGPU) transport ---------- */

function startBrowserWorker() {
  if (browserWorker) return;
  browserWorker = new Worker("worker.js", { type: "module" });
  browserWorker.onmessage = (e) => handleEvent(e.data.type, e.data);
  browserWorker.onerror = (e) => {
    const where = [e.filename, e.lineno, e.colno].filter(Boolean).join(":");
    appendElement(
      "div",
      "msg error",
      `browser worker error: ${e.message || "failed to load worker.js"}${where ? " (" + where + ")" : ""}`
    );
    els.modelStatus.textContent = "\u25c9 in-browser \u00b7 worker failed";
    els.modelStatus.className = "";
    setBusy(false);
  };
  browserWorker.postMessage({ type: "init" });
}

function stopBrowserWorker() {
  if (browserWorker) {
    browserWorker.terminate();
    browserWorker = null;
  }
}

function setTransport(t) {
  state.transport = t;
  els.transportServer.classList.toggle("active", t === "server");
  els.transportBrowser.classList.toggle("active", t === "browser");
  hideApproval();
  els.messages.innerHTML = "";
  state.liveBubble = null;
  state.agentState = null;
  state.sessionId = null;
  updateTurnCounter();
  if (t === "server") {
    stopBrowserWorker();
    els.modelStatus.className = "";
    refreshHealth();
    refreshSessions();
    createSession();
  } else {
    startBrowserWorker();
    els.modelStatus.textContent = "\u25c9 in-browser \u00b7 loading model\u2026";
    els.modelStatus.className = "busy";
    els.sessionList.innerHTML = "<li>in-browser session (not persisted)</li>";
  }
}

/* ---------- actions ---------- */

async function refreshSessions() {
  if (state.transport !== "server") return;
  try {
    const { sessions } = await fetchJSON("/api/sessions");
    els.sessionList.innerHTML = "";
    for (const s of sessions) {
      const li = document.createElement("li");
      li.textContent = (s.preview || s.session_id) + `  (${s.turn_count}/${s.max_turns})`;
      li.title = s.session_id;
      if (s.session_id === state.sessionId) li.className = "active";
      li.addEventListener("click", () => selectSession(s.session_id));
      els.sessionList.appendChild(li);
    }
  } catch (err) {
    els.sessionList.innerHTML = `<li>failed to load sessions</li>`;
  }
}

async function selectSession(id) {
  if (state.streaming || state.pendingApproval) return;
  if (state.transport !== "server") return;
  try {
    const { state: agentState } = await fetchJSON(`/api/sessions/${id}`);
    state.sessionId = id;
    renderMessages(agentState);
    refreshSessions();
  } catch (err) {
    appendElement("div", "msg error", "could not load session: " + err.message);
  }
}

async function createSession() {
  hideApproval();
  if (state.transport === "browser") {
    if (!browserWorker) startBrowserWorker();
    browserWorker.postMessage({ type: "reset" });
    return;
  }
  try {
    const { session_id } = await fetchJSON("/api/sessions", { method: "POST" });
    state.sessionId = session_id;
    els.messages.innerHTML = "";
    updateTurnCounter();
    refreshSessions();
    els.input.focus();
  } catch (err) {
    appendElement("div", "msg error", "could not create session: " + err.message);
  }
}

async function clearSession() {
  if (state.streaming || state.pendingApproval) return;
  if (!confirm("Clear the chat history of this session?")) return;
  if (state.transport === "browser") {
    browserWorker.postMessage({ type: "reset" });
    return;
  }
  if (!state.sessionId) return;
  try {
    const { state: agentState } = await fetchJSON(`/api/sessions/${state.sessionId}/clear`, {
      method: "POST",
    });
    renderMessages(agentState);
    refreshSessions();
    els.input.focus();
  } catch (err) {
    appendElement("div", "msg error", "could not clear history: " + err.message);
  }
}

async function deleteCurrentSession() {
  if (state.streaming || state.pendingApproval) return;
  if (state.transport === "browser") {
    clearSession();
    return;
  }
  if (!state.sessionId) return;
  if (!confirm("Delete this session permanently?")) return;
  try {
    await fetchJSON(`/api/sessions/${state.sessionId}`, { method: "DELETE" });
    await createSession();
  } catch (err) {
    appendElement("div", "msg error", "could not delete session: " + err.message);
  }
}

async function sendMessage() {
  if (state.streaming || state.pendingApproval) return;
  const text = els.input.value.trim();
  if (!text) return;
  if (state.transport === "browser") {
    els.input.value = "";
    appendElement("div", "msg user", text);
    setBusy(true);
    if (!browserWorker) startBrowserWorker();
    browserWorker.postMessage({ type: "chat", text });
    return;
  }
  if (!state.sessionId) return;
  els.input.value = "";
  appendElement("div", "msg user", text);
  setBusy(true);
  try {
    await streamPost(`/api/sessions/${state.sessionId}/messages`, { message: text });
  } catch (err) {
    appendElement("div", "msg error", err.message);
    setBusy(false);
  }
}

async function approveAction(approved) {
  if (!state.pendingApproval) return;
  hideApproval();
  setBusy(true);
  if (state.transport === "browser") {
    if (!browserWorker) startBrowserWorker();
    browserWorker.postMessage({ type: approved ? "approve" : "reject" });
    return;
  }
  if (!state.sessionId) return;
  try {
    await streamPost(`/api/sessions/${state.sessionId}/${approved ? "approve" : "reject"}`, {});
  } catch (err) {
    appendElement("div", "msg error", err.message);
    setBusy(false);
  }
}

async function refreshHealth() {
  try {
    const h = await fetchJSON("/api/health");
    const dot = els.modelStatus.querySelector(".dot");
    dot.className = "dot " + (h.ok ? "dot-on" : "dot-off");
    els.modelStatus.appendChild(document.createTextNode(" " + (h.ok ? "local \u00b7 online" : "local \u00b7 model OFFLINE")));
    if (!h.ok) appendElement("div", "msg error", "Model server offline. Start it with .\\windows\\server_config.ps1");
  } catch (err) {
    els.modelStatus.textContent = "status: unreachable";
  }
}

/* ---------- wiring ---------- */

els.send.addEventListener("click", sendMessage);
els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) sendMessage();
});
els.newSession.addEventListener("click", createSession);
els.clearSession.addEventListener("click", clearSession);
els.deleteSession.addEventListener("click", deleteCurrentSession);
els.approve.addEventListener("click", () => approveAction(true));
els.reject.addEventListener("click", () => approveAction(false));
els.transportServer.addEventListener("click", () => setTransport("server"));
els.transportBrowser.addEventListener("click", () => setTransport("browser"));
document.addEventListener("keydown", (e) => {
  if (!state.pendingApproval) return;
  if (e.key === "a" || e.key === "A") approveAction(true);
  else if (e.key === "r" || e.key === "R") approveAction(false);
  else if (e.key === "Escape") approveAction(false);
});

(async function init() {
  await refreshHealth();
  await refreshSessions();
  await createSession();
})();
