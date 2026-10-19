"""Phase 10 smoke test: skills (a tool allowlist, never an approval bypass).

Self-contained: a tempfile skills dir plus fake tools, no live deps. Proves the
allowlist denies unlisted tools, that an allowed *gated* tool still parks at the
hardcoded gate, and that the allowlist clears on the next user turn.
Runnable directly or via pytest.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from core import (
    ChatMessage,
    ToolCall,
    ToolDefinition,
    ToolRegistry,
    new_state,
    step,
)
from tools import register_skills_tools
from tools.skills import load_skills


def _skills_dir(tmp: Path) -> Path:
    directory = tmp / "skills"
    directory.mkdir()
    (directory / "research.json").write_text(
        json.dumps(
            {
                "name": "research",
                "description": "do research",
                "prompt": "Be thorough.",
                "tools": ["echo"],
            }
        ),
        encoding="utf-8",
    )
    (directory / "writes.json").write_text(
        json.dumps(
            {
                "name": "writes",
                "description": "risky writes",
                "prompt": "Write carefully.",
                "tools": ["risky"],
            }
        ),
        encoding="utf-8",
    )
    (directory / "open.json").write_text(
        json.dumps({"name": "open", "description": "no allowlist", "prompt": "Anything goes."}),
        encoding="utf-8",
    )
    return directory


def _registry(skills_dir: Path) -> ToolRegistry:
    registry = ToolRegistry()
    register_skills_tools(registry, skills_dir=skills_dir)
    registry.register(
        ToolDefinition(
            name="echo",
            description="echo",
            requires_approval=False,
            read_only=True,
            parameters={"type": "object", "properties": {}, "required": []},
        ),
        lambda arguments: "echo ran",
    )
    registry.register(
        ToolDefinition(
            name="risky",
            description="gated",
            requires_approval=True,
            parameters={"type": "object", "properties": {}, "required": []},
        ),
        lambda arguments: "risky ran",
    )
    return registry


def test_run_skill_sets_allowlist_and_injects_prompt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        registry = _registry(_skills_dir(Path(tmp)))
        state = new_state(target="windows", model="test")
        msg = registry.dispatch(
            state, ToolCall(id="s1", name="run_skill", arguments={"name": "research"})
        )
        assert msg is not None
        assert state.tool_allowlist == ["echo"]
        assert "Be thorough." in msg.content
        assert any(
            m.role == "system" and "Be thorough." in (m.content or "")
            for m in state.messages
        )


def test_allowlist_denies_unlisted_tool() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        registry = _registry(_skills_dir(Path(tmp)))
        state = new_state(target="windows", model="test")
        registry.dispatch(
            state, ToolCall(id="s1", name="run_skill", arguments={"name": "research"})
        )
        denied = registry.dispatch(state, ToolCall(id="c1", name="risky", arguments={}))
        assert denied is not None
        assert "not allowed by the active skill" in denied.content
        allowed = registry.dispatch(state, ToolCall(id="c2", name="echo", arguments={}))
        assert allowed is not None
        assert allowed.content == "echo ran"


def test_allowlist_is_not_an_approval_bypass() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        registry = _registry(_skills_dir(Path(tmp)))
        state = new_state(target="windows", model="test")
        registry.dispatch(
            state, ToolCall(id="s1", name="run_skill", arguments={"name": "writes"})
        )
        assert state.tool_allowlist == ["risky"]
        parked = registry.dispatch(state, ToolCall(id="c1", name="risky", arguments={}))
        assert parked is None
        assert state.pending_approval is not None
        assert state.pending_approval.tool_name == "risky"


def test_skill_without_tools_allows_everything() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        registry = _registry(_skills_dir(Path(tmp)))
        state = new_state(target="windows", model="test")
        registry.dispatch(state, ToolCall(id="s1", name="run_skill", arguments={"name": "open"}))
        assert state.tool_allowlist is None
        assert registry.dispatch(state, ToolCall(id="c1", name="echo", arguments={})) is not None


def test_unknown_skill_errors() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        registry = _registry(_skills_dir(Path(tmp)))
        state = new_state(target="windows", model="test")
        msg = registry.dispatch(
            state, ToolCall(id="s1", name="run_skill", arguments={"name": "nope"})
        )
        assert msg is not None
        assert msg.content.startswith("error: unknown skill")
        assert state.tool_allowlist is None


def test_list_skills_renders_allowlist() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        registry = _registry(_skills_dir(Path(tmp)))
        state = new_state(target="windows", model="test")
        msg = registry.dispatch(state, ToolCall(id="s1", name="list_skills", arguments={}))
        assert msg is not None
        assert "research" in msg.content
        assert "[echo]" in msg.content


def test_missing_skills_dir_is_empty() -> None:
    assert load_skills(Path("does-not-exist-xyz")) == {}


def test_allowlist_cleared_on_next_user_turn() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        registry = _registry(_skills_dir(Path(tmp)))
        state = new_state(target="windows", model="test")
        registry.dispatch(
            state, ToolCall(id="s1", name="run_skill", arguments={"name": "research"})
        )
        assert state.tool_allowlist == ["echo"]

        state.messages.append(ChatMessage(role="user", content="new request"))
        provider = lambda _state: ChatMessage(role="assistant", content="ok")
        step(state, provider, registry)
        assert state.tool_allowlist is None


if __name__ == "__main__":
    import sys

    failures = 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001 - test runner
            failures += 1
            print(f"FAIL  {test.__name__}: {exc!r}")
    print(f"\nAll {len(tests) - failures}/{len(tests)} skills tests passed.")
    sys.exit(1 if failures else 0)
