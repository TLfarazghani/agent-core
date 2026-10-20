"""run_code handler — Windows sandbox backend via Docker (docker-py).

Schema (research doc §3.4): ``{language, code, timeout_seconds?}``.
``requires_approval`` is always true for this tool (hardcoded in
``core.tool_registry.HARDCODED_APPROVAL_TOOLS``); this handler only ever runs
after ``resolve_approval(approved=True)``.

Sandbox defaults (research doc §4.4, hardened 2026-10-20):

- ``--network none`` (``network_disabled=True``) — no outbound/inbound net.
- memory (``mem_limit``) + CPU (``nano_cpus``) limits.
- ``pids_limit`` — kills fork bombs (`while True: os.fork()`) cheaply.
- ``read_only=True`` + a ``/tmp`` tmpfs — the image's root filesystem is
  writable memory only; persistence inside the sandbox is impossible and root
  kits can't survive, while tmpfs pages count against the memory limit, so a
  tmpfs flood OOMs instead of filling the host disk.
- ``cap_drop=["ALL"]`` + ``security_opts=["no-new-privileges:true"]`` — the
  container runs with zero capabilities and no privilege escalation.
- Output cap (``max_output_chars``) — a ``print`` bomb returns a bounded,
  truncated string instead of flooding model context / memory.
- Images are pinned to **manifest-list digests** (queried from Docker Hub's
  registry API at hardening time), not mutable tags: the same bytes run
  every time regardless of what upstream retags. Override via
  ``images=`` in ``make_handler`` (tests inject fakes; live runs that want a
  different image pass a mapping).

If a digest goes stale upstream (image not found), pass ``images`` with the
plain tag — and note the pin in docs. The docker client is injectable so
tests can supply a fake without a daemon.
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

# Manifest-list digests for _IMAGES, fetched 2026-10-20 from
# registry-1.docker.io (Docker-Content-Digest of the multi-arch index), so
# pinning stays platform-portable. Re-verify before updating.
_PINNED_DIGESTS = {
    "python": "sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1",
    "javascript": "sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0",
    "bash": "sha256:61962062d969cb46dfc2bad061d36342406fa485f64f246aa7e95693ca07df1f",
}

_COMMAND_PREFIX = {
    "python": ["python", "-c"],
    "javascript": ["node", "-e"],
    "bash": ["bash", "-c"],
}

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MEM_LIMIT = "256m"
DEFAULT_NANO_CPUS = int(0.5 * 1e9)  # 0.5 CPU
# Generous for `python -c` snippets; a fork bomb hits the wall long before
# the memory/CPU limits make it expensive.
DEFAULT_PIDS_LIMIT = 64
# Bounded tool output keeps a print bomb out of model context. Note: the
# truncation marker itself is appended AFTER the cut, so the result can
# slightly exceed this number of characters.
DEFAULT_MAX_OUTPUT_CHARS = 100_000


def default_images() -> dict[str, str]:
    """``_IMAGES`` with each tag replaced by its pinned digest reference."""
    images: dict[str, str] = {}
    for language, image in _IMAGES.items():
        digest = _PINNED_DIGESTS.get(language)
        if digest and "@" not in image:
            repo = image.split(":", 1)[0]
            image = f"{repo}@{digest}"
        images[language] = image
    return images


def make_handler(
    client: Any | None = None,
    mem_limit: str = DEFAULT_MEM_LIMIT,
    nano_cpus: int = DEFAULT_NANO_CPUS,
    pids_limit: int = DEFAULT_PIDS_LIMIT,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    read_only: bool = True,
    cap_drop: tuple[str, ...] = ("ALL",),
) -> Callable[[dict[str, Any]], str]:
    images = default_images()

    def handler(arguments: dict[str, Any]) -> str:
        language = arguments["language"]
        code = arguments["code"]
        timeout = arguments.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)

        if language not in images:
            return f"error: unsupported language '{language}'"
        if not code.strip():
            return "error: empty code"

        active_client = client or docker.from_env()
        image = images[language]
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
                pids_limit=pids_limit,
                read_only=read_only,
                # Writable scratch space when the root fs is read-only;
                # tmpfs pages count against the container's memory cgroup.
                tmpfs={"/tmp": ""} if read_only else None,
                cap_drop=list(cap_drop),
                security_opt=["no-new-privileges:true"],
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
            if len(logs) > max_output_chars:
                logs = (
                    logs[:max_output_chars]
                    + f"\n... [output truncated at {max_output_chars} chars]"
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
