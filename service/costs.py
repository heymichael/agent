"""Token cost lookup and calculation for OpenAI models.

Prices are per 1 million tokens (USD), sourced from
https://openai.com/api/pricing/ as of 2026-04-10.
"""

from __future__ import annotations

TOKEN_COSTS: dict[str, dict[str, float]] = {
    # Flagship
    "gpt-4o":           {"input": 2.50,  "cached_input": 1.25,  "output": 10.00},
    "gpt-4o-mini":      {"input": 0.15,  "cached_input": 0.075, "output": 0.60},
    "gpt-5.4":          {"input": 2.50,  "cached_input": 0.25,  "output": 15.00},
    "gpt-5.4-mini":     {"input": 0.75,  "cached_input": 0.075, "output": 4.50},
    "gpt-5.4-nano":     {"input": 0.20,  "cached_input": 0.02,  "output": 1.25},
    "gpt-5.4-pro":      {"input": 30.00, "cached_input": 30.00, "output": 180.00},
    # Reasoning
    "o3":               {"input": 2.00,  "cached_input": 0.50,  "output": 8.00},
    "o3-mini":          {"input": 1.10,  "cached_input": 0.55,  "output": 4.40},
    "o4-mini":          {"input": 1.10,  "cached_input": 0.275, "output": 4.40},
    "o1":               {"input": 15.00, "cached_input": 7.50,  "output": 60.00},
    "o1-mini":          {"input": 1.10,  "cached_input": 0.55,  "output": 4.40},
}

_PER_M = 1_000_000


def calculate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
) -> float | None:
    """Return estimated USD cost, or None if the model isn't in TOKEN_COSTS.

    ``cached_tokens`` is the subset of ``prompt_tokens`` that were served
    from the OpenAI prompt cache (reported as ``usage.prompt_tokens_details.cached_tokens``).
    """
    rates = TOKEN_COSTS.get(model)
    if rates is None:
        return None

    uncached_input = prompt_tokens - cached_tokens
    cost = (
        (uncached_input * rates["input"] / _PER_M)
        + (cached_tokens * rates["cached_input"] / _PER_M)
        + (completion_tokens * rates["output"] / _PER_M)
    )
    return round(cost, 6)
