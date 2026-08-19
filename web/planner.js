"use strict";

/* Planning & task tracking — JS port of core/planner.py.
 *
 * Mirrors the Python contract exactly: makePlan sets state.plan from a goal +
 * ordered step descriptions, updatePlan flips one step's status. Plan steps
 * are bookkeeping only — every tool call still hits the engine's hardcoded
 * approval gate (web/engine.js), never bypassed by the plan.
 */

const VALID_STATUSES = ["pending", "in_progress", "done", "failed", "skipped"];

function makePlan(state, goal, steps) {
  state.plan = {
    goal: String(goal).trim(),
    steps: steps.map((desc, i) => ({
      id: `step_${i + 1}`,
      description: String(desc).trim(),
      status: "pending",
      result: null,
    })),
  };
  return state.plan;
}

function updatePlan(state, stepId, status, result) {
  if (!state.plan) throw new Error("no plan in progress; call make_plan first");
  if (!VALID_STATUSES.includes(status)) {
    throw new Error(`invalid step status: ${JSON.stringify(status)}`);
  }
  for (const step of state.plan.steps) {
    if (step.id === stepId) {
      step.status = status;
      if (result !== undefined && result !== null) step.result = result;
      return step;
    }
  }
  throw new Error(`no plan step with id ${JSON.stringify(stepId)}`);
}

export { VALID_STATUSES, makePlan, updatePlan };