# MCP tool contracts (generated)

Source: `mcp_server/protocol/registry.py`. Regenerate with `python -m scripts.mcp_contracts --write`; verify in CI with `python -m scripts.mcp_contracts --check`.

Runtime: **29** tools; external integration subset: **16**; Bot approved: **22**; business writes: **10**.

`registry.json` contains every input declaration, handler, minimum output JSON shape, ACL enforcement/scope, test reference and compatibility status. `tools.json` intentionally publishes only the external integration subset. Neither file grants permissions.

Read/write classifies business intent; legacy HTTP/stdio reads may run migrations/cache writes. Bot read contexts remain read-only. Output shapes describe the existing JSON inside TextContent; no SDK outputSchema is enabled. `items_detail` keeps its list wrapper, `procurement_hub` keeps its array, `canonical_fanout_event` can return null, and `canonical_fanout` keeps per_canonical rather than the older description's warehouses/items.

| Tool | Domain | Mode | ACL path | Path enforcement | Warehouse scope | External subset | Bot approved |
|------|--------|------|----------|------------------|-----------------|-----------------|--------------|
| `warehouse_list` | inventory | read | `GET /api/v1/warehouses` | none; Bot adapter checks the documented path | filter warehouses by token | yes | yes |
| `items_list` | inventory | read | `GET /api/v1/items` | handler | warehouse_code checked in handler/service | yes | yes |
| `items_detail` | inventory | read | `GET /api/v1/items/<id>` | handler | warehouse_code checked in handler/service | yes | yes |
| `movements_list` | inventory | read | `GET /api/v1/movements` | handler | warehouse_code checked in handler/service | yes | yes |
| `restock_create` | inbound | write | `POST /api/v1/restock` | handler | warehouse_code checked in handler/service | yes | yes |
| `restock_list` | inbound | read | `GET /api/v1/restock` | handler | warehouse_code checked in handler/service | yes | yes |
| `item_forecast` | forecast | read | `GET /api/v1/forecast/item/<id>` | handler | warehouse_code checked in handler/service | yes | yes |
| `procurement_store` | procurement | read | `GET /api/v1/procurement/store` | handler | warehouse_code checked in handler/service | yes | yes |
| `procurement_hub` | procurement | read | `GET /api/v1/procurement/hub` | handler | filter authorized warehouses before aggregation | yes | yes |
| `outbound_create` | outbound | write | `POST /api/v1/outbound` | handler | warehouse_code checked in service | yes | yes |
| `outbound_list` | outbound | read | `GET /api/v1/outbound` | handler | warehouse_code checked in service | yes | yes |
| `outbound_rollback` | outbound | write | `POST /api/v1/outbound/rollback` | handler | warehouse_code checked in service | yes | yes |
| `warehouse_consumption` | consumption | read | `GET /api/v1/consumption/warehouse` | none; Bot adapter checks the documented path | warehouse_code checked in service | yes | yes |
| `item_consumption` | consumption | read | `GET /api/v1/consumption/item/<id>` | none; Bot adapter checks the documented path | warehouse_code checked in service | yes | yes |
| `canonical_list` | canonical | read | `GET /api/v1/canonical` | handler | global; no token warehouse filtering | no | yes |
| `canonical_detail` | canonical | read | `GET /api/v1/canonical/<id>` | handler | global bindings; no token warehouse filtering | no | yes |
| `canonical_categories` | canonical | read | `GET /api/v1/canonical/categories` | handler | global; no token warehouse filtering | no | yes |
| `canonical_fanout_targets` | canonical | read | `GET /api/v1/canonical/fanout-targets` | handler | global; no token warehouse filtering | no | yes |
| `canonical_conflicts` | canonical | read | `GET /api/v1/canonical/conflicts` | handler | global; no token warehouse filtering | no | yes |
| `canonical_fanout_event` | canonical | read | `GET /api/v1/canonical/fanout/event/<id>` | handler | global; no token warehouse filtering | no | yes |
| `canonical_create` | canonical | write | `POST /api/v1/canonical` | handler | global; no token warehouse filtering | no | no |
| `canonical_update` | canonical | write | `POST /api/v1/canonical/<id>/update` | handler | global; no token warehouse filtering | no | no |
| `canonical_claim` | canonical | write | `POST /api/v1/canonical/claim` | handler | warehouse_code checked in service | no | no |
| `canonical_unclaim` | canonical | write | `POST /api/v1/canonical/unclaim` | handler | warehouse_code checked in service | no | no |
| `canonical_fanout` | canonical | write | `POST /api/v1/canonical/fanout` | handler | explicit warehouse_codes; no token warehouse filtering; distribution center required | no | no |
| `canonical_resolve_conflict` | canonical | write | `POST /api/v1/canonical/conflicts/<id>/resolve` | handler | global; no token warehouse filtering | no | no |
| `canonical_set_binding_active` | canonical | write | `POST /api/v1/canonical/binding-active` | handler | warehouse_code checked in service | no | no |
| `store_order_list` | store_order | read | `GET /api/v1/store-orders` | handler; exact path or path plus slash | any participating warehouse grants full order visibility, including other warehouses; optional warehouse filter | yes | yes |
| `store_order_detail` | store_order | read | `GET /api/v1/store-orders/<id>` | handler; exact path or path plus slash | any participating warehouse grants full order visibility, including other warehouses | yes | yes |
