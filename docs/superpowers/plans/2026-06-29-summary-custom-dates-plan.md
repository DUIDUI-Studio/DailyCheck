# Summary custom dates — subproject 7 plan

Spec: `2026-06-29-summary-custom-dates-design.md`. PRD §2.6.

## Tasks

### T1 — pure fn `parse_summary_dates` (RED → GREEN)
- 8 cases: defaults / start only / end only / both / start>end / span>365 / future end / invalid format / range=7d ignored.
- Impl: `blueprints/summary_dates.py`.

### T2 — `/summary` view accepts start/end (RED → GREEN)
- 5 cases: 200 with both / 200 with start only / 400 start>end / 400 invalid format / range=7d → defaults.
- Impl: modify `blueprints/reports.py::summary`.

### T3 — `/summary/export` accepts start/end (RED → GREEN)
- 2 cases: 200 CSV with date-range filter / range=7d ignored.
- Impl: modify `blueprints/reports.py::summary_export`.

### T4 — quick buttons on summary.html
- 6 buttons: 本周/上周/本月/上月/本季/本年.
- JS: `static/summary_dates.js` computes date range, navigates.
- Test: 1 simple "page contains all 6 buttons".

### T5 — verify + draft PR
- `pytest -q` green, push `feat/summary-custom-dates`, draft PR.

## Branch

`feat/summary-custom-dates` based on `preview-phase1` (independent).
