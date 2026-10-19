"""Local-only usage statistics, reusing the HUD's scanner, palette and chart."""

from __future__ import annotations

import model
import providers
import tokens
import ui_theme as T
from panel import draw_bars
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView,
                               QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)


class UsageChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.layers = []
        self.setMinimumHeight(160)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        peak = max((sum(values) for values in zip(*(row[0] for row in self.layers))), default=0)
        draw_bars(painter, QRectF(4, 4, self.width() - 8, self.height() - 8), self.layers, peak)


class UsageWindow(QWidget):
    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("Agent HUD · 用量统计")
        self.resize(680, 520)
        self.setMinimumSize(480, 400)
        self.setStyleSheet(
            "QWidget { background: #16161A; color: #EDEDF0; }"
            "QComboBox { background: #25252D; border: 1px solid #45454F; padding: 5px; }"
            "QTableWidget { background: #1E1E24; border: 0; gridline-color: #33333B; }"
            "QHeaderView::section { background: #25252D; padding: 6px; border: 0; }"
        )
        self.setFont(T.ui(10))
        box = QVBoxLayout(self)
        box.setContentsMargins(18, 16, 18, 16)
        box.setSpacing(12)
        filters = QHBoxLayout()
        heading = QLabel("用量统计")
        heading.setFont(T.title(15))
        filters.addWidget(heading)
        filters.addStretch()
        self.period = QComboBox()
        for key, title in (("5h", "过去 5 小时"), ("24h", "过去 24 小时"),
                           ("7d", "过去 7 天"), ("30d", "过去 30 天")):
            self.period.addItem(title, key)
        self.period.setCurrentIndex(1)
        filters.addWidget(self.period)
        self.agent = QComboBox()
        self.agent.addItem("全部 Agent", None)
        for pid, name, _mark in providers.REGISTRY:
            self.agent.addItem(name, pid)
        filters.addWidget(self.agent)
        box.addLayout(filters)
        self.total = QLabel("等待本地采集…")
        self.total.setFont(T.numeric(21))
        box.addWidget(self.total)
        self.chart = UsageChart(self)
        box.addWidget(self.chart)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Agent", "Token", "已记录费用（USD）"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setWordWrap(False)
        box.addWidget(self.table, 1)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #8E8E93;")
        box.addWidget(self.note)
        self.snapshot = None
        self.period.currentIndexChanged.connect(self.refresh)
        self.agent.currentIndexChanged.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._poll)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def _poll(self):
        if tokens.cached() is not self.snapshot:
            self.refresh()

    def refresh(self, *_args):
        self.snapshot = tokens.cached()
        readings = self.snapshot or {}
        key, chosen = self.period.currentData(), self.agent.currentData()
        rows, layers, total, priced = [], [], 0, 0
        for pid, name, _mark in providers.REGISTRY:
            if chosen is not None and chosen != pid:
                continue
            reading = readings.get(pid)
            entry = getattr(reading, "ranges", {}).get(key)
            if not entry:
                rows.append((name, "暂无记录", "—"))
                continue
            count, cost = entry["tokens"], entry["cost_usd"]
            total += count
            priced += entry["priced_tokens"]
            rows.append((name, model.human_tokens(count), "≈$%.2f" % cost if cost is not None else "—"))
            layers.append((entry["buckets"], T.agent_color(pid)))
        self.chart.layers = layers
        self.chart.update()
        self.total.setText("%s tokens" % model.human_tokens(total))
        self.table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, col, item)
        seconds, count = tokens.RANGES[key]
        minutes = seconds // count // 60
        self.note.setText(
            "左侧较早，右侧现在 · 每柱 %s · 仅统计仍保留的本地记录。\n"
            "费用仅含来源已记录的美元计价（覆盖 %.0f%% Token）；— 表示未知，不等于免费。"
            % ("%d 分钟" % minutes if minutes < 60 else "%d 小时" % (minutes // 60),
               priced * 100 / total if total else 0))
