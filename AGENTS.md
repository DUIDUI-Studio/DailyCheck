# DailyCheck Agent Entry

At task entry, read `~/.agents/AGENTS.md`, `AGENT.md` and the role, authority and delivery rules in
`docs/agent-framework/workflow.md`. Before changing a subsystem, read the
architecture overview and relevant sections of `docs/ARCHITECTURE.md`; consult
`CLAUDE.md` and `docs/DEV_ENV.md` for the affected stack and environment.
Choose a new agent/model only when needed, using the user-global
`~/.agents/skills/orca-model-routing/{SKILL.md,routing.json}`. These references
do not create separate work stages or require another agent.

Default to one main executor owning investigation, design, related implementation,
repair, validation and delivery follow-up. Use helpers only for a useful independent
result. A dedicated Coordinator is conditional; managing a helper does not remove
the main executor's write ownership. Preserve one writer and independent read-only
review, plus explicit Owner limits and production authority. Do not resume HOLD tasks.

`workflow.md` is authoritative for issue intake, branch naming, PR gates, merge
responsibility, deployment, and owner red lines. Explicit Owner decisions take
precedence and must be recorded for red-line changes. The Owner retired the project
model-orchestration document; the project routing JSON remains deprecated.

## Delivery lifecycle terminal state (global)

This contract applies to every DailyCheck worktree, manual dispatch, and
scheduled automation.

- CI green is an intermediate state, not delivery completion. CI green
  automatically hands the PR to the Prod Agent for independent review,
  candidate deployment, merge, and production verification.
- The main executor or explicitly assigned Coordinator must keep following the
  same work item until a terminal state is proven from the active Git host,
  CI and deployment evidence. An open PR is not an implicit transfer of ownership.
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
