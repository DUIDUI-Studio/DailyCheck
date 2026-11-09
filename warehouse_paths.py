"""Resolve stored warehouse paths against an explicitly selected data root.

Configured resolution requires both ``db_dir`` and ``base_dir``, even for
absolute metadata paths. Absolute metadata paths are retained. The legacy
``db/...`` registration prefix maps onto that configured directory (for example
``db/warehouses/x.db`` becomes ``<db_dir>/warehouses/x.db``) even when the
configured directory is not named ``db``. When the configured directory is
inside ``base_dir``, its exact code-relative prefix takes precedence, retaining
existing registrations such as ``db/custom-data/warehouses/x.db``. Other
relative forms retain the application base fallback. Bot callers pass only
their explicit base, suppressing configured remapping. Without either argument
the raw stored path is returned, preserving legacy CWD-relative behavior.
"""
from __future__ import annotations

from pathlib import Path


def resolve_warehouse_path(stored: str | Path, base_dir: str | Path | None = None,
                           *, db_dir: str | Path | None = None) -> Path:
    if db_dir is not None and base_dir is None:
        raise ValueError("base_dir is required when db_dir is supplied")
    path = Path(stored)
    if path.is_absolute():
        return path
    if db_dir is not None and path.parts and path.parts[0] == "db":
        data = Path(db_dir)
        if base_dir is not None and data.is_relative_to(Path(base_dir)):
            prefix = data.relative_to(Path(base_dir))
            if prefix != Path(".") and path.is_relative_to(prefix):
                return Path(base_dir) / path
        return data.joinpath(*path.parts[1:])
    if base_dir is not None:
        return Path(base_dir) / path
    return path


def warehouse_registration_path(db_path: str | Path, *, base_dir: str | Path,
                                db_dir: str | Path) -> str:
    """Return the existing portable registration form for a newly created DB.

    Files under the code root keep the exact legacy ``relative_to(base_dir)``
    form, including nested data roots such as ``db/custom-data``. The resolver
    recognizes that configured prefix before portable ``db/`` remapping.
    Files under a configured external data root are stored as the legacy
    ``db/...`` form so the same resolver maps them back without a metadata
    rewrite.
    """
    path = Path(db_path)
    if path.is_relative_to(Path(base_dir)):
        return str(path.relative_to(Path(base_dir)))
    if path.is_relative_to(Path(db_dir)):
        return str(Path("db") / path.relative_to(Path(db_dir)))
    raise ValueError("warehouse file outside configured data roots")
