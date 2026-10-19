"""The expanded usage panel: one card per agent.

Card surfaces, radii and type follow the macOS HoverPanelView; the token strip
is the 24h / 15-minute histogram the statistics page draws, folded into the card
of the agent the dock is focused on.
"""

from __future__ import annotations

import time

import model
import money
import motion
import ui_theme as T
from PySide6.QtCore import QRectF, QSize, Qt, QRect
from PySide6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy,  # noqa: E402
                               QToolButton, QVBoxLayout, QWidget)

CARD_RADIUS = 10
HEIGHT_MS = 200                   # the card's bottom edge following its content
CROSS_MS = 160                    # one agent's card dissolving into the next
PANEL_RADIUS = 16
TRACK = T.TRACK
BAR_ACCENT = QColor("#8AA0FF")


def money_symbol(currency: str | None) -> str:
    return {"CNY": "¥", "USD": "$", "EUR": "€"}.get((currency or "").upper(), "")


def cost_text(entries) -> str:
    """Every agent's day folded into one figure, in US dollars.

    Providers keep their ledgers in their own currency - the Harness whale
    ledger in CNY, OpenCode's database in USD - so the day's costs are converted
    at the configured rate and added up as a single dollar amount.
    """
    total = 0.0
    counted = False
    for _name, _colour, reading in entries:
        cost = getattr(reading, "today_cost", None)
        if cost is None:
            continue
        value = money.to_usd(cost, getattr(reading, "cost_currency", None))
        if value is None:
            continue
        total += value
        counted = True
    if not counted:
        return "无本地计价"
    return "≈$%.2f" % total


def wrap_lines(text: str, metrics: QFontMetrics, width: int,
               max_lines: int = 2) -> list[str]:
    """Wrap to at most max_lines lines, breaking inside CJK runs and eliding the rest."""
    text = " ".join(text.split())
    if not text:
        return []
    if width <= 0:
        return [metrics.elidedText(text, Qt.ElideRight, 1)]
    lines: list[str] = []
    index = 0
    while index < len(text) and len(lines) < max_lines:
        end = index + 1
        while end < len(text) and metrics.horizontalAdvance(text[index:end + 1]) <= width:
            end += 1
        if end >= len(text):
            lines.append(text[index:])
            index = len(text)
            break
        space = text.rfind(" ", index, end + 1)
        if space > index:
            lines.append(text[index:space])
            index = space + 1
        else:
            lines.append(text[index:end])
            index = end
    if index < len(text) and lines:
        lines[-1] = metrics.elidedText(lines[-1] + text[index:], Qt.ElideRight, width)
    return lines


class ProgressBar(QWidget):
    """A 6px rounded capsule bar with a soft highlight on the filled part."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.color = T.IDLE
        self.unlimited = False
        self.anim = motion.Scalar(self, self.update, ("share", "grown"))
        self.anim.snap("grown", 1.0)

    @property
    def share(self) -> float:
        return self.anim.value("share")

    def set_value(self, share: float, color: QColor, unlimited: bool = False,
                  animate: bool = False) -> None:
        self.color = color
        self.unlimited = unlimited
        target = max(0.0, min(1.0, share))
        if animate:
            self.anim.to("share", target, 500, motion.IN_OUT_CUBIC)
        else:
            self.anim.snap("share", target)

    def grow(self, ms: int = 380) -> None:
        """Opening the panel runs every bar out from zero to its real length."""
        self.anim.snap("grown", 0.0)
        self.anim.to("grown", 1.0, ms, motion.OUT_QUART)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        radius = self.height() / 2.0
        painter.setPen(Qt.NoPen)
        painter.setBrush(TRACK)
        painter.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), radius, radius)
        grown = self.anim.value("grown")
        fraction = 1.0 if self.unlimited else self.share * grown
        width = self.width() * fraction
        if width <= 0.5:
            return
        width = max(self.height(), width)
        rect = QRectF(0, 0, min(width, self.width()), self.height())
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0.0, self.color.lighter(118))
        gradient.setColorAt(1.0, self.color)
        painter.setBrush(gradient)
        painter.drawRoundedRect(rect, radius, radius)
        painter.setBrush(QColor(255, 255, 255, 46))
        painter.drawRoundedRect(QRectF(rect.left(), rect.top() + 1.0, rect.width(),
                                       self.height() / 2.0 - 1.0), radius / 2.0, radius / 2.0)


class QuotaRow(QWidget):
    """Window name, progress capsule, used percentage and reset countdown."""

    def __init__(self, window: model.QuotaWindow, label_w: int, used_w: int,
                 reset_w: int, warn: float, crit: float, parent=None):
        super().__init__(parent)
        self.window = window
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        self.name = QLabel(window.short)
        self.name.setFont(T.ui(9.5))
        self.name.setStyleSheet("color: %s;" % T.TEXT_DIM.name())
        self.name.setFixedWidth(label_w)
        row.addWidget(self.name)

        level = window.level(warn, crit)
        color = T.color_for(level)
        self.bar = ProgressBar()
        share = 0.0 if window.used_percent is None else window.used_percent / 100.0
        self.bar.set_value(share, color, window.unlimited)
        self.bar.grow()
        holder = QWidget()
        holder.setFixedHeight(6)
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.addWidget(self.bar)
        row.addWidget(holder, 1)

        if window.unlimited:
            used_text = "不限量"
        elif window.used_percent is None:
            used_text = "--"
        else:
            used_text = "剩余 %.0f%%" % (100.0 - window.used_percent)
        if window.used_amount is not None and window.total_amount:
            used_text += " · 剩 %.0f" % (window.total_amount - window.used_amount)
        self.used = QLabel(used_text)
        self.used.setFont(T.numeric(9.5, QFont.DemiBold))
        self.used.setStyleSheet("color: %s;" % color.name())
        self.used.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.used.setFixedWidth(used_w)
        row.addWidget(self.used)

        self.reset = QLabel(T.countdown(window.reset_at))
        self.reset.setFont(T.mono(9.0))
        self.reset.setStyleSheet("color: %s;" % T.TEXT_DIM.name())
        self.reset.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.reset.setFixedWidth(reset_w)
        row.addWidget(self.reset)

        self.setFixedHeight(max(18, self.sizeHint().height()))

    def apply_window(self, window, warn: float, crit: float, animate: bool = True) -> None:
        """Push a fresh reading into the row without rebuilding it.

        A poll that only moves a percentage should slide the bar and fade the
        figure across, not tear the card down and build it again.
        """
        if window is not None:
            self.window = window
        level = self.window.level(warn, crit)
        color = T.color_for(level)
        share = (0.0 if self.window.used_percent is None
                 else self.window.used_percent / 100.0)
        if self.bar.unlimited != self.window.unlimited:
            self.bar.unlimited = self.window.unlimited
        if self.bar.share != share:
            self.bar.set_value(share, color, self.window.unlimited, animate=animate)
        else:
            self.bar.set_value(share, color, self.window.unlimited)
        text = self._used_text()
        if self.used.text() != text:
            self.used.setText(text)
        self.used.setStyleSheet("color: %s;" % color.name())

    def _used_text(self) -> str:
        window = self.window
        if window.unlimited:
            text = "不限量"
        elif window.used_percent is None:
            text = "--"
        else:
            text = "剩余 %.0f%%" % (100.0 - window.used_percent)
        if window.used_amount is not None and window.total_amount:
            text += " · 剩 %.0f" % (window.total_amount - window.used_amount)
        return text

    def refresh_countdown(self) -> None:
        text = T.countdown(self.window.reset_at)
        if self.reset.text() != text:
            self.reset.setText(text)


class Pill(QLabel):
    """A small rounded badge, used for plans such as "plus"."""

    def __init__(self, text: str, color: QColor = T.ACCENT_PLAN, parent=None):
        super().__init__(text, parent)
        self.setFont(T.ui(8.5, QFont.DemiBold))
        self.setStyleSheet(
            "QLabel { color: %s; background: rgba(255,255,255,0.10);"
            " border-radius: 8px; padding: 2px 8px; }" % color.name())
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)


class ElidedLabel(QLabel):
    """Right-aligned single line that fades out instead of clipping."""

    def __init__(self, text: str, color: QColor = T.TEXT_DIM, size: float = 9.0,
                 mode=Qt.ElideMiddle, parent=None):
        super().__init__(text, parent)
        self.full = text
        self.color = color
        self.mode = mode
        self.setFont(T.ui(size))
        self.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumWidth(60)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setFont(self.font())
        painter.setPen(self.color)
        text = QFontMetrics(self.font()).elidedText(self.full, self.mode, self.width())
        painter.drawText(self.rect(), int(Qt.AlignRight | Qt.AlignVCenter), text)


class WordLabel(QLabel):
    """Small footnote text, wrapped to a bounded number of lines."""

    def __init__(self, text: str, width: int, color: QColor = T.TEXT_DIM,
                 size: float = 9.0, max_lines: int = 2, parent=None):
        super().__init__(parent)
        font = T.ui(size)
        self.setFont(font)
        metrics = QFontMetrics(font)
        lines = wrap_lines(text, metrics, width, max_lines)
        self.setText("\n".join(lines))
        self.setStyleSheet("color: %s;" % color.name())
        self.setFixedWidth(width)
        self.setFixedHeight(max(1, len(lines)) * metrics.lineSpacing())
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)


def draw_bars(painter: QPainter, plot: QRectF, layers, peak: int,
              growth: float = 1.0) -> None:
    """96 quarter-hour columns, one segment per layer, oldest on the left.

    layers is a list of (values, colour); an empty reading still draws the
    baseline, so a card with nothing to report keeps the same shape as the rest.
    growth scales every column from the baseline, which is how the chart grows
    upward when the panel opens.
    """
    count = max(1, len(layers[0][0]) if layers else 96)
    step = plot.width() / count
    bar_w = max(1.0, step * 0.62)
    if peak <= 0:
        painter.setPen(QColor(255, 255, 255, 26))
        painter.drawLine(int(plot.left()), int(plot.bottom()) - 1,
                         int(plot.right()), int(plot.bottom()) - 1)
        return
    for index in range(count):
        bottom = plot.bottom()
        for values, colour in layers:
            value = values[index] if index < len(values) else 0
            if value <= 0:
                continue
            height = max(1.2, (value / peak) * plot.height() * growth)
            rect = QRectF(plot.left() + index * step + (step - bar_w) / 2.0,
                          bottom - height, bar_w, height)
            painter.setPen(Qt.NoPen)
            painter.setBrush(colour)
            if bar_w < 2.5:
                # Ninety-six columns in a card leave each bar about a pixel
                # wide; rounding a one-pixel rect is pure overhead.
                painter.drawRect(rect)
            else:
                radius = min(1.5, bar_w / 2.0)
                painter.drawRoundedRect(rect, radius, radius)
            bottom -= height
    painter.setPen(QColor(255, 255, 255, 26))
    for fraction in (0.25, 0.5, 0.75):
        x = plot.left() + plot.width() * fraction
        painter.drawLine(int(x), int(plot.bottom()) - 3, int(x), int(plot.bottom()))


class TokenStrip(QWidget):
    """One agent's 24 hours: 96 quarter-hour buckets and their total."""

    MINUTES = 15
    COUNT = 96

    def __init__(self, reading, pid: str, parent=None):
        super().__init__(parent)
        self.buckets = list(getattr(reading, "buckets", []) or [0] * self.COUNT)
        if len(self.buckets) < self.COUNT:
            self.buckets = [0] * (self.COUNT - len(self.buckets)) + self.buckets
        self.buckets = self.buckets[:self.COUNT]
        self.total = sum(self.buckets)
        self._peak = max(self.buckets) if self.buckets else 0
        self.color = T.agent_color(pid)
        self.caption = "24h Token · 每 15 分钟一格 · 共 %s" % model.human_tokens(self.total)
        self.setFixedHeight(48)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.anim = motion.Scalar(self, self.update, ("growth",))
        self.anim.snap("growth", 1.0)

    def grow(self, ms: int = 320) -> None:
        self.anim.snap("growth", 0.0)
        self.anim.to("growth", 1.0, ms, motion.OUT_CUBIC)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing, True)
        rect = QRectF(self.rect())
        painter.setFont(T.ui(8.5))
        painter.setPen(T.TEXT_FAINT)
        painter.drawText(QRectF(0, 0, rect.width(), 12),
                         int(Qt.AlignLeft | Qt.AlignVCenter), self.caption)
        plot = QRectF(0, 15, rect.width(), rect.height() - 15)
        draw_bars(painter, plot, [(self.buckets, self.color)], self._peak,
                  self.anim.value("growth"))


class AgentCard(QWidget):
    """One agent's card inside the usage panel."""

    def __init__(self, report: model.ProviderReport, marks: T.Marks, warn: float,
                 crit: float, inner_width: int, focus: bool = False,
                 reading=None, burn: dict | None = None,
                 burn_window: model.QuotaWindow | None = None, parent=None):
        super().__init__(parent)
        self.pid = report.pid
        self.rows: list[QuotaRow] = []
        self.focused = focus
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 13)
        outer.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        icon = QLabel()
        pixmap = marks.pixmap(report.pid, 20, self.devicePixelRatioF() or 1.0)
        if pixmap is not None:
            icon.setPixmap(pixmap)
        icon.setFixedSize(20, 20)
        header.addWidget(icon)

        name = QLabel(report.name)
        name.setFont(T.title(11.5, QFont.DemiBold))
        name.setStyleSheet("color: %s;" % T.TEXT.name())
        header.addWidget(name)
        if report.plan:
            header.addWidget(Pill(str(report.plan)[:18]))
        header.addStretch(1)

        level = report.worst_level(warn, crit)
        dot = QLabel("●")
        dot.setFont(T.ui(8.0))
        dot.setStyleSheet("color: %s;" % T.color_for(level).name())
        header.addWidget(dot)
        header.addWidget(ElidedLabel("已连接" if report.ok else "读取失败", T.TEXT_DIM, 9.0), 0)
        outer.addLayout(header)

        if report.windows:
            metrics = QFontMetrics(T.ui(9.5))
            numeric = QFontMetrics(T.numeric(9.5, QFont.DemiBold))
            mono = QFontMetrics(T.mono(9.0))
            label_w = max(metrics.horizontalAdvance(w.short) for w in report.windows) + 6
            used_strings = []
            for window in report.windows:
                if window.unlimited:
                    text = "不限量"
                elif window.used_percent is None:
                    text = "--"
                else:
                    text = "剩余 %.0f%%" % (100.0 - window.used_percent)
                if window.used_amount is not None and window.total_amount:
                    text += " · 剩 %.0f" % (window.total_amount - window.used_amount)
                used_strings.append(text)
            used_w = max(numeric.horizontalAdvance(s) for s in used_strings) + 4
            reset_w = mono.horizontalAdvance("99d 99h") + 6
            rows_box = QVBoxLayout()
            rows_box.setSpacing(7)
            for window in report.windows:
                row = QuotaRow(window, label_w, used_w, reset_w, warn, crit)
                self.rows.append(row)
                rows_box.addWidget(row)
            outer.addLayout(rows_box)
            outer.addSpacing(1)

        if burn and burn.get("hours_to_empty") is not None and burn_window is not None:
            hours_left = None
            if burn_window.reset_at:
                hours_left = max(0.0, (burn_window.reset_at - time.time()) / 3600.0)
            if hours_left is None or burn["hours_to_empty"] < hours_left:
                outer.addWidget(WordLabel(
                    "按当前速度，%s 额度约 %s 后耗尽（%.1f%%/小时）"
                    % (burn_window.short, T.hours_text(burn["hours_to_empty"]),
                       burn["pct_per_hour"]),
                    inner_width, T.CRITICAL if hours_left and
                    burn["hours_to_empty"] < hours_left * 0.5 else T.WARNING, 9.0, 2))

        # Every card carries the same 24h histogram, even an agent that has
        # spent nothing: the empty baseline is the point, so the cards read as
        # one family instead of some having a chart and others not.
        self.strip = TokenStrip(reading, report.pid)
        outer.addWidget(self.strip)
        self.strip.grow()

        footer_lines: list[tuple[str, QColor]] = []
        primary = []
        if report.balance:
            primary.append("可用余额 %s" % report.balance)
        # The ledger is the same record the histogram is drawn from, so the
        # number and the bars can never disagree.
        today = getattr(reading, "today", None)
        if today is None:
            today = report.today_tokens
        if today is not None:
            piece = "今日 %s tokens" % model.human_tokens(today)
            cost = getattr(reading, "today_cost", None) or report.today_cost
            currency = getattr(reading, "cost_currency", None) or "USD"
            if cost:
                # one currency everywhere, at the configured rate
                piece += " · " + money.usd(cost, currency)
            primary.append(piece)
        if report.note:
            primary.append(report.note)
        if primary:
            footer_lines.append((" · ".join(primary),
                                 T.ACCENT_NOTE if report.note else T.TEXT_DIM))
        elif not report.windows:
            footer_lines.append(("余额模式（无额度窗口）" if report.pid == "dsh" else "暂无数据",
                                 T.TEXT_DIM))
        for detail in report.details:
            if detail.label == "余额说明":
                footer_lines.append((detail.value, T.ACCENT_NOTE))
                break
        for text, color in footer_lines:
            outer.addWidget(WordLabel(text, inner_width, color, 9.0, 2))
        outer.addStretch(0)
        self.outer = outer
        # A card is never compressed: if the panel runs out of room it scrolls,
        # it does not overlap the card below it.
        outer.activate()
        self.setMinimumHeight(self.sizeHint().height())

    def refresh(self) -> None:
        for row in self.rows:
            row.refresh_countdown()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, CARD_RADIUS, CARD_RADIUS)
        painter.setPen(Qt.NoPen)
        painter.setBrush(T.CARD_BG)
        painter.drawPath(path)
        painter.setBrush(Qt.NoBrush)
        if self.focused:
            painter.setPen(QColor(255, 255, 255, 60))
            painter.drawPath(path)
            accent = QPainterPath()
            accent.addRoundedRect(QRectF(rect.left() + 0.5, rect.top() + 8, 2.5,
                                         rect.height() - 16), 1.25, 1.25)
            painter.setPen(Qt.NoPen)
            painter.setBrush(T.color_for("ok"))
            painter.drawPath(accent)
        else:
            painter.setPen(T.CARD_EDGE)
            painter.drawPath(path)


class StackedStrip(QWidget):
    """The whole fleet's 24 hours: one column per quarter hour, stacked by agent."""

    def __init__(self, entries, parent=None):
        super().__init__(parent)
        self.entries = entries
        self.setFixedHeight(56)
        # Bucket and peak are fixed for the life of this widget: recomputing
        # them on every paint was the chart's whole cost.
        self.layers = []
        self._peak = 0
        count = 96
        for _name, colour, reading in entries:
            buckets = list(getattr(reading, "buckets", []) or [])
            if len(buckets) < count:
                buckets = [0] * (count - len(buckets)) + buckets
            self.layers.append((buckets[:count], colour))
        for index in range(count):
            self._peak = max(self._peak, sum(v[index] for v, _c in self.layers))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.anim = motion.Scalar(self, self.update, ("growth",))
        self.anim.snap("growth", 1.0)

    def grow(self, ms: int = 320) -> None:
        self.anim.snap("growth", 0.0)
        self.anim.to("growth", 1.0, ms, motion.OUT_CUBIC)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing, True)
        rect = QRectF(self.rect())
        painter.setFont(T.ui(8.5))
        painter.setPen(T.TEXT_FAINT)
        painter.drawText(QRectF(0, 0, rect.width(), 12),
                         int(Qt.AlignLeft | Qt.AlignVCenter),
                         "全部 Agent 24h 趋势 · 每 15 分钟一格")
        plot = QRectF(0, 15, rect.width(), rect.height() - 15)
        draw_bars(painter, plot, self.layers, self._peak, self.anim.value("growth"))


class Stat(QWidget):
    """One figure with its caption, for the totals header."""

    def __init__(self, caption: str, value: str, colour=None, parent=None):
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(1)
        head = QLabel(caption)
        head.setFont(T.ui(8.5))
        head.setStyleSheet("color: %s;" % T.TEXT_FAINT.name())
        box.addWidget(head)
        value_label = QLabel(value)
        value_label.setFont(T.numeric(12.5, QFont.DemiBold))
        value_label.setStyleSheet("color: %s;" % (colour or T.TEXT).name())
        box.addWidget(value_label)


class TotalsCard(QWidget):
    """Every agent at once: today, the last 24 hours, cost and the split."""

    def __init__(self, entries, inner_width: int, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 13)
        outer.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("全部 Agent 综合统计")
        title.setFont(T.title(11.5, QFont.DemiBold))
        title.setStyleSheet("color: %s;" % T.TEXT.name())
        header.addWidget(title)
        header.addStretch(1)
        caption = QLabel("ALL AGENTS TOTAL")
        caption.setFont(T.title(8.5, QFont.DemiBold, 1.2))
        caption.setStyleSheet("color: %s;" % T.TEXT_FAINT.name())
        header.addWidget(caption)
        outer.addLayout(header)

        today = sum(int(getattr(r, "today", 0) or 0) for _n, _c, r in entries)
        window = sum(int(getattr(r, "window_total", 0) or 0) for _n, _c, r in entries)
        stats = QHBoxLayout()
        stats.setSpacing(18)
        stats.addWidget(Stat("今日总计", "%s tokens" % model.human_tokens(today)))
        stats.addWidget(Stat("过去 24 小时", "%s tokens" % model.human_tokens(window)))
        stats.addWidget(Stat("今日估算花费", cost_text(entries), T.ACCENT_PLAN))
        stats.addStretch(1)
        outer.addLayout(stats)

        self.strip = StackedStrip(entries)
        outer.addWidget(self.strip)
        self.strip.grow()

        ranked = sorted(entries, key=lambda e: -(getattr(e[2], "today", 0) or 0))
        outer.addLayout(self._breakdown(ranked, today, inner_width))
        self.outer = outer
        outer.activate()
        self.setMinimumHeight(self.sizeHint().height())

    def _breakdown(self, ranked, today, inner_width: int) -> QVBoxLayout:
        """One line per agent instead of a paragraph that gets cut in half.

        The joined-up sentence was elided after two lines, so the agent that
        mattered least was the one that disappeared. A row each - a dot, the
        name, a share bar and the figure - never truncates.
        """
        box = QVBoxLayout()
        box.setSpacing(4)
        top = max([int(getattr(r, "today", 0) or 0) for _n, _c, r in ranked] or [1])
        for name, colour, reading in ranked:
            value = int(getattr(reading, "today", 0) or 0)
            share = (value / today * 100.0) if today else 0.0
            row = QHBoxLayout()
            row.setSpacing(7)
            dot = QLabel("●")
            dot.setFont(T.ui(7.5))
            dot.setStyleSheet("color: %s;" % colour.name())
            dot.setFixedWidth(9)
            row.addWidget(dot)
            label = QLabel(name)
            label.setFont(T.ui(9.0))
            label.setStyleSheet("color: %s;" % T.TEXT_DIM.name())
            label.setFixedWidth(96)
            row.addWidget(label)

            bar = ProgressBar()
            bar.set_value((value / top) if top else 0.0, colour)
            bar.grow(320)
            holder = QWidget()
            holder.setFixedHeight(6)
            holder_box = QHBoxLayout(holder)
            holder_box.setContentsMargins(0, 0, 0, 0)
            holder_box.addWidget(bar)
            row.addWidget(holder, 1)

            figure = QLabel("%s (%.0f%%)" % (model.human_tokens(value), share))
            figure.setFont(T.numeric(8.5, QFont.DemiBold))
            figure.setStyleSheet("color: %s;" % T.TEXT_DIM.name())
            figure.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            figure.setFixedWidth(102)
            row.addWidget(figure)
            box.addLayout(row)
        return box

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, CARD_RADIUS, CARD_RADIUS)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0x23, 0x24, 0x2C, 0xFF))
        painter.drawPath(path)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QColor(255, 255, 255, 34))
        painter.drawPath(path)


class Ghost(QWidget):
    """The previous card, held as a picture only while it dissolves away.

    A child of the panel, raised above the cards, and transparent to the mouse:
    it is a picture of what was there, never something to click. The widget is
    made once and reused; the picture itself is dropped the moment the fade
    ends, and the grab happens once per switch rather than once per frame.
    """

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._pix = None
        self._alpha = 0.0
        self.hide()

    def snapshot_from(self, widget) -> None:
        """Capture the widget as it looks right now - once."""
        pixmap = widget.grab()
        self._pix = None if pixmap.isNull() else pixmap
        self._alpha = 1.0
        self.setGeometry(widget.rect())
        if self._pix is not None:
            self.show()
            self.raise_()

    def set_alpha(self, value: float) -> None:
        if self._pix is None:
            return
        self._alpha = max(0.0, min(1.0, value))
        if self._alpha <= 0.02:
            self.release()
        else:
            self.update()

    def release(self) -> None:
        """Drop the picture and get out of the way."""
        self._pix = None
        self._alpha = 0.0
        self.hide()

    def paintEvent(self, _event) -> None:
        if self._pix is None:
            return
        painter = QPainter(self)
        painter.setOpacity(self._alpha)
        painter.drawPixmap(0, 0, self._pix)


class MonthCard(QWidget):
    """The calendar month's usage: what every harness spent, in tokens and money.

    Drawn rather than built from labels, like the alert card that came before
    it: one widget, no layout work, and the same rounded panel as every other
    card in the panel.
    """

    def __init__(self, totals, parent=None) -> None:
        super().__init__(parent)
        self.totals = dict(totals or {})
        self.rows = sorted((self.totals.get("by_agent") or {}).items(),
                           key=lambda row: -row[1])
        self.setMinimumHeight(self.sizeHint().height())
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_NoSystemBackground, True)

    def sizeHint(self) -> QSize:
        holder = self.parentWidget()
        width = holder.width() if holder is not None else 472
        return QSize(width, 128 + 22 * max(1, len(self.rows)))

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, CARD_RADIUS, CARD_RADIUS)
        painter.fillPath(path, QColor(T.CARD_BG))

        total = int(self.totals.get("tokens") or 0)
        since = self.totals.get("since") or ""
        days = int(self.totals.get("days") or 0)
        usd = self.totals.get("cost_usd")

        painter.setFont(T.title(12.5, QFont.Bold))
        painter.setPen(QColor(T.TEXT))
        painter.drawText(QRect(16, 10, self.width() - 32, 20),
                         int(Qt.AlignLeft | Qt.AlignVCenter),
                         "本月（全部 Agent）")
        painter.setFont(T.ui(10.0))
        painter.setPen(QColor(T.TEXT_DIM))
        painter.drawText(QRect(16, 10, self.width() - 32, 20),
                         int(Qt.AlignRight | Qt.AlignVCenter),
                         "%s 起 · %d 天" % (since, days))

        painter.setFont(T.numeric(21.0, QFont.Bold))
        painter.setPen(QColor(T.TEXT))
        painter.drawText(QRect(16, 34, self.width() - 32, 30),
                         int(Qt.AlignLeft | Qt.AlignVCenter),
                         model.human_tokens(total))

        money_text = ("≈ $%.2f" % usd) if isinstance(usd, (int, float)) else "无本地计价"
        painter.setFont(T.numeric(15.0, QFont.Bold))
        painter.setPen(QColor(T.WARNING if isinstance(usd, (int, float)) else T.TEXT_DIM))
        painter.drawText(QRect(16, 34, self.width() - 32, 30),
                         int(Qt.AlignRight | Qt.AlignVCenter), money_text)

        painter.setFont(T.ui(10.5))
        y = 72
        peak = max([v for _p, v in self.rows] + [1])
        for pid, tokens in self.rows:
            painter.setPen(QColor(T.agent_color(pid)))
            painter.drawText(QRect(16, y, 12, 18),
                             int(Qt.AlignLeft | Qt.AlignVCenter), "•")
            painter.setPen(QColor(T.TEXT))
            painter.drawText(QRect(28, y, 150, 18),
                             int(Qt.AlignLeft | Qt.AlignVCenter),
                             {"codex": "Codex", "opencode": "OpenCode",
                              "workbuddy": "WorkBuddy", "dsh": "DeepSeek Harness",
                              "antigravity": "Antigravity"}.get(pid, pid))
            bar = QRectF(self.width() - 210, y + 7, 150, 4)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 26))
            painter.drawRoundedRect(bar, 2, 2)
            painter.setBrush(QColor(T.agent_color(pid)))
            painter.drawRoundedRect(QRectF(bar.left(), bar.top(),
                                           bar.width() * (tokens / peak), bar.height()), 2, 2)
            painter.setPen(QColor(T.TEXT_DIM))
            # Right-aligned in a box wide enough for seven characters: at 48px
            # a three-digit figure lost its leading digit, so 1284.3M read as
            # 284.3M and the rows no longer added up to the total above them.
            painter.drawText(QRect(self.width() - 302, y, 88, 18),
                             int(Qt.AlignRight | Qt.AlignVCenter),
                             "%.1fM" % (tokens / 1e6))
            y += 22


class Panel(QWidget):
    """The hover panel: a rounded dark surface holding one card per agent."""

    def __init__(self, marks: T.Marks, width: int, parent=None):
        super().__init__(parent)
        self.marks = marks
        self.cards: list[AgentCard] = []
        self.totals_card = None
        # 0 shut, 1 fully open: the surface grows out of the docked edge, so a
        # panel that has just been asked for is not yet a whole rectangle.
        self.reveal = 1.0
        self.anim = motion.Scalar(self, self._on_anim, ("reveal", "height", "cross"))
        #: Called on every frame of a height change so the window can re-place
        #: the card. The window itself never moves for this: only its lower
        #: bound follows, and the dock is anchored to the top.
        self.on_height = None
        self._height_live = False
        self._in_anim = False
        self._height_target = None
        self._shown_pid = None
        self.ghost = Ghost(self)
        self.anim.snap("reveal", 1.0)
        # The panel paints all of itself in paintEvent; declaring that stops the
        # system from filling newly exposed area white on the frame where the
        # card set changes and the window grows.
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.fixed_width = int(width)
        self.setFixedWidth(width)
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(13, 11, 13, 13)
        self.outer.setSpacing(8)
        self.outer.setAlignment(Qt.AlignTop)

        header = QHBoxLayout()
        header.setSpacing(8)
        caption = QLabel("AGENT HUD")
        caption.setFont(T.title(9.5, QFont.DemiBold, 1.4))
        caption.setStyleSheet("color: %s;" % T.TEXT_FAINT.name())
        header.addWidget(caption)
        header.addStretch(1)
        self.status = QLabel("等待首次刷新")
        self.status.setFont(T.ui(9.0))
        self.status.setStyleSheet("color: %s;" % T.TEXT_FAINT.name())
        header.addWidget(self.status)
        self.on_statistics = None
        statistics = QToolButton()
        statistics.setText("统计")
        statistics.setToolTip("查看 5 小时、24 小时、7 天和 30 天用量")
        statistics.setStyleSheet("color: #8AA0FF; background: transparent; border: 0;")
        statistics.clicked.connect(lambda: self.on_statistics() if self.on_statistics else None)
        header.addWidget(statistics)
        self.outer.addLayout(header)

        # No scroll container at all. The panel lays its cards straight out and
        # asks for the height they need, so everything is on screen at once -
        # and the cards keep their own minimum heights, so nothing is squeezed.
        self.cards_box = QVBoxLayout()
        self.cards_box.setSpacing(8)
        self.outer.addLayout(self.cards_box)

    def set_month(self, totals) -> None:
        """The month page: one card and nothing else."""
        self.cards = []
        self.totals_card = None
        while self.cards_box.count():
            item = self.cards_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.ghost.release()
        card = MonthCard(totals, self)
        self.cards_box.addWidget(card)
        card.show()
        self._shown_pid = "__month__"
        self.status.setText("本月统计 · 本地账本")

    def set_reports(self, entries, warn: float, crit: float, stamp: float,
                    interval: int, totals=None) -> None:
        """entries: (report, focus, reading, burn, burn_window), in dock order."""
        while self.cards_box.count():
            item = self.cards_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.cards = []
        self.totals_card = None

        # Measured from the width the panel was given, not from QWidget.width():
        # before the first layout that answers with the default 640, and cards
        # built 586 wide inside a 472 panel are clipped along the right edge.
        inner = self.fixed_width - 2 * 13 - 2 * 14
        if totals:
            totals_card = TotalsCard(totals, inner, self)
            self.totals_card = totals_card
            self.cards_box.addWidget(totals_card)
            totals_card.show()
        # One card: the agent the dock is aimed at. The totals card above sums
        # every agent, so nothing is lost by not spreading all five out - and
        # what the panel is for is the detail of the one the pointer chose.
        for report, focus, reading, burn, burn_window in entries:
            if not focus:
                continue
            card = AgentCard(report, self.marks, warn, crit, inner, True,
                             reading, burn, burn_window, self)
            self.cards.append(card)
            self.cards_box.addWidget(card)
            # A widget built with an already-visible parent stays hidden until
            # it is shown, and a hidden widget reports an empty size hint.
            card.show()

        if stamp:
            when = time.strftime("%H:%M:%S", time.localtime(stamp))
            self.status.setText("更新 %s · 每 %d 秒刷新" % (when, interval))
        else:
            self.status.setText("刷新中…")
        self.update()

    def set_max_height(self, height: int) -> None:
        """Cap the panel so it never runs past the bottom of the screen.

        The maximum is set on the widget itself: a cap kept only in a helper
        would be ignored by the layout, which is what let the panel grow past
        the display and clip its own bottom cards.
        """
        self.max_h = max(120, int(height))
        if self.maximumHeight() != self.max_h:
            self.setMaximumHeight(self.max_h)
        self.outer.activate()

    def _on_anim(self) -> None:
        """One callback for every frame of reveal and height, so the two share
        a clock instead of each running their own.

        Re-entrant by necessity: placing the card reads its height, and setting
        a height notifies this callback. The guard makes the inner notification
        a no-op instead of a loop.
        """
        if self._in_anim:
            return
        self._in_anim = True
        try:
            self.update()
            self.ghost.set_alpha(self.anim.value("cross"))
            if self.on_height is not None:
                self.on_height()
        finally:
            self._in_anim = False

    def begin_crossfade(self, pid) -> None:
        """Dissolve the card that is up into the one about to replace it.

        Only a change of agent starts this. The same card refreshed by a poll
        must not blink, or every reading that arrived would set the panel
        flickering - which is why the decision is taken from the identity, not
        from the fact that set_reports was called.
        """
        same = pid == self._shown_pid
        self._shown_pid = pid
        if same or motion.REDUCED or not self.isVisible():
            if not same:
                self.ghost.release()
            return
        if self.width() <= 1 or self.height() <= 1:
            return
        # Captured with any previous ghost still on screen, so a second switch
        # mid-fade dissolves out of what is actually visible rather than jumping
        # back to an older card.
        self.ghost.snapshot_from(self)
        self.anim.snap("cross", 1.0)
        self.anim.to("cross", 0.0, CROSS_MS, motion.IN_OUT_CUBIC)

    def content_height(self) -> int:
        """The height the panel needs: its header, its margins, and its cards.

        Summed from the cards themselves rather than asked of the layout: it is
        the one measurement that cannot be thrown off by an intermediate widget.
        """
        box = self.cards_box
        box.activate()
        cards = 0
        count = 0
        for index in range(box.count()):
            item = box.itemAt(index)
            widget = item.widget() if item is not None else None
            if widget is None or widget.isHidden():
                continue
            cards += max(widget.sizeHint().height(), widget.minimumHeight())
            count += 1
        if count > 1:
            cards += box.spacing() * (count - 1)
        margins = self.outer.contentsMargins()
        header = max(16, self.status.sizeHint().height())
        natural = max(240, cards + header + 8 + margins.top() + margins.bottom())
        stable = getattr(self, "stable_h", 0)
        if stable:
            natural = stable
        cap = getattr(self, "max_h", None)
        if cap:
            natural = min(natural, cap)
        return natural

    def preferred_height(self) -> int:
        """The height to lay out with this frame: the content, eased towards.

        Reading a height must not start anything. This used to compare the
        content against its last target and launch the animation from inside
        what looks like a query - so any caller, any helper, any future code
        that merely asked how tall the cards were could set an animation going,
        and it was called on every frame. Computing, retargeting and animating
        are three separate things now: content_height() computes,
        retarget_height() retargets, motion does the rest.
        """
        natural = self.content_height()
        if not self.isVisible():
            return natural
        if self._height_target is None:
            self._height_target = float(natural)
            self.anim.snap("height", float(natural))
        return max(120, int(round(self.anim.value("height"))))

    def retarget_height(self) -> None:
        """Follow a new content height, over HEIGHT_MS. Called on new content."""
        natural = self.content_height()
        target = self._height_target
        if not self.isVisible() or target is None:
            self._height_target = float(natural)
            self.anim.snap("height", float(natural))
            return
        if abs(target - natural) <= 1.0:
            return
        self._height_target = float(natural)
        self.anim.to("height", float(natural), HEIGHT_MS, motion.OUT_CUBIC)

    def sizeHint(self) -> QSize:
        """The panel answers with its own height, so the layout cannot guess.

        Left to the layout the panel was squeezed into whatever sliver of the
        window was left over, and every card was compressed into it; asking to
        be exactly as tall as its cards are, up to the cap, is what keeps them
        from overlapping.
        """
        return QSize(self.fixed_width, self.preferred_height())

    def minimumSizeHint(self) -> QSize:
        return QSize(self.fixed_width, 60)

    def set_reveal(self, fraction: float) -> None:
        self.anim.snap("reveal", max(0.0, min(1.0, fraction)))

    def tween_reveal(self, fraction: float, ms: int, curve) -> None:
        self.anim.to("reveal", max(0.0, min(1.0, fraction)), ms, curve)

    def tween_values(self, reports, warn: float, crit: float) -> bool:
        """Slide an open panel's bars to new readings; False if cards differ."""
        if not self.cards:
            return False
        by_pid = {r.pid: r for r in reports}
        for card in self.cards:
            report = by_pid.get(card.pid)
            if report is None or len(report.windows) != len(card.rows):
                return False
            for row in card.rows:
                match = next((w for w in report.windows
                              if w.key == row.window.key), None)
                if match is None:
                    return False
            for row in card.rows:
                match = next(w for w in report.windows if w.key == row.window.key)
                row.apply_window(match, warn, crit, animate=True)
        return True

    def refresh(self) -> None:
        for card in self.cards:
            card.refresh()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        # the surface emerges from the edge it is docked to
        reveal = self.anim.value("reveal")
        if reveal < 0.999:
            grown = 12.0 * (1.0 - reveal)
            rect = QRectF(rect.left(), rect.top() + grown, rect.width(),
                          max(4.0, rect.height() - grown))
        path = QPainterPath()
        path.addRoundedRect(rect, PANEL_RADIUS, PANEL_RADIUS)
        painter.setPen(Qt.NoPen)
        painter.setBrush(T.PANEL_BG)
        painter.drawPath(path)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(T.PANEL_EDGE)
        painter.drawPath(path)
