"""Unit tests for mcp_server.period_parser.

All functions are pure — no database or network access required.
"""

from datetime import date
from unittest.mock import patch

import pytest

from mcp_server.period_parser import parse_period, PeriodParseError, _month_offset


# ── _month_offset ────────────────────────────────────────────────────────

class TestMonthOffset:
    def test_same_month(self):
        assert _month_offset(date(2026, 3, 15), 0) == "2026-03"

    def test_one_month_back(self):
        assert _month_offset(date(2026, 3, 15), 1) == "2026-02"

    def test_cross_year_boundary(self):
        assert _month_offset(date(2026, 1, 10), 1) == "2025-12"

    def test_several_months_back(self):
        assert _month_offset(date(2026, 6, 1), 5) == "2026-01"

    def test_full_year_back(self):
        assert _month_offset(date(2026, 3, 1), 12) == "2025-03"


# ── parse_period: None ───────────────────────────────────────────────────

class TestParseNone:
    def test_none_returns_none_tuple(self):
        assert parse_period(None) == (None, None)


# ── parse_period: exact month ────────────────────────────────────────────

class TestParseMonth:
    def test_normal_month(self):
        assert parse_period("2026-03") == ("2026-03", "2026-03")

    def test_january(self):
        assert parse_period("2026-01") == ("2026-01", "2026-01")

    def test_december(self):
        assert parse_period("2026-12") == ("2026-12", "2026-12")

    def test_with_whitespace(self):
        assert parse_period("  2026-05  ") == ("2026-05", "2026-05")


# ── parse_period: quarter ────────────────────────────────────────────────

class TestParseQuarter:
    def test_q1(self):
        assert parse_period("2026-Q1") == ("2026-01", "2026-03")

    def test_q2(self):
        assert parse_period("2026-Q2") == ("2026-04", "2026-06")

    def test_q3(self):
        assert parse_period("2026-Q3") == ("2026-07", "2026-09")

    def test_q4(self):
        assert parse_period("2026-Q4") == ("2026-10", "2026-12")

    def test_lowercase_q(self):
        assert parse_period("2025-q3") == ("2025-07", "2025-09")


# ── parse_period: half-year ──────────────────────────────────────────────

class TestParseHalf:
    def test_h1(self):
        assert parse_period("2026-H1") == ("2026-01", "2026-06")

    def test_h2(self):
        assert parse_period("2026-H2") == ("2026-07", "2026-12")

    def test_lowercase_h(self):
        assert parse_period("2026-h1") == ("2026-01", "2026-06")


# ── parse_period: full year ──────────────────────────────────────────────

class TestParseYear:
    def test_year(self):
        assert parse_period("2026") == ("2026-01", "2026-12")

    def test_past_year(self):
        assert parse_period("2025") == ("2025-01", "2025-12")


# ── parse_period: YTD ────────────────────────────────────────────────────

class TestParseYTD:
    @patch("mcp_server.period_parser.date")
    def test_ytd_march(self, mock_date):
        mock_date.today.return_value = date(2026, 3, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        assert parse_period("YTD") == ("2026-01", "2026-03")

    @patch("mcp_server.period_parser.date")
    def test_ytd_january(self, mock_date):
        mock_date.today.return_value = date(2026, 1, 5)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        assert parse_period("YTD") == ("2026-01", "2026-01")

    def test_ytd_case_insensitive(self):
        result = parse_period("ytd")
        assert result[0] is not None

    def test_ytd_mixed_case(self):
        result = parse_period("Ytd")
        assert result[0] is not None


# ── parse_period: last-N-months ──────────────────────────────────────────

class TestParseLastN:
    @patch("mcp_server.period_parser.date")
    def test_last_3_months(self, mock_date):
        mock_date.today.return_value = date(2026, 3, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        assert parse_period("last-3-months") == ("2026-01", "2026-03")

    @patch("mcp_server.period_parser.date")
    def test_last_1_month(self, mock_date):
        mock_date.today.return_value = date(2026, 3, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        assert parse_period("last-1-months") == ("2026-03", "2026-03")

    @patch("mcp_server.period_parser.date")
    def test_last_12_months_cross_year(self, mock_date):
        mock_date.today.return_value = date(2026, 3, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
        assert parse_period("last-12-months") == ("2025-04", "2026-03")

    def test_case_insensitive(self):
        result = parse_period("Last-6-Months")
        assert result[0] is not None


# ── parse_period: errors ─────────────────────────────────────────────────

class TestParseErrors:
    def test_garbage_input(self):
        with pytest.raises(PeriodParseError):
            parse_period("next tuesday")

    def test_invalid_month_13(self):
        with pytest.raises(PeriodParseError):
            parse_period("2026-13")

    def test_invalid_month_00(self):
        with pytest.raises(PeriodParseError):
            parse_period("2026-00")

    def test_invalid_quarter_5(self):
        with pytest.raises(PeriodParseError):
            parse_period("2026-Q5")

    def test_invalid_half_3(self):
        with pytest.raises(PeriodParseError):
            parse_period("2026-H3")

    def test_last_0_months(self):
        with pytest.raises(PeriodParseError):
            parse_period("last-0-months")

    def test_empty_string(self):
        with pytest.raises(PeriodParseError):
            parse_period("")
