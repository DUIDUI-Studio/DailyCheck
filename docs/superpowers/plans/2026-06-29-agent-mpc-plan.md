# Agent MPC — subproject 6 plan

Spec: `2026-06-29-agent-mpc-design.md`. PRD §2.3.

## Tasks

### T1 — schema (RED → GREEN)
- master.db: `agent_tokens` table.
- Test: table + columns.

### T2 — pure fn `path_matches` (RED → GREEN)
- 5 cases: exact / prefix / wildcard / no match / trailing slash.
- Impl: `blueprints/agent_mpc_pure.py`.

### T3 — auth decorator + token verify (RED → GREEN)
- Helper: `verify_token(request) -> agent_token_row | None` reading `Authorization: Bearer ...`.
- Helper: `check_path_allowed(row, method, path) -> bool`.
- Tests: 6 cases (missing header / wrong token / revoked / path ok / path forbidden / warehouse ok / warehouse forbidden).

### T4 — read routes (RED → GREEN)
- 4 read paths covered: `/api/v1/items`, `/api/v1/items/<id>`, `/api/v1/movements`, `/api/v1/categories`, `/api/v1/templates`, `/api/v1/forecast/item/<id>`, `/api/v1/procurement/store`, `/api/v1/procurement/hub`, `/api/v1/notifications/feed` (returns empty).
- Tests per route: success + 400 warehouse_code_required + 403 path/warehouse.

### T5 — write routes (RED → GREEN)
- 3 write paths: `/api/v1/restock` (POST), `/api/v1/procurement/recompute` (POST), `/api/v1/forecast/recompute` (POST).
- Test: each write requires path in write whitelist.

### T6 — access.log JSON lines
- Helper: `_write_mpc_access_log(token_id, method, path, status, duration_ms)` appends JSON.
- Test: monkeypatch file path, call once, verify JSON line.

### T7 — /admin/mpc-usage page
- Aggregate token stats (count, error_rate, last_call).
- Tests: 2 (empty / with calls).

### T8 — nav + land cards
- Admin nav: "MPC 用量" → /admin/mpc-usage.
- Tests: 2 simple.

### T9 — security tests
- SQL injection attempt via path param → must be parameterized.
- CSRF: API must not require session cookie (Authorization header only).
- Tampered token (extra chars) → 401.

### T10 — verify + draft PR
- `pytest -q` green, push `feat/agent-mpc`, draft PR.

## Branch

`feat/agent-mpc` based on `preview-phase1` (depends on subproject 1 forecast + subproject 2 procurement read logic, both on preview-phase1).
