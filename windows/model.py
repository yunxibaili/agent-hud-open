"""Shared quota model: window, level colours and a provider report."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Design tokens taken from the original macOS project
# (Sources/AgentHUDDesktop/Theme/StatusPalette.swift).
OK = "#3ddc84"
WARNING = "#ffd23f"
CRITICAL = "#ff453a"
IDLE = "#9a9aa0"

# AlertPolicy.swift: warningUsed = 70, criticalUsed = 90.
WARNING_USED = 70.0
CRITICAL_USED = 90.0


def level_of_used(used: float | None, warn: float = WARNING_USED, crit: float = CRITICAL_USED) -> str:
    if used is None:
        return "idle"
    if used >= crit:
        return "critical"
    if used >= warn:
        return "warning"
    return "ok"


def color_of(level: str) -> str:
    return {"ok": OK, "warning": WARNING, "critical": CRITICAL}.get(level, IDLE)


def reset_text(reset_at: float | None, now: float | None = None) -> str:
    """Compact countdown such as "2h13m" / "3d4h" / "12m"."""
    if not reset_at:
        return ""
    now = now if now is not None else time.time()
    delta = reset_at - now
    if delta <= 0:
        return "已重置"
    minutes = int(delta // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h" if hours else f"{days}d"


def period_short(minutes: int | None) -> str:
    if not minutes:
        return "额度"
    known = {300: "5h", 1440: "1d", 10080: "7d", 43200: "30d", 525600: "1y"}
    if minutes in known:
        return known[minutes]
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


@dataclass
class QuotaWindow:
    key: str
    label: str
    short: str
    used_percent: float | None = None
    reset_at: float | None = None
    duration_minutes: int | None = None
    all_models: bool = True
    note: str | None = None
    used_amount: float | None = None
    total_amount: float | None = None
    unlimited: bool = False

    @property
    def remaining_percent(self) -> float | None:
        if self.used_percent is None:
            return None
        return max(0.0, 100.0 - self.used_percent)

    def level(self, warn: float = WARNING_USED, crit: float = CRITICAL_USED) -> str:
        if self.unlimited:
            return "ok"
        return level_of_used(self.used_percent, warn, crit)


@dataclass
class Detail:
    label: str
    value: str


@dataclass
class ProviderReport:
    pid: str
    name: str
    mark: str
    color: str = IDLE
    ok: bool = False
    installed: bool = True
    #: False when the client itself is absent - no engine, no credential, no
    #: process, no home - as opposed to a reading that merely failed this time.
    #: A transient failure keeps its card; an absent client loses it.
    available: bool = True
    windows: list[QuotaWindow] = field(default_factory=list)
    balance: str | None = None
    balance_currency: str | None = None
    account: str | None = None
    plan: str | None = None
    today_tokens: int | None = None
    today_cost: float | None = None
    note: str | None = None
    details: list[Detail] = field(default_factory=list)
    updated_at: float = 0.0

    # ---- presentation helpers -------------------------------------------------

    def headline_window(self) -> QuotaWindow | None:
        """The most-used window that carries a level - what the bar reports."""
        rated = [w for w in self.windows if w.used_percent is not None and not w.unlimited]
        if not rated:
            return None
        return max(rated, key=lambda w: w.used_percent)

    def worst_level(self, warn: float, crit: float) -> str:
        if not self.ok:
            return "idle"
        levels = [w.level(warn, crit) for w in self.windows if w.used_percent is not None]
        if "critical" in levels:
            return "critical"
        if "warning" in levels:
            return "warning"
        if levels:
            return "ok"
        if self.balance_currency:
            return balance_level(self.balance_currency, self.raw_balance)
        return "ok" if self.balance else "idle"

    raw_balance: float | None = None
    quota_note: str | None = None


def balance_level(currency: str, amount: float | None) -> str:
    """AlertPolicy.balanceLevel: depleted at <= 0, low at the currency line."""
    if amount is None:
        return "idle"
    if amount <= 0:
        return "critical"
    line = {"CNY": 10.0, "USD": 2.0}.get((currency or "").upper())
    if line is not None and amount <= line:
        return "warning"
    return "ok"


def human_tokens(value: int | None) -> str:
    if value is None:
        return ""
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if abs(value) >= limit:
            return f"{value / limit:.2f}{suffix}".replace(".00", "")
    return str(value)
