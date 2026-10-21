"""Windows adapters for upstream's local transcript layouts (Apache-2.0).

Reference: OpenAgentSessions, QwenSessions, TencentBuddySessions and
OpenClawSessions at jazzenchen/agent-hud-open bf19ac9. Only counters, model names
and public service labels leave readers; no prompts or credentials are retained.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import tokens
from winx import IS_WINDOWS, localappdata, userprofile

LOCAL_PROVIDERS = {"claude", "codebuddy", "pi", "kimi", "qwen", "openclaw", "hermes", "zcode", "copilot", "grok"}
USAGE_PROVIDERS = LOCAL_PROVIDERS | {"devin"}


def _home(variable, default):
    return Path(os.environ.get(variable) or userprofile() / default).expanduser()


def layouts(pid):
    home = userprofile()
    if pid == "claude":
        roots = ([_home("CLAUDE_CONFIG_DIR", ".claude")] if os.environ.get("CLAUDE_CONFIG_DIR")
                 else [home / ".claude", home / ".config" / "claude"])
        return [(root / "projects", "**/*.jsonl") for root in roots]
    if pid == "codebuddy":
        return [(_home("CODEBUDDY_CONFIG_DIR", ".codebuddy") / "projects", "**/*.jsonl")]
    if pid == "pi":
        return [(_home("PI_CODING_AGENT_DIR", ".pi/agent") / "sessions", "**/*.jsonl")]
    if pid == "kimi":
        return [(_home("KIMI_CODE_HOME", ".kimi-code") / "sessions", "**/wire.jsonl"),
                (home / ".kimi" / "sessions", "**/wire.jsonl")]
    if pid == "qwen":
        root = _home("QWEN_RUNTIME_DIR", str(_home("QWEN_HOME", ".qwen")))
        return [(root / "projects", "**/chats/**/*.jsonl")]
    if pid == "openclaw":
        base = home / ".openclaw"
        if not base.exists():
            base = home / ".clawdbot"
        roots = [_home("OPENCLAW_STATE_DIR", str(base))]
        roots += [p for p in home.glob(".openclaw-*") if p.is_dir()]
        return [(root / "agents", "*/sessions/*.jsonl*") for root in roots]
    if pid == "hermes":
        suffix = os.environ.get("HERMES_DATA_DIR_SUFFIX", "")
        root = _home("HERMES_HOME", ".hermes" + suffix)
        if IS_WINDOWS and not os.environ.get("HERMES_HOME"):
            native = localappdata() / ("hermes" + suffix)
            if (native / "state.db").is_file() or any((native / "profiles").glob("*/state.db")):
                root = native
        return [(root, "state.db"), (root / "profiles", "*/state.db")]
    if pid == "zcode":
        return [(home / ".zcode" / "cli" / "db", "db.sqlite")]
    if pid == "devin":
        return [(home / ".local" / "share" / "devin" / "cli", "sessions.db")]
    if pid == "copilot":
        return [(_home("COPILOT_HOME", ".copilot") / "session-state", "*/events.jsonl")]
    if pid == "grok":
        root = _home("GROK_HOME", ".grok")
        return [(root / "sessions", "**/updates.jsonl"), (root / "logs", "**/unified.jsonl")]
    return []


def timestamp(value):
    if isinstance(value, str):
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        return value / 1000 if value > 1e11 else float(value)
    raise ValueError("Missing or invalid timestamp")


def counter(value):
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 1 << 63:
        raise ValueError("Invalid token counter")
    return value


def _records(path):
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                if not line.endswith("\n"):
                    continue  # The client may still be appending its final line.
                raise
            if isinstance(value, dict):
                yield number, value


def _event(stamp, parts, name, identity, cost=None):
    total = sum(parts.values())
    if total <= 0:
        return None
    if not isinstance(name, str) or not name.strip():
        name = "Unknown"
    if not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        cost = None
    return (timestamp(stamp), total, cost, name[:160], parts, str(identity))


def _claude(path):
    events = {}
    stem = Path(path).stem
    for ordinal, row in _records(path):
        message = row.get("message") or {}
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if message.get("model") == "<synthetic>":
            continue
        # A fork's copied lines belong to its original session.
        if not stem.startswith("agent-") and row.get("sessionId") not in (None, stem):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        identity = message.get("id") or row.get("requestId") or "%s:%d" % (stem, ordinal)
        parts = {"input": counter(usage.get("input_tokens")),
                 "output": counter(usage.get("output_tokens")),
                 "cache_read": counter(usage.get("cache_read_input_tokens")),
                 "cache_write": counter(usage.get("cache_creation_input_tokens"))}
        event = _event(row.get("timestamp"), parts, message.get("model"), identity)
        if event:
            events[event[5]] = event
    return list(events.values())


def _pi(path, openclaw=False):
    events = {}
    for ordinal, row in _records(path):
        message = row.get("message") or {}
        if row.get("type") != "message" or not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        provider, name = message.get("provider"), message.get("model")
        if openclaw and (message.get("api") == "openclaw-transcript" or provider == "claude-cli"
                or provider == "openclaw" and name in ("delivery-mirror", "gateway-injected")
                or str(message.get("idempotencyKey", "")).startswith("codex-app-server:")):
            continue
        parts = {key: counter(usage.get(field)) for key, field in
                 (("input", "input"), ("output", "output"),
                  ("cache_read", "cacheRead"), ("cache_write", "cacheWrite"))}
        at = timestamp(row.get("timestamp") or message.get("timestamp"))
        response = message.get("responseId")
        identity = ((provider, response) if response
                    else (provider, row.get("id") or Path(path).name, at, name))
        cost = usage.get("cost")
        event = _event(at, parts, name, identity, cost.get("total") if isinstance(cost, dict) else None)
        if event:
            events[event[5]] = event
    return list(events.values())


def _qwen(path):
    events = {}
    for ordinal, row in _records(path):
        if row.get("forkedFrom") is not None or row.get("type") != "system" or row.get("subtype") != "ui_telemetry":
            continue
        event = (row.get("systemPayload") or {}).get("uiEvent") or {}
        if event.get("event.name") != "qwen-code.api_response":
            continue
        prompt = counter(event.get("input_token_count"))
        cached = counter(event.get("cached_content_token_count"))
        output = counter(event.get("output_token_count"))
        thoughts = counter(event.get("thoughts_token_count"))
        total = event.get("total_token_count")
        if cached > prompt:
            raise ValueError("Cache exceeds input")
        if total == prompt + output + thoughts:
            output += thoughts
        parts = {"input": prompt - cached, "output": output, "cache_read": cached, "cache_write": 0}
        item = _event(event.get("event.timestamp") or row.get("timestamp"), parts, event.get("model"),
                      row.get("uuid") or "%s:%d" % (Path(path).name, ordinal))
        if item:
            events[item[5]] = item
    return list(events.values())


def _kimi(path):
    events, name = {}, "Unknown"
    for ordinal, row in _records(path):
        if row.get("type") == "llm.request":
            name = row.get("model") or name
        if row.get("type") == "usage.record" and row.get("usageScope") == "turn":
            usage = row.get("usage") or {}
            fields = ("inputOther", "output", "inputCacheRead", "inputCacheCreation")
            stamp, identity = row.get("time"), "%s:%d" % (path, ordinal)
            model_name = row.get("model") or name
        else:
            message = row.get("message") or {}
            if not isinstance(message, dict) or message.get("type") != "StatusUpdate":
                continue
            payload = message.get("payload") or {}
            usage = payload.get("token_usage")
            if not isinstance(usage, dict):
                continue
            fields = ("input_other", "output", "input_cache_read", "input_cache_creation")
            stamp, identity = row.get("timestamp"), payload.get("message_id") or "%s:%d" % (path, ordinal)
            model_name = name
        parts = {key: counter(usage.get(field)) for key, field in
                 zip(("input", "output", "cache_read", "cache_write"), fields)}
        item = _event(stamp, parts, model_name, identity)
        if item and (item[5] not in events or item[1] >= events[item[5]][1]):
            events[item[5]] = item
    return list(events.values())


def _database(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    con.execute("PRAGMA query_only=1")
    deadline = time.monotonic() + 2.4
    con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    return con


def _columns(con, table):
    # The table name is always a literal owned by a reader, never database content.
    return {row[1] for row in con.execute("PRAGMA table_info(%s)" % table)}


def _hermes(path):
    events = []
    with closing(_database(path)) as con:
        session, usage = _columns(con, "sessions"), _columns(con, "session_model_usage")
        if not {"id", "started_at"} <= session:
            raise ValueError("Unsupported Hermes schema")
        def column(alias, columns, name, fallback="NULL"):
            return "%s.%s" % (alias, name) if name in columns else fallback
        counts = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        joined = {"session_id", "model"} <= usage
        model = "COALESCE(u.model, %s)" % column("s", session, "model", "'Unknown'") if joined else column("s", session, "model", "'Unknown'")
        at = "COALESCE(u.first_seen, s.started_at)" if joined and "first_seen" in usage else "s.started_at"
        fields = ["s.id", model, at]
        for name in counts:
            fallback = column("s", session, name, "0")
            fields.append("CASE WHEN u.session_id IS NULL THEN %s ELSE %s END" %
                          (fallback, column("u", usage, name, "0")) if joined else fallback)
        for name in ("billing_provider", "billing_base_url", "actual_cost_usd", "estimated_cost_usd"):
            fallback = column("s", session, name)
            fields.append("CASE WHEN u.session_id IS NULL THEN %s ELSE %s END" %
                          (fallback, column("u", usage, name)) if joined else fallback)
        activity = "MAX(s.started_at, COALESCE(%s, 0), COALESCE(%s, 0))" % (
            column("s", session, "ended_at"), column("s", session, "last_activity_at"))
        if joined and "last_seen" in usage:
            activity = "MAX(%s, COALESCE(u.last_seen, 0))" % activity
        sql = "SELECT %s FROM sessions s %s WHERE %s >= ? LIMIT 100001" % (
            ", ".join(fields), "LEFT JOIN session_model_usage u ON u.session_id=s.id" if joined else "", activity)
        for index, row in enumerate(con.execute(sql, (time.time() - 32 * 86400,))):
            if index == 100000:
                raise ValueError("Hermes row limit reached")
            parts = dict(zip(("input", "output", "cache_read", "cache_write"), map(counter, row[3:7])))
            cost = next((value for value in row[9:11]
                         if isinstance(value, (int, float)) and math.isfinite(value) and value > 0), None)
            item = _event(row[2], parts, row[1], "%s:%s:%d" % (path, row[0], index), cost)
            if item:
                events.append(item + ({"billing_source": tokens.billing_source(row[7], row[8])},))
    return events


def _zcode(path):
    events = []
    with closing(_database(path)) as con:
        columns = _columns(con, "model_usage")
        computed = "computed_total_tokens" in columns
        sql = ("SELECT id, model_id, COALESCE(completed_at, started_at), input_tokens, output_tokens, "
               "reasoning_tokens, cache_read_input_tokens, cache_creation_input_tokens, %s "
               "FROM model_usage WHERE COALESCE(completed_at, started_at) >= ? "
               "ORDER BY COALESCE(completed_at, started_at) DESC LIMIT 100001") % ("computed_total_tokens" if computed else "NULL")
        for index, row in enumerate(con.execute(sql, ((time.time() - 32 * 86400) * 1000,))):
            if index == 100000:
                raise ValueError("ZCode row limit reached")
            prompt, output, reasoning, read, write = map(counter, row[3:8])
            inclusive = not computed or row[8] == prompt + output
            if inclusive and read + write > prompt:
                raise ValueError("Cache exceeds input")
            parts = {"input": prompt - read - write if inclusive else prompt,
                     "output": output if inclusive else output + reasoning,
                     "cache_read": read, "cache_write": write}
            item = _event(row[2], parts, row[1], row[0])
            if item:
                events.append(item)
    return events


def _devin(path):
    events = {}
    with closing(_database(path)) as con:
        sql = ("SELECT row_id, session_id, created_at, "
               "CASE WHEN json_valid(chat_message) THEN json_extract(chat_message, "
               "'$.message_id', '$.metadata.request_id', '$.metadata.generation_model', "
               "'$.metadata.created_at', '$.metadata.metrics') END "
               "FROM message_nodes WHERE created_at >= ? ORDER BY row_id DESC LIMIT 100001")
        for index, row in enumerate(con.execute(sql, (time.time() - 32 * 86400,))):
            if index == 100000:
                raise ValueError("Devin row limit reached")
            fields = json.loads(row[3]) if row[3] else None
            if not isinstance(fields, list) or len(fields) != 5 or not isinstance(fields[4], dict):
                continue
            usage = fields[4]
            if usage.get("input_tokens") is None and usage.get("total_prompt_tokens") is not None:
                prompt, read = counter(usage.get("total_prompt_tokens")), counter(usage.get("total_cached_tokens"))
                if read > prompt:
                    raise ValueError("Cache exceeds input")
                parts = {"input": prompt - read, "output": counter(usage.get("total_completion_tokens")),
                         "cache_read": read, "cache_write": 0}
            else:
                parts = {key: counter(usage.get(field)) for key, field in
                         (("input", "input_tokens"), ("output", "output_tokens"),
                          ("cache_read", "cache_read_tokens"), ("cache_write", "cache_creation_tokens"))}
            item = _event(fields[3] or row[2], parts, fields[2], (row[1], fields[1] or fields[0] or row[0]))
            if item:
                events.setdefault(item[5], item)
    return list(events.values())


def _copilot(path):
    events, peaks, seen, started = [], {}, set(), False
    for ordinal, row in _records(path):
        if row.get("type") == "session.start":
            started = True
        if row.get("type") != "session.shutdown":
            continue
        data = row.get("data") or {}
        for name, metric in (data.get("modelMetrics") or {}).items():
            identity = (row.get("id") or ordinal, name)
            if identity in seen:
                continue
            seen.add(identity)
            usage = metric.get("usage") or {}
            counts = [counter(usage.get(key)) for key in ("inputTokens", "outputTokens", "cacheReadTokens")]
            before = peaks.get(name, [0, 0, 0] if started else None)
            peaks[name] = [max(a, b) for a, b in zip(peaks.get(name, counts), counts)]
            if before is None:
                continue
            prompt, output, read = [max(0, a - b) for a, b in zip(counts, before)]
            read = min(prompt, read)
            parts = {"input": prompt - read, "output": output, "cache_read": read, "cache_write": 0}
            item = _event(row.get("timestamp"), parts, data.get("currentModel") if name == "auto" else name,
                          (Path(path).parent.name, identity))
            if item:
                events.append(item)
    return events


def _grok(path):
    events, models, generations = {}, {}, {}
    unified = Path(path).name == "unified.jsonl"
    name = "Unknown"
    for ordinal, row in _records(path):
        if unified:
            pid = row.get("pid")
            message = row.get("msg")
            if message == "AuthManager::new":
                generations[pid] = generations.get(pid, 0) + 1
                continue
            session = row.get("sid")
            if not session:
                continue
            scope = (session, pid, generations.get(pid, 0))
            usage = row.get("ctx") or {}
            field = {"model changed": "model", "model catalog: notifying clients": "current_model_id",
                     "backend_search: model switch": "new_model"}.get(message)
            if field:
                models[scope] = usage.get(field) or "Unknown"
                continue
            if message != "shell.turn.inference_done":
                continue
            prompt, output, read = (counter(usage.get(key)) for key in
                                    ("prompt_tokens", "completion_tokens", "cached_prompt_tokens"))
            stamp, name = row.get("ts"), models.get(scope, "Unknown")
            identity = row.get("event_id") or row.get("eventId") or usage.get("event_id")
            if not identity:
                identity = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        else:
            if row.get("method") not in ("session/update", "_x.ai/session/update"):
                continue
            params = row.get("params") or {}
            session, meta = params.get("sessionId"), params.get("_meta") or {}
            update = params.get("update") or {}
            name = (update.get("_meta") or {}).get("modelId") or meta.get("modelId") or name
            if update.get("sessionUpdate") != "turn_completed":
                continue
            usage = update.get("usage") or {}
            if usage.get("inputTokens") is None or usage.get("outputTokens") is None:
                continue  # Context-size-only records cannot describe actual use.
            prompt, output = counter(usage.get("inputTokens")), counter(usage.get("outputTokens"))
            read = counter(usage.get("cachedReadTokens", usage.get("cacheReadTokens")))
            used_models = usage.get("modelUsage") or {}
            if len(used_models) == 1:
                name = next(iter(used_models))
            stamp, identity = meta.get("agentTimestampMs"), meta.get("eventId") or "%s:%d" % (path, ordinal)
        if not session or read > prompt:
            raise ValueError("Invalid Grok usage")
        parts = {"input": prompt - read, "output": output, "cache_read": read, "cache_write": 0}
        item = _event(stamp, parts, name, (session, identity))
        if item:
            events[item[5]] = item + (session,)
    return list(events.values())


READERS = {"claude": _claude, "codebuddy": _claude, "pi": _pi,
           "openclaw": lambda path: _pi(path, openclaw=True), "kimi": _kimi, "qwen": _qwen,
           "hermes": _hermes, "zcode": _zcode, "devin": _devin, "copilot": _copilot, "grok": _grok}


def collect(pid, now):
    events, seen, errors = {}, set(), 0
    files = []
    for root, pattern in layouts(pid):
        files.extend(root.glob(pattern))
    candidates = []
    for path in set(files):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            errors += 1
    # A moved project can retain a second copy of the same transcript.
    ordered = sorted(candidates, reverse=True)
    if pid == "grok":
        ordered.sort(key=lambda row: row[1].name != "unified.jsonl")
    inferred = set()
    for _modified, path in ordered:
        if "tool-results" in path.parts or path.name.endswith((".zst", ".tmp", ".ledger.jsonl")):
            continue
        if pid == "qwen" and "subagents" in path.parts:
            continue
        try:
            stat = path.stat()
            database = path.suffix in (".db", ".sqlite")
            if not database and stat.st_mtime < now - 32 * 86400:
                continue
            if not database and stat.st_size > 64 * 1024 * 1024:
                errors += 1
                continue
            if pid in ("claude", "codebuddy"):
                if path.name in seen:
                    continue
                seen.add(path.name)
            for event in tokens.cached_rows(str(path), READERS[pid]):
                if pid == "grok":
                    if path.name == "unified.jsonl":
                        inferred.add(event[6])
                    elif event[6] in inferred:
                        continue
                events.setdefault(event[5], event)
        except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
            errors += 1
    reading = tokens._bucketize(list(events.values()), now, "USD", "本地会话记录", pid)
    if errors:
        reading.notice = "部分来源不可读、格式不支持或超过读取上限，统计不完整"
    if pid == "copilot" and files:
        reading.notice = (reading.notice + " · " if reading.notice else "") + "仅关闭快照，尚未读取运行中 OTel 用量"
    if pid == "grok" and files:
        reading.notice = (reading.notice + " · " if reading.notice else "") + "推理日志优先；不以旧上下文大小估算用量"
    if pid == "hermes" and files:
        reading.notice = (reading.notice + " · " if reading.notice else "") + "会话/模型累计按首次记录时间归档，跨日用量不能精确拆分"
    return reading
