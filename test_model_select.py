"""Phase 7 smoke test: model selectability (AGENT_CORE_MODEL / ctx budget).

Proves the model is no longer hardcoded: ``default_model()`` and
``default_max_context_tokens()`` honor their env vars with the shipped 1.2B /
32K defaults, and ``new_agent_state()`` seeds the session's model from the
same source (so the CLI, web UI, and server config all agree). Runnable
directly or via pytest.
"""

from __future__ import annotations

import os

from core.sessions import DEFAULT_MODEL, default_model, new_agent_state
from windows.orchestrator import (
    DEFAULT_MAX_CONTEXT_TOKENS,
    default_max_context_tokens,
)


def _with_env(name: str, value: str | None):
    def decorate(fn):
        def wrapper(*args, **kwargs):
            previous = os.environ.get(name)
            try:
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
                return fn(*args, **kwargs)
            finally:
                if previous is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = previous

        return wrapper

    return decorate


def test_default_model_is_shipped_1_2b() -> None:
    assert DEFAULT_MODEL == "LFM2.5-1.2B-Instruct"
    assert default_model() == DEFAULT_MODEL


@_with_env("AGENT_CORE_MODEL", "LFM2.5-2.6B")
def test_default_model_honors_env() -> None:
    assert default_model() == "LFM2.5-2.6B"


@_with_env("AGENT_CORE_MODEL", "LFM2.5-2.6B")
def test_new_agent_state_seeds_model_from_env() -> None:
    state = new_agent_state()
    assert state.model == "LFM2.5-2.6B"


def test_new_agent_state_explicit_model_wins() -> None:
    state = new_agent_state(model="LFM2.5-8B-A1B")
    assert state.model == "LFM2.5-8B-A1B"


def test_context_budget_default_is_32k() -> None:
    assert default_max_context_tokens() == DEFAULT_MAX_CONTEXT_TOKENS
    assert DEFAULT_MAX_CONTEXT_TOKENS == 32768


@_with_env("AGENT_CORE_MAX_CONTEXT_TOKENS", "131072")
def test_context_budget_honors_env() -> None:
    assert default_max_context_tokens() == 131072


@_with_env("AGENT_CORE_MAX_CONTEXT_TOKENS", "bogus")
def test_context_budget_ignores_invalid_env() -> None:
    assert default_max_context_tokens() == DEFAULT_MAX_CONTEXT_TOKENS


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
    print(f"\nAll {len(tests)} model-select tests passed.")


if __name__ == "__main__":
    main()