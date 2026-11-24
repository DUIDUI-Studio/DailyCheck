# DailyCheck Agent Entry

Read `~/.agents/AGENTS.md` for the owner-approved global execution policy.
Before changing code, read `AGENT.md`, `CLAUDE.md`, `docs/ARCHITECTURE.md`,
`docs/DEV_ENV.md`, and `docs/agent-framework/workflow.md`.

`workflow.md` is authoritative for issue intake, branch naming, PR gates, merge
responsibility, deployment, and owner red lines. Model selection follows only
`~/.agents/skills/orca-model-routing/{SKILL.md,routing.json}`; the project model
orchestration document has been retired by the Owner.

## Delivery lifecycle terminal state (global)

The current GitHub release rules are in the opening section of
`docs/agent-framework/workflow.md`.

- ORCA develops and independently reviews each change in a separate branch and
  worktree. Run targeted developer checks and send the exact PR to Peter.
- GitHub runs the full test and required report checks for the exact submitted
  commit. Peter (or the accepted local release Worker) owns the manual release
  handoff. The legacy VPS automatic release triggers are disabled.
- Keep following the same work item until both required GitHub checks, the
  merged commit, the linked Issue and the actual settled deployment receipt
  are all verified. A green CI or merge alone is intermediate.
- Follow the cited workflow for preview, cutover, observation, merge and
  rollback. Report the actual stage and evidence of any failure.
- The Coordinator tracks evidence and routes rework. Peter or the approved
  release Worker performs deployment, merge and production verification.
