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
    """Return a non-colliding path in out_dir, suffixing _2, _3, ... if needed."""
    path = out_dir / f"{stem}.{ext}"
    n = 2
    while path.exists():
        path = out_dir / f"{stem}_{n}.{ext}"
        n += 1
    return path