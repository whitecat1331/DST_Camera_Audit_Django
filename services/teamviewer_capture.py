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

from services.password_hints import HintStore, first_then_rest

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


def _find_session_window(
    gw,
    teamviewer_id: str,
    *,
    title_hint: str = "",
    exclude: frozenset[int] = frozenset(),
) -> _TVWindow | None:
    """Return the best TeamViewer session window (fresh EnumWindows scan).

    exclude: windows already open before this connect. Any "<host> - TeamViewer"
    window qualifies, so a session left open from earlier would otherwise be
    taken as the new one (an FX1260 lane once captured an "FX1361 L2" window).
    """
    del gw  # PyGetWindow cache goes stale after login; use Win32 enum instead.
    windows = [w for w in _enum_session_windows(teamviewer_id) if w[0] not in exclude]
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
    # Extra backspaces in case Ctrl+A didn't select (some remote desktops). One
    # call: pyautogui pauses 0.1 s after every call, so 40 calls cost 4 s.
    pyautogui.press("backspace", presses=40, interval=0.005)
    time.sleep(0.2)


def _type_secret(pyautogui, text: str) -> None:
    """Type text via write() so symbols like @ work reliably."""
    # pyautogui.write handles shift chars better than deprecated typewrite.
    pyautogui.write(str(text), interval=0.05)


def _grab_session_image(hwnd) -> Image.Image | None:
    """Fast on-screen grab of a TeamViewer session window (for reconnect checks)."""
    left, top, width, height = _hwnd_rect(hwnd)
    if width < 80 or height < 80:
        return None
    try:
        from PIL import ImageGrab
    except ImportError:
        return None
    bbox = (left, top, left + width, top + height)
    try:
        return ImageGrab.grab(bbox=bbox, all_screens=True)
    except TypeError:
        return ImageGrab.grab(bbox=bbox)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TV] session grab failed: %s", type(exc).__name__)
        return None


def reconnect_card_score(img: Image.Image) -> float:
    """Share of center rows whose widest flat-white run has the same left/right edges.

    TeamViewer's "connection lost" card is a flat white rectangle: most of its rows
    (all but the text lines) share one set of edges. A bright DragonCam photo (a
    sunlit road) has wide white runs too, but their edges wander row to row.
    Measured 2026-10-29: real frames ≤ 0.16 (three bright L2 photos included),
    a synthetic card 0.67.
    """
    from collections import Counter

    rgb = img.convert("RGB")
    w, h = rgb.size
    crop = rgb.crop((int(w * 0.28), int(h * 0.32), int(w * 0.72), int(h * 0.68)))
    crop = crop.resize((max(1, crop.width // 2), max(1, crop.height // 2)), Image.NEAREST)
    cw, ch = crop.size
    px = crop.load()
    edges: Counter[tuple[int, int]] = Counter()
    for y in range(ch):
        best = (0, 0, 0)
        start = None
        for x in range(cw + 1):
            white = False
            if x < cw:
                r, g, b = px[x, y]
                white = min(r, g, b) >= 245 and max(r, g, b) - min(r, g, b) <= 6
            if white and start is None:
                start = x
            elif not white and start is not None:
                if x - start > best[0]:
                    best = (x - start, start, x)
                start = None
        if best[0] >= 0.35 * cw:
            edges[(best[1] // 2, best[2] // 2)] += 1
    return edges.most_common(1)[0][1] / ch if edges else 0.0


def _looks_like_reconnect_dialog(img: Image.Image) -> bool:
    """TeamViewer 'connection lost' card: a flat white rectangle in the center."""
    w, h = img.size
    if w < 100 or h < 100:
        return False
    return reconnect_card_score(img) >= 0.35


def _focus_remote_desktop(pyautogui, hwnd) -> None:
    """Click into the remote desktop area so the Ubuntu lock-screen field gets focus."""
    left, top, width, height = _hwnd_rect(hwnd)
    if width < 80 or height < 80:
        return
    # Skip TeamViewer chrome (title + toolbar); click mid-lower remote content.
    x = left + width // 2
    y = top + int(height * 0.58)
    try:
        pyautogui.click(x, y)
        time.sleep(0.35)
        # Second click near typical password-field band on GNOME lock screens.
        pyautogui.click(x, top + int(height * 0.72))
        time.sleep(0.35)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TV] focus click failed: %s", type(exc).__name__)


# Frames are compared at this size: enough to see a lock screen give way to the
# desktop, cheap enough to grab every 0.4 s.
_FRAME_SIZE = (192, 104)
# Mean per-channel difference (0-255) between frames at _FRAME_SIZE. Measured on
# FX1173 L1 (2026-10-29): an idle session 0.0-0.1 between grabs; the DragonCam
# desktop up to 2.7 as its picture updates; clicking the lock clock to show the
# password prompt 5.4; the prompt giving way to the desktop 64.8.
STABLE_DIFF = 4.0
WAKE_DIFF = 3.0
UNLOCK_DIFF = 15.0
RECONNECT_WAIT_SEC = 40


class PartnerOffline(RuntimeError):
    """TeamViewer said "Unable to connect": the partner isn't online."""


def _small_frame(hwnd) -> Image.Image | None:
    img = _grab_session_image(hwnd)
    return None if img is None else img.convert("RGB").resize(_FRAME_SIZE)


def frame_diff(a: Image.Image | None, b: Image.Image | None) -> float:
    if a is None or b is None:
        return 255.0
    from PIL import ImageChops, ImageStat

    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


def _wait_until_settled(
    hwnd,
    should_cancel: CancelFn | None,
    *,
    timeout_sec: float,
    progress: ProgressFn | None = None,
) -> Image.Image | None:
    """Poll the session until two frames in a row barely change (and no reconnect card).

    Returns the settled frame, or the last one when the timeout ran out.
    """
    start = time.time()
    deadline = start + timeout_sec
    prev = None
    calm = 0
    while time.time() < deadline:
        if should_cancel and should_cancel():
            raise CaptureCancelled("TeamViewer capture cancelled")
        if not ctypes.windll.user32.IsWindow(hwnd):
            return None
        full = _grab_session_image(hwnd)
        if full is not None and _looks_like_reconnect_dialog(full):
            if progress:
                progress("Waiting for TeamViewer reconnect…")
            deadline = max(deadline, start + RECONNECT_WAIT_SEC)  # a reconnect may take a while
            prev, calm = None, 0
            _sleep_cancellable(1.0, should_cancel)
            continue
        cur = None if full is None else full.convert("RGB").resize(_FRAME_SIZE)
        calm = calm + 1 if cur is not None and frame_diff(cur, prev) < STABLE_DIFF else 0
        if calm >= 2:
            return cur
        prev = cur
        _sleep_cancellable(0.4, should_cancel)
    logger.info("[TV] session not settled after %.0fs", timeout_sec)
    return prev


def _login(
    pyautogui,
    hwnd,
    passwords: list[str],
    order: list[int],
    should_cancel: CancelFn | None,
    progress: ProgressFn,
) -> int | None:
    """Type camera passwords (in `order`) until the lock screen gives way.

    Returns the number that unlocked, or None. Stops at the first unlock, so a
    password is never typed into the desktop. Every password is tried otherwise:
    "the wake click changed nothing" is not proof of an unlocked session (the
    prompt may already be up, FX1408 2026-10-29), and a wrong remembered
    password must still fall through to the other one.
    """
    _focus_remote_desktop(pyautogui, hwnd)
    for attempt, number in enumerate(order, start=1):
        if should_cancel and should_cancel():
            raise CaptureCancelled("TeamViewer capture cancelled")
        progress(f"Login password {attempt}/{len(order)}")
        if attempt > 1:
            _focus_remote_desktop(pyautogui, hwnd)
        _clear_password_field(pyautogui)
        _type_secret(pyautogui, passwords[number - 1])
        typed = _small_frame(hwnd)
        pyautogui.press("enter")
        deadline = time.time() + 6
        while time.time() < deadline:
            _sleep_cancellable(0.4, should_cancel)
            if frame_diff(_small_frame(hwnd), typed) >= UNLOCK_DIFF:
                logger.info("[TV] unlocked with camera password #%s (try %s)", number, attempt)
                return number
        logger.info("[TV] camera password #%s did not unlock", number)
    return None


def _main_window() -> int | None:
    """The TeamViewer main window (title exactly "TeamViewer", no owner)."""
    user32 = ctypes.windll.user32
    found: list[int] = []

    def _cb(hwnd, _lp):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):
            if _get_window_title(hwnd).strip().lower() == "teamviewer":
                found.append(hwnd)
        return True

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return found[0] if found else None


def _unable_to_connect_ok(hwnd) -> tuple[int, int] | None:
    """Screen point of the "Unable to connect" dialog's blue OK button, if showing.

    The dialog is drawn inside the main window, centered, OK at (+188, +45).
    """
    try:
        from PIL import ImageGrab
    except ImportError:
        return None
    left, top, width, height = _hwnd_rect(hwnd)
    x, y = left + width // 2 + 188, top + height // 2 + 45
    try:
        img = ImageGrab.grab(bbox=(x - 20, y - 17, x + 20, y + 17), all_screens=True).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    px = list(img.getdata())
    blue = sum(1 for r, _g, b in px if b > 180 and r < 160) / max(1, len(px))
    return (x, y) if blue >= 0.5 else None


def _click_ok(main: int, point: tuple[int, int]) -> None:
    import pyautogui

    ctypes.windll.user32.SetForegroundWindow(main)
    time.sleep(0.2)
    pyautogui.click(*point)


def _clear_unable_to_connect() -> None:
    main = _main_window()
    ok = _unable_to_connect_ok(main) if main else None
    if ok:
        logger.info("[TV] clearing a leftover 'Unable to connect' dialog")
        _click_ok(main, ok)
        time.sleep(0.5)


def _auth_window() -> int | None:
    """TeamViewer asks for a password: the connection password was refused."""
    return ctypes.windll.user32.FindWindowW(None, "TeamViewer Authentication") or None


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


# TeamViewer ID -> {"connect": n, "camera": n}: password numbers that last worked.
TV_HINTS = HintStore("tv_password_hints.json", "TV_PASSWORD_HINTS_PATH")


def _hint(tv_id: str, field: str) -> int | None:
    value = TV_HINTS.get(tv_id)
    number = value.get(field) if isinstance(value, dict) else None
    return number if isinstance(number, int) else None


def _remember(tv_id: str, field: str, number: int) -> None:
    if _hint(tv_id, field) == number:
        return
    value = TV_HINTS.get(tv_id)
    value = dict(value) if isinstance(value, dict) else {}
    value[field] = number
    TV_HINTS.set(tv_id, value)


def _connect(
    gw,
    teamviewer_id: str,
    teamviewer_path: str,
    passwords: list[str],
    order: list[int],
    stale: frozenset[int],
    wait_sec: float,
    progress: ProgressFn,
    cancelled: CancelFn,
) -> tuple[_TVWindow, int]:
    """Launch the session; returns (window, connection password number).

    Watches what TeamViewer actually shows instead of a fixed wait:
    session window → connected; "TeamViewer Authentication" → that password was
    refused, try the next; "Unable to connect" → partner offline, stop.
    """
    _clear_unable_to_connect()  # a dialog left from an earlier lane must not read as this one's
    for attempt, number in enumerate(order, start=1):
        if cancelled():
            raise CaptureCancelled("TeamViewer capture cancelled")
        progress(f"TeamViewer connect {teamviewer_id} (try {attempt}/{len(order)})")
        try:
            proc = subprocess.Popen(
                [teamviewer_path, "-i", str(teamviewer_id), "--Password", str(passwords[number - 1])]
            )
        except OSError as exc:
            logger.warning("[TV] launch failed: %s", type(exc).__name__)
            continue
        t0 = time.time()
        while time.time() - t0 < wait_sec:
            if cancelled():
                if proc.poll() is None:
                    proc.terminate()
                raise CaptureCancelled("TeamViewer capture cancelled")
            win = _find_session_window(gw, teamviewer_id, exclude=stale)
            if win:
                logger.info("[TV] session window=%r in %.1fs", win.title, time.time() - t0)
                return win, number
            auth = _auth_window()
            if auth:
                logger.info("[TV] connection password #%s refused for id=%s", number, teamviewer_id)
                ctypes.windll.user32.PostMessageW(auth, 0x0010, 0, 0)
                break
            main = _main_window() if time.time() - t0 > 2 else None
            ok = _unable_to_connect_ok(main) if main else None
            if ok:
                _click_ok(main, ok)
                logger.info("[TV] partner offline id=%s (%.1fs)", teamviewer_id, time.time() - t0)
                raise PartnerOffline(f"TeamViewer partner {teamviewer_id} is offline (Unable to connect)")
            time.sleep(0.25)
        else:
            logger.info("[TV] no session for id=%s after %.0fs", teamviewer_id, wait_sec)
        if proc.poll() is None:
            proc.terminate()
    raise RuntimeError(f"TeamViewer session failed for id={teamviewer_id}")


def capture_dragoneye_via_teamviewer(
    *,
    teamviewer_id: str,
    teamviewer_passwords: list[str],
    camera_passwords: list[str],
    output_dir: str | Path,
    filename: str = "de_tv.png",
    camera_username: str = "",  # unused — kept for call-site compatibility
    teamviewer_path: str = DEFAULT_TEAMVIEWER_PATH,
    wait_for_connection_sec: int = 30,
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
    t0 = time.perf_counter()
    tv_id = str(teamviewer_id)
    logger.info("[TV] connect id=%s passwords=%s", tv_id, len(teamviewer_passwords))

    stale = frozenset(hwnd for hwnd, _title, _area in _enum_session_windows())
    if stale:
        logger.info("[TV] ignoring %s session window(s) open before connect", len(stale))
    try:
        win, connect_no = _connect(
            gw,
            tv_id,
            teamviewer_path,
            teamviewer_passwords,
            first_then_rest(len(teamviewer_passwords), _hint(tv_id, "connect")),
            stale,
            wait_for_connection_sec,
            progress,
            _cancelled,
        )
        _remember(tv_id, "connect", connect_no)

        session_title_hint = win.title
        session_hwnd = win._hWnd
        try:
            win.activate()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TV] activate failed: %s", type(exc).__name__)

        progress("Waiting for remote session to settle…")
        if _wait_until_settled(session_hwnd, should_cancel, timeout_sec=12, progress=progress) is None:
            raise RuntimeError(f"TeamViewer session closed before login for id={tv_id}")

        if camera_passwords:
            progress(f"OS login ({len(camera_passwords)} password(s))")
            unlocked = _login(
                pyautogui,
                session_hwnd,
                camera_passwords,
                first_then_rest(len(camera_passwords), _hint(tv_id, "camera")),
                should_cancel,
                progress,
            )
            if unlocked:
                _remember(tv_id, "camera", unlocked)

        # Do NOT send Escape or type TV_USERNAME — both break the session / desktop.
        progress("Waiting after OS login…")
        _wait_until_settled(session_hwnd, should_cancel, timeout_sec=8, progress=progress)

        # Re-resolve session window in case the handle changed after login.
        try:
            refreshed = _find_session_window(
                gw, tv_id, title_hint=session_title_hint, exclude=stale
            )
            if refreshed is not None:
                win = refreshed
                session_hwnd = win._hWnd
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
                _sleep_cancellable(0.4, should_cancel)
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
                # Reject captures that still show the reconnect overlay.
                try:
                    saved = Image.open(filepath)
                    if _looks_like_reconnect_dialog(saved):
                        raise RuntimeError("capture still shows TeamViewer reconnect dialog")
                except RuntimeError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[TV] post-capture check skipped: %s", type(exc).__name__)
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
