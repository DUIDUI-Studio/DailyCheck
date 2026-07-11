# Auto-Subproject Runbook

This runbook is loaded **before** every subproject execution in `DailyCheck`.
It is the single source of truth for how an agent walks a subproject from PRD
clause to draft PR, with no human-in-the-loop checkpoints inside the run.

Owner review happens **after** the runbook exits: at the end of Phase 1, or
when the runbook fails and reports a blocker.

## Inputs the agent must collect before doing anything

1. The current subproject index N (1–7). The order is **PRD §3.1**:
   `1=forecast → 2=procurement → 3=notifications-event-bus → 4=item-publish
   → 5=recipe-publish+notify → 6=agent-mpc → 7=summary-custom-dates`.
2. The overview PRD: `docs/superpowers/specs/2026-06-29-overview-prd.md`.
3. All **earlier** subproject specs/plans in
   `docs/superpowers/specs/` and `docs/superpowers/plans/`. Their interfaces
   are now contracts — do not break them.
4. The repo's existing conventions (Flask blueprints, `db/warehouses/*.db` +
   `db/master.db`, `permissions.py` roles, `logged_client` fixture,
   `pytest -q`, `ruff check .`).

## Hard rules (cannot be relaxed by the agent)

- **PRD-locked items are immutable.** §8 of the overview PRD enumerates
  them. If a subproject spec/plan appears to require relaxing one, STOP and
  report the conflict in the PR description; do not silently rewrite.
- **TDD is mandatory.** Every new capability follows RED → GREEN → commit
  per task. No "implement first, test later" and no "batch tests at the end."
- **Bug fixes also get a regression test** written first.
- **All three test layers are required** for each subproject (PRD §3.3):
  unit, integration (`Flask test_client` + `logged_client`), E2E (Playwright
  via `.playwright-mcp/`).
- **Coverage target is path-based, not percentage** (PRD §3.4). Cover every
  PRD-locked formula, every "must-automate" scenario, and every error/fallback
  path. UI-decorator Jinja edits are exempt.
- **Stop at draft PR.** Never `gh pr create` without `--draft`. Never merge.
  Never push to `main`. Never `--force` anything. Never skip hooks.
- **E2E honesty.** If Playwright cannot run (missing browser, auth not
  configured, etc.), do not fake the screenshot. Mark the E2E step as
  "manual verification needed" in the PR description and continue.

## Run sequence (per subproject)

### 1. Spec — `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`

Filename date = today. Topic slug = `forecast` | `procurement` |
`notifications` | `item-publish` | `recipe-publish` | `agent-mpc` |
`summary-custom-dates`.

The spec must:

- Open with `> 引用 PRD §X.Y` for every PRD clause it implements.
- Restate the data contract **verbatim** (field names, types, status codes)
  then add implementation notes — never weaken the contract.
- For each "must-automate" scenario (PRD §1.1 A1–A6), name the trigger point
  in code and the operator-visible surface (`/admin/health` or similar).
- Enumerate: irreversible actions, reversible actions + undo surface,
  failure visibility surface.
- Spell out the test matrix mapped to PRD §X.Y test bullets.

### 2. Plan — `docs/superpowers/plans/YYYY-MM-DD-<topic>-plan.md`

The plan must:

- List every task as `TASK N: <verb> <thing> → verify: <check>`.
- Each task corresponds to **one commit** at the end.
- For each task, name the test file(s) to create/modify.
- Mark dependency order. Identify which tasks can run in parallel and which
  cannot (only the agent's own parallel batches, not across subprojects).
- End with a "verification gate" section: exact commands and expected exit
  codes, matching PRD §3.5.

### 3. Implementation loop

For each task in plan order:

1. **RED**: write the failing test(s). Run `pytest -q <test_file>` and
   confirm the new test fails for the right reason. Commit message starts
   with `test(<scope>):`.
2. **GREEN**: write the minimum production code to pass. Run `pytest -q`
   on the touched area, then the full suite, and confirm green. Commit
   message starts with `feat(<scope>):` or `fix(<scope>):`.
3. **REFACTOR** only if the change is a strict simplification of code the
   task itself wrote. Do **not** refactor adjacent code, comments, or
   formatting. Re-run `pytest -q` after. Commit with `refactor(<scope>):`.

A task that requires both test and impl in the same commit (e.g. tiny
bugfix) is acceptable **only** if a regression test is part of the diff.

### 4. Cross-cutting checks (before opening the PR)

- `pytest -q` exits 0.
- `ruff check .` exits 0. If `ruff` is not yet configured, add a minimal
  `pyproject.toml [tool.ruff]` block as part of subproject 1 and stop
  arguing about it later.
- `grep -RIn 'TODO\|FIXME\|XXX' --include='*.py' blueprints/ db/ app.py`
  returns no new TODOs from this subproject.
- For every PRD §1.1 must-automate scenario this subproject owns: the
  scheduler hook, event trigger, or admin-visible status is wired and
  exercised by at least one integration test.

### 5. E2E (Playwright)

- Use the project's `.playwright-mcp/` browser. Re-use existing journeys if
  the new flow extends them; otherwise add a new journey under
  `tests/e2e/journeys/`.
- Capture screenshots into `docs/superpowers/prs/assets/<topic>/`.
- If the environment is missing (no Playwright server reachable, no
  logged-in fixture), record the gap in the PR description under
  "Manual verification needed" and continue. Do not invent a fake
  screenshot.

### 6. PR description — `docs/superpowers/prs/YYYY-MM-DD-<topic>.md`

Sections, in this order:

1. **Summary** — 1–3 sentences, user-visible behavior.
2. **PRD refs** — bullet list of `PRD §X.Y` clauses implemented.
3. **Files changed** — one path per line, generated from `git diff main...HEAD --name-only`.
4. **Test plan** — checkbox list mirroring PRD §X.Y test bullets + the
   three-layer breakdown (unit / integration / E2E).
5. **`pytest -q` output** — last 20 lines of the final run, captured
   verbatim.
6. **`ruff check .` output** — full output.
7. **E2E evidence** — embed screenshots with `![desc](assets/.../foo.png)`
   or, if E2E could not run, an explicit "Manual verification needed"
   block listing what the owner must click through.
8. **Must-automate checklist** — copy of PRD §1.1 rows this subproject
   owns, with the implementation location (file:line) and the operator
   surface.
9. **Open questions / risks** — anything the owner should look at first.

### 7. Open the draft PR

```bash
git push -u origin HEAD
gh pr create --draft --title "<type>(<scope>): <topic>" \
  --body-file docs/superpowers/prs/YYYY-MM-DD-<topic>.md
```

Print the PR URL into the run report. Stop.

### 8. Run report

Print to the user a 5–10 line block:

```
SUBPROJECT N: <topic>
  branch:      <name>
  commits:     <count>
  spec:        <path>
  plan:        <path>
  pr:          <url> (draft)
  pytest:      green
  ruff:        green
  e2e:         green | manual-needed
  next:        subproject N+1 (<topic>) — auto-continuing
```

Then proceed to the next subproject unless this is the end of Phase 1
(subproject 3) — in which case PAUSE and report Phase 1 done.

## Phase boundary

- **Phase 1** = subprojects 1, 2, 3. After subproject 3's PR is open, the
  runbook exits and the agent reports Phase 1 complete. Owner decides
  whether to continue.
- **Phase 2** = subprojects 4, 5, 6. After subproject 6, another pause.
- **Phase 3** = subproject 7. Final pause.

The agent must not auto-continue across a phase boundary, even if the
user has approved auto-continuation within a phase. This is the
non-negotiable "owner review" gate from PRD §3.5.

## Branch and commit hygiene

- One branch per subproject: `feat/<topic>`, `fix/<topic>`, or
  `chore/<topic>` as the work type suggests. Branch from `main`.
- One commit per task from the plan. Squash-merge is the owner's call.
- Conventional commit prefixes only: `feat / fix / refactor / test /
  docs / chore / perf / ci`.
- No commits that mix two subprojects' work.
- Never amend a published commit. If a hook fails, fix the issue and
  make a new commit.

## When the runbook should bail out

- The PRD-locked interface from a prior subproject doesn't match the
  current code (e.g. route path changed). Stop, file the conflict in
  the PR description, exit.
- A test in the existing suite breaks and the cause is outside this
  subproject's scope. Stop, report, exit.
- A required secret / DB / env is missing. Stop, report what's missing,
  exit.
- Two valid implementations exist with material tradeoffs (e.g. sync vs
  async, polling vs push) and the PRD doesn't decide. Stop, ask the
  owner, exit.

In all bail-out cases, the work-in-progress stays on the branch in a
commit. The owner decides whether to revert, fix-forward, or hand back.
