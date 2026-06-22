# clone-warehouse CLI

**Date:** 2026-06-22
**Status:** Implemented

## Context

Creating a new warehouse currently requires manually copying `db/warehouses/wh_NNN.db` or running one-off Python scripts. Both are error-prone (FK collisions, stale stock values). This adds a first-class CLI command.

## Design

`flask --app app clone-warehouse <src_code> <new_code> <name>`

1. Validates `<src_code>.db` exists and `<new_code>.db` does not.
2. Initializes the destination DB schema via existing `init_warehouse_db` (also seeds FIXED_CATEGORIES).
3. Calls `db.clone.clone_warehouse_catalog()` to copy catalog with FK remapping and stock reset.
4. Registers the new warehouse in `master.db` (mirrors `create-warehouse`).

## Trade-offs

- Stock reset to 0 because a new store has no inventory. If a future flow needs to inherit stock, add a `--keep-stock` flag (YAGNI for now).
- Categories matched by name (not ID). FIXED_CATEGORIES are pre-seeded with stable IDs, so this is safe; custom categories created in the source are added to the destination.

## Verification

- `tests/test_clone_warehouse.py` covers FK remap, quantity reset, and field preservation.
- Manual smoke test: `flask --app app clone-warehouse wh_002 wh_demo 演示店`