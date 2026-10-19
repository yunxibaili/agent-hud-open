"""Print what each dock ring is actually showing, and why.

Run:  py -3.12 ringcheck.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication

app = QApplication([])
import config
import dock
import providers
import ui_theme as T

T.resolve_fonts()
cfg = config.load()

reports = []
for name in ("codex", "opencode", "workbuddy", "dsh", "antigravity"):
    mod = getattr(providers, name, None)
    if mod is None:
        continue
    try:
        reports.extend(mod.scan(cfg))
    except Exception as exc:
        print("  %-12s scan failed: %s" % (name, exc))

bar = dock.DockBar(None, None)
bar.resize(bar.natural_size())

print()
print("%-11s %-9s %-7s %-10s %-8s %-8s %s"
      % ("agent", "headline", "used", "remaining", "arc", "label", "slot"))
for report in reports:
    window = report.headline_window()
    share = bar.share_of(report)
    slot = dock.SlotMotion(bar, lambda: None)
    if share is not None:
        slot.sweep = share
    print("%-11s %-9s %-7s %-10s %-8s %-8s loading=%s"
          % (report.name,
             window.key if window else "-",
             ("%.0f%%" % window.used_percent) if window and window.used_percent is not None else "-",
             ("%.0f%%" % (100 - window.used_percent)) if window and window.used_percent is not None else "-",
             ("%.2f" % share) if share is not None else "-",
             bar.slot_percent(report, slot) or "-",
             getattr(slot, "loading", "-")))
print()
print("arc 0.28 = a ring a little over a quarter full = 28% LEFT.")
print("If Codex shows an arc near 0.72 instead, share_of() is not the path in use.")
