"""Pure parser for /summary custom date range query params.

Spec §0.1 (self-decision): `range=` is silently ignored. When start/end
are absent we fall back to the legacy default (past 7 days ending
today). The function returns (start_date, end_date, error_message);
callers convert the error message into a flash + 400.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Optional


# --- flash messages locked by PRD §2.6.3 -----------------------------
ERR_START_AFTER_END = "开始日期不能晚于结束日期"
ERR_SPAN_OVER_1Y = "时间范围不能超过 1 年"
ERR_INVALID_FORMAT = "日期格式应为 YYYY-MM-DD"
ERR_FUTURE_END = "结束日期不能晚于今天 + 1 天"

# --- bound locks ----------------------------------------------------
_MAX_DAYS = 365
_FUTURE_END_TOLERANCE_DAYS = 1  # end may be at most today + 1

_DATE_FMT = "%Y-%m-%d"


def _today() -> _dt.date:
    """Injectable clock — tests monkeypatch this to freeze 'today'."""
    return _dt.date.today()


def _parse_date(raw: Any) -> Optional[_dt.date]:
    """Parse a YYYY-MM-DD string into date. Returns None on missing/empty/invalid.

    Strict format: 10 chars, exactly 4 dashes, calendar-valid. A bare
    regex match is not enough — we round-trip through strptime so
    e.g. '2026-02-30' is rejected.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
    else:
        s = str(raw).strip()
    if not s:
        return None
    try:
        return _dt.datetime.strptime(s, _DATE_FMT).date()
    except ValueError:
        return None


def parse_summary_dates(args: Any) -> tuple[Optional[_dt.date], Optional[_dt.date], Optional[str]]:
    """Return (start, end, error) for a summary view.

    `args` must be dict-like with `start`, `end`, `range` keys
    (Flask request.args compatible). `range` is read but ignored
    per spec §0.1.

    Rules (PRD §2.6.3):
      - missing start → today - 7 days
      - missing end   → today
      - start > end   → ERR_START_AFTER_END
      - (end - start).days > 365 → ERR_SPAN_OVER_1Y
      - end > today + 1 day → ERR_FUTURE_END
      - malformed start or end → ERR_INVALID_FORMAT
    """
    today = _today()
    default_start = today - _dt.timedelta(days=7)

    def _get(name: str) -> Optional[str]:
        """Dict-like access that works for both Flask request.args
        and plain objects (test convenience)."""
        if hasattr(args, "get"):
            v = args.get(name)
            if v is not None:
                return v
        return getattr(args, name, None)

    start_raw = _get("start")
    end_raw = _get("end")
    start = _parse_date(start_raw)
    end = _parse_date(end_raw)

    # `_parse_date` collapses missing AND malformed to None. Distinguish
    # the two by re-checking the raw string. A present-but-bad value
    # trips the format check below.
    start_provided = bool(start_raw) and str(start_raw).strip() != ""
    end_provided = bool(end_raw) and str(end_raw).strip() != ""

    if start_provided and start is None:
        return None, None, ERR_INVALID_FORMAT
    if end_provided and end is None:
        return None, None, ERR_INVALID_FORMAT

    if start is None:
        start = default_start
    if end is None:
        end = today

    if start > end:
        return None, None, ERR_START_AFTER_END

    if (end - start).days > _MAX_DAYS:
        return None, None, ERR_SPAN_OVER_1Y

    if end > today + _dt.timedelta(days=_FUTURE_END_TOLERANCE_DAYS):
        return None, None, ERR_FUTURE_END

    # `range=` param is intentionally ignored (spec §0.1). Read it so
    # the unused-arg linter doesn't complain; the caller documents this
    # in the view.
    _ = _get("range")

    return start, end, None
