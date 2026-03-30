"""Tests for service.resolve — generic canonical value resolver."""

import pytest

from service.resolve import (
    CanonicalMatch,
    resolve_canonical_value,
    _normalise,
    DEFAULT_AUTO_ACCEPT,
    DEFAULT_SUGGEST,
)


DEPARTMENTS = ["Marketing", "Engineering", "Sales", "Finance", "Operations"]

OWNERS = [
    "alice@example.com",
    "bob@example.com",
    "carol@example.com",
    "dave@example.com",
]

PAYMENT_METHODS = ["Check", "ACH", "CreditCard", "Wire", "PayPal"]


# ── _normalise ───────────────────────────────────────────────────────────

class TestNormalise:
    def test_lowercase(self):
        assert _normalise("MARKETING") == "marketing"

    def test_strips_punctuation(self):
        assert _normalise("R&D, Inc.") == "rd inc"

    def test_collapses_whitespace(self):
        assert _normalise("  foo   bar  ") == "foo bar"

    def test_empty(self):
        assert _normalise("") == ""


# ── Exact matching ───────────────────────────────────────────────────────

class TestExactMatch:
    def test_exact_case(self):
        result = resolve_canonical_value("Marketing", DEPARTMENTS)
        assert result is not None
        assert result.value == "Marketing"
        assert result.match == "exact"

    def test_case_insensitive(self):
        result = resolve_canonical_value("marketing", DEPARTMENTS)
        assert result is not None
        assert result.value == "Marketing"
        assert result.match == "exact"

    def test_case_insensitive_upper(self):
        result = resolve_canonical_value("ENGINEERING", DEPARTMENTS)
        assert result is not None
        assert result.value == "Engineering"
        assert result.match == "exact"

    def test_normalised_match(self):
        """Punctuation-stripped input matches canonical value."""
        candidates = ["O'Brien & Co", "Smith LLC"]
        result = resolve_canonical_value("obrien co", candidates)
        assert result is not None
        assert result.value == "O'Brien & Co"
        assert result.match == "exact"


# ── Fuzzy / close matching ───────────────────────────────────────────────

class TestFuzzyMatch:
    def test_close_match_auto_accepts(self):
        result = resolve_canonical_value("Marketng", DEPARTMENTS)
        assert result is not None
        assert result.value == "Marketing"
        assert result.match == "close"

    def test_fuzzy_match_needs_confirmation(self):
        result = resolve_canonical_value("Mrktig", DEPARTMENTS)
        assert result is not None
        assert result.value == "Marketing"
        assert result.match in ("fuzzy", "disambiguate")

    def test_typo_in_email(self):
        result = resolve_canonical_value("alice@exmple.com", OWNERS)
        assert result is not None
        assert result.value == "alice@example.com"
        assert result.match in ("close", "fuzzy", "disambiguate")

    def test_payment_method_typo(self):
        result = resolve_canonical_value("CreditCrd", PAYMENT_METHODS)
        assert result is not None
        assert result.value == "CreditCard"
        assert result.match in ("close", "fuzzy")


# ── Not found ────────────────────────────────────────────────────────────

class TestNotFound:
    def test_completely_unrelated(self):
        result = resolve_canonical_value("Xylophone", DEPARTMENTS)
        assert result is None

    def test_empty_input(self):
        result = resolve_canonical_value("", DEPARTMENTS)
        assert result is None

    def test_whitespace_only(self):
        result = resolve_canonical_value("   ", DEPARTMENTS)
        assert result is None

    def test_empty_candidates(self):
        result = resolve_canonical_value("Marketing", [])
        assert result is None


# ── Disambiguation ───────────────────────────────────────────────────────

class TestDisambiguation:
    def test_similar_candidates_flagged(self):
        candidates = ["Marketing", "Marketng"]
        result = resolve_canonical_value("Markting", candidates)
        assert result is not None
        assert result.match == "disambiguate"
        assert len(result.alternatives) >= 1

    def test_distinct_candidates_no_disambiguation(self):
        result = resolve_canonical_value("Marketng", DEPARTMENTS)
        assert result is not None
        assert result.value == "Marketing"
        assert result.alternatives == []


# ── Integration with resolver.resolve_filter ─────────────────────────────

class TestResolverIntegration:
    """Verify _resolve_with_candidates is called by the filter path."""

    def test_enum_exact(self):
        from mcp_server.resolver import resolve_filter
        result = resolve_filter("paymentMethod", "ACH")
        assert result["status"] == "ok"
        assert result["value"] == "ACH"

    def test_enum_case_insensitive(self):
        from mcp_server.resolver import resolve_filter
        result = resolve_filter("paymentMethod", "ach")
        assert result["status"] == "ok"
        assert result["value"] == "ACH"

    def test_enum_fuzzy_close(self):
        from mcp_server.resolver import resolve_filter
        result = resolve_filter("paymentMethod", "CreditCrd")
        assert result["status"] == "ok"
        assert result["value"] == "CreditCard"

    def test_enum_fuzzy_weak(self):
        from mcp_server.resolver import resolve_filter
        result = resolve_filter("paymentMethod", "Crdt")
        assert result["status"] in ("did_you_mean", "invalid_filter")

    def test_enum_boolean_unaffected(self):
        from mcp_server.resolver import resolve_filter
        result = resolve_filter("track1099", True)
        assert result["status"] == "ok"
        assert result["value"] is True

    def test_enum_boolean_string_not_matched(self):
        """Boolean fields shouldn't fuzzy-match against string 'true'."""
        from mcp_server.resolver import resolve_filter
        result = resolve_filter("track1099", "yes")
        assert result["status"] == "invalid_filter"
