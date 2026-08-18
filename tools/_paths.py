"""Shared path helpers for local doc-gen tools (create_docx, create_pptx)."""

from __future__ import annotations

import os
import re
from pathlib import Path


def default_output_dir() -> Path:
    """Output directory, overridable via AGENT_CORE_OUTPUT_DIR."""
    return Path(os.environ.get("AGENT_CORE_OUTPUT_DIR", "output"))


def slugify(text: str) -> str:
    """Turn a title into a safe filename stem."""
    slug = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().replace(" ", "_")
    return slug or "document"


def unique_path(out_dir: Path, stem: str, ext: str) -> Path:
    """Return a non-colliding path in out_dir, suffixing _2, _3, ... if needed.

    Claims the filename atomically with O_CREAT|O_EXCL so two concurrent
    callers can't both pick the same path (the check-then-save race in the
    old ``while path.exists()`` loop). The file is created empty; callers
    overwrite it via their own save.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        name = f"{stem}.{ext}" if n == 1 else f"{stem}_{n}.{ext}"
        path = out_dir / name
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return path
        except FileExistsError:
            n += 1