"""Offline checks for the parts that cannot be exercised without a live client:
the Antigravity parsers, the docking geometry on screens other than this one,
and the three-level hover, replayed with explicit timestamps."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import providers.antigravity as ag  # noqa: E402

from PySide6.QtCore import QPoint, QRect, QSize  # noqa: E402

import hover  # noqa: E402
import placement as geo  # noqa: E402

FAIL = []

#: Displays a dock may be asked to park on. The negative origins are the ones
#: that catch geometry written as if every screen started at (0, 0).
SCREENS = [QRect(0, 0, 2560, 1440), QRect(0, 0, 1920, 1080), QRect(0, 0, 3840, 2160),
           QRect(0, 0, 1366, 768), QRect(0, 0, 800, 600), QRect(-1920, 0, 1920, 1080),
           QRect(0, -1080, 2560, 1080)]
STRIP = QSize(199, 42)
TALL = QSize(42, 199)


def check_hover() -> None:
    """The three levels, driven with explicit timestamps instead of a mouse.

    HoverMachine takes its current time as an argument for exactly this reason:
    the interaction can be replayed deterministically, so "hover peeks, a click
    opens, leaving collapses" is a regression test and not a manual ritual.
    """
    print("-- Three-level hover (deterministic replay)")
    peek_delay, rest_delay, collapse = 0.10, 1.6, 0.25
    HIDDEN, PEEK, PANEL = hover.Level.HIDDEN, hover.Level.PEEK, hover.Level.PANEL

    levels = hover.DockLevels(rest_delay, collapse, peek_delay)
    t = 1000.0
    check("starts at level 1", levels.level, HIDDEN)

    levels.pointer_entered(0, t)
    check("a mark hovered does not open anything at once", levels.level, HIDDEN)
    t += peek_delay
    levels.tick(t)
    check("level 2 after the peek delay", levels.level, PEEK)

    levels.pointer_entered(2, t)
    check("the line follows the pointer to another mark", levels.index, 2)
    t += rest_delay
    levels.tick(t)
    # Resting is not asking. Sitting on a mark shows the line and nothing more;
    # the card is opened by a click, and only by a click.
    check("resting on a mark still only shows the line", levels.level, PEEK)

    levels.click(2)
    levels.tick(t)
    check("a click is what opens the card", levels.level, PANEL)

    t += 0.05
    levels.pointer_exited(t)
    check("the panel survives inside the collapse debounce", levels.level, PANEL)
    t += collapse - 0.06
    levels.tick(t)
    check("still open a hair before the debounce ends", levels.level, PANEL)
    closing_deadline = levels.panel.deadline
    if closing_deadline is None:
        closing_deadline = t
    levels.tick(closing_deadline + 0.001)
    check("everything collapses once the debounce deadline passes",
          levels.level, HIDDEN)

    # Empty space inside the window must never arm the rest timer, or crossing
    # the dock on the way somewhere else would open the panel on first touch.
    idle = hover.DockLevels(rest_delay, collapse, peek_delay)
    t2 = 2000.0
    idle.pointer_entered(None, t2)
    t2 += rest_delay + 1.0
    idle.tick(t2)
    check("hovering empty dock space never opens the panel", idle.panel.is_open, False)
    idle.pointer_entered(0, t2)
    check("touching a mark right after still does not", idle.panel.is_open, False)
    idle.tick(t2 + peek_delay)
    check("...it only comes up as level 2", idle.level, PEEK)

    # A click is immediate and toggles.
    clicked = hover.DockLevels(rest_delay, collapse, peek_delay)
    clicked.click(1)
    check("a click opens the panel immediately", clicked.level, PANEL)
    check("a click focuses that mark", clicked.index, 1)
    clicked.click(1)
    check("clicking the same mark closes it", clicked.level, HIDDEN)

    # Alt-hover is forceOpen on the same machine.
    forced = hover.DockLevels(rest_delay, collapse, peek_delay)
    forced.open_panel(3)
    check("a forced open lands on level 3", forced.level, PANEL)

    # An early timer must keep its deadline, or the hover waits forever.
    machine = hover.HoverMachine()
    deadline = machine.reduce(hover.Event.POINTER_ENTERED, 3000.0, 1.0, 0.2)
    early = machine.reduce(hover.Event.TIMER_FIRED, 3000.2)
    check("an early timer keeps its deadline",
          (machine.state, early), (hover.State.OPENING, deadline))
    machine.reduce(hover.Event.TIMER_FIRED, deadline + 0.01)
    check("the deadline that arrives opens it", machine.state, hover.State.OPEN)
    machine.reduce(hover.Event.POINTER_EXITED, 4000.0)
    check("leaving starts closing", machine.state, hover.State.CLOSING)
    machine.reduce(hover.Event.POINTER_ENTERED, 4000.1)
    check("coming back cancels the close", machine.state, hover.State.OPEN)


def check_placement() -> None:
    """The strip must stay on screen, reachable and peeking by exactly 3px."""
    print("-- Docking geometry on every screen shape")
    for frame in SCREENS:
        label = "(%d,%d %dx%d)" % (frame.left(), frame.top(), frame.width(), frame.height())
        for edge in geo.Edge:
            for mode in (geo.Mode.DOCK, geo.Mode.NOTCH):
                if mode == geo.Mode.NOTCH and edge != geo.Edge.TOP:
                    continue
                size = STRIP if edge.is_horizontal else TALL
                tag = "%s %s/%s" % (label, mode.value, edge.value)
                for offset in (0.0, 0.25, 0.5, 0.75, 1.0):
                    placement = geo.Placement(mode=mode, edge=edge, offset=offset, inset=3)
                    strip = geo.strip_rect(size, frame, placement)
                    if not frame.contains(strip):
                        check(tag + " offset %.2f on screen" % offset, False, True)
                        continue
                    hot = geo.edge_hot_zone(strip, frame, edge)
                    if hot.intersected(frame).isEmpty():
                        check(tag + " hot zone on screen", False, True)
                    if mode == geo.Mode.NOTCH and strip.top() != frame.top():
                        check(tag + " notch flush", False, True)
                    slid = strip.translated(geo.hidden_offset(strip, frame, edge))
                    if edge.is_horizontal:
                        visible = max(0, min(slid.bottom(), frame.bottom())
                                      - max(slid.top(), frame.top()) + 1)
                    else:
                        visible = max(0, min(slid.right(), frame.right())
                                      - max(slid.left(), frame.left()) + 1)
                    if visible != geo.PEEK_SLIVER:
                        check(tag + " offset %.2f sliver" % offset, visible,
                              geo.PEEK_SLIVER)
    check("placement: every screen/edge/offset keeps the dock usable", True, True)

    print("-- Docking geometry: drag snapping")
    cases = [
        (QRect(0, 0, 2560, 1440), QPoint(1280, 4), geo.Edge.TOP),
        (QRect(0, 0, 2560, 1440), QPoint(1280, 1436), geo.Edge.BOTTOM),
        (QRect(0, 0, 2560, 1440), QPoint(1200, 1300), geo.Edge.BOTTOM),
        (QRect(0, 0, 2560, 1440), QPoint(4, 720), geo.Edge.LEFT),
        (QRect(0, 0, 2560, 1440), QPoint(2556, 720), geo.Edge.RIGHT),
        (QRect(-1920, 0, 1920, 1080), QPoint(-1850, 540), geo.Edge.LEFT),
        (QRect(-1920, 0, 1920, 1080), QPoint(-60, 540), geo.Edge.RIGHT),
        (QRect(-1920, 0, 1920, 1080), QPoint(-960, 4), geo.Edge.TOP),
        (QRect(0, -1080, 2560, 1080), QPoint(1280, -1076), geo.Edge.TOP),
    ]
    for frame, point, want in cases:
        start = geo.Placement(mode=geo.Mode.DOCK, edge=geo.Edge.RIGHT, offset=0.5, inset=3)
        got = geo.drag_placement(point, frame, STRIP, start).edge
        check("drag %s in %s -> %s" % (point, frame.getRect(), want.value), got, want)
    notch = geo.Placement(mode=geo.Mode.NOTCH, edge=geo.Edge.TOP, offset=0.5, inset=0)
    dragged = geo.drag_placement(QPoint(1200, 1300), QRect(0, 0, 2560, 1440), STRIP, notch)
    check("a notch dragged inward becomes a dock",
          (dragged.mode, dragged.edge), (geo.Mode.DOCK, geo.Edge.BOTTOM))


def check_money() -> None:
    """Every cost is a dollar figure, at one configured rate."""
    print("-- Money: one currency in every figure")
    import money
    money.set_rate(7.0)
    check("a CNY cost converts", round(money.to_usd(70.0, "CNY"), 4), 10.0)
    check("a USD cost passes through", money.to_usd(1.7, "USD"), 1.7)
    check("an unknown currency is not guessed at", money.to_usd(5.0, "JPY"), None)
    check("a missing amount stays missing", money.to_usd(None, "CNY"), None)
    check("the phrase", money.usd(7.0, "CNY"), "≈$1.00")
    check("a slot figure", money.compact(70.0, "CNY"), "$10.0")
    check("a big slot figure", money.compact(70000.0, "CNY"), "$10.0k")
    money.set_rate(0)
    check("a bad rate is refused", money.rate(), 7.0)
    money.set_rate(7.14)


def check_costs() -> None:
    """The totals card adds mixed currencies up, and shows one symbol."""
    print("-- Costs: a single dollar total")
    import money
    import panel
    money.set_rate(7.14)

    class Reading:
        def __init__(self, cost, currency):
            self.today_cost = cost
            self.cost_currency = currency

    entries = [("DeepSeek Harness", None, Reading(71.4, "CNY")),
               ("OpenCode", None, Reading(1.7, "USD"))]
    text = panel.cost_text(entries)
    check("one dollar sign only", text.count("$"), 1)
    check("no second currency", text.count("+"), 0)
    check("both folded in", text, "≈$11.70")
    check("nothing priced", panel.cost_text([("A", None, Reading(None, "CNY"))]),
          "无本地计价")


def check_ring_art() -> None:
    """The ring is a watch ring: it lights up into its own head."""
    print("-- Ring art: conical gradient and an end cap")
    import math
    from PySide6.QtGui import QColor
    from PySide6.QtCore import QRectF
    import dock

    bar = dock.DockBar.__new__(dock.DockBar)      # only the maths is under test
    ring = QRectF(0.0, 0.0, 28.0, 28.0)
    colour = QColor("#3DDC84")

    gradient = bar._conical(ring, colour, 0.25)
    stops = gradient.stops()
    check("two stops", len(stops), 2)
    tail = [c for position, c in stops if abs(position - 0.0) < 1e-6][0]
    head = [c for position, c in stops if abs(position - 0.75) < 1e-6][0]
    check("the head is the full colour",
          (head.red(), head.green(), head.blue()),
          (colour.red(), colour.green(), colour.blue()))
    check("the tail is dimmed", tail.value() < head.value(), True)
    check("a full ring needs no ramp",
          len(bar._conical(ring, colour, 1.0).stops()), 2)

    # The arc is drawn from twelve o'clock going clockwise, and the end cap has
    # to sit where it stops: the two share this convention, so pin it down.
    def cap(share):
        theta = math.radians(90.0 - 360.0 * share)
        return (round(math.cos(theta)), round(-math.sin(theta)))

    check("nothing swept caps at twelve", cap(0.0), (0, -1))
    check("a quarter turn caps at three", cap(0.25), (1, 0))
    check("half a turn caps at six", cap(0.50), (0, 1))
    check("three quarters cap at nine", cap(0.75), (-1, 0))

    # The ring shows what is LEFT, so a spent window draws almost nothing.
    import model
    report = model.ProviderReport(pid="codex", name="Codex", mark="CX")
    report.ok = True
    report.windows = [model.QuotaWindow(key="5h", label="5h", short="5h",
                                        used_percent=90.0)]
    check("a spent window leaves a tenth", round(bar.share_of(report), 3), 0.1)


def check_hover_line() -> None:
    """The one-line capsule never elides the label it was asked to carry."""
    print("-- Hover line: it grows at once and shrinks smoothly")
    try:
        from PySide6.QtWidgets import QApplication
    except Exception:
        return
    import ui_theme as theme
    import peek
    app = QApplication.instance() or QApplication([])
    theme.resolve_fonts()

    short = [peek.Segment("Codex", theme.title(11.5), theme.TEXT)]
    long = [peek.Segment("OpenCode", theme.title(11.5), theme.TEXT),
            peek.Segment("5h", theme.ui(10.5), theme.TEXT_DIM),
            peek.Segment("7% (2h 07m)", theme.numeric(10.5), theme.TEXT),
            peek.Segment("7d", theme.ui(10.5), theme.TEXT_DIM),
            peek.Segment("40% (4d 7h)", theme.numeric(10.5), theme.TEXT),
            peek.Segment("30d", theme.ui(10.5), theme.TEXT_DIM),
            peek.Segment("88% (5d 11h)", theme.numeric(10.5), theme.TEXT)]

    pill = peek.LinePill(42)
    pill.set_segments(short, animate=False)
    narrow = pill.natural_width()
    pill.set_segments(long, animate=True)
    wide = pill.natural_width()
    check("a longer label is wider", wide > narrow, True)
    check("and it is drawn at once, not stretched into",
          round(pill.painted_width()), wide)
    pill.set_segments(short, animate=True)
    check("a shorter label still fits while it shrinks",
          pill.painted_width() >= pill.natural_width(), True)


def check_fullscreen_standdown() -> None:
    """A fullscreen game in front means the HUD gets out of the way."""
    print("-- Fullscreen: the HUD stands down over a game")
    import inspect
    import hud

    probe = inspect.getsource(hud.foreground_is_fullscreen)
    check("the shell is never taken for a game",
          "SHELL_CLASSES" in probe
          and "Progman" in hud.SHELL_CLASSES
          and "WorkerTrayWnd" not in hud.SHELL_CLASSES
          and "Shell_TrayWnd" in hud.SHELL_CLASSES, True)
    check("it measures the whole screen, not the work area",
          "screen.geometry()" in probe, True)
    tick = inspect.getsource(hud.HudWindow._hover_tick_inner)
    check("the hover tick stands down", "_game_in_front" in tick, True)
    check("and takes the edge hot zone with it",
          tick.index("_game_in_front") < tick.index("_sliver_rect"), True)
    check("a reminder will not pop over one",
          "game_in_front" in inspect.getsource(hud.HudWindow._show_reminder),
          True)
    check("the probe is throttled",
          "_game_probe_at" in inspect.getsource(hud.HudWindow._game_in_front),
          True)


def check_motion() -> None:
    """The durations and curves the motion is specified in."""
    print("-- Motion: the specified timings")
    from PySide6.QtCore import QEasingCurve
    import inspect
    import dock
    import hud
    import motion
    import panel
    import peek

    check("the capsule unfolds in 420ms", dock.BOOT_MS, 420)
    check("slots are staggered by 70ms", dock.STAGGER_MS, 70)
    check("the first sweep is 650ms", dock.SWEEP_MS, 650)
    check("later readings tween in 500ms", dock.TWEEN_MS, 500)
    check("the magnet is 1.08 over an 0.78 backdrop",
          (dock.HOVER_SCALE, dock.HOVER_DIM), (1.08, 0.78))
    check("and takes 150ms", dock.HOVER_MS, 150)
    check("the refresh chase is 600ms", dock.FLOW_MS, 600)
    check("the breath is 3600ms idle and 1200ms busy",
          (dock.BREATH_MS, dock.BREATH_BUSY_MS), (3600.0, 1200.0))
    check("the line morphs in 140ms", peek.MORPH_MS, 140)
    check("the panel opens in 240ms and shuts in 160ms",
          (hud.PANEL_OPEN_MS, hud.PANEL_CLOSE_MS), (240, 160))
    check("the tuck is 300ms and the wake 220ms",
          (hud.TUCK_MS, hud.WAKE_MS), (300, 220))
    check("animation frames are 16ms, the breath 66ms",
          (hud.FRAME_ACTIVE_MS, hud.FRAME_AMBIENT_MS), (16, 66))
    check("bars grow in 380ms",
          inspect.signature(panel.ProgressBar.grow).parameters["ms"].default, 380)
    check("charts grow in 320ms",
          inspect.signature(panel.TokenStrip.grow).parameters["ms"].default, 320)
    check("the entrance springs", motion.OUT_BACK is QEasingCurve.OutBack, True)
    check("the sweep eases out", motion.OUT_CUBIC is QEasingCurve.OutCubic, True)
    check("the tuck eases both ways",
          motion.IN_OUT_CUBIC is QEasingCurve.InOutCubic, True)
    check("the breath is a sine", motion.IN_OUT_SINE is QEasingCurve.InOutSine,
          True)


def check_wake_sweep() -> None:
    """Coming out from behind the edge runs every ring up from nothing."""
    print("-- Wake sweep: zero to the real figure, clockwise")
    import dock
    import model
    import ui_theme as theme

    check("slots start 35ms apart", dock.WAKE_STAGGER_MS, 35)
    check("and each takes 480ms", dock.WAKE_SWEEP_MS, 480)
    check("the glowing end cap is gone", hasattr(dock.DockBar, "_end_cap"), False)

    # The arc convention the sweep is drawn with: twelve o'clock, clockwise.
    import inspect
    body = inspect.getsource(dock.DockBar._draw_ring)
    check("it starts at twelve o'clock", "90 * 16" in body, True)
    check("and sweeps clockwise", "share))" in body and "-int(round(" in body, True)

    try:
        from PySide6.QtWidgets import QApplication
    except Exception:
        return                      # no display: the constants above still hold
    app = QApplication.instance() or QApplication([])
    theme.resolve_fonts()

    used = {"codex": 38, "opencode": 86}
    bar = dock.DockBar(theme.Marks())
    bar.set_edge("top", False)
    bar.set_presentation(True, True, "ring")
    bar.expected = 2
    reports = []
    for pid, percent in used.items():
        report = model.ProviderReport(pid=pid, name=pid, mark="XX")
        report.ok = True
        report.updated_at = 1
        report.windows = [model.QuotaWindow(key="5h", label="5h", short="5h",
                                            used_percent=percent)]
        reports.append(report)
    bar.set_reports(reports, 70, 90)
    # The first reading queues its own sweep; fire it, then land the rings on
    # their figures so the wake below starts from a settled dock.
    bar.advance(bar._sweeps[-1][0] + 0.001, 0.0, 0.0)
    for pid in used:
        bar.slots[pid].loading = False
        bar.slots[pid].snap("sweep", bar.share_of_report(pid) or 0.0)

    settled = {pid: bar.slots[pid].sweep for pid in used}
    check("rings sit at their remaining figures when idle",
          {pid: round(v, 2) for pid, v in settled.items()},
          {"codex": 0.62, "opencode": 0.14})

    bar.wake_sweep()
    check("the wake resets every ring to zero",
          {pid: round(bar.slots[pid].sweep, 3) for pid in used},
          {"codex": 0.0, "opencode": 0.0})
    check("one queued run per online agent", len(bar._sweeps), 2)
    check("they carry the wake duration",
          sorted({row[3] for row in bar._sweeps}), [dock.WAKE_SWEEP_MS])
    gaps = [round(bar._sweeps[i + 1][0] - bar._sweeps[i][0], 3)
            for i in range(len(bar._sweeps) - 1)]
    check("staggered by exactly 35ms", gaps, [0.035])

    # driven past every due time, each ring must land on its own figure
    bar.advance(bar._sweeps[-1][0] + 0.001, 0.0, 0.0)
    check("nothing is left queued", len(bar._sweeps), 0)

    # the figure beside a mark counts up with the ring, and a balance does not
    report = reports[0]
    slot = bar.slots["codex"]
    slot.snap("sweep", 0.0)
    check("the number starts at zero", bar.slot_percent(report, slot), "0%")
    slot.snap("sweep", 0.31)
    check("it counts up with the arc", bar.slot_percent(report, slot), "31%")
    slot.snap("sweep", 0.62)
    check("and settles on what is left", bar.slot_percent(report, slot), "62%")
    balance = model.ProviderReport(pid="dsh", name="DSH", mark="DS")
    balance.ok = True
    balance.raw_balance = 142.0
    balance.balance_currency = "CNY"
    check("a balance is not counted up",
          bar.slot_percent(balance, slot), None)

    # the wake is wired to the hidden -> visible edge and nothing else
    import inspect as _inspect
    import hud
    reveal = _inspect.getsource(hud.HudWindow._reveal)
    check("an unchanged state returns early",
          "if self.revealed == revealed:" in reveal, True)
    check("the sweep sits inside the waking branch",
          reveal.index("wake_sweep") > reveal.index("if revealed:"), True)
    refresh = _inspect.getsource(hud.HudWindow._manual_refresh)
    check("a manual refresh sweeps too", "wake_sweep" in refresh, True)


def check_assertions() -> None:
    """The invariants the animation code leans on."""
    print("-- Motion: invariants")
    import motion
    from PySide6.QtCore import QObject

    owner = QObject()
    seen = []
    scalar = motion.Scalar(owner, lambda: seen.append(1), ("value",))
    check("a scalar starts at zero", scalar.value("value"), 0.0)
    scalar.snap("value", 0.4)
    check("snapping is immediate", scalar.value("value"), 0.4)
    check("nothing is running when still", scalar.running(), 0)


def check(name, got, want):
    ok = got == want
    print("%-4s %s" % ("ok" if ok else "FAIL", name))
    if not ok:
        print("     got  %r" % (got,))
        print("     want %r" % (want,))
        FAIL.append(name)


def main() -> int:
    print("-- Antigravity: process candidates")
    lines = [
        {"ProcessId": 700, "Name": "language_server_windows_x64.exe",
         "CommandLine": r'C:\Users\x\AppData\Local\Programs\Antigravity\language_server_windows_x64.exe '
                        r'--app_data_dir antigravity --csrf_token 8f2b-token --extension_server_port 51999'},
        {"ProcessId": 900, "Name": "agy.exe",
         "CommandLine": r'C:\tools\antigravity-cli\agy.exe --csrf_token "quoted token"'},
        {"ProcessId": 42, "Name": "notepad.exe", "CommandLine": "notepad.exe"},
    ]
    found = ag.candidates_from_processes(lines)
    check("keeps two services", len(found), 2)
    # The original sorts plain servers (0) before agy CLI (1) before the IDE (2).
    check("server first", found[0]["pid"], 700)
    check("server token", found[0]["token"], "8f2b-token")
    check("extension port", found[0]["extension_port"], 51999)
    check("cli second", found[1]["pid"], 900)
    check("quoted token unwrapped", found[1]["token"], "quoted token")

    print("-- Antigravity: RetrieveUserQuotaSummary")
    document = {"response": {"groups": [
        {"displayName": "Gemini Models", "buckets": [
            {"bucketId": "gemini-5h", "displayName": "Five Hour Limit",
             "remainingFraction": 0.5, "resetTime": "2026-10-14T08:10:58Z"},
            {"bucketId": "gemini-7d", "displayName": "Weekly Limit", "remainingFraction": 0.75}]},
        {"displayName": "Claude and GPT models", "buckets": [
            {"bucketId": "3p-5h", "displayName": "Five Hour Limit", "remainingFraction": 1.0},
            {"bucketId": "3p-7d", "displayName": "Weekly Limit", "remainingFraction": 0.9},
            {"bucketId": "disabled", "disabled": True, "remainingFraction": 0.1}]},
    ]}}
    windows, ids = ag.parse_summary(document)
    check("four enabled buckets, disabled dropped", len(windows), 4)
    check("allModels off with two groups", windows[0].all_models, False)
    check("gemini used 50%", round(windows[0].used_percent or -1, 2), 50.0)
    check("5h cadence", windows[0].duration_minutes, 300)
    check("weekly cadence", windows[1].duration_minutes, 10080)
    check("group word", windows[2].short, "第三方 5h")
    check("reset parsed", windows[0].reset_at is not None, True)

    print("-- Antigravity: GetUserStatus fallback")
    status = {"userStatus": {
        "email": "a@b.c", "teamId": "team-1",
        "userTier": {"name": "Pro"},
        "cascadeModelConfigData": {"clientModelConfigs": [
            {"label": "Gemini 3 Pro", "modelOrAlias": {"model": "gemini-3-pro"},
             "quotaInfo": {"remainingFraction": 0.5, "resetTime": "2026-10-14T08:10:58Z"}},
            {"label": "Claude Sonnet 5", "modelOrAlias": {"model": "claude-sonnet"},
             "quotaInfo": {"remainingFraction": 0.25}},
            {"label": "Gemini Flash Lite", "modelOrAlias": {"model": "gemini-flash-lite"},
             "quotaInfo": {"remainingFraction": 0.9}},
        ]}}}
    windows, email, team, plan = ag.parse_status(status)
    check("identity", (email, team, plan), ("a@b.c", "team-1", "Pro"))
    check("lite skipped, two families", len(windows), 2)
    check("worst gemini bucket wins", round([w for w in windows if w.short == "Gemini"][0].used_percent, 1), 50.0)
    check("third party word", sorted(w.short for w in windows), ["Gemini", "第三方"])

    print()
    check_hover()

    print()
    check_placement()

    print()
    check_money()

    print()
    check_costs()

    print()
    check_ring_art()

    print()
    check_motion()

    print()
    check_fullscreen_standdown()

    print()
    check_hover_line()

    print()
    check_wake_sweep()

    print()
    check_assertions()

    print()
    print("FAILED: %d" % len(FAIL) if FAIL else "all offline checks passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
