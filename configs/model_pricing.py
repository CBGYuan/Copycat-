"""Configurable estimate rates for models exposed through Intel GNAI.

These are NOT invoice data.  GNAI may apply an enterprise contract, internal
chargeback, caching discounts, or other adjustments that a normal inference
API key cannot see.  Keep only explicitly approved rates here; an unknown
model intentionally produces ``cost_estimate_available=False`` instead of
silently borrowing another model's price.

Units are USD per one million tokens.
"""

MODEL_RATES_USD_PER_MTOK = {
    # Existing Copycat estimate, moved from static/js/log_viewer.js. Confirm
    # these values with the GNAI owner if the UI must reflect Intel chargeback
    # rather than a public-list-price estimate.
    "claude-4-6-sonnet": {
        "input": 3.0,
        "output": 15.0,
        "source": "Configured GNAI estimate",
    },
}

