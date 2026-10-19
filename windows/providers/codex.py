"""Codex quota - the Codex app-server's own account/rateLimits/read.

Port of Sources/AgentHUDCore/Providers/Codex/{CodexAppServerClient,
CodexRateLimits,CodexLocator}.swift to Windows paths.  One short-lived stdio
connection per refresh; the engine is never asked to start a thread or a turn.
"""

from __future__ import annotations

import datetime
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

import model
from model import Detail, ProviderReport, QuotaWindow
from winx import appdata, every_existing, localappdata, popen, userprofile

NAME = "Codex"
MARK = "CX"

WINDOW_NAMES = {300: ("5 小时额度", "5h"), 1440: ("每日额度", "1d"),
                10080: ("每周额度", "7d"), 43200: ("每月额度", "30d"),
                525600: ("每年额度", "1y")}


# --------------------------------------------------------------------------- #
# locating the engine
# --------------------------------------------------------------------------- #

def codex_home() -> Path:
    value = os.environ.get("CODEX_HOME")
    if value:
        return Path(os.path.expandvars(value))
    return userprofile() / ".codex"


def _newest_engine_dir(parent: Path) -> Path | None:
    """The newest versioned bin folder that actually holds codex.exe."""
    try:
        dirs = [p for p in parent.iterdir() if (p / "codex.exe").exists()]
    except Exception:
        return None
    if not dirs:
        return None
    return max(dirs, key=lambda p: p.stat().st_mtime)


def find_executable(configured: str | None = None) -> tuple[Path | None, list[str]]:
    """The Codex engine, preferring the Desktop app's bundled binary.

    GUI launches on Windows have a thin PATH, so the app's own engine comes
    first.  The npm shim cannot carry stdio as a child process, so it is only
    reported, never used.
    """
    tried: list[str] = []
    if configured:
        candidate = Path(os.path.expandvars(configured))
        if candidate.exists():
            return candidate, tried
        tried.append(str(candidate))

    candidates: list[Path] = []
    newest = _newest_engine_dir(localappdata() / "OpenAI" / "Codex" / "bin")
    if newest:
        candidates.append(newest / "codex.exe")
    candidates.append(localappdata() / "OpenAI" / "Codex" / "bin" / "2e5e00daee91c61d" / "codex.exe")
    candidates += [
        userprofile() / ".codex" / "plugins" / ".plugin-appserver" / "codex.exe",
        localappdata() / "Programs" / "Codex" / "codex.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Codex" / "codex.exe",
        userprofile() / ".bun" / "bin" / "codex.exe",
        userprofile() / ".local" / "bin" / "codex.exe",
    ]
    found = shutil.which("codex")
    if found:
        candidates.append(Path(found))
    shim = appdata() / "npm" / "codex.cmd"
    if shim.exists():
        candidates.append(shim)

    for candidate in every_existing(candidates):
        if candidate.suffix.lower() == ".cmd":
            tried.append(str(candidate) + " (npm shim skipped: cannot carry stdio)")
            continue
        return candidate, tried
    return None, tried


# --------------------------------------------------------------------------- #
# protocol
# --------------------------------------------------------------------------- #

def _pump_lines(stream, sink: queue.Queue) -> None:
    try:
        for line in stream:
            sink.put(line)
    except Exception:
        pass
    sink.put(None)


def _pump_text(stream, sink: list) -> None:
    try:
        for line in stream:
            text = line.decode("utf-8", "replace").strip()
            if text:
                sink.append(text)
    except Exception:
        pass


def query(executable: Path, home: Path, timeout: float = 30) -> dict:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    proc = popen([str(executable), "app-server", "--listen", "stdio://"],
                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 cwd=os.environ.get("TEMP") or str(home), env=env, bufsize=0)
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=_pump_lines, args=(proc.stdout, lines), daemon=True).start()
    errors: list[str] = []
    threading.Thread(target=_pump_text, args=(proc.stderr, errors), daemon=True).start()

    def send(message: dict) -> None:
        proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        proc.stdin.flush()

    answers: dict[int, dict] = {}
    started = False
    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "agent_hud", "title": "Agent HUD", "version": "0.1.0"}}})
        deadline = time.time() + timeout
        grace_until = deadline
        while len(answers) < (3 if started else 1):
            if time.time() > min(deadline, grace_until):
                break
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None and lines.empty():
                    break
                continue
            if line is None:
                break
            try:
                message = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                continue
            ident = message.get("id")
            if not isinstance(ident, int) or ident not in (1, 2, 3):
                continue
            if ident == 1 and "result" in message:
                send({"method": "initialized"})
                send({"id": 2, "method": "account/rateLimits/read"})
                send({"id": 3, "method": "account/read", "params": {}})
                started = True
                continue
            answers[ident] = message
            if ident == 2:
                grace_until = time.time() + 2.0
            if ident == 1 and "error" in message:
                raise RuntimeError("Codex initialize 失败: %s" % message["error"])
    finally:
        try:
            proc.kill()
        except Exception:
            pass

    limits = answers.get(2)
    if limits is None:
        detail = next((e for e in errors if e.strip()), "")
        if proc.poll() is not None:
            raise RuntimeError("Codex 引擎提前退出（code %s）%s" % (proc.returncode, detail))
        raise RuntimeError("Codex 额度查询超时")
    if "error" in limits:
        raise RuntimeError("Codex app-server: %s" % limits["error"].get("message", "error"))
    reading = limits.get("result") or {}
    reading["account"] = ((answers.get(3) or {}).get("result") or {}).get("account")
    return reading


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def limit_label(minutes, secondary: bool) -> str:
    if minutes in WINDOW_NAMES:
        return WINDOW_NAMES[minutes][0]
    if minutes is None:
        return "次要用量额度" if secondary else "用量额度"
    if minutes % 1440 == 0:
        return "%d 天额度" % (minutes // 1440)
    if minutes % 60 == 0:
        return "%d 小时额度" % (minutes // 60)
    return "%d 分钟额度" % minutes


def buckets_of(reading: dict):
    by_id = reading.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        keys = sorted(by_id, key=lambda k: (k != "codex", k))
        return [(key, by_id[key] or {}) for key in keys]
    single = reading.get("rateLimits")
    if isinstance(single, dict):
        return [(single.get("limitId") or "codex", single)]
    return []


def window_of(raw, secondary: bool, bucket_label):
    if not isinstance(raw, dict):
        return None
    used = raw.get("usedPercent")
    if not isinstance(used, (int, float)):
        return None
    minutes = raw.get("windowDurationMins")
    minutes = int(minutes) if isinstance(minutes, (int, float)) else None
    label = limit_label(minutes, secondary)
    if bucket_label:
        label = "%s · %s" % (bucket_label, label)
    reset = raw.get("resetsAt")
    return QuotaWindow(key="%s:%s" % (bucket_label or "codex", "secondary" if secondary else "primary"),
                       label=label, short=model.period_short(minutes), used_percent=float(used),
                       reset_at=float(reset) if isinstance(reset, (int, float)) else None,
                       duration_minutes=minutes, all_models=bucket_label is None)


def fetch(cfg: dict) -> ProviderReport:
    options = (cfg.get("providers", {}) or {}).get("codex", {}) or {}
    report = ProviderReport(pid="codex", name=NAME, mark=MARK)
    home = codex_home()
    report.details.append(Detail("CODEX_HOME", str(home)))

    executable, tried = find_executable(options.get("executable"))
    if executable is None:
        report.available = False
        report.note = "未找到 Codex 引擎 codex.exe，可在配置里设置 providers.codex.executable"
        report.details += [Detail("尝试路径", p) for p in tried[:4]]
        return report

    report.details.append(Detail("引擎", str(executable)))
    # The app-server is a Node process, and the handshake plus three round trips
    # all have to fit inside one deadline. On a busy machine the cold start alone
    # can eat most of it; a single timeout used to report the whole agent as
    # unavailable, so Codex dropped out of the dock until the next poll brought
    # it back - a flicker with nothing behind it. A second, shorter attempt costs
    # a few seconds and the second one lands far more often than the first.
    reading = None
    failure = None
    for budget in (float(options.get("timeout", 45)),
                   float(options.get("retry_timeout", 25))):
        try:
            reading = query(executable, home, budget)
            break
        except Exception as exc:
            failure = exc
    if reading is None:
        report.note = str(failure)
        return report

    account = reading.get("account") or {}
    report.account = account.get("email")
    plan = None
    for _, bucket in buckets_of(reading):
        if bucket.get("planType"):
            plan = bucket["planType"]
            break
    report.plan = plan or account.get("planType")
    if not report.account:
        workspace = reading.get("accountId")
        report.account = ("工作区 %s…" % workspace[:8]) if isinstance(workspace, str) and workspace else "已登录"

    for bucket_id, bucket in buckets_of(reading):
        label = (bucket.get("limitName") or bucket_id) if bucket_id != "codex" else None
        for secondary, slot in ((False, "primary"), (True, "secondary")):
            window = window_of(bucket.get(slot), secondary, label)
            if window:
                report.windows.append(window)

    credits = reading.get("rateLimitResetCredits") or {}
    count = credits.get("availableCount")
    if isinstance(count, int):
        report.details.append(Detail("重置券", "%d 张" % count))

    report.today_tokens = _today_tokens(home)
    report.available = True          # the engine answered, so the client is here
    report.ok = bool(report.windows)
    if not report.ok:
        report.note = "Codex 未返回任何额度窗口（可能未登录）"
    report.updated_at = time.time()
    return report


# --------------------------------------------------------------------------- #
# today's tokens (cheap: only rollouts touched since local midnight)
# --------------------------------------------------------------------------- #

def _today_tokens(home: Path) -> int | None:
    midnight = datetime.datetime.combine(datetime.date.today(), datetime.time.min).timestamp()
    total = 0
    seen = 0
    for root in (home / "sessions", home / "archived_sessions"):
        if not root.exists():
            continue
        for path in root.rglob("rollout-*.jsonl"):
            try:
                if path.stat().st_mtime < midnight:
                    continue
            except OSError:
                continue
            seen += 1
            total += _sum_rollout(path, midnight)
            if seen >= 60:
                break
    return total if seen else None


def recent_token_events(home: Path, hours: float = 26.0) -> list[tuple[float, int]]:
    """Dated per-step token counts from the rollouts of the last hours.

    Only the app's own history view reads this; the quota path is unchanged.
    """
    cutoff = time.time() - hours * 3600
    events: list[tuple[float, int]] = []
    for root in (home / "sessions", home / "archived_sessions"):
        if not root.exists():
            continue
        for path in root.rglob("rollout-*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        if '"token_count"' not in line:
                            continue
                        try:
                            record = json.loads(line)
                        except Exception:
                            continue
                        if record.get("type") != "event_msg":
                            continue
                        payload = record.get("payload") or {}
                        if payload.get("type") != "token_count":
                            continue
                        stamp = record.get("timestamp")
                        if not stamp:
                            continue
                        try:
                            moment = datetime.datetime.fromisoformat(
                                stamp.replace("Z", "+00:00")).timestamp()
                        except Exception:
                            continue
                        if moment < cutoff:
                            continue
                        info = payload.get("info") or {}
                        tokens = (info.get("last_token_usage") or {}).get("total_tokens")
                        if isinstance(tokens, (int, float)) and tokens > 0:
                            events.append((moment, int(tokens)))
            except Exception:
                continue
    return events


def _sum_rollout(path: Path, midnight: float) -> int:
    subtotal = 0
    previous = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"token_count"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if record.get("type") != "event_msg":
                    continue
                payload = record.get("payload") or {}
                if payload.get("type") != "token_count":
                    continue
                stamp = record.get("timestamp")
                if stamp:
                    try:
                        moment = datetime.datetime.fromisoformat(
                            stamp.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        moment = None
                    if moment is not None and moment < midnight:
                        previous = None
                        continue
                info = payload.get("info") or {}
                last = (info.get("last_token_usage") or {}).get("total_tokens")
                current = (info.get("total_token_usage") or {}).get("total_tokens")
                if isinstance(last, (int, float)):
                    subtotal += int(last)
                elif isinstance(current, (int, float)) and previous is not None and current >= previous:
                    subtotal += int(current - previous)
                if isinstance(current, (int, float)):
                    previous = current
    except Exception:
        return 0
    return subtotal
