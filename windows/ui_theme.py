"""Design tokens, fonts and the brand-artwork cache for the PySide6 HUD.

Everything visual that mirrors the original macOS app lives here: the palette
from Sources/AgentHUDDesktop/Theme/StatusPalette.swift, the alert thresholds
from Sources/AgentHUDCore/Logic/Alerts/AlertPolicy.swift, and the vendor to
bundled-artwork table from Sources/AgentHUDDesktop/Components/AgentLogo.swift.
"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetrics, QPainter,
                           QPainterPath, QPixmap)

# --------------------------------------------------------------------------- #
# palette (StatusPalette.swift + the dock/panel surfaces of the macOS HUD)
# --------------------------------------------------------------------------- #

OK = QColor("#3DDC84")
WARNING = QColor("#FFD23F")
CRITICAL = QColor("#FF453A")
IDLE = QColor("#9A9AA0")

DOCK_BG = QColor(0x14, 0x14, 0x17, 0xE6)
DOCK_EDGE = QColor(0xFF, 0xFF, 0xFF, 0x1A)
DOCK_TRACK = QColor(0xFF, 0xFF, 0xFF, 0x26)
DOCK_HOVER = QColor(0xFF, 0xFF, 0xFF, 0x0D)

PANEL_BG = QColor(0x16, 0x16, 0x1A, 0xF2)
PANEL_EDGE = QColor(0xFF, 0xFF, 0xFF, 0x12)
CARD_BG = QColor(0x1E, 0x1E, 0x24, 0xFF)
CARD_EDGE = QColor(0xFF, 0xFF, 0xFF, 0x12)
TRACK = QColor(0xFF, 0xFF, 0xFF, 0x22)

TEXT = QColor("#EDEDF0")
TEXT_DIM = QColor("#8E8E93")
TEXT_FAINT = QColor("#6C6C72")
ACCENT_PLAN = QColor("#D9BE6A")
ACCENT_NOTE = QColor("#E8836B")
DIVIDER = QColor(0xFF, 0xFF, 0xFF, 0x0F)

# One hue per agent, used by the 24h token bars and the totals breakdown.
# Codex keeps the OpenAI green its mark is tinted with; the rest follow each
# product's own brand colour.
AGENT_COLORS = {
    "codex": QColor("#10A37F"),
    "opencode": QColor("#7C9CFF"),
    "antigravity": QColor("#5B8DEF"),
    "workbuddy": QColor("#07C160"),
    "dsh": QColor("#5B7CFA"),
    "claude": QColor("#D97757"),
    "codebuddy": QColor("#4DBED3"),
    "pi": QColor("#E5C07B"),
    "kimi": QColor("#5A8EFF"),
    "qwen": QColor("#8B7AF4"),
    "openclaw": QColor("#FF6B6B"),
    "devin": QColor("#84C2F4"),
}


def agent_color(pid: str) -> QColor:
    return AGENT_COLORS.get(pid, QColor("#8AA0FF"))

LEVEL_COLORS = {"ok": OK, "warning": WARNING, "critical": CRITICAL, "idle": IDLE}


def color_for(level: str) -> QColor:
    return LEVEL_COLORS.get(level, IDLE)


# --------------------------------------------------------------------------- #
# fonts
# --------------------------------------------------------------------------- #

UI_FAMILIES = ["Segoe UI Variable Display", "Segoe UI", "Microsoft YaHei UI",
               "Microsoft YaHei", "Segoe UI Symbol"]
MONO_FAMILIES = ["Cascadia Mono", "JetBrains Mono", "Consolas", "Courier New"]
TITLE_FAMILIES = ["Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI"]


def _base(families, size, weight=QFont.Normal, letter_spacing=None) -> QFont:
    font = QFont()
    font.setFamilies(families)
    font.setPointSizeF(size)
    font.setWeight(weight)
    if letter_spacing is not None:
        font.setLetterSpacing(QFont.AbsoluteSpacing, letter_spacing)
    return font


def _tabular(font: QFont) -> QFont:
    """Ask for tabular figures so digits keep a constant width."""
    try:
        font.setFeature("tnum", 1)
    except Exception:
        pass
    return font


def ui(size=10.0, weight=QFont.Normal, spacing=None) -> QFont:
    return _base(UI_FAMILIES, size, weight, spacing)


def title(size=10.0, weight=QFont.DemiBold, spacing=None) -> QFont:
    return _base(TITLE_FAMILIES, size, weight, spacing)


def mono(size=10.0, weight=QFont.Normal, spacing=None) -> QFont:
    return _base(MONO_FAMILIES, size, weight, spacing)


def numeric(size=10.0, weight=QFont.DemiBold) -> QFont:
    """Proportional font with tabular figures - used for aligned numbers."""
    return _tabular(ui(size, weight))


def available_families() -> set[str]:
    try:
        return set(QFontDatabase.families())
    except Exception:
        return set()


def resolve_fonts() -> str:
    """Trim the fallback lists to families this machine really has."""
    global UI_FAMILIES, MONO_FAMILIES, TITLE_FAMILIES
    have = available_families()
    if not have:
        return "unknown"
    def keep(names, default):
        kept = [n for n in names if n in have]
        return kept or [default]
    UI_FAMILIES = keep(UI_FAMILIES, "Sans Serif")
    MONO_FAMILIES = keep(MONO_FAMILIES, "Courier New")
    TITLE_FAMILIES = keep(TITLE_FAMILIES, UI_FAMILIES[0])
    return "%s / %s" % (UI_FAMILIES[0], MONO_FAMILIES[0])


# --------------------------------------------------------------------------- #
# brand artwork (AgentLogo.swift: vendor -> bundled png)
# --------------------------------------------------------------------------- #

RESOURCE_DIR = (Path(__file__).resolve().parent.parent
                / "Sources" / "AgentHUDDesktop" / "Resources")

# The user's own avatar, used for the tray icon and as the Harness mark.
AVATAR_PATH = Path(__file__).resolve().parent / "assets" / "avatar.png"

# provider id -> (file, tint). Codex shares OpenAI's mark and tints it with the
# app's OpenAI green; OpenCode ships a variant drawn for dark backgrounds.
ARTWORK = {
    "codex": ("chatgpt", QColor(16, 163, 127)),
    "opencode": ("opencode-dark", None),
    "antigravity": ("antigravity", None),
    "workbuddy": ("workbuddy", None),
    "dsh": ("deepseek", None),
    "claude": ("claude", None),
    "codebuddy": ("codebuddy", None),
    "pi": ("pi", None),
    "kimi": ("kimi", None),
    "qwen": ("qwen", None),
    "openclaw": ("openclaw", None),
}

# Vendors whose mark is a different size inside its own canvas.
ART_INSET = {"codex": 0.86, "opencode": 0.90, "antigravity": 0.94,
             "workbuddy": 0.92, "dsh": 0.92}


class Marks:
    """Loads, tints and scales the bundled artwork, caching per device pixel ratio."""

    def __init__(self):
        self._sources: dict[str, QPixmap] = {}
        self._scaled: dict[tuple, QPixmap] = {}
        self._avatar: QPixmap | None = None

    def avatar(self) -> QPixmap | None:
        """The user's picture, cropped to a circle. Used for the tray icon."""
        if self._avatar is not None:
            return self._avatar
        pixmap = QPixmap()
        if AVATAR_PATH.exists():
            pixmap.load(str(AVATAR_PATH))
        if pixmap.isNull():
            self._avatar = QPixmap()
            return None
        side = min(pixmap.width(), pixmap.height())
        out = QPixmap(side, side)
        out.fill(Qt.transparent)
        mask = QPainter(out)
        mask.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addEllipse(0, 0, side, side)
        mask.setClipPath(path)
        # centre the crop on the face, which sits a little above middle
        mask.drawPixmap(int((side - pixmap.width()) / 2),
                        int((side - pixmap.height()) * 0.30), pixmap)
        mask.end()
        self._avatar = out
        return out

    def source(self, pid: str) -> QPixmap | None:
        if pid in self._sources:
            return self._sources[pid]
        name, tint = ARTWORK.get(pid, (None, None))
        pixmap = QPixmap()
        if name:
            path = RESOURCE_DIR / (name + ".png")
            if path.exists():
                pixmap.load(str(path))
        if pixmap.isNull():
            self._sources[pid] = pixmap
            return None
        if tint is not None:
            pixmap = self._tint(pixmap, tint)
        self._sources[pid] = pixmap
        return pixmap

    @staticmethod
    def _tint(source: QPixmap, color: QColor) -> QPixmap:
        out = QPixmap(source.size())
        out.fill(Qt.transparent)
        painter = QPainter(out)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(0, 0, source)
        painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        painter.fillRect(out.rect(), color)
        painter.end()
        return out

    def pixmap(self, pid: str, size: int, dpr: float) -> QPixmap | None:
        size = max(8, int(round(size)))
        key = (pid, size, round(dpr, 2))
        if key in self._scaled:
            return self._scaled[key]
        source = self.source(pid)
        if source is None or source.isNull():
            return None
        inset = ART_INSET.get(pid, 0.92)
        target = max(6, int(round(size * inset * dpr)))
        scaled = source.scaled(target, target, Qt.KeepAspectRatio,
                               Qt.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        self._scaled[key] = scaled
        return scaled


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def countdown(reset_at, now=None) -> str:
    """Spaced countdown such as "2h 25m" / "3d 4h" / "12m"."""
    if not reset_at:
        return ""
    delta = reset_at - (now if now is not None else time.time())
    if delta <= 0:
        return "已重置"
    minutes = int(delta // 60)
    if minutes < 60:
        return "%dm" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh %02dm" % (hours, minutes)
    days, hours = divmod(hours, 24)
    return "%dd %dh" % (days, hours)


def hours_text(hours: float) -> str:
    if hours is None:
        return ""
    minutes = int(round(hours * 60))
    if minutes < 60:
        return "%dm" % max(1, minutes)
    whole, minutes = divmod(minutes, 60)
    if whole < 24:
        return "%dh %02dm" % (whole, minutes)
    days, whole = divmod(whole, 24)
    return "%dd %dh" % (days, whole)


def level_rank(level: str) -> int:
    return {"idle": 0, "ok": 1, "warning": 2, "critical": 3}.get(level, 0)


def rounded_path(painter: QPainter, rect: QRectF, radius: float) -> None:
    painter.drawRoundedRect(rect, radius, radius)


def elide(painter: QPainter, font: QFont, text: str, width: int) -> str:
    return QFontMetrics(font).elidedText(text, Qt.ElideRight, max(10, width))
