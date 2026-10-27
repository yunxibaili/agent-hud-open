"""Per-agent local token ledger: 24 hours in 15-minute buckets, plus today.

Every client keeps its own dated record of what it spent:

* Codex   - the token_count events in its rollout logs,
* OpenCode- the assistant rows of opencode.db (v2 session_message plus
  V1-only message rows, or storage/message/*.json),
* WorkBuddy - the WorkBuddy client's own logs,
* DSH     - all Harness turns, including those billed through another service.

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
import providers.opencode as opencode_provider
import money
from winx import appdata, localappdata, userprofile

BUCKET_MINUTES = 15
BUCKET_COUNT = 96
WINDOW_SECONDS = BUCKET_COUNT * BUCKET_MINUTES * 60
CACHE_SECONDS = 90.0
RANGES = {"5h": (5 * 3600, 20), "24h": (24 * 3600, 96),
          "7d": (7 * 86400, 168), "30d": (30 * 86400, 120)}
TOKEN_KINDS = {"input": "输入（非缓存）", "output": "输出", "cache_read": "缓存读取", "cache_write": "缓存写入"}

# Billing routes describe the service used, never the execution client.
WORKBUDDY_ROUTES = ("workbuddy", "workbuddy-ai", "workbuddy-global")
OPENCODE_ROUTES = ("opencode-go",)


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
    month_tokens: int = 0
    month_cost_usd: float | None = None
    month_since: str = ""
    quota_sources: tuple[str, ...] = ()

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
    month_start = _start_of_month(now)
    reading.month_since = datetime.datetime.fromtimestamp(month_start).date().isoformat()
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
        if stamp >= month_start:
            reading.month_tokens += int(tokens)
            if currency == "USD" and event_cost is not None:
                reading.month_cost_usd = (reading.month_cost_usd or 0.0) + event_cost
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
            metadata = event[6] if len(event) > 6 and isinstance(event[6], dict) else {}
            bill_source = metadata.get("billing_source", "其他/未记录")
            sources = grouped.setdefault("billing_sources", [])
            if bill_source not in sources:
                sources.append(bill_source)
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


def billing_source(provider, base_url=None):
    """Only public service labels leave a reader, never account or endpoint details."""
    provider = provider if isinstance(provider, str) else ""
    if provider.startswith(WORKBUDDY_ROUTES):
        return "WorkBuddy"
    if provider == "deepseek-official":
        return "DeepSeek API"
    service = opencode_provider.service_for(provider, base_url)
    return opencode_provider.SERVICES[service][0] if service else "其他/未记录"


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
    provider = data.get("providerID") or (named.get("providerID") if isinstance(named, dict) else None)
    return (stamp, total, cost, name, parts, None, {"billing_source": billing_source(provider)})


def _opencode(now: float) -> TokenReading:
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
                        # One read transaction keeps both schemas on the same snapshot.
                        c.execute("BEGIN")
                        tables = {row[0] for row in c.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'")}
                        if "session_message" not in tables:
                            return [(created, blob, None) for created, blob in c.execute(
                                "SELECT time_created, data FROM message "
                                "WHERE time_created > ?", (cutoff_ms,))]
                        # V2 is canonical for every session it contains. The fork
                        # filter mirrors the official session stats query.
                        session_columns = ({row[1] for row in c.execute("PRAGMA table_info(session_v2)")}
                                           if "session_v2" in tables else set())
                        if {"id", "fork_session_id", "time_created"} <= session_columns:
                            v2 = c.execute(
                                "SELECT m.time_created, m.data FROM session_message AS m "
                                "JOIN session_v2 AS s ON s.id = m.session_id "
                                "WHERE m.type = 'assistant' AND m.time_created > ? "
                                "AND (s.fork_session_id IS NULL "
                                "OR m.time_created >= s.time_created)",
                                (cutoff_ms,)).fetchall()
                        else:
                            v2 = c.execute(
                                "SELECT time_created, data FROM session_message "
                                "WHERE type = 'assistant' AND time_created > ?",
                                (cutoff_ms,)).fetchall()
                        rows = [(created, blob, "assistant") for created, blob in v2]
                        if "message" in tables:
                            # Sessions that never migrated exist only in V1's table.
                            legacy = c.execute(
                                "SELECT m.time_created, m.data FROM message AS m "
                                "WHERE m.time_created > ? AND NOT EXISTS ("
                                "SELECT 1 FROM session_message AS sm "
                                "WHERE sm.session_id = m.session_id)",
                                (cutoff_ms,)).fetchall()
                            rows += [(created, blob, None) for created, blob in legacy]
                        return rows
                    finally:
                        c.close()
                rows = cached_rows(str(db), _parse_db, cache_tag=cutoff_ms)
                for created, blob, kind in rows:
                    try:
                        data = json.loads(blob)
                    except Exception:
                        continue
                    if kind != "assistant" and data.get("role") != "assistant":
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
        return (int(value) if isinstance(value, (int, float))
                and math.isfinite(value) and value >= 0 else 0)
    return (number("inputTokens") + number("outputTokens")
            + number("cacheWriteTokens") + number("cacheReadTokens"))


def _harness_events(prefixes: tuple[str, ...] = (),
                    exclude: tuple[str, ...] = ()) -> tuple[list, str]:
    """Assistant turns of one route, read out of the Harness session logs."""
    reader_factory = _zstd_reader()
    events, files = [], 0
    for home in _harness_homes():
        pattern = os.path.join(str(home), "sessions", "**", "session*.jsonl*")
        for path in glob.glob(pattern, recursive=True):
            if not path.endswith((".zstd", ".jsonl")):
                continue
            files += 1
            try:
                if path.endswith(".zstd"):
                    if reader_factory is None:
                        continue
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
    requested_at = None
    provider = ""
    model_name = "Unknown"
    for line in lines:
        if not any(kind in line for kind in ('"assistant/message"', '"request/header"',
                                              '"request/context"', '"step/start"')):
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if not isinstance(record, dict):
            continue
        data = record.get("data") or {}
        if not isinstance(data, dict):
            continue
        kind = record.get("type")
        stamp = record.get("time")
        if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            continue
        stamp = float(stamp) / 1000.0
        if kind == "step/start":
            requested_at = stamp
            continue
        if kind in ("request/header", "request/context"):
            header = data.get("header")
            config = header.get("config") if isinstance(header, dict) else None
            config = config if kind == "request/header" and isinstance(config, dict) else data
            if isinstance(config.get("provider"), str):
                provider = config["provider"]
            if isinstance(config.get("model"), str):
                model_name = config["model"] or "Unknown"
            # A request that crosses a tariff boundary uses its start, not its finish.
            requested_at = stamp
            continue
        if kind != "assistant/message":
            continue
        message = data.get("message")
        source = message.get("source") if isinstance(message, dict) else None
        route = source.get("provider") if isinstance(source, dict) else None
        route = route if isinstance(route, str) else provider
        name = source.get("model") if isinstance(source, dict) else None
        name = name if isinstance(name, str) and name else model_name
        usage = data.get("usage")
        tokens = _harness_usage(usage) if isinstance(usage, dict) else 0
        if tokens > 0:
            fields = {"input": "inputTokens", "output": "outputTokens",
                      "cache_read": "cacheReadTokens", "cache_write": "cacheWriteTokens"}
            # Missing cache counters are unknown, not evidence of zero cache usage.
            parts = {key: usage[field] for key, field in fields.items()
                     if isinstance(usage.get(field), int) and usage[field] >= 0}
            cost = (money.deepseek_cost(name, parts, requested_at)
                    if route == "deepseek-official" and requested_at is not None else None)
            out.append((stamp, tokens, cost, name, parts, route))
        requested_at = None
    return out


_harness_cache: dict = {}

def harness_by_route(now: float | None = None) -> dict[str, list]:
    """Scan the Harness session logs once and split the turns by provider.

    Returns {"workbuddy": [usage_event], "opencode": [...], "dsh": [...]},
    for billing-source labels. Every returned turn still belongs to DSH.
    The result is cached until a session log actually changes on disk.
    """
    now = now if now is not None else time.time()
    stamp_key = tuple(sorted(
        (p, os.stat(p).st_mtime, os.stat(p).st_size)
        for p in _harness_log_files()))
    if _harness_cache.get("key") == stamp_key and _harness_cache.get("split"):
        return _harness_cache["split"]
    all_events, source = _harness_events()
    split: dict[str, list] = {"workbuddy": [], "opencode": [], "zen": [], "dsh": []}
    for event in all_events:
        provider = event[5]
        if provider.startswith(WORKBUDDY_ROUTES):
            split["workbuddy"].append(event)
        elif provider.startswith(OPENCODE_ROUTES):
            split["opencode"].append(event)
        elif opencode_provider.service_for(provider, None) == "zen":
            split["zen"].append(event)
        else:
            split["dsh"].append(event)
    _harness_cache["key"] = stamp_key
    _harness_cache["split"] = split
    return split

def _workbuddy(now: float) -> TokenReading:
    return _bucketize(_workbuddy_local(), now, None, "WorkBuddy local logs", "workbuddy")


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
    events = [(ts, count, money.to_usd(cost, "CNY"), name, parts, None,
               {"billing_source": billing_source(provider)})
              for route in split.values() for ts, count, cost, name, parts, provider in route]
    reading = _bucketize(events, now, "USD", "Harness sessions", "dsh")
    reading.quota_sources = tuple(name for route, name in (
        ("workbuddy", "WorkBuddy"), ("opencode", "OpenCode Go"), ("zen", "OpenCode Zen"))
        if any(now - WINDOW_SECONDS <= event[0] <= now for event in split[route]))
    if reading.quota_sources:
        # A route name proves the service, not which of its accounts paid.
        reading.notice = ("近24h额度来源：%s；额度账户未关联，余量暂不可确认"
                          % " / ".join(reading.quota_sources))

    # Both records describe the same execution client; never add them together.
    _cost, _currency, whale_tokens = _whale_today()
    if whale_tokens:
        reading.today = max(reading.today, int(whale_tokens))
        reading.source = "Harness sessions + whale ledger"
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
    """Calendar totals from the same scanner snapshot as the statistics view."""
    now = now if now is not None else time.time()
    start = _start_of_month(now)
    since = datetime.datetime.fromtimestamp(start).date().isoformat()
    readings = cached() or {}
    current = {pid: reading for pid, reading in readings.items() if reading.month_since == since}
    by_agent = {pid: reading.month_tokens for pid, reading in current.items() if reading.month_tokens}
    costs = [reading.month_cost_usd for reading in current.values() if reading.month_cost_usd is not None]
    return {
        "since": since,
        "days": datetime.datetime.fromtimestamp(now).day,
        "tokens": sum(by_agent.values()),
        "by_agent": by_agent,
        "cost_usd": sum(costs) if costs else None,
        "ready": bool(current),
        "incomplete": any(reading.notice for reading in readings.values()),
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
            readings[pid].notice = "来源读取失败，当前统计不可用"
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
