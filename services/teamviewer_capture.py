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

from PIL import Image

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]
CancelFn = Callable[[], bool]

DEFAULT_TEAMVIEWER_PATH = r"C:\Program Files\TeamViewer\TeamViewer.exe"


class CaptureCancelled(Exception):
    """Raised when an audit cancel was requested mid-TeamViewer capture."""

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:  # noqa: BLE001
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def _noop(_: str) -> None:
    return None


SW_RESTORE = 9
SW_SHOW = 5
SW_MAXIMIZE = 3
DWMWA_EXTENDED_FRAME_BOUNDS = 9

_SKIP_TV_TITLES = {"teamviewer", "teamviewer authentication"}


class _TVWindow:
    """Lightweight TeamViewer session window handle (avoids stale PyGetWindow objects)."""

    def __init__(self, hwnd: int, title: str) -> None:
        self._hWnd = hwnd
        self.title = title

    def activate(self) -> None:
        user32 = ctypes.windll.user32
        if not user32.IsWindow(self._hWnd):
            return
        if user32.IsIconic(self._hWnd):
            user32.ShowWindow(self._hWnd, SW_RESTORE)
            time.sleep(0.3)
        user32.ShowWindow(self._hWnd, SW_SHOW)
        user32.SetForegroundWindow(self._hWnd)


def _get_window_title(hwnd) -> str:
    user32 = ctypes.windll.user32
    buf = ctypes.create_unicode_buffer(512)
    length = user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value if length else ""


def _hwnd_rect(hwnd) -> tuple[int, int, int, int]:
    """Return (left, top, width, height) preferring DWM frame bounds."""
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    try:
        dwmapi = ctypes.windll.dwmapi
        hr = dwmapi.DwmGetWindowAttribute(
            hwnd,
            DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if hr == 0:
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w > 0 and h > 0:
                return rect.left, rect.top, w, h
    except Exception:  # noqa: BLE001
        pass
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _hwnd_area(hwnd) -> int:
    user32 = ctypes.windll.user32
    if not user32.IsWindow(hwnd):
        return 0
    _left, _top, width, height = _hwnd_rect(hwnd)
    return max(0, width) * max(0, height)


def _enum_session_windows(teamviewer_id: str = "") -> list[tuple[int, str, int]]:
    """Enumerate visible TeamViewer remote-session top-level windows.

    When teamviewer_id is empty, returns every remote session window.
    """
    user32 = ctypes.windll.user32
    clean_id = str(teamviewer_id or "").replace(" ", "")
    found: list[tuple[int, str, int]] = []

    def _callback(hwnd, _lp):
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            return True
        title = _get_window_title(hwnd).strip()
        lowered = title.lower()
        if not title or lowered in _SKIP_TV_TITLES:
            return True
        if "teamviewer" not in lowered:
            return True
        # Remote session windows look like "<host> - TeamViewer".
        is_session = " - teamviewer" in lowered
        if clean_id:
            is_session = is_session or clean_id in title.replace(" ", "")
        if not is_session:
            return True
        found.append((hwnd, title, _hwnd_area(hwnd)))
        return True

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(EnumWindowsProc(_callback), 0)

    def _rank(item: tuple[int, str, int]) -> tuple[int, int, int]:
        _hwnd, title, area = item
        id_match = 1 if clean_id in title.replace(" ", "") else 0
        has_area = 1 if area >= 200 * 150 else 0
        return (has_area, id_match, area)

    found.sort(key=_rank, reverse=True)
    return found


def _find_session_window(gw, teamviewer_id: str, *, title_hint: str = "") -> _TVWindow | None:
    """Return the best TeamViewer session window (fresh EnumWindows scan)."""
    del gw  # PyGetWindow cache goes stale after login; use Win32 enum instead.
    windows = _enum_session_windows(teamviewer_id)
    if title_hint:
        hinted = [w for w in windows if title_hint in w[1]]
        if hinted:
            windows = hinted + [w for w in windows if w not in hinted]
    for hwnd, title, _area in windows:
        if ctypes.windll.user32.IsWindow(hwnd):
            return _TVWindow(hwnd, title)
    return None


def _prepare_window_for_capture(hwnd) -> tuple[int, int, int, int]:
    """Restore / show the window and wait until it reports a usable size."""
    user32 = ctypes.windll.user32
    if not user32.IsWindow(hwnd):
        raise ValueError("TeamViewer session HWND is no longer valid")

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.4)
    user32.ShowWindow(hwnd, SW_SHOW)
    user32.ShowWindow(hwnd, SW_MAXIMIZE)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)

    last = (0, 0, 0, 0)
    for _ in range(30):
        if not user32.IsWindow(hwnd):
            raise ValueError("TeamViewer session HWND is no longer valid")
        last = _hwnd_rect(hwnd)
        _left, _top, width, height = last
        if width >= 200 and height >= 150:
            return last
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.ShowWindow(hwnd, SW_MAXIMIZE)
        time.sleep(0.35)

    _left, _top, width, height = last
    raise ValueError(f"Invalid window dimensions: {width}x{height}")


def _capture_window_printwindow(hwnd, output_path: str) -> str:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    left, top, width, height = _prepare_window_for_capture(hwnd)

    hwnd_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    old_bmp = gdi32.SelectObject(mem_dc, bitmap)

    captured = user32.PrintWindow(hwnd, mem_dc, 2) or user32.PrintWindow(hwnd, mem_dc, 0)
    if captured:
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

    gdi32.SelectObject(mem_dc, old_bmp)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, hwnd_dc)

    # Fallback: grab the on-screen region (works when PrintWindow returns empty).
    logger.warning("[TV] PrintWindow failed — falling back to screen region capture")
    try:
        from PIL import ImageGrab
    except ImportError as exc:
        raise RuntimeError("PrintWindow failed and ImageGrab is unavailable") from exc

    # Re-read bounds in case the window moved while PrintWindow ran.
    left, top, width, height = _prepare_window_for_capture(hwnd)
    right, bottom = left + width, top + height
    try:
        img = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
    except TypeError:
        # Older Pillow without all_screens=
        img = ImageGrab.grab(bbox=(left, top, right, bottom))
    img.save(output_path)
    return output_path


def _resolve_capture_window(
    teamviewer_id: str,
    *,
    title_hint: str = "",
    pinned_hwnd: int | None = None,
) -> _TVWindow:
    """Find a live session window suitable for screenshot."""
    user32 = ctypes.windll.user32
    if pinned_hwnd and user32.IsWindow(pinned_hwnd):
        title = _get_window_title(pinned_hwnd).strip()
        if title and (not title_hint or title_hint in title):
            area = _hwnd_area(pinned_hwnd)
            logger.info(
                "[TV] reusing pinned hwnd=%s title=%r area=%s",
                pinned_hwnd,
                title,
                area,
            )
            return _TVWindow(pinned_hwnd, title)

    windows = _enum_session_windows(teamviewer_id)
    if title_hint:
        hinted = [w for w in windows if title_hint in w[1]]
        if hinted:
            windows = hinted + [w for w in windows if w not in hinted]
    if not windows and title_hint:
        # Session title may not include the numeric TV id — match by hint alone.
        windows = _enum_windows_by_title_hint(title_hint)
    if not windows:
        sample = _sample_teamviewer_titles()
        logger.warning(
            "[TV] no session window for id=%s hint=%r; visible TV titles=%s",
            teamviewer_id,
            title_hint,
            sample,
        )
    for hwnd, title, area in windows:
        if not user32.IsWindow(hwnd):
            continue
        if area >= 200 * 150:
            return _TVWindow(hwnd, title)
    for hwnd, title, _area in windows:
        if user32.IsWindow(hwnd):
            return _TVWindow(hwnd, title)
    raise ValueError(f"No TeamViewer session window found for id={teamviewer_id}")


def _enum_windows_by_title_hint(title_hint: str) -> list[tuple[int, str, int]]:
    """Find session windows whose title contains the remembered session name."""
    if not title_hint:
        return []
    user32 = ctypes.windll.user32
    found: list[tuple[int, str, int]] = []

    def _callback(hwnd, _lp):
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            return True
        title = _get_window_title(hwnd).strip()
        if title_hint in title and "teamviewer" in title.lower():
            found.append((hwnd, title, _hwnd_area(hwnd)))
        return True

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(EnumWindowsProc(_callback), 0)
    found.sort(key=lambda item: item[2], reverse=True)
    return found


def _sample_teamviewer_titles(limit: int = 8) -> list[str]:
    user32 = ctypes.windll.user32
    titles: list[str] = []

    def _callback(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _get_window_title(hwnd).strip()
        if title and "teamviewer" in title.lower() and title not in titles:
            titles.append(title)
        return True

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(EnumWindowsProc(_callback), 0)
    return titles[:limit]


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
    """Type the camera/OS password on the Ubuntu lock-screen password field."""
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


def _sleep_cancellable(seconds: float, should_cancel: CancelFn | None, *, chunk: float = 0.4) -> None:
    """Sleep in short chunks so cancel can interrupt long waits."""
    if seconds <= 0:
        return
    deadline = time.time() + seconds
    while True:
        if should_cancel and should_cancel():
            raise CaptureCancelled("TeamViewer capture cancelled")
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(chunk, remaining))


def cleanup_teamviewer_ui(*, teamviewer_id: str | None = None) -> int:
    """Best-effort close remote sessions and session-note dialogs.

    Used on cancel / teardown. Returns number of session windows closed.
    """
    user32 = ctypes.windll.user32
    WM_CLOSE = 0x0010
    closed = 0
    windows = _enum_session_windows(teamviewer_id or "")
    for hwnd, title, _area in windows:
        try:
            if user32.IsWindow(hwnd):
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                closed += 1
                logger.info("[TV] cleanup closed session hwnd=%s title=%r", hwnd, title)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TV] cleanup close failed: %s", type(exc).__name__)
    try:
        _close_session_notes(
            teamviewer_id=teamviewer_id,
            timeout_sec=6,
            session_title_hint="",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TV] cleanup notes failed: %s", type(exc).__name__)
    logger.info("[TV] cleanup done closed=%s id=%r", closed, teamviewer_id)
    return closed


def _close_session_window(win, teamviewer_id: str | None = None, *, session_title_hint: str = "") -> None:
    if win is not None:
        try:
            ctypes.windll.user32.PostMessageW(win._hWnd, 0x0010, 0, 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TV] close session window failed: %s", type(exc).__name__)
    _close_session_notes(teamviewer_id=teamviewer_id, session_title_hint=session_title_hint)


def _close_session_notes(
    teamviewer_id: str | None = None,
    timeout_sec: int = 8,
    *,
    session_title_hint: str = "",
) -> None:
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
            elif session_title_hint and session_title_hint in title:
                close = False  # never close the active remote session by title match
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


def capture_dragoneye_via_teamviewer(
    *,
    teamviewer_id: str,
    teamviewer_passwords: list[str],
    camera_passwords: list[str],
    output_dir: str | Path,
    filename: str = "de_tv.png",
    camera_username: str = "",  # unused — kept for call-site compatibility
    teamviewer_path: str = DEFAULT_TEAMVIEWER_PATH,
    wait_for_connection_sec: int = 60,
    on_progress: ProgressFn | None = None,
    should_cancel: CancelFn | None = None,
) -> Path:
    """Connect TeamViewer → OS login (if needed) → screenshot session window."""
    del camera_username  # never type into desktop after OS login
    progress = on_progress or _noop

    def _cancelled() -> bool:
        return bool(should_cancel and should_cancel())

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

    try:
        for idx, tv_pwd in enumerate(teamviewer_passwords, start=1):
            if _cancelled():
                raise CaptureCancelled("TeamViewer capture cancelled")
            progress(f"TeamViewer connect {teamviewer_id} (try {idx}/{len(teamviewer_passwords)})")
            try:
                proc = subprocess.Popen(
                    [teamviewer_path, "-i", str(teamviewer_id), "--Password", str(tv_pwd)]
                )
            except OSError as exc:
                logger.warning("[TV] launch failed: %s", type(exc).__name__)
                continue

            _sleep_cancellable(10, should_cancel)
            if proc.poll() is not None and proc.returncode != 0:
                logger.info("[TV] process exited early code=%s", proc.returncode)
                continue

            elapsed = 0
            win = None
            while elapsed < wait_for_connection_sec:
                if _cancelled():
                    raise CaptureCancelled("TeamViewer capture cancelled")
                win = _find_session_window(gw, teamviewer_id)
                if win:
                    break
                if proc.poll() is not None and proc.returncode != 0:
                    break
                _sleep_cancellable(2, should_cancel)
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

        session_title_hint = win.title
        session_hwnd = win._hWnd
        try:
            win.activate()
            _sleep_cancellable(3, should_cancel)
        except CaptureCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TV] activate failed: %s", type(exc).__name__)

        _sleep_cancellable(6, should_cancel)

        # Remote Ubuntu lock screen: password field is focused. Type camera password once.
        if camera_passwords:
            if _cancelled():
                raise CaptureCancelled("TeamViewer capture cancelled")
            progress(f"OS login ({len(camera_passwords)} password try)")
            _attempt_os_passwords(pyautogui, camera_passwords, progress)

        # Do NOT send Escape or type TV_USERNAME — both break the session / desktop.
        _sleep_cancellable(2, should_cancel)

        # Re-resolve session window in case the handle changed after login.
        try:
            refreshed = _find_session_window(gw, teamviewer_id, title_hint=session_title_hint)
            if refreshed is not None:
                win = refreshed
                session_hwnd = win._hWnd
                try:
                    win.activate()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[TV] activate before capture failed: %s", type(exc).__name__)
                _sleep_cancellable(1.5, should_cancel)
        except CaptureCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TV] refresh session window failed: %s", type(exc).__name__)

        if _cancelled():
            raise CaptureCancelled("TeamViewer capture cancelled")
        progress("Capturing TeamViewer session…")
        last_err: Exception | None = None
        for attempt in range(1, 6):
            if _cancelled():
                raise CaptureCancelled("TeamViewer capture cancelled")
            try:
                win = _resolve_capture_window(
                    teamviewer_id,
                    title_hint=session_title_hint,
                    pinned_hwnd=session_hwnd,
                )
                session_hwnd = win._hWnd
                try:
                    win.activate()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[TV] activate on attempt %s failed: %s", attempt, type(exc).__name__)
                _sleep_cancellable(0.8, should_cancel)
                left, top, width, height = _hwnd_rect(win._hWnd)
                logger.info(
                    "[TV] capture attempt %s hwnd=%s title=%r bounds=%sx%s@%s,%s",
                    attempt,
                    win._hWnd,
                    win.title,
                    width,
                    height,
                    left,
                    top,
                )
                _capture_window_printwindow(win._hWnd, str(filepath))
                last_err = None
                break
            except CaptureCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.warning(
                    "[TV] capture attempt %s failed: %s: %s",
                    attempt,
                    type(exc).__name__,
                    exc,
                )
                _sleep_cancellable(2.0, should_cancel)
        if last_err is not None:
            raise RuntimeError(f"TeamViewer screenshot failed: {last_err}") from last_err
    except CaptureCancelled:
        logger.info("[TV] cancelled id=%s — cleaning up", teamviewer_id)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        cleanup_teamviewer_ui(teamviewer_id=str(teamviewer_id))
        raise
    finally:
        # Normal path closes the session after a successful (or failed) capture attempt.
        # Cancel path already cleaned up above; skip double-close when cancelled.
        if not _cancelled() and win is not None:
            try:
                _close_session_window(
                    win,
                    teamviewer_id=str(teamviewer_id),
                    session_title_hint=getattr(win, "title", "") or "",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[TV] final close failed: %s", type(exc).__name__)

    logger.info(
        "[TV] saved path=%s elapsed=%.1fs",
        filepath,
        time.perf_counter() - t0,
    )
    progress(f"TeamViewer saved {filename}")
    return filepath
