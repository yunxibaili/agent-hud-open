r"""DSH (DeepSeek Harness) balance and local activity.

The macOS project reads $DSH_HOME session logs and the official balance
endpoint.  On Windows the Harness home is $DSH_HOME (default %USERPROFILE%\.dsh)
while the desktop app keeps a second home under
%APPDATA%\ai.deepseek.harness.desktop\dsh, so both are read.  On top of the
balance this reports what Harness has mounted (its profile bundles) and today's
local usage, which is what the HUD panel shows beside the other four tools.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from pathlib import Path

import model
from model import Detail, ProviderReport
from providers.base import http_json, number, walk
from winx import appdata, every_existing, userprofile

NAME = "DeepSeek Harness"
MARK = "DS"
OFFICIAL = "https://api.deepseek.com"


def homes(cfg: dict) -> list[Path]:
    options = (cfg.get("providers", {}) or {}).get("dsh", {}) or {}
    configured = [Path(os.path.expandvars(p)) for p in (options.get("homes") or [])]
    candidates = configured + [
        Path(os.path.expandvars(os.environ["DSH_HOME"])) if os.environ.get("DSH_HOME") else None,
        userprofile() / ".dsh",
        appdata() / "ai.deepseek.harness.desktop" / "dsh",
    ]
    return every_existing(candidates)


# --------------------------------------------------------------------------- #
# settings / credentials
# --------------------------------------------------------------------------- #

def yaml_scalars(path: Path, section: str, keys: tuple[str, ...]) -> dict:
    """Minimal two-level YAML reader: just enough for llm-deepseek's two keys."""
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return {}
    found, in_section, section_indent = {}, False, 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if stripped.startswith(section + ":"):
            in_section, section_indent = True, indent
            continue
        if in_section:
            if indent <= section_indent and not stripped.startswith("-"):
                in_section = False
                continue
            for key in keys:
                if stripped.startswith(key + ":"):
                    value = stripped.split(":", 1)[1].strip().strip("'\"")
                    if value:
                        found[key] = value
            in_section = in_section or indent > section_indent
    return found


def credential_map(home: Path) -> dict:
    """Flat KEY: value lines and the refs: block of .credentials.yaml."""
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
        elif ":" in line and not line.startswith((" ", "\t", "-")):
            key, _, value = line.partition(":")
            if key.strip().isupper() and value.strip():
                found[key.strip()] = value.strip().strip("'\"")
    return found


def resolve_key(home: Path, details: list[Detail]) -> tuple[str | None, str, str | None]:
    settings = home / "settings.yaml"
    config = yaml_scalars(settings, "llm-deepseek", ("apiKeyEnv", "baseURL"))
    credentials = credential_map(home)
    env_name = config.get("apiKeyEnv", "DEEPSEEK_API_KEY")
    key = os.environ.get(env_name) or credentials.get(env_name)
    base = os.environ.get("DEEPSEEK_BASE_URL") or config.get("baseURL") or OFFICIAL
    if key:
        details.append(Detail("Key 来源", "%s @ %s" % (env_name, home.name)))
    return key, base, config.get("apiKeyEnv")


def _balance_error(base: str, home: Path, status: int) -> str:
    if status in (401, 403):
        return ("DeepSeek API Key 无效或已过期（%s 的 .credentials.yaml，状态 %s）；"
                "重新登录 Harness 或换一个 key 后余额即可显示" % (home.name, status))
    return "余额查询失败: HTTP %s @ %s" % (status, base)


def balance(home: Path, details: list[Detail]):
    """Return (amount, currency, note, had_key) for one Harness home."""
    key, base, _ = resolve_key(home, details)
    if not key:
        return None, None, "未配置 DeepSeek API Key（settings.yaml 的 llm-deepseek.apiKeyEnv 或环境变量）", False
    origin = base.rstrip("/")
    if origin != OFFICIAL:
        return None, None, "自定义 API 地址不支持官方余额查询: %s" % origin, True
    status, document, text = http_json(OFFICIAL + "/user/balance",
                                       headers={"Authorization": "Bearer " + key}, timeout=15)
    if status != 200 or not isinstance(document, dict):
        return None, None, _balance_error(origin, home, status), True
    infos = document.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        return None, None, "余额响应中没有 balance_infos", True
    total = 0.0
    currency = "CNY"
    for info in infos:
        if not isinstance(info, dict):
            continue
        amount = number(info.get("total_balance"))
        if amount is None:
            continue
        currency = info.get("currency") or currency
        total += amount
    return total, currency, None, True


# --------------------------------------------------------------------------- #
# local activity
# --------------------------------------------------------------------------- #

def whale_usage(home: Path):
    path = home / ".dshw-usage.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def today_from_whale(home: Path):
    data = whale_usage(home)
    if not data:
        return None, None
    today = datetime.date.today().isoformat()
    events = data.get("events")
    tokens = cost = 0
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict) or event.get("day") != today:
                continue
            value = number(event.get("tokens"))
            if value:
                tokens += int(value)
            value = number(event.get("cost"))
            if value:
                cost += value
    if tokens == 0 and data.get("date") == today:
        value = number(data.get("todayUsage"))
        if value:
            tokens = int(value)
    return (tokens or None), (cost or None)


def mounted_bundles(home: Path):
    bundles: list[str] = []
    for profile in sorted((home / "profiles").glob("*/package.json")) if (home / "profiles").exists() else []:
        try:
            document = json.loads(profile.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        names = walk(document, "dsh", "profile", "bundles")
        if isinstance(names, list):
            for name in names:
                if isinstance(name, str) and name not in bundles:
                    bundles.append(name)
        dependencies = document.get("dependencies")
        if isinstance(dependencies, dict):
            for name in dependencies:
                if name.startswith("dsh-") and name not in bundles:
                    bundles.append(name)
    return bundles


def session_count(home: Path) -> int:
    root = home / "sessions"
    if not root.exists():
        return 0
    count = 0
    try:
        for workspace in os.scandir(root):
            if not workspace.is_dir():
                continue
            try:
                count += sum(1 for _ in os.scandir(workspace.path))
            except OSError:
                continue
    except OSError:
        return 0
    return count


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def fetch(cfg: dict) -> ProviderReport:
    report = ProviderReport(pid="dsh", name=NAME, mark=MARK)
    found = homes(cfg)
    if not found:
        report.available = False
        report.note = "未找到 DSH 目录（$DSH_HOME 或 %USERPROFILE%\\.dsh）"
        return report

    balance_value = currency = None
    balance_note = None
    tokens = cost = None
    bundles: list[str] = []
    sessions = 0
    for home in found:
        report.details.append(Detail("DSH HOME", str(home)))
        if balance_value is None:
            value, unit, note, had_key = balance(home, report.details)
            if value is not None:
                balance_value, currency, balance_note = value, unit, None
            elif note and (balance_note is None or had_key):
                # A home that actually carried a key gives the meaningful reason.
                balance_note = note
        home_tokens, home_cost = today_from_whale(home)
        if home_tokens and not tokens:
            tokens, cost = home_tokens, home_cost
        for bundle in mounted_bundles(home):
            if bundle not in bundles:
                bundles.append(bundle)
        sessions += session_count(home)

    report.balance = ("%s %.2f" % (currency, balance_value)) if balance_value is not None else None
    report.raw_balance = balance_value
    report.balance_currency = currency
    report.today_tokens = tokens
    report.today_cost = cost
    report.details.append(Detail("会话", str(sessions)))

    plugins = [b.split("/")[-1] for b in bundles if not b.startswith("@deepseek-ai/")]
    if plugins:
        report.details.append(Detail("挂载插件", ", ".join(plugins[:6])))
        report.account = ", ".join(plugins[:3])
    if bundles:
        report.details.append(Detail("全部 bundle", "%d 个" % len(bundles)))

    report.ok = bool(report.balance) or bool(report.windows) or bool(report.today_tokens)
    if not report.ok:
        report.note = balance_note or "未读取到 DSH 余额或本地用量"
    elif balance_note and not report.today_tokens and not plugins:
        # Harness is plainly in use - it has sessions and plugins mounted - so a
        # balance this machine cannot read is not worth a red line on the card.
        report.details.append(Detail("余额说明", balance_note))
    report.updated_at = time.time()
    return report
