"""Keep the Agent HUD running.

Starts hud.py with the same interpreter, waits for it, records why it exited
(the child's exit code plus whatever it wrote to stderr) and starts it again -
unless the HUD itself was asked to quit from its own menu. A HUD that keeps
dying instantly backs off instead of spinning.

    pythonw watchdog.py      normal (this is what run.vbs launches)
    python  watchdog.py -v   same, but the child shares this console
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
QUIT_FLAG = HERE / ".hud-quit"
ERR_LOG = HERE / "hud.stderr.log"
LOG = HERE / "hud.log"

CREATE_NO_WINDOW = 0x08000000
FAST_EXIT_SECONDS = 20.0
BACKOFF_AFTER = 3
BACKOFF_SECONDS = 60.0


def log(message: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except Exception:
        pass


def interpreter() -> str:
    """A windowless Python: pythonw beside whichever interpreter runs us."""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose = "-v" in argv
    pythonw = interpreter()
    QUIT_FLAG.unlink(missing_ok=True)
    log("watchdog: supervising hud.py with %s" % pythonw)
    consecutive_fast = 0
    while True:
        started = time.time()
        try:
            handle = None if verbose else open(ERR_LOG, "ab")
            try:
                if handle is not None:
                    handle.write(("\n--- hud start %s ---\n"
                                  % time.strftime("%Y-%m-%d %H:%M:%S")).encode())
                    handle.flush()
                proc = subprocess.Popen(
                    [pythonw, str(HERE / "hud.py")], cwd=str(HERE),
                    stdout=None if verbose else subprocess.DEVNULL,
                    stderr=None if verbose else handle,
                    creationflags=0 if verbose else CREATE_NO_WINDOW)
                code = proc.wait()
            finally:
                if handle is not None:
                    handle.close()
        except Exception as exc:
            log("watchdog: could not start hud.py: %r" % exc)
            code = -1
        lived = time.time() - started

        if QUIT_FLAG.exists():
            QUIT_FLAG.unlink(missing_ok=True)
            log("watchdog: hud.py was asked to quit, stopping")
            return 0

        consecutive_fast = consecutive_fast + 1 if lived < FAST_EXIT_SECONDS else 0
        if consecutive_fast >= BACKOFF_AFTER:
            log("watchdog: hud.py exited after %.0fs (code %s), backing off %.0fs"
                % (lived, code, BACKOFF_SECONDS))
            deadline = time.time() + BACKOFF_SECONDS
            while time.time() < deadline:
                if QUIT_FLAG.exists():
                    QUIT_FLAG.unlink(missing_ok=True)
                    return 0
                time.sleep(0.5)
            consecutive_fast = 0
            continue
        log("watchdog: hud.py exited after %.0fs (code %s), restarting" % (lived, code))
        time.sleep(2.0)


if __name__ == "__main__":
    raise SystemExit(main())
