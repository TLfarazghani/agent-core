"""Per-session observability: usage telemetry + doctor self-diagnostics (Phase 12).

Stdlib-only and local-only — nothing here phones home or persists anywhere new.

Two halves:

- **Usage:** providers that know real token counts (llama.cpp returns
  ``usage.prompt_tokens`` / ``usage.completion_tokens`` with
  ``stream_options.include_usage``) call :func:`record_usage` once per generate
  with the server-reported numbers plus generation latency. The records live on
  ``AgentState.usage`` (a ``list[TurnUsage]``) so they serialize with the
  session and survive CLI ↔ web resumes. :func:`session_usage` folds them into
  a summary; :func:`format_cost` renders it for the ``/cost`` slash command.
  Inference is local, so the "cost" is tokens only — never dollars.

- **Doctor:** ``/doctor`` probes the machine's dependencies the way
  ``Core-agent.bat`` does: llama-server ``/health``, a model/ctx match against
  ``/v1/models``, the Docker daemon for ``run_code``, and session/memory dir
  writability. Every probe is injectable (``urlopen`` / probe callables), so
  tests never touch the network or a live daemon, and each probe is surrounded
  so a failure degrades to an ``ok: False`` check instead of raising.

Pure summary/format logic is mirrored line-for-line in ``web/telemetry.js``;
``/doctor`` is host-only (a browser tab cannot see the host's Docker or dirs).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

# Canonical llama-server endpoint (OpenAI-compatible). windows/orchestrator.py
# re-exports this as DEFAULT_BASE_URL so CLI / web never duplicate the value.
DEFAULT_SERVER_URL = "http://127.0.0.1:8001/v1"

# Expected server ``--ctx-size`` per model alias — mirrors the
# windows/server_config.ps1 table (1.2B 32K default; 2.6B and 8B-A1B 128K).
# Model-level facts about the LFM2.5 checkpoints, not Windows-only knowledge.
EXPECTED_CTX_BY_MODEL: dict[str, int] = {
    "LFM2.5-1.2B-Instruct": 32768,
    "LFM2.5-2.6B": 131072,
    "LFM2.5-8B-A1B": 128000,
}

DEFAULT_CONTEXT_TOKENS = 32768


# ---------------------------------------------------------------------------
# Usage recording (fed by providers that capture real server usage)
# ---------------------------------------------------------------------------


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict or an object (OpenAI usage works either way)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def record_usage(state: Any, usage: Any = None, latency_s: float | None = None) -> dict:
    """Append one per-generate record to ``state.usage``. Never raises.

    ``usage`` is whatever the provider captured (an OpenAI usage object, its
    ``model_dump()`` dict, or None); only integers survive normalization. A
    record with zeros is still useful — it carries the generation latency for
    per-turn tok/s even when the server does not report token counts.
    ``latency_s`` is the provider's measured generate time; when omitted, a
    ``latency_s`` inside ``usage`` is used.
    """
    usage = usage or {}
    if latency_s is None:
        latency_s = _obj_get(usage, "latency_s", 0.0)
    record = {
        "prompt_tokens": max(0, _as_int(_obj_get(usage, "prompt_tokens", 0))),
        "completion_tokens": max(0, _as_int(_obj_get(usage, "completion_tokens", 0))),
        "total_tokens": max(0, _as_int(_obj_get(usage, "total_tokens", 0))),
        "latency_s": max(0.0, _as_float(latency_s)),
        "at": time.time(),
    }
    try:
        from .state import TurnUsage

        usage_list = getattr(state, "usage", None)
        if usage_list is None:
            usage_list = []
            state.usage = usage_list
        try:
            usage_list.append(TurnUsage(**record))
        except (TypeError, ValueError):
            # Duck-typed state / constructor mismatch: store the plain record.
            usage_list.append(record)
    except Exception:  # noqa: BLE001 - telemetry must never break a generate
        pass
    return record


def session_usage(state: Any) -> dict:
    """Fold ``state.usage`` into a per-session summary (plain dict, JS-shaped).

    ``tokens_per_second`` is completion tokens over total generation time —
    the same number ``docs/benchmarks.md`` measures. Zero when no latency was
    captured.
    """
    records = list(getattr(state, "usage", None) or [])
    prompt = sum(max(0, _as_int(_obj_get(r, "prompt_tokens", 0))) for r in records)
    completion = sum(max(0, _as_int(_obj_get(r, "completion_tokens", 0))) for r in records)
    total = sum(max(0, _as_int(_obj_get(r, "total_tokens", 0))) for r in records)
    generate_seconds = sum(max(0.0, _as_float(_obj_get(r, "latency_s", 0.0))) for r in records)
    if not total and (prompt or completion):
        # Server usage always carries total_tokens; fall back in case a
        # future provider reports only the split.
        total = prompt + completion
    last = _last_tps(records)
    return {
        "turns": len(records),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "generate_seconds": round(generate_seconds, 3),
        "tokens_per_second": round(completion / generate_seconds, 2) if generate_seconds > 0 else 0.0,
        "last_tokens_per_second": last,
    }


def _last_tps(records: list) -> float:
    if not records:
        return 0.0
    last = records[-1]
    latency = max(0.0, _as_float(_obj_get(last, "latency_s", 0.0)))
    completion = max(0, _as_int(_obj_get(last, "completion_tokens", 0)))
    return round(completion / latency, 2) if latency > 0 else 0.0


def format_tokens(value: int) -> str:
    """Thousands-separated count, e.g. ``1234`` -> ``"1,234"`` (JS-mirrored)."""
    return f"{max(0, int(value)):,}"


def format_cost(report: dict) -> str:
    """Render a ``session_usage`` summary for ``/cost`` (JS-mirrored text)."""
    if not report.get("turns"):
        return (
            "No usage recorded yet this session.\n"
            "  Server-reported tokens appear after the first generation.\n"
            "  Inference is local: nothing is metered (see /context for the budget)."
        )
    return (
        f"Session usage: {report['turns']} generation(s)\n"
        f"  prompt tokens:      {format_tokens(report.get('prompt_tokens', 0))}\n"
        f"  completion tokens:  {format_tokens(report.get('completion_tokens', 0))}\n"
        f"  total tokens:       {format_tokens(report.get('total_tokens', 0))}\n"
        f"  generation time:    {report.get('generate_seconds', 0.0):.1f}s"
        f" (mean {report.get('tokens_per_second', 0.0):.1f} tok/s,"
        f" last turn {report.get('last_tokens_per_second', 0.0):.1f} tok/s)\n"
        "  inference cost:     $0.00 - local, no metered API calls"
    )


# ---------------------------------------------------------------------------
# Doctor probes (self-diagnostics; every default probe is injectable)
# ---------------------------------------------------------------------------


def _urlopen_status(url: str, timeout: float, urlopen) -> tuple[int | None, str]:
    """Fetch ``url`` and return ``(status, error_text)``. Never raises."""
    try:
        with urlopen(url, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            return int(status), ""
    except Exception as exc:  # noqa: BLE001 - a probe failure is a result, not a crash
        return None, f"{type(exc).__name__}: {exc}"


def check_llama_server(
    base_url: str = DEFAULT_SERVER_URL,
    timeout: float = 2.0,
    urlopen: Callable | None = None,
) -> dict:
    """Probe llama-server's ``/health`` (the same check cli.py does at startup)."""
    urlopen = urlopen or urllib.request.urlopen
    health_url = base_url.removesuffix("/v1") + "/health"
    status, error = _urlopen_status(health_url, timeout, urlopen)
    if status == 200:
        return {"ok": True, "detail": f"GET {health_url} -> 200"}
    return {
        "ok": False,
        "detail": f"llama-server unreachable at {health_url}"
        + (f" ({error})" if error else f" (status {status})"),
        "hint": "start it with: .\\windows\\server_config.ps1",
    }


def check_model_match(
    base_url: str = DEFAULT_SERVER_URL,
    expected_model: str = "",
    timeout: float = 3.0,
    urlopen: Callable | None = None,
) -> dict:
    """Compare ``/v1/models``' first id against the session's model (Core-agent's
    ``:server_model`` probe). A mismatch means :8001 is serving another model."""
    urlopen = urlopen or urllib.request.urlopen
    if not expected_model:
        return {"ok": False, "skipped": True, "detail": "no session model to compare;"}
    try:
        with urlopen(base_url + "/models", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"could not probe {base_url}/models ({exc})"}
    models = data.get("data") or []
    server_model = str(models[0].get("id")) if models else ""
    if not server_model:
        return {"ok": False, "detail": "/v1/models returned no ids"}
    if server_model == expected_model:
        return {"ok": True, "detail": f"server serves {server_model} (session expects the same)"}
    return {
        "ok": False,
        "detail": f"server serves {server_model}, session expects {expected_model}",
        "hint": "stop llama-server, then run server_config.ps1 for this model",
    }


def check_context_budget(
    model: str = "",
    max_context_tokens: int = DEFAULT_CONTEXT_TOKENS,
    expected_by_model: dict[str, int] | None = None,
) -> dict:
    """Compare the client context budget to the model's table ``--ctx-size``.

    A mismatch means the wire-request budget and the server window disagree
    (Phase 6 trims to the client budget; the server silently drops payload past
    ``--ctx-size``, so both must tell the same story).
    """
    table = EXPECTED_CTX_BY_MODEL if expected_by_model is None else expected_by_model
    expected = table.get(model)
    if expected is None:
        return {
            "ok": True,
            "skipped": True,
            "detail": f"no known ctx table entry for {model!r} - skipped",
        }
    if max_context_tokens == expected:
        return {"ok": True, "detail": f"{max_context_tokens} matches the ctx table for {model}"}
    return {
        "ok": False,
        "detail": f"budget {max_context_tokens} != expected {expected} for {model}",
        "hint": f"set AGENT_CORE_MAX_CONTEXT_TOKENS={expected}",
    }


def _docker_probe() -> tuple[int | None, str]:
    """Default Docker check: ``docker info``, the same one Core-agent.bat runs."""
    if shutil.which("docker") is None:
        return None, "docker CLI not found on PATH"
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, output[:300]


def check_docker(probe: Callable[[], tuple[int | None, str]] | None = None) -> dict:
    """Docker daemon availability for ``run_code`` (injectable probe)."""
    rc, output = (probe or _docker_probe)()
    if rc == 0:
        return {"ok": True, "detail": "Docker daemon reachable - run_code sandbox available"}
    if rc is None and output.startswith("docker CLI not found"):
        return {"ok": False, "detail": output, "hint": "install Docker Desktop for run_code"}
    if rc is None:
        return {"ok": False, "detail": f"docker probe failed: {output}"}
    return {
        "ok": False,
        "detail": f"Docker daemon not reachable (exit {rc}): {output or 'docker info failed'}",
        "hint": "start Docker Desktop; run_code needs it",
    }


def check_dir_writable(path: Path | str) -> dict:
    """Create ``path`` if missing and prove a temp file round-trips, then clean up."""
    path = Path(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe_file = path / f".doctor_{int(time.time() * 1000)}.tmp"
        probe_file.write_text("doctor", encoding="utf-8")
        content = probe_file.read_text(encoding="utf-8")
        probe_file.unlink()
        if content != "doctor":
            raise OSError("written content did not round-trip")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"{path} not writable ({exc})"}
    return {"ok": True, "detail": f"writable: {path}"}


def run_doctor(
    state: Any,
    base_url: str = DEFAULT_SERVER_URL,
    max_context_tokens: int = DEFAULT_CONTEXT_TOKENS,
    session_dir: Path | None = None,
    memory_dir: Path | None = None,
    probes: dict[str, Callable[[], dict]] | None = None,
) -> dict:
    """Run every check and return a report dict. Never raises.

    ``probes`` maps a check name to a zero-arg callable returning
    ``{"ok": bool, "detail": str, ...}``; supplying one replaces that default
    probe entirely (the test seam). Checks touched: ``llama_server``,
    ``model_match``, ``context_budget``, ``docker``, ``session_dir``,
    ``memory_dir``.
    """
    if session_dir is None:
        from .sessions import SESSION_DIR as session_dir  # local import avoids a cycle
    if memory_dir is None:
        from .memory import MEMORY_DIR as memory_dir

    defaults: dict[str, Callable[[], dict]] = {
        "llama_server": lambda: check_llama_server(base_url=base_url),
        "model_match": lambda: check_model_match(
            base_url=base_url, expected_model=getattr(state, "model", "")
        ),
        "context_budget": lambda: check_context_budget(
            model=getattr(state, "model", ""), max_context_tokens=max_context_tokens
        ),
        "docker": check_docker,
        "session_dir": lambda: check_dir_writable(session_dir),
        "memory_dir": lambda: check_dir_writable(memory_dir),
    }
    if probes:
        defaults.update(probes)

    checks: dict[str, dict] = {}
    for name, probe in defaults.items():
        try:
            result = probe() or {"ok": False, "detail": "probe returned nothing"}
        except Exception as exc:  # noqa: BLE001 - contained: report and move on
            result = {"ok": False, "detail": f"probe failed: {type(exc).__name__}: {exc}"}
        checks[name] = result

    return {
        "session_id": getattr(state, "session_id", ""),
        "model": getattr(state, "model", ""),
        "base_url": base_url,
        "max_context_tokens": max_context_tokens,
        "checks": checks,
        "ok": all(bool(check.get("ok")) for check in checks.values()),
    }


def format_doctor(report: dict) -> str:
    """Render a ``run_doctor`` report for ``/doctor`` (plain text, no ANSI)."""
    checks = report.get("checks", {})
    lines = [
        f"Doctor for session {str(report.get('session_id', ''))[:6]}"
        f" (model {report.get('model', '?')},"
        f" budget {report.get('max_context_tokens', '?')},"
        f" server {report.get('base_url', '?')}):"
    ]
    for name, result in checks.items():
        marker = "ok" if result.get("ok") else "FAIL"
        if result.get("skipped"):
            marker = "skip"
        line = f"  {name:<15} {marker} - {result.get('detail', '')}"
        hint = result.get("hint")
        if hint and not result.get("ok"):
            line += f"\n{'':<20}hint: {hint}"
        lines.append(line)
    problems = sum(1 for r in checks.values() if not r.get("ok"))
    lines.append(
        "Overall: ALL CHECKS PASSED" if problems == 0 else f"Overall: {problems} problem(s) found"
    )
    return "\n".join(lines)


__all__ = [
    "DEFAULT_CONTEXT_TOKENS",
    "DEFAULT_SERVER_URL",
    "EXPECTED_CTX_BY_MODEL",
    "check_context_budget",
    "check_docker",
    "check_dir_writable",
    "check_llama_server",
    "check_model_match",
    "format_cost",
    "format_doctor",
    "format_tokens",
    "record_usage",
    "run_doctor",
    "session_usage",
]
