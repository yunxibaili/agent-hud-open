"""Animation primitives for the HUD.

Everything that moves is driven by one of three things, and nothing else:

* a QVariantAnimation for a discrete change - a boot sweep, a new percentage,
  the pull of the pointer onto a mark. Qt runs all of them off a single internal
  timer and they stop themselves the moment they land, so a still dock costs
  nothing at all;
* a QPropertyAnimation on a QGraphicsOpacityEffect for a fade, which installs
  the effect for the length of the transition and then takes it straight back
  off again so steady-state painting is never routed through a texture;
* one frame timer in the window for the slow ambient breath, which drops to a
  low rate when nothing is happening and stops entirely when the dock is away.

Easing is named here rather than imported everywhere, so the shape of the
motion is one table instead of a hundred call sites.
"""

from __future__ import annotations

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QObject,
                            QPropertyAnimation, QVariantAnimation)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

# The curves the HUD uses, in one place.
OUT_BACK = QEasingCurve.OutBack          # a spring with a small overshoot
OUT_CUBIC = QEasingCurve.OutCubic
OUT_QUART = QEasingCurve.OutQuart
OUT_QUAD = QEasingCurve.OutQuad
IN_CUBIC = QEasingCurve.InCubic
IN_OUT_CUBIC = QEasingCurve.InOutCubic
IN_OUT_SINE = QEasingCurve.InOutSine
LINEAR = QEasingCurve.Linear


def settle(owner) -> None:
    """Land every animation the owner is running on its final value.

    Used when the system preference changes mid-flight: whatever was travelling
    is stopped and written to where it was going, so nothing is left half way
    and no running state survives the switch.
    """
    for anim in list(getattr(owner, "_anims", {}).values()):
        if anim.state() != QAbstractAnimation.Stopped:
            anim.stop()
            anim.setCurrentTime(anim.duration())


def dur(ms: int) -> int:
    """How long an animation may take, given the system preference.

    One policy, applied where the animations are built rather than at every
    call site, so a reduced-motion setting cannot be half-honoured. Zero means
    snap: the caller still reaches the right final state, it just does not
    travel there.
    """
    return 0 if REDUCED else int(ms)


def running(animation) -> bool:
    return (animation is not None
            and animation.state() == QAbstractAnimation.Running)


class Scalar(QObject):
    """A float that can be sent to a new value over time.

    The animation object is built once per name and re-used, so a slot that
    updates every two minutes does not allocate a new animation each time.
    """

    def __init__(self, owner: QObject, changed, names=()):
        super().__init__(owner)
        self._changed = changed
        self._values: dict[str, float] = {name: 0.0 for name in names}
        self._anims: dict[str, QVariantAnimation] = {}

    def value(self, name: str) -> float:
        return self._values.get(name, 0.0)

    def set(self, name: str, value: float) -> None:
        self._values[name] = float(value)
        self._changed()

    def to(self, name: str, value: float, ms: int, curve=OUT_CUBIC) -> None:
        if REDUCED:
            # Straight to the final value - and the animation that may already
            # be running is stopped first, or its next frame would write the old
            # value straight back over the top of the final one.
            anim = self._anims.get(name)
            if anim is not None:
                anim.stop()
            self.set(name, value)
            return
        anim = self._anims.get(name)
        if anim is None:
            anim = QVariantAnimation(self)
            anim.valueChanged.connect(lambda v, key=name: self.set(key, v))
            anim.finished.connect(self._changed)
            self._anims[name] = anim
        anim.stop()
        anim.setStartValue(float(self._values.get(name, 0.0)))
        anim.setEndValue(float(value))
        anim.setDuration(max(1, int(ms)))
        anim.setEasingCurve(curve)
        anim.start()

    def snap(self, name: str, value: float) -> None:
        anim = self._anims.get(name)
        if anim is not None:
            anim.stop()
        self.set(name, value)

    def running(self) -> int:
        return sum(1 for a in self._anims.values() if running(a))

    def stop(self) -> None:
        for anim in self._anims.values():
            anim.stop()


def animate(owner, start, end, ms, curve, on_value) -> QVariantAnimation:
    """One throwaway QVariantAnimation; it deletes itself when it finishes."""
    anim = QVariantAnimation(owner)
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setDuration(max(1, int(ms)))
    anim.setEasingCurve(curve)
    anim.valueChanged.connect(on_value)
    anim.start(QAbstractAnimation.DeleteWhenStopped)
    return anim


def fade(widget: QWidget, start: float, end: float, ms: int, curve,
         on_done=None) -> QPropertyAnimation:
    """Fade a whole widget, then remove the effect so painting stays plain.

    A QGraphicsOpacityEffect renders the subtree through an offscreen buffer, so
    it is installed for the transition only and taken off again the moment the
    animation lands. A widget left with the effect would pay for it forever.
    """
    effect = widget.graphicsEffect()
    if not isinstance(effect, QGraphicsOpacityEffect):
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(float(start))
        widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setStartValue(float(start))
    anim.setEndValue(float(end))
    anim.setDuration(max(1, int(ms)))
    anim.setEasingCurve(curve)

    def _done():
        widget.setGraphicsEffect(None)
        if on_done is not None:
            on_done()

    anim.finished.connect(_done)
    anim.start(QAbstractAnimation.DeleteWhenStopped)
    return anim


# ---------------------------------------------------------------------------
# Reduced motion.

#: Set once at start-up from the system setting. Every animation that is merely
#: decorative asks this first; anything that conveys a state change keeps a very
#: short fade instead of a slide.
REDUCED = False

#: SPI_GETCLIENTAREAANIMATION - the same switch Windows uses for its own menus
#: and window effects, so the HUD follows the setting the user already made.
SPI_GETCLIENTAREAANIMATION = 0x1042


def detect_reduced() -> bool:
    """Read the system animation preference. Failure means "animate normally"."""
    global REDUCED
    try:
        import ctypes
        value = ctypes.c_int(1)
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(value), 0)
        REDUCED = bool(ok) and not bool(value.value)
    except Exception:
        REDUCED = False          # never let a failed probe change behaviour
    return REDUCED
