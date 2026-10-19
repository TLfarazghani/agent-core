"""Phase 9 smoke test: file & code-intelligence tools.

Every test uses a ``tempfile`` root; no real repo paths and no live deps.
Covers read/edit round-trips, absent + non-unique ``old_string``, root escape
rejection, and that the mutating tools halt at the approval gate.
Runnable directly or via pytest.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from core import AgentState, ToolCall, ToolRegistry, new_state, resolve_approval
from tools import register_files_tools


def _new_state() -> AgentState:
    return new_state(target="windows", model="LFM2.5-1.2B-Instruct")


def _registry(root: Path) -> ToolRegistry:
    registry = ToolRegistry()
    register_files_tools(registry, root=root)
    return registry


def _call(registry: ToolRegistry, name: str, args: dict):
    return registry.dispatch(
        _new_state(), ToolCall(id="call_0001", name=name, arguments=args)
    )


def test_read_file_returns_content() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "note.txt").write_text("hello\nworld\n", encoding="utf-8", newline="")
        msg = _call(_registry(root), "read_file", {"path": "note.txt"})
        assert "hello\nworld" in msg.content


def test_read_file_truncates() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "big.txt").write_text("x" * 500, encoding="utf-8")
        msg = _call(_registry(root), "read_file", {"path": "big.txt", "max_chars": 10})
        assert msg.content.startswith("x" * 10)
        assert "truncated" in msg.content


def test_read_file_missing_reports_error() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        msg = _call(_registry(Path(tmp)), "read_file", {"path": "nope.txt"})
        assert msg.content.startswith("error:")


def test_write_then_read_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        registry = _registry(root)
        state = _new_state()
        assert (
            registry.dispatch(
                state,
                ToolCall(
                    id="c1",
                    name="write_file",
                    arguments={"path": "sub/out.txt", "content": "abc"},
                ),
            )
            is None
        )
        assert state.pending_approval is not None
        assert (root / "sub" / "out.txt").exists() is False
        resolve_approval(state, registry, approved=True)
        assert state.messages[-1].content.startswith("wrote")
        assert (root / "sub" / "out.txt").read_text(encoding="utf-8") == "abc"


def test_edit_file_replaces_unique_match() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "code.py"
        target.write_text("value = 1\nprint(value)\n", encoding="utf-8")
        registry = _registry(root)
        state = _new_state()
        registry.dispatch(
            state,
            ToolCall(
                id="c1",
                name="edit_file",
                arguments={
                    "path": "code.py",
                    "old_string": "value = 1",
                    "new_string": "value = 2",
                },
            ),
        )
        resolve_approval(state, registry, approved=True)
        assert target.read_text(encoding="utf-8") == "value = 2\nprint(value)\n"


def test_edit_file_absent_old_string_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "a.txt"
        target.write_text("one\n", encoding="utf-8")
        registry = _registry(root)
        state = _new_state()
        registry.dispatch(
            state,
            ToolCall(
                id="c1",
                name="edit_file",
                arguments={"path": "a.txt", "old_string": "zzz", "new_string": "y"},
            ),
        )
        resolve_approval(state, registry, approved=True)
        assert target.read_text(encoding="utf-8") == "one\n"


def test_edit_file_non_unique_old_string_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "b.txt"
        target.write_text("dup\ndup\n", encoding="utf-8")
        registry = _registry(root)
        state = _new_state()
        registry.dispatch(
            state,
            ToolCall(
                id="c1",
                name="edit_file",
                arguments={"path": "b.txt", "old_string": "dup", "new_string": "x"},
            ),
        )
        resolve_approval(state, registry, approved=True)
        assert target.read_text(encoding="utf-8") == "dup\ndup\n"


def test_out_of_root_paths_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "root"
        root.mkdir()
        outside = Path(tmp) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        registry = _registry(root)
        for name, args in (
            ("read_file", {"path": str(outside)}),
            ("read_file", {"path": "../outside.txt"}),
            ("list_dir", {"path": ".."}),
            ("grep", {"pattern": "secret", "path": ".."}),
        ):
            msg = _call(registry, name, args)
            assert msg.content.startswith("error:"), (name, msg.content)


def test_write_and_edit_are_approval_gated() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "existing.txt").write_text("old", encoding="utf-8")
        registry = _registry(root)
        state = _new_state()
        for name, args in (
            ("write_file", {"path": "new.txt", "content": "x"}),
            ("edit_file", {"path": "existing.txt", "old_string": "old", "new_string": "new"}),
        ):
            state.pending_approval = None
            registry.dispatch(state, ToolCall(id="c", name=name, arguments=args))
            assert state.pending_approval is not None, name
            assert state.pending_approval.tool_name == name
        assert not (root / "new.txt").exists()
        assert (root / "existing.txt").read_text(encoding="utf-8") == "old"


def test_list_dir_marks_directories() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "pkg").mkdir()
        (root / "file.txt").write_text("x", encoding="utf-8")
        msg = _call(_registry(root), "list_dir", {"path": "."})
        lines = msg.content.splitlines()
        assert "pkg/" in lines
        assert "file.txt" in lines


def test_glob_finds_nested_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "core").mkdir()
        (root / "core" / "loop.py").write_text("", encoding="utf-8")
        (root / "readme.md").write_text("", encoding="utf-8")
        msg = _call(_registry(root), "glob", {"pattern": "**/*.py"})
        assert "core/loop.py" in msg.content
        assert "readme.md" not in msg.content


def test_glob_rejects_traversal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        msg = _call(_registry(Path(tmp)), "glob", {"pattern": "../*.txt"})
        assert msg.content.startswith("error:")


def test_grep_returns_path_line_text() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.py").write_text("import os\nprint('needle')\n", encoding="utf-8")
        msg = _call(_registry(root), "grep", {"pattern": "needle", "path": "."})
        assert "a.py:2:" in msg.content
        assert "needle" in msg.content


def test_grep_invalid_regex_reports_error() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        msg = _call(_registry(Path(tmp)), "grep", {"pattern": "("})
        assert msg.content.startswith("error:")


def test_registry_definitions_carry_flags() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        registry = _registry(Path(tmp))
        defs = {d.name: d for d in registry.definitions()}
        for name in ("read_file", "list_dir", "glob", "grep"):
            assert defs[name].read_only is True
            assert defs[name].requires_approval is False
        for name in ("write_file", "edit_file"):
            assert defs[name].requires_approval is True
            assert defs[name].read_only is False


def main() -> int:
    tests = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"{len(tests)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

