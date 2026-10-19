"""Local usage history: a 24h / 15-minute token histogram and burn-rate forecast.

The data layer only reports a running "today" total, so this module samples it
on every poll and keeps the differences. Window levels are sampled the same way,
which is what the exhaustion forecast is computed from. Nothing here talks to a
provider; it only observes the reports the HUD already has.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

HISTORY_PATH = Path(__file__).resolve().parent / "history.json"

BUCKET_MINUTES = 15
BUCKET_COUNT = 96          # 24 hours
MAX_EVENTS = 6000
KEEP_SECONDS = 48 * 3600
# AlertPolicy.resetRise: a drop larger than this is a reset, not noise.
RESET_RISE = 5.0
# How long a token gain marks an agent as working.
ACTIVE_SECONDS = 240


class History:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else HISTORY_PATH
        self.tokens: dict[str, list[list[float]]] = {}
        self.windows: dict[str, list[list[float]]] = {}
        self.last: dict[str, dict] = {}
        self.load()

    # -- persistence --------------------------------------------------------
    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        for key in ("tokens", "windows"):
            raw = data.get(key)
            if isinstance(raw, dict):
                target = getattr(self, key)
                for pid, rows in raw.items():
                    if isinstance(rows, list):
                        target[pid] = [[float(r[0]), float(r[1])] for r in rows
                                       if isinstance(r, list) and len(r) == 2]
        if isinstance(data.get("last"), dict):
            self.last = data["last"]

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(
                {"tokens": self.tokens, "windows": self.windows, "last": self.last}),
                encoding="utf-8")
        except Exception:
            pass

    def _trim(self, now: float) -> None:
        cutoff = now - KEEP_SECONDS
        for store in (self.tokens, self.windows):
            for key, rows in list(store.items()):
                rows = [r for r in rows if r[0] >= cutoff]
                store[key] = rows[-MAX_EVENTS:]
                if not rows:
                    del store[key]

    # -- observation --------------------------------------------------------
    def observe(self, report, now: float | None = None) -> list[str]:
        """Record one report. Returns the keys of windows that just reset."""
        now = now if now is not None else time.time()
        resets: list[str] = []
        previous = self.last.get(report.pid) or {}

        if report.today_tokens is not None:
            before = previous.get("today")
            if isinstance(before, (int, float)):
                delta = float(report.today_tokens) - float(before)
                if delta < 0:
                    # The day rolled over; everything counted is new.
                    delta = float(report.today_tokens)
                if delta > 0:
                    self.tokens.setdefault(report.pid, []).append([now, delta])
            previous["today"] = report.today_tokens

        for window in report.windows:
            if window.used_percent is None:
                continue
            key = "%s|%s" % (report.pid, window.key)
            rows = self.windows.setdefault(key, [])
            if rows and rows[-1][1] - window.used_percent > RESET_RISE:
                resets.append(window.key)
            rows.append([now, float(window.used_percent)])
            # Keep the level samples short: the forecast only reads the last hours.
            if len(rows) > 400:
                del rows[:-400]

        previous["ts"] = now
        self.last[report.pid] = previous
        self._trim(now)
        return resets

    # -- queries ------------------------------------------------------------
    def token_buckets(self, pid: str, now: float | None = None,
                      count: int = BUCKET_COUNT, minutes: int = BUCKET_MINUTES) -> list[int]:
        """Token totals per bucket, oldest first, covering the last count*minutes."""
        now = now if now is not None else time.time()
        width = minutes * 60
        out = [0] * count
        for stamp, delta in self.tokens.get(pid, []):
            age = now - stamp
            if age < 0:
                continue
            index = int(age // width)
            if 0 <= index < count:
                out[count - 1 - index] += int(delta)
        return out

    def recent_tokens(self, pid: str, seconds: float, now: float | None = None) -> int:
        """Tokens recorded in the last seconds.

        Events are appended in time order, so this walks backwards and stops at
        the first one that is too old. The animation tick asks this question
        many times a second, and scanning the whole history - over a thousand
        events for Codex - was the dock's single largest cost while idle.
        """
        now = now if now is not None else time.time()
        cutoff = now - seconds
        total = 0
        for stamp, delta in reversed(self.tokens.get(pid, [])):
            if stamp < cutoff:
                break
            total += int(delta)
        return total

    def is_active(self, pid: str, now: float | None = None,
                  seconds: float = ACTIVE_SECONDS) -> bool:
        return self.recent_tokens(pid, seconds, now) > 0

    def burn(self, pid: str, window_key: str, now: float | None = None,
             hours: float = 3.0) -> dict | None:
        """Percentage-per-hour over the sampled window plus time to exhaustion."""
        now = now if now is not None else time.time()
        rows = self.windows.get("%s|%s" % (pid, window_key)) or []
        cutoff = now - hours * 3600
        rows = [r for r in rows if r[0] >= cutoff]
        if len(rows) < 3:
            return None
        first, last = rows[0], rows[-1]
        span = (last[0] - first[0]) / 3600.0
        if span < 0.15:
            return None
        rise = last[1] - first[1]
        if rise <= 0:
            return {"pct_per_hour": 0.0, "hours_to_empty": None, "used": last[1]}
        rate = rise / span
        remaining = max(0.0, 100.0 - last[1])
        return {"pct_per_hour": rate,
                "hours_to_empty": (remaining / rate) if rate > 0 else None,
                "used": last[1]}

    def already_seeded(self, pid: str, tag: str) -> bool:
        return (self.last.get(pid) or {}).get("seeded") == tag

    def mark_seeded(self, pid: str, tag: str) -> None:
        entry = self.last.setdefault(pid, {})
        entry["seeded"] = tag

    def seed_events(self, pid: str, events) -> int:
        """Add (timestamp, tokens) pairs from a client's own log, once.

        The histogram needs two samples to see a difference, so a fresh install
        would show an empty chart for the first couple of polls. The clients
        already keep dated token events on disk; reading them back is the same
        data, not invented history.
        """
        events = [(float(ts), int(tokens)) for ts, tokens in events if tokens]
        events.sort()
        self.tokens[pid] = [[ts, tokens] for ts, tokens in events][-MAX_EVENTS:]
        return len(events)

    def save_throttled(self, now: float | None = None, every: float = 60.0) -> None:
        now = now if now is not None else time.time()
        if now - getattr(self, "_saved_at", 0.0) >= every:
            self._saved_at = now
            self.save()
