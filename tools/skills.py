"""Phase 10 skills: reusable, tool-allowlisted workflows.

A skill is a declarative JSON file ``{name, description, prompt, tools?}`` under
``skills/``. ``run_skill`` injects the skill's prompt and constrains the tool set
to the skill's allowlist (``AgentState.tool_allowlist``), which
``core.tool_registry.dispatch()`` enforces. A skill is a **tool allowlist, not a
permission bypass**: every allowed call still passes the hardcoded approval gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from core import takes_state
from core.state import ChatMessage

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
SKILL_TOOLS = {"run_skill", "list_skills"}


def load_skills(skills_dir: Optional[Path] = None) -> dict[str, dict]:
    """Read every ``*.json`` skill, keyed by name (filename stem fallback)."""
    directory = Path(skills_dir) if skills_dir is not None else SKILLS_DIR
    skills: dict[str, dict] = {}
    if not directory.exists():
        return skills
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        name = str(data.get("name") or path.stem)
        tools = data.get("tools")
        data["name"] = name
        data["tools"] = [str(t) for t in tools] if isinstance(tools, list) else None
        data.setdefault("description", "")
        data.setdefault("prompt", "")
        skills[name] = data
    return skills


def make_run_skill_handler(skills_dir: Optional[Path] = None) -> Callable:
    def handler(state, arguments: dict) -> str:
        skills = load_skills(skills_dir)
        name = str(arguments.get("name", "")).strip()
        skill = skills.get(name)
        if skill is None:
            available = ", ".join(sorted(skills)) or "none"
            return f"error: unknown skill '{name}' (available: {available})"
        allowed = skill.get("tools")
        state.tool_allowlist = list(allowed) if allowed else None
        prompt = skill.get("prompt", "")
        state.messages.append(
            ChatMessage(role="system", content=f"Skill '{name}' active: {prompt}")
        )
        allowed_text = ", ".join(state.tool_allowlist) if state.tool_allowlist else "all"
        return f"skill '{name}' active. Allowed tools: {allowed_text}.\n{prompt}"

    return takes_state(handler)


def make_list_skills_handler(skills_dir: Optional[Path] = None) -> Callable:
    def handler(state, arguments: dict) -> str:
        skills = load_skills(skills_dir)
        if not skills:
            return "No skills installed."
        lines = ["Skills:"]
        for name, skill in sorted(skills.items()):
            allowed = skill.get("tools")
            suffix = f"  [{', '.join(allowed)}]" if allowed else ""
            lines.append(f"  {name}: {skill.get('description', '')}{suffix}")
        return "\n".join(lines)

    return takes_state(handler)


def register_skills_tools(registry, skills_dir: Optional[Path] = None) -> None:
    """Register ``run_skill`` and ``list_skills``."""
    handlers = {
        "run_skill": make_run_skill_handler(skills_dir),
        "list_skills": make_list_skills_handler(skills_dir),
    }
    registry.load_json("tools/registry.json", handlers, names=SKILL_TOOLS)
