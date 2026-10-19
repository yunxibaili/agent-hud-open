"""The five tools this Windows HUD watches."""

from __future__ import annotations

import importlib

REGISTRY = [
    ("codex", "Codex", "CX"),
    ("opencode", "OpenCode", "OC"),
    ("antigravity", "Antigravity", "AG"),
    ("workbuddy", "WorkBuddy", "WB"),
    ("dsh", "DSH", "DS"),
]

ORDER = [pid for pid, _, _ in REGISTRY]
META = {pid: (name, mark) for pid, name, mark in REGISTRY}


def implementations():
    """Import lazily so one broken module cannot stop the others."""
    found = {}
    for pid in ORDER:
        try:
            module = importlib.import_module(f"providers.{pid}")
            found[pid] = module.fetch
        except Exception as exc:  # pragma: no cover - defensive
            def broken(_cfg, _pid=pid, _exc=exc):
                from model import ProviderReport
                return ProviderReport(pid=_pid, name=META[_pid][0], mark=META[_pid][1],
                                      note=f"provider 加载失败: {_exc}")
            found[pid] = broken
    return found
