"""Phase 5 smoke test: long-term memory (core/memory.py + remember/recall).

Proves persistence, the traversal guard on keys, cross-session recall, and
the bounded recall used to seed new sessions. Uses a temp dir -- never the
real ``~/.agent-core/memory/``. Runnable directly or via pytest.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from core import AgentState, ToolCall, ToolRegistry, new_state
from core.memory import (
    KINDS,
    list_memories,
    load_memory,
    memory_path,
    recall_bounded,
    recall_memories,
    save_memory,
)
from core.sessions import new_agent_state
from tools.cognitive import register_cognitive_tools


def make_cognitive_registry(memory_dir: Path) -> ToolRegistry:
    registry = ToolRegistry()
    register_cognitive_tools(registry, memory_dir=memory_dir)
    return registry


def test_save_and_load_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = Path(tmp)
        save_memory("birthday", "March 3", kind="fact", memory_dir=memory_dir)
        entry = load_memory("birthday", memory_dir=memory_dir)
        assert entry is not None
        assert entry["key"] == "birthday"
        assert entry["content"] == "March 3"
        assert entry["kind"] == "fact"
        assert "created_at" in entry


def test_save_writes_json_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = Path(tmp)
        save_memory("k", "v", source_session="sess1", memory_dir=memory_dir)
        raw = json.loads((memory_dir / "k.json").read_text(encoding="utf-8"))
        assert raw["source_session"] == "sess1"


def test_invalid_kind_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            save_memory("k", "v", kind="garbage", memory_dir=Path(tmp))
        except ValueError:
            pass
        else:
            raise AssertionError("invalid kind must raise ValueError")


def test_memory_path_rejects_traversal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        bad_keys = [
            "..\\..\\Users\\bob\\Desktop\\evil",
            "../../Users/bob/Desktop/evil",
            "a/b",
            "a\\b",
            "C:\\evil",
            "..",
            ".",
        ]
        for key in bad_keys:
            try:
                memory_path(key, Path(tmp))
            except ValueError:
                pass
            else:
                raise AssertionError(f"memory_path({key!r}) must raise ValueError")
        good = memory_path("okay-key", Path(tmp))
        assert good.name == "okay-key.json"


def test_recall_keyword_and_persistence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = Path(tmp)
        save_memory("name", "user prefers tea", kind="preference", memory_dir=memory_dir)
        save_memory("project", "agent-core is local-first", kind="fact", memory_dir=memory_dir)
        hits = recall_memories("tea", memory_dir=memory_dir)
        assert len(hits) == 1
        assert hits[0]["key"] == "name"
        # empty topic recalls everything
        assert len(recall_memories("", memory_dir=memory_dir)) == 2


def test_recall_bounded_fits_token_budget() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = Path(tmp)
        for i in range(20):
            save_memory(
                f"fact_{i}",
                "the quick brown fox jumps over the lazy dog " * 4,
                memory_dir=memory_dir,
            )
        text = recall_bounded("fox", memory_dir=memory_dir, max_tokens=64)
        # recall_bounded builds "[kind] key: content" system lines and trims them
        assert text  # at least one memory fits
        from core.context import estimate_message_tokens
        from core import ChatMessage

        tokens = estimate_message_tokens(ChatMessage(role="system", content=text))
        assert tokens <= 64


def test_remember_and_recall_through_registry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = Path(tmp)
        registry = make_cognitive_registry(memory_dir)
        state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")

        msg = registry.dispatch(
            state,
            ToolCall(
                id="call_1",
                name="remember",
                arguments={"key": "city", "content": "user lives in Lyon", "kind": "fact"},
            ),
        )
        assert msg is not None and msg.role == "tool"
        assert "remembered" in msg.content
        assert load_memory("city", memory_dir=memory_dir) is not None

        msg2 = registry.dispatch(
            state,
            ToolCall(id="call_2", name="recall", arguments={"topic": "Lyon"}),
        )
        assert msg2 is not None and "Lyon" in msg2.content


def test_remember_rejects_bad_key_and_kind() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        registry = make_cognitive_registry(Path(tmp))
        state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
        for args in (
            {"key": "..\\evil", "content": "x"},
            {"key": "k", "content": "x", "kind": "bogus"},
        ):
            msg = registry.dispatch(
                state, ToolCall(id="c", name="remember", arguments=args)
            )
            assert msg is not None
            assert msg.content.startswith("error")


def test_none_memory_dir_uses_default() -> None:
    """Regression: remember/recall registered without a memory_dir must not
    crash (None used to override the MEMORY_DIR default -> 'NoneType' mkdir)."""
    import core.memory as memory_mod

    with tempfile.TemporaryDirectory() as tmp:
        original = memory_mod.MEMORY_DIR
        try:
            memory_mod.MEMORY_DIR = Path(tmp)
            registry = ToolRegistry()
            register_cognitive_tools(registry)  # memory_dir stays None
            state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")

            msg = registry.dispatch(
                state,
                ToolCall(
                    id="call_1",
                    name="remember",
                    arguments={"key": "city", "content": "user lives in Lyon"},
                ),
            )
            assert msg is not None and msg.role == "tool"
            assert not msg.content.startswith("error")
            assert load_memory("city", memory_dir=Path(tmp)) is not None

            msg2 = registry.dispatch(
                state,
                ToolCall(id="call_2", name="recall", arguments={"topic": "Lyon"}),
            )
            assert msg2 is not None and "Lyon" in msg2.content
        finally:
            memory_mod.MEMORY_DIR = original


def test_new_agent_state_seeds_bounded_recall() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = Path(tmp)
        save_memory("greeting", "always greet warmly", kind="preference", memory_dir=memory_dir)
        state = new_agent_state(memory_dir=memory_dir)
        system_text = " ".join(m.content for m in state.messages if m.role == "system")
        assert "greet warmly" in system_text


def test_new_agent_state_without_memory_is_clean() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = Path(tmp) / "empty"
        state = new_agent_state(memory_dir=memory_dir)
        assert len(state.messages) == 1  # just the system prompt


def test_kinds_constant() -> None:
    assert KINDS == ("fact", "preference", "lesson", "session_summary")


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {test.__name__}: {type(exc).__name__}: {exc}")
    if failures:
        raise SystemExit(f"{failures} test(s) failed")
    print(f"\nAll {len(tests)} memory tests passed.")


if __name__ == "__main__":
    main()