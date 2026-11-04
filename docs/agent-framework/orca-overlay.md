# DailyCheck × Orca Overlay

> **Scope:** DailyCheck-specific only.
> Generic Orca mechanics are defined by `~/.agents/ORCA-WORKFLOW-MAP.md` and the
> version-matched `orca skills get orchestration`. This file must not redefine
> Run / Task / Dispatch lifecycle.
>
> **Authority:** `docs/agent-framework/workflow.md` > this file > global defaults.

---

## 1. Roles and authority

| Role | Runs where | Owns | Does not |
|---|---|---|---|
| **Coordinator** | Orca session | task classification, dispatch, settlement, delivery tracking | merge / deploy |
| **Dev Agent** | Orca worker on the dev machine | code, tests, PR | merge |
| **Prod Agent** | VPS `wdg_vps` | independent review, merge decision, blue-green deploy, prod verification, rollback | edit main content directly |
| **Owner (Eric)** | human | requirements, red-line decisions | — |

Only the Prod Agent may merge, deploy or close an Issue. The Coordinator only
tracks and routes.

---

## 2. Project gates

- **Branch:** `{fix|feat|chore|docs|ci|test|refactor|perf}/issue-<N>-<slug>`
  (CI gate H1). Never use an agent or user identity prefix.
- **One issue, one branch, one PR** (CI gate H2). Check open PRs and remote
  `*issue-<N>*` branches before dispatch.
- **PR body:** exactly one each of `## 报告 1/4：需求对齐`, `## Checklist`,
  `## 生产侧事项`; no literal `❌` character in the body.
- **CI required checks:**
  - `CI / test (pull_request)`
  - `CI / agent-framework-reports (pull_request)`
- **Architecture sync:** any commit touching `blueprints/`, `db/`, `mcp_server/`,
  `app.py`, `config.py`, `permissions.py`, `templates/`, `static/`, `tests/`,
  `.github/workflows/`, `deploy/` or `scripts/` must update
  `docs/ARCHITECTURE.md` in the same commit.
- **Red lines:** expanding `tests/known_failures.txt`, editing CI workflows,
  changing branch protection, removing ARCHITECTURE iron rules, or editing
  `workflow.md` requires Owner approval.

---

## 3. Delivery state machine

| State | Criterion | Next owner |
|---|---|---|
| `in_ci` | required checks not terminal | Coordinator |
| `ci_failed` | any required check red | original writer |
| `awaiting_prod` | both required checks success, PR open | Prod Agent |
| `awaiting_deploy` | merged, production anchor not advanced | Prod Agent |
| `deploy_failed` | deploy failed | `code` → writer; `envfix` → Prod Agent |
| `deploy_unverified` | bounded window exhausted, no anchor | Prod Agent |
| `delivered` | merged + Issue closed + CI success + **production anchor** | done |

Deployment evidence is `deployed.sha == merge sha` on the production anchor, not
a PR comment. Use `~/.local/state/dailycheck/automation/bin/dc_delivery.py`.

---

## 4. Nightly Dispatch Drain

| Item | Value |
|---|---|
| Automation | Orca automation `DailyCheck Nightly Dispatch Drain` |
| Schedule | `5 20,21,22,23,0,1,2,3,4,5 * * *` Asia/Shanghai |
| Model | DeepSeek Flash `[1M]` / high |
| Concurrency | max 3 new workers per wave, max 6 active |

Flow:

1. Acquire a per-issue claim under
   `~/.local/state/dailycheck/automation/claims/issue-<N>/`; there is no global
   drain lock. A fresh claim is not dispatchable; a stale claim is reclaimed only
   after confirming the owner worker is not live.
2. First run at/after 20:00 with no `triage.md`: skip the empty-queue NO_ACTION
   and continue to triage.
3. Refresh Gitea: open issues, PRs, branches, claims.
4. Owner requests and decision sync.
5. Planner split consumption.
6. **Holistic triage** writes
   `~/.local/state/dailycheck/automation/<YYYY-MM-DD>/triage.md` with:
   - `BATCH`: 2–3 issues sharing a concrete boundary; each keeps its own branch/PR.
   - `SPLIT`: too broad; planner first, max 5 sub-issues.
   - `SOLO`: independent.
   - `DEFER`: blocked, owner-decision, claimed, red-line, or high-risk.
7. Dispatch in triage order (BATCH → SOLO → SPLIT), not raw `queue-next` order.
8. End the turn; process Orca wakes; release claims only after the worker is
   settled and the item reaches a round-terminal state; summarize.

Dispatch logging: after every `worker-start` — drain or manual coordinator —
append one `dc_state.py log --type dispatch` entry with `role`, `agent`,
`model_requested`, `model_effective`, `effort`, `complexity_score`, `playbook`
and `worktree_path`.

Hard limits: never merge, deploy, close issues, self-authorize owner decisions,
or combine multiple issues into one PR.

---

## 5. Model routing adoption

DailyCheck adopts the user-global policy at
`~/.agents/skills/orca-model-routing/{SKILL.md,routing.json}`.

- Only the legacy **model / CLI / effort / fallback table** is replaced.
- Project permissions, review, CI, merge, deploy and all authority boundaries
  remain unchanged.
- Legacy routing file: `docs/agent-framework/orca-model-routing.json`
  SHA-256: `96cd6df779692e9a51c1b682419d9853de9dd10ec0632c96db36da344a5b97bf`
  It is marked `deprecated: true` and is retained for audit only.
- The project-level resolver `scripts/agent_framework/model_routing.py` fails
  closed on the deprecated file and must not generate launch commands.
- If the legacy file changes, reconcile this adoption before dispatch.
- The global policy does not name this project; this file is the project-side
  record of the adoption.

---

## 6. pstack-orca overlay

Use the global adapter at `~/.agents/skills/pstack-orca/`.

Project-specific additions:

- Task spec must carry the branch naming and PR template gates from §2.
- `opening-a-pr` must include the three required PR sections; the worker opens
  the PR and stops.
- The evidence gate in `report-template.md` applies unchanged.
- `shipping`, `babysit`, `autopilot-*` remain disabled; merge and deploy belong
  to the Prod Agent.
- Every issue still gets its own branch and PR; batching changes sequencing and
  shared context, not PR scope.
- Read-only native subagents are allowed inside a worker. Orca worker creation
  and Dispatch remain forbidden.
- Host skills and CLI tools remain available: code-review agents, `codex review`,
  ruff, semgrep, browser testing. pstack-orca does not disable them.

---

## 7. Project paths

| Path / address | Use |
|---|---|
| `~/Documents/GitHub/DailyCheck/` | main checkout |
| `~/orca/workspaces/DailyCheck/` | Orca worktrees |
| `http://hub01:3000/eric/dailycheck` | Gitea repo |
| `~/.config/gitea/token` or `~/.config/gitea-token` | Dev token |
| `~/.local/state/dailycheck/automation/state/` | queue / owner / dispatch state |
| `~/.local/state/dailycheck/automation/<YYYY-MM-DD>/` | report / outcome / triage |
| `~/.local/state/dailycheck/automation/bin/dc_state.py` | state manager |
| `~/.local/state/dailycheck/automation/bin/dc_delivery.py` | delivery verdict |
| `wdg_vps:/root/DailyCheck` | production worktree |
| `wdg_vps:/var/lib/dailycheck-deploy/` | blue-green state |
| `wdg_vps:/var/lib/dailycheck-prod-agent/inbox.jsonl` | Prod Agent wake queue |
| `wdg_vps:/var/log/dailycheck/` | deploy / webhook logs |
| `wdg_vps:/root/.hermes/logs/` | Hermes / Prod Agent logs |

---

## 8. Coupling map

| Contract | Orca side | DailyCheck side |
|---|---|---|
| Dispatch | `worker-start` / Task / Dispatch / worktree | Dev Agent role, one writer per worktree |
| Settlement | `worker_done`, `worker-show`, `check` | PR, CI, evidence gate |
| Model | `orca-model-routing` | adoption recorded in §5 |
| Automation | Orca automation scheduler + wake | queue / owner / triage state, Gitea |
| Method | `pstack-orca` | project overlay in §6 |

Not Orca: CI, merge, blue-green deploy, delivery state machine, owner approval.
`worker_done` is an Orca settlement, not a project delivery.
