"""WorkBuddy credits - the quota read the original project never had.

Two things are added on top of the macOS project's local-log-only WorkBuddy
support:

* credential discovery for the Windows CodeBuddyExtension auth store, including
  the WorkBuddy 5.6+ at-rest encryption ($wbEncrypted field envelopes), which
  is opened with the key the installed WorkBuddy.exe hands out when it is run
  as plain Node (ELECTRON_RUN_AS_NODE=1);
* the billing endpoint itself, POST /v2/billing/meter/get-user-resource, plus
  the enterprise variant, turned into the shared QuotaWindow shape.

The wire behaviour was ported from the DSH plugin dsh-workbuddy-connect
(MIT), which is itself a port of Sliverkiss/workbuddy2api.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import struct
import subprocess
import time
from pathlib import Path

import model
from model import Detail, ProviderReport, QuotaWindow
from providers.base import http_json, number, walk
from winx import (CREATE_NO_WINDOW, appdata, every_existing, localappdata,
                  popen, powershell, userprofile)

NAME = "WorkBuddy"
MARK = "WB"

CN_BILLING = "https://www.codebuddy.cn"
GLOBAL_BILLING = "https://www.workbuddy.ai"
CLIENT_UA = "CLI/2.63.2 CodeBuddy/2.63.2"
HELPER = ('process.stdout.write(String(process._linkedBinding('
          '"electron_browser_workbuddy_storage").loggerGet()))')

VARIANTS = {
    "cn": {"filename": "workbuddy-desktop.info", "base": CN_BILLING,
           "origin": CN_BILLING, "own": ".workbuddy-auth.json", "label": "WorkBuddy"},
    "global": {"filename": "workbuddy-desktop-ai.info", "base": GLOBAL_BILLING,
               "origin": GLOBAL_BILLING, "own": ".workbuddy-ai-auth.json",
               "label": "WorkBuddy AI"},
}

_electron_cache: dict = {}


# --------------------------------------------------------------------------- #
# at-rest decryption (WorkBuddy 5.6+)
# --------------------------------------------------------------------------- #

def _length_prefixed(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack(">I", len(raw)) + raw


def _aad(key_id: str, suite: int) -> bytes:
    return (b"WB-AAD\0" + bytes([1]) + _length_prefixed("WBEV1")
            + _length_prefixed("sym-v1") + struct.pack(">I", suite)
            + _length_prefixed(key_id) + bytes([2, 0, 0]))


def _unwrap_field(key: bytes, field) -> str:
    envelope = json.loads(base64.b64decode(field["envelope"]).decode("utf-8"))
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ciphertext = base64.b64decode(envelope["ciphertext"])
    tag = base64.b64decode(envelope["authTag"])
    nonce = base64.b64decode(envelope["nonce"])
    aad = _aad(envelope["keyId"], envelope["suite"])
    return AESGCM(key).decrypt(nonce, ciphertext + tag, aad).decode("utf-8")


def _wrapped_fields(document: dict) -> list[tuple[str, str, dict]]:
    """(container, field name, envelope) for every $wbEncrypted value found."""
    found = []
    candidates = [document]
    for section in ("account", "auth", "user", "profile"):
        if isinstance(document.get(section), dict):
            candidates.append(document[section])
    for container in candidates:
        for name, value in container.items():
            if isinstance(value, dict) and value.get("$wbEncrypted") == 1:
                found.append((container, name, value))
    return found


def _find_electron(options: dict) -> Path | None:
    configured = options.get("electron_paths") or []
    paths = [Path(os.path.expandvars(p)) for p in configured]
    paths += [
        localappdata() / "Programs" / "WorkBuddy" / "WorkBuddy.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WorkBuddy" / "WorkBuddy.exe",
        localappdata() / "Programs" / "WorkBuddy AI" / "WorkBuddyAI.exe",
    ]
    found = every_existing(paths)
    if found:
        return found[0]

    if "registry" in _electron_cache:
        return _electron_cache["registry"]
    script = ("$ErrorActionPreference='SilentlyContinue';"
              "$roots=@('HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall',"
              "'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall',"
              "'HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall');"
              "Get-ChildItem $roots | ForEach-Object { Get-ItemProperty $_.PSPath } | "
              "Where-Object { $_.DisplayName -match 'WorkBuddy' -and $_.DisplayIcon } | "
              "Select-Object DisplayName,DisplayIcon | ConvertTo-Json -Compress")
    output = powershell(script, timeout=30).strip()
    resolved = None
    try:
        parsed = json.loads(output) if output else []
        if isinstance(parsed, dict):
            parsed = [parsed]
        for entry in parsed:
            icon = (entry or {}).get("DisplayIcon") or ""
            match = re.match(r'^"?(.+?\.exe)"?(?:,\d+)?$', icon.strip(), re.I)
            if match:
                candidate = Path(match.group(1))
                if candidate.exists():
                    resolved = candidate
                    break
    except Exception:
        resolved = None
    _electron_cache["registry"] = resolved
    return resolved


def _at_rest_key(options: dict) -> tuple[bytes | None, str]:
    if "key" in _electron_cache:
        return _electron_cache["key"], _electron_cache.get("keynote", "")
    executable = _find_electron(options)
    if executable is None:
        _electron_cache["key"] = None
        _electron_cache["keynote"] = "未找到 WorkBuddy.exe，无法解开 $wbEncrypted 字段"
        return None, _electron_cache["keynote"]
    env = dict(os.environ)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        proc = popen([str(executable), "-e", HELPER], stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env)
        out, _ = proc.communicate(timeout=30)
        payload = json.loads(out.decode("utf-8", "replace").strip())
        secret = payload.get("atRestSecretKey")
        if payload.get("version") != 1 or not isinstance(secret, str) or not secret:
            raise ValueError("helper payload 不合法")
        decoded = base64.b64decode(secret)
        if len(decoded) != 32 or decoded == bytes(32):
            raise ValueError("atRestSecretKey 长度不是 32 字节")
        key = hashlib.sha256(secret.encode("utf-8")).digest()
        _electron_cache["key"] = key
        _electron_cache["keynote"] = "Electron 密钥来自 %s" % executable
        return key, _electron_cache["keynote"]
    except Exception as exc:
        _electron_cache["key"] = None
        _electron_cache["keynote"] = "WorkBuddy 密钥助手失败: %s" % exc
        return None, _electron_cache["keynote"]


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #

def auth_candidates(variant: str, options: dict) -> list[tuple[Path, str]]:
    meta = VARIANTS[variant]
    roots = [localappdata() / "CodeBuddyExtension" / "Data" / "Public" / "auth",
             appdata() / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    paths: list[tuple[Path, str]] = []
    if variant == "cn":
        paths.append((userprofile() / ".dsh" / ".workbuddy-auth.cn.json", "manual"))
    else:
        paths.append((userprofile() / ".dsh" / ".workbuddy-auth.global.json", "manual"))
    for root in roots:
        paths.append((root / meta["filename"], "desktop"))
    for home in (userprofile() / ".dsh", appdata() / "ai.deepseek.harness.desktop" / "dsh"):
        paths.append((home / meta["own"], "own"))
    for vault in options.get("vault_paths") or []:
        paths.append((Path(os.path.expandvars(vault)), "manual"))
    return paths


def manual_credential(variant: str, options: dict) -> dict | None:
    tokens = options.get("tokens") or {}
    entry = tokens.get(variant)
    if isinstance(entry, str):
        entry = {"access_token": entry}
    if not isinstance(entry, dict):
        return None
    token = entry.get("access_token") or entry.get("token")
    if not token:
        return None
    return {"variant": variant, "access_token": token, "refresh_token": entry.get("refresh_token", ""),
            "uid": entry.get("uid", ""), "domain": entry.get("domain", ""),
            "enterprise_id": entry.get("enterprise_id", ""), "nickname": entry.get("nickname"),
            "source": "config.json"}


def parse_document(text: str, variant: str, options: dict, source: str) -> tuple[dict | None, str]:
    """Return (credential, note).  Handles plaintext, plugin-owned and encrypted shapes."""
    try:
        document = json.loads(text)
    except Exception:
        return None, "auth 文件不是合法 JSON"
    if not isinstance(document, dict):
        return None, "auth 文件不是 JSON 对象"
    if isinstance(document.get("credential"), dict) and document.get("version"):
        document = {"account": {"uid": document["credential"].get("uid", "")},
                    "auth": document["credential"]}

    wrapped = _wrapped_fields(document)
    if wrapped:
        key, note = _at_rest_key(options)
        if key is None:
            return None, note
        import copy as _copy
        document = _copy.deepcopy(document)
        for container, name, _ in _wrapped_fields(document):
            try:
                container[name] = _unwrap_field(key, container[name])
            except Exception as exc:
                return None, "字段 %s 解密失败: %s" % (name, exc)

    auth = document.get("auth") if isinstance(document.get("auth"), dict) else document
    identity = document.get("account") if isinstance(document.get("account"), dict) else document
    token = auth.get("accessToken")
    if not isinstance(token, str) or not token:
        return None, "auth 文件里没有 accessToken"
    domain = auth.get("domain") if isinstance(auth.get("domain"), str) else ""
    enterprise = identity.get("enterpriseId")
    return {"variant": variant, "access_token": token,
            "refresh_token": auth.get("refreshToken") if isinstance(auth.get("refreshToken"), str) else "",
            "uid": identity.get("uid") if isinstance(identity.get("uid"), str) else "",
            "domain": domain or ("www.workbuddy.ai" if variant == "global" else "www.workbuddy.cn"),
            "enterprise_id": enterprise if isinstance(enterprise, str) else "",
            "nickname": identity.get("nickname") if isinstance(identity.get("nickname"), str) else None,
            "source": source}, ""


# --------------------------------------------------------------------------- #
# billing
# --------------------------------------------------------------------------- #

def parse_cycle_end(value):
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})$", value.strip())
    if not match:
        return None
    try:
        moment = datetime.datetime(*[int(part) for part in match.groups()])
    except ValueError:
        return None
    return moment.timestamp()


def billing_headers(credential: dict) -> dict:
    headers = {"Authorization": "Bearer " + credential["access_token"],
               "Accept": "application/json", "Content-Type": "application/json",
               "X-Requested-With": "XMLHttpRequest",
               "Origin": VARIANTS[credential["variant"]]["origin"],
               "Referer": VARIANTS[credential["variant"]]["origin"] + "/",
               "User-Agent": CLIENT_UA}
    if credential.get("uid"):
        headers["X-User-Id"] = credential["uid"]
    if credential.get("enterprise_id"):
        headers["X-Enterprise-Id"] = credential["enterprise_id"]
        headers["X-Tenant-Id"] = credential["enterprise_id"]
    if credential.get("domain"):
        headers["X-Domain"] = credential["domain"]
    return headers


def fetch_credits(credential: dict, timeout: float = 30) -> dict:
    import datetime as _dt
    base = VARIANTS[credential["variant"]]["base"]
    headers = billing_headers(credential)
    if credential["variant"] == "cn" and credential.get("enterprise_id"):
        status, document, text = http_json(base + "/v2/billing/meter/get-enterprise-user-usage",
                                           headers=headers, body={}, timeout=timeout)
        return _parse_enterprise(status, document, text)
    now = _dt.datetime.now()
    body = {
        "PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca", "Status": [0, 3],
        "PackageEndTimeRangeBegin": now.strftime("%Y-%m-%d %H:%M:%S"),
        "PackageEndTimeRangeEnd": (now + _dt.timedelta(days=36865)).strftime("%Y-%m-%d %H:%M:%S"),
    }
    status, document, text = http_json(base + "/v2/billing/meter/get-user-resource",
                                       headers=headers, body=body, timeout=timeout)
    return _parse_personal(status, document, text)


def _envelope_error(status: int, document, text: str):
    if status == -1:
        return RuntimeError(text)
    if status == 401:
        return RuntimeError("登录已失效，请在 WorkBuddy 客户端重新登录")
    if status == 402:
        return RuntimeError("积分不足")
    message = ""
    if isinstance(document, dict):
        message = str(document.get("msg") or walk(document, "displayMsg", "zh")
                      or walk(document, "displayMsg", "en") or "")
    return RuntimeError("HTTP %s %s" % (status, message or text.strip()[:120]))


def _parse_personal(status: int, document, text: str) -> dict:
    if not isinstance(document, dict) or document.get("code") not in (0, None):
        raise _envelope_error(status, document, text)
    accounts = walk(document, "data", "Response", "Data", "Accounts")
    if not isinstance(accounts, list):
        raise _envelope_error(status, document, text)
    packages, total_size, total_remain, reset = [], 0.0, 0.0, None
    for raw in accounts:
        if not isinstance(raw, dict):
            continue
        size = number(raw.get("CycleCapacitySizePrecise")) or number(raw.get("CycleCapacitySize")) or 0.0
        remain = number(raw.get("CycleCapacityRemainPrecise"))
        if remain is None:
            remain = number(raw.get("CycleCapacityRemain"))
        if remain is None:
            remain = number(raw.get("CapacityRemain")) or 0.0
        if not size:
            size = number(raw.get("CapacitySizePrecise")) or number(raw.get("CapacitySize")) or 0.0
        remain = max(0.0, remain)
        end = parse_cycle_end(raw.get("CycleEndTime"))
        packages.append({"name": raw.get("PackageName") or "(未命名)", "size": size,
                         "remain": remain, "end": end})
        total_size += size
        total_remain += remain
        if end and (reset is None or end < reset):
            reset = end
    return {"total_size": total_size, "total_remain": total_remain, "reset": reset,
            "packages": packages, "unlimited": False}


def _parse_enterprise(status: int, document, text: str) -> dict:
    if not isinstance(document, dict) or document.get("code") not in (0, None):
        raise _envelope_error(status, document, text)
    sources = []
    for candidate in (document.get("data"), document):
        if isinstance(candidate, dict):
            if isinstance(candidate.get("data"), dict):
                sources.append(candidate["data"])
            sources.append(candidate)
    limit = used = None
    reset_text = None
    for source in sources:
        value = number(source.get("limitNum"))
        if value is None:
            value = number(source.get("limit_num"))
        if value is None:
            continue
        limit = value
        used = number(source.get("credit"))
        if used is None:
            used = number(source.get("used_num"))
        if isinstance(source.get("cycleResetTime"), str) and source["cycleResetTime"]:
            reset_text = source["cycleResetTime"]
        break
    if limit is None:
        raise RuntimeError("企业版账单没有可识别的额度字段")
    if limit == -1:
        return {"total_size": 0.0, "total_remain": 0.0, "reset": None,
                "packages": [{"name": "enterprise", "size": 0.0, "remain": 0.0}],
                "unlimited": True, "reset_text": reset_text}
    used = used or 0.0
    remain = max(0.0, limit - used)
    return {"total_size": float(limit), "total_remain": remain, "reset": None,
            "packages": [{"name": "enterprise", "size": float(limit), "remain": remain}],
            "unlimited": False, "reset_text": reset_text}


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def fetch(cfg: dict) -> ProviderReport:
    options = (cfg.get("providers", {}) or {}).get("workbuddy", {}) or {}
    report = ProviderReport(pid="workbuddy", name=NAME, mark=MARK)
    notes: list[str] = []

    credentials: list[dict] = []
    for variant in ("cn", "global"):
        manual = manual_credential(variant, options)
        if manual:
            credentials.append(manual)
            continue
        read_any = False
        for path, kind in auth_candidates(variant, options):
            if not path.exists():
                continue
            read_any = True
            try:
                text_value = path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                notes.append("%s 读取失败: %s" % (path.name, exc))
                continue
            credential, note = parse_document(text_value, variant, options, str(path))
            if credential:
                credentials.append(credential)
                break
            notes.append("%s: %s" % (path.name, note))
        if not read_any:
            notes.append("未找到 %s" % VARIANTS[variant]["filename"])

    if not credentials:
        report.available = False
        report.note = "；".join(notes[:2]) or "未找到 WorkBuddy 凭据"
        report.details.append(Detail("提示", "可在 config.json 的 providers.workbuddy.tokens 手填 Token 兜底"))
        return report

    # Two accounts of one vendor must not read alike in the narrow panel.
    shorts = {"cn": "积分", "global": "AI积分"}
    failures = []
    for credential in credentials:
        variant = credential["variant"]
        label = VARIANTS[variant]["label"]
        try:
            credits = fetch_credits(credential)
        except Exception as exc:
            failures.append("%s: %s" % (label, exc))
            continue
        report.details.append(Detail("来源", "%s ← %s" % (label, credential["source"])))
        if credential.get("nickname"):
            report.account = report.account or credential["nickname"]
        if credits.get("unlimited"):
            report.windows.append(QuotaWindow(key="workbuddy:%s" % variant,
                                              label="%s · 企业积分（不限量）" % label,
                                              short=shorts.get(variant, "积分"),
                                              used_percent=None, unlimited=True))
            continue
        size, remain = credits["total_size"], credits["total_remain"]
        if size <= 0:
            failures.append("%s: 账单没有额度上限" % label)
            continue
        used = max(0.0, min(100.0, (size - remain) / size * 100.0))
        reset = credits.get("reset")
        if reset is None and credits.get("reset_text"):
            reset = parse_cycle_end(credits["reset_text"])
        report.windows.append(QuotaWindow(
            key="workbuddy:%s" % variant, label="%s · 周期积分" % label,
            short=shorts.get(variant, "积分"),
            used_percent=used, reset_at=reset,
            used_amount=size - remain, total_amount=size))
        for package in credits.get("packages", [])[:3]:
            report.details.append(Detail("  " + str(package["name"])[:22],
                                         "剩余 %.0f / %.0f" % (package["remain"], package["size"])))
        report.details.append(Detail("剩余积分", "%.1f / %.0f" % (remain, size)))

    report.ok = bool(report.windows)
    if not report.ok:
        report.note = "；".join(failures[:2]) or "未读取到 WorkBuddy 积分"
    elif failures:
        report.details.append(Detail("部分失败", "；".join(failures[:2])))
    report.updated_at = time.time()
    return report
