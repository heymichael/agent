"""Tests for service.resolve — generic canonical value resolver."""

import pytest

pytestmark = [pytest.mark.expense_analytics, pytest.mark.vendor_management]

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
    """Normalisation must be idempotent, lowercase, and strip noise characters."""

    def test_lowercase(self):
        """Upper-case input must be folded to lowercase."""
        assert _normalise("MARKETING") == "marketing"

    def test_strips_punctuation(self):
        """Punctuation and special characters must be removed during normalisation."""
        assert _normalise("R&D, Inc.") == "rd inc"

    def test_collapses_whitespace(self):
        """Runs of whitespace must collapse to a single space with no leading/trailing padding."""
        assert _normalise("  foo   bar  ") == "foo bar"

    def test_empty(self):
        """Empty strings must normalise to empty, not raise."""
        assert _normalise("") == ""


# ── Exact matching ───────────────────────────────────────────────────────

class TestExactMatch:
    """Exact matches (including case-folded) must resolve without fuzzy scoring."""

    def test_exact_case(self):
        """A value matching a candidate exactly must resolve as 'exact'."""
        result = resolve_canonical_value("Marketing", DEPARTMENTS)
        assert result is not None
        assert result.value == "Marketing"
        assert result.match == "exact"

    def test_case_insensitive(self):
        """Case differences alone must still count as an exact match."""
        result = resolve_canonical_value("marketing", DEPARTMENTS)
        assert result is not None
        assert result.value == "Marketing"
        assert result.match == "exact"

    def test_case_insensitive_upper(self):
        """All-caps input must resolve to the correctly-cased canonical value."""
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
    """Minor typos must resolve to the nearest canonical value, not fail outright."""

    def test_close_match_auto_accepts(self):
        """A single-character typo must auto-accept without user confirmation."""
        result = resolve_canonical_value("Marketng", DEPARTMENTS)
        assert result is not None
        assert result.value == "Marketing"
        assert result.match == "close"

    def test_fuzzy_match_needs_confirmation(self):
        """Heavily abbreviated input must flag for confirmation rather than auto-accepting."""
        result = resolve_canonical_value("Mrktig", DEPARTMENTS)
        assert result is not None
        assert result.value == "Marketing"
        assert result.match in ("fuzzy", "disambiguate")

    def test_typo_in_email(self):
        """Typos in email addresses must still resolve to the correct owner."""
        result = resolve_canonical_value("alice@exmple.com", OWNERS)
        assert result is not None
        assert result.value == "alice@example.com"
        assert result.match in ("close", "fuzzy", "disambiguate")

    def test_payment_method_typo(self):
        """Payment method typos must fuzzy-resolve to the correct canonical value."""
        result = resolve_canonical_value("CreditCrd", PAYMENT_METHODS)
        assert result is not None
        assert result.value == "CreditCard"
        assert result.match in ("close", "fuzzy")


# ── Not found ────────────────────────────────────────────────────────────

class TestNotFound:
    """Unresolvable inputs must return None, never a false-positive match."""

    def test_completely_unrelated(self):
        """A value with no plausible candidate must return None."""
        result = resolve_canonical_value("Xylophone", DEPARTMENTS)
        assert result is None

    def test_empty_input(self):
        """Empty string input must return None, not match a candidate."""
        result = resolve_canonical_value("", DEPARTMENTS)
        assert result is None

    def test_whitespace_only(self):
        """Whitespace-only input must be treated as empty and return None."""
        result = resolve_canonical_value("   ", DEPARTMENTS)
        assert result is None

    def test_empty_candidates(self):
        """An empty candidate list must return None regardless of input."""
        result = resolve_canonical_value("Marketing", [])
        assert result is None


# ── Disambiguation ───────────────────────────────────────────────────────

class TestDisambiguation:
    """Ambiguous inputs equidistant from multiple candidates must surface alternatives."""

    def test_similar_candidates_flagged(self):
        """When two candidates score similarly, the result must include alternatives for the caller."""
        candidates = ["Marketing", "Marketng"]
        result = resolve_canonical_value("Markting", candidates)
        assert result is not None
        assert result.match == "disambiguate"
        assert len(result.alternatives) >= 1

    def test_distinct_candidates_no_disambiguation(self):
        """A clear best match among well-separated candidates must not carry alternatives."""
        result = resolve_canonical_value("Marketng", DEPARTMENTS)
        assert result is not None
        assert result.value == "Marketing"
        assert result.alternatives == []


# ── Integration with resolver.resolve_filter ─────────────────────────────

class TestResolverIntegration:
    """Verify _resolve_with_candidates is called by the filter path."""

    def test_enum_exact(self):
        """Exact enum values must pass through the filter resolver unchanged."""
        from mcp_server.resolver import resolve_filter
        result = resolve_filter("paymentMethod", "ACH")
        assert result["status"] == "ok"
        assert result["value"] == "ACH"

    def test_enum_case_insensitive(self):
        """Enum resolution must be case-insensitive end-to-end."""
        from mcp_server.resolver import resolve_filter
        result = resolve_filter("paymentMethod", "ach")
        assert result["status"] == "ok"
        assert result["value"] == "ACH"

    def test_enum_fuzzy_close(self):
        """Close typos on enum values must auto-resolve through the filter path."""
        from mcp_server.resolver import resolve_filter
        result = resolve_filter("paymentMethod", "CreditCrd")
        assert result["status"] == "ok"
        assert result["value"] == "CreditCard"

    def test_enum_fuzzy_weak(self):
        """Weak fuzzy matches on enums must not silently auto-accept."""
        from mcp_server.resolver import resolve_filter
        result = resolve_filter("paymentMethod", "Crdt")
        assert result["status"] in ("did_you_mean", "invalid_filter")

    def test_enum_boolean_unaffected(self):
        """Boolean filter values must bypass string resolution entirely."""
        from mcp_server.resolver import resolve_filter
        result = resolve_filter("track1099", True)
        assert result["status"] == "ok"
        assert result["value"] is True

    def test_enum_boolean_string_not_matched(self):
        """Boolean fields shouldn't fuzzy-match against string 'true'."""
        from mcp_server.resolver import resolve_filter
        result = resolve_filter("track1099", "yes")
        assert result["status"] == "invalid_filter"
