"""Resolve stored warehouse paths against an explicitly selected data root.

Absolute metadata paths are retained. A caller that omits base_dir keeps the
legacy CWD-relative behavior; bot callers always supply db_dir().parent.
"""
from __future__ import annotations

from pathlib import Path


def resolve_warehouse_path(stored: str | Path, base_dir: str | Path | None = None) -> Path:
    path = Path(stored)
    return Path(base_dir) / path if not path.is_absolute() and base_dir is not None else path
