"""Unit tests for mcp_server.period_parser.

All functions are pure — no database or network access required.
"""

from datetime import date
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.expense_analytics

from mcp_server.period_parser import parse_period, PeriodParseError, _month_offset


# ── _month_offset ────────────────────────────────────────────────────────

class TestMonthOffset:
    """Month arithmetic must handle year boundaries and zero offsets correctly."""

    def test_same_month(self):
        """Zero offset must return the same year-month as the reference date."""
        assert _month_offset(date(2026, 3, 15), 0) == "2026-03"

    def test_one_month_back(self):
        """An offset of 1 must return the immediately preceding month."""
        assert _month_offset(date(2026, 3, 15), 1) == "2026-02"

    def test_cross_year_boundary(self):
        """Subtracting past January must wrap into the previous year's December."""
        assert _month_offset(date(2026, 1, 10), 1) == "2025-12"

    def test_several_months_back(self):
        """Multi-month offsets must land on the correct month within the same year."""
        assert _month_offset(date(2026, 6, 1), 5) == "2026-01"

    def test_full_year_back(self):
        """A 12-month offset must land on the same month of the previous year."""
        assert _month_offset(date(2026, 3, 1), 12) == "2025-03"


# ── parse_period: None ───────────────────────────────────────────────────

class TestParseNone:
    """A None period must propagate as (None, None), not raise."""

    def test_none_returns_none_tuple(self):
        """None input must yield a (None, None) tuple, leaving date filtering disabled."""
        assert parse_period(None) == (None, None)


# ── parse_period: exact month ────────────────────────────────────────────

class TestParseMonth:
    """YYYY-MM inputs must parse to a single-month range with no off-by-one errors."""

    def test_normal_month(self):
        """A mid-year month must produce an identical start and end month."""
        assert parse_period("2026-03") == ("2026-03", "2026-03")

    def test_january(self):
        """January (year-start boundary) must not underflow."""
        assert parse_period("2026-01") == ("2026-01", "2026-01")

    def test_december(self):
        """December (year-end boundary) must not overflow."""
        assert parse_period("2026-12") == ("2026-12", "2026-12")

    def test_with_whitespace(self):
        """Leading/trailing whitespace must be stripped before parsing."""
        assert parse_period("  2026-05  ") == ("2026-05", "2026-05")


# ── parse_period: quarter ────────────────────────────────────────────────

class TestParseQuarter:
    """Quarter tokens must map to the correct three-month range boundaries."""

    def test_q1(self):
        """Q1 must span January through March."""
        assert parse_period("2026-Q1") == ("2026-01", "2026-03")

    def test_q2(self):
        """Q2 must span April through June."""
        assert parse_period("2026-Q2") == ("2026-04", "2026-06")

    def test_q3(self):
        """Q3 must span July through September."""
        assert parse_period("2026-Q3") == ("2026-07", "2026-09")

    def test_q4(self):
        """Q4 must span October through December."""
        assert parse_period("2026-Q4") == ("2026-10", "2026-12")

    def test_lowercase_q(self):
        """Quarter parsing must be case-insensitive."""
        assert parse_period("2025-q3") == ("2025-07", "2025-09")


# ── parse_period: half-year ──────────────────────────────────────────────

class TestParseHalf:
    """Half-year tokens must map to the correct six-month range boundaries."""

    def test_h1(self):
        """H1 must span January through June."""
        assert parse_period("2026-H1") == ("2026-01", "2026-06")

    def test_h2(self):
        """H2 must span July through December."""
        assert parse_period("2026-H2") == ("2026-07", "2026-12")

    def test_lowercase_h(self):
        """Half-year parsing must be case-insensitive."""
        assert parse_period("2026-h1") == ("2026-01", "2026-06")


# ── parse_period: full year ──────────────────────────────────────────────

class TestParseYear:
    """A bare four-digit year must expand to the full January–December range."""

    def test_year(self):
        """The current year must span January through December."""
        assert parse_period("2026") == ("2026-01", "2026-12")

    def test_past_year(self):
        """A past year must span January through December of that year."""
        assert parse_period("2025") == ("2025-01", "2025-12")


# ── parse_period: YTD ────────────────────────────────────────────────────

class TestParseYTD:
    """YTD must always start at January of the current year and end at the current month."""

    @patch("mcp_server.period_parser.date")
    def test_ytd_march(self, mock_date):
        """YTD in March must span January through March."""
        mock_date.today.return_value = date(2026, 3, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        assert parse_period("YTD") == ("2026-01", "2026-03")

    @patch("mcp_server.period_parser.date")
    def test_ytd_january(self, mock_date):
        """YTD in January must collapse to a single-month range."""
        mock_date.today.return_value = date(2026, 1, 5)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        assert parse_period("YTD") == ("2026-01", "2026-01")

    def test_ytd_case_insensitive(self):
        """Lowercase 'ytd' must be accepted."""
        result = parse_period("ytd")
        assert result[0] is not None

    def test_ytd_mixed_case(self):
        """Mixed-case 'Ytd' must be accepted."""
        result = parse_period("Ytd")
        assert result[0] is not None


# ── parse_period: last-N-months ──────────────────────────────────────────

class TestParseLastN:
    """last-N-months must produce an N-month window ending at the current month."""

    @patch("mcp_server.period_parser.date")
    def test_last_3_months(self, mock_date):
        """last-3-months from March must span January through March."""
        mock_date.today.return_value = date(2026, 3, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        assert parse_period("last-3-months") == ("2026-01", "2026-03")

    @patch("mcp_server.period_parser.date")
    def test_last_1_month(self, mock_date):
        """last-1-months must collapse to the current month only."""
        mock_date.today.return_value = date(2026, 3, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        assert parse_period("last-1-months") == ("2026-03", "2026-03")

    @patch("mcp_server.period_parser.date")
    def test_last_12_months_cross_year(self, mock_date):
        """A 12-month window must correctly cross the year boundary."""
        mock_date.today.return_value = date(2026, 3, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        assert parse_period("last-12-months") == ("2025-04", "2026-03")

    def test_case_insensitive(self):
        """last-N-months parsing must be case-insensitive."""
        result = parse_period("Last-6-Months")
        assert result[0] is not None


# ── parse_period: errors ─────────────────────────────────────────────────

class TestParseErrors:
    """Malformed or out-of-range period strings must raise PeriodParseError, never silently succeed."""

    def test_garbage_input(self):
        """Free-text that doesn't match any period pattern must be rejected."""
        with pytest.raises(PeriodParseError):
            parse_period("next tuesday")

    def test_invalid_month_13(self):
        """Month 13 is out of range and must be rejected."""
        with pytest.raises(PeriodParseError):
            parse_period("2026-13")

    def test_invalid_month_00(self):
        """Month 00 is out of range and must be rejected."""
        with pytest.raises(PeriodParseError):
            parse_period("2026-00")

    def test_invalid_quarter_5(self):
        """Quarter 5 does not exist and must be rejected."""
        with pytest.raises(PeriodParseError):
            parse_period("2026-Q5")

    def test_invalid_half_3(self):
        """Half 3 does not exist and must be rejected."""
        with pytest.raises(PeriodParseError):
            parse_period("2026-H3")

    def test_last_0_months(self):
        """A zero-month window is nonsensical and must be rejected."""
        with pytest.raises(PeriodParseError):
            parse_period("last-0-months")

    def test_empty_string(self):
        """An empty string must raise, not be treated as None."""
        with pytest.raises(PeriodParseError):
            parse_period("")
