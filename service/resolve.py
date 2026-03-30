"""Generic canonical value resolver.

Pure-Python fuzzy matching for small sets of canonical strings (departments,
owners, enum values). No database dependency — callers load candidates and
pass them in.

Uses ``difflib.SequenceMatcher`` which is well-suited for candidate sets
under a few hundred items.  For large-scale fuzzy search (thousands of
vendors), use ``pg_trgm`` via ``pg_client.find_vendors_similar`` instead.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher


DEFAULT_AUTO_ACCEPT = 0.85
DEFAULT_SUGGEST = 0.6
DEFAULT_SIMILAR = 0.75


class CanonicalMatch:
    """Result of resolve_canonical_value."""
    __slots__ = ("value", "match", "alternatives")

    def __init__(self, value: str, match: str,
                 alternatives: list[str] | None = None):
        self.value = value
        self.match = match  # "exact" | "close" | "fuzzy" | "disambiguate"
        self.alternatives = alternatives or []


def _normalise(text: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace."""
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def resolve_canonical_value(
    user_input: str,
    candidates: list[str],
    *,
    auto_accept: float = DEFAULT_AUTO_ACCEPT,
    suggest: float = DEFAULT_SUGGEST,
    similar: float = DEFAULT_SIMILAR,
) -> CanonicalMatch | None:
    """Resolve *user_input* against a list of canonical *candidates*.

    Resolution cascade:
      1. Exact match
      2. Case-insensitive match
      3. Normalised match (strip punctuation / whitespace)
      4. SequenceMatcher ratio — two-tier:
         * close (>= *auto_accept*): accept automatically
         * fuzzy (>= *suggest*): caller should confirm with the user
      5. Disambiguation: if a second candidate scores within *similar* of
         the best, flag as ambiguous.

    Returns ``None`` when no candidate scores above the *suggest* threshold.
    """
    user_input = user_input.strip()
    if not user_input or not candidates:
        return None

    # Step 1: exact match
    for c in candidates:
        if c == user_input:
            return CanonicalMatch(c, "exact")

    # Step 2: case-insensitive
    input_lower = user_input.lower()
    for c in candidates:
        if c.lower() == input_lower:
            return CanonicalMatch(c, "exact")

    # Step 3: normalised match
    input_norm = _normalise(user_input)
    if input_norm:
        for c in candidates:
            if _normalise(c) == input_norm:
                return CanonicalMatch(c, "exact")

    # Step 4: fuzzy via SequenceMatcher
    scored = [(c, _ratio(user_input, c)) for c in candidates]
    scored.sort(key=lambda t: t[1], reverse=True)

    if not scored or scored[0][1] < suggest:
        return None

    best_candidate, best_score = scored[0]
    match_type = "close" if best_score >= auto_accept else "fuzzy"

    # Step 5: disambiguation — check for near-ties
    alternatives = [
        c for c, s in scored[1:]
        if s >= similar and s >= suggest
    ]
    if alternatives:
        match_type = "disambiguate"

    return CanonicalMatch(best_candidate, match_type, alternatives)
