"""Account for reported model usage; never substitute tool text for model tokens.

Cost values are API-equivalent scenarios, not ChatGPT subscription charges.
Unknown cache writes and per-request context tiers remain explicit intervals.
"""
import json
from decimal import Decimal


TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_tokens",
                "output_tokens", "reasoning_output_tokens")


def normalize_usage(value):
    if not isinstance(value, dict):
        return None
    usage = {key: value[key] for key in TOKEN_FIELDS if value.get(key) is not None}
    for details, source, target in (
        ("input_tokens_details", "cached_tokens", "cached_input_tokens"),
        ("input_tokens_details", "cache_write_tokens", "cache_write_tokens"),
        ("output_tokens_details", "reasoning_tokens", "reasoning_output_tokens"),
    ):
        nested = value.get(details)
        if isinstance(nested, dict) and nested.get(source) is not None:
            if target in usage and usage[target] != nested[source]:
                return None
            usage[target] = nested[source]
    if any(type(count) is not int or count < 0 for count in usage.values()):
        return None
    if not all(key in usage for key in ("input_tokens", "output_tokens")):
        return None
    if usage.get("cached_input_tokens", 0) + usage.get("cache_write_tokens", 0) > usage["input_tokens"]:
        return None
    if usage.get("reasoning_output_tokens", 0) > usage["output_tokens"]:
        return None
    return usage


def usage_from_events(path):
    """Return complete-run usage only; a failed or truncated run is not free."""
    turns, started, seen_ids = [], 0, set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict):
            return None
        kind = event.get("type")
        if kind in ("turn.failed", "error"):
            return None
        if kind == "turn.started":
            started += 1
        if kind == "turn.completed":
            turn_id = event.get("turn_id")
            if turn_id is not None:
                if not isinstance(turn_id, str) or not turn_id:
                    return None
                if turn_id in seen_ids:
                    return None
                seen_ids.add(turn_id)
            usage = normalize_usage(event.get("usage"))
            if usage is None:
                return None
            turns.append(usage)
    if not turns or (started and started != len(turns)):
        return None
    # Optional fields must be reported for every turn to support a total.
    fields = set.intersection(*(set(turn) for turn in turns))
    return {key: sum(turn[key] for turn in turns) for key in TOKEN_FIELDS if key in fields}


def cost_bounds(usage, price_record, model, service_tier, context_tier=None):
    """Bounds conditional on the named model, service tier, and price record.

    CLI turn totals cannot determine the context tier of individual requests.
    Enumerate both published tiers unless the caller has separate evidence.
    Cache reads and writes are disjoint subsets of input; reasoning is already
    inside output. Neither is an extra token charge on top of its parent.
    """
    usage = normalize_usage(usage)
    if usage is None or "cached_input_tokens" not in usage:
        return None
    if model != price_record["model"] or service_tier != price_record["service_tier"]:
        return None
    rates = price_record["usd_per_million_tokens"]
    tiers = [context_tier] if context_tier is not None else list(rates)
    if any(tier not in rates for tier in tiers):
        raise ValueError("Unknown context tier")
    remaining = usage["input_tokens"] - usage["cached_input_tokens"]
    writes = [usage["cache_write_tokens"]] if "cache_write_tokens" in usage else [0, remaining]
    costs = []
    for tier in tiers:
        rate = {key: Decimal(str(value)) for key, value in rates[tier].items()}
        for written in writes:
            costs.append(((remaining - written) * rate["input"]
                          + usage["cached_input_tokens"] * rate["cached_input"]
                          + written * rate["cache_write"]
                          + usage["output_tokens"] * rate["output"]) / Decimal(1_000_000))
    unknown = []
    if "cache_write_tokens" not in usage:
        unknown.append("cache_write_tokens")
    if context_tier is None:
        unknown.append("per_request_context_tier")
    return {"api_equivalent_usd_min": float(min(costs)),
            "api_equivalent_usd_max": float(max(costs)),
            "total_tokens": usage["input_tokens"] + usage["output_tokens"],
            "unknown_fields": unknown, "price_date": price_record["as_of"],
            "model": model, "service_tier": service_tier,
            "subscription_charge": None}


def savings_bounds(control, candidate):
    if control is None or candidate is None:
        return None
    for field in ("model", "service_tier", "price_date"):
        if control[field] != candidate[field]:
            raise ValueError("Cannot compare different pricing scenarios")
    return {"api_equivalent_usd_saved_min": control["api_equivalent_usd_min"] - candidate["api_equivalent_usd_max"],
            "api_equivalent_usd_saved_max": control["api_equivalent_usd_max"] - candidate["api_equivalent_usd_min"],
            "total_tokens_saved": control["total_tokens"] - candidate["total_tokens"]}
