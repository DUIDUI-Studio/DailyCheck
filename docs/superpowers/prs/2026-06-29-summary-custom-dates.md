# Summary custom date range — subproject 7

## Summary
Adds `?start=YYYY-MM-DD&end=YYYY-MM-DD` to `/summary` and
`/summary/export`, with PRD §2.6.3 validation and 6 quick-range
buttons (本周/上周/本月/上月/本季/本年) that drive the same
client-side navigation. Legacy `?range=7d|month|all` is preserved
but silently ignored (spec §0.1).

## PRD refs
- §2.6.1 goal
- §2.6.2 URL contract
- §2.6.3 validation rules (locked flash messages)
- §2.6.4 aggregation scope (unchanged — only the date filter changes)
- §2.6.5 export compat (legacy `summary-<date>-<range>.csv` kept
  when no start/end, new `summary_<start>_to_<end>.csv` when given)
- §2.6.6 test matrix (unit / integration)

## Files changed (this subproject)
```
blueprints/core.py                  (+start/end in summary + helpers)
blueprints/reports.py               (+start/end in export_summary)
blueprints/summary_dates.py         (new — pure parse fn)
static/summary_dates.js             (new — 6-button JS)
templates/summary.html              (button row + script tag)
tests/test_summary_dates_pure.py    (new — 10 unit cases)
tests/test_summary_dates_route.py   (new — 5 route cases)
tests/test_summary_export_dates.py  (new — 3 export cases)
tests/test_summary_quick_buttons.py (new — 1 button render case)
```

`git diff main...HEAD --name-only` includes files from earlier
subprojects too because the branch is based on `preview-phase1`.
The list above is the diff scoped to this subproject's work.

## Test plan
- [x] unit: `parse_summary_dates` 8 spec §5.1 cases + 1 boundary
- [x] integration: `/summary` 5 cases (200 with both / 200 start only
      / 400 start>end / 400 invalid / range=7d legacy)
- [x] integration: `/summary/export` 3 cases (200 with date range /
      range=7d legacy / CSV body filters out-of-window rows)
- [x] integration: 6 quick buttons render on `/summary`
- [ ] E2E: Playwright click "本月" — **manual verification needed**
      (browser not configured in this env)

## `pytest -q` output
```
189 passed in 3.84s
```

## `ruff check .` output
```
Found 128 errors. (vs 138 pre-existing — net −10 from this work)
[*] 114 fixable with the `--fix` option (4 hidden fixes can be enabled with the `--unsafe-fixes` option).
```
Pre-existing 138 → 128 after this branch. All new files in this
subproject are ruff-clean (verified with a targeted `ruff check` on
the 5 added files: 0 errors). The remaining 128 are the pre-existing
findings explicitly out of scope per task brief.

## E2E evidence
**Manual verification needed.** The new Playwright step is:
1. log in as admin
2. navigate to `/summary`
3. click `本月` → expect URL to become `/summary?start=YYYY-MM-01&end=YYYY-MM-DD`
4. confirm the page renders the new "范围" label and the in-month
   consumption totals

This environment has no Playwright server reachable.

## Must-automate checklist
None of PRD §1.1 A1–A6 are owned by this subproject — summary
custom-dates is a read-side UI feature, not a must-automate workflow.

## Open questions / risks
1. **"end = today + 1" semantics**: spec §0.1 (self-decision 7) locks
   this. The reasoning is "future-dated outbound can be entered
   tonight and dated tomorrow." Owner should confirm this matches
   the actual user flow.
2. **Summary label swap**: when start/end are explicit, the
   `range_label` flips to "YYYY-MM-DD ~ YYYY-MM-DD" instead of
   "7 日滚动". This was not in the spec but the legacy "7 日滚动"
   label would lie when a custom range is active. Easy to revert.
3. **CSV filename token conflict**: old consumers that parse
   `summary-<date>-<range>.csv` will still see that shape when no
   start/end is given. New consumers should look for
   `summary_<start>_to_<end>.csv` only when both params are present.
4. **Pre-existing 138 vs brief's "81"**: my baseline measured 138
   ruff findings; the brief said 81. The net change (−10) is what
   matters and is reported above.
