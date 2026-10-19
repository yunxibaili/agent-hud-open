"""Agent HUD (Windows) - configuration.

Deliberately small: one JSON file next to the code, deep-merged over the
defaults below.  Everything a user may need to override by hand (paths,
fallback WorkBuddy tokens, refresh cadence) lives here.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
LOG_PATH = ROOT / "hud.log"

DEFAULTS = {
    "refresh_seconds": 120,
    "retry_seconds": 30,       # a failed reading is retried this much sooner
    "stagger_seconds": 0.4,
    "panel": {
        "opacity": 0.96,
        "rounded": True,
        "x": None,             # None -> centre horizontally on the primary screen
        "y": 10,
        "scale": 1.0,
        "always_on_top": True,
        "collapsed_seconds": 0.25,
    },
    "dock": {
        "mode": "dock",          # dock | notch (glued to the top of the screen)
        "edge": "top",           # top | bottom | left | right
        "offset": 0.5,           # 0 = start of the edge, 1 = end
        "inset": 3,              # distance kept from the screen edge
        "show_percent": False,   # percentages beside the marks
        "style": "ring",         # ring | glow
        "edge_peek": True,       # retreat until summoned
        "peek_sliver": 7,        # pixels of the capsule left visible while tucked away
        "hover_delay": 1.6,      # seconds resting on a mark before the panel opens
        "peek_delay": 0.1,       # seconds before the one-line hover capsule appears
        "collapse_delay": 0.25,  # debounce before everything slides away
    },
    "providers": {
        "codex": {"enabled": True, "executable": None, "timeout": 30},
        "opencode": {"enabled": True},
        "antigravity": {"enabled": True, "timeout": 20},
        "workbuddy": {
            "enabled": True,
            "electron_paths": [],   # %LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe is the default
            "tokens": {},           # manual fallback, see docs/README.windows.md
        },
        "dsh": {"enabled": True, "homes": []},
        "claude": {"enabled": True},
        "codebuddy": {"enabled": True},
        "pi": {"enabled": True},
        "kimi": {"enabled": True},
        "qwen": {"enabled": True},
        "openclaw": {"enabled": True},
        "devin": {"enabled": True},
        "hermes": {"enabled": True},
        "zcode": {"enabled": True},
        "copilot": {"enabled": True},
        "grok": {"enabled": True},
    },
    "currency": {
        "usd_cny": 7.14,         # CNY per 1 USD; every cost is shown in dollars
    },
    "quota_warning_used": 70,
    "quota_critical_used": 90,
    "show_zero_windows": False,
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    raw = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
    return _merge(DEFAULTS, raw)


def save(cfg: dict, path: Path | None = None) -> None:
    path = path or CONFIG_PATH
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def ensure_file() -> Path:
    """Create config.json with the defaults on first run so it can be edited."""
    if not CONFIG_PATH.exists():
        save(DEFAULTS)
    return CONFIG_PATH


def home(*parts: str) -> Path:
    return Path(os.path.expanduser("~")).joinpath(*parts)
