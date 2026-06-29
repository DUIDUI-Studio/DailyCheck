# Recipe publish — subproject 5 plan

Spec: `2026-06-29-recipe-publish-design.md`. PRD §2.5.

## Tasks

### T1 — schema (RED → GREEN)
- master.db: `recipe_publish_events`, `recipe_publish_event_warehouses`.
- warehouse.db: `product_bom_versions`, `product_bom_store_versions`, `products.current_version_id`.
- Test: schema exists.

### T2 — pure fn `create_new_bom_version` (RED → GREEN)
- 3 cases: new product → version=1 / existing version=2 → version=3 / no reuse.
- Impl: `blueprints/publish_recipe_pure.py`.

### T3 — POST /admin/publish/recipe (RED → GREEN)
- 5 cases: 404 product / 400 empty_bom / full success (2 wh) / partial success / emit_event → /notifications shows 1.
- Impl: blueprint view, per-store try/except, calls `emit_event` at end.

### T4 — nav + land cards
- Admin nav: "配方发布" → /admin/publish/recipe.
- Land card: admin only.
- Tests: 2 simple.

### T5 — verify + draft PR
- `pytest -q` green, push `feat/recipe-publish`, draft PR.

## Branch

`feat/recipe-publish` based on `preview-phase1` (depends on subproject 3 `emit_event` which is already on `preview-phase1`).
