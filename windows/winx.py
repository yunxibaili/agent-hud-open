"""Windows plumbing: hidden child processes, paths, TCP table and a tray icon.

Everything here is ctypes + stdlib so the HUD needs no third-party package
except (optionally) Pillow, which is only ever used as a nicety.
"""

from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import struct
import sys
import threading
from ctypes import wintypes as wt
from pathlib import Path

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #

def env_path(name: str, default: Path | None = None) -> Path | None:
    value = os.environ.get(name)
    if value:
        return Path(os.path.expandvars(value))
    return default


def userprofile() -> Path:
    return env_path("USERPROFILE", Path(os.path.expanduser("~")))


def localappdata() -> Path:
    return env_path("LOCALAPPDATA", userprofile() / "AppData" / "Local")


def appdata() -> Path:
    return env_path("APPDATA", userprofile() / "AppData" / "Roaming")


def every_existing(paths) -> list[Path]:
    seen, out = set(), []
    for path in paths:
        if path is None:
            continue
        try:
            key = str(path).lower()
        except Exception:
            continue
        if key in seen or not path.exists():
            continue
        seen.add(key)
        out.append(path)
    return out


# --------------------------------------------------------------------------- #
# child processes
# --------------------------------------------------------------------------- #

def popen(args, **kwargs):
    """Popen that never flashes a console window on Windows."""
    if IS_WINDOWS:
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kwargs.setdefault("startupinfo", si)
    return subprocess.Popen(args, **kwargs)


def run_hidden(args, timeout: float = 20, env: dict | None = None, cwd: str | None = None,
               text: bool = True) -> tuple[int, str, str]:
    """Run a command to completion with no window, returning (rc, stdout, stderr)."""
    try:
        proc = popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     stdin=subprocess.DEVNULL, env=env, cwd=cwd)
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        rc = proc.returncode
        decode = (lambda b: b.decode("utf-8", "replace")) if text else (lambda b: b)
        return rc if rc is not None else -1, decode(out or b""), decode(err or b"")
    decode = (lambda b: b.decode("utf-8", "replace")) if text else (lambda b: b)
    return proc.returncode or 0, decode(out or b""), decode(err or b"")


def powershell(script: str, timeout: float = 25) -> str:
    """Run a PowerShell snippet hidden; empty string on any failure."""
    shell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                         "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if not os.path.exists(shell):
        shell = "powershell"
    rc, out, _ = run_hidden([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-Command", script], timeout=timeout)
    return out if rc == 0 else out


# --------------------------------------------------------------------------- #
# TCP listeners (replaces macOS lsof / netstat)
# --------------------------------------------------------------------------- #

# GetExtendedTcpTable layouts. A Go server that resolves "localhost" may bind
# IPv6 only, so both address families are enumerated.
_TCP_TABLE_OWNER_PID_LISTENER = 3
_TCP_FAMILIES = ((2, 24, 8, 20), (23, 56, 20, 52))  # (AF, row size, port off, pid off)


def _tcp_listeners(iphlpapi, family: int, row_size: int, port_offset: int, pid_offset: int):
    size = wt.DWORD(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, family,
                                 _TCP_TABLE_OWNER_PID_LISTENER, 0)
    if size.value == 0:
        return []
    buf = ctypes.create_string_buffer(size.value)
    if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, family,
                                    _TCP_TABLE_OWNER_PID_LISTENER, 0) != 0:
        return []
    count = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint32))[0]
    rows = []
    for index in range(count):
        base = 4 + index * row_size
        if base + row_size > len(buf):
            break
        local_port = ctypes.c_uint32.from_buffer(buf, base + port_offset).value
        owning_pid = ctypes.c_uint32.from_buffer(buf, base + pid_offset).value
        rows.append((socket.ntohs(local_port & 0xFFFF), owning_pid))
    return rows


def listening_ports(pid: int) -> list[int]:
    if not IS_WINDOWS:
        return []
    iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    ports = set()
    for family, row_size, port_offset, pid_offset in _TCP_FAMILIES:
        try:
            rows = _tcp_listeners(iphlpapi, family, row_size, port_offset, pid_offset)
        except Exception:
            continue
        for port, owner in rows:
            if owner == pid and 0 < port < 65536:
                ports.add(port)
    return sorted(ports)


# --------------------------------------------------------------------------- #
# icon rendering (pure stdlib: a 32-bit .ico with a ring)
# --------------------------------------------------------------------------- #

def _ring_pixels(size: int, color: tuple[int, int, int], ring: float = 0.30,
                 thickness: float = 0.22) -> list[list[tuple[int, int, int, int]]]:
    cx = cy = (size - 1) / 2.0
    outer = size / 2.0 - 0.5
    inner = outer * (1.0 - thickness)
    hole = outer * (1.0 - ring)
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if d > outer:
                row.append((0, 0, 0, 0))
            elif d >= inner:
                row.append((*color, 255))
            elif d <= inner and d >= hole:
                row.append((*color, 90))
            else:
                row.append((*color, 40))
        rows.append(row)
    return rows


def write_ico(path: Path, color: tuple[int, int, int], size: int = 32) -> bool:
    """Write a small ring icon as a BMP-style .ico. Returns True on success."""
    try:
        rows = _ring_pixels(size, color)
        header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                             size * size * 4, 0, 0, 0, 0)
        xor = bytearray()
        for y in range(size - 1, -1, -1):
            for (r, g, b, a) in rows[y]:
                xor += bytes((b, g, r, a))
        mask_row = ((size + 31) // 32) * 4
        and_mask = bytes(mask_row * size)
        image = header + bytes(xor) + and_mask
        directory = struct.pack("<HHH", 0, 1, 1)
        entry = struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                            len(image), 6 + 16)
        path.write_bytes(directory + entry + image)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# tray icon
# --------------------------------------------------------------------------- #

if IS_WINDOWS:  # pragma: no cover - Windows only
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_longlong
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    WNDPROCTYPE = WNDPROC

    WM_APP = 0x8000
    WM_COMMAND = 0x0111
    WM_DESTROY = 0x0002
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_CLOSE = 0x0010
    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
    IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
    TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x0002, 0x0100, 0x0080
    CALLBACK_MESSAGE = WM_APP + 20

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wt.HINSTANCE), ("hIcon", wt.HANDLE),
                    ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                    ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
                    ("hIconSm", wt.HANDLE)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
                    ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT),
                    ("hIcon", wt.HANDLE), ("szTip", wt.WCHAR * 128),
                    ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
                    ("szInfo", wt.WCHAR * 256), ("uVersion", wt.UINT),
                    ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                    ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HANDLE)]

    class TrayIcon:
        """A minimal Shell_NotifyIcon tray icon with a popup menu.

        Runs its own message loop on a thread so the Tk main loop stays free.
        """

        def __init__(self, tooltip: str, commands: list[tuple[int, str]],
                     on_command, on_activate=None, icon_path: Path | None = None):
            self.tooltip = tooltip[:127]
            self.commands = commands
            self.on_command = on_command
            self.on_activate = on_activate
            self.icon_path = Path(icon_path) if icon_path else None
            self.hwnd = None
            self.hicon = None
            self._nid = None
            self._wndproc = WNDPROC(self._dispatch)
            self._thread = None
            self._ready = threading.Event()
            self._class_name = f"AgentHUDTray_{os.getpid()}"

        # -- lifecycle ------------------------------------------------------
        def start(self) -> bool:
            self.error = None
            self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
            self._thread.start()
            return self._ready.wait(5.0)

        def stop(self) -> None:
            if self.hwnd:
                user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

        def _load_icon(self):
            if self.icon_path and self.icon_path.exists():
                handle = user32.LoadImageW(None, str(self.icon_path), IMAGE_ICON, 0, 0,
                                           LR_LOADFROMFILE | LR_DEFAULTSIZE)
                if handle:
                    return handle
            return user32.LoadIconW(None, wt.LPCWSTR(32512))  # IDI_APPLICATION

        def set_icon(self, icon_path: Path) -> None:
            path = Path(icon_path)
            if not self.hwnd:
                self.icon_path = path
                return
            handle = user32.LoadImageW(None, str(path), IMAGE_ICON, 0, 0,
                                       LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if not handle:
                return
            self.icon_path = path
            self.hicon = handle
            self._nid.hIcon = handle
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

        def set_tooltip(self, text: str) -> None:
            self.tooltip = text[:127]
            if self._nid:
                self._nid.szTip = self.tooltip
                shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

        # -- internals ------------------------------------------------------
        def _run(self) -> None:
            try:
                self._create_window()
                self._add_icon()
                self._ready.set()
                msg = wt.MSG()
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
            except Exception as exc:
                self.error = str(exc)
                self._ready.set()
            finally:
                self._remove_icon()

        def _create_window(self) -> None:
            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = self._class_name
            user32.RegisterClassExW(ctypes.byref(wc))
            self.hwnd = user32.CreateWindowExW(0, self._class_name, "Agent HUD", 0,
                                               0, 0, 0, 0, None, None, hinst, None)

        def _add_icon(self) -> None:
            self.hicon = self._load_icon()
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self.hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = CALLBACK_MESSAGE
            nid.hIcon = self.hicon
            nid.szTip = self.tooltip
            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                raise OSError("Shell_NotifyIcon(NIM_ADD) failed: %s"
                              % ctypes.get_last_error())
            self._nid = nid

        def _remove_icon(self) -> None:
            if self._nid is not None:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
                self._nid = None

        def _dispatch(self, hwnd, msg, wparam, lparam):
            try:
                if msg == CALLBACK_MESSAGE:
                    if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                        if self.on_activate:
                            self.on_activate()
                    elif lparam == WM_RBUTTONUP:
                        self._popup()
                    return 0
                if msg == WM_COMMAND:
                    ident = wparam & 0xFFFF
                    if self.on_command:
                        self.on_command(ident)
                    return 0
                if msg == WM_CLOSE:
                    self._remove_icon()
                    user32.DestroyWindow(hwnd)
                    user32.PostQuitMessage(0)
                    return 0
            except Exception:
                pass
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        def _popup(self) -> None:
            menu = user32.CreatePopupMenu()
            for ident, label in self.commands:
                if label is None:
                    user32.AppendMenuW(menu, 0x0800, 0, None)  # MF_SEPARATOR
                else:
                    user32.AppendMenuW(menu, 0x0000, ident, label)
            point = wt.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(self.hwnd)
            cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                        point.x, point.y, 0, self.hwnd, None)
            user32.DestroyMenu(menu)
            if cmd and self.on_command:
                self.on_command(cmd)

else:  # pragma: no cover - non Windows fallback keeps imports working
    class TrayIcon:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return False

        def stop(self):
            pass

        def set_tooltip(self, text):
            pass

        def set_icon(self, path):
            pass


def render_icon(path: Path, level: str) -> None:
    """(Re)draw the tray icon in the worst current status colour."""
    palette = {"ok": (0x3d, 0xdc, 0x84), "warning": (0xff, 0xd2, 0x3f),
               "critical": (0xff, 0x45, 0x3a), "idle": (0x9a, 0x9a, 0xa0)}
    write_ico(path, palette.get(level, palette["idle"]))
