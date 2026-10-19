"""Edge docking geometry - a port of Sources/AgentHUDDesktop/Notch/NotchGeometry.swift.

Swift uses a bottom-left origin; everything here is Qt's top-left origin, so
the vertical terms are flipped. The rules are otherwise the same:

* notch mode is glued to the top of the screen,
* a docked strip parks on the edge nearest the pointer and is measured along
  that edge with an offset in 0...1,
* the expanded panel is anchored to the same edge, centred on the collapsed
  rect and grown inward.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from PySide6.QtCore import QPoint, QRect, QSize

# NotchGeometry.swift corner radii
NOTCH_CORNER_RADIUS = 12
FALLBACK_CORNER_RADIUS = 10
LOGO_CORNER_RADIUS = 10
COLLAPSED_TOP_RADIUS = 8
EXPANDED_TOP_RADIUS = 16

# Distance a dock keeps from its screen edge. An edge-peeked dock slides fully
# out of sight (sliver 0) and is summoned back from the edge hot zone.
EDGE_INSET = 3
PEEK_SLIVER = 0


class Edge(str, Enum):
    TOP = "top"
    BOTTOM = "bottom"
    LEFT = "left"
    RIGHT = "right"

    @property
    def is_horizontal(self) -> bool:
        return self in (Edge.TOP, Edge.BOTTOM)


class Mode(str, Enum):
    NOTCH = "notch"
    DOCK = "dock"


@dataclass
class Placement:
    mode: Mode = Mode.DOCK
    edge: Edge = Edge.TOP
    #: position along the edge, 0 = start (left / top), 1 = end (right / bottom)
    offset: float = 0.5
    #: distance the strip keeps from its screen edge
    inset: int = EDGE_INSET

    def to_dict(self) -> dict:
        return {"mode": self.mode.value, "edge": self.edge.value,
                "offset": round(float(self.offset), 4), "inset": int(self.inset)}

    @staticmethod
    def from_dict(data: dict | None) -> "Placement":
        data = data or {}
        try:
            mode = Mode(data.get("mode", "dock"))
        except ValueError:
            mode = Mode.DOCK
        try:
            edge = Edge(data.get("edge", "top"))
        except ValueError:
            edge = Edge.TOP
        try:
            offset = float(data.get("offset", 0.5))
        except (TypeError, ValueError):
            offset = 0.5
        return Placement(mode=mode, edge=edge, offset=min(1.0, max(0.0, offset)),
                         inset=int(data.get("inset", EDGE_INSET) or 0))


def strip_rect(strip: QSize, frame: QRect, placement: Placement) -> QRect:
    """Where the mark strip sits, at placement.offset along its edge and on screen."""
    # Notch mode is glued to the screen top: its flaps are square against the
    # bezel, so no inset may lift it away (NotchGeometry.detect).
    inset = 0 if placement.mode == Mode.NOTCH else placement.inset
    horizontal = placement.edge.is_horizontal
    long_side = strip.width() if horizontal else strip.height()
    thick = strip.height() if horizontal else strip.width()
    if horizontal:
        width = min(long_side, frame.width())
        travel = frame.width() - width
        x = frame.left() + int(round(travel * placement.offset)) if travel > 0 else frame.left()
        x = max(frame.left(), min(x, frame.right() + 1 - width))
        y = (frame.top() + inset if placement.edge == Edge.TOP
             else frame.bottom() + 1 - thick - inset)
        return QRect(x, y, width, thick)
    height = min(long_side, frame.height())
    travel = frame.height() - height
    y = frame.top() + int(round(travel * placement.offset)) if travel > 0 else frame.top()
    y = max(frame.top(), min(y, frame.bottom() + 1 - height))
    x = (frame.left() + inset if placement.edge == Edge.LEFT
         else frame.right() + 1 - thick - inset)
    return QRect(x, y, thick, height)


def drag_placement(point: QPoint, frame: QRect, strip: QSize, placement: Placement,
                   grab_fraction: float = 0.5) -> Placement:
    """Park the strip at the edge nearest the pointer, keeping the grabbed point under it."""

    def distance(edge: Edge) -> float:
        if edge == Edge.TOP:
            return abs(point.y() - frame.top())
        if edge == Edge.BOTTOM:
            return abs(frame.bottom() - point.y())
        if edge == Edge.LEFT:
            return abs(point.x() - frame.left())
        return abs(frame.right() - point.x())

    # Starting from the current edge keeps an exact diagonal tie stable.
    edge = placement.edge
    for candidate in Edge:
        if distance(candidate) < distance(edge):
            edge = candidate
    run = strip.width() if placement.edge.is_horizontal else strip.height()
    length = frame.width() if edge.is_horizontal else frame.height()
    measured = min(run, length)
    travel = length - measured
    reading = (point.x() - frame.left()) if edge.is_horizontal else (point.y() - frame.top())
    origin = reading - measured * grab_fraction
    result = replace(placement)
    # Only the notch has to become something else to leave its place.
    if result.mode == Mode.NOTCH:
        result.mode = Mode.DOCK
    result.edge = edge
    result.offset = min(1.0, max(0.0, origin / travel)) if travel > 0 else 0.5
    return result


def centre_along(anchor: QRect, size: QSize, frame: QRect, edge: Edge) -> QRect:
    """A box of size, aligned to edge, centred on anchor along that edge."""
    if edge.is_horizontal:
        width = min(size.width(), frame.width())
        height = min(size.height(), frame.height())
        x = max(frame.left(), min(anchor.center().x() - width // 2, frame.right() + 1 - width))
        if edge == Edge.TOP:
            return QRect(x, anchor.bottom() + 1, width, height)
        return QRect(x, anchor.top() - height, width, height)
    width = min(size.width(), frame.width())
    height = min(size.height(), frame.height())
    y = max(frame.top(), min(anchor.center().y() - height // 2, frame.bottom() + 1 - height))
    if edge == Edge.LEFT:
        return QRect(anchor.right() + 1, y, width, height)
    return QRect(anchor.left() - width, y, width, height)


def hidden_offset(strip: QRect, frame: QRect, edge: Edge, sliver: int = 0) -> QPoint:
    """How far the strip slides off screen so only sliver pixels stay visible.

    Measured against the frame, not against (0, 0): a display above or left of
    the primary one has negative coordinates, and a slide of "height minus
    sliver" would leave the strip sitting inside that screen instead of off it.
    A sliver of 0 slides the strip fully out of sight.
    """
    if edge == Edge.TOP:
        return QPoint(0, (frame.top() + sliver - 1) - strip.bottom())
    if edge == Edge.BOTTOM:
        return QPoint(0, (frame.bottom() - sliver + 1) - strip.top())
    if edge == Edge.LEFT:
        return QPoint((frame.left() + sliver - 1) - strip.right(), 0)
    return QPoint((frame.right() - sliver + 1) - strip.left(), 0)


def edge_hot_zone(strip: QRect, frame: QRect, edge: Edge,
                  thickness: int = 4, slack: int = 14) -> QRect:
    """The sliver along the screen edge that summons a hidden dock back.

    Every term is measured from the strip or from the screen frame, never from
    an assumed origin: a second monitor left of the primary one, or any virtual
    desktop that does not start at (0, 0), would otherwise leave the dock with
    no way back.
    """
    if edge == Edge.TOP:
        return QRect(strip.left() - slack, frame.top(),
                     strip.width() + 2 * slack, thickness)
    if edge == Edge.BOTTOM:
        return QRect(strip.left() - slack, frame.bottom() + 1 - thickness,
                     strip.width() + 2 * slack, thickness)
    if edge == Edge.LEFT:
        return QRect(frame.left(), strip.top() - slack,
                     thickness, strip.height() + 2 * slack)
    return QRect(frame.right() + 1 - thickness, strip.top() - slack,
                 thickness, strip.height() + 2 * slack)


def screen_frame(available: QRect, full: QRect) -> QRect:
    """The frame a dock is measured against: the whole screen, menu bar included."""
    return QRect(full) if not full.isEmpty() else QRect(available)
