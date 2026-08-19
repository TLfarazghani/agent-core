"""Phase 6 smoke test: context-window budget (core/context.py).

Proves the turn-safe trimming invariants: never splits an assistant ``tool_calls``
message from the ``tool`` results that answer it, always keeps the system prompt
and the in-flight last user turn, drops oldest turns first, and never mutates
input. Runnable directly or via pytest.
"""

from __future__ import annotations

from core import ChatMessage, ToolCall
from core.context import estimate_message_tokens, estimate_tokens, trim_to_budget


def system() -> ChatMessage:
    return ChatMessage(role="system", content="You are a helpful assistant.")


def user(text: str) -> ChatMessage:
    return ChatMessage(role="user", content=text)


def assistant(text: str) -> ChatMessage:
    return ChatMessage(role="assistant", content=text)


def assistant_with_call() -> ChatMessage:
    return ChatMessage(
        role="assistant",
        content="",
        function_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})],
    )


def tool_result() -> ChatMessage:
    return ChatMessage(role="tool", tool_call_id="c1", content="echo: x")


def test_estimate_min_one_per_message() -> None:
    assert estimate_message_tokens(ChatMessage(role="user", content="")) == 1
    assert estimate_message_tokens(ChatMessage(role="user", content="ab")) == 1
    assert estimate_message_tokens(ChatMessage(role="user", content="abcd")) == 1
    assert estimate_message_tokens(ChatMessage(role="user", content="abcde")) == 2


def test_estimate_chars_per_4() -> None:
    msg = ChatMessage(role="user", content="x" * 100)
    assert estimate_message_tokens(msg) == 25
    assert estimate_tokens([msg, msg]) == 50


def test_estimate_counts_tool_calls_and_ids() -> None:
    with_call = assistant_with_call()
    plain = user("hello")
    # plain text "hello" -> 2 tokens; the call adds name+args JSON
    assert estimate_tokens([plain]) == 2
    assert estimate_tokens([with_call]) > estimate_tokens([user("")])


def test_trim_none_or_zero_budget_returns_everything() -> None:
    msgs = [system(), user("a"), assistant("b")]
    for budget in (None, 0, -5):
        kept, dropped = trim_to_budget(msgs, budget)
        assert kept == msgs
        assert dropped == []


def test_trim_empty_and_no_user() -> None:
    assert trim_to_budget([], 100) == ([], [])
    msgs = [system()]
    assert trim_to_budget(msgs, 100) == (msgs, [])


def test_trim_always_keeps_system_and_last_turn() -> None:
    msgs = [system(), user("first"), assistant("one"), user("last"), assistant("two")]
    kept, dropped = trim_to_budget(msgs, 1)  # tiny budget: floor only
    # Floor (system + last turn) kept even when over budget; middle turns drop.
    assert kept == [system(), user("last"), assistant("two")]
    assert dropped == [user("first"), assistant("one")]


def test_trim_drops_oldest_turns_first() -> None:
    msgs = [system(), user("first"), assistant("one"), user("last"), assistant("two")]
    # budget fits exactly system + last user turn -> middle-first user turn drops
    kept, dropped = trim_to_budget(msgs, estimate_tokens([system(), user("last"), assistant("two")]))
    assert kept == [system(), user("last"), assistant("two")]
    assert dropped == [user("first"), assistant("one")]


def test_trim_keeps_newer_turns_that_fit() -> None:
    msgs = [
        system(),
        user("old a"), assistant("old a reply"),
        user("mid b"), assistant("mid b reply"),
        user("new c"), assistant("new c reply"),
    ]
    # fit system + last two turns, drop the oldest
    kept_budget = estimate_tokens([system(), user("mid b"), assistant("mid b reply"), user("new c"), assistant("new c reply")])
    kept, dropped = trim_to_budget(msgs, kept_budget)
    assert kept == [system(), user("mid b"), assistant("mid b reply"), user("new c"), assistant("new c reply")]
    assert dropped == [user("old a"), assistant("old a reply")]


def test_trim_never_splits_tool_pair() -> None:
    msgs = [
        system(),
        user("first"),
        assistant_with_call(),
        tool_result(),
        user("last"),
        assistant("done"),
    ]
    # small budget: only system + last turn fit -> the whole first turn
    # (user + assistant tool_calls + tool result) must drop together
    kept, dropped = trim_to_budget(msgs, estimate_tokens([system(), user("last"), assistant("done")]))
    assert kept == [system(), user("last"), assistant("done")]
    assert dropped == [user("first"), assistant_with_call(), tool_result()]


def test_trim_keeps_tool_pair_when_turn_kept() -> None:
    msgs = [
        system(),
        user("call it"),
        assistant_with_call(),
        tool_result(),
        user("last"),
        assistant("done"),
    ]
    kept, dropped = trim_to_budget(msgs, estimate_tokens(msgs))
    assert kept == msgs
    assert dropped == []


def test_trim_accepts_dicts_and_preserves_order() -> None:
    dict_msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "one"},
        {"role": "user", "content": "last"},
        {"role": "assistant", "content": "two"},
    ]
    kept, dropped = trim_to_budget(dict_msgs, estimate_tokens([dict_msgs[0], dict_msgs[3], dict_msgs[4]]))
    assert kept == [dict_msgs[0], dict_msgs[3], dict_msgs[4]]
    assert dropped == [dict_msgs[1], dict_msgs[2]]
    assert kept[0]["role"] == "system"


def test_trim_does_not_mutate_input() -> None:
    msgs = [system(), user("a"), assistant("b"), user("c"), assistant("d")]
    snapshot = [m.model_copy(deep=True) for m in msgs]
    trim_to_budget(msgs, estimate_tokens([msgs[0], msgs[3], msgs[4]]))
    assert msgs == snapshot


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
    print(f"\nAll {len(tests) - failures}/{len(tests)} context smoke tests passed.")
    sys.exit(1 if failures else 0)