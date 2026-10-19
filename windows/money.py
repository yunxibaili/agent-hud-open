"""Currency display: every cost is shown in US dollars.

Providers report costs in whatever currency their own ledger uses - the Harness
whale ledger writes CNY, OpenCode's database writes USD - so one exchange rate
folds them into a single figure the totals card can add up and the dock can
print in a slot. Nothing here touches the data layer: the original amount and
its currency stay on the report, and only the rendering passes through this.
"""

from __future__ import annotations

import datetime
import math

# CNY per 1 USD. Overridable from config ("currency": {"usd_cny": 7.14}).
DEFAULT_USD_CNY = 7.14

_rate = DEFAULT_USD_CNY

# Verified 2026-10-19: https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
_DEEPSEEK_CNY = {"deepseek-flash": (1.0, 0.02, 4.0),
                 "deepseek-v4-flash": (1.0, 0.02, 4.0),
                 "deepseek-v4-flash-vision-exp": (1.0, 0.02, 4.0),
                 "deepseek-v4-pro": (4.5, 0.15, 13.5)}
# https://www.beijing.gov.cn/cs/gncs/zcwj/202603/t20260327_4568275.html
# State Council's 2026 holiday schedule; weekend make-up days remain off-peak.
_HOLIDAYS_2026 = (("01-01", "01-03"), ("02-15", "02-23"), ("04-04", "04-06"),
                  ("05-01", "05-05"), ("06-19", "06-21"), ("09-25", "09-27"),
                  ("10-01", "10-07"))


def deepseek_cost(name: str, parts: dict, requested_at: float) -> float | None:
    """Current CNY list-price estimate for one official API request, not a bill."""
    rates = _DEEPSEEK_CNY.get(name.lower())
    if rates is None or any(not isinstance(parts.get(key), int) or parts[key] < 0
                            for key in ("input", "output", "cache_read", "cache_write")):
        return None
    if not math.isfinite(requested_at):
        return None
    try:
        moment = datetime.datetime.fromtimestamp(
            requested_at, datetime.timezone(datetime.timedelta(hours=8)))
    except (OverflowError, OSError, ValueError):
        return None
    # shortcut: holiday dates are verified for 2026 only; refresh before pricing another year.
    if moment.year != 2026:
        return None
    day = moment.strftime("%m-%d")
    holiday = any(start <= day <= end for start, end in _HOLIDAYS_2026)
    peak = moment.weekday() < 5 and not holiday and (9 <= moment.hour < 12 or 14 <= moment.hour < 18)
    fresh, cached, output = rates
    return ((parts["input"] + parts["cache_write"]) * fresh
            + parts["cache_read"] * cached + parts["output"] * output) * (2 if peak else 1) / 1_000_000


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
