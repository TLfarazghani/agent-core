"""Phase 9: file & code-intelligence tools.

Read-only tools (``read_file``, ``list_dir``, ``glob``, ``grep``) run without
approval and are marked ``read_only`` so ``plan`` mode may use them. Mutating
tools (``write_file``, ``edit_file``) carry ``requires_approval`` so the
hardcoded gate in ``core.tool_registry.dispatch()`` parks them.

Every path is resolved under ``AGENT_CORE_FILE_ROOT`` (default: the repo root)
and rejected if it escapes that root. Stdlib-only; tests inject a temp root.

Sensitive-file guard (2026-10-20): the tool root is NOT an all-clear -- a
stray ``.env``, private key, or credentials file inside it is as dangerous to
read as one outside (its contents would land in model context). Explicit
content reads therefore refuse well-known secret files:
``read_file``/``edit_file`` fail on a match (with the matched pattern in the
message), ``grep`` skips matches while scanning and reports the skip count,
and ``list_dir``/``glob`` stay name-only (they never expose content).
``AGENT_CORE_FILE_ALLOW_SENSITIVE=1`` disables the guard explicitly; the
better fix is a narrower ``AGENT_CORE_FILE_ROOT`` pointing at a working
directory that holds no secrets. Writes remain human-gated; note that an
*approved* write may still create or replace any file under the root,
including files the guard would refuse to read back until allowed.

Root choice guidance: the repo default exists because the agent's job is
working on this repository. On anything shared or semi-trusted, set
``AGENT_CORE_FILE_ROOT`` narrower -- e.g. a dedicated workspace dir -- and
keep separate roots per transport.
"""

from __future__ import annotations

import fnmatch
import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Iterator, Optional

DEFAULT_MAX_CHARS = 20_000
MAX_READ_CHARS = 200_000
MAX_WRITE_CHARS = 2_000_000
MAX_LIST_ENTRIES = 500
MAX_GLOB_RESULTS = 500
MAX_GREP_RESULTS = 200
MAX_GREP_FILE_BYTES = 1_000_000
MAX_GREP_FILES = 2_000
SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "models",
    "node_modules",
    "vendor",
}

# Content-bearing secret files: matched (fnmatch, case-insensitive) against
# the root-relative path AND its basename, so a bare pattern like ".env" hits
# every depth while dir-scoped patterns like ".ssh/*" key on the full path.
# Mostly env/credential/key classes from gitignore lore. Extend with
# stronger-named local blocks, not a broader "*secret*"-style wildcard.
SENSITIVE_PATTERNS = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*.kdbx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".htpasswd",
    ".netrc",
    ".npmrc",
    ".git-credentials",
    "credentials.json",
    "credentials*.json",
    "service-account*.json",
    "secrets.yml",
    "secrets.yaml",
    ".ssh/*",
    ".aws/*",
    ".kube/config",
    ".gnupg/*",
)


def default_root() -> Path:
    """The allowed file root: ``AGENT_CORE_FILE_ROOT`` or the repo root."""
    configured = os.environ.get("AGENT_CORE_FILE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def _allow_sensitive() -> bool:
    """Explicit override disabling the sensitive-file read guard."""
    return os.environ.get("AGENT_CORE_FILE_ALLOW_SENSITIVE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def sensitive_match(rel_posix: str) -> str | None:
    """Return the SENSITIVE_PATTERNS entry matching ``rel_posix`` or None."""
    lowered = rel_posix.replace("\\", "/").lower()
    name = lowered.rsplit("/", 1)[-1]
    for pattern in SENSITIVE_PATTERNS:
        pat = pattern.lower()
        if fnmatch.fnmatch(lowered, pat) or fnmatch.fnmatch(name, pat):
            return pattern
    return None


def _sensitive_refusal(display: str, pattern: str) -> str:
    return (
        f"error: refused to read '{display}' -- it matches sensitive-file "
        f"pattern '{pattern}'. Secret files must not enter model context. "
        "Move the secret out of AGENT_CORE_FILE_ROOT (or set a narrower "
        "root), or set AGENT_CORE_FILE_ALLOW_SENSITIVE=1 to override "
        "explicitly."
    )


def resolve_path(path: str, root: Path) -> Path:
    """Resolve ``path`` under ``root``, rejecting any escape (incl. symlinks)."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")
    root = Path(root).expanduser().resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path escapes the allowed root: {path!r}")
    return resolved


def _display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix() or "."
    except ValueError:
        return path.as_posix()


def _root(injected: Optional[Path]) -> Path:
    return injected.resolve() if injected is not None else default_root()


def _read_text(path: Path) -> str:
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".agent-core-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def _contained(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def _iter_files(directory: Path) -> Iterator[Path]:
    for current, dirnames, filenames in os.walk(directory):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield Path(current) / name


def make_read_file_handler(root: Optional[Path] = None) -> Callable:
    injected = root

    def handler(arguments: dict) -> str:
        base = _root(injected)
        raw = arguments.get("path", "")
        try:
            target = resolve_path(raw, base)
        except ValueError as exc:
            return f"error: {exc}"
        if not target.exists():
            return f"error: file not found: {raw}"
        if target.is_dir():
            return f"error: is a directory: {_display(target, base)}"
        display = _display(target, base)
        matched = sensitive_match(display)
        if matched and not _allow_sensitive():
            return _sensitive_refusal(display, matched)
        max_chars = _clamp(
            arguments.get("max_chars", DEFAULT_MAX_CHARS), 1, MAX_READ_CHARS
        )
        try:
            text = _read_text(target)
        except OSError as exc:
            return f"error: {exc}"
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... [truncated at {max_chars} chars]"
        return text

    return handler


def make_write_file_handler(root: Optional[Path] = None) -> Callable:
    injected = root

    def handler(arguments: dict) -> str:
        base = _root(injected)
        raw = arguments.get("path", "")
        try:
            target = resolve_path(raw, base)
        except ValueError as exc:
            return f"error: {exc}"
        content = arguments.get("content")
        if not isinstance(content, str):
            return "error: content must be a string"
        if len(content) > MAX_WRITE_CHARS:
            return f"error: content exceeds {MAX_WRITE_CHARS} chars"
        if target.exists() and target.is_dir():
            return f"error: is a directory: {_display(target, base)}"
        try:
            _atomic_write(target, content)
        except OSError as exc:
            return f"error: {exc}"
        return f"wrote {len(content)} chars to {_display(target, base)}"

    return handler


def make_edit_file_handler(root: Optional[Path] = None) -> Callable:
    injected = root

    def handler(arguments: dict) -> str:
        base = _root(injected)
        raw = arguments.get("path", "")
        old = arguments.get("old_string")
        new = arguments.get("new_string")
        if not isinstance(old, str) or not old:
            return "error: old_string must be a non-empty string"
        if not isinstance(new, str):
            return "error: new_string must be a string"
        try:
            target = resolve_path(raw, base)
        except ValueError as exc:
            return f"error: {exc}"
        if not target.exists():
            return f"error: file not found: {raw}"
        if target.is_dir():
            return f"error: is a directory: {_display(target, base)}"
        display = _display(target, base)
        matched = sensitive_match(display)
        if matched and not _allow_sensitive():
            # edit both READS the file (into the replace attempt) and rewrites
            # it; the guard applies to the read half, so refuse symmetrically.
            return _sensitive_refusal(display, matched)
        try:
            text = _read_text(target)
        except OSError as exc:
            return f"error: {exc}"
        count = text.count(old)
        if count == 0:
            return f"error: old_string not found in {_display(target, base)}"
        if count > 1:
            return (
                f"error: old_string occurs {count} times in "
                f"{_display(target, base)}; it must be unique"
            )
        try:
            _atomic_write(target, text.replace(old, new, 1))
        except OSError as exc:
            return f"error: {exc}"
        return f"edited {_display(target, base)} (1 replacement)"

    return handler


def make_list_dir_handler(root: Optional[Path] = None) -> Callable:
    injected = root

    def handler(arguments: dict) -> str:
        base = _root(injected)
        raw = arguments.get("path", ".")
        try:
            target = resolve_path(raw, base)
        except ValueError as exc:
            return f"error: {exc}"
        if not target.exists():
            return f"error: directory not found: {raw}"
        if not target.is_dir():
            return f"error: not a directory: {_display(target, base)}"
        try:
            entries = sorted(
                target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
        except OSError as exc:
            return f"error: {exc}"
        lines = []
        for entry in entries[:MAX_LIST_ENTRIES]:
            suffix = "/" if entry.is_dir() else ""
            lines.append(_display(entry, base) + suffix)
        if len(entries) > MAX_LIST_ENTRIES:
            lines.append(f"... [{len(entries) - MAX_LIST_ENTRIES} more entries]")
        return "\n".join(lines) if lines else "(empty directory)"

    return handler


def make_glob_handler(root: Optional[Path] = None) -> Callable:
    injected = root

    def handler(arguments: dict) -> str:
        base = _root(injected)
        pattern = arguments.get("pattern", "")
        if not isinstance(pattern, str) or not pattern.strip():
            return "error: pattern must be a non-empty string"
        pattern = pattern.replace("\\", "/")
        if Path(pattern).is_absolute():
            return "error: pattern must be relative to the file root"
        if ".." in pattern.split("/"):
            return "error: pattern must not contain '..'"
        try:
            start = resolve_path(arguments.get("root") or ".", base)
        except ValueError as exc:
            return f"error: {exc}"
        if not start.is_dir():
            return f"error: not a directory: {_display(start, base)}"
        try:
            matches = sorted(start.glob(pattern), key=lambda p: p.as_posix().lower())
        except (ValueError, OSError) as exc:
            return f"error: {exc}"
        lines = []
        for match in matches:
            if not _contained(match, base):
                continue
            suffix = "/" if match.is_dir() else ""
            lines.append(_display(match.resolve(), base) + suffix)
            if len(lines) >= MAX_GLOB_RESULTS:
                lines.append(f"... [stopped at {MAX_GLOB_RESULTS} matches]")
                break
        return "\n".join(lines) if lines else "(no matches)"

    return handler


def make_grep_handler(root: Optional[Path] = None) -> Callable:
    injected = root

    def handler(arguments: dict) -> str:
        base = _root(injected)
        pattern = arguments.get("pattern", "")
        if not isinstance(pattern, str) or not pattern:
            return "error: pattern must be a non-empty string"
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"error: invalid regex: {exc}"
        raw = arguments.get("path") or "."
        try:
            start = resolve_path(raw, base)
        except ValueError as exc:
            return f"error: {exc}"
        if not start.exists():
            return f"error: path not found: {raw}"
        max_results = _clamp(
            arguments.get("max_results", 100), 1, MAX_GREP_RESULTS
        )
        files = [start] if start.is_file() else _iter_files(start)
        if start.is_file():
            # An explicit grep on a single secret file is just a content read:
            # apply the same refusal as read_file.
            display = _display(start, base)
            matched = sensitive_match(display)
            if matched and not _allow_sensitive():
                return _sensitive_refusal(display, matched)
        results = []
        scanned = 0
        skipped_sensitive = 0
        for path in files:
            scanned += 1
            if scanned > MAX_GREP_FILES:
                results.append(f"... [stopped after {MAX_GREP_FILES} files]")
                break
            if not _contained(path, base):
                continue
            if sensitive_match(_display(path, base)) and not _allow_sensitive():
                skipped_sensitive += 1
                continue
            try:
                if path.stat().st_size > MAX_GREP_FILE_BYTES:
                    continue
            except OSError:
                continue
            try:
                text = _read_text(path)
            except OSError:
                continue
            if "\x00" in text:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    results.append(
                        f"{_display(path, base)}:{lineno}: {line.strip()[:300]}"
                    )
                    if len(results) >= max_results:
                        return "\n".join(results)
        note = (
            f" [skipped {skipped_sensitive} sensitive file(s)]"
            if skipped_sensitive
            else ""
        )
        return ("\n".join(results) if results else "(no matches)") + note

    return handler
