"""Devin CLI's cached quota, ported from upstream DevinQuota.swift bf19ac9.

Reads no credentials and makes no request. Cache format is the CLI's JSON
envelope with a protobuf payload; only subscription fields are presented.
"""

import base64
import json
import math
import time

import model
import tokens
from winx import userprofile


def _varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            break
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("Malformed protobuf integer")


def _fields(data):
    fields, offset = {}, 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if not 0 < number < 1 << 29:
            raise ValueError("Invalid protobuf field")
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, offset = _varint(data, offset)
            else:
                size = 8 if wire == 1 else 4
            if offset + size > len(data):
                raise ValueError("Truncated protobuf field")
            value = data[offset:offset + size]
            offset += size
        else:
            raise ValueError("Unsupported protobuf wire type")
        fields[number] = value
    return fields


def fetch(cfg):
    directory = userprofile() / ".cache" / "devin" / "cli"
    report = model.ProviderReport(pid="devin", name="Devin CLI", mark="DV",
                                  available=directory.exists(), installed=directory.exists())
    reading = (tokens.cached() or {}).get("devin")
    report.today_tokens = reading.today if reading else None
    candidates = []
    for path in directory.glob("user_status.*.bin"):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not candidates:
        report.available = (userprofile() / ".local/share/devin/cli/sessions.db").exists()
        report.installed = report.available
        report.ok = report.available
        report.updated_at = time.time()
        report.note = "仅本地会话用量；尚未发现 Devin CLI 额度缓存"
        return report
    for modified, path in sorted(candidates, reverse=True):
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            envelope = json.loads(path.read_text(encoding="utf-8"))
            root = _fields(base64.b64decode(envelope["payload"], validate=True))
            status = _fields(root[13])
            observed = envelope.get("fetched_at_secs", modified)
            if not isinstance(observed, (int, float)) or not math.isfinite(observed) or observed <= 0:
                observed = modified
            windows = []
            for label, short, percent, reset, duration in (
                    ("每日额度", "1d", 15, 17, 1440), ("每周额度", "7d", 14, 18, 10080)):
                date = status.get(reset)
                if not isinstance(date, int) or not 0 < date <= 253402300799:
                    date = None
                used = status.get(percent, 0 if date is not None else None)
                if not isinstance(used, int) or not 0 <= used <= 100:
                    used = None
                if used is not None or date is not None:
                    windows.append(model.QuotaWindow(
                        key="devin:" + short, label=label, short=short, used_percent=used,
                        reset_at=date, duration_minutes=duration))
            plan = _fields(status[1]).get(2) if isinstance(status.get(1), bytes) else None
            if not windows and not plan:
                continue
            report.plan = plan.decode("utf-8")[:80] if isinstance(plan, bytes) else None
            report.windows = windows
            report.updated_at = observed
            report.ok = True
            report.note = "Devin CLI 缓存额度；需在 CLI 中刷新才能更新"
            if time.time() - observed > 1800:
                report.note += " · 缓存超过 30 分钟"
            return report
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    report.note = "Devin CLI 缓存格式不可用，请在 CLI 中刷新"
    return report
