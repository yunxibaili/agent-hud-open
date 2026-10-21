"""Local-only usage statistics, reusing the HUD's scanner, palette and chart."""

from __future__ import annotations

import model
import providers
import tokens
import ui_theme as T
from panel import draw_bars
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QHeaderView,
                               QLayout, QScrollArea,
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


class UsageHeatmap(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.values = [[0] * 24 for _ in range(7)]
        self.setFixedHeight(132)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setFont(T.ui(8))
        painter.setPen(T.TEXT_DIM)
        painter.drawText(0, 12, "工作时段 · 周一至周日 × 小时（本地时间）· 越亮用量越多")
        width = (self.width() - 28) / 24
        peak = max((value for row in self.values for value in row), default=0)
        for day, row in enumerate(self.values):
            painter.setPen(T.TEXT_DIM)
            painter.drawText(0, 35 + day * 13, "一二三四五六日"[day])
            for hour, value in enumerate(row):
                colour = QColor(T.OK)
                colour.setAlpha(35 + int(220 * value / peak) if peak else 35)
                painter.fillRect(QRectF(25 + hour * width, 25 + day * 13, width - 2, 10), colour)
        painter.setPen(T.TEXT_DIM)
        for hour in range(0, 24, 4):
            painter.drawText(int(25 + hour * width), 129, str(hour))


class UsageWindow(QWidget):
    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("Agent HUD · 用量统计")
        self.resize(840, 720)
        self.setMinimumSize(480, 360)
        self.setStyleSheet(
            "QWidget { background: #16161A; color: #EDEDF0; }"
            "QComboBox { background: #25252D; border: 1px solid #45454F; padding: 5px; }"
            "QTableWidget { background: #1E1E24; border: 0; gridline-color: #33333B; }"
            "QHeaderView::section { background: #25252D; padding: 6px; border: 0; }"
        )
        self.setFont(T.ui(10))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        body = QWidget()
        box = QVBoxLayout(body)
        box.setSizeConstraint(QLayout.SetMinimumSize)
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
        details = QHBoxLayout()
        self.model = QComboBox()
        self.model.addItem("全部模型", None)
        self.model.setMinimumContentsLength(20)
        self.model.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        details.addWidget(self.model, 1)
        self.kind = QComboBox()
        self.kind.addItem("全部 Token 类型", None)
        for key, title in tokens.TOKEN_KINDS.items():
            self.kind.addItem(title, key)
        details.addWidget(self.kind)
        box.addLayout(details)
        self.total = QLabel("等待本地采集…")
        self.total.setFont(T.numeric(21))
        box.addWidget(self.total)
        self.chart = UsageChart(self)
        box.addWidget(self.chart)
        self.heatmap = UsageHeatmap(self)
        box.addWidget(self.heatmap)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Agent", "模型", "计费来源", "Token", "缓存读占输入", "记录/估算费用（USD）"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setWordWrap(False)
        self.table.setMinimumHeight(180)
        box.addWidget(self.table, 1)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #8E8E93;")
        box.addWidget(self.note)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self.snapshot = None
        self.period.currentIndexChanged.connect(self.refresh)
        self.agent.currentIndexChanged.connect(self.refresh)
        self.model.currentIndexChanged.connect(self.refresh)
        self.kind.currentIndexChanged.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._poll)

    def showEvent(self, event):
        super().showEvent(event)
        if self.screen():
            frame = self.screen().availableGeometry()
            self.resize(min(self.width(), frame.width() - 24), min(self.height(), frame.height() - 24))
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
        if self.snapshot is None:
            self.total.setText("正在读取本地记录…")
            self.note.setText("首次扫描在后台进行，完成后自动显示；尚未采集的用量不记作零。")
            return
        readings = self.snapshot or {}
        key, chosen = self.period.currentData(), self.agent.currentData()
        names = set()
        for pid, reading in readings.items():
            if chosen is None or pid == chosen:
                names.update(reading.ranges.get(key, {}).get("models", {}))
        selected_model = self.model.currentData()
        choices = [self.model.itemData(i) for i in range(1, self.model.count())]
        if choices != sorted(names):
            self.model.blockSignals(True)
            self.model.clear()
            self.model.addItem("全部模型", None)
            for name in sorted(names):
                self.model.addItem("未记录模型" if name == "Unknown" else name, name)
            index = self.model.findData(selected_model)
            self.model.setCurrentIndex(max(0, index))
            self.model.blockSignals(False)
        selected_model, kind = self.model.currentData(), self.kind.currentData()
        rows, layers, total, priced, observed, classified = [], [], 0, 0, 0, 0
        activity = [[0] * 24 for _ in range(7)]
        for pid, name, _mark in providers.REGISTRY:
            if chosen is not None and chosen != pid:
                continue
            reading = readings.get(pid)
            entry = getattr(reading, "ranges", {}).get(key)
            if not entry:
                rows.append((name, "—", "—", "暂无记录", "—", "—"))
                continue
            values = [0] * tokens.RANGES[key][1]
            for model_name, data in entry["models"].items():
                if selected_model is not None and selected_model != model_name:
                    continue
                observed += data["tokens"]
                classified += data["known_tokens"]
                count = data["parts"][kind] if kind else data["tokens"]
                total += count
                cost = None if kind else data["cost_usd"]
                if not kind:
                    priced += data["priced_tokens"]
                buckets = data["part_buckets"][kind] if kind else data["buckets"]
                values = [a + b for a, b in zip(values, buckets)]
                grid = data["part_activity"][kind] if kind else data["activity"]
                activity = [[a + b for a, b in zip(left, right)] for left, right in zip(activity, grid)]
                incoming = sum(data["parts"][part] for part in ("input", "cache_read", "cache_write"))
                hit = ("%.0f%%" % (data["parts"]["cache_read"] * 100 / incoming)
                       if incoming and data["known_tokens"] == data["tokens"] else "—")
                rows.append((name, "未记录模型" if model_name == "Unknown" else model_name,
                             " / ".join(data.get("billing_sources", ["其他/未记录"])),
                             model.human_tokens(count), hit, "≈$%.2f" % cost if cost is not None else "—"))
            layers.append((values, T.agent_color(pid)))
        self.chart.layers = layers
        self.chart.update()
        self.heatmap.values = activity
        self.heatmap.update()
        self.total.setText("%s tokens%s" % (model.human_tokens(total), " · " + tokens.TOKEN_KINDS[kind] if kind else ""))
        self.table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if col >= 3:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, col, item)
        seconds, count = tokens.RANGES[key]
        minutes = seconds // count // 60
        price_note = ("按 Token 类型筛选时不分摊费用。" if kind else
                      "记录/估算费用覆盖 %.0f%% Token；— 表示未知，不等于免费。" % (priced * 100 / total if total else 0))
        self.note.setText(
            "左侧较早，右侧现在 · 每柱 %s · 仅统计仍保留的本地记录。\n%s\n"
            "输入/输出/缓存分项覆盖 %.0f%% Token；缺失分项不猜测。"
            % ("%d 分钟" % minutes if minutes < 60 else "%d 小时" % (minutes // 60),
               price_note, classified * 100 / observed if observed else 0))
        notices = ["%s：%s" % (providers.META[pid][0], reading.notice)
                   for pid, reading in readings.items()
                   if (chosen is None or chosen == pid) and reading.notice]
        if notices:
            self.note.setText(self.note.text() + "\n" + "\n".join(notices[:3]))
        if chosen in (None, "dsh"):
            self.note.setText(self.note.text() + "\nDeepSeek 官方 API 按现行人民币单价、请求开始时间和北京时间峰谷规则估算，再换算为美元；不代表账单。")
