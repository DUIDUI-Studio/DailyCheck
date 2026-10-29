# DailyCheck Agent Entry

Before changing code, read `AGENT.md`, `CLAUDE.md`, `docs/ARCHITECTURE.md`,
`docs/DEV_ENV.md`, `docs/agent-framework/workflow.md`, and
`docs/agent-framework/orca-model-orchestration.md`.

`workflow.md` is authoritative for issue intake, branch naming, PR gates, merge
responsibility, deployment, and owner red lines. The Orca routing document and
JSON define role/model/effort selection and infrastructure fallback behavior.

## Delivery lifecycle terminal state (global)

This contract applies to every DailyCheck worktree, manual dispatch, and
scheduled automation.

- CI green is an intermediate state, not delivery completion. CI green
  automatically hands the PR to the Prod Agent for independent review,
  candidate deployment, merge, and production verification.
- The Coordinator or initiating agent must keep following the same work item
  until a terminal state is proven from live Gitea/CI/deployment evidence.
- A successful delivery is terminal only when the PR is merged, the linked
  Issue is closed, both required CI checks are successful, and the Prod Agent
  has written a successful deployment result after its verification and
  observation window.
- While the PR is open, follow merge conflicts, required checks, review
  blockers, and whether the Prod Agent has started. After merge, follow the
  deployment report; do not report "CI green", "merged", or "PR closed" as the
  final project outcome when deployment is still pending.
- If deployment fails, route by ownership: in-scope code failures return to the
  original Issue writer; environment or infrastructure failures go to the Prod
  Agent without changing PR code; review blockers return to the writer and then
  a fresh read-only Reviewer.
- Scheduled tasks must use persistent state and reconcile on later runs. They
  must not mark an item complete at CI green or merge, and must distinguish
  `awaiting_prod`, `awaiting_deploy`, `deploy_failed`, and `delivered` in their
  reports.
- The Coordinator must not merge, deploy, or close the Issue. Those remain Prod
  Agent actions; the Coordinator owns tracking, evidence, and rework routing.
