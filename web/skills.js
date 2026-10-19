"use strict";

/* Phase 10 skills — JS port of tools/skills.py.
 *
 * A skill is `{ name, description, prompt, tools? }`; activating it sets
 * AgentState.tool_allowlist for the turn (enforced in web/engine.js) and injects
 * its prompt. A tool allowlist, not an approval bypass: allowed calls still run
 * the gate. Pure and Node-testable (see test_webgpu_engine.mjs).
 */

function normalizeSkill(raw, fallbackName = "") {
  if (!raw || typeof raw !== "object") return null;
  const name = String(raw.name || fallbackName || "").trim();
  if (!name) return null;
  const tools = Array.isArray(raw.tools) ? raw.tools.map(String) : null;
  return {
    name,
    description: String(raw.description || ""),
    prompt: String(raw.prompt || ""),
    tools,
  };
}

function loadSkills(rawSkills) {
  const skills = {};
  for (const raw of rawSkills || []) {
    const skill = normalizeSkill(raw);
    if (skill) skills[skill.name] = skill;
  }
  return skills;
}

function activateSkill(state, name, skills) {
  const skill = (skills || {})[name];
  if (!skill) {
    const available = Object.keys(skills || {}).sort().join(", ") || "none";
    return `error: unknown skill '${name}' (available: ${available})`;
  }
  state.tool_allowlist = skill.tools ? skill.tools.slice() : null;
  state.messages.push({ role: "system", content: `Skill '${skill.name}' active: ${skill.prompt}` });
  const allowed = state.tool_allowlist ? state.tool_allowlist.join(", ") : "all";
  return `skill '${skill.name}' active. Allowed tools: ${allowed}.\n${skill.prompt}`;
}

function renderSkills(skills) {
  const names = Object.keys(skills || {}).sort();
  if (!names.length) return "No skills installed.";
  const lines = ["Skills:"];
  for (const name of names) {
    const skill = skills[name];
    const suffix = skill.tools ? `  [${skill.tools.join(", ")}]` : "";
    lines.push(`  ${name}: ${skill.description}${suffix}`);
  }
  return lines.join("\n");
}

export { normalizeSkill, loadSkills, activateSkill, renderSkills };
