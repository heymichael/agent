"""Tests for the unified vendor resolver in service.pg_client.

Tests for _normalise (pure function) run directly.
Tests for resolve_vendor_by_identifier mock the pool to avoid Postgres.
"""

from unittest.mock import patch, MagicMock
from contextlib import contextmanager

import pytest

pytestmark = [pytest.mark.expense_analytics, pytest.mark.vendor_management]

from service.pg_client import (
    _normalise,
    _is_uuid,
    VendorMatch,
    resolve_vendor_by_identifier,
    FUZZY_AUTO_ACCEPT,
    FUZZY_SUGGEST,
    SIMILAR_THRESHOLD,
)


# ── Mock helpers ─────────────────────────────────────────────────────────

class _MockCursor:
    def __init__(self, rows=None, row=None):
        self._rows = rows or []
        self._row = row

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


class _MockConn:
    def __init__(self, query_map=None):
        self._query_map = query_map or {}

    def execute(self, sql, params=None):
        for pattern, result in self._query_map.items():
            if pattern in sql:
                if callable(result):
                    return result(sql, params)
                if isinstance(result, list):
                    return _MockCursor(rows=result)
                return _MockCursor(row=result)
        return _MockCursor()


class _MockPool:
    def __init__(self, query_map=None):
        self._conn = _MockConn(query_map)

    @contextmanager
    def connection(self):
        yield self._conn


# ── _normalise ───────────────────────────────────────────────────────────

class TestNormalise:
    """Vendor name normalisation must produce stable, deterministic keys for matching."""

    def test_lowercase(self):
        """Mixed-case vendor names must normalise to lowercase for case-insensitive matching."""
        assert _normalise("ACME Corp") == "acme corp"

    def test_strips_punctuation(self):
        """Punctuation must be stripped so 'Acme, Inc.' matches 'Acme Inc'."""
        assert _normalise("Acme, Inc.") == "acme inc"

    def test_collapses_whitespace(self):
        """Extra whitespace must collapse to single spaces for consistent keys."""
        assert _normalise("  foo   bar  ") == "foo bar"

    def test_empty_string(self):
        """An empty string must normalise to empty, not raise or return whitespace."""
        assert _normalise("") == ""

    def test_special_chars(self):
        """Apostrophes, ampersands, and suffixes must be stripped for fuzzy-safe keys."""
        assert _normalise("O'Brien & Associates, LLC") == "obrien associates llc"


# ── _is_uuid ─────────────────────────────────────────────────────────────

class TestIsUuid:
    """UUID detection must correctly gate the UUID-lookup code path."""

    def test_valid_uuid(self):
        """A well-formed UUID-4 string must be recognised as a UUID."""
        assert _is_uuid("550e8400-e29b-41d4-a716-446655440000") is True

    def test_invalid_uuid(self):
        """A plain name string must never be treated as a UUID."""
        assert _is_uuid("Maya Glenn") is False

    def test_empty(self):
        """An empty string must not be treated as a valid UUID."""
        assert _is_uuid("") is False


# ── resolve_vendor_by_identifier ─────────────────────────────────────────

_VENDOR_ACME = {
    "id": "550e8400-e29b-41d4-a716-446655440001",
    "name": "Acme Corp",
    "aliases": None,
    "source_system": "manual",
    "source_system_id": "acme-corp",
}

_VENDOR_AWS = {
    "id": "550e8400-e29b-41d4-a716-446655440002",
    "name": "Amazon Web Services",
    "aliases": ["AWS", "Amazon"],
    "source_system": "manual",
    "source_system_id": "amazon-web-services",
}

_VENDOR_OBRIEN = {
    "id": "550e8400-e29b-41d4-a716-446655440003",
    "name": "O'Brien & Associates, LLC",
    "aliases": None,
    "source_system": "manual",
    "source_system_id": "obrien-associates-llc",
}

_VENDOR_INTEREXY = {
    "id": "550e8400-e29b-41d4-a716-446655440004",
    "name": "Interexy",
    "aliases": None,
    "source_system": "manual",
    "source_system_id": "interexy",
}

_VENDOR_INTEREXY_LLC = {
    "id": "550e8400-e29b-41d4-a716-446655440005",
    "name": "Interexy LLC",
    "aliases": None,
    "source_system": "manual",
    "source_system_id": "interexy-llc",
}

_ALL_FULL_VENDORS = [_VENDOR_ACME, _VENDOR_AWS, _VENDOR_OBRIEN, _VENDOR_INTEREXY, _VENDOR_INTEREXY_LLC]

_ALL_VENDORS_LIGHT = [
    {"id": v["id"], "name": v["name"], "aliases": v.get("aliases")}
    for v in _ALL_FULL_VENDORS
]

_FULL_BY_ID = {v["id"]: v for v in _ALL_FULL_VENDORS}


def _make_pool(*, name_match=None, uuid_match=None, similar_results=None, all_vendors=None):
    """Build a mock pool for resolver tests.

    name_match: vendor dict returned by find_vendor_by_name
    uuid_match: vendor dict returned by get_vendor(uuid)
    similar_results: list of (vendor_dict, score) for find_vendors_similar
    all_vendors: list of vendor dicts for _load_all_vendors_light
    """
    all_v = all_vendors if all_vendors is not None else _ALL_VENDORS_LIGHT

    def _handle(sql, params):
        if "similarity" in sql:
            rows = []
            if similar_results:
                for vendor, score in similar_results:
                    row = dict(vendor)
                    row["sim_score"] = score
                    rows.append(row)
            return _MockCursor(rows=rows)
        if "LOWER(v.name) = LOWER" in sql:
            if name_match:
                return _MockCursor(row=dict(name_match))
            return _MockCursor(row=None)
        if "WHERE v.id" in sql:
            vid = params[0] if params else None
            if uuid_match and vid == uuid_match["id"]:
                return _MockCursor(row=dict(uuid_match))
            full = _FULL_BY_ID.get(vid)
            if full:
                return _MockCursor(row=dict(full))
            return _MockCursor(row=None)
        if "id::text, name, aliases" in sql:
            return _MockCursor(rows=[dict(v) for v in all_v])
        return _MockCursor()

    return _MockPool({"": _handle})


class TestResolveVendorByIdentifier:
    """The vendor resolver must return the correct match type across exact, alias, fuzzy, and disambiguation paths."""

    @patch("service.pg_client.get_pool")
    def test_empty_identifier(self, mock_pool):
        """An empty identifier must return None without querying the database."""
        assert resolve_vendor_by_identifier("") is None

    @patch("service.pg_client.get_pool")
    def test_whitespace_only(self, mock_pool):
        """Whitespace-only input must be treated as empty and return None."""
        assert resolve_vendor_by_identifier("   ") is None

    @patch("service.pg_client.get_pool")
    def test_exact_uuid(self, mock_pool):
        """A valid UUID must resolve via direct ID lookup and return an exact match."""
        mock_pool.return_value = _make_pool(uuid_match=_VENDOR_ACME)
        result = resolve_vendor_by_identifier(_VENDOR_ACME["id"])
        assert result is not None
        assert result.vendor["id"] == _VENDOR_ACME["id"]
        assert result.match == "exact"

    @patch("service.pg_client.get_pool")
    def test_exact_name(self, mock_pool):
        """An exact vendor name must resolve without falling through to fuzzy matching."""
        mock_pool.return_value = _make_pool(name_match=_VENDOR_ACME)
        result = resolve_vendor_by_identifier("Acme Corp")
        assert result is not None
        assert result.vendor["id"] == _VENDOR_ACME["id"]
        assert result.match in ("exact", "disambiguate")

    @patch("service.pg_client.get_pool")
    def test_alias_match(self, mock_pool):
        """A known alias must resolve to its parent vendor as an exact match."""
        mock_pool.return_value = _make_pool(
            all_vendors=_ALL_VENDORS_LIGHT,
        )
        result = resolve_vendor_by_identifier("AWS")
        assert result is not None
        assert result.vendor["id"] == _VENDOR_AWS["id"]
        assert result.match == "exact"

    @patch("service.pg_client.get_pool")
    def test_normalised_match(self, mock_pool):
        """A normalised form of a vendor name must match even without original punctuation."""
        mock_pool.return_value = _make_pool(
            all_vendors=_ALL_VENDORS_LIGHT,
        )
        result = resolve_vendor_by_identifier("obrien associates llc")
        assert result is not None
        assert result.vendor["id"] == _VENDOR_OBRIEN["id"]
        assert result.match in ("exact", "disambiguate")

    @patch("service.pg_client.get_pool")
    def test_fuzzy_close_match(self, mock_pool):
        """A near-miss typo must resolve via fuzzy matching with 'close' confidence."""
        mock_pool.return_value = _make_pool(
            similar_results=[(_VENDOR_ACME, 0.65)],
            all_vendors=_ALL_VENDORS_LIGHT,
        )
        result = resolve_vendor_by_identifier("Acmee Corp")
        assert result is not None
        assert result.vendor["id"] == _VENDOR_ACME["id"]
        assert result.match in ("close", "disambiguate")

    @patch("service.pg_client.get_pool")
    def test_fuzzy_weak_match(self, mock_pool):
        """A weak similarity must still surface a candidate rather than returning None."""
        mock_pool.return_value = _make_pool(
            similar_results=[(_VENDOR_ACME, 0.30)],
            all_vendors=_ALL_VENDORS_LIGHT,
        )
        result = resolve_vendor_by_identifier("Acm Crp")
        assert result is not None
        assert result.vendor["id"] == _VENDOR_ACME["id"]
        assert result.match in ("fuzzy", "disambiguate")

    @patch("service.pg_client.get_pool")
    def test_not_found(self, mock_pool):
        """A completely unknown identifier must return None, not a false-positive match."""
        mock_pool.return_value = _make_pool(
            similar_results=[],
            all_vendors=_ALL_VENDORS_LIGHT,
        )
        result = resolve_vendor_by_identifier("Completely Unknown Vendor XYZ")
        assert result is None

    @patch("service.pg_client.get_pool")
    def test_exact_name_skips_disambiguation(self, mock_pool):
        """Exact name match should return 'exact' even when similar vendors exist."""
        mock_pool.return_value = _make_pool(
            name_match=_VENDOR_INTEREXY,
            similar_results=[
                (_VENDOR_INTEREXY, 0.9),
                (_VENDOR_INTEREXY_LLC, 0.75),
            ],
            all_vendors=_ALL_VENDORS_LIGHT,
        )
        result = resolve_vendor_by_identifier("Interexy")
        assert result is not None
        assert result.vendor["id"] == _VENDOR_INTEREXY["id"]
        assert result.match == "exact"
        assert not result.alternatives

    @patch("service.pg_client.get_pool")
    def test_fuzzy_disambiguation_with_similar_names(self, mock_pool):
        """Non-exact match should still trigger disambiguation when similar vendors exist."""
        mock_pool.return_value = _make_pool(
            similar_results=[
                (_VENDOR_INTEREXY, 0.65),
                (_VENDOR_INTEREXY_LLC, 0.55),
            ],
            all_vendors=_ALL_VENDORS_LIGHT,
        )
        result = resolve_vendor_by_identifier("Interexi")
        assert result is not None
        assert result.vendor["id"] == _VENDOR_INTEREXY["id"]
        assert result.match == "disambiguate"
        assert len(result.alternatives) >= 1
        alt_ids = [a["id"] for a in result.alternatives]
        assert _VENDOR_INTEREXY_LLC["id"] in alt_ids

    @patch("service.pg_client.get_pool")
    def test_non_uuid_string_does_not_crash(self, mock_pool):
        """Regression: "Maya Glenn" must not be passed to a UUID query."""
        mock_pool.return_value = _make_pool(
            similar_results=[],
            all_vendors=[],
        )
        result = resolve_vendor_by_identifier("Maya Glenn")
        assert result is None
