"""Currency display: every cost is shown in US dollars.

Providers report costs in whatever currency their own ledger uses - the Harness
whale ledger writes CNY, OpenCode's database writes USD - so one exchange rate
folds them into a single figure the totals card can add up and the dock can
print in a slot. Nothing here touches the data layer: the original amount and
its currency stay on the report, and only the rendering passes through this.
"""

from __future__ import annotations

# CNY per 1 USD. Overridable from config ("currency": {"usd_cny": 7.14}).
DEFAULT_USD_CNY = 7.14

_rate = DEFAULT_USD_CNY


def set_rate(usd_cny) -> float:
    """Set how many CNY one USD buys; a bad value keeps the previous rate."""
    global _rate
    try:
        value = float(usd_cny)
    except (TypeError, ValueError):
        return _rate
    if value > 0.1:
        _rate = value
    return _rate


def rate() -> float:
    return _rate


def to_usd(amount, currency) -> float | None:
    """Convert an amount to USD, or None when the currency is not known.

    An unrecognised currency is deliberately not guessed at: showing a yen
    figure as dollars would be worse than showing nothing.
    """
    if amount is None:
        return None
    code = (currency or "USD").upper()
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    if code == "USD":
        return value
    if code == "CNY":
        return value / _rate
    if code == "EUR":
        return value * 1.08
    return None


def usd(amount, currency="USD", digits: int = 2) -> str:
    """The one figure every cost line shows: "≈$11.51"."""
    value = to_usd(amount, currency)
    if value is None:
        return "--"
    return "≈$%.*f" % (digits, value)


def compact(amount, currency, digits: int = 1) -> str:
    """A slot-sized figure: "$20", "$19.9", "$1.2k"."""
    value = to_usd(amount, currency)
    if value is None:
        return "--"
    if abs(value) >= 1000:
        return "$%.1fk" % (value / 1000.0)
    if abs(value) >= 100:
        return "$%.0f" % value
    return "$%.*f" % (digits, value)
