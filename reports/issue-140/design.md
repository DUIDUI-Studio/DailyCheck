# Issue 140 — integration design and blocking evidence

Task authority: task_c1ad7027772a / ctx_78107c9eea94. Sole integration writer; no delegation, merge, deployment or production access. Assigned feature playbook and three principles apply with project overrides. Branch: feat/issue-140-wms-owner-details, based on origin/main ccc0a76; fetched and merged origin/main (already up to date).

## Runtime baseline

GET /wms -> Cookie login -> distribution_center_only (including existing platform-admin bypass) -> MCP wms SELECT service -> optional pymssql. The stock/list/summary services all bind configured customer_id. Document rows contain only display numbers and no unique identifier or detail link. Inventory uses TOP capped at 500, without a next page. Issue #140 currently has no comments; open PR inventory has no #140 PR; remote branches have no issue-140 branch. Existing similarly named worktree is the coordinator context, not a second writer.

## Chosen domain boundary (before implementation)

Use a separate Web SELECT service beside mcp_server/service/wms.py, reusing connection/config/error handling only. Preserve the configured-owner MCP functions and registry unchanged. A warehouse-bound WebScope must come from trusted server evidence: only verified 朴宇 identity may read all owners; other warehouses retain configured-owner access. No arbitrary warehouse/customer query input can create or widen a scope.

InventoryPage contains deterministic rows, filtered matching count, page number/size and has-next/previous; each row includes stable balance identity, owner identity/name and goods identity. Ordering must use a verified unique stock key (not quantity alone); SQL Server 2008 R2 needs ROW_NUMBER rather than OFFSET/FETCH. Scope and owner/goods predicates apply before numbering and counting. Summary must label the same filter scope and avoid mixed-unit sums. Pagination describes current query data, not a cross-request frozen snapshot.

DocumentKey is validated direction plus verified unique header ID. DocumentDetail has header + owner + lines, with explicit missing, empty-lines and unavailable/truncated states. Lookup binds ID plus server scope; never retrieve unscoped then authorize. Back links reconstruct local list filters rather than accepting an arbitrary redirect URL.

Alternatives: expanding existing MCP functions risks single-owner boundary drift; removing customer predicate globally widens unrelated warehouses; a display document number is not established unique; TOP 500 is not complete browsing. Reject these alternatives. New service keeps Web and MCP authority explicit.

## Throughput checkpoint — baseline/schema phase

- [ ] Blocking first steps: complete native baseline pytest, trusted warehouse/schema evidence and route model before feature SQL.
- [x] Independent workstreams: n/a; SQL, routes and templates share one scope/identity contract.
- [x] Shared mutable state: sole writer in this worktree; coordinator supplies fresh read-only review.
- [x] Smallest safe decomposition: one integration owner prevents scope/identity drift; service -> routes/templates -> verification are sequential verifiable units.

## Evidence gap

Repository and trusted existing local WMS implementation establish header tables/IDs, stock columns and fixed customer behavior, but do not establish authorized warehouse ID/name mapping, customer-name table, balance unique key, detail tables/joins/line identity or actual supported lot/location fields. Must obtain sanitized Prod read-only schema/identity evidence through coordinator before implementing these SQL shapes. No external WMS connection has been made.

## Route/data sketch (schema-independent)

- GET /wms: parse a closed set of view/direction, positive bounded page size, page number, ISO date, exact goods code, selected warehouse and optional positive owner identity. Validate syntactic input before queries. Resolve scope from trusted server policy plus authorized warehouse choices. The selected owner only narrows that scope.
- GET /wms/documents/<direction>/<document_id>: same Cookie/DC decorators and blueprint no-store; validate direction/ID, resolve the same warehouse scope, return one scoped header and its scoped lines. Missing or outside scope share 404; invalid filters 400; unavailable query safe fixed 502/503, not an empty order. A header with zero lines explicitly says upstream returned no lines.
- Inventory and document list rows receive URLs from validated direction/unique ID, never from upstream text. Every upstream string stays Jinja-escaped. Return navigation uses url_for with named local filter fields; no arbitrary next URL.
- Selected scope is explicit in the heading. Owner options are produced inside warehouse scope; stock filters narrow rows and matching counts consistently. No quantity aggregation across units. Empty filters and true empty data are distinct from incomplete/unavailable data.
- Read operations reuse connection lifecycle and parameter binding. SQL identifiers come only from a fixed direction registry, never a query argument; bound warehouse and owner identities cannot change SQL structure.

## Verification plan

1. Focused service cases capture parameterized SELECTs and exercise stable multi-page traversal using >500 fixture rows from distinct owners, including colliding document display numbers and IDs across directions.
2. Real Flask app fixtures cover login/DC/viewer/non-DC/admin, no-store, all read routes refusing writes, filter/back-link preservation, owner and warehouse tampering, escaping, empty/missing/unavailable states and unchanged local stock.
3. MCP regression and contract/permissions/architecture gates demonstrate unchanged configured-owner tools; mutation checks remove the warehouse predicate or corrupt direction/ID mapping and must make focused assertions fail.
4. Local browser desktop/mobile evidence uses a clearly labelled stub upstream and temporary application databases. Prod alone verifies real upstream schema compatibility and actual multi-owner/details after deployment; fixture evidence is not a production claim.
5. Final complete native pytest result compared to baseline/known failures, changed-file Ruff, ordinary explicit origin push, one PR, and issue stage evidence only. Fresh review and terminal production tracking belong to coordinator/Prod.

## Baseline evidence so far

- `python reports/issue-140/reproduce_baseline.py`: native exit 0, both real rendered Flask document tables have no link; stock SELECT binds 155/TOP 500, no paging (upstream stub only). Output baseline-reproduction.txt and baseline-in/out.html.
- `python -m pytest -q tests/test_wms_dashboard.py tests/test_mcp_wms.py`: native exit 0, 52 passed in 12.43s. Output baseline-focused.txt.
- `python -m scripts.mcp_contracts --check`: native exit 0, 32 runtime / 19 external / 22 Bot approved. Output baseline-mcp-contracts.txt.
- `python scripts/arch_map.py --check`: native exit 0, source matches generated map. Output baseline-arch-map.txt.
- First attempted full run in pre-existing /tmp/dc-venv failed collection (httpx missing); discarded as incomplete baseline. Created isolated .venv using uv, installed project requirements + pytest/ruff/httpx; the replacement full run is the authoritative baseline when its native terminal summary completes. Dependency versions preserved in environment-packages.txt. No third-party WMS credentials were loaded or query issued.

## Completed baseline

` .venv/bin/python -m pytest -q --junitxml=reports/issue-140/baseline.xml ` completed with native exit **1**: **14 failed, 3614 passed, 39 warnings in 300.51s**. All 14 failures exactly match tests/known_failures.txt; native comparison `python scripts/check_known_failures.py reports/issue-140/baseline-pytest.txt` exits **0**. The initial missing-httpx attempt is not used as the baseline. The matching .txt/.xml/native-exit artifacts are preserved in this directory.

`python -m pytest -q tests/test_route_permission_matrix.py tests/test_arch_map.py` exits **0**: **62 passed in 3.89s** (baseline-permissions.txt).

Throughput checkpoint at schema hold: baseline complete; implementation remains blocked only on authoritative warehouse/schema evidence. The one-writer coupled-feature decomposition remains unchanged. No application code has been changed.


## Schema/scope gate resolved

Owner explicitly approved IDs (1,3), both 朴宇冷库 and 朴宇仓, via coordinator message msg_f1cb8604ba87. Default is both; individual selection narrows. Other pre-existing visible warehouses retain the configured customer boundary, never multi-owner. Coordinator authorized real SELECT evidence is external to Git at `/Users/ericmr/.cache/dailycheck-coordination/run_0cbbf33a0d93/upstream-schema-evidence.json`, SHA-256 `800f541cf28d52588fef9263098a16d0eed5918def761067b9c31e76cc57b110`. Only structural schema and approved policy are implemented; raw real data and credentials are not committed. Dev made no upstream connections.

Verified keys: goodsBalanceID; storeInID/storeOutID; storeInDetailID/storeOutDetailID; separate storeInPositionID/storeOutPositionID. Owner identity comes from sy_customer; goods code/name/unit from sy_goods. Detail qty/lotNo/storeLotNo and position allocations are supported. One-to-many positions are fetched separately, not joined into line quantities.

Throughput checkpoint — service phase: baseline/schema gates complete; one coupled workstream, one writer, sequential service then routes/templates then verification. Separate service selected over a parameter extending existing MCP authority. Service verification executes SQL after only SQL Server-to-SQLite dialect substitutions against synthetic schema, proves 607 rows without duplicate/omitted balance IDs, distinct owners with colliding goods codes, both document directions and colliding numbers, empty ID3, outside-scope rejection, unchanged configured-owner fallback, and independent location allocations. This is local fixture evidence, not upstream runtime verification.

## Web integration checkpoint

Routes/templates now share parsed Filters, WebScope, and server-bound owner choices. Existing unrelated warehouse choices require configured-owner stock/history and still constrain every query to that owner; no multi-owner fallback is allowed. Cookie login/DC/viewer access, permission manifest and generated architecture map stay synchronized. List/page counts and SKU summaries use the same predicates, detail URLs use direction+unique ID, and back URLs reconstruct named local filters.

Browser QA used Orca embedded browser against a temporary Flask harness with synthetic SQL fixture data and a visible fixture banner. Desktop inbound link -> detail -> original-filter return, mobile outbound link -> detail -> return, owner selection and next-page navigation all worked; mobile 390x844/coarse pointer had no root overflow and horizontal table scrolling. Source screenshots and browser JSON remain local under reports/issue-140; they are not production evidence or committed runtime assets. Four mutations (warehouse boundary, document identity, direction, duplicate qty) each yielded assertion failures and source was restored. The first final-suite attempt was deliberately superseded after strengthening direction-sensitive fixtures; only the subsequent complete run qualifies as final.


## Full-suite navigation regression and repair

The first complete post-change suite found 19 failures: 14 baseline plus five native navigation-contract failures because an unconfigured WMS list now returned 503. Preserve the established list-page 200 error rendering (with explicit safe error code and no result/empty-success content); details retain 502/503. The nav contract is legitimate during optional upstream outages, so this is an implementation fix, not a test or known-failures relaxation. That failing complete run is preserved as navigation-regression-pytest.txt/native exit 1. A new complete final suite follows the repair.

## Evidence archive

After verification, raw stdout/XML, mutation and local preview scripts, HTML, screenshots and browser JSON are moved without renaming into `/Users/ericmr/.cache/dailycheck-coordination/run_0cbbf33a0d93/writer-evidence-task_c1ad7027772a/`. Earlier commands above show their original generation paths; the archive preserves original output, including the interrupted attempt and the rejected navigation-regression run. Only design and standard reports are committed. Current complete final output is checked before archival and is the only output accepted for final counts/known-failures comparison. The code candidate remains frozen for independent review; report-only descendants do not change application source.
