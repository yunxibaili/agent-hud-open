"""Local transcript providers: no sign-in, network request or model invocation."""

import time

import local_usage
import model
import providers
import tokens


def fetch(cfg, *, pid):
    name, mark = providers.META[pid]
    available = any(root.exists() for root, _pattern in local_usage.layouts(pid))
    reading = (tokens.cached() or {}).get(pid)
    return model.ProviderReport(
        pid=pid, name=name, mark=mark, installed=available, available=available, ok=available,
        today_tokens=reading.today if reading else None,
        note=(getattr(reading, "notice", "") or "本地 Token 记录；未读取在线额度") if available else "尚未发现本地会话记录",
        updated_at=time.time())
