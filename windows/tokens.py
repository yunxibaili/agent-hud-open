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
import math
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
RANGES = {"5h": (5 * 3600, 20), "24h": (24 * 3600, 96),
          "7d": (7 * 86400, 168), "30d": (30 * 86400, 120)}
TOKEN_KINDS = {"input": "输入（非缓存）", "output": "输出", "cache_read": "缓存读取", "cache_write": "缓存写入"}

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
    ranges: dict = field(default_factory=dict)
    notice: str = ""

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
    return datetime.datetime.combine(datetime.datetime.fromtimestamp(now).date(),
                                     datetime.time.min).timestamp()


def _blank(pid: str) -> TokenReading:
    return TokenReading(pid=pid)


def _bucketize(events, now: float, currency: str | None = None,
               source: str = "", pid: str = "") -> TokenReading:
    """Fold (timestamp, tokens, cost) triples into the 15-minute buckets."""
    reading = TokenReading(pid=pid, source=source)
    reading.events = len(events)
    reading.ranges = {key: {"tokens": 0, "cost_usd": None, "priced_tokens": 0, "models": {},
                            "buckets": [0] * count}
                      for key, (_seconds, count) in RANGES.items()}
    midnight = _start_of_today(now)
    cost = 0.0
    cost_known = False
    for event in events:
        stamp, tokens = event[0], event[1]
        if not math.isfinite(stamp) or not math.isfinite(tokens) or tokens < 0:
            continue
        age = now - stamp
        if age < 0:
            continue
        event_cost = event[2] if len(event) > 2 else None
        if (not isinstance(event_cost, (int, float)) or not math.isfinite(event_cost)
                or event_cost < 0):
            event_cost = None
        moment = datetime.datetime.fromtimestamp(stamp) if age < 30 * 86400 else None
        for key, (seconds, count) in RANGES.items():
            if age >= seconds:
                continue
            entry = reading.ranges[key]
            entry["tokens"] += int(tokens)
            index = count - 1 - int(age // (seconds / count))
            entry["buckets"][index] += int(tokens)
            name = event[3] if len(event) > 3 and isinstance(event[3], str) else "Unknown"
            parts = event[4] if len(event) > 4 and isinstance(event[4], dict) else {}
            valid_parts = (set(parts) == set(TOKEN_KINDS)
                           and all(isinstance(value, int) and value >= 0 for value in parts.values())
                           and sum(parts.values()) == tokens)
            if name not in entry["models"]:
                entry["models"][name] = {
                    "tokens": 0, "buckets": [0] * count, "cost_usd": None, "priced_tokens": 0,
                    "parts": {kind: 0 for kind in TOKEN_KINDS}, "known_tokens": 0,
                    "part_buckets": {kind: [0] * count for kind in TOKEN_KINDS},
                    "activity": [[0] * 24 for _ in range(7)],
                    "part_activity": {kind: [[0] * 24 for _ in range(7)] for kind in TOKEN_KINDS}}
            grouped = entry["models"][name]
            grouped["tokens"] += int(tokens)
            grouped["buckets"][index] += int(tokens)
            grouped["activity"][moment.weekday()][moment.hour] += int(tokens)
            if valid_parts:
                grouped["known_tokens"] += int(tokens)
                for kind in TOKEN_KINDS:
                    grouped["parts"][kind] += parts[kind]
                    grouped["part_buckets"][kind][index] += parts[kind]
                    grouped["part_activity"][kind][moment.weekday()][moment.hour] += parts[kind]
            if currency == "USD" and event_cost is not None:
                entry["cost_usd"] = (entry["cost_usd"] or 0.0) + event_cost
                entry["priced_tokens"] += int(tokens)
                grouped["cost_usd"] = (grouped["cost_usd"] or 0.0) + event_cost
                grouped["priced_tokens"] += int(tokens)
        if tokens:
            index = int(age // (BUCKET_MINUTES * 60))
            if 0 <= index < BUCKET_COUNT:
                reading.buckets[BUCKET_COUNT - 1 - index] += int(tokens)
            if stamp >= midnight:
                reading.today += int(tokens)
        if event_cost is not None and stamp >= midnight:
            cost += float(event_cost)
            cost_known = True
    if cost_known:
        reading.today_cost = cost
        reading.cost_currency = currency
    return reading


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #

def _codex_rollout_parser(path: str, cutoff: float | None = None) -> list:
    import datetime
    out = []
    previous = None
    started = None
    inherited_ordinal = None
    model_name = "Unknown"
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not any(kind in line for kind in ('"token_count"', '"session_meta"', '"turn_context"')):
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue
            if not isinstance(record, dict):
                continue
            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if record.get("type") == "turn_context":
                model_name = payload.get("model") or model_name
                continue
            if record.get("type") == "session_meta":
                if started is None and payload.get("id"):
                    try:
                        started = datetime.datetime.fromisoformat(
                            (payload.get("timestamp") or record["timestamp"]).replace("Z", "+00:00")).timestamp()
                        inherited_ordinal = payload.get("subagent_history_start_ordinal")
                    except (ValueError, TypeError, KeyError):
                        pass
                continue
            if record.get("type") != "event_msg":
                continue
            if payload.get("type") != "token_count":
                continue
            stamp = record.get("timestamp")
            info = payload.get("info") or {}
            if not isinstance(info, dict):
                continue
            totals = info.get("total_token_usage") or {}
            if not isinstance(totals, dict):
                continue
            keys = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens")
            counts = [totals.get(key, 0) for key in keys]
            if any(not isinstance(v, int) or v < 0 for v in counts) or not stamp:
                continue
            if previous is not None and all(value >= old for value, old in zip(counts[:3], previous[:3])):
                values = [value - old for value, old in zip(counts, previous)]
                values[3] = max(0, values[3])
            else:
                last = info.get("last_token_usage") or {}
                if not isinstance(last, dict):
                    continue
                values = [last.get(key, counts[index]) for index, key in enumerate(keys)]
                if any(not isinstance(value, int) or value < 0 for value in values):
                    previous = counts
                    continue
            tokens = sum(values[:2])
            previous = counts
            try:
                moment = datetime.datetime.fromisoformat(
                    stamp.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            ordinal = record.get("ordinal")
            if (started is not None and moment < started
                    or isinstance(inherited_ordinal, int) and isinstance(ordinal, int) and ordinal <= inherited_ordinal):
                continue
            if not isinstance(tokens, (int, float)) or not math.isfinite(tokens) or tokens <= 0:
                continue
            prompt, output, read, write = values
            if read + write > prompt:
                continue
            parts = {"input": prompt - read - write, "output": output, "cache_read": read, "cache_write": write}
            if cutoff is None or moment >= cutoff:
                out.append((moment, int(tokens), None, model_name, parts))
    return out

def _codex_rollout_files(home, cutoff: float | None = None) -> list[str]:
    files = []
    for root in (home / "sessions", home / "archived_sessions"):
        if root.exists():
            files += glob.glob(os.path.join(str(root), "**", "rollout-*.jsonl"),
                               recursive=True)
    cutoff = time.time() - 32 * 86400 if cutoff is None else cutoff
    newest = {}
    for path in files:
        try:
            modified = os.path.getmtime(path)
            name = os.path.basename(path)
            if modified >= cutoff and (name not in newest or modified > newest[name][0]):
                newest[name] = (modified, path)
        except OSError:
            continue
    return [row[1] for row in newest.values()]

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


def _opencode_event(stamp, data):
    total, cost = _opencode_message_tokens(data)
    usage = data.get("tokens") or {}
    cache = usage.get("cache") if isinstance(usage.get("cache"), dict) else {}
    def number(value):
        return int(value or 0)
    parts = {"input": number(usage.get("input")),
             "output": number(usage.get("output")) + number(usage.get("reasoning")),
             "cache_read": number(cache.get("read")), "cache_write": number(cache.get("write"))}
    named = data.get("model")
    name = data.get("modelID") or (named.get("id") if isinstance(named, dict) else named) or "Unknown"
    return (stamp, total, cost, name, parts)


def _opencode(now: float, extra_events=None) -> TokenReading:
    if extra_events is None:
        extra_events = [(ts, tok) for ts, tok in harness_by_route(now)["opencode"]]
    cutoff_ms = int((_start_of_today(now) - 32 * 86400) * 1000)
    events: list[tuple[float, int, float | None]] = []
    source = ""
    for root in _opencode_roots():
        db = root / "opencode.db"
        if db.exists():
            try:
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
                rows = cached_rows(str(db), _parse_db, cache_tag=cutoff_ms)
                for created, blob in rows:
                    try:
                        data = json.loads(blob)
                    except Exception:
                        continue
                    if data.get("role") != "assistant":
                        continue
                    tokens, cost = _opencode_message_tokens(data)
                    if tokens:
                        events.append(_opencode_event(created / 1000.0, data))
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
                    if os.path.getmtime(path) * 1000 < cutoff_ms:
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
                    events.append(_opencode_event(created / 1000.0, data))
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
    roots = [userprofile() / ".workbuddy", appdata() / "WorkBuddy"]
    seen = set()
    for root in roots:
        if not root.exists():
            continue
        for pattern in ("sessions/**/*.jsonl", "projects/**/*.jsonl", "**/*.jsonl"):
            for path in glob.glob(os.path.join(str(root), pattern), recursive=True):
                identity = os.path.normcase(os.path.realpath(path))
                if identity in seen:
                    continue
                seen.add(identity)
                try:
                    if os.path.getsize(path) > 64 * 1024 * 1024:
                        continue
                    out += cached_rows(path, _transcript_rows)
                except Exception:
                    continue
    return out


def _transcript_rows(path: str) -> list:
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"usage"' not in line:
                continue
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    out += _transcript_usage(record)
            except (ValueError, TypeError):
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
    for file in _codex_rollout_files(home, start):
        rows = cached_rows(file, _codex_rollout_parser)
        total += sum(int(event[1]) for event in rows if start <= event[0] <= now)
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
# (path, query scope) -> (file/WAL stamp, payload), so large logs are read once and
# re-used until they actually change on disk.
_file_cache: dict = {}

def cached_rows(path: str, parser, cache_tag=None) -> list:
    """Cache one current payload per path, including SQLite WAL changes."""
    try:
        stat = os.stat(path)
    except OSError:
        return []
    key = (path, cache_tag)
    stamp = (stat.st_mtime_ns, stat.st_size)
    for suffix in ("-wal",):
        try:
            related = os.stat(path + suffix)
            stamp += (related.st_mtime_ns, related.st_size)
        except OSError:
            stamp += (None, None)
    with _lock:
        hit = _file_cache.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    rows = parser(path)
    with _lock:
        for old in [old for old in _file_cache if old[0] == path and old != key]:
            del _file_cache[old]
        _file_cache[key] = (stamp, rows)
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
    import local_usage
    import providers
    for pid in providers.ORDER:
        source = _SOURCES.get(pid)
        if pid in local_usage.USAGE_PROVIDERS:
            source = lambda stamp, current=pid: local_usage.collect(current, stamp)
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
