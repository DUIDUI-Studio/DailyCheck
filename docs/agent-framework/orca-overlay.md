# DailyCheck × Orca Overlay

> Scope: DailyCheck-specific adoption. Explicit Owner decisions take precedence;
> project authority and delivery follow [`workflow.md`](workflow.md).
> Generic execution policy lives in `~/.agents/AGENTS.md`; Orca mechanics come
> from the installed, version-matched `orca-cli` / `orchestration` guides.

## 1. Complete-result ownership

The current Dev normally owns investigation, design, related implementation,
repair, validation, PR and delivery follow-up in one context. A multi-file task
or a skill invocation does not require another agent. Frontend, backend,
planning and testing are capabilities, not mandatory handoff stages.

Use native helpers for worthwhile independent auxiliary results. Use Orca workers
when cross-CLI/account/environment execution, separate workspaces, formal
independence or supervised lifecycle is needed, or the owner explicitly requests
Orca. Assign a complete verifiable outcome, not a sequence of command-sized tasks.

A dedicated Coordinator is conditional on multiple independent workstreams or an
explicit assignment. It remains read-only for business code. A main executor
managing helpers retains write ownership. One shared worktree has one writer;
formal Review is an independent read-only session. Only Prod may merge, deploy
or close an Issue. An open PR does not automatically transfer ownership.

## 2. Project gates

- Branch: `{fix|feat|chore|docs|ci|test|refactor|perf}/issue-<N>-<slug>` (H1).
- One Issue, one branch, one PR (H2), with workflow.md's same-root-cause exception.
  Check current claims, branches and PRs before starting; reuse a suitable isolated
  task worktree rather than creating another layer of workspaces.
- PR body has exactly one each of `## 报告 1/4：需求对齐`, `## Checklist` and
  `## 生产侧事项`; no literal red-cross placeholder remains.
- Required checks are `test` and `agent-framework-reports`. Verify actual platform,
  workflow/run/evidence identity; Gitea renders their full `CI / … (pull_request)` names.
- Commits touching architecture subsystems must update `docs/ARCHITECTURE.md` in
  the same commit. Read its overview and the affected sections before changes.
- Expanding known failures, editing CI/protection/workflow, or weakening architecture
  iron rules requires Owner authorization. Record an already granted decision;
  do not ask again for the same approved change.

## 3. Delivery and recovery

| State | Evidence / next owner |
|---|---|
| `in_ci` | Checks pending; main executor or assigned Coordinator follows |
| `ci_failed` / `merge_conflict` | Original writer repairs in the same work item |
| `review_blocker` | Writer repairs; fresh independent Reviewer verifies |
| `awaiting_prod` | Checks pass, PR open; confirm Prod actually received the item |
| `awaiting_deploy` | Merged without successful deployment evidence; Prod owns action |
| `deploy_failed` | Code → original writer; environment/infrastructure → Prod |
| `deploy_unverified` | Observation budget exhausted without verified anchor; retain unresolved state |
| `delivered` | PR merged + Issue closed + required CI successful + successful verified deployment |

Keep the original acceptance and actual accepting owner visible. Deployment
claims require the active lane's production anchor and verification/observation
evidence, not a PR comment or `worker_done`. Use existing delivery tooling only
when it supports the selected platform and lane; do not infer live state from
this document or a historical path.

For new tasks, recoverable local debugging has no universal one/three-attempt cap.
Continue with changed inputs or evidence; reconsider a repeatedly failing premise.
Explicit budgets, production fences, uncertain side effects and one-writer
constraints remain binding. A controlled stop request is distinct from proving
exit before a replacement writer starts. Existing HOLD tasks do not resume.

## 4. Scheduled and manual dispatch

Existing automations keep their enabled state, schedule, role authority and
explicit budgets. Inspect live automation definitions instead of treating a
copied cron, model or concurrency count as current configuration. This adoption
does not enable a disabled dispatch job or repair the production handoff service.

When an authorized dispatcher starts a new item:

1. Reconcile active Git host, Issue/PR identity, claims and real live writers.
2. Choose an independently acceptable outcome. Split only at actual outcome or
   ownership boundaries; broad scope alone does not mandate a Planner or fixed
   sub-issue count. Related files and front/backend changes can stay together.
3. Choose current execution, a native helper or an Orca worker according to §1;
   respect an already assigned coordinator-only role.
4. Record actual dispatch identity, model/effort/pool and worktree in the existing
   state record. Score/playbook are optional; missing advice is not a blocker.
5. Use runtime events and settlement per the installed guide. Persist unresolved
   delivery states so later runs reconcile them; never mark done at CI green.

Batching may share context across separate Issues, but each keeps its required
branch/PR. Concurrency follows actual independent work, exclusive writers,
capacity and explicit budgets, not a universal copied worker count.

## 5. Global policy adoption

Owner approved the execution refactor on 2026-11-15. DailyCheck adopts
`~/.agents/AGENTS.md` and `~/.agents/skills/orca-model-routing/{SKILL.md,routing.json}`
for new-task execution and model selection, subject to the project gates above.
Model selection is separate from task decomposition. Codex remains on its official
subscription; Claude Code CLI may select configured CCSwitch model IDs. Helpers
may choose an adequate supported model without switching shared provider state.
Scoring is advisory and cannot veto authorized work.

The project `orca-model-routing.json` remains deprecated audit material;
`scripts/agent_framework/model_routing.py` still fails closed on it. No fallback
sequence is maintained here. Existing task scopes, limits and stop decisions are
not silently migrated by this adoption.

## 6. Optional methods

`pstack-orca` supplies optional methods for the current executor. It does not
require a playbook, a worker, a separate architect, Arena candidates or a chain of
reviewers. `opening-a-pr` includes project gates and leaves the main executor
responsible for follow-up. Independent Review remains read-only. Worker Task
permissions continue to govern delegation; native helper availability does not
grant recursive Orca dispatch or production authority.

CI, merge, blue-green deployment and Owner decisions are project responsibilities.
Orca settlement only proves the worker lifecycle facts it records.

## 7. Platform and operational references

For this GitHub checkout, `origin` is `https://github.com/DUIDUI-Studio/DailyCheck.git`.
Always recheck the active remote and live configuration before acting. The old
Gitea repository and its credentials are not the default for GitHub work items.
See [`workflow.md`](workflow.md) for platform-specific contracts and
[`../ARCHITECTURE.md`](../ARCHITECTURE.md) for deployment paths and safeguards.
Local automation state remains under `~/.local/state/dailycheck/automation/`;
actual scheduler settings come from Orca. These paths do not grant production access.
