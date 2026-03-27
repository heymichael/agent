"""Deterministic parser for human-friendly period strings.

Converts period expressions into (start_month, end_month) tuples in YYYY-MM
format. The LLM normalises natural language ("last quarter", "first half of
2026") into one of the accepted formats; this module handles the rest
deterministically.

Accepted formats:
    "2026-02"        -> ("2026-02", "2026-02")   exact month
    "2026-Q1"        -> ("2026-01", "2026-03")   quarter
    "2026-H1"        -> ("2026-01", "2026-06")   half-year
    "2026"           -> ("2026-01", "2026-12")   full year
    "YTD"            -> ("2026-01", <current>)   year to date
    "last-3-months"  -> computed from today       rolling window
    None             -> (None, None)              all time
"""

from __future__ import annotations

import re
from datetime import date


class PeriodParseError(Exception):
    """Raised when a period string doesn't match any accepted format."""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        super().__init__(
            f"Unrecognised period format: '{raw}'. "
            "Accepted formats: YYYY-MM, YYYY-QN, YYYY-HN, YYYY, YTD, last-N-months."
        )


_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$", re.IGNORECASE)
_HALF_RE = re.compile(r"^(\d{4})-H([12])$", re.IGNORECASE)
_YEAR_RE = re.compile(r"^(\d{4})$")
_LAST_N_RE = re.compile(r"^last-(\d+)-months$", re.IGNORECASE)


def _month_offset(ref: date, months_back: int) -> str:
    """Return YYYY-MM for *months_back* months before *ref*."""
    total = ref.year * 12 + (ref.month - 1) - months_back
    y, m = divmod(total, 12)
    return f"{y}-{m + 1:02d}"


def parse_period(period: str | None) -> tuple[str | None, str | None]:
    """Convert a period string into ``(start_month, end_month)`` inclusive.

    Returns ``(None, None)`` when *period* is ``None`` (meaning "all time").
    Raises :class:`PeriodParseError` for unrecognised input.
    """
    if period is None:
        return (None, None)

    period = period.strip()

    m = _MONTH_RE.match(period)
    if m:
        return (period, period)

    m = _QUARTER_RE.match(period)
    if m:
        year, q = m.group(1), int(m.group(2))
        start = f"{year}-{(q - 1) * 3 + 1:02d}"
        end = f"{year}-{q * 3:02d}"
        return (start, end)

    m = _HALF_RE.match(period)
    if m:
        year, h = m.group(1), int(m.group(2))
        start = f"{year}-{(h - 1) * 6 + 1:02d}"
        end = f"{year}-{h * 6:02d}"
        return (start, end)

    m = _YEAR_RE.match(period)
    if m:
        return (f"{period}-01", f"{period}-12")

    if period.upper() == "YTD":
        today = date.today()
        return (f"{today.year}-01", today.strftime("%Y-%m"))

    m = _LAST_N_RE.match(period)
    if m:
        n = int(m.group(1))
        if n < 1:
            raise PeriodParseError(period)
        today = date.today()
        start = _month_offset(today, n - 1)
        end = today.strftime("%Y-%m")
        return (start, end)

    raise PeriodParseError(period)
