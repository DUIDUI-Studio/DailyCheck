# Agent token permission management (#24)

## Creation contract

Platform-admin authentication and CSRF protection apply to preview and confirmation.
The Web form defaults to no read paths, no write paths and no warehouses. Paths are
comma-separated `/api/v1/...` prefixes, optionally ending in `*`; a standalone `*`
grants all paths. A prefix includes descendants. Invalid paths, unknown warehouses
and inconsistent scope/checkbox input fail before token generation.

Warehouse semantics remain unchanged: JSON `null` is all current and future
warehouses; `[]` is no warehouses; a list restricts access to those codes. The new
form explicitly sends its scope, with `none` as default. Legacy empty warehouse
input still normalizes to `null`; it cannot silently create a token because the
server first shows that effective scope for review. Multiple selected warehouses
are retained with `getlist`, including legacy comma-separated input.

Preview creates no token. The server signs the normalized ACL and administrator ID
for ten minutes; confirmation reads that payload rather than unsigned form ACLs,
rechecks warehouse existence and requires an extra checkbox for all warehouses or
wildcard paths. Return to the form to change the proposal. After creation the list
displays read/write paths and distinguishes all/no warehouses. The existing audit
records the creator and normalized ACL without token material. Listing and creating
a new token do not rewrite existing ACLs. Named independent tokens and revocation
remain supported. No existing official credential is exercised or modified here.

## Templates and in-place updates: considered, deferred

Multiple independent named tokens already exist. A template/update endpoint is
not needed to fix the error flag, cache safety or creation UI, and would add an
unnecessary mutation surface to this PR. No such endpoint is introduced.

A follow-up design should satisfy these reviewable conditions before implementation:

- Templates store versioned ACL proposals, never secrets or live links. Applying a
  template copies a snapshot into the same preview flow. Editing a template never
  changes issued tokens, and has no wildcard/default fallback.
- Create, template management and ACL update require platform-admin authorization,
  CSRF for session callers and input validation identical to issuance. API callers
  need a dedicated admin identity; a normal MCP bearer token cannot manage ACLs.
- In-place updates require the expected ACL revision (compare-and-swap). Display
  before/after read/write/warehouse sets; reject stale proposals. Treat null as all
  including future warehouses, [] as none. Prefix/wildcard additions are potential
  expansion, not harmless string substitutions. If containment cannot be proven,
  require explicit expansion confirmation; never auto-inherit broader defaults.
- Record actor, token ID, old/new ACL, template version, request ID and outcome in
  durable audit atomically with the update. The current best-effort file audit is
  insufficient for this guarantee; settle durable storage before adding updates.
  Audit must exclude raw tokens, hashes and encrypted token values.
- Keep token ID/hash/secret, creation metadata and revocation state unchanged.
  A revoked token cannot be revived by an ACL update. Create/list/revoke remain
  available. Cross-admin/stale/denied/failed-audit tests must prove zero mutations;
  separate tests must prove explicit approved expansion and null/[] behavior.

This is a design proposal, not implemented update/template behavior or production
acceptance evidence. It does not authorize changing any official token.
