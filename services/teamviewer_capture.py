"""DragonEye camera capture via TeamViewer (cnoc-multitool VBE Checks scheme)."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]

DEFAULT_TEAMVIEWER_PATH = r"C:\Program Files\TeamViewer\TeamViewer.exe"

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:  # noqa: BLE001
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def _noop(_: str) -> None:
    return None


def _capture_window_printwindow(hwnd, output_path: str) -> str:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid window dimensions: {width}x{height}")

    hwnd_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    old_bmp = gdi32.SelectObject(mem_dc, bitmap)

    captured = user32.PrintWindow(hwnd, mem_dc, 2) or user32.PrintWindow(hwnd, mem_dc, 0)
    if not captured:
        gdi32.SelectObject(mem_dc, old_bmp)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)
        raise RuntimeError("PrintWindow failed for TeamViewer session window")

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = width
    bmi.biHeight = -height
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0

    pixel_buf = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(mem_dc, bitmap, 0, height, pixel_buf, ctypes.byref(bmi), 0)
    gdi32.SelectObject(mem_dc, old_bmp)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, hwnd_dc)

    img = Image.frombuffer("RGBA", (width, height), pixel_buf, "raw", "BGRA", 0, 1)
    img.save(output_path)
    return output_path


def _find_session_window(gw, teamviewer_id: str):
    all_tv = gw.getWindowsWithTitle("TeamViewer")
    if not all_tv:
        return None
    clean_id = str(teamviewer_id).replace(" ", "")
    skip = {"teamviewer", "teamviewer authentication"}

    for w in all_tv:
        if clean_id in w.title.replace(" ", ""):
            return w
    for w in all_tv:
        stripped = w.title.strip()
        if stripped and stripped.lower() not in skip:
            return w
    return None


def _clear_password_field(pyautogui) -> None:
    """Clear the focused password field before typing a candidate."""
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.15)
    pyautogui.press("backspace")
    time.sleep(0.15)
    # Extra backspaces in case Ctrl+A didn't select (some remote desktops).
    for _ in range(40):
        pyautogui.press("backspace")
    time.sleep(0.2)


def _type_secret(pyautogui, text: str) -> None:
    """Type text via write() so symbols like @ work reliably."""
    # pyautogui.write handles shift chars better than deprecated typewrite.
    pyautogui.write(str(text), interval=0.05)


def _attempt_os_passwords(
    pyautogui,
    passwords: list[str],
    progress: ProgressFn,
) -> None:
    """Try each OS/DragonCam password on the focused password field.

    Remote Ubuntu sessions usually already show user ``dragonadmin`` with focus
    on the password box. Typing a username first concatenates into the password
    (e.g. ``dcam`` + ``149@dcam``) and looks like a huge password string.
    """
    if not passwords:
        return
    for idx, cam_pwd in enumerate(passwords, start=1):
        progress(f"Login password {idx}/{len(passwords)}")
        logger.info("[TV] login password attempt %s/%s len=%s", idx, len(passwords), len(cam_pwd))
        try:
            _clear_password_field(pyautogui)
            _type_secret(pyautogui, cam_pwd)
            pyautogui.press("enter")
            time.sleep(5)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TV] password attempt %s failed: %s", idx, type(exc).__name__)


def _attempt_app_login(
    pyautogui,
    username: str,
    passwords: list[str],
    progress: ProgressFn,
) -> None:
    """Optional second-stage DragonCam username+password after OS desktop is up."""
    if not username or not passwords:
        return
    progress(f"DragonCam app login as {username}")
    logger.info("[TV] DragonCam username login user=%s", username)
    time.sleep(2)
    try:
        _type_secret(pyautogui, username)
        pyautogui.press("tab")
        time.sleep(0.5)
        _clear_password_field(pyautogui)
        _type_secret(pyautogui, passwords[0])
        pyautogui.press("enter")
        time.sleep(4)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TV] DragonCam app login failed: %s", type(exc).__name__)


def _close_session_notes(teamviewer_id: str | None = None, timeout_sec: int = 8) -> None:
    user32 = ctypes.windll.user32
    WM_CLOSE = 0x0010
    GW_OWNER = 4

    def _title(hwnd):
        buf = ctypes.create_unicode_buffer(512)
        length = user32.GetWindowTextW(hwnd, buf, len(buf))
        return buf.value if length else ""

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    existing = set()

    def _snap(hwnd, _lp):
        if user32.IsWindowVisible(hwnd) and _title(hwnd).strip().lower() == "teamviewer":
            existing.add(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(_snap), 0)
    min_wait = time.time() + 3
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        found = [False]

        def _enum(hwnd, _lp):
            if not user32.IsWindowVisible(hwnd):
                return True
            title = _title(hwnd).strip()
            lowered = title.lower()
            close = False
            if "session note" in lowered:
                close = True
            elif teamviewer_id and str(teamviewer_id).replace(" ", "").lower() in lowered.replace(" ", ""):
                close = True
            elif lowered == "teamviewer":
                owner = user32.GetWindow(hwnd, GW_OWNER)
                if owner and hwnd not in existing:
                    close = True
            if close:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                found[0] = True
            return True

        user32.EnumWindows(EnumWindowsProc(_enum), 0)
        if not found[0] and time.time() >= min_wait:
            break
        time.sleep(0.5)


def _close_session_window(win, teamviewer_id: str | None = None) -> None:
    if win is not None:
        try:
            ctypes.windll.user32.PostMessageW(win._hWnd, 0x0010, 0, 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TV] close session window failed: %s", type(exc).__name__)
    _close_session_notes(teamviewer_id=teamviewer_id)


def _dismiss_remote_popups(pyautogui, rounds: int = 3, interval: float = 0.8) -> None:
    for _ in range(rounds):
        pyautogui.press("escape")
        time.sleep(interval)


def capture_dragoneye_via_teamviewer(
    *,
    teamviewer_id: str,
    teamviewer_passwords: list[str],
    camera_passwords: list[str],
    output_dir: str | Path,
    filename: str = "de_tv.png",
    camera_username: str = "",
    teamviewer_path: str = DEFAULT_TEAMVIEWER_PATH,
    wait_for_connection_sec: int = 60,
    on_progress: ProgressFn | None = None,
) -> Path:
    """Connect TeamViewer → type DragonCam credentials → screenshot session window."""
    progress = on_progress or _noop
    try:
        import pyautogui
        import pygetwindow as gw
    except ImportError as exc:
        raise RuntimeError(
            "pyautogui and PyGetWindow are required for DragonEye TeamViewer capture"
        ) from exc

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / filename

    teamviewer_path = os.path.expandvars(teamviewer_path)
    if not os.path.isfile(teamviewer_path):
        raise RuntimeError(f"TeamViewer executable not found at {teamviewer_path}")

    if not teamviewer_passwords:
        raise RuntimeError("No TeamViewer connection passwords configured")

    win = None
    proc = None
    t0 = time.perf_counter()
    logger.info("[TV] connect id=%s passwords=%s", teamviewer_id, len(teamviewer_passwords))

    for idx, tv_pwd in enumerate(teamviewer_passwords, start=1):
        progress(f"TeamViewer connect {teamviewer_id} (try {idx}/{len(teamviewer_passwords)})")
        try:
            proc = subprocess.Popen(
                [teamviewer_path, "-i", str(teamviewer_id), "--Password", str(tv_pwd)]
            )
        except OSError as exc:
            logger.warning("[TV] launch failed: %s", type(exc).__name__)
            continue

        time.sleep(10)
        if proc.poll() is not None and proc.returncode != 0:
            logger.info("[TV] process exited early code=%s", proc.returncode)
            continue

        elapsed = 0
        win = None
        while elapsed < wait_for_connection_sec:
            win = _find_session_window(gw, teamviewer_id)
            if win:
                break
            if proc.poll() is not None and proc.returncode != 0:
                break
            time.sleep(2)
            elapsed += 2

        if win:
            logger.info("[TV] session window='%s'", win.title)
            break

        if proc and proc.poll() is None:
            proc.terminate()
        win = None
        logger.info("[TV] password try %s failed for id=%s", idx, teamviewer_id)

    if not win:
        raise RuntimeError(f"TeamViewer session failed for id={teamviewer_id}")

    try:
        win.activate()
        time.sleep(3)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TV] activate failed: %s", type(exc).__name__)

    time.sleep(6)

    # Remote Ubuntu lock/login: user (dragonadmin) is pre-selected; focus is on
    # the password field. Do NOT type TV_USERNAME here — that was concatenating
    # into the password (looked like a huge wrong password) and skipping retries.
    if camera_passwords:
        progress(f"OS/camera login ({len(camera_passwords)} password tries)")
        _attempt_os_passwords(pyautogui, camera_passwords, progress)
        # Optional second stage if DragonCam prompts for dcam after desktop loads.
        if camera_username:
            _attempt_app_login(pyautogui, camera_username, camera_passwords, progress)

    progress("Dismissing remote popups…")
    _dismiss_remote_popups(pyautogui)

    # Re-resolve session window in case the handle changed after login.
    try:
        refreshed = _find_session_window(gw, teamviewer_id)
        if refreshed is not None:
            win = refreshed
            win.activate()
            time.sleep(1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TV] refresh session window failed: %s", type(exc).__name__)

    progress("Capturing TeamViewer session…")
    try:
        _capture_window_printwindow(win._hWnd, str(filepath))
    except Exception as exc:
        raise RuntimeError(f"TeamViewer screenshot failed: {exc}") from exc
    finally:
        _close_session_window(win, teamviewer_id=str(teamviewer_id))

    logger.info(
        "[TV] saved path=%s elapsed=%.1fs",
        filepath,
        time.perf_counter() - t0,
    )
    progress(f"TeamViewer saved {filename}")
    return filepath
