"""Quota and local-usage providers supported by the Windows HUD."""

from __future__ import annotations

import importlib
from functools import partial

REGISTRY = [
    ("codex", "Codex", "CX"),
    ("opencode", "OpenCode", "OC"),
    ("antigravity", "Antigravity", "AG"),
    ("workbuddy", "WorkBuddy", "WB"),
    ("dsh", "DSH", "DS"),
    ("claude", "Claude Code", "CL"),
    ("codebuddy", "CodeBuddy", "CB"),
    ("pi", "Pi", "PI"),
    ("kimi", "Kimi", "KM"),
    ("qwen", "Qwen Code", "QW"),
    ("openclaw", "OpenClaw", "OA"),
    ("devin", "Devin CLI", "DV"),
]

ORDER = [pid for pid, _, _ in REGISTRY]
META = {pid: (name, mark) for pid, name, mark in REGISTRY}


def implementations():
    """Import lazily so one broken module cannot stop the others."""
    found = {}
    from local_usage import LOCAL_PROVIDERS
    for pid in ORDER:
        try:
            if pid in LOCAL_PROVIDERS:
                from providers.local import fetch
                found[pid] = partial(fetch, pid=pid)
                continue
            module = importlib.import_module(f"providers.{pid}")
            found[pid] = module.fetch
        except Exception as exc:  # pragma: no cover - defensive
            def broken(_cfg, _pid=pid, _exc=exc):
                from model import ProviderReport
                return ProviderReport(pid=_pid, name=META[_pid][0], mark=META[_pid][1],
                                      note=f"provider 加载失败: {_exc}")
            found[pid] = broken
    return found
