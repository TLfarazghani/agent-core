"""run_code handler — Windows sandbox backend via Docker (docker-py).

Schema (research doc §3.4): ``{language, code, timeout_seconds?}``.
``requires_approval`` is always true for this tool (hardcoded in
``core.tool_registry.HARDCODED_APPROVAL_TOOLS``); this handler only ever runs
after ``resolve_approval(approved=True)``.

Sandbox defaults (research doc §4.4): ``--network none``, memory + cpu limits.
The docker client is injectable so tests can supply a fake without a daemon.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

import docker

_IMAGES = {
    "python": "python:3.12-slim",
    "javascript": "node:20-slim",
    "bash": "bash:5",
}

_COMMAND_PREFIX = {
    "python": ["python", "-c"],
    "javascript": ["node", "-e"],
    "bash": ["bash", "-c"],
}

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MEM_LIMIT = "256m"
DEFAULT_NANO_CPUS = int(0.5 * 1e9)  # 0.5 CPU


def make_handler(
    client: Any | None = None,
    mem_limit: str = DEFAULT_MEM_LIMIT,
    nano_cpus: int = DEFAULT_NANO_CPUS,
) -> Callable[[dict[str, Any]], str]:
    def handler(arguments: dict[str, Any]) -> str:
        language = arguments["language"]
        code = arguments["code"]
        timeout = arguments.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)

        if language not in _IMAGES:
            return f"error: unsupported language '{language}'"
        if not code.strip():
            return "error: empty code"

        active_client = client or docker.from_env()
        image = _IMAGES[language]
        command = _COMMAND_PREFIX[language] + [code]

        container = None
        try:
            container = active_client.containers.run(
                image,
                command=command,
                detach=True,
                network_disabled=True,
                mem_limit=mem_limit,
                nano_cpus=nano_cpus,
                stdout=True,
                stderr=True,
            )
            wait_result: dict[str, Any] = {}

            def _wait() -> None:
                wait_result.update(container.wait())

            waiter = threading.Thread(target=_wait, daemon=True)
            waiter.start()
            waiter.join(timeout=timeout)
            if waiter.is_alive():
                try:
                    container.kill()
                except Exception:  # noqa: BLE001 - container may already be gone
                    pass
                return "error: execution timed out"

            logs = (
                container.logs(stdout=True, stderr=True)
                .decode("utf-8", errors="replace")
                .strip()
            )
            status = wait_result.get("StatusCode", -1)
            if status != 0:
                return f"exit code {status}\n{logs}"
            return logs or "(no output)"
        except Exception as exc:  # noqa: BLE001 - surface docker errors as tool results
            return f"error: docker: {exc}"
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:  # noqa: BLE001 - cleanup must not mask the result
                    pass

    return handler