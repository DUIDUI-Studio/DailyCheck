# Protected native preparation contract

This directory contains the inert launcher template for the H/G production
controller. Nothing here installs, provisions, enables or restarts a service.
Source/installation acceptance is separate from Prod operations.

## Fixed entrypoints

Installed later by Prod (after separate approval):

| Entrypoint | Mode | Purpose |
|---|---|---|
| `/usr/local/sbin/dailycheck-native` | root:root 0755 | Fixed isolated launcher rendered from `dailycheck-native.in` |
| `/etc/dailycheck-native/policy.json` | root:root 0600 | Only the filled, approved `InstalledPolicy` |
| `/var/lib/dailycheck-native-records/preparation-plan.json` | root:root 0600 | Approved `PreparationPlan` |
| `/var/lib/dailycheck-native-records/baseline-evidence.json` | root:root 0600 | `BaselineEvidence` |
| `/var/lib/dailycheck-native-records/source-public-map.json` | root:root 0600 | Approved source/public map (rows) |
| `/var/lib/dailycheck-native-records/observation-ledger.json` | root:root 0600 | Real `ObservationLedger` (>=600 s) |
| `/var/lib/dailycheck-native-records/runtime.json` | root:root 0600 | `RuntimeRecord` written only by the publisher |
| `/var/lib/dailycheck-native-records/baseline-transition.json` | root:root 0600 | `BaselineTransition` consumed by the unchanged initializer |
| `/var/lib/dailycheck-native-reviews/requests/<uid>/<candidate>.json` | root:root 0640, group reviewer | Review request (non-secret digests + references) |
| `/var/lib/dailycheck-native-reviews/<uid>` | reviewer:reviewer 0700 | Reviewer-owned result directory (created at installation) |
| `/var/lib/dailycheck-native-reviews/<uid>/<candidate>.json` | reviewer:reviewer 0600 | Reviewer-owned result |

The successor `policy.closure` must additionally pin
`scripts/prod_agent/native_prepare.py`; the H10 workflow/producer pins and the
original installation packet stay frozen.

## Operations

```
dailycheck-native render-preparation      # inert units + launcher + generated manifest
dailycheck-native export-baseline         # root + original attempt fence + uninitialized state
dailycheck-native publish-runtime --kind baseline|green
dailycheck-native publish-baseline        # root + existing fence; writes only the transition
dailycheck-native prepare-review-request  # authenticated candidate/CI bindings
dailycheck-native publish-review --candidate-digest <sha256> --decision pass
```

- `render-preparation` is pure: it writes no host file and changes no state. The
  output lists exact before/after unit targets, the four fixed units, the
  launcher bytes and their SHA256s. Runtime/package hashes are measured after
  separate provisioning; a system-Python shortcut is rejected.
- `export-baseline` creates or byte-verifies only
  `<release_root>/<public_sha>` plus `release.json`. It never updates refs,
  changes main/anchor/state, or imports application code as root.
- `publish-runtime` runs as a separate observer: no attempt descriptor, attempt
  identity, key or inherited application environment. Baseline requires a real
  >=600 s continuity ledger; green requires the actual completed stage journal
  (including its real >=600 s observation). It writes atomically and reads back
  through unchanged `ProtectedRuntime` validation. Reuse of a baseline ledger
  is only valid while its process/route identity still matches live observation;
  on drift, remove the ledger so a new >=600 s window is recorded.
- `publish-baseline` re-reads the approved outcome/map/tree and the actual
  remote main through the protected read client. It emits only the fresh
  transition file; it never initializes, signs or grants.
- `prepare-review-request` is root-side binding generation only. The reviewer
  process under the designated `dc-native-review` UID reads its protected
  request, verifies its effective UID, and records its own explicit `pass`.
  Root/app accounts cannot invoke `publish-review` or supply a reviewer string.

## Prod operations contract (minimum)

1. Provision the separate control/app runtimes, accounts, bind-mounted DB and
   secrets under separate approval; do not copy the H10 template verbatim.
2. Fill and approve the plan/evidence/map/ledger records at the fixed paths;
   unresolved values must make every operation fail.
3. Install the rendered launcher and `policy.json`; keep all records root:0600.
4. Apply/restart units, reload nginx and switch traffic only as an explicit
   Prod operation. This source task performs none of those actions.
5. Retain the actual command/native-exit observations with the deployment
   report. Local fixture evidence is not production acceptance.
