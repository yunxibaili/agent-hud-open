"""The dock's one-line answers: the hover peek and the reminder pill.

Both are the same shape: a capsule as thick as the compact dock that slides out
of the strip on its inward side, carrying one row of text. The hover peek names
one agent and its windows; the reminder announces something that just happened.

The line morphs rather than being rebuilt. Sliding from one mark to the next
must not blink the capsule shut and open again, so the widget keeps its identity
and only the width it draws and the text inside it change, tweened so the
capsule stretches from one label's length to the other's.
"""

from __future__ import annotations

import model
import motion
import ui_theme as T
from PySide6.QtCore import QPoint, QTimer, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import QSizePolicy, QWidget

PAD_H = 13.0
PAIR_GAP = 5.0
SEG_GAP = 12.0
DOT = 7.0
MORPH_MS = 140
# The line arrives and leaves rather than switching on and off. Both are short
# enough to feel immediate, and both start from wherever the last one got to, so
# a pointer moving back and forth across the marks never sees a jump.
APPEAR_MS = 150
APPEAR_SHIFT = 6.0
VANISH_MS = 110
VANISH_SHIFT = 3.0


class Segment:
    __slots__ = ("text", "font", "color", "kind", "_width")

    def __init__(self, text: str, font: QFont, color: QColor, kind: str = "text"):
        self.text = text
        self.font = font
        self.color = color
        self.kind = kind
        # Measured once: the same label is asked for its width on every paint
        # and building a QFontMetrics each time is the expensive part.
        self._width = (DOT if kind == "dot"
                       else QFontMetrics(font).horizontalAdvance(text))

    def width(self, dot_gap: float = 0.0) -> float:
        return self._width


class LinePill(QWidget):
    """A single row of segments in a capsule as thick as the dock."""

    def __init__(self, height: int = 42, parent=None):
        super().__init__(parent)
        self.segments: list[Segment] = []
        self.pad = PAD_H
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.background = T.DOCK_BG
        self.edge_color = T.DOCK_EDGE
        # The width the capsule is drawn at, which during a morph is still on
        # its way to the width the widget actually occupies, and the opacity of
        # the words inside it. Both ride on one reusable pair of animations.
        self.anim = motion.Scalar(self, self.update,
                              ("drawn", "text", "appear", "shift"))
        self.breathe = 0.0
        self.closing = False       # fading out: not interactive, not a hit
        self._toward = QPoint(0, 0)

    def fade_in(self, toward: QPoint) -> None:
        """Arrive: fade up while sliding the last few pixels out of the dock.

        Interruptible by construction - the animation starts at whatever the
        current opacity and offset are, so being asked to leave halfway through
        arriving carries on from there instead of snapping.
        """
        self._toward = QPoint(toward)
        # Idempotent on purpose. Callers poll - this is asked for on every hover
        # tick, sixty milliseconds apart - and restarting a 150ms animation every
        # 60ms means it never reaches the end: the fade stalls part way and stays
        # there. Asking for what is already true must do nothing.
        if self.isVisible() and not self.closing and self.anim.value("appear") >= 0.99:
            return
        self.closing = False
        if not self.isVisible():
            self.anim.snap("appear", 0.0)
            self.anim.snap("shift", APPEAR_SHIFT)
        self.setVisible(True)
        self.anim.to("appear", 1.0, APPEAR_MS, motion.OUT_QUART)
        self.anim.to("shift", 0.0, APPEAR_MS, motion.OUT_QUART)

    def fade_out(self, toward: QPoint) -> None:
        """Leave, and only then stop being a widget."""
        if not self.isVisible() or self.closing:
            return          # already leaving; polling must not restart it
        self.closing = True
        self._toward = QPoint(toward)
        self.anim.to("appear", 0.0, VANISH_MS, motion.IN_CUBIC)
        self.anim.to("shift", VANISH_SHIFT, VANISH_MS, motion.IN_CUBIC)
        QTimer.singleShot(VANISH_MS + 10, self._settle)

    def _settle(self) -> None:
        """Hide once the fade has really finished, unless it was called off."""
        if self.closing and self.anim.value("appear") <= 0.01:
            self.closing = False
            self.setVisible(False)

    def set_segments(self, segments: list[Segment], animate: bool = True) -> None:
        """Swap the row's content, morphing the capsule across if it can.

        The widget takes its new width at once - the window would otherwise be
        resized on every frame - and the capsule is drawn at an animated width
        inside it, centred, which reads as the same capsule stretching from one
        label to the other rather than blinking shut and reopening.
        """
        was = self.anim.value("drawn") or float(self.width())
        self.segments = segments
        target = float(self.natural_width())
        # Two pixels of slack on each side. The capsule is painted at its own
        # natural width and centred in this widget, so the text never has to
        # reach the widget's edge - without the slack the last glyph of a long
        # line was cut by the widget boundary, mid-character and with no
        # ellipsis to explain it.
        self.setFixedWidth(int(round(target)) + 16)
        if not animate or was <= 0 or abs(was - target) < 1.5:
            self.anim.snap("drawn", target)
            self.anim.snap("text", 1.0)
            return
        self.anim.set("drawn", was)          # start where the old capsule was
        self.anim.to("drawn", target, MORPH_MS, motion.OUT_QUART)
        # the words fade back in over the stretch
        self.anim.snap("text", 0.2)
        self.anim.to("text", 1.0, int(MORPH_MS * 0.85), motion.OUT_QUAD)

    @property
    def drawn(self) -> float:
        return self.anim.value("drawn") or float(self.width())

    @property
    def text_alpha(self) -> float:
        return self.anim.value("text") or 1.0

    def natural_width(self) -> int:
        if not self.segments:
            return int(2 * self.pad)
        total = sum(s.width() for s in self.segments)
        total += SEG_GAP * (len(self.segments) - 1)
        # Six pixels of slack: the capsule is drawn one pixel inside its rect
        # and the text one pad inside that, and without the slack a label that
        # fits exactly in theory came out as an ellipsis in practice.
        width = max(150, total + 2 * self.pad + 6)
        screen = self.screen()
        if screen is not None:
            width = min(width, screen.availableGeometry().width() - 24)
        return int(round(width))

    def painted_width(self) -> float:
        """The width the capsule is actually drawn at, this frame.

        Never narrower than the label it is carrying. Growing is therefore
        instant - a capsule that stretched into a longer name would show that
        name as an ellipsis for the length of the stretch, which is exactly the
        one thing the line must not do. Shrinking still animates, because a
        wider capsule around a shorter name is harmless.
        """
        return min(float(self.width()),
                   max(self.drawn or float(self.width()),
                       float(self.natural_width())))

    def set_breathe(self, value: float) -> None:
        self.breathe = value
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        appear = max(0.0, min(1.0, self.anim.value("appear")))
        if appear < 0.999:
            painter.setOpacity(appear)
        shift = self.anim.value("shift")
        if shift:
            painter.translate(self._toward.x() * shift, self._toward.y() * shift)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing, True)
        width = self.painted_width()
        left = (self.width() - width) / 2.0
        rect = QRectF(left + 0.5, 0.5, max(4.0, width - 1.0), self.height() - 1.0)
        radius = rect.height() / 2.0
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self.background)
        painter.drawPath(path)

        # The capsule's edge carries a slow glow, the same breath the dock has.
        pulse = 0.5 + 0.5 * (self.breathe * 2.0 * 3.141592653589793 % 1.0)
        edge = QColor(self.edge_color)
        edge.setAlpha(int(self.edge_color.alpha() * (0.75 + 0.45 * pulse)))
        painter.setBrush(Qt.NoBrush)
        painter.setPen(edge)
        painter.drawPath(path)

        painter.save()
        painter.setClipPath(path)
        painter.setOpacity(max(0.0, min(1.0, self.text_alpha)))
        x = rect.left() + self.pad
        available = rect.width() - 2 * self.pad
        for index, segment in enumerate(self.segments):
            if segment.kind == "dot":
                painter.setPen(Qt.NoPen)
                painter.setBrush(segment.color)
                painter.drawEllipse(QPointF(x + DOT / 2, rect.center().y()), DOT / 2, DOT / 2)
                x += DOT
            else:
                metrics = QFontMetrics(segment.font)
                painter.setFont(segment.font)
                painter.setPen(segment.color)
                text = segment.text
                if x + metrics.horizontalAdvance(text) > rect.left() + self.pad + available:
                    text = metrics.elidedText(text, Qt.ElideRight,
                                              max(10, int(rect.left() + self.pad
                                                           + available - x)))
                painter.drawText(QRectF(x, rect.top(), max(10.0, rect.left() + self.pad
                                                           + available - x), rect.height()),
                                 int(Qt.AlignLeft | Qt.AlignVCenter), text)
                x += metrics.horizontalAdvance(text)
            if index < len(self.segments) - 1:
                x += SEG_GAP
        painter.restore()
        painter.end()


def peek_segments(report: model.ProviderReport, warn: float, crit: float,
                  now: float | None = None) -> list[Segment]:
    """One agent, one line: its name and each window as "5h 100% (1h 34m)"."""
    name_segment = Segment(report.name, T.title(11.5, QFont.DemiBold), T.TEXT)
    segments = [name_segment]
    if not report.ok and report.note:
        segments.append(Segment(report.note, T.ui(10.0), T.ACCENT_NOTE))
        return segments
    for window in report.windows[:5]:
        level = window.level(warn, crit)
        color = T.color_for(level)
        segments.append(Segment(window.short, T.ui(10.5), T.TEXT_DIM))
        if window.unlimited:
            value = "不限量"
        elif window.used_percent is None:
            value = "--"
        else:
            value = "%.0f%%" % (100.0 - window.used_percent)
        counted = T.countdown(window.reset_at, now)
        if counted:
            value += " (%s)" % counted
        segments.append(Segment(value, T.numeric(10.5, QFont.DemiBold), color))
    if not report.windows:
        if report.raw_balance is not None:
            segments.append(Segment("余额", T.ui(10.5), T.TEXT_DIM))
            import money
            segments.append(Segment(money.compact(report.raw_balance,
                                                  report.balance_currency),
                                    T.numeric(10.5, QFont.DemiBold),
                                    T.color_for(report.worst_level(warn, crit))))
        elif report.today_tokens:
            segments.append(Segment("今日", T.ui(10.5), T.TEXT_DIM))
            segments.append(Segment("%s tokens" % model.human_tokens(report.today_tokens),
                                    T.numeric(10.5, QFont.DemiBold), T.TEXT))
        else:
            segments.append(Segment(report.note or "暂无可读额度", T.ui(10.0), T.TEXT_DIM))
    return segments


def reminder_segments(icon_text: str, text: str, level: str,
                      detail: str | None = None) -> list[Segment]:
    color = T.color_for(level)
    segments = [Segment("", T.ui(10.0), color, kind="dot"),
                Segment(icon_text, T.title(11.0, QFont.DemiBold), T.TEXT),
                Segment(text, T.ui(10.5), T.TEXT)]
    if detail:
        segments.append(Segment(detail, T.numeric(10.5, QFont.DemiBold), color))
    return segments
