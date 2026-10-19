"""Agent HUD for Windows - a docked, three-level floating HUD.

Docking and the hover state machine are ports of the macOS app:

* NotchGeometry.swift -> windows/placement.py (edge snap, strip rect, panel growth)
* HoverMachine.swift  -> windows/hover.py (open / collapse deadlines)
* AgentPeekView.swift -> windows/peek.py (the one-line hover capsule)
* HoverPanelView.swift -> windows/panel.py (the card panel)

Level 1 is the compact strip, or a 3px sliver when edge-peek is on. Level 2 is a
single-line capsule for the mark under the pointer. Level 3 is the whole panel,
opened by a click or by Alt-hovering.
Leaving the strip and the panel collapses everything after a 250ms debounce.

    pythonw hud.py            normal launch
    python hud.py --once      query once, print, exit
    python hud.py --hidden    start in the tray
    python hud.py --expand    start with the panel open (for screenshots)
    python hud.py --peek N    start focused on mark N (for screenshots)
"""

from __future__ import annotations

import argparse
import ctypes
import math
import os
import queue
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config  # noqa: E402
import history as history_module  # noqa: E402
import hover  # noqa: E402
import model  # noqa: E402
import money  # noqa: E402
import motion  # noqa: E402
import peek as peek_module  # noqa: E402
import placement as geo  # noqa: E402
import providers  # noqa: E402
import running  # noqa: E402
import tokens  # noqa: E402
import ui_theme as T  # noqa: E402
import winx  # noqa: E402
from dock import DockBar, HEIGHT as DOCK_HEIGHT, MONTH_PID  # noqa: E402
from panel import Panel  # noqa: E402
from peek import LinePill, peek_segments, reminder_segments  # noqa: E402
from usage_view import UsageWindow  # noqa: E402

from PySide6.QtCore import QLockFile, QPoint, QRect, QSize, Qt, QTimer  # noqa: E402
from PySide6.QtGui import (QActionGroup, QColor, QIcon, QPainter, QRegion,  # noqa: E402
                           QPixmap)
from PySide6.QtWidgets import (QApplication, QGraphicsDropShadowEffect,  # noqa: E402
                               QMenu, QSystemTrayIcon, QWidget)

VERSION = "0.1.14"
SHADOW = 30
GAP = 6
PANEL_WIDTH = 472
PILL_RESERVE = 680
HOVER_FAST_MS = 33
HOVER_SLOW_MS = 100
FRAME_ACTIVE_MS = 16
FRAME_IDLE_MS = 66
REMINDER_SECONDS = 5.0
HIDE_AFTER = 0.6

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
VK_MENU = 0x12


def log(message: str) -> None:
    try:
        with open(config.LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except Exception:
        pass


def alt_pressed() -> bool:
    if not winx.IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(VK_MENU) & 0x8000)
    except Exception:
        return False


def set_click_through(hwnd: int, enabled: bool) -> None:
    """Let clicks fall through a peeked-away dock to the app underneath."""
    if not winx.IS_WINDOWS or not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_TRANSPARENT) if enabled else (style & ~WS_EX_TRANSPARENT)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# background polling (unchanged data layer)
# --------------------------------------------------------------------------- #

class Poller(threading.Thread):
    def __init__(self, cfg: dict, pid: str, fetch, out: queue.Queue,
                 stop: threading.Event, delay: float):
        super().__init__(name="poll-" + pid, daemon=True)
        self.cfg = cfg
        self.pid = pid
        self.fetch = fetch
        self.out = out
        self.stop = stop
        self.delay = delay

    def run(self) -> None:
        self.stop.wait(self.delay)
        while not self.stop.is_set():
            try:
                report = self.fetch(self.cfg)
            except Exception as exc:
                report = model.ProviderReport(pid=self.pid,
                                              name=providers.META[self.pid][0],
                                              mark=providers.META[self.pid][1],
                                              note="刷新异常: %s" % exc)
            try:
                if self.stop.is_set():
                    return
                self.out.put(report)
            except Exception:
                return
            key = "refresh_seconds" if report.ok or not report.available else "retry_seconds"
            interval = max(15.0, float(self.cfg.get(key, 120 if key == "refresh_seconds" else 30)))
            deadline = time.time() + interval
            while not self.stop.is_set() and time.time() < deadline:
                self.stop.wait(0.2)


# --------------------------------------------------------------------------- #
# the floating window
# --------------------------------------------------------------------------- #

class HudWindow(QWidget):
    def __init__(self, cfg: dict, marks: T.Marks):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.cfg = cfg
        self.marks = marks
        dock_cfg = cfg.get("dock", {}) or {}
        self.warn = float(cfg.get("quota_warning_used", 70))
        self.crit = float(cfg.get("quota_critical_used", 90))
        self.interval = max(15, int(cfg.get("refresh_seconds", 120)))

        self.placement = geo.Placement.from_dict(dock_cfg)
        self.show_percent = bool(dock_cfg.get("show_percent", False))
        self.style = str(dock_cfg.get("style", "ring"))
        self.edge_peek = bool(dock_cfg.get("edge_peek", True))
        self.revealed = not self.edge_peek
        self.hover_delay = float(dock_cfg.get("hover_delay", hover.PANEL_REST_DELAY))
        self.collapse_delay = float(dock_cfg.get("collapse_delay", hover.COLLAPSE_DELAY))
        self.levels = hover.DockLevels(self.hover_delay, self.collapse_delay,
                                      float(dock_cfg.get("peek_delay", hover.PEEK_DELAY)))

        self.reports: dict[str, model.ProviderReport] = {}
        self.queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.running_agents: set[str] = set()
        self.runtime_known = False
        self.runtime_queue: queue.Queue = queue.Queue()
        self.enabled = [pid for pid in providers.ORDER
                        if (cfg.get("providers", {}).get(pid, {}) or {}).get("enabled", True)]
        self.history = history_module.History()
        self.month_page = False
        self.month_data = tokens.month_cached()
        self.month_queue: queue.Queue = queue.Queue()
        self.month_thread: threading.Thread | None = None
        self.readings = None
        self._keep_region = QRegion()
        self._placing = False
        self._window_reserve = QSize()

        self.phase = 0.0
        self._frame_at = time.monotonic()
        self.press_at: QPoint | None = None
        self.press_origin: QPoint | None = None
        self.dragging = False
        self.alt_opened = False
        self.outside_since: float | None = None
        self.reminder_until = 0.0
        self.prev_used: dict[str, dict[str, float]] = {}
        self.forecast_warned_until: dict[tuple[str, str], float] = {}
        self.was_active: dict[str, bool] = {}
        self.peek_sig = None
        self.panel_sig = None

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setWindowTitle("Agent HUD")
        if not cfg.get("panel", {}).get("always_on_top", True):
            self.setWindowFlag(Qt.WindowStaysOnTopHint, False)

        self.content = QWidget(self)
        self.content.setAttribute(Qt.WA_TranslucentBackground, True)
        self.content.setContentsMargins(SHADOW, SHADOW, SHADOW, SHADOW)

        self.dock = DockBar(marks, self.content)
        self.peek_pill = LinePill(int(DOCK_HEIGHT), self.content)
        self.reminder = LinePill(int(DOCK_HEIGHT), self.content)
        self.reminder.background = QColor(0x1B, 0x1C, 0x22, 0xF2)
        self.panel = Panel(marks, PANEL_WIDTH, self.content)
        self.panel.on_statistics = self._open_statistics
        self.statistics_window = None
        for widget in (self.dock, self.peek_pill, self.reminder, self.panel):
            shadow = QGraphicsDropShadowEffect(widget)
            shadow.setBlurRadius(26)
            shadow.setOffset(0, 6)
            shadow.setColor(QColor(0, 0, 0, 150))
            widget.setGraphicsEffect(shadow)
        self.peek_pill.hide()
        self.reminder.hide()
        self.panel.hide()
        self.panel.on_height = self._apply_geometry
        self.tuck_anim = motion.Scalar(self, self._tuck_changed, ("tuck",))
        self.tuck_anim.snap("tuck", 0.0 if self.revealed else 1.0)

        self._build_tray()
        self._apply_direction()
        self._apply_presentation()
        self._apply_geometry()

        self.hover_timer = QTimer(self)
        self.hover_timer.setInterval(HOVER_FAST_MS if self.revealed else HOVER_SLOW_MS)
        self.hover_timer.timeout.connect(self._hover_tick)
        self.hover_timer.start()

        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(1000)
        self.clock_timer.timeout.connect(self._clock_tick)
        self.clock_timer.start()

        self.anim_timer = QTimer(self)
        self.anim_timer.setInterval(FRAME_ACTIVE_MS)
        self.anim_timer.timeout.connect(self._animate)
        self.anim_timer.start()

        self.drain_timer = QTimer(self)
        self.drain_timer.setInterval(300)
        self.drain_timer.timeout.connect(self._drain)
        self.drain_timer.start()

        self.reminder_timer = QTimer(self)
        self.reminder_timer.setSingleShot(True)
        self.reminder_timer.timeout.connect(self._hide_reminder)

        self.start_pollers()
        self.runtime_thread = threading.Thread(target=running.watch,
                                               args=(self.runtime_queue, self.stop_event),
                                               name="agent-processes", daemon=True)
        self.runtime_thread.start()
        money.set_rate((cfg.get("currency") or {}).get("usd_cny", money.DEFAULT_USD_CNY))
        self.token_scanner = tokens.Scanner(stop=self.stop_event)
        self.token_scanner.start()
        self.month_timer = QTimer(self)
        self.month_timer.setInterval(int(tokens.MONTH_SECONDS * 1000))
        self.month_timer.timeout.connect(self._refresh_month)
        self.month_timer.start()
        self._refresh_month()
        self._refresh_dock()

    # -- screen helpers -----------------------------------------------------
    def screen_frame(self) -> QRect:
        screen = self.screen() or QApplication.primaryScreen()
        return screen.geometry() if screen else QRect(0, 0, 1280, 720)

    def strip_rect(self) -> QRect:
        return geo.strip_rect(self.dock.natural_size(), self.screen_frame(), self.placement)

    def hot_zone(self) -> QRect:
        return geo.edge_hot_zone(self.strip_rect(), self.screen_frame(), self.placement.edge)

    # -- layout -------------------------------------------------------------
    def _apply_direction(self) -> None:
        self.dock.set_edge(self.placement.edge.value, self.placement.mode == geo.Mode.NOTCH)
        self._tuck_changed()

    def _apply_presentation(self) -> None:
        horizontal = self.placement.edge.is_horizontal
        self.dock.set_edge(self.placement.edge.value, self.placement.mode == geo.Mode.NOTCH)
        self.dock.set_presentation(horizontal, self.show_percent, self.style)
        self.dock.setFixedSize(self.dock.natural_size())

    def _apply_geometry(self) -> None:
        if self._placing or self.dragging:
            return
        self._placing = True
        try:
            frame = self.screen_frame()
            edge = self.placement.edge
            strip = self.strip_rect()
            # Keep the native backing surface stable; only Qt children change page size.
            width = min(frame.width(), max(PILL_RESERVE + 2 * SHADOW,
                                           strip.width() + 2 * SHADOW))
            if not edge.is_horizontal:
                width = min(frame.width(), max(width, strip.width() + GAP +
                                               PILL_RESERVE + 2 * SHADOW))
            height = frame.height()
            self._window_reserve = QSize(width, height)
            if self.size() != self._window_reserve:
                self.setFixedSize(self._window_reserve)
            x = max(frame.left(), min(strip.center().x() - width // 2,
                                       frame.right() + 1 - width))
            if edge == geo.Edge.LEFT:
                x = frame.left()
            elif edge == geo.Edge.RIGHT:
                x = frame.right() + 1 - width
            self.move(x, frame.top())
            self.content.setGeometry(self.rect())
            origin = self.pos()
            self.dock.setGeometry(strip.translated(-origin))
            anchor = QRect(strip)
            keep = QRegion(strip)
            side_y = strip.center().y()
            if not edge.is_horizontal:
                widgets = [w for w in (self.reminder, self.peek_pill, self.panel)
                           if not w.isHidden()]
                total = sum(w.sizeHint().height() if w is self.panel else w.height()
                            for w in widgets) + max(0, len(widgets) - 1) * GAP
                side_y = max(frame.top() + SHADOW,
                             min(side_y - total // 2, frame.bottom() + 1 - SHADOW - total))
            for widget in (self.reminder, self.peek_pill, self.panel):
                if widget.isHidden():
                    continue
                if widget is self.panel:
                    room = (frame.height() - strip.height() - 3 * GAP
                            - DOCK_HEIGHT - SHADOW if edge.is_horizontal
                            else frame.height() - 2 * SHADOW)
                    self.panel.set_max_height(int(room))
                    self.panel.outer.activate()
                    target_height = min(self.panel.sizeHint().height(), self.panel.max_h)
                    widget.resize(PANEL_WIDTH, max(1, target_height))
                size = widget.size()
                if not edge.is_horizontal:
                    anchor = strip
                target = geo.centre_along(anchor, size, frame, edge)
                step = {geo.Edge.TOP: QPoint(0, GAP), geo.Edge.BOTTOM: QPoint(0, -GAP),
                        geo.Edge.LEFT: QPoint(GAP, 0), geo.Edge.RIGHT: QPoint(-GAP, 0)}[edge]
                target.translate(step)
                if not edge.is_horizontal:
                    target.moveTop(side_y)
                    side_y += target.height() + GAP
                target.moveLeft(max(frame.left(), min(target.left(),
                                                      frame.right() + 1 - target.width())))
                target.moveTop(max(frame.top(), min(target.top(),
                                                    frame.bottom() + 1 - target.height())))
                widget.setGeometry(target.translated(-origin))
                keep |= QRegion(target.adjusted(-GAP, -GAP, GAP, GAP))
                anchor = target
            self._keep_region = keep
            self.update()
        finally:
            self._placing = False
        set_click_through(int(self.winId()), not self.revealed)

    def paintEvent(self, event) -> None:
        # Clear old child bounds too; alpha-zero pixels also pass clicks through on Windows.
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(event.rect(), Qt.transparent)

    def _tuck_changed(self) -> None:
        signs = {geo.Edge.TOP: QPoint(0, -1), geo.Edge.BOTTOM: QPoint(0, 1),
                 geo.Edge.LEFT: QPoint(-1, 0), geo.Edge.RIGHT: QPoint(1, 0)}
        self.dock.set_tuck(self.tuck_anim.value("tuck"), signs[self.placement.edge])
        self.dock.update()

    def _reveal(self, revealed: bool) -> None:
        if self.revealed == revealed:
            return
        self.revealed = revealed
        if not revealed:
            self.levels.collapse()
            self.month_page = False
            self._reminder_visible(False)
            self.peek_pill.hide()
            self.panel.hide()
        self.tuck_anim.to("tuck", 0.0 if revealed else 1.0, 220, motion.OUT_CUBIC)
        self._frame_at = time.monotonic()
        self.anim_timer.start(FRAME_ACTIVE_MS)
        self.hover_timer.setInterval(HOVER_FAST_MS if revealed else HOVER_SLOW_MS)
        self._apply_geometry()

    # -- hover / levels -----------------------------------------------------
    def _bar_reports(self) -> list[model.ProviderReport]:
        out = []
        # WorkBuddy remains a quota entry while DSH is running through it.
        show_workbuddy = "dsh" in self.running_agents
        for pid in self.enabled:
            if pid not in self.running_agents and not (pid == "workbuddy" and show_workbuddy):
                continue
            report = self.reports.get(pid)
            if report is None:
                name, mark = providers.META[pid]
                report = model.ProviderReport(pid=pid, name=name, mark=mark)
            reading = (self.readings or {}).get(pid)
            if pid == "dsh" and reading is not None and reading.quota_sources:
                # Do not present DeepSeek's balance as a different service's quota.
                report = replace(report, balance=None, raw_balance=None,
                                 balance_currency=None, windows=[], plan=None,
                                 today_tokens=reading.today, today_cost=reading.today_cost,
                                 ok=bool(reading.events), note=None, details=[],
                                 quota_note=reading.notice)
            out.append(report)
        return out

    def _dock_reports(self) -> list[model.ProviderReport]:
        return self._bar_reports() + [model.ProviderReport(pid=MONTH_PID, name="月度总量", mark="月", ok=True)]

    def _dock_index(self, point: QPoint) -> int:
        local = self.dock.mapFromGlobal(point)
        drawn = self.dock.index_at(local) if self.dock.rect().contains(local) else -1
        if drawn < 0:
            return -1
        pid = self.dock.reports[drawn].pid
        return next((i for i, report in enumerate(self._dock_reports()) if report.pid == pid), -1)

    def _refresh_month(self) -> None:
        if self.stop_event.is_set() or (self.month_thread and self.month_thread.is_alive()):
            return
        self.month_thread = threading.Thread(target=self._month_worker, name="month-scanner", daemon=True)
        self.month_thread.start()

    def _month_worker(self) -> None:
        self.month_queue.put(tokens.refresh_month())

    def show_month(self) -> None:
        self.month_page = True
        self.panel_sig = None
        self.levels.collapse()
        self.levels.open_panel(len(self._bar_reports()))
        self._apply_level()

    def _apply_level(self) -> None:
        level = self.levels.level
        index = self.levels.index
        reports = self._dock_reports()
        if level == hover.Level.HIDDEN:
            self.month_page = False
        show_peek = (level.value >= hover.Level.PEEK.value and index is not None
                     and 0 <= index < len(reports) and reports[index].pid != MONTH_PID
                     and not self.month_page and not self._reminder_showing())
        show_panel = (level.value >= hover.Level.PANEL.value and index is not None
                      and 0 <= index < len(reports) and not self._reminder_showing()
                      and (self.month_page or reports[index].pid != MONTH_PID))

        signature = (tuple((r.pid, r.updated_at) for r in reports), index,
                     self.month_page, id(self.readings), id(self.month_data))
        if self.month_page:
            signature = ("month", id(self.month_data))
        changed = False
        if show_peek and signature != self.peek_sig:
            self.peek_sig = signature
            self.peek_pill.set_segments(peek_segments(reports[index], self.warn, self.crit))
            changed = True
        if show_panel and signature != self.panel_sig:
            self.panel_sig = signature
            if self.month_page:
                self.panel.set_month(self.month_data or {})
            else:
                self._rebuild_panel(reports, index)
            self.panel.retarget_height()
            changed = True

        was = (not self.peek_pill.isHidden(), not self.panel.isHidden())
        if show_peek:
            if not was[0]:
                self.peek_pill.fade_in(self._inward())
        else:
            self.peek_pill.hide()
        self.panel.setVisible(show_panel)
        if show_panel and not was[1]:
            self.panel.set_reveal(0.0)
            self.panel.tween_reveal(1.0, 180, motion.OUT_CUBIC)
        if changed or was != (show_peek, show_panel):
            self._apply_geometry()

    def _inward(self) -> QPoint:
        return {geo.Edge.TOP: QPoint(0, 1), geo.Edge.BOTTOM: QPoint(0, -1),
                geo.Edge.LEFT: QPoint(1, 0), geo.Edge.RIGHT: QPoint(-1, 0)}[self.placement.edge]

    def _rebuild_panel(self, reports, index: int) -> None:
        focus_pid = reports[index].pid if 0 <= index < len(reports) else None
        entries = []
        for report in reports:
            if report.pid == MONTH_PID:
                continue
            focused = report.pid == focus_pid
            reading = (self.readings or {}).get(report.pid)
            burn = burn_window = None
            if focused:
                best = None
                for window in report.windows:
                    estimate = self.history.forecast(report, window)
                    if not estimate or not estimate["before_reset"]:
                        continue
                    if best is None or estimate["hours_to_empty"] < best[0]:
                        best = (estimate["hours_to_empty"], estimate, window)
                if best:
                    burn, burn_window = best[1], best[2]
            entries.append((report, focused, reading, burn, burn_window))
        stamp = max((r.updated_at for r in reports), default=0)
        totals = [(name, T.agent_color(pid), reading)
                  for pid, name, _mark in providers.REGISTRY
                  if (reading := (self.readings or {}).get(pid)) is not None
                  and (reading.events or pid in self.running_agents)]
        self.panel.begin_crossfade(focus_pid)
        self.panel.set_reports(entries, self.warn, self.crit, stamp, self.interval, totals)
        self.panel.outer.activate()

    def _hover_tick(self) -> None:
        if self.dragging:
            return
        point = self.cursor().pos()
        inside = self._keep_region.contains(point)

        if not self.revealed:
            if alt_pressed() or self.hot_zone().contains(point):
                self._reveal(True)
            return

        index = -1
        if inside:
            index = self._dock_index(point)
        drawn = self.dock.index_at(self.dock.mapFromGlobal(point)) if inside else -1
        if drawn != self.dock.hover_index:
            self.dock.hover_index = drawn
            self.dock.set_hover(drawn)

        now = time.monotonic()
        if inside:
            self.outside_since = None
            self.levels.pointer_entered(index if index >= 0 else None)
        else:
            self.levels.pointer_exited()
            if self.outside_since is None:
                self.outside_since = now
        self.levels.tick()

        if alt_pressed() and inside:
            if index == len(self._bar_reports()):
                if not self.month_page:
                    self.show_month()
            elif index >= 0:
                self.month_page = False
                self.levels.open_panel(index)
            elif not self.month_page:
                self.levels.open_panel(None)
            self.alt_opened = True
        elif self.alt_opened and not alt_pressed():
            self.alt_opened = False
            if not inside:
                self.levels.collapse()

        self._apply_level()

        if (self.edge_peek and not inside and self.levels.level == hover.Level.HIDDEN
                and self.outside_since is not None
                and now - self.outside_since > HIDE_AFTER):
            self._reveal(False)

    # -- reminders ----------------------------------------------------------
    def _reminder_showing(self) -> bool:
        return time.time() < self.reminder_until

    def _reminder_visible(self, visible: bool) -> None:
        if visible:
            self.reminder.show()
        else:
            self.reminder_until = 0.0
            self.reminder.hide()

    def _show_reminder(self, segments) -> None:
        self.reminder.set_segments(segments)
        self.reminder_until = time.time() + REMINDER_SECONDS
        self.reminder.fade_in(self._inward())
        self.reminder_sig = True
        self.levels.collapse()
        self.month_page = False
        self.peek_pill.hide()
        self.panel.hide()
        self._apply_geometry()
        self.reminder_timer.start(int(REMINDER_SECONDS * 1000))

    def _hide_reminder(self) -> None:
        self._reminder_visible(False)
        self._apply_geometry()
        self._apply_level()

    def _audit(self, reports) -> None:
        """Quota alerts, resets and finished sessions, one pill at a time."""
        now = time.time()
        for report in reports:
            if not report.ok or not 0 <= now - report.updated_at <= history_module.MAX_READING_AGE:
                continue
            for window in report.windows:
                seen = self.prev_used.setdefault(report.pid, {})
                if (window.used_percent is None or not math.isfinite(window.used_percent)
                        or not 0 <= window.used_percent <= 100 or window.unlimited):
                    seen.pop(window.key, None)
                    continue
                if window.reset_at is not None and (not math.isfinite(window.reset_at) or window.reset_at <= now):
                    continue
                before = seen.get(window.key)
                seen[window.key] = window.used_percent
                key = (report.pid, window.key)
                if (before is not None and window.used_percent <= 0.5
                        and before - window.used_percent >= history_module.RESET_RISE):
                    self.forecast_warned_until.pop(key, None)
                forecast = self.history.forecast(report, window, now=now)
                at_risk = forecast is not None and forecast["before_reset"]
                if before is None:
                    # Existing risk at startup is a quiet baseline, not a new event.
                    if at_risk:
                        self.forecast_warned_until[key] = window.reset_at
                    continue
                if before < self.crit <= window.used_percent:
                    self._show_reminder(reminder_segments(
                        report.name, "%s 额度接近用尽" % window.short, "critical",
                        "已用 %.0f%%" % window.used_percent))
                    return
                if at_risk and now >= self.forecast_warned_until.get(key, 0):
                    self.forecast_warned_until[key] = window.reset_at
                    self._show_reminder(reminder_segments(
                        report.name, "%s 预计撑不到重置" % window.short, "warning",
                        "约 %s 耗尽 · %s 后重置" % (T.hours_text(forecast["hours_to_empty"]),
                                                   T.countdown(window.reset_at, now))))
                    return
            active = self.history.is_active(report.pid)
            if self.was_active.get(report.pid) and not active and report.today_tokens:
                self.was_active[report.pid] = active
                self._show_reminder(reminder_segments(
                    report.name, "一轮回复已完成", "ok",
                    "今日 %s tokens" % model.human_tokens(report.today_tokens)))
                return
            self.was_active[report.pid] = active

    def _reset_reminder(self, report, key: str) -> None:
        self._show_reminder(reminder_segments(report.name, "额度已重置", "ok", key))

    # -- polling ------------------------------------------------------------
    def start_pollers(self, immediate: bool = False) -> None:
        self.poller_stop = threading.Event()
        self.pollers = []
        implementations = providers.implementations()
        stagger = float(self.cfg.get("stagger_seconds", 0.4))
        for index, pid in enumerate(self.enabled):
            fetch = implementations.get(pid)
            if not fetch:
                continue
            delay = 0.05 * index if immediate else stagger * index
            thread = Poller(self.cfg, pid, fetch, self.queue, self.poller_stop, delay)
            thread.start()
            self.pollers.append(thread)

    def restart_pollers(self, immediate: bool = True) -> None:
        if getattr(self, "poller_stop", None):
            self.poller_stop.set()
        self.start_pollers(immediate=immediate)

    def _drain(self) -> None:
        changed = False
        before = tuple(r.pid for r in self._bar_reports())
        while True:
            try:
                agents = self.runtime_queue.get_nowait()
            except queue.Empty:
                break
            known = agents is not None
            agents = set(agents or ()) & set(providers.ORDER)
            if known != self.runtime_known or agents != self.running_agents:
                self.runtime_known = known
                self.running_agents = agents
                log("running agents: %s" % (",".join(sorted(agents)) if known else "unavailable"))
                changed = True
        while True:
            try:
                data = self.month_queue.get_nowait()
            except queue.Empty:
                break
            if data is not None:
                self.month_data = data
                self.dock.month_text = money.usd(data.get("cost_usd")) if data.get("cost_usd") is not None else ""
                changed = True
        readings = tokens.cached()
        if readings is not self.readings:
            self.readings = readings
            self._refresh_month()
            changed = True
        while True:
            try:
                report = self.queue.get_nowait()
            except queue.Empty:
                break
            previous = self.reports.get(report.pid)
            if (not report.ok and report.available and previous is not None
                    and previous.updated_at and (previous.windows or previous.raw_balance is not None)):
                report = replace(previous, ok=False,
                                 note="读取失败，保留上次成功数据 · %s" % (report.note or "稍后重试"))
            self.reports[report.pid] = report
            resets = self.history.observe(report) if report.ok else []
            self.history.save_throttled()
            log("%s ok=%s windows=%d note=%s" % (report.pid, report.ok,
                                                 len(report.windows), report.note or ""))
            changed = True
            if resets:
                self._reset_reminder(report, resets[0])
        if not changed:
            return
        if before != tuple(r.pid for r in self._bar_reports()):
            self.levels.collapse()
            self.month_page = False
            if self.levels.sticky and self._bar_reports():
                self.levels.open_panel(0)
        self._audit(self._bar_reports())
        self._refresh_dock()
        self.peek_sig = self.panel_sig = None
        if self.levels.level != hover.Level.HIDDEN:
            self._apply_level()
        self._update_tray()

    def _refresh_dock(self) -> None:
        reports = self._dock_reports()
        active = {r.pid: self.history.is_active(r.pid) for r in reports}
        self.dock.set_reports(reports, self.warn, self.crit, active)
        self._apply_presentation()
        self._apply_geometry()

    def _update_tray(self) -> None:
        worst = "ok"
        parts = []
        for report in self._bar_reports():
            level = report.worst_level(self.warn, self.crit)
            if T.level_rank(level) > T.level_rank(worst):
                worst = level
            parts.append("%s %s" % (report.name.split(" ")[0], DockBar.slot_text(report)))
        self.tray.setIcon(self._tray_icon(worst))
        status = " · ".join(parts) if parts else ("暂无运行中的 Agent" if self.runtime_known else "运行检测暂不可用")
        self.tray.setToolTip("Agent HUD · " + status)

    def _clock_tick(self) -> None:
        if self.panel.isVisible():
            self.panel.refresh()
        if self._reminder_showing():
            remaining = self.reminder_until - time.time()
            if remaining <= 0:
                self._hide_reminder()

    def _animate(self) -> None:
        if not self.revealed and not self.tuck_anim.running():
            self.anim_timer.stop()
            return
        now = time.monotonic()
        elapsed = min(0.25, now - self._frame_at)
        self._frame_at = now
        active = any(self.history.is_active(pid) for pid in self.enabled)
        self.phase += elapsed / (1.2 if active else 3.6)
        self.dock.advance(now, self.phase, now / 1.2)
        loading = any(slot.loading for pid, slot in self.dock.slots.items() if pid != MONTH_PID)
        interval = (FRAME_ACTIVE_MS if loading or self.dock.busy() or self.tuck_anim.running()
                    else FRAME_IDLE_MS)
        if self.anim_timer.interval() != interval:
            self.anim_timer.setInterval(interval)
        if self.dock.isVisible():
            self.dock.update()

    # -- interaction --------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.press_at = event.globalPosition().toPoint()
            self.press_origin = QPoint(self.x(), self.y())
            self.dragging = False
            event.accept()
        elif event.button() == Qt.RightButton:
            self.menu.popup(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event):
        if self.press_at is None or self.press_origin is None:
            return
        point = event.globalPosition().toPoint()
        delta = point - self.press_at
        if not self.dragging and delta.manhattanLength() < 6:
            return
        self.dragging = True
        self.move(self.press_origin + delta)
        event.accept()

    def mouseReleaseEvent(self, event):
        if self.press_at is None:
            return
        point = event.globalPosition().toPoint()
        if self.dragging:
            screen = QApplication.screenAt(point) or self.screen() or QApplication.primaryScreen()
            frame = screen.geometry() if screen else self.screen_frame()
            self.placement = geo.drag_placement(point, frame,
                                                self.dock.natural_size(), self.placement)
            if screen:
                self.windowHandle().setScreen(screen)
            self._save_placement()
            self.levels.collapse()
            self.levels.last_index = None
            self._apply_direction()
            self._apply_presentation()
        else:
            index = self._dock_index(point)
            if index == len(self._bar_reports()):
                if self.month_page:
                    self.month_page = False
                    self.levels.collapse()
                else:
                    self.show_month()
            elif index >= 0:
                if self.month_page:
                    self.levels.collapse()
                self.month_page = False
                self.panel_sig = None
                self.levels.click(index)
        self.press_at = None
        self.dragging = False
        self._apply_geometry()
        self._apply_level()

    def _save_placement(self) -> None:
        dock_cfg = self.cfg.setdefault("dock", {})
        dock_cfg.update(self.placement.to_dict())
        try:
            config.save(self.cfg)
        except Exception:
            pass

    # -- tray & menu --------------------------------------------------------
    def _tray_icon(self, level: str) -> QIcon:
        artwork = self.windowIcon()
        if not artwork.isNull():
            icon = QIcon()
            for size in (16, 20, 24, 32, 48, 64):
                pixmap = artwork.pixmap(size, size)
                painter = QPainter(pixmap)
                painter.setRenderHint(QPainter.Antialiasing, True)
                pen = painter.pen()
                pen.setWidthF(max(1.0, size / 16))
                pen.setColor(QColor(20, 20, 24))
                painter.setPen(pen)
                painter.setBrush(T.color_for(level))
                diameter = max(4, round(size * 0.3))
                painter.drawEllipse(QRect(size - diameter - 1, size - diameter - 1,
                                          diameter, diameter))
                painter.end()
                icon.addPixmap(pixmap)
            return icon
        size = 32
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(20, 20, 24, 235))
        painter.drawEllipse(QRect(1, 1, size - 2, size - 2))
        pen = painter.pen()
        pen.setWidthF(3.0)
        pen.setCapStyle(Qt.RoundCap)
        pen.setColor(T.color_for(level))
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawArc(QRect(8, 8, size - 16, size - 16), 90 * 16, -270 * 16)
        painter.end()
        return QIcon(pixmap)

    def _build_tray(self) -> None:
        self.menu = QMenu()
        self._fill_menu(self.menu)
        self.tray = QSystemTrayIcon(self._tray_icon("idle"), self)
        self.tray.setToolTip("Agent HUD")
        tray_menu = QMenu()
        self._fill_menu(tray_menu)
        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(self._on_tray)
        self.tray.show()

    def _fill_menu(self, menu: QMenu) -> None:
        menu.setStyleSheet("QMenu { background: #1E1E24; color: #EDEDF0; border: 1px solid #45454F; "
                           "padding: 5px; } QMenu::item { padding: 6px 18px; } "
                           "QMenu::item:selected { background: #393941; } "
                           "QMenu::separator { height: 1px; background: #45454F; margin: 4px; }")
        menu.addAction("立即刷新", self.refresh_now)
        menu.addAction("月度总量", self._open_month_menu)
        menu.addAction("用量统计", self._open_statistics)
        fixed = menu.addAction("固定面板")
        fixed.setCheckable(True)
        fixed.setChecked(self.levels.sticky)
        fixed.toggled.connect(self._set_sticky)
        percent = menu.addAction("显示百分比")
        percent.setCheckable(True)
        percent.setChecked(self.show_percent)
        percent.triggered.connect(self._toggle_percent)
        style_menu = menu.addMenu("圆环 / 流光")
        group = QActionGroup(style_menu)
        for key, label in (("ring", "圆环 Ring"), ("glow", "流光 Glow")):
            action = style_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.style == key)
            action.setActionGroup(group)
            action.triggered.connect(lambda _c=False, k=key: self._set_style(k))
        edge_menu = menu.addMenu("停靠位置")
        edge_group = QActionGroup(edge_menu)
        entries = [("notch", "顶部刘海"), ("top", "上边缘"), ("bottom", "下边缘"),
                   ("left", "左边缘"), ("right", "右边缘")]
        for key, label in entries:
            action = edge_menu.addAction(label)
            action.setCheckable(True)
            current = ("notch" if (self.placement.mode == geo.Mode.NOTCH and key == "notch")
                        else self.placement.edge.value)
            action.setChecked(current == key)
            action.setActionGroup(edge_group)
            action.triggered.connect(lambda _c=False, k=key: self._set_edge(k))
        peek_action = menu.addAction("贴边隐身")
        peek_action.setCheckable(True)
        peek_action.setChecked(self.edge_peek)
        peek_action.triggered.connect(self._toggle_edge_peek)
        menu.addSeparator()
        menu.addAction("打开 config.json", self.open_config)
        menu.addAction("打开日志", self.open_log)
        menu.addSeparator()
        menu.addAction("退出", self.quit)

    def _on_tray(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.setVisible(not self.isVisible())
            if self.isVisible():
                self._reveal(True)

    def refresh_now(self) -> None:
        self.dock.play_flow()
        self.restart_pollers(immediate=True)
        self._refresh_month()

    def _open_month_menu(self) -> None:
        self.show()
        self._reveal(True)
        self.show_month()

    def _open_statistics(self) -> None:
        if self.statistics_window is None:
            self.statistics_window = UsageWindow()
        self.statistics_window.show()
        self.statistics_window.raise_()
        self.statistics_window.activateWindow()

    def _set_sticky(self, checked: bool) -> None:
        self.levels.sticky = bool(checked)
        for menu in (self.menu, self.tray.contextMenu()):
            for action in menu.actions():
                if action.text() == "固定面板":
                    action.setChecked(self.levels.sticky)

    def open_config(self) -> None:
        config.ensure_file()
        os.startfile(str(config.CONFIG_PATH))  # noqa: S606

    def open_log(self) -> None:
        config.LOG_PATH.touch(exist_ok=True)
        os.startfile(str(config.LOG_PATH))  # noqa: S606

    def quit(self) -> None:
        self.history.save()
        (HERE / ".hud-quit").touch()
        self.stop_event.set()
        self.poller_stop.set()
        self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event):
        self.quit()
        event.accept()

    # -- settings -----------------------------------------------------------
    def _toggle_percent(self, checked: bool) -> None:
        self.show_percent = bool(checked)
        self.cfg.setdefault("dock", {})["show_percent"] = self.show_percent
        config.save(self.cfg)
        self._apply_presentation()
        self._apply_geometry()

    def _set_style(self, style: str) -> None:
        self.style = style
        self.cfg.setdefault("dock", {})["style"] = style
        config.save(self.cfg)
        self._apply_presentation()

    def _toggle_edge_peek(self, checked: bool) -> None:
        self.edge_peek = bool(checked)
        self.cfg.setdefault("dock", {})["edge_peek"] = self.edge_peek
        config.save(self.cfg)
        self._reveal(not self.edge_peek)

    def _set_edge(self, key: str) -> None:
        if key == "notch":
            self.placement = geo.Placement(mode=geo.Mode.NOTCH, edge=geo.Edge.TOP,
                                           offset=0.5, inset=0)
        else:
            self.placement = geo.Placement(mode=geo.Mode.DOCK, edge=geo.Edge(key),
                                           offset=self.placement.offset,
                                           inset=self.placement.inset or geo.EDGE_INSET)
        self.levels.collapse()
        self.month_page = False
        self._save_placement()
        self._apply_direction()
        self._apply_presentation()
        self._apply_geometry()
        self._apply_level()


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def enable_dpi_awareness() -> None:
    if not winx.IS_WINDOWS:
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Agent HUD for Windows (PySide6)")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--hidden", action="store_true")
    parser.add_argument("--expand", action="store_true",
                        help="open the panel on the first mark at start")
    parser.add_argument("--peek", type=int, default=None,
                        help="with --expand, the mark to focus")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("only", nargs="*")
    args = parser.parse_args(argv)

    if args.selftest:
        import selftest
        return selftest.main()
    if args.once:
        import check
        reports = check.run(args.only or None)
        print(check.as_json(reports) if args.json else check.as_text(reports))
        return 0

    if not winx.IS_WINDOWS:
        print("This HUD targets Windows.", file=sys.stderr)
        return 1

    enable_dpi_awareness()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Agent HUD")
    app.setApplicationVersion(VERSION)
    app.setWindowIcon(QIcon(str(HERE / "assets" / "agent-hud-icon-v1.png")))
    app.setQuitOnLastWindowClosed(False)
    motion.detect_reduced()

    instance_lock = QLockFile(str(HERE / ".hud.lock"))
    instance_lock.setStaleLockTime(0)
    if not instance_lock.tryLock(0):
        if instance_lock.error() == QLockFile.LockError.LockFailedError:
            log("already running; skipped duplicate launch")
            return 0
        log("could not acquire HUD lock: %s" % instance_lock.error())
        return 1

    families = T.resolve_fonts()
    config.ensure_file()
    cfg = config.load()
    log("started v%s (dock=%s/%s, fonts: %s)"
        % (VERSION, (cfg.get("dock", {}) or {}).get("mode", "dock"),
           (cfg.get("dock", {}) or {}).get("edge", "top"), families))

    marks = T.Marks()
    hud = HudWindow(cfg, marks)
    if args.expand:
        hud._set_sticky(True)
        hud.revealed = True
        hud.hover_timer.setInterval(HOVER_FAST_MS)
        hud.tuck_anim.snap("tuck", 0.0)
    hud.show()
    if args.hidden:
        hud.hide()
    if args.expand:
        hud.levels.open_panel(args.peek if args.peek is not None else 0)
        hud._apply_level()
    code = app.exec()
    instance_lock.unlock()
    log("stopped")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
