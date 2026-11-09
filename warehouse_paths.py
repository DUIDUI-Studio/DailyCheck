"""Resolve stored warehouse paths against an explicitly selected data root.

Absolute metadata paths are retained. When ``db_dir`` is supplied, the legacy
``db/...`` registration prefix maps onto that configured directory (for example
``db/warehouses/x.db`` becomes ``<db_dir>/warehouses/x.db``) even when the
configured directory is not named ``db``. An explicit ``base_dir`` then keeps
its documented application fallback and Bot precedence for all other relative
forms; Bot callers pass only ``db_dir().parent``. Without either argument the
raw stored path is returned, preserving the legacy CWD-relative behavior.
"""
from __future__ import annotations

from pathlib import Path


def resolve_warehouse_path(stored: str | Path, base_dir: str | Path | None = None,
                           *, db_dir: str | Path | None = None) -> Path:
    path = Path(stored)
    if path.is_absolute():
        return path
    if db_dir is not None and path.parts and path.parts[0] == "db":
        return Path(db_dir).joinpath(*path.parts[1:])
    if base_dir is not None:
        return Path(base_dir) / path
    return path


def warehouse_registration_path(db_path: str | Path, *, base_dir: str | Path,
                                db_dir: str | Path) -> str:
    """Return the existing portable registration form for a newly created DB.

    Files under the code root keep the exact legacy ``relative_to(base_dir)``
    form. Files under a configured external data root are stored as the legacy
    ``db/...`` form so the same resolver maps them back without a metadata
    rewrite.
    """
    path = Path(db_path)
    if path.is_relative_to(Path(base_dir)):
        return str(path.relative_to(Path(base_dir)))
    if path.is_relative_to(Path(db_dir)):
        return str(Path("db") / path.relative_to(Path(db_dir)))
    raise ValueError("warehouse file outside configured data roots")
