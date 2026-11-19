# /ship — implement, test, commit, push, submit
1. Run the project's test suite. If failures, stop and report.
2. Summarize changed files (one line each).
3. Commit with a conventional-commit message.
4. STOP. Ask: 'Push and open PR?' Do not push without explicit yes.
5. Before opening a draft, use the public requirement Issue and the private Ops source Issue. Create any missing issue first.
6. For GitHub, use `python -m scripts.agent_framework.pr_submission draft` to allocate the PR number. Ask Root to install the source binding for that draft, then use the same module's `ready` command for the exact head. See the GitHub PR submission section in `docs/agent-framework/workflow.md`.
7. After submission, report the PR URL and follow the project's CI and Prod delivery workflow.
