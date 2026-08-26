"""Server-side LLM cost estimates.

The inference response is authoritative for measured token usage.  This module
only converts those tokens to an estimate using the explicit per-model table;
it never presents the result as an invoice or attempts to scrape a web page.
"""

from configs.model_pricing import MODEL_RATES_USD_PER_MTOK


def estimate_usage_cost(model: str, prompt_tokens: int, completion_tokens: int) -> dict:
    """Return a JSON-ready cost estimate for one inference call.

    Unknown models return an unavailable result.  That fail-closed behaviour
    prevents a model switch from continuing to display a plausible but wrong
    dollar amount.
    """
    model_name = str(model or "").strip()
    rates = MODEL_RATES_USD_PER_MTOK.get(model_name)
    if not rates:
        return {
            "cost_estimate_available": False,
            "estimated_cost_usd": None,
            "cost_breakdown": None,
            "model": model_name,
            "rate_source": None,
            "rates_usd_per_mtok": None,
        }

    in_tokens = max(0, int(prompt_tokens or 0))
    out_tokens = max(0, int(completion_tokens or 0))
    input_usd = in_tokens / 1_000_000 * float(rates["input"])
    output_usd = out_tokens / 1_000_000 * float(rates["output"])
    total_usd = input_usd + output_usd
    return {
        "cost_estimate_available": True,
        "estimated_cost_usd": total_usd,
        "cost_breakdown": {
            "input_usd": input_usd,
            "output_usd": output_usd,
        },
        "model": model_name,
        "rate_source": rates.get("source") or "Configured estimate",
        "rates_usd_per_mtok": {
            "input": float(rates["input"]),
            "output": float(rates["output"]),
        },
    }

