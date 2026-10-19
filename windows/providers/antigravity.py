"""Antigravity quota from the already-running local language server.

Port of Sources/AgentHUDCore/Providers/Antigravity/{AntigravityClient,
AntigravityService}.swift.  macOS ps/lsof become a PowerShell CIM query and
the Win32 TCP table; only loopback is contacted, and the CSRF token never
leaves memory.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import model
from model import Detail, ProviderReport, QuotaWindow
from providers.base import http_json, iso_to_epoch, number, walk
from winx import listening_ports, powershell, userprofile

NAME = "Antigravity"
MARK = "AG"

SERVICE_PATH = "/exa.language_server_pb.LanguageServerService/"
STATUS_BODY = {"metadata": {"ideName": "antigravity", "extensionName": "antigravity",
                            "locale": "en", "ideVersion": "unknown"}}

FLAG_RE = {
    name: re.compile(r"(?:^|\s)--" + name + r"(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))")
    for name in ("app_data_dir", "csrf_token", "extension_server_port",
                 "extension_server_csrf_token")
}
AGY_RE = re.compile(r"(?:^|/)agy(?:\.exe)?(?:\s|$)")


# --------------------------------------------------------------------------- #
# process discovery
# --------------------------------------------------------------------------- #

def _flag(name: str, command: str):
    if "--" + name not in command:
        return None
    match = FLAG_RE[name].search(command)
    if not match:
        return None
    for group in match.groups():
        if group:
            return group
    return None


def candidates_from_processes(records: list[dict]) -> list[dict]:
    found = []
    for record in records:
        if not isinstance(record, dict):
            continue
        pid = record.get("ProcessId")
        command = record.get("CommandLine") or ""
        name = record.get("Name") or ""
        if not isinstance(pid, int) or not command:
            continue
        # Windows command lines use backslashes; the patterns are written the
        # POSIX way, so separators are normalised before matching.
        lower = (name + " " + command).lower().replace("\\", "/")
        if "antigravity" not in lower and "agy" not in lower and "language_server" not in lower:
            continue
        cli = ("/antigravity-cli/" in lower or "/antigravity_cli/" in lower
               or ("agy" in lower and AGY_RE.search(lower) is not None))
        server = (("language_server" in lower or "language-server" in lower)
                  and ("antigravity" in lower
                       or (_flag("app_data_dir", command) or "").startswith("antigravity")))
        if not (cli or server):
            continue
        token = _flag("csrf_token", command) or ""
        if not cli and not token:
            continue
        port = _flag("extension_server_port", command)
        try:
            port = int(port) if port else None
        except ValueError:
            port = None
        found.append({"pid": pid, "token": token, "extension_port": port,
                      "extension_token": _flag("extension_server_csrf_token", command),
                      "priority": 1 if cli else (2 if "antigravity-ide" in lower else 0),
                      "name": name})
    found.sort(key=lambda item: (item["priority"], item["pid"]))
    return found


def running_services() -> list[dict]:
    script = ("$ErrorActionPreference='SilentlyContinue';"
              "Get-CimInstance Win32_Process | "
              "Where-Object { $_.Name -match 'antigravity|language_server|agy' } | "
              "Select-Object ProcessId,Name,CommandLine | "
              "ConvertTo-Json -Compress -Depth 2")
    output = powershell(script, timeout=30).strip()
    if not output:
        return []
    try:
        parsed = json.loads(output)
    except Exception:
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    return candidates_from_processes(parsed)


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #

def endpoints_for(candidate: dict) -> list[tuple[str, int, str]]:
    ports = listening_ports(candidate["pid"])
    locations = [("https", port, candidate["token"]) for port in ports]
    if candidate.get("extension_port"):
        locations.append(("http", candidate["extension_port"],
                          candidate.get("extension_token") or candidate["token"]))
    return locations


def call(scheme: str, port: int, token: str, method: str, body=None, timeout: float = 3):
    headers = {"Connect-Protocol-Version": "1", "Content-Type": "application/json"}
    if token:
        headers["X-Codeium-Csrf-Token"] = token
    url = "%s://127.0.0.1:%d%s%s" % (scheme, port, SERVICE_PATH, method)
    status, document, text = http_json(url, headers=headers, body=body if body is not None else {},
                                       method="POST", timeout=timeout, insecure=(scheme == "https"))
    if status != 200 or not isinstance(document, dict):
        raise RuntimeError("HTTP %s" % status)
    return document


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def cadence_minutes(bucket_id: str, display_name: str):
    text = (bucket_id + " " + (display_name or "")).lower().replace("_", " ").replace("-", " ")
    if "weekly" in text:
        return 10080
    if "five hour" in text or "5 hour" in text:
        return 300
    return None


def parse_summary(document: dict):
    groups = None
    for path in (("response", "groups"), ("summary", "groups"), ("groups",)):
        candidate = walk(document, *path)
        if isinstance(candidate, list):
            groups = candidate
            break
    if groups is None:
        raise RuntimeError("响应中没有 groups")

    windows, seen_ids, active_groups = [], set(), set()
    found = []
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            raise RuntimeError("group 缺少 buckets")
        for bucket in buckets:
            if not isinstance(bucket, dict) or bucket.get("disabled") is True:
                continue
            bucket_id = bucket.get("bucketId")
            if not isinstance(bucket_id, str) or not bucket_id or bucket_id in seen_ids:
                raise RuntimeError("bucketId 缺失或重复")
            seen_ids.add(bucket_id)
            active_groups.add(index)
            remaining = bucket.get("remaining")
            fraction = bucket.get("remainingFraction")
            if fraction is None and isinstance(remaining, dict):
                fraction = remaining.get("remainingFraction")
                if fraction is None and remaining.get("case") == "remainingFraction":
                    fraction = remaining.get("value")
            fraction = number(fraction)
            if fraction is None or not (0 <= fraction <= 1):
                continue
            group_name = group.get("displayName") if isinstance(group.get("displayName"), str) else None
            bucket_name = bucket.get("displayName") if isinstance(bucket.get("displayName"), str) else bucket_id
            label = " · ".join([x for x in (group_name, bucket_name) if x])
            found.append({"group": index, "name": group_name, "id": bucket_id, "label": label,
                          "used": (1.0 - fraction) * 100.0,
                          "reset": iso_to_epoch(bucket.get("resetTime")),
                          "minutes": cadence_minutes(bucket_id, bucket.get("displayName") or "")})

    multi = len({entry["group"] for entry in found}) > 1
    for entry in found:
        short = model.period_short(entry["minutes"])
        if multi and entry["name"]:
            short = "%s %s" % (group_word(entry["name"]), short)
        windows.append(QuotaWindow(key="antigravity:" + entry["id"], label=entry["label"],
                                   short=short, used_percent=entry["used"],
                                   reset_at=entry["reset"], duration_minutes=entry["minutes"],
                                   all_models=not multi))
    return windows, set("antigravity:" + i for i in seen_ids)


def group_word(name: str) -> str:
    lower = name.lower()
    if "claude" in lower or "gpt" in lower:
        return "第三方"
    parts = name.split(" ")
    return parts[0] if parts else name


def parse_status(document: dict):
    status = document.get("userStatus")
    if not isinstance(status, dict) and isinstance(document.get("response"), dict):
        status = document["response"].get("userStatus")
    if not isinstance(status, dict):
        raise RuntimeError("响应中没有 userStatus")
    email = status.get("email") if isinstance(status.get("email"), str) else None
    team = status.get("teamId") if isinstance(status.get("teamId"), str) else None
    configs = walk(status, "cascadeModelConfigData", "clientModelConfigs")
    pools: dict[str, QuotaWindow] = {}
    if isinstance(configs, list):
        for config in configs:
            if not isinstance(config, dict):
                continue
            fraction = number(walk(config, "quotaInfo", "remainingFraction"))
            if fraction is None or not (0 <= fraction <= 1):
                continue
            alias = walk(config, "modelOrAlias", "model")
            model_id = alias if isinstance(alias, str) else ""
            label = config.get("label") if isinstance(config.get("label"), str) else model_id
            lower = (model_id + " " + (label or "")).lower()
            if any(word in lower for word in ("lite", "autocomplete", "image")):
                continue
            if "gemini" in lower:
                family, name, short = "gemini", "Gemini Models", "Gemini"
            elif "claude" in lower or "gpt" in lower:
                family, name, short = "claude-gpt", "Claude and GPT models", "第三方"
            else:
                family, name, short = model_id, label, label
            if not family:
                continue
            used = (1.0 - fraction) * 100.0
            window = QuotaWindow(key="antigravity:legacy:" + family, label=name, short=short,
                                 used_percent=used,
                                 reset_at=iso_to_epoch(walk(config, "quotaInfo", "resetTime")))
            prior = pools.get(family)
            if prior is None or (prior.used_percent is not None and used > prior.used_percent):
                pools[family] = window
    windows = [pools[key] for key in sorted(pools)]
    for window in windows:
        window.all_models = len(windows) == 1
    plan = walk(status, "userTier", "name") or walk(status, "planStatus", "planInfo", "planName")
    return windows, email, team, plan


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def fetch(cfg: dict) -> ProviderReport:
    options = (cfg.get("providers", {}) or {}).get("antigravity", {}) or {}
    report = ProviderReport(pid="antigravity", name=NAME, mark=MARK)
    gemini_home = userprofile() / ".gemini"
    if gemini_home.exists():
        report.details.append(Detail("GEMINI HOME", str(gemini_home)))

    try:
        candidates = running_services()
    except Exception as exc:
        report.note = "进程探测失败: %s" % exc
        return report

    if not candidates:
        report.installed = gemini_home.exists()
        report.available = False
        report.note = "未检测到 antigravity / language_server 进程，启动并登录 Antigravity 后即可读取"
        return report

    report.details.append(Detail("进程", ", ".join(str(c["pid"]) for c in candidates[:4])))
    deadline = time.time() + float(options.get("timeout", 20))
    fallback = None
    for candidate in candidates[:6]:
        if time.time() > deadline:
            break
        for scheme, port, token in endpoints_for(candidate):
            if time.time() > deadline:
                break
            try:
                document = call(scheme, port, token, "RetrieveUserQuotaSummary",
                                {"forceRefresh": True}, timeout=3)
                windows, ids = parse_summary(document)
            except Exception:
                windows, ids = [], set()
            if windows:
                report.windows = windows
                report.details.append(Detail("端点", "%s://127.0.0.1:%d" % (scheme, port)))
                try:
                    status = call(scheme, port, token, "GetUserStatus", STATUS_BODY, timeout=3)
                    _, email, team, plan = parse_status(status)
                    report.account = email
                    report.plan = plan
                    if team:
                        report.details.append(Detail("Team", team))
                except Exception:
                    pass
                report.ok = True
                report.updated_at = time.time()
                return report
            try:
                status = call(scheme, port, token, "GetUserStatus", STATUS_BODY, timeout=3)
                windows, email, team, plan = parse_status(status)
                if windows:
                    fallback = (windows, email, team, plan, "%s://127.0.0.1:%d" % (scheme, port))
                    break
            except Exception:
                continue
        if fallback:
            break

    if fallback:
        windows, email, team, plan, endpoint = fallback
        report.windows = windows
        report.account = email
        report.plan = plan
        report.ok = True
        report.details.append(Detail("端点", endpoint + " (GetUserStatus)"))
        if team:
            report.details.append(Detail("Team", team))
        report.updated_at = time.time()
        return report

    report.note = "Antigravity 本地服务未返回可读取的额度"
    return report
