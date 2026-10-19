"""Hover state machine - a 1:1 port of Sources/AgentHUDDesktop/Notch/HoverMachine.swift.

Open after hover_delay, collapse after collapse_delay; the caller schedules one
timer for the returned deadline and feeds TIMER_FIRED back in. On top of that
sits the three-level dock: hidden, a one-line peek for the agent under the
pointer, and the whole panel once that agent is clicked (or Alt is held).
"""

from __future__ import annotations

import time
from enum import Enum

# ScreenHUD.swift: "On a dock that separates its agents, hovering only peeks;
# a click asks for the agent's whole panel." Resting on a mark for this long is
# the exception, and it is deliberately long enough that ordinary hovering never
# trips it.
PANEL_REST_DELAY = 1.6
COLLAPSE_DELAY = 0.25
# The one-line peek answers the pointer almost at once.
PEEK_DELAY = 0.10


class State(str, Enum):
    COLLAPSED = "collapsed"
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"


class Event(str, Enum):
    POINTER_ENTERED = "pointerEntered"
    POINTER_EXITED = "pointerExited"
    TIMER_FIRED = "timerFired"
    FORCE_OPEN = "forceOpen"
    FORCE_COLLAPSE = "forceCollapse"


class Level(int, Enum):
    """How much of the dock is showing."""

    HIDDEN = 1     # the compact strip only (or its 3px sliver)
    PEEK = 2       # plus the single-line capsule for one agent
    PANEL = 3      # plus the whole card panel


class HoverMachine:
    """The Swift struct, verbatim: a state plus a reduce() step."""

    def __init__(self, state: State = State.COLLAPSED, deadline: float | None = None,
                 hover_delay: float = PANEL_REST_DELAY,
                 collapse_delay: float = COLLAPSE_DELAY):
        self.state = state
        self.deadline = deadline
        self.hover_delay = hover_delay
        self.collapse_delay = collapse_delay

    @property
    def is_open(self) -> bool:
        return self.state in (State.OPEN, State.CLOSING)

    def reduce(self, event: Event, now: float | None = None,
               hover_delay: float | None = None,
               collapse_delay: float | None = None) -> float | None:
        """Return the new deadline to schedule, or None to cancel the timer."""
        now = time.monotonic() if now is None else now
        hover_delay = self.hover_delay if hover_delay is None else hover_delay
        collapse_delay = self.collapse_delay if collapse_delay is None else collapse_delay
        state, deadline = self.state, self.deadline

        if state == State.COLLAPSED and event == Event.POINTER_ENTERED:
            when = now + hover_delay
            self.state, self.deadline = State.OPENING, when
            return when

        if state == State.OPENING and event == Event.POINTER_EXITED:
            self.state, self.deadline = State.COLLAPSED, None
            return None

        if state == State.OPENING and event == Event.TIMER_FIRED and now >= (deadline or 0.0):
            self.state, self.deadline = State.OPEN, None
            return None

        if state == State.OPEN and event == Event.POINTER_EXITED:
            when = now + collapse_delay
            self.state, self.deadline = State.CLOSING, when
            return when

        if state == State.CLOSING and event == Event.POINTER_ENTERED:
            self.state, self.deadline = State.OPEN, None
            return None

        if state == State.CLOSING and event == Event.TIMER_FIRED and now >= (deadline or 0.0):
            self.state, self.deadline = State.COLLAPSED, None
            return None

        if event == Event.FORCE_OPEN:
            self.state, self.deadline = State.OPEN, None
            return None

        if event == Event.FORCE_COLLAPSE:
            self.state, self.deadline = State.COLLAPSED, None
            return None

        # An early timer keeps its deadline: the caller replaces its timer after
        # every transition, so waiting with no timer would strand the hover.
        return deadline


class DockLevels:
    """The dock's three levels over the machines above.

    * HIDDEN - the compact strip, or the 3px sliver of an edge-peeked dock.
    * PEEK   - the strip plus the one-line capsule for the agent under the pointer.
    * PANEL  - the strip, the line and the whole card panel for that agent.

    The peek answers the pointer almost at once and follows it from mark to
    mark; the panel needs the machine's hover delay or a click, which is
    immediate. Everything collapses together once the pointer leaves.
    """

    def __init__(self, hover_delay: float = PANEL_REST_DELAY,
                 collapse_delay: float = COLLAPSE_DELAY,
                 peek_delay: float = PEEK_DELAY):
        self.hover_delay = hover_delay
        self.collapse_delay = collapse_delay
        self.peek_delay = peek_delay
        self.panel = HoverMachine(hover_delay=hover_delay, collapse_delay=collapse_delay)
        self.peek = HoverMachine(hover_delay=peek_delay, collapse_delay=collapse_delay)
        # The mark a click focused. It is not a pin: the original closes the
        # panel as soon as the pointer leaves its edge, and so does this.
        self.clicked_index: int | None = None
        self.last_index: int | None = None
        #: A pinned panel ignores the pointer entirely - that is what the dock's
        #: own 固定面板 switch has always meant, and it is how a panel stays up
        #: for a screenshot while the mouse is somewhere else.
        self.sticky = False

    # -- queries ------------------------------------------------------------
    @property
    def level(self) -> Level:
        if self.panel.is_open:
            return Level.PANEL
        if self.peek.is_open:
            return Level.PEEK
        return Level.HIDDEN

    @property
    def index(self) -> int | None:
        return self.clicked_index if self.clicked_index is not None else self.last_index

    @property
    def clicked(self) -> bool:
        return self.clicked_index is not None

    # -- events -------------------------------------------------------------
    def pointer_entered(self, index: int | None = None,
                        now: float | None = None) -> float | None:
        """The pointer is over the window; index is the mark under it, if any.

        A mark arms both machines. Empty space inside the window keeps a line or
        a panel that is already up, but it must never arm the rest timer - the
        pointer crossing the dock on its way somewhere else would then open the
        panel the moment it touched a mark.
        """
        now = time.monotonic() if now is None else now
        if index is not None:
            self.last_index = index
            self.peek.reduce(Event.POINTER_ENTERED, now, self.peek_delay, self.collapse_delay)
            # The panel machine is deliberately NOT armed here. Arming it meant
            # resting on a mark for PANEL_REST_DELAY opened the whole card with
            # no click at all - the pointer only had to stop moving over an
            # icon. Hovering shows the line; a click, and only a click, asks for
            # the card. (Alt still force-opens it for a quick look.)
        else:
            if self.peek.state == State.COLLAPSED:
                self.peek.reduce(Event.POINTER_ENTERED, now, self.peek_delay,
                                 self.collapse_delay)
            if self.panel.state == State.OPENING:
                self.panel.reduce(Event.POINTER_EXITED, now)
        pending = [d for d in (self.peek.deadline, self.panel.deadline) if d is not None]
        return min(pending) if pending else None

    def pointer_exited(self, now: float | None = None) -> float | None:
        if self.sticky:
            return None
        now = time.monotonic() if now is None else now
        deadlines = [
            self.peek.reduce(Event.POINTER_EXITED, now, self.peek_delay, self.collapse_delay),
            self.panel.reduce(Event.POINTER_EXITED, now, self.hover_delay, self.collapse_delay),
        ]
        pending = [d for d in deadlines if d is not None]
        return min(pending) if pending else None

    def tick(self, now: float | None = None) -> float | None:
        if self.sticky:
            return None
        now = time.monotonic() if now is None else now
        deadlines = [
            self.peek.reduce(Event.TIMER_FIRED, now, self.peek_delay, self.collapse_delay),
            self.panel.reduce(Event.TIMER_FIRED, now, self.hover_delay, self.collapse_delay),
        ]
        if self.panel.state == State.COLLAPSED:
            self.clicked_index = None
        pending = [d for d in deadlines if d is not None]
        return min(pending) if pending else None

    def click(self, index: int | None) -> None:
        """A click on a mark opens that agent's whole panel; clicking it again closes."""
        if index is None:
            return
        if self.clicked_index == index:
            self.collapse()
            return
        self.clicked_index = index
        self.last_index = index
        self.panel.reduce(Event.FORCE_OPEN)
        self.peek.reduce(Event.FORCE_OPEN)

    def open_panel(self, index: int | None = None) -> None:
        if index is not None:
            self.last_index = index
        self.panel.reduce(Event.FORCE_OPEN)
        self.peek.reduce(Event.FORCE_OPEN)

    def collapse(self) -> None:
        self.clicked_index = None
        self.panel.reduce(Event.FORCE_COLLAPSE)
        self.peek.reduce(Event.FORCE_COLLAPSE)
