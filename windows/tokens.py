"""Per-agent local token ledger: 24 hours in 15-minute buckets, plus today.

Every client keeps its own dated record of what it spent:

* Codex   - the token_count events in its rollout logs,
* OpenCode- the assistant rows of opencode.db (or storage/message/*.json),
* WorkBuddy - the WorkBuddy/CodeBuddy logs, plus every DeepSeek Harness turn
  that the dsh-workbuddy-connect plugin routed to the workbuddy provider,
* DSH     - the Harness turns routed to DeepSeek's own endpoints.

The quantity counted is "tokens the model touched": input + output + cache
writes + cache reads, with reasoning folded into output where a client reports
it separately. Clients that report nothing still get a reading - an empty
histogram and 0 - so every card keeps the same shape.
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import providers.codex as codex_provider
from winx import appdata, localappdata, userprofile

BUCKET_MINUTES = 15
BUCKET_COUNT = 96
WINDOW_SECONDS = BUCKET_COUNT * BUCKET_MINUTES * 60
CACHE_SECONDS = 90.0

#: Which Harness provider routes belong to which card. A turn is attributed to
#: the route it used, so no two cards count the same request: the WorkBuddy
#: plugin's turns go to the WorkBuddy card, and every other Harness turn - the
#: DeepSeek endpoints and the OpenCode Go pool alike - is Harness's own work.
WORKBUDDY_ROUTES = ("workbuddy", "workbuddy-ai", "workbuddy-global")
# A Harness turn routed to the OpenCode Go pool is OpenCode usage, so it is
# counted on the OpenCode card and kept off the DSH card.
OPENCODE_ROUTES = ("opencode-go",)
DSH_EXCLUDE_ROUTES = WORKBUDDY_ROUTES + OPENCODE_ROUTES


@dataclass
class TokenReading:
    pid: str
    buckets: list[int] = field(default_factory=lambda: [0] * BUCKET_COUNT)
    today: int = 0
    today_cost: float | None = None
    cost_currency: str | None = None
    source: str = ""
    events: int = 0

    @property
    def window_total(self) -> int:
        return sum(self.buckets)

    @property
    def peak(self) -> int:
        return max(self.buckets) if self.buckets else 0


def _zstd_reader():
    try:
        import zstandard
        return zstandard.ZstdDecompressor().stream_reader
    except Exception:
        pass
    try:
        from compression import zstd as _zstd
        return lambda fh: _zstd.ZstdFile(fh)
    except Exception:
        return None


def _start_of_today(now: float) -> float:
    return datetime.datetime.combine(datetime.date.today(),
                                     datetime.time.min).timestamp()


def _blank(pid: str) -> TokenReading:
    return TokenReading(pid=pid)


def _bucketize(events, now: float, currency: str | None = None,
               source: str = "", pid: str = "") -> TokenReading:
    """Fold (timestamp, tokens, cost) triples into the 15-minute buckets."""
    reading = TokenReading(pid=pid, source=source)
    reading.events = len(events)
    midnight = _start_of_today(now)
    cost = 0.0
    for event in events:
        stamp, tokens = event[0], event[1]
        age = now - stamp
        if age < 0:
            continue
        if tokens:
            index = int(age // (BUCKET_MINUTES * 60))
            if 0 <= index < BUCKET_COUNT:
                reading.buckets[BUCKET_COUNT - 1 - index] += int(tokens)
            if stamp >= midnight:
                reading.today += int(tokens)
        if len(event) > 2 and event[2]:
            if stamp >= midnight:
                cost += float(event[2])
    if cost:
        reading.today_cost = cost
        reading.cost_currency = currency
    return reading


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #

def _codex_rollout_parser(path: str, cutoff: float | None = None) -> list:
    import datetime
    out = []
    if cutoff is None:
        cutoff = time.time() - 26 * 3600
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"token_count"' not in line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue
            if record.get("type") != "event_msg":
                continue
            payload = record.get("payload") or {}
            if payload.get("type") != "token_count":
                continue
            stamp = record.get("timestamp")
            info = payload.get("info") or {}
            tokens = (info.get("last_token_usage") or {}).get("total_tokens")
            if not isinstance(tokens, (int, float)) or tokens <= 0 or not stamp:
                continue
            try:
                moment = datetime.datetime.fromisoformat(
                    stamp.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if moment >= cutoff:
                out.append((moment, int(tokens)))
    return out

def _codex_rollout_files(home) -> list[str]:
    files = []
    for root in (home / "sessions", home / "archived_sessions"):
        if root.exists():
            files += glob.glob(os.path.join(str(root), "**", "rollout-*.jsonl"),
                               recursive=True)
    cutoff = time.time() - 26 * 3600
    return [p for p in files if os.path.getmtime(p) >= cutoff]

def _codex(now: float) -> TokenReading:
    home = codex_provider.codex_home()
    events: list[tuple[float, int]] = []
    for path in _codex_rollout_files(home):
        rows = cached_rows(path, _codex_rollout_parser)
        events += rows
    return _bucketize(events, now, None, "Codex rollouts", "codex")


# --------------------------------------------------------------------------- #
# OpenCode
# --------------------------------------------------------------------------- #

def _opencode_roots() -> list[Path]:
    roots = [userprofile() / ".local" / "share" / "opencode",
             appdata() / "opencode", localappdata() / "opencode"]
    seen, out = set(), []
    for root in roots:
        key = str(root).lower()
        if key not in seen and root.exists():
            seen.add(key)
            out.append(root)
    return out


def _opencode_message_tokens(data: dict) -> tuple[int, float | None]:
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return 0, None
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    def number(value):
        return int(value) if isinstance(value, (int, float)) else 0
    total = (number(tokens.get("input")) + number(tokens.get("output"))
             + number(tokens.get("reasoning")) + number(cache.get("write"))
             + number(cache.get("read")))
    if total == 0:
        total = number(tokens.get("total"))
    cost = data.get("cost")
    return total, float(cost) if isinstance(cost, (int, float)) else None


def _opencode(now: float, extra_events=None) -> TokenReading:
    if extra_events is None:
        extra_events = [(ts, tok) for ts, tok in harness_by_route(now)["opencode"]]
    cutoff_ms = int((now - WINDOW_SECONDS - 3600) * 1000)
    events: list[tuple[float, int, float | None]] = []
    source = ""
    for root in _opencode_roots():
        db = root / "opencode.db"
        if db.exists():
            try:
                con = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"),
                                      uri=True, timeout=20)
                con.execute("PRAGMA query_only=1")
                def _parse_db(p):
                    c = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"),
                                        uri=True, timeout=20)
                    c.execute("PRAGMA query_only=1")
                    try:
                        return c.execute(
                            "SELECT time_created, data FROM message "
                            "WHERE time_created > ?", (cutoff_ms,)).fetchall()
                    finally:
                        c.close()
                rows = cached_rows(str(db), _parse_db)
                con.close()
                for created, blob in rows:
                    try:
                        data = json.loads(blob)
                    except Exception:
                        continue
                    if data.get("role") != "assistant":
                        continue
                    tokens, cost = _opencode_message_tokens(data)
                    if tokens:
                        events.append((created / 1000.0, tokens, cost))
                source = "opencode.db"
            except Exception:
                pass
            if events:
                break
    if not events:
        for root in _opencode_roots():
            pattern = os.path.join(str(root), "storage", "message", "**", "*.json")
            for path in glob.glob(pattern, recursive=True):
                try:
                    if os.path.getmtime(path) < now - WINDOW_SECONDS - 3600:
                        continue
                    data = json.loads(Path(path).read_text(encoding="utf-8",
                                                           errors="replace"))
                except Exception:
                    continue
                if not isinstance(data, dict) or data.get("role") != "assistant":
                    continue
                created = (data.get("time") or {}).get("created")
                tokens, cost = _opencode_message_tokens(data)
                if tokens and isinstance(created, (int, float)):
                    events.append((created / 1000.0, tokens, cost))
            if events:
                source = "storage/message"
                break
    if extra_events:
        # Turns Harness routed to the OpenCode Go pool are OpenCode usage too,
        # and they are not in this client's own database.
        events = events + list(extra_events)
        source = (source or "opencode.db") + " + Harness"
    return _bucketize(events, now, "USD" if events else None,
                      source or "opencode.db", "opencode")


# --------------------------------------------------------------------------- #
# DeepSeek Harness sessions (shared with the WorkBuddy route)
# --------------------------------------------------------------------------- #

def _harness_log_files() -> list[str]:
    files = []
    for home in _harness_homes():
        pattern = os.path.join(str(home), "sessions", "**", "session*.jsonl*")
        files += glob.glob(pattern, recursive=True)
    return files

def _harness_homes() -> list[Path]:
    out = []
    env = os.environ.get("DSH_HOME")
    for candidate in ([Path(os.path.expandvars(env))] if env else []) + [
            userprofile() / ".dsh",
            appdata() / "ai.deepseek.harness.desktop" / "dsh"]:
        if candidate.exists() and candidate not in out:
            out.append(candidate)
    return out


def _harness_usage(usage: dict) -> int:
    def number(key):
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) else 0
    return (number("inputTokens") + number("outputTokens")
            + number("cacheWriteTokens") + number("cacheReadTokens"))


def _harness_events(prefixes: tuple[str, ...] = (),
                    exclude: tuple[str, ...] = ()) -> tuple[list, str]:
    """Assistant turns of one route, read out of the Harness session logs."""
    reader_factory = _zstd_reader()
    if reader_factory is None:
        return [], ""
    events, files = [], 0
    for home in _harness_homes():
        pattern = os.path.join(str(home), "sessions", "**", "session*.jsonl*")
        for path in glob.glob(pattern, recursive=True):
            if not path.endswith((".zstd", ".jsonl")):
                continue
            files += 1
            try:
                if path.endswith(".zstd"):
                    with open(path, "rb") as raw:
                        stream = reader_factory(raw)
                        lines = _iter_lines(stream)
                        events += _harness_scan(lines, prefixes, exclude)
                else:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        events += _harness_scan(fh, prefixes, exclude)
            except Exception:
                continue
    return events, ("Harness sessions" if events else "")


def _iter_lines(stream):
    import io
    return io.TextIOWrapper(stream, encoding="utf-8", errors="replace")


def _harness_scan(lines, prefixes: tuple[str, ...] = (),
                  exclude: tuple[str, ...] = ()):
    out = []
    for line in lines:
        if '"assistant/message"' not in line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if record.get("type") != "assistant/message":
            continue
        data = record.get("data") or {}
        provider = (((data.get("message") or {}).get("source") or {})
                    .get("provider") or "")
        tokens = _harness_usage(data.get("usage") or {})
        stamp = record.get("time")
        if tokens and isinstance(stamp, (int, float)):
            # the route travels with the event, so the caller can split by card
            out.append((float(stamp) / 1000.0, tokens, provider))
    return out


_harness_cache: dict = {}

def harness_by_route(now: float | None = None) -> dict[str, list]:
    """Scan the Harness session logs once and split the turns by provider.

    Returns {"workbuddy": [(ts, tokens)], "opencode": [...], "dsh": [...]},
    so the three cards agree and no turn lands on two of them. The result is
    cached until a session log actually changes on disk.
    """
    now = now if now is not None else time.time()
    stamp_key = tuple(sorted(
        (p, os.stat(p).st_mtime, os.stat(p).st_size)
        for p in _harness_log_files()))
    if _harness_cache.get("key") == stamp_key and _harness_cache.get("split"):
        return _harness_cache["split"]
    all_events, source = _harness_events()
    split: dict[str, list] = {"workbuddy": [], "opencode": [], "dsh": []}
    for stamp, tokens, provider in all_events:
        if provider.startswith(WORKBUDDY_ROUTES):
            split["workbuddy"].append((stamp, tokens))
        elif provider.startswith(OPENCODE_ROUTES):
            split["opencode"].append((stamp, tokens))
        else:
            split["dsh"].append((stamp, tokens))
    _harness_cache["key"] = stamp_key
    _harness_cache["split"] = split
    return split

def _workbuddy(now: float) -> TokenReading:
    events = [(ts, tok) for ts, tok in harness_by_route(now)["workbuddy"]]
    extra = _workbuddy_local()
    source = "Harness sessions"
    if extra:
        events += extra
        source += " + local logs"
    return _bucketize(events, now, None, source, "workbuddy")


def _workbuddy_local() -> list:
    """WorkBuddy / CodeBuddy transcript usage, where those clients keep it."""
    out = []
    roots = [userprofile() / ".workbuddy", userprofile() / ".codebuddy",
             appdata() / "WorkBuddy", localappdata() / "CodeBuddyExtension"]
    for root in roots:
        if not root.exists():
            continue
        for pattern in ("sessions/**/*.jsonl", "projects/**/*.jsonl", "**/*.jsonl"):
            for path in glob.glob(os.path.join(str(root), pattern), recursive=True):
                try:
                    if os.path.getsize(path) > 64 * 1024 * 1024:
                        continue
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            if '"usage"' not in line:
                                continue
                            try:
                                record = json.loads(line)
                            except Exception:
                                continue
                            out += _transcript_usage(record)
                except Exception:
                    continue
    return out


def _transcript_usage(record: dict):
    message = record.get("message") if isinstance(record.get("message"), dict) else record
    usage = message.get("usage") or (record.get("providerData") or {}).get("usage")
    if not isinstance(usage, dict):
        return []
    def number(*keys):
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        return 0
    tokens = (number("input_tokens", "inputTokens", "prompt_tokens")
              + number("output_tokens", "outputTokens", "completion_tokens")
              + number("cache_read_input_tokens", "cacheReadTokens")
              + number("cache_creation_input_tokens", "cacheWriteTokens"))
    stamp = record.get("timestamp") or record.get("time")
    if not tokens or not stamp:
        return []
    if isinstance(stamp, str):
        try:
            stamp = datetime.datetime.fromisoformat(
                stamp.replace("Z", "+00:00")).timestamp()
        except Exception:
            return []
    elif isinstance(stamp, (int, float)) and stamp > 1e11:
        stamp = stamp / 1000.0
    return [(float(stamp), tokens)]


def _dsh(now: float) -> TokenReading:
    split = harness_by_route(now)
    events = [(ts, tok) for ts, tok in split["dsh"]]
    reading = _bucketize(events, now, None, "Harness sessions", "dsh")

    # The whale widget keeps the HOST's day: every request the Harness served,
    # whichever product's endpoint it went to. Its total is therefore the only
    # place the day's real figure exists - the session logs only carry the
    # DeepSeek-native routes, which can be empty for a whole day - so the day is
    # taken from the ledger and the turns already counted on the WorkBuddy and
    # OpenCode cards are subtracted from it, leaving DSH its own share.
    cost, currency, whale_tokens = _whale_today()
    if whale_tokens:
        claimed = 0
        for route in ("workbuddy", "opencode"):
            claimed += sum(tok for ts, tok in split[route] if _is_today(ts, now))
        reading.today = max(0, int(whale_tokens) - int(claimed))
        reading.source = "whale ledger - other routes"
    if cost:
        reading.today_cost = cost
        reading.cost_currency = currency
    return reading


def _start_of_month(now: float) -> float:
    """The first instant of the calendar month that now falls in."""
    moment = datetime.datetime.fromtimestamp(now)
    return datetime.datetime(moment.year, moment.month, 1).timestamp()


def _is_today(stamp: float, now: float) -> bool:
    return stamp >= _start_of_today(now)


def _whale_today() -> tuple[float | None, str, int]:
    """(cost, currency, tokens) the whale ledger recorded for today."""
    today = datetime.date.today().isoformat()
    total = 0.0
    tokens = 0
    for home in _harness_homes():
        path = home / ".dshw-usage.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        for event in data.get("events") or []:
            if not isinstance(event, dict) or event.get("day") != today:
                continue
            value = event.get("cost")
            if isinstance(value, (int, float)):
                total += float(value)
            count = event.get("tokens")
            if isinstance(count, (int, float)):
                tokens += int(count)
    return ((total, "CNY", tokens) if total or tokens else (None, "CNY", 0))


def codex_month(now: float | None = None) -> int:
    """Codex tokens for the calendar month, out of its own rollout files.

    The parser and the file filter were both pinned to the last 26 hours, which
    is right for the daily card and useless for a month, so the window is passed
    in instead.
    """
    now = now if now is not None else time.time()
    start = _start_of_month(now)
    total = 0
    try:
        home = codex_provider.codex_home()
    except Exception:
        return 0
    for root in (home / "sessions", home / "archived_sessions"):
        if not root.exists():
            continue
        for file in glob.glob(os.path.join(str(root), "**", "rollout-*.jsonl"),
                              recursive=True):
            try:
                if os.path.getmtime(file) < start:
                    continue
            except OSError:
                continue
            rows = cached_rows(file, lambda p: _codex_rollout_parser(p, start))
            total += sum(int(tokens) for stamp, tokens in rows if stamp >= start)
    return total


def whale_month() -> tuple[float | None, str, int]:
    """(cost, currency, tokens) the whale ledger recorded this month."""
    first = datetime.date.today().replace(day=1).isoformat()
    total = 0.0
    tokens = 0
    for home in _harness_homes():
        path = home / ".dshw-usage.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        for event in data.get("events") or []:
            if not isinstance(event, dict):
                continue
            day = event.get("day")
            if not isinstance(day, str) or day < first:
                continue
            value = event.get("cost")
            if isinstance(value, (int, float)):
                total += float(value)
            count = event.get("tokens")
            if isinstance(count, (int, float)):
                tokens += int(count)
    return ((total, "CNY", tokens) if total or tokens else (None, "CNY", 0))


_month_cache: dict = {"at": 0.0, "data": None}
MONTH_SECONDS = 1800.0


def month_cached() -> dict | None:
    """The month's usage as last computed, or None before the first pass.

    Scanning a month of logs takes a couple of seconds, so it is never done on
    the UI thread: the panel asks for this and shows what it has.
    """
    return _month_cache.get("data")


def refresh_month(now: float | None = None) -> dict | None:
    """Recompute the month's usage. Called off the UI thread, at most hourly-ish."""
    try:
        data = month_totals(now)
    except Exception:
        return _month_cache.get("data")
    _month_cache["data"] = data
    _month_cache["at"] = time.time()
    return data


def month_totals(now: float | None = None) -> dict:
    """Every harness's usage for the calendar month, in tokens and dollars.

    Tokens come from each source's own records - the Harness session logs for
    the three routed agents, the rollout files for Codex - and the money comes
    from the whale ledger, the only one of them that records a cost.
    """
    import money

    now = now if now is not None else time.time()
    start = _start_of_month(now)
    by_agent: dict[str, int] = {}
    try:
        split = harness_by_route(now)
    except Exception:
        split = {}
    for pid, events in (split or {}).items():
        by_agent[pid] = sum(int(tokens) for stamp, tokens in events
                            if stamp >= start)
    by_agent["codex"] = codex_month(now)
    cost, currency, _ledger = whale_month()
    return {
        "since": datetime.datetime.fromtimestamp(start).date().isoformat(),
        "days": datetime.datetime.fromtimestamp(now).day,
        "tokens": sum(by_agent.values()),
        "by_agent": by_agent,
        "cost_usd": money.to_usd(cost, currency) if cost is not None else None,
    }


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

_SOURCES = {"codex": _codex, "opencode": _opencode, "workbuddy": _workbuddy,
            "dsh": _dsh}

_cache: dict = {"at": 0.0, "data": None, "busy": False}
_lock = threading.Lock()
# (path, mtime, size) -> parsed payload, so large log files are read once and
# re-used until they actually change on disk.
_file_cache: dict = {}

def cached_rows(path: str, parser) -> list:
    """Parse a log file once, keyed by (path, mtime, size)."""
    try:
        stat = os.stat(path)
    except OSError:
        return []
    key = (path, stat.st_mtime, stat.st_size)
    hit = _file_cache.get(key[:1] + (stat.st_mtime, stat.st_size))
    if hit is not None:
        return hit
    rows = parser(path)
    _file_cache[key] = rows
    return rows


def collect(now: float | None = None, force: bool = False) -> dict[str, TokenReading]:
    """Every card's token reading. Cached briefly: the scan reads real logs."""
    now = now if now is not None else time.time()
    with _lock:
        fresh = _cache["data"] is not None and not force and \
            now - _cache["at"] < CACHE_SECONDS
        if fresh:
            return _cache["data"]
    readings: dict[str, TokenReading] = {}
    for pid in ("codex", "opencode", "workbuddy", "dsh", "antigravity"):
        source = _SOURCES.get(pid)
        if source is None:
            readings[pid] = _blank(pid)          # no local ledger for this one
            continue
        try:
            readings[pid] = source(now)
        except Exception:
            readings[pid] = _blank(pid)
    with _lock:
        _cache["data"] = readings
        _cache["at"] = time.time()
    return readings


def cached() -> dict[str, TokenReading] | None:
    """The last collected readings, or None before the first scan finishes."""
    return _cache["data"]


class Scanner(threading.Thread):
    """Collects the ledger off the UI thread and hands it over when ready."""

    def __init__(self, interval: float = CACHE_SECONDS, stop: threading.Event | None = None):
        super().__init__(name="token-scanner", daemon=True)
        self.interval = interval
        self.stop = stop or threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            started = time.time()
            try:
                collect()
            except Exception:
                pass
            deadline = started + self.interval
            while not self.stop.is_set() and time.time() < deadline:
                self.stop.wait(0.5)
