"""OpenCode (+ Go, GLM and Kimi pools) quota.

Port of Sources/AgentHUDCore/Providers/OpenAgents/{OpenAgentCredentials,
OpenAgentQuota}.swift to Windows paths.  Read-only credential discovery plus
the vendors' own documented metadata endpoints; no model request is ever made.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import model
from model import Detail, ProviderReport, QuotaWindow
from providers.base import iso_to_epoch, number, walk
from winx import appdata, every_existing, localappdata, userprofile

NAME = "OpenCode"
MARK = "OC"

# Cloudflare in front of opencode.ai rejects the bare Python user agent.
UA = "opencode/1.0.0"

SERVICES = {
    "zen": ("OpenCode Zen", None),
    "go": ("OpenCode Go", "https://opencode.ai/zen/go/v1/usage"),
    "glm_cn": ("GLM", "https://open.bigmodel.cn/api/monitor/usage/quota/limit"),
    "glm_global": ("GLM", "https://api.z.ai/api/monitor/usage/quota/limit"),
    "kimi_cn": ("Kimi", "https://api.kimi.com/coding/v1/usages"),
    "kimi_global": ("Kimi", "https://api.kimi.ai/coding/v1/usages"),
}


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #

def config_roots() -> list[Path]:
    return every_existing([
        userprofile() / ".config" / "opencode",
        appdata() / "opencode",
        localappdata() / "opencode",
    ])


def data_roots() -> list[Path]:
    return every_existing([
        userprofile() / ".local" / "share" / "opencode",
        appdata() / "opencode",
        localappdata() / "opencode",
        userprofile() / ".config" / "opencode",
    ])


def read_json(path: Path):
    try:
        if path.stat().st_size > 1024 * 1024:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    # opencode.jsonc allows comments
    import re
    stripped = re.sub(r"//[^\n]*", "", text)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
    stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
    try:
        return json.loads(stripped)
    except Exception:
        return None


def service_for(provider: str, base_url: str | None, overrides: dict | None = None):
    """Map an OpenCode provider id (or an explicit base URL) onto a pool."""
    if not base_url:
        base_url = (overrides or {}).get(provider)
    if base_url:
        low = base_url.lower()
        try:
            url = urlsplit(low)
            if url.hostname == "opencode.ai":
                path = url.path.rstrip("/")
                return "go" if path == "/zen/go" or path.startswith("/zen/go/") else "zen"
        except ValueError:
            return None
        if "api.z.ai" in low and ("/api/coding/paas/v4" in low or "/api/anthropic" in low):
            return "glm_global"
        if "open.bigmodel.cn" in low and ("/api/coding/paas/v4" in low or "/api/anthropic" in low):
            return "glm_cn"
        if "api.kimi.ai" in low:
            return "kimi_global"
        if "api.kimi.com" in low:
            return "kimi_cn"
        return None
    return {
        "opencode-go": "go",
        "opencode": "zen",
        "opencode-zen": "zen",
        "zai": "glm_global",
        "zai-coding-plan": "glm_global",
        "zhipuai-coding-plan": "glm_cn",
        "kimi-for-coding": "kimi_cn",
        "kimi-coding": "kimi_cn",
        "kimi-code": "kimi_cn",
    }.get(provider)


def discover(cfg: dict) -> tuple[list[dict], list[tuple[str, str]]]:
    """Return (credentials, notes); each credential is {service, token, source}."""
    found: list[dict] = []
    notes: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(service, token, source, priority=5):
        """Keep one credential per pool: the client's own store wins over env
        defaults, which win over the refs Harness resolved for its plugins."""
        if not service or not token:
            return
        token = token.strip()
        if not token or (service, token) in seen:
            return
        seen.add((service, token))
        for index, existing in enumerate(found):
            if existing["service"] == service:
                if priority < existing["priority"]:
                    found[index] = {"service": service, "token": token,
                                    "source": source, "priority": priority}
                return
        found.append({"service": service, "token": token, "source": source, "priority": priority})

    overrides: dict[str, str] = {}
    for root in config_roots():
        for name in ("opencode.jsonc", "opencode.json"):
            document = read_json(root / name)
            if not isinstance(document, dict):
                continue
            providers = document.get("provider")
            if isinstance(providers, dict):
                for provider, entry in providers.items():
                    url = walk(entry, "options", "baseURL")
                    if isinstance(url, str):
                        overrides[provider] = url
            notes.append(("配置", str(root / name)))

    env = os.environ
    for var, service in (("OPENCODE_GO_API_KEY", "go"), ("OPENCODE_API_KEY", "zen"),
                         ("ZAI_API_KEY", "glm_global"), ("Z_AI_API_KEY", "glm_global"),
                         ("ZAI_CODING_CN_API_KEY", "glm_cn"), ("BIGMODEL_API_KEY", "glm_cn"),
                         ("ZHIPU_API_KEY", "glm_cn"), ("GLM_API_KEY", "glm_cn"),
                         ("KIMI_API_KEY", "kimi_cn"), ("KIMI_CODE_API_KEY", "kimi_cn")):
        if env.get(var):
            add(service, env[var], "env:" + var, priority=2)

    for home in (userprofile() / ".dsh", appdata() / "ai.deepseek.harness.desktop" / "dsh"):
        refs = read_dsh_refs_merged(home)
        for var, service in (("OPENCODE_GO_API_KEY", "go"), ("OPENCODE_API_KEY", "zen"),
                             ("ZAI_API_KEY", "glm_global"), ("KIMI_API_KEY", "kimi_cn")):
            if refs.get(var):
                add(service, refs[var], "dsh:" + str(home / ".credentials.yaml"), priority=3)

    for root in data_roots():
        auth = read_json(root / "auth.json")
        if not isinstance(auth, dict):
            continue
        notes.append(("凭据", str(root / "auth.json")))
        for provider, entry in auth.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("type") not in ("api", "oauth"):
                continue
            token = entry.get("key") or entry.get("access")
            service = service_for(provider, None, overrides)
            if isinstance(token, str) and token and not token.startswith("!"):
                add(service, token, "auth.json:%s" % provider, priority=1)

    db = None
    for root in data_roots():
        candidate = root / "opencode.db"
        if candidate.exists():
            db = candidate
            break
    if db:
        notes.append(("会话库", "%s (%.1f GB)" % (db, db.stat().st_size / 1024 ** 3)))

    return found, notes


def read_dsh_refs_merged(home: Path) -> dict:
    path = home / ".credentials.yaml"
    found: dict[str, str] = {}
    if not path.exists():
        return found
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return found
    in_refs = False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("refs:"):
            in_refs = True
            continue
        if in_refs:
            if not line.startswith((" ", "\t")):
                in_refs = False
                continue
            key, _, value = line.strip().partition(":")
            if key and value.strip():
                found[key.strip()] = value.strip().strip("'\"")
        elif ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            if key.strip().isupper() and value.strip():
                found[key.strip()] = value.strip().strip("'\"")
    return found


# --------------------------------------------------------------------------- #
# quota endpoints
# --------------------------------------------------------------------------- #

def _headers(service: str, token: str) -> dict:
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json",
               "User-Agent": UA}
    if service.startswith("glm"):
        org = os.environ.get("Z_AI_ORGANIZATION")
        project = os.environ.get("Z_AI_PROJECT")
        if org and project and os.environ.get("Z_AI_USAGE_SCOPE") == "team":
            headers["Bigmodel-Organization"] = org
            headers["Bigmodel-Project"] = project
    return headers


def _endpoint(service: str) -> str:
    url = SERVICES[service][1]
    if url is None:
        raise ValueError("OpenCode Zen 余额接口尚未接入")
    if service.startswith("glm") and os.environ.get("Z_AI_USAGE_SCOPE") == "team":
        return url + "?type=2"
    return url


def fetch_pool(credential: dict, timeout: float = 20):
    """Return (plan, windows) for one credential; raises RuntimeError with a reason."""
    from providers.base import http_json
    service, token = credential["service"], credential["token"]
    status, document, text = http_json(_endpoint(service), headers=_headers(service, token),
                                       timeout=timeout)
    if status == -1:
        raise RuntimeError(text)
    if status != 200 or not isinstance(document, dict):
        raise RuntimeError("HTTP %s %s" % (status, text.strip()[:120]))
    if service == "go":
        return _parse_go(document)
    if service.startswith("glm"):
        return _parse_glm(document)
    return _parse_kimi(document)


def _add(windows, plan, key, full, short, used, reset=None, duration=None, all_models=True):
    if used is None:
        return
    used = max(0.0, min(100.0, float(used)))
    label = full + (" · " + plan if plan else "")
    windows.append(QuotaWindow(key=key, label=label, short=short, used_percent=used,
                               reset_at=reset, duration_minutes=duration, all_models=all_models))


def _parse_go(document):
    usage = document.get("usage")
    if not isinstance(usage, dict):
        raise RuntimeError("OpenCode Go 返回了无法识别的结构")
    names = {"rolling": ("5 小时限制", "5h", 300),
             "weekly": ("每周限制", "7d", 10080),
             "monthly": ("每月限制", "30d", 43200)}
    windows = []
    for key in ("rolling", "weekly", "monthly"):
        value = usage.get(key)
        if not isinstance(value, dict):
            continue
        percent = number(value.get("percent"))
        full, short, minutes = names[key]
        _add(windows, None, "go:" + key, full, short, percent,
             reset=iso_to_epoch(value.get("resetsAt")), duration=minutes)
    return None, windows


def _parse_glm(document):
    if document.get("success") is not True and number(document.get("code")) != 200:
        raise RuntimeError("GLM 返回失败: %s" % str(document.get("msg") or document.get("code")))
    data = document.get("data") or {}
    limits = data.get("limits")
    if not isinstance(limits, list):
        raise RuntimeError("GLM 未返回 limits")
    plan = data.get("planName") if isinstance(data.get("planName"), str) else None
    units = {1: 1440, 3: 60, 5: 1, 6: 10080}
    windows = []
    for raw in limits:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if kind not in ("TOKENS_LIMIT", "CREDIT_LIMIT", "TIME_LIMIT"):
            continue
        unit, count = number(raw.get("unit")), number(raw.get("number"))
        percent = number(raw.get("percentage"))
        if unit is None or count is None or percent is None:
            continue
        limit = number(raw.get("usage"))
        if limit and limit > 0:
            current = number(raw.get("currentValue")) or 0.0
            remaining = number(raw.get("remaining"))
            used = max(limit - remaining, current) if remaining is not None else current
            percent = max(0.0, used) / limit * 100.0
        minutes = int(units.get(int(unit), 0) * count) or None
        reset_ms = number(raw.get("nextResetTime"))
        reset = reset_ms / 1000.0 if reset_ms else None
        monthly = kind == "TIME_LIMIT" and int(unit) == 5 and count == 1
        if kind == "TIME_LIMIT":
            full, short = ("MCP 每月用量" if monthly else "MCP 用量"), "MCP"
        else:
            credits = kind == "CREDIT_LIMIT"
            short = model.period_short(minutes)
            full = ("%s积分" % short) if credits else ("%s限额" % short)
        _add(windows, plan, "glm:%s:%s:%s" % (kind, unit, count), full, short, percent,
             reset=reset, duration=None if monthly else minutes, all_models=kind != "TIME_LIMIT")
    return plan, windows


def _parse_kimi(document):
    plan = walk(document, "membership", "level")
    plan = plan if isinstance(plan, str) else None
    windows = []
    usages = document.get("usages")
    if isinstance(usages, dict):
        for field, key, full, short, minutes in (
                ("limit_5h", "5h", "5 小时额度", "5h", 300),
                ("limit_7d", "weekly", "周额度", "7d", 10080),
                ("limit_month_total", "monthly", "月总额度", "30d", None)):
            ratio = number(walk(usages, field, "used_ratio"))
            if ratio is None:
                continue
            _add(windows, plan, "kimi:" + key, full, short, ratio * 100.0,
                 reset=iso_to_epoch(walk(usages, field, "reset_time")), duration=minutes)
    if not windows:
        usage = document.get("usage")
        if isinstance(usage, dict):
            limit = number(usage.get("limit"))
            used = number(usage.get("used"))
            remaining = number(usage.get("remaining"))
            if limit and limit > 0:
                if used is None and remaining is not None:
                    used = max(0.0, limit - remaining)
                if used is not None:
                    _add(windows, plan, "kimi:weekly", "周额度", "7d", used / limit * 100.0,
                         reset=iso_to_epoch(usage.get("resetTime")), duration=10080)
    return plan, windows


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def fetch(cfg: dict) -> ProviderReport:
    report = ProviderReport(pid="opencode", name=NAME, mark=MARK)
    credentials, notes = discover(cfg)
    for label, value in notes[:4]:
        report.details.append(Detail(label, value))
    if not credentials:
        report.available = False
        report.note = "未找到 OpenCode 凭据（auth.json 或环境变量 OPENCODE_GO_API_KEY）"
        return report

    failures = []
    accounts = []
    zen = False
    for credential in credentials:
        pool = SERVICES[credential["service"]][0]
        if credential["service"] == "zen":
            zen = True
            continue
        try:
            plan, windows = fetch_pool(credential)
        except Exception as exc:
            failures.append("%s: %s" % (pool, exc))
            continue
        if not windows:
            failures.append("%s: 未返回额度窗口" % pool)
            continue
        for window in windows:
            window.key = "%s|%s" % (credential["service"], window.key)
            window.label = "%s · %s" % (pool, window.label)
        report.windows += windows
        accounts.append(pool)
        report.details.append(Detail("来源", "%s ← %s" % (pool, credential["source"])))
        if plan:
            report.plan = plan

    report.account = " / ".join(dict.fromkeys(accounts)) or None
    report.ok = bool(report.windows) or (zen and all(c["service"] == "zen" for c in credentials))
    if not report.ok:
        report.note = "；".join(failures[:2]) or "未读取到额度"
    elif failures:
        report.details.append(Detail("部分失败", "；".join(failures[:2])))
    if zen:
        report.note = (report.note + " · " if report.note else "") + "Zen 按量用量见统计；账户余额未读取"
    report.updated_at = time.time()
    return report
