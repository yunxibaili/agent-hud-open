"""The dock strip: a compact, docked row or column of agent marks.

One integrated capsule whose corner radius is half its height. Each slot is a
ring drawn with status colours around the real brand artwork, optionally with a
percentage beside it. Two presentations are available:

* ring - the ring fill is the share of the window that is left, swept round with
  a conical gradient and capped with a glowing dot, the way a watch draws it,
* glow - a soft breathing halo behind the mark whose colour is the status level
  and whose speed picks up while that agent has a turn running.

Motion lives here too: the entrance, the staggered sweep onto the first real
reading, the pull of the pointer onto a mark, and the chase that says a refresh
is in flight. Every discrete change is a QVariantAnimation owned by the slot it
belongs to; the slow ambient breath is folded in from the window's frame timer.
"""

from __future__ import annotations

import math
import time

import model
import money
import motion
import ui_theme as T
from PySide6.QtCore import (QEasingCurve, QObject, QPoint, QPointF, QRectF, QSize,
                            Qt, QTimer, QVariantAnimation)
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QFont, QFontMetrics,
                           QPainter, QPainterPath, QPen, QRadialGradient)
from PySide6.QtWidgets import QSizePolicy, QWidget

RING = 28.0
RING_STROKE = 2.5
PAD = 8.5                         # room for the ring's stroke at the capsule edge
SLOT_GAP = 5.0
INNER_GAP = 7.0
HEIGHT = RING + 2 * PAD          # 42, the compact dock's thickness
DOCK_THICKNESS = HEIGHT
NOTCH_RADIUS = 11.0              # the corners that face into the screen

HOVER_SCALE = 1.08               # a mark under the pointer grows by this much
HOVER_DIM = 0.78                 # and the rest step back to this opacity
HOVER_MS = 150
BOOT_MS = 420                    # the capsule unfolding out of the edge
BOOT_SLIDE = 16.0                # the distance it travels on its way in
SLOT_MS = 200                      # a slot arriving, leaving, or sliding home
TINT_MS = 200                      # the ring's colour following a state change
MONTH_PID = "month"                # a stand-in report that draws the month mark
TUCK_VISIBLE = 10.0                # px of the capsule left on screen when tucked
STAGGER_MS = 70                  # the gap between neighbouring slots' sweeps
SWEEP_MS = 650                   # the first sweep onto a real percentage
WAKE_STAGGER_MS = 35             # the much tighter stagger a wake uses
WAKE_SWEEP_MS = 480              # and its shorter, snappier run
TWEEN_MS = 500                   # every later change of that percentage
FLOW_MS = 600                    # the chase that marks a manual refresh
BREATH_MS = 3600.0               # the idle breath, and
BREATH_BUSY_MS = 1200.0          # the quickened one while a turn is running
PEEK_LINE = 3.0                  # the sliver a tucked dock leaves behind


class SlotMotion(QObject):
    """Every animated value of one agent's slot."""

    def __init__(self, parent, changed):
        super().__init__(parent)
        self._changed = changed
        self.scale = 1.0
        self.alpha = 1.0          # fades in on arrival, out on departure
        self.shift = 0.0          # px still to travel into its new place
        self.color_now = None     # the colour actually on screen
        self.color_from = None
        self.color_to = None
        self.tint = 0.0           # 0..1 across the current colour change
        self.leaving = False      # on its way out: drawn, never clickable
        self.sweep = 0.0          # the 0..1 of the ring that is drawn
        self.target = 0.0         # the reading it is on its way to
        self.target = 0.0         # the real share it is heading for
        self.enter = 0.0          # the boot entrance, 0..1
        self.flow = 0.0           # the refresh chase, 0..1
        self.loading = True       # no reading has arrived for this agent yet
        self._anims: dict[str, QVariantAnimation] = {}

    def _anim(self, name: str) -> QVariantAnimation:
        anim = self._anims.get(name)
        if anim is None:
            anim = QVariantAnimation(self)
            anim.valueChanged.connect(lambda value, key=name: self._set(key, value))
            anim.finished.connect(self._changed)
            self._anims[name] = anim
        return anim

    def _set(self, name: str, value) -> None:
        setattr(self, name, float(value))
        self._changed()

    def to(self, name: str, value: float, ms: int, curve) -> None:
        if motion.REDUCED:
            anim = self._anims.get(name)
            if anim is not None:
                anim.stop()          # or its next frame writes the old value back
            self.snap(name, value)
            return
        anim = self._anim(name)
        anim.stop()
        anim.setStartValue(float(getattr(self, name)))
        anim.setEndValue(float(value))
        anim.setDuration(max(1, int(ms)))
        anim.setEasingCurve(curve)
        anim.start()

    def snap(self, name: str, value: float) -> None:
        anim = self._anims.get(name)
        if anim is not None:
            anim.stop()
        self._set(name, value)

    def running(self) -> int:
        from PySide6.QtCore import QAbstractAnimation
        return sum(1 for a in self._anims.values()
                   if a.state() == QAbstractAnimation.Running)


def capsule_path(rect: QRectF, radius: float, square=()) -> QPainterPath:
    """A rounded rect whose named corners are square, for a dock flush with an edge.

    square is any of "tl", "tr", "br", "bl".
    """
    path = QPainterPath()
    r = radius
    left, top, right, bottom = rect.left(), rect.top(), rect.right(), rect.bottom()
    path.moveTo(left + (0 if "tl" in square else r), top)
    path.lineTo(right - (0 if "tr" in square else r), top)
    if "tr" not in square:
        path.quadTo(right, top, right, top + r)
    path.lineTo(right, bottom - (0 if "br" in square else r))
    if "br" not in square:
        path.quadTo(right, bottom, right - r, bottom)
    path.lineTo(left + (0 if "bl" in square else r), bottom)
    if "bl" not in square:
        path.quadTo(left, bottom, left, bottom - r)
    path.lineTo(left, top + (0 if "tl" in square else r))
    if "tl" not in square:
        path.quadTo(left, top, left + r, top)
    path.closeSubpath()
    return path


class DockBar(QWidget):
    def __init__(self, marks: T.Marks, parent=None):
        super().__init__(parent)
        self.marks = marks
        self.reports: list[model.ProviderReport] = []
        self.expected = 5
        self.hover_index = -1
        self.warn = 70.0
        self.crit = 90.0
        self.notch = False
        self.edge = "top"
        self.horizontal = True
        self.show_percent = False
        self.style = "ring"                     # or "glow"
        self.phase = 0.0
        self.active: dict[str, bool] = {}
        self.text_font = T.mono(10.5, QFont.DemiBold)
        self.text_width = max(34, QFontMetrics(self.text_font).horizontalAdvance("100%"))
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self.slots: dict[str, SlotMotion] = {}
        self.order: list[str] = []

        # ambient, refreshed from the window's single frame timer
        self.breath = 0.0                       # 0..1 of the breathing period
        self.spin = 0.0                         # 0..1 of the loading rotation
        # How far the capsule has slid out of sight, and which way.
        self.tuck = 0.0
        self.tuck_sign = QPoint(0, 0)
        self.tuck_offset = 0.0
        self.tuck = 0.0                         # 0 parked, 1 fully tucked away

        # boot
        self.enter = 1.0
        self._boot: QVariantAnimation | None = None
        self._sweeps: list[tuple[float, str, float]] = []
        self._tucked_before = False

        # pointer magnet
        self._magnet = 0.0
        self._magnet_on = -1
        self._magnet_anim: QVariantAnimation | None = None

        # manual-refresh chase
        self._flow = 0.0
        self._flow_anim: QVariantAnimation | None = None

        # Every animated value wants a repaint; one repaint a frame is enough.
        self._repaint_pending = False

    def repaint_soon(self) -> None:
        """Ask for a repaint, at most once per frame.

        A sweep moves four slots at once and the ambient breath nudges every
        one of them; without this each nudge queues its own repaint of the same
        widget in the same frame.
        """
        if self._repaint_pending:
            return
        self._repaint_pending = True
        QTimer.singleShot(0, self._repaint_now)

    def _repaint_now(self) -> None:
        self._repaint_pending = False
        self.update()

    # -- content ------------------------------------------------------------
    def set_reports(self, reports, warn, crit, active: dict[str, bool] | None = None) -> None:
        was = list(getattr(self, "reports", []))
        old_at = {r.pid: i for i, r in enumerate(was)}
        fresh = list(reports)
        new_at = {r.pid: i for i, r in enumerate(fresh)}
        booted = bool(getattr(self, "order", None))

        # An agent that has just gone stays on screen for the length of its
        # fade, in the place it was, so it leaves rather than vanishing. It is
        # drawn but never clickable while it goes.
        stay = list(fresh)
        for report in was:
            if report.pid in new_at:
                continue
            slot = self.slots.get(report.pid)
            if slot is None or not booted:
                continue
            slot.leaving = True
            if report.pid not in [r.pid for r in stay]:
                stay.insert(min(old_at[report.pid], len(stay)), report)
        self.reports = stay
        self.warn = warn
        self.crit = crit
        if active is not None:
            self.active = dict(active)

        seen = []
        for index, report in enumerate(self.reports):
            pid = report.pid
            seen.append(pid)
            slot = self.slots.get(pid)
            if slot is None:
                slot = SlotMotion(self, self.repaint_soon)
                slot.enter = 0.0
                self.slots[pid] = slot
            share = self.share_of(report)
            share = 0.0 if share is None else share
            loading = getattr(report, "updated_at", 0) <= 0
            if slot.loading:
                if not loading:
                    # The first real reading sweeps the ring round to it, each
                    # slot a beat after the one before. A run already queued for
                    # this agent is left alone: a second one would queue behind
                    # the first and the ring would stutter.
                    if not any(row[1] == pid for row in self._sweeps):
                        self._sweeps.append(
                            (time.monotonic() + index * STAGGER_MS / 1000.0, pid,
                             share, SWEEP_MS))
                    slot.target = share
            else:
                slot.target = share
                if abs(slot.sweep - share) > 1e-4:
                    slot.to("sweep", share, TWEEN_MS, motion.IN_OUT_CUBIC)
        # Slots that have moved along the row slide from where they were; slots
        # that have just appeared fade in. Both run on their own Scalar, and
        # because the drawing and the hit test read the same shift, a mark can
        # never be drawn in one place and clickable in another.
        live = [r.pid for r in self.reports if not self.slots.get(r.pid, None)
                or not self.slots[r.pid].leaving]
        run = self.natural_size()
        span = run.width() if self.horizontal else run.height()
        pitch = (span - 2 * PAD) / max(1, len(self.reports))
        for report in self.reports:
            slot = self.slots.get(report.pid)
            if slot is None:
                continue
            here = [r.pid for r in self.reports].index(report.pid)
            if slot.leaving:
                if slot.alpha > 0.99 and booted:
                    slot.to("alpha", 0.0, SLOT_MS, motion.OUT_CUBIC)
                continue
            if booted and report.pid not in old_at:
                slot.alpha = 0.0
                slot.to("alpha", 1.0, SLOT_MS, motion.OUT_CUBIC)
            # kept agents: old index vs the index they hold now
            if booted and report.pid in old_at and here != old_at[report.pid]:
                moved = (old_at[report.pid] - here) * pitch
                if abs(moved) > 0.5:
                    slot.shift = moved
                    slot.to("shift", 0.0, SLOT_MS, motion.OUT_CUBIC)
        self.order = seen
        for pid in list(self.slots):
            if pid not in seen:
                self.slots.pop(pid).deleteLater()
        self._slots_key = None
        self.updateGeometry()
        self.update()

    def set_edge(self, edge: str, notch: bool) -> None:
        self.edge = edge
        self.notch = notch
        self.update()

    def set_presentation(self, horizontal: bool, show_percent: bool, style: str) -> None:
        if horizontal and not show_percent:
            show_percent = False
        self.horizontal = horizontal
        self.show_percent = show_percent and horizontal
        self.style = style
        self.updateGeometry()
        self.update()

    @staticmethod
    def slot_text(report: model.ProviderReport) -> str:
        if report.raw_balance is not None:
            return money.compact(report.raw_balance, report.balance_currency)
        window = report.headline_window()
        if window is None:
            return "--"
        if window.unlimited:
            return "∞"
        if window.used_percent is None:
            return "--"
        return "%.0f%%" % window.used_percent

    def share_of(self, report: model.ProviderReport) -> float | None:
        """How much of the headline window is LEFT, as a 0..1 share of the ring.

        The ring is a gauge of what is still in the pot, so a nearly untouched
        window draws a nearly full circle and a spent one draws almost nothing.
        The hover line and the cards show what is left too, so one reading
        runs the whole way through: ring, figure and card all say the same
        thing. Only the all-agents total reports usage rather than what is
        left, because that card is about how much was spent.
        """
        window = report.headline_window()
        if window is None:
            return None
        if window.unlimited:
            return 1.0
        if window.used_percent is None:
            return None
        return max(0.0, min(1.0, (100.0 - window.used_percent) / 100.0))

    def slot_percent(self, report, slot) -> str | None:
        """The figure beside a mark, counted up as its ring sweeps.

        Only a real percentage counts; a balance is a running total, not a
        dial, so it is printed as it stands.
        """
        window = report.headline_window()
        if window is None or window.unlimited or window.used_percent is None:
            return None
        left = max(0.0, min(100.0, 100.0 - window.used_percent))
        target = self.share_of(report)
        if not target:
            return "%.0f%%" % 0.0
        shown = left * (max(0.0, min(1.0, slot.sweep)) / target)
        return "%.0f%%" % max(0.0, min(left, shown))

    def remaining_text(self, report: model.ProviderReport) -> str:
        if report.pid == MONTH_PID:
            # The month mark is a button, but it is not silent: the figure beside
            # it is what the page behind it says, kept on the bar by the hud so
            # the dock never has to reach into the token module itself.
            return getattr(self, "month_text", "") or ""
        """The figure beside a mark: how much of the window is left.

        It matches the ring it sits next to, so the number and the arc never
        disagree. An agent with no quota window at all - DSH reports a balance,
        WorkBuddy sometimes only a day's spend - falls back to what it does have
        rather than printing a dash that says nothing.
        """
        if report.raw_balance is not None:
            return money.compact(report.raw_balance, report.balance_currency)
        window = report.headline_window()
        if window is not None:
            if window.unlimited:
                return "∞"
            if window.used_percent is not None:
                return "%.0f%%" % (100.0 - window.used_percent)
        cost = getattr(report, "today_cost", None)
        if cost:
            return money.usd(cost, getattr(report, "cost_currency", None) or "CNY", 0)
        balance = getattr(report, "balance", None)
        if balance:
            return balance
        today = getattr(report, "today_tokens", None)
        if today:
            return model.human_tokens(today)
        return "--"

    # -- motion --------------------------------------------------------------
    def play_boot(self) -> None:
        """Unfold the capsule out of the edge the dock is parked on."""
        self.enter = 0.0
        boot = QVariantAnimation(self)
        boot.setStartValue(0.0)
        boot.setEndValue(1.0)
        boot.setDuration(BOOT_MS)
        boot.setEasingCurve(motion.OUT_BACK)
        boot.valueChanged.connect(self._boot_step)
        boot.finished.connect(self._boot_done)
        self._boot = boot
        boot.start()

    def _boot_step(self, value) -> None:
        self.enter = float(value)
        self.update()

    def _boot_done(self) -> None:
        self.enter = 1.0
        for slot in self.slots.values():
            slot.enter = 1.0
        self.update()

    def wake_sweep(self) -> None:
        """Every ring runs up from nothing, clockwise, as the dock comes out.

        This is the hidden -> visible edge only. A dock that is already out just
        leaves its rings where they are, so sliding the pointer from mark to
        mark never restarts them; the caller is responsible for that, and the
        reveal path already returns early when the state has not changed.
        """
        now = time.monotonic()
        self._sweeps.clear()          # a wake supersedes any pending first sweep
        beat = 0
        for pid in self.order:
            slot = self.slots.get(pid)
            share = self.share_of_report(pid)
            if slot is None or share is None:
                continue              # nothing to draw: no window at all
            # A ring that never left its loading comet is settled into the run
            # too: waking must show every figure counting up, not one comet that
            # waits for a poll before it will move.
            slot.loading = False
            slot.target = share
            slot.snap("sweep", 0.0)   # visibly reset before it runs up
            # The beat counts the marks that are actually on the strip, so an
            # absent agent does not leave a gap in the run.
            self._sweeps.append((now + beat * WAKE_STAGGER_MS / 1000.0, pid,
                                 share, WAKE_SWEEP_MS))
            beat += 1
        self._sweeps.sort(key=lambda row: row[0])
        self.update()

    def share_of_report(self, pid: str) -> float | None:
        for report in self.reports:
            if report.pid == pid:
                return self.share_of(report)
        return None

    def set_hover(self, index: int) -> None:
        """Pull the mark under the pointer out and let the rest step back."""
        if index == self._magnet_on:
            return
        self._magnet_on = index
        self._magnet_anim = self._tween(self._magnet_anim, self._magnet,
                                        1.0 if index >= 0 else 0.0,
                                        HOVER_MS, motion.OUT_QUAD, self._set_magnet)

    def _set_magnet(self, value) -> None:
        self._magnet = float(value)
        self.update()

    def play_flow(self) -> None:
        """One clockwise chase round every ring: a refresh is in flight."""
        self._flow_anim = self._tween(self._flow_anim, 0.0, 1.0, FLOW_MS,
                                      motion.LINEAR, self._set_flow)

    def _set_flow(self, value) -> None:
        self._flow = float(value)
        for slot in self.slots.values():
            slot.flow = self._flow
        self.update()

    @staticmethod
    def _tween(existing, start, end, ms, curve, on_value) -> QVariantAnimation:
        if existing is not None:
            existing.stop()
        anim = QVariantAnimation()
        anim.setStartValue(float(start))
        anim.setEndValue(float(end))
        anim.setDuration(max(1, int(ms)))
        anim.setEasingCurve(curve)
        anim.valueChanged.connect(on_value)
        anim.finished.connect(lambda: on_value(end))
        anim.start()
        return anim

    def set_tuck(self, value: float, sign: QPoint) -> None:
        """How far the capsule is drawn outside the screen edge.

        The window does not move for this. Sliding the window out meant asking
        the screen clamp for permission, and the clamp does not grant more than
        the margin the window keeps around its content - so the capsule stopped
        just short of the edge and stayed there in full view. Painting it out of
        the edge instead always works, and the window's own rectangle clips it.
        """
        self.tuck = max(0.0, min(1.0, float(value)))
        self.tuck_sign = QPoint(sign)
        span = max(0.0, HEIGHT - TUCK_VISIBLE)
        self.tuck_offset = span * self.tuck
        if self.tuck > 0.5 and not self._tucked_before:
            self._tucked_before = True
            self.update()
        elif self.tuck <= 0.5:
            self._tucked_before = False

    def set_tucked(self, fraction: float) -> None:
        """0 parked, 1 slid away; the sliver it leaves is drawn as a glow line."""
        if abs(self.tuck - fraction) < 0.002:
            return
        self.tuck = max(0.0, min(1.0, fraction))
        self.update()

    def advance(self, now: float, breath: float, spin: float) -> None:
        """Fold the ambient values in and fire any sweep that has come due."""
        self.breath = breath
        self.spin = spin
        for report in list(self.reports):
            slot = self.slots.get(report.pid)
            if slot is not None and slot.leaving and slot.alpha <= 0.02:
                self.reports = [r for r in self.reports if r.pid != report.pid]
                self.slots.pop(report.pid, None)
                self.order = [p for p in self.order if p != report.pid]
        fired = False
        while self._sweeps and now >= self._sweeps[0][0]:
            _due, pid, share, ms = self._sweeps.pop(0)
            slot = self.slots.get(pid)
            if slot is not None:
                slot.loading = False
                slot.to("sweep", share, ms, motion.OUT_CUBIC)
            fired = True
        if fired:
            self.update()

    def idle_breath(self) -> float:
        """The 0..1 sine the ambient glow rides on."""
        return 0.5 + 0.5 * math.sin(self.breath * 2.0 * math.pi)

    def busy(self) -> bool:
        """Something is moving and wants frames at the fast rate."""
        if self._sweeps:
            return True
        count = sum(slot.running() for slot in self.slots.values())
        for anim in (self._boot, self._magnet_anim, self._flow_anim):
            if anim is not None:
                from PySide6.QtCore import QAbstractAnimation
                if anim.state() == QAbstractAnimation.Running:
                    count += 1
        return count > 0

    # -- geometry -----------------------------------------------------------
    def slot_length(self) -> float:
        base = RING + 2 * (SLOT_GAP / 2)
        if self.show_percent:
            base += INNER_GAP + self.text_width
        return base

    def hover_hot(self, ring: QRectF, text: QRectF) -> QRectF:
        hot = QRectF(ring)
        if not text.isEmpty():
            hot = hot.united(QRectF(text))
        return hot.adjusted(-SLOT_GAP, -PAD / 2, SLOT_GAP, PAD / 2)

    def natural_size(self) -> QSize:
        count = max(len(self.reports), self.expected)
        run = 2 * PAD + count * self.slot_length() + max(0, count - 1) * SLOT_GAP
        thick = HEIGHT
        if self.horizontal:
            return QSize(int(round(run)), int(round(thick)))
        return QSize(int(round(thick)), int(round(run)))

    def grow_scale(self) -> float:
        """How much of its full size the strip is drawn at while it unfolds."""
        return 1.0 if self.enter >= 0.999 else (0.55 + 0.45 * self.enter)

    def _slots(self):
        """(report, ring rect, text rect) for every mark.

        The layout is cached: the hover tick asks for it several times a frame
        and the answer only changes when the size, the count or the boot does.
        """
        count = len(self.reports)
        key = (count, self.width(), self.height(), self.show_percent,
               self.horizontal, round(self.grow_scale(), 2))
        if getattr(self, "_slots_key", None) == key:
            return self._slots_cache
        out = []
        if not count:
            self._slots_key, self._slots_cache = key, out
            return out
        grow = self.grow_scale()
        ring_size = RING * grow
        slot_len = self.slot_length() * grow
        gap = SLOT_GAP * grow
        run = 2 * PAD + count * slot_len + max(0, count - 1) * gap
        if self.horizontal:
            start = (self.width() - run) / 2.0 + PAD
        else:
            start = (self.height() - run) / 2.0 + PAD
        for index, report in enumerate(self.reports):
            offset = start + index * (slot_len + gap)
            if self.horizontal:
                cy = self.height() / 2.0
                ring = QRectF(offset + gap / 2, cy - ring_size / 2, ring_size, ring_size)
                # The percentage is off by default: the strip is then just the
                # marks, and half the width.
                text = (QRectF(ring.right() + INNER_GAP, ring.top(),
                               self.text_width * grow, ring_size)
                        if self.show_percent else QRectF())
            else:
                cx = self.width() / 2.0
                ring = QRectF(cx - ring_size / 2, offset + gap / 2, ring_size, ring_size)
                text = QRectF()
            out.append((report, ring, text))
        self._slots_key, self._slots_cache = key, out
        return out

    def _slot_delta(self, report) -> QPoint:
        """Where a slot is drawn, relative to where it is laid out."""
        slot = self.slots.get(report.pid)
        if slot is None or abs(slot.shift) < 0.5:
            return QPoint(0, 0)
        step = int(round(slot.shift))
        return QPoint(step, 0) if self.horizontal else QPoint(0, step)

    def index_at(self, point: QPoint) -> int:
        """The mark under the pointer, or -1.

        Tested against the same shift the painter applies, so a mark that is on
        its way somewhere is clicked where it is drawn and not where it is
        going; and a mark that is on its way out is not clickable at all, since
        acting on an agent that has already gone is never what was meant.
        """
        for index, (report, ring, text) in enumerate(self._slots()):
            slot = self.slots.get(report.pid)
            if slot is None or slot.leaving or slot.alpha < 0.35:
                continue
            delta = self._slot_delta(report)
            probe = QPointF(point)
            if delta.x() or delta.y():
                probe = QPointF(point.x() - delta.x(), point.y() - delta.y())
            if self.hover_hot(ring, text).contains(probe):
                return index
        return -1

    # -- painting -----------------------------------------------------------
    def paintEvent(self, _event):
        painter = QPainter(self)
        if self.tuck_offset > 0.0:
            # Only what is left on screen is the capsule's inner edge, so the
            # coloured status line is drawn there rather than on the side that
            # has slid out of sight.
            painter.translate(self.tuck_sign.x() * self.tuck_offset,
                              self.tuck_sign.y() * self.tuck_offset)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing
                               | QPainter.SmoothPixmapTransform, True)
        dpr = self.devicePixelRatioF() or 1.0
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = min(rect.width(), rect.height()) / 2.0

        # The boot: the capsule unfolds out of the edge it is parked on, so it
        # starts narrower and a little further out, and fades up as it lands.
        enter = self.enter
        if enter < 0.999:
            grown = 0.55 + 0.45 * enter
            if self.horizontal:
                width = rect.width() * grown
                rect = QRectF(rect.center().x() - width / 2.0, rect.top(), width,
                              rect.height())
            else:
                height = rect.height() * grown
                rect = QRectF(rect.left(), rect.center().y() - height / 2.0,
                              rect.width(), height)
            slide = BOOT_SLIDE * (1.0 - enter)
            if self.edge == "top":
                rect.translate(0, -slide)
            elif self.edge == "bottom":
                rect.translate(0, slide)
            elif self.edge == "left":
                rect.translate(-slide, 0)
            else:
                rect.translate(slide, 0)
            painter.setOpacity(max(0.0, min(1.0, 0.35 + 0.65 * enter)))
            radius = min(rect.width(), rect.height()) / 2.0

        square = ()
        if self.notch and self.edge == "top":
            radius = NOTCH_RADIUS
            square = ("tl", "tr")
            thick = getattr(self, "edge_slack", 0)
            if thick:
                rect = QRectF(rect.left(), rect.top() - thick, rect.width(),
                              rect.height() + thick)
        path = capsule_path(rect, radius, square)
        painter.setPen(Qt.NoPen)
        painter.setBrush(T.DOCK_BG)
        painter.drawPath(path)

        painter.save()
        painter.setClipPath(path)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QColor(255, 255, 255, 26))
        painter.drawPath(path)
        inset = rect.adjusted(1, 1, -1, -1)
        painter.setPen(QColor(255, 255, 255, 10))
        painter.drawRoundedRect(inset, max(0.0, radius - 1), max(0.0, radius - 1))
        # a hairline of status colour along the edge the dock is parked on
        self._edge_line(painter, rect)

        # The clip stays on for the marks as well: while the capsule unfolds it
        # is narrower than its final size, and a mark drawn outside it would
        # float over the desktop unattached to anything.
        magnet = self._magnet
        for index, (report, ring, text) in enumerate(self._slots()):
            slot_hint = self.slots.get(report.pid)
            if slot_hint is not None and slot_hint.leaving:
                pass          # drawn, fading, and never highlighted
            elif index == self.hover_index:
                self._draw_hover_glow(painter, report, ring, text)
            pulled = index == self._magnet_on
            scale = (1.0 + (HOVER_SCALE - 1.0) * magnet) if pulled \
                else (1.0 - (1.0 - HOVER_DIM) * magnet)
            painter.save()
            delta = self._slot_delta(report)
            if delta.x() or delta.y():
                painter.translate(delta.x(), delta.y())
            slot_now = self.slots.get(report.pid)
            fade = 1.0 if slot_now is None else max(0.0, min(1.0, slot_now.alpha))
            painter.setOpacity((1.0 if pulled
                                else 1.0 - (1.0 - HOVER_DIM) * magnet) * fade)
            if pulled and magnet > 0.0:
                centre = ring.center()
                painter.translate(centre)
                painter.scale(scale, scale)
                painter.translate(-centre)
            self._draw_slot(painter, report, ring, text, dpr)
            painter.restore()

        painter.restore()
        painter.end()

    def _draw_hover_glow(self, painter: QPainter, report, ring: QRectF,
                         text: QRectF) -> None:
        """The mark under the pointer answers with its own colour, softly.

        A flat grey block read as a selection box pasted over the mark; a radial
        wash of the agent's status colour reads as the mark lighting up.
        """
        hot = self.hover_hot(ring, text)
        tint = T.color_for(report.worst_level(self.warn, self.crit))
        radius = max(hot.width(), hot.height()) * 0.62
        gradient = QRadialGradient(hot.center(), max(1.0, radius))
        inner = QColor(tint)
        inner.setAlpha(42)
        mid = QColor(tint)
        mid.setAlpha(15)
        outer = QColor(tint)
        outer.setAlpha(0)
        gradient.setColorAt(0.0, inner)
        gradient.setColorAt(0.55, mid)
        gradient.setColorAt(1.0, outer)
        painter.setPen(Qt.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(hot, hot.height() / 2, hot.height() / 2)

    def _edge_line(self, painter: QPainter, rect: QRectF) -> None:
        edge = getattr(self, "edge", "top")
        if edge == "top":
            line = QRectF(rect.left() + 10, rect.top() + 0.5, rect.width() - 20, 1.4)
        elif edge == "bottom":
            line = QRectF(rect.left() + 10, rect.bottom() - 1.9, rect.width() - 20, 1.4)
        elif edge == "left":
            line = QRectF(rect.left() + 0.5, rect.top() + 10, 1.4, rect.height() - 20)
        else:
            line = QRectF(rect.right() - 1.9, rect.top() + 10, 1.4, rect.height() - 20)
        colors = [T.color_for(r.worst_level(self.warn, self.crit)) for r in self.reports[:4]]
        if not colors:
            return
        # The hairline breathes with the rest of the dock, slowly when nothing
        # is running and faster while a turn is in flight.
        pulse = self.idle_breath()
        alpha = int(70 + 70 * pulse)
        vertical = line.height() > line.width()
        for index, color in enumerate(colors):
            if vertical:
                # A dock on a side edge runs its colours down the line, not
                # across a strip that is only 1.4px wide.
                step = line.height() / len(colors)
                piece = QRectF(line.left(), line.top() + index * step,
                               line.width(), step)
            else:
                step = line.width() / len(colors)
                piece = QRectF(line.left() + index * step, line.top(),
                               step, line.height())
            glow = QColor(color)
            glow.setAlpha(alpha)
            painter.setPen(Qt.NoPen)
            painter.setBrush(glow)
            painter.drawRoundedRect(piece, 0.7, 0.7)

    def _tinted(self, slot, target: QColor) -> QColor:
        """The ring's colour, easing into a new state instead of snapping.

        Eased from the colour actually on screen, so a state that changes twice
        in quick succession carries on from where the eye last saw it. The
        decision is not delayed by any of this: worst_level() has already run and
        the reading, the wording and the alert are all settled - only the ink
        waits.
        """
        target = QColor(target)
        if slot is None or motion.REDUCED:
            if slot is not None:
                slot.color_now = slot.color_from = slot.color_to = target
            return target
        if slot.color_now is None:
            slot.color_now = slot.color_from = slot.color_to = target
            return slot.color_now
        if slot.color_to != target:
            slot.color_from = QColor(slot.color_now)
            slot.color_to = target
            slot.snap("tint", 0.0)
            slot.to("tint", 1.0, TINT_MS, motion.IN_OUT_SINE)
        a, b = slot.color_from, slot.color_to
        if a is None or b is None:
            return slot.color_now
        f = max(0.0, min(1.0, getattr(slot, "tint", 0.0)))
        slot.color_now = QColor(
            int(round(a.red() + (b.red() - a.red()) * f)),
            int(round(a.green() + (b.green() - a.green()) * f)),
            int(round(a.blue() + (b.blue() - a.blue()) * f)))
        return slot.color_now

    def _draw_slot(self, painter: QPainter, report, ring: QRectF, text: QRectF,
                   dpr: float) -> None:
        slot = self.slots.get(report.pid)
        level = report.worst_level(self.warn, self.crit)
        color = self._tinted(slot, T.color_for(level))
        running = bool(self.active.get(report.pid))
        pulse = self.idle_breath()

        if slot is None:
            slot = SlotMotion(self, self.repaint_soon)
            slot.sweep = self.share_of(report) or 0.0
        if report.pid == MONTH_PID:
            # It has no quota, so it gets no ring and no comet: a quiet circle
            # and its glyph, which reads as a button rather than as a gauge.
            self._draw_track(painter, ring)
        elif slot.loading:
            self._draw_track(painter, ring)
            self._draw_comet(painter, ring, color)
        elif self.style == "glow":
            self._draw_glow(painter, ring, color, slot.sweep, pulse, running)
        else:
            self._draw_ring(painter, ring, color, slot, pulse, running, report)

        pixmap = self.marks.pixmap(report.pid, int(RING * 0.60), dpr)
        if pixmap is not None:
            size = pixmap.width() / (pixmap.devicePixelRatio() or 1.0)
            if running:
                size *= 1.0 + 0.045 * pulse
            painter.drawPixmap(QPoint(int(round(ring.center().x() - size / 2)),
                                      int(round(ring.center().y() - size / 2))), pixmap)
        else:
            painter.setPen(color)
            painter.setFont(T.title(9.5, QFont.Bold))
            painter.drawText(ring, int(Qt.AlignCenter), report.mark)

        if not text.isEmpty():
            painter.setFont(self.text_font)
            painter.setPen(color if report.ok else T.IDLE)
            label = self.slot_percent(report, slot)
            painter.drawText(text, int(Qt.AlignLeft | Qt.AlignVCenter),
                             label if label is not None
                             else self.remaining_text(report))

    @staticmethod
    def _use_gradient_pen(painter: QPainter, gradient, width: float = RING_STROKE) -> None:
        """Stroke with a gradient: an arc is drawn by the pen, not the brush."""
        pen = QPen()
        pen.setWidthF(width)
        pen.setCapStyle(Qt.RoundCap)
        pen.setBrush(QBrush(gradient))
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

    def _draw_track(self, painter: QPainter, ring: QRectF) -> None:
        pen = painter.pen()
        pen.setWidthF(RING_STROKE)
        pen.setCapStyle(Qt.RoundCap)
        pen.setColor(T.DOCK_TRACK)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawArc(ring, 0, 360 * 16)

    def _conical(self, ring: QRectF, color: QColor, share: float) -> QConicalGradient:
        """A gradient that runs from a dim start to a bright end, clockwise.

        The arc is drawn from twelve o'clock going clockwise, which in conical
        space (which runs anticlockwise from its angle) is the band from
        1 - share up to 1. Putting the dim stop at 1 - share and the bright one
        at 0 - the same place as 1, where the arc ends - makes the ring glow
        into its own head.
        """
        gradient = QConicalGradient(ring.center(), 90.0)
        bright = QColor(color)
        if share >= 0.999:
            gradient.setColorAt(0.0, bright)
            gradient.setColorAt(1.0, bright)
            return gradient
        dim = QColor(color)
        dim.setHsv(dim.hue(), max(0, int(dim.saturation() * 0.94)),
                   max(0, int(dim.value() * 0.72)), bright.alpha())
        # The arc is drawn from twelve o'clock clockwise, so it runs through
        # conical space from 1 at its tail down to 1 - share at its head. The
        # dim stop sits at the tail and the bright one at the head, which makes
        # the ring light up into its own tip the way a watch ring does.
        gradient.setColorAt(max(0.001, 1.0 - share), bright)
        gradient.setColorAt(0.0, dim)
        return gradient

    def _draw_ring(self, painter: QPainter, ring: QRectF, color: QColor, slot,
                   pulse: float, running: bool, report=None) -> None:
        self._draw_track(painter, ring)
        share = max(0.0, min(1.0, slot.sweep))
        if share <= 0.004 and report is not None:
            # A ring that is simply sitting at its reading must draw that
            # reading. Left at zero it draws a zero-length arc, and a zero-length
            # arc with a round cap is a single dot at the top of the mark - which
            # is exactly what a settled dock was showing.
            settled = not self._sweeps and not self.active.get(report.pid)
            if settled:
                share = max(0.0, min(1.0, self.share_of(report) or 0.0))
        if share > 0.004:
            arc = QColor(color)
            if running:
                arc.setAlpha(int(150 + 105 * pulse))
            self._use_gradient_pen(painter, self._conical(ring, arc, share))
            painter.drawArc(ring, 90 * 16, -int(round(360 * 16 * share)))
        if slot.flow > 0.0:
            self._flow_arc(painter, ring, color, slot.flow)

    def _flow_arc(self, painter: QPainter, ring: QRectF, color: QColor,
                  progress: float) -> None:
        """A highlight chasing clockwise: the refresh says it is working."""
        alpha = int(170 * math.sin(math.pi * max(0.0, min(1.0, progress))))
        if alpha <= 3:
            return
        head = 90.0 - 360.0 * progress
        span = 74.0
        gradient = QConicalGradient(ring.center(), head - span)
        clear = QColor(color)
        clear.setAlpha(0)
        mid = QColor(color)
        mid.setAlpha(alpha // 3)
        bright = QColor(255, 255, 255)
        bright.setAlpha(alpha)
        gradient.setColorAt(0.0, clear)
        gradient.setColorAt(0.14, mid)
        gradient.setColorAt(0.205, bright)
        gradient.setColorAt(0.21, clear)
        gradient.setColorAt(1.0, clear)
        self._use_gradient_pen(painter, gradient)
        painter.drawArc(ring, int(head * 16), -int(span * 16))

    def _draw_comet(self, painter: QPainter, ring: QRectF, color: QColor) -> None:
        """A ninety degree arc with a fading tail, turning while data is due.

        The head starts at twelve o'clock and turns clockwise: the same point
        and the same direction as the progress arc it hands over to, so the
        loading state does not jump when the figure arrives.
        """
        head = (90.0 - 360.0 * (self.spin % 1.0)) % 360.0
        span = 90.0
        gradient = QConicalGradient(ring.center(), head - span)
        clear = QColor(color)
        clear.setAlpha(0)
        mid = QColor(color)
        mid.setAlpha(120)
        bright = QColor(color)
        bright.setAlpha(235)
        gradient.setColorAt(0.0, clear)
        gradient.setColorAt(0.10, mid)
        gradient.setColorAt(0.245, bright)
        gradient.setColorAt(0.25, bright)
        gradient.setColorAt(1.0, clear)
        self._use_gradient_pen(painter, gradient)
        painter.drawArc(ring, int(head * 16), -int(span * 16))

    def _draw_glow(self, painter: QPainter, ring: QRectF, color: QColor, share,
                   pulse: float, running: bool) -> None:
        strength = 0.35 if share is None else max(0.22, share)
        centre = ring.center()
        radius = ring.width() * (0.78 + 0.10 * pulse * (1.6 if running else 1.0))
        gradient = QRadialGradient(centre, radius)
        inner = QColor(color)
        inner.setAlpha(int(255 * strength * (0.45 + 0.55 * pulse)))
        mid = QColor(color)
        mid.setAlpha(int(70 * strength))
        outer = QColor(color)
        outer.setAlpha(0)
        gradient.setColorAt(0.0, inner)
        gradient.setColorAt(0.55, mid)
        gradient.setColorAt(1.0, outer)
        painter.setPen(Qt.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(centre, radius, radius)
        pen = painter.pen()
        pen.setWidthF(1.4)
        pen.setColor(QColor(color.red(), color.green(), color.blue(),
                            int(150 + 80 * pulse)))
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(ring)
        if share is not None and share > 0.004:
            # even in glow mode the show of progress is worth a thin ring
            arc = QColor(color)
            arc.setAlpha(int(120 + 80 * pulse))
            self._use_gradient_pen(painter, self._conical(ring, arc, share))
            painter.drawArc(ring, 90 * 16, -int(round(360 * 16 * share)))
