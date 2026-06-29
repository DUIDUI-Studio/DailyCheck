# Item publish — subproject 4 plan

Spec: `2026-06-29-item-publish-design.md`. PRD §2.4.

## Tasks

### T1 — schema (RED → GREEN)
- Test: `publish_templates`, `template_versions`, `publish_events`, `publish_event_items` tables exist; `items.created_by_publish_event_id` column exists.
- Impl: extend `db/__init__.py` schema.

### T2 — pure fn `compute_publish_diff` (RED → GREEN)
- 4 cases: add / skip / conflict (single field diff) / empty store.
- Impl: `blueprints/publish_items_pure.py`.

### T3 — POST /admin/publish/items/preview (RED → GREEN)
- 5 cases: 404 unknown template_version / 400 unknown warehouse / add / skip / conflict.
- Impl: blueprint view.

### T4 — POST /admin/publish/items/confirm (RED → GREEN)
- 7 cases: 400 missing_resolutions / 400 unresolved_conflicts / keep_store / overwrite / merge / partial success (1 ok + 1 fail) / created_by_publish_event_id set.
- Impl: blueprint view, per-store try/except.

### T5 — nav + land cards
- Admin nav: add "品项发布" link to /admin/publish/items/preview.
- Land card: add for admin.
- Tests: 2 simple "nav contains X" tests.

### T6 — verify + draft PR
- `pytest -q` green, ruff clean, push branch `feat/item-publish`, draft PR.

## Dependency order

T1 → T2 → T3 → T4 → T5 → T6 (sequential).

## Branch

`feat/item-publish` based on `preview-phase1` (which already has 1+2+3 + nav).
