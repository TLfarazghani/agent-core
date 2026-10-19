"use strict";

/* Phase 11 task backlog — JS port of core/tasks.py.
 *
 * Pure and Node-testable (see test_webgpu_engine.mjs). Task state lives on
 * AgentState.tasks and serializes with the session, mirroring the Python core.
 * Bookkeeping only: a task's work still passes the approval gate.
 */

const VALID_TASK_STATUSES = ["pending", "in_progress", "done", "failed", "skipped"];

function nextId(state) {
  let highest = 0;
  for (const task of state.tasks || []) {
    const digits = String(task.id || "").replace(/^task_/, "");
    if (/^\d+$/.test(digits)) highest = Math.max(highest, parseInt(digits, 10));
  }
  return `task_${highest + 1}`;
}

function createTask(state, description) {
  const text = String(description || "").trim();
  if (!text) throw new Error("task description must be a non-empty string");
  const task = { id: nextId(state), description: text, status: "pending", result: null };
  if (!Array.isArray(state.tasks)) state.tasks = [];
  state.tasks.push(task);
  return task;
}

function updateTask(state, taskId, status, result = null) {
  if (!VALID_TASK_STATUSES.includes(status)) {
    throw new Error(`invalid task status: ${JSON.stringify(status)}`);
  }
  for (const task of state.tasks || []) {
    if (task.id === taskId) {
      task.status = status;
      if (result !== null && result !== undefined) task.result = result;
      return task;
    }
  }
  throw new Error(`no task with id ${JSON.stringify(taskId)}`);
}

function listTasks(state) {
  return (state.tasks || []).slice();
}

function renderTasks(state) {
  const tasks = state.tasks || [];
  if (!tasks.length) return "No tasks.";
  const markers = { done: "✓", in_progress: "▶", failed: "✗", pending: "·", skipped: "−" };
  const lines = ["Tasks:"];
  for (const task of tasks) {
    const suffix = task.result ? ` — ${task.result}` : "";
    lines.push(
      `  ${markers[task.status] || "·"} ${task.id} ${task.description} [${task.status}]${suffix}`
    );
  }
  return lines.join("\n");
}

export { VALID_TASK_STATUSES, createTask, updateTask, listTasks, renderTasks };
