# Finite ordinary release credentials

This tooling prepares a reusable entry for the existing GitHub ordinary lane.
It is not installed or activated by merging its source. Production admission,
independent review, installation, credential grants and deployment remain Prod
operator actions. It does not fix the Web signing key or create private issues.

## Shape and trust boundary

`release_credentials.py` renders the repository templates `release_entry.py` and
`release_selector.py` into a new private directory. The generated selector embeds
one explicit operator admission; the generated entry embeds its selector SHA256.
There is no runtime profile file, environment selector, token service, new App
installation/scope, or background credential renewal. An operator must obtain and
review the source and all pins before privileged installation outside candidate
custody. Rendering candidate-supplied values with root privileges is not admission.
Root is the trusted operator; this mechanism does not sandbox malicious root code.

The rendered entry execs the unchanged `deploy_state.py run` to take the real OS
flock. Under that inherited FD it validates the exact baseline and source binding,
then calls the existing fixed App/repository role shim via its supported `--exec`
protocol. A one-use inherited pipe returns to the same PID/PGID before execing the
ordinary pipeline. The existing pipeline selects this generated selector through
its `PYTHON` interface. Other Python commands transparently exec the real Python.

At `deploy_merge.py merge`, the selector validates the private preserved helper
closure, exact PR/H/B/session/AttemptIdentity, `merge-start`, deployment provenance,
generation, native barrier, process FD and source binding. It renews once, checks
those facts again through the bound continuation, then execs the real helper.
At `deploy_report.py write`, it requires the matching released outcome, token,
session/generation/rc/stage and source binding before its own single renewal.
Each new helper constructs fresh HostingConfig/HostingClient objects. Refreshing
files never refreshes a client already running in another process.

`invocation.json` and `renewal-intent-{startup,finalizer,typed-report}.json` are
exclusive, fsynced consumption records, not authority or success receipts.
A settled failure may leave current generation ahead of the deployed anchor;
both are explicitly pinned separately, without deleting that failure. The same
previously consumed PR/H remains rejected. Failure, interruption or UNKNOWN never deletes a slot or automatically repeats a
renewal/write. A second render refuses an existing directory. The operator must
reconcile native outcome and process custody before any separately authorized
attempt; changing a directory/session does not reset issue-level counters.
The real finalizer and durable publication adapter retain their independent
checks, state transactions, finite writes and uncertainty handling.

## Explicit admission

The JSON object must contain exactly these fields (unknown fields are rejected):

| Fields | Required operator facts |
| --- | --- |
| `uid`, `python` | Production root UID 0 and the approved absolute venv interpreter |
| `root`, `repo`, `state`, `log`, `shim` | Exact absolute private bundle, checkout, state, log and fixed shim paths |
| `tmp` | Exactly `<root>/scratch/tmp` |
| `pr`, `head`, `base`, `generation`, `deployed_generation` | Exact positive integer PR, full lowercase H/B, current attempt generation and deployed anchor generation separately |
| `session`, `task`, `dispatch` | Exact unique attempt identity and current orchestration lineage |
| `shim_sha` | Reviewed SHA256 of the existing fixed App/repo role shim |
| `binding_sha256` | Real resolver digest of the independently registered exact private source mapping |
| `pins` | Reviewed SHA256 for every path listed below; no additions or omissions |

The eight required pins are `scripts/blue_green/deploy_pipeline.sh`,
`deploy_state.py`, `deploy_merge.py`, `deploy_report.py`, `publication_authority.py`,
`deploy_triage_pure.py` in that same directory, `scripts/hosting.py`, and
`scripts/prod_agent/github_readiness.py`. The renderer does not discover or approve
hashes from the candidate. The startup checkout and preserved helper closure must
match those pins. State, hosting and publication schema modules are also re-read
from the checkout at late boundaries; these three must retain admitted bytes
across checkout/restore. A protocol change requires independent review and a new
explicit operator admission.

Preparation syntax (paths are placeholders, not a live release command):

```text
python -B -m scripts.blue_green.release_credentials --admission <reviewed.json> --destination <exact-new-root>
```

The destination must equal `root`, remain outside the candidate checkout, and not
already exist. Install the reviewed bytes under root custody: bundle and scratch
directories 0700, entry/selector 0700, records 0600, safe parent custody, no links.
Do not execute repository templates directly; their admission constants are unset.
After separate admission the caller selects generated `entry.py --execute-admitted`
with the approved interpreter. An execution flag alone supplies no approval.

The finite native caller must retain one Popen/session, record PID/start before
receive, positively wait and retain native rc/ECHILD and descendant settlement.
Timeout, SSH loss or a vanished listener cannot justify relaunch or cleanup.
The entry preserves that caller relationship; it does not replace it with a daemon.
The pipeline still owns full verify, real observation >=10 minutes, rollback,
finalization and typed reporting. A typed-report failure remains incomplete
publication even when the native pipeline retains an earlier success exit code.

## Private source registration and early admission

`publication_authority.resolve_source_binding(state_dir, pr)` is the schema and
digest authority. The entry calls it under the existing state transaction **before
startup renewal, acquire, checkout, candidate stages or remote writes**, and checks
its digest against admission. It rechecks on each late phase and continuation.
Missing/invalid/ambiguous mappings and changed exact mappings reject the entry.
The ordinary pipeline also resolves the same schema under its real inherited
fence before acquire/candidate stages, so direct invocation fails early too. Only
the explicit credential entry additionally binds the operator-supplied mapping
digest and prevents a missing mapping from reaching startup renewal.

The existing `bindings.json` shape is:

```json
{"version":1,"bindings":[{"pr":160,"public":{"provider":"github","host":"github.com","repository_id":1448986421,"repository":"DUIDUI-Studio/DailyCheck"},"source":{"provider":"github","host":"github.com","repository_id":1449125557,"repository":"DUIDUI-Studio/DailyCheck-ops","kind":"issue","number":7}}]}
```

These are illustrative distinct numbers. Obtain the actual private issue identity
from the authorized private source; never infer it from a public issue or PR
number. A GitHub source must match the fixed ops repository identity; the existing
provider-qualified Gitea source policy remains supported. An optional
`source_revision` follows the existing resolver's exact six-field schema
(`provider`, `host`, `repository_id`, `repository`, full `sha`,
`source_public_map_sha256`). Do not weaken identity validation for registration.

Only the separately authorized privileged operator/installer may register a mapping.
It must use `deploy_state.publication_config_transaction(directory)` (the same
`.attempt.lock` then `.state.lock`), preserve all existing entries, validate the
complete proposed document with the actual resolver, and atomically replace the
private configuration with fsynced bytes and directory. Never edit bindings while
holding an inherited deployment FD, bypass this transaction, auto-create an issue,
or reuse an old executor/consumed directory. There is no automatic installer here.
After registration, read the exact mapping through the resolver and explicitly
carry its `binding_sha256` into the separately reviewed admission.

Report diagnostics now retain a bounded local HostingError code, including
`private_source_binding_missing`; arbitrary exception text and credential values
are not printed. Private diagnostics/outcomes and the original failure survive.

## Offline validation and cleanup

`tests/test_release_process.py` uses an owned fake shim and HTTP/Git executable
ports with the unchanged native state, finalizer, report and publication consumers.
It invalidates the startup token deterministically and verifies late native HTTP
uses the newly constructed client's refreshed value. It also checks PID/PGID/FD,
contender exclusion, exact identity refusal, duplicates/UNKNOWN and positive
waits/ECHILD. No production profile or real App is executed.

Preserve consumption records, original failed results and review evidence. Remove
only this attempt's scratch/temp files after positive native process and descendant
settlement and evidence capture. Never delete somebody else's state or erase an
UNKNOWN slot as cleanup. Tests use task-owned system temporary directories.
