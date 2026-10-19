"""Windows adapters for upstream's local transcript layouts (Apache-2.0).

Reference: OpenAgentSessions, QwenSessions, TencentBuddySessions and
OpenClawSessions at jazzenchen/agent-hud-open bf19ac9. Only counters and model
names leave the readers; no prompts, identities or credentials are retained.
"""

from __future__ import annotations

import datetime
import json
import math
import os
from pathlib import Path

import tokens
from winx import userprofile

LOCAL_PROVIDERS = {"claude", "codebuddy", "pi", "kimi", "qwen", "openclaw"}


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


READERS = {"claude": _claude, "codebuddy": _claude, "pi": _pi,
           "openclaw": lambda path: _pi(path, openclaw=True), "kimi": _kimi, "qwen": _qwen}


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
    for _modified, path in sorted(candidates, reverse=True):
        if "tool-results" in path.parts or path.name.endswith((".zst", ".tmp", ".ledger.jsonl")):
            continue
        if pid == "qwen" and "subagents" in path.parts:
            continue
        try:
            stat = path.stat()
            if stat.st_mtime < now - 32 * 86400:
                continue
            if stat.st_size > 64 * 1024 * 1024:
                errors += 1
                continue
            if pid in ("claude", "codebuddy"):
                if path.name in seen:
                    continue
                seen.add(path.name)
            for event in tokens.cached_rows(str(path), READERS[pid]):
                events.setdefault(event[5], event)
        except (OSError, ValueError, TypeError, AttributeError):
            errors += 1
    reading = tokens._bucketize(list(events.values()), now, "USD", "本地会话记录", pid)
    if errors:
        reading.notice = "部分日志损坏、不可读或超过 64 MiB，统计不完整"
    return reading
