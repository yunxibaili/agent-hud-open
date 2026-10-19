"""Diagnostic runner: query the five providers once and print the result.

    python check.py            # human readable, all five
    python check.py codex dsh  # only the named ones
    python check.py --json     # machine readable
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config  # noqa: E402
import model  # noqa: E402
import providers  # noqa: E402


def run(only: list[str] | None = None) -> list[model.ProviderReport]:
    cfg = config.load()
    implementations = providers.implementations()
    names = only or providers.ORDER
    reports = []
    for pid in names:
        if pid not in implementations:
            continue
        started = time.time()
        try:
            report = implementations[pid](cfg)
        except Exception as exc:  # pragma: no cover - defensive
            report = model.ProviderReport(pid=pid, name=providers.META[pid][0],
                                          mark=providers.META[pid][1], note="异常: %s" % exc)
        report.elapsed = time.time() - started  # type: ignore[attr-defined]
        reports.append(report)
    return reports


def as_text(reports) -> str:
    lines = []
    for report in reports:
        level = report.worst_level(model.WARNING_USED, model.CRITICAL_USED)
        lines.append("=" * 78)
        lines.append("%-22s %-6s %s" % (report.name, level, ("OK" if report.ok else "FAIL")))
        if report.account:
            lines.append("  账户   : %s" % report.account)
        if report.plan:
            lines.append("  计划   : %s" % report.plan)
        if report.balance:
            lines.append("  余额   : %s" % report.balance)
        if report.today_tokens is not None:
            lines.append("  今日   : %s tokens%s" % (model.human_tokens(report.today_tokens),
                                                    "  ¥%.4f" % report.today_cost if report.today_cost else ""))
        for window in report.windows:
            used = "  --  " if window.used_percent is None else "%5.1f%%" % window.used_percent
            extra = ""
            if window.used_amount is not None and window.total_amount:
                extra = "  (%.1f/%.0f)" % (window.used_amount, window.total_amount)
            lines.append("  [%-14s] %-34s 已用 %s%s  重置 %s"
                         % (window.short, window.label[:34], used, extra,
                            model.reset_text(window.reset_at)))
        for detail in report.details:
            lines.append("  · %s: %s" % (detail.label, detail.value))
        if report.note:
            lines.append("  ! %s" % report.note)
        lines.append("  (%0.2fs)" % getattr(report, "elapsed", 0.0))
    return "\n".join(lines)


def as_json(reports) -> str:
    payload = []
    for report in reports:
        payload.append({
            "id": report.pid, "name": report.name, "ok": report.ok, "level": report.worst_level(
                model.WARNING_USED, model.CRITICAL_USED),
            "account": report.account, "plan": report.plan, "balance": report.balance,
            "today_tokens": report.today_tokens, "note": report.note,
            "windows": [{"key": w.key, "label": w.label, "short": w.short,
                         "used": w.used_percent, "reset": w.reset_at} for w in report.windows],
            "details": [{"label": d.label, "value": d.value} for d in report.details],
        })
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main(argv: list[str]) -> int:
    only = [a for a in argv if not a.startswith("-")]
    reports = run(only or None)
    print(as_json(reports) if "--json" in argv else as_text(reports))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
