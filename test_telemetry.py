"""Phase 12 tests: core/telemetry.py — usage accumulation + doctor probes.

Self-contained (fake urlopen / fake docker probe / tempfile dirs) — no live
llama-server, no Docker daemon, no network. Runnable directly or via pytest.
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
from pathlib import Path

from core import AgentState, new_state
from core.sessions import save_session
from core.telemetry import (
    DEFAULT_SERVER_URL,
    EXPECTED_CTX_BY_MODEL,
    check_context_budget,
    check_docker,
    check_dir_writable,
    check_llama_server,
    check_model_match,
    format_cost,
    format_doctor,
    format_tokens,
    record_usage,
    run_doctor,
    session_usage,
)


class FakeResponse:
    """Minimal urlopen context-manager double."""

    def __init__(self, status: int = 200, payload: list | dict | None = None) -> None:
        self.status = status
        self._payload = payload if payload is not None else {}

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False


def fake_urlopen_down(url, timeout=None):  # noqa: ARG001 - signature only
    raise urllib.error.URLError("connection refused")


def usage_record(prompt: int, completion: int, latency: float, total: int = 0) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total or prompt + completion,
        "latency_s": latency,
        "at": 1792460000.0,
    }


# ---------------------------------------------------------------------------
# record_usage / session_usage
# ---------------------------------------------------------------------------


def test_record_usage_from_dict() -> None:
    state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
    record = record_usage(state, {"prompt_tokens": 1000, "completion_tokens": 120, "total_tokens": 1120}, 1.25)
    assert record["prompt_tokens"] == 1000
    assert state.usage[0].prompt_tokens == 1000
    assert state.usage[0].completion_tokens == 120
    assert state.usage[0].total_tokens == 1120
    assert state.usage[0].latency_s == 1.25
    assert state.usage[0].at > 0


def test_record_usage_tolerates_missing_total() -> None:
    state = new_state(target="windows", model="m")
    record_usage(state, {"prompt_tokens": 10, "completion_tokens": 4}, 0.5)
    summary = session_usage(state)
    assert summary["total_tokens"] == 14, "total falls back to prompt+completion"


def test_record_usage_never_raises_on_garbage() -> None:
    state = new_state(target="windows", model="m")

    class Junk:
        prompt_tokens = "not-a-number"

    record_usage(state, None, 0.0)
    record_usage(state, {"prompt_tokens": -5, "completion_tokens": None}, "bad")
    record_usage(state, Junk(), None)  # type: ignore[arg-type]
    assert len(state.usage) == 3
    assert session_usage(state)["total_tokens"] == 0


def test_session_usage_empty_state() -> None:
    summary = session_usage(new_state(target="windows", model="m"))
    assert summary == {
        "turns": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "generate_seconds": 0.0,
        "tokens_per_second": 0.0,
        "last_tokens_per_second": 0.0,
    }


def test_session_usage_totals_and_tok_per_second() -> None:
    state = new_state(target="windows", model="m")
    record_usage(state, usage_record(2000, 100, 2.0))
    record_usage(state, usage_record(1500, 200, 4.0))
    summary = session_usage(state)
    assert summary["turns"] == 2
    assert summary["prompt_tokens"] == 3500
    assert summary["completion_tokens"] == 300
    assert summary["total_tokens"] == 3800
    assert summary["generate_seconds"] == 6.0
    assert summary["tokens_per_second"] == 50.0  # 300 / 6.0
    assert summary["last_tokens_per_second"] == 50.0  # 200 / 4.0


def test_usage_serializes_with_the_session() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = new_state(target="windows", model="LFM2.5-1.2B-Instruct")
        record_usage(state, usage_record(123, 45, 1.5))
        save_session(state, Path(tmp))
        loaded = AgentState.model_validate_json(
            (Path(tmp) / f"{state.session_id}.json").read_text(encoding="utf-8")
        )
        assert loaded.usage[0].prompt_tokens == 123
        assert session_usage(loaded)["total_tokens"] == 168


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------


def test_format_tokens() -> None:
    assert format_tokens(0) == "0"
    assert format_tokens(1234) == "1,234"
    assert format_tokens(9182736) == "9,182,736"


def test_format_cost_empty() -> None:
    text = format_cost(session_usage(new_state(target="windows", model="m")))
    assert text.startswith("No usage recorded yet")
    assert "$0.00" not in text or "budget" in text


def test_format_cost_populated() -> None:
    report = session_usage(
        new_state(target="windows", model="m")
    )  # then fill in manually for exact formatting
    report = {
        "turns": 3,
        "prompt_tokens": 2014,
        "completion_tokens": 412,
        "total_tokens": 2426,
        "generate_seconds": 6.25,
        "tokens_per_second": 65.92,
        "last_tokens_per_second": 80.0,
    }
    text = format_cost(report)
    assert "Session usage: 3 generation(s)" in text
    assert "2,014" in text and "412" in text and "2,426" in text
    assert "(mean 65.9 tok/s, last turn 80.0 tok/s)" in text
    assert "$0.00" in text and "local" in text


# ---------------------------------------------------------------------------
# doctor probes (all injectable, no live dependencies)
# ---------------------------------------------------------------------------


def test_check_llama_server_up() -> None:
    result = check_llama_server(
        base_url=DEFAULT_SERVER_URL,
        urlopen=lambda url, timeout: FakeResponse(200),
    )
    assert result["ok"] is True
    assert "/health" in result["detail"]


def test_check_llama_server_down_has_hint() -> None:
    result = check_llama_server(base_url=DEFAULT_SERVER_URL, urlopen=fake_urlopen_down)
    assert result["ok"] is False
    assert "connection refused" in result["detail"]
    assert result["hint"].endswith("server_config.ps1")


def test_check_llama_server_non_200_is_down() -> None:
    result = check_llama_server(
        base_url=DEFAULT_SERVER_URL,
        urlopen=lambda url, timeout: FakeResponse(503),
    )
    assert result["ok"] is False
    assert "503" in result["detail"]


def test_check_model_match_agrees() -> None:
    result = check_model_match(
        base_url=DEFAULT_SERVER_URL,
        expected_model="LFM2.5-1.2B-Instruct",
        urlopen=lambda url, timeout: FakeResponse(200, {"data": [{"id": "LFM2.5-1.2B-Instruct"}]}),
    )
    assert result["ok"] is True
    assert "serves LFM2.5-1.2B-Instruct" in result["detail"]


def test_check_model_match_detects_wrong_model() -> None:
    result = check_model_match(
        base_url=DEFAULT_SERVER_URL,
        expected_model="LFM2.5-2.6B",
        urlopen=lambda url, timeout: FakeResponse(200, {"data": [{"id": "LFM2.5-1.2B-Instruct"}]}),
    )
    assert result["ok"] is False
    assert "serves LFM2.5-1.2B-Instruct, session expects LFM2.5-2.6B" in result["detail"]


def test_check_model_match_server_down() -> None:
    result = check_model_match(base_url=DEFAULT_SERVER_URL, expected_model="m", urlopen=fake_urlopen_down)
    assert result["ok"] is False
    assert "could not probe" in result["detail"]


def test_check_context_budget_matches_the_table() -> None:
    for model, ctx in EXPECTED_CTX_BY_MODEL.items():
        assert check_context_budget(model, ctx)["ok"] is True
    fail = check_context_budget("LFM2.5-2.6B", 32768)
    assert fail["ok"] is False
    assert "32768 != expected 131072" in fail["detail"]
    assert "AGENT_CORE_MAX_CONTEXT_TOKENS=131072" in fail["hint"]
    skip = check_context_budget("Some-Future-Model", 32768)
    assert skip["ok"] is True and skip["skipped"] is True


def test_check_docker_probe_states() -> None:
    assert check_docker(probe=lambda: (0, ""))["ok"] is True
    missing = check_docker(probe=lambda: (None, "docker CLI not found on PATH"))
    assert missing["ok"] is False and "Docker Desktop" in missing["hint"]
    down = check_docker(probe=lambda: (1, "Cannot connect to the Docker daemon"))
    assert down["ok"] is False
    assert "start Docker Desktop" in down["hint"]


def test_check_dir_writable_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        assert check_dir_writable(tmp)["ok"] is True
        assert check_dir_writable(Path(tmp) / "nested" / "dir")["ok"] is True
    # A plain file cannot be a writable directory.
    with tempfile.TemporaryDirectory() as tmp:
        blocker = Path(tmp) / "file"
        blocker.write_text("x", encoding="utf-8")
        result = check_dir_writable(blocker)
        assert result["ok"] is False
        assert "not writable" in result["detail"]


# ---------------------------------------------------------------------------
# run_doctor / format_doctor
# ---------------------------------------------------------------------------


def make_state(model: str = "LFM2.5-1.2B-Instruct") -> AgentState:
    return new_state(target="windows", model=model)


def passing_probes() -> dict:
    return {
        "llama_server": lambda: {"ok": True, "detail": "GET /health -> 200"},
        "model_match": lambda: {"ok": True, "detail": "server serves the same model"},
        "docker": lambda: {"ok": True, "detail": "Docker daemon reachable"},
    }


def test_run_doctor_all_ok() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report = run_doctor(
            make_state(),
            session_dir=Path(tmp),
            memory_dir=Path(tmp),
            probes=passing_probes(),
        )
    assert report["ok"] is True
    assert set(report["checks"]) == {
        "llama_server",
        "model_match",
        "context_budget",
        "docker",
        "session_dir",
        "memory_dir",
    }
    assert all(check["ok"] for check in report["checks"].values())


def test_run_doctor_detects_each_problem_class() -> None:
    probes = {
        "llama_server": lambda: check_llama_server(base_url=DEFAULT_SERVER_URL, urlopen=fake_urlopen_down),
        "model_match": lambda: check_model_match(
            base_url=DEFAULT_SERVER_URL,
            expected_model="LFM2.5-2.6B",
            urlopen=lambda url, timeout: FakeResponse(200, {"data": [{"id": "LFM2.5-1.2B-Instruct"}]}),
        ),
        "context_budget": lambda: check_context_budget("LFM2.5-2.6B", 32768),
        "docker": lambda: check_docker(probe=lambda: (1, "daemon down")),
    }
    with tempfile.TemporaryDirectory() as tmp:
        report = run_doctor(make_state(model="LFM2.5-2.6B"), session_dir=Path(tmp), memory_dir=Path(tmp), probes=probes)
    assert report["ok"] is False
    assert {name for name, c in report["checks"].items() if not c["ok"]} == {
        "llama_server",
        "model_match",
        "context_budget",
        "docker",
    }


def test_run_doctor_probe_crash_is_contained() -> None:
    def boom() -> dict:
        raise RuntimeError("boom")

    with tempfile.TemporaryDirectory() as tmp:
        report = run_doctor(make_state(), session_dir=Path(tmp), memory_dir=Path(tmp), probes={"docker": boom})
    assert report["ok"] is False
    assert "RuntimeError" in report["checks"]["docker"]["detail"]


def test_run_doctor_unwritable_dirs_fail() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        blocker = Path(tmp) / "file"
        blocker.write_text("x", encoding="utf-8")
        report = run_doctor(
            make_state(),
            session_dir=blocker,
            memory_dir=blocker,
            probes=passing_probes(),
        )
    assert report["ok"] is False
    assert report["checks"]["session_dir"]["ok"] is False
    assert report["checks"]["memory_dir"]["ok"] is False


def test_format_doctor_text() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ok = run_doctor(make_state(), session_dir=Path(tmp), memory_dir=Path(tmp), probes=passing_probes())
        probes = dict(passing_probes())
        probes["docker"] = lambda: {"ok": False, "detail": "daemon unreachable", "hint": "start Docker"}
        bad = run_doctor(make_state(), session_dir=Path(tmp), memory_dir=Path(tmp), probes=probes)

    ok_text = format_doctor(ok)
    assert "ALL CHECKS PASSED" in ok_text
    assert "llama_server" in ok_text and "Doctor for session" in ok_text
    bad_text = format_doctor(bad)
    assert "FAIL" in bad_text
    assert "Overall: 1 problem(s) found" in bad_text
    assert "hint: start Docker" in bad_text


def test_doctor_constants_hold_canonical_url() -> None:
    assert DEFAULT_SERVER_URL == "http://127.0.0.1:8001/v1"
    # Mirrors windows/server_config.ps1's ctx table (values the doctor checks).
    assert EXPECTED_CTX_BY_MODEL == {
        "LFM2.5-1.2B-Instruct": 32768,
        "LFM2.5-2.6B": 131072,
        "LFM2.5-8B-A1B": 128000,
    }


if __name__ == "__main__":
    failures = 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001 - test runner mirrors test_smoke.py
            failures += 1
            print(f"FAIL  {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\nAll {len(tests) - failures}/{len(tests)} telemetry tests passed.")
    sys.exit(1 if failures else 0)
