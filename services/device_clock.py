"""Read the clock a device shows in its capture.

- CBW: the Date/Time page's "Current:" text, written next to the image by
  services/cbw_capture.py (exact; no OCR).
- VNC (LaserTech screen): Windows' built-in OCR finds the bottom date, then
  reads the clock line under it with its colons blanked out. The engine drops
  the digits of "10:12:01 AM" and returns just "AM" (2026-10-29: 3 of 40 real
  captures, at every crop and scale tried); with the two 2-px colon columns
  painted white it read all 47 captures on disk, zeros sometimes as "O".
- DragonCam (TeamViewer): OCR reads Ubuntu's top-bar clock ("Oct 29 10:15"),
  minute precision. The app's status-bar "Time: hh:mm:ss" is NOT the live
  clock: on 2026-10-29 it lagged the top bar in 32 of 132 local captures, by
  up to 82 minutes, so it is never used (it would fail good devices).
- OvrC WattBox (Time Settings page): OCR reads "CURRENT DEVICE TIME
  2026-10-29 13:51:11" (24-hour). The page is captured at 2x; at 1x the engine
  misread 13:51:11 as "1361:11", so a reading counts only once two OCR passes
  agree on it.

A clock that can't be read gives None: the review page then says "Unread",
never a fail. Readings are wall clocks with no zone: what the device displays.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

CLOCK_SIDECAR_SUFFIX = ".clock.txt"

_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})(?:\.\d+)?\s*([AP]M)?", re.I)
# "10 12 01 AM": the clock line with its colons blanked (see _blank_colons).
_BLANKED_CLOCK_RE = re.compile(r"(\d{1,2})\s+(\d{2})\s+(\d{2})\s*([AP]M)?", re.I)
_DIGIT_LOOKALIKES = str.maketrans("OoIl", "0011")
# A colon on the LaserTech clock line is 2 px wide; the narrowest digit ("1") is 6.
_COLON_MAX_WIDTH = 2
_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_TOP_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*(\d{1,2})\s+(\d{1,2}):(\d{2})\s*([AP]M)?", re.I
)
# WattBox "2026-10-29 13:51:11"; OCR may drop the colons or the date's hyphens.
_WATTBOX_RE = re.compile(r"(\d{4})\s*-?\s*(\d{2})\s*-\s*(\d{2})\s*(\d{2})\s*:?\s*(\d{2})\s*:?\s*(\d{2})(?!\d)")
# "Thu, 29 Oct 2026 09:12:19"
_CBW_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})")



@dataclass(frozen=True)
class Reading:
    raw: str  # the text the clock was read from
    wall: datetime  # naive: the device's wall clock
    source: str  # "page" | "ocr" | "ocr-min" (minute precision) | "ovrc"

    @property
    def iso(self) -> str:
        return self.wall.replace(microsecond=0).isoformat()


def _hour24(hh: int, ampm: str | None) -> int:
    ampm = (ampm or "").upper()
    if ampm == "PM" and hh != 12:
        return hh + 12
    if ampm == "AM" and hh == 12:
        return 0
    return hh



# ── CBW (page text) ────────────────────────────────────────────────────────


def parse_cbw_current(text: str) -> datetime | None:
    m = _CBW_RE.search(text or "")
    if not m or m[2].lower() not in _MONTHS:
        return None
    return datetime(int(m[3]), _MONTHS[m[2].lower()], int(m[1]), int(m[4]), int(m[5]), int(m[6]))


def write_clock_sidecar(image: Path, text: str) -> None:
    try:
        Path(str(image) + CLOCK_SIDECAR_SUFFIX).write_text(text or "", encoding="utf-8")
    except OSError:
        logger.warning("[CLOCK] could not write sidecar for %s", image, exc_info=True)


def _read_sidecar(image: Path) -> Reading | None:
    side = Path(str(image) + CLOCK_SIDECAR_SUFFIX)
    try:
        text = side.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    wall = parse_cbw_current(text)
    return Reading(text[:128], wall, "page") if wall else None


# ── OCR (Windows.Media.Ocr) ────────────────────────────────────────────────


async def _ocr_lines_async(img):
    from PIL import Image
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    rgba = img.convert("RGBA")
    r, g, b, a = rgba.split()
    writer = DataWriter()
    writer.write_bytes(Image.merge("RGBA", (b, g, r, a)).tobytes())
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, rgba.width, rgba.height, BitmapAlphaMode.PREMULTIPLIED
    )
    result = await OcrEngine.try_create_from_user_profile_languages().recognize_async(bitmap)
    out = []
    for line in result.lines:
        rects = [w.bounding_rect for w in line.words]
        box = (
            min(r.x for r in rects),
            min(r.y for r in rects),
            max(r.x + r.width for r in rects),
            max(r.y + r.height for r in rects),
        )
        out.append((line.text, box))
    return out


def ocr_lines(img) -> list[tuple[str, tuple[float, float, float, float]]]:
    """[(line text, (x0, y0, x1, y1))]. Runs its own event loop: call from any thread."""
    return asyncio.run(_ocr_lines_async(img))


def _ocr_crop(img, box, scale: int) -> str:
    from PIL import Image, ImageOps

    x0, y0, x1, y1 = box
    box = (max(0, int(x0)), max(0, int(y0)), min(img.width, int(x1)), min(img.height, int(y1)))
    if box[2] <= box[0] or box[3] <= box[1]:
        return ""
    crop = img.crop(box)
    big = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
    return " ".join(t for t, _ in ocr_lines(ImageOps.expand(big, 30, fill="white")))


def _dark_runs(img, box) -> list[tuple[int, int]]:
    """[x0, x1) spans of columns in box that contain a dark pixel (the glyphs)."""
    x0, y0, x1, y1 = box
    px = img.load()
    runs: list[tuple[int, int]] = []
    start = None
    for x in range(x0, x1 + 1):
        dark = x < x1 and any(sum(px[x, y][:3]) < 400 for y in range(y0, y1))
        if dark and start is None:
            start = x
        elif not dark and start is not None:
            runs.append((start, x))
            start = None
    return runs


def _blank_colons(img, box):
    """A copy of img with the colons on the clock line in box painted white."""
    out = img.copy()
    px = out.load()
    for x0, x1 in _dark_runs(img, box):
        if x1 - x0 <= _COLON_MAX_WIDTH:
            for x in range(x0, x1):
                for y in range(box[1], box[3]):
                    px[x, y] = (255, 255, 255)
    return out


def _blank_dot_pairs(img, box):
    """A copy of img with every colon in box painted white, found by shape.

    A colon's columns are dark in two clusters with a gap between (its dots);
    a digit's are not. Width alone can't tell: at 1x a WattBox "1" is 2 px.
    """
    out = img.copy()
    px = out.load()
    src = img.load()
    for x0, x1 in _dark_runs(img, box):
        rows = [y for y in range(box[1], box[3]) if any(sum(src[x, y][:3]) < 400 for x in range(x0, x1))]
        gaps = [b - a for a, b in zip(rows, rows[1:]) if b - a > 1]
        if len(gaps) == 1 and gaps[0] - 1 >= 2 and x1 - x0 <= (rows[-1] - rows[0] + 1) / 2:
            for x in range(x0, x1):
                for y in range(box[1], box[3]):
                    px[x, y] = (255, 255, 255)
    return out


def _first_clock(img, boxes, scales) -> tuple[re.Match, str] | tuple[None, str]:
    for box in boxes:
        for scale in scales:
            text = _ocr_crop(img, box, scale)
            found = list(_CLOCK_RE.finditer(text))
            if found:
                return found[-1], text
    return None, ""


def read_vnc(img) -> Reading | None:
    """LaserTech screen: the live clock sits under the bottom date."""
    found = ocr_lines(img)
    dates = [(t, box) for t, box in found if _DATE_RE.search(t)]
    if not dates:
        return None
    text, (x0, y0, x1, y1) = max(dates, key=lambda d: d[1][1])
    date = _DATE_RE.search(text)
    h = y1 - y0
    line = (max(0, int(x0) - 12), int(y1), min(img.width, int(x1) + 25), min(img.height, int(y1 + h + 14)))
    blanked = _ocr_crop(_blank_colons(img, line), line, 3).translate(_DIGIT_LOOKALIKES)
    clock = _BLANKED_CLOCK_RE.search(blanked)
    if clock is None:
        # The colon-ful ladder that read 37 of 40 before: a fallback only.
        boxes = [
            (x0 - 12, y1, x1 + 10, y1 + h + 12),
            (x0 - 12, y1, x1 - 15, y1 + h + 12),
            (x0 - 12, y1 - 2, x1 + 25, y1 + h + 16),
        ]
        clock, _text = _first_clock(img, boxes, (2, 3, 1))
    if clock is None:
        return None
    hh, mm, ss, ampm = int(clock[1]), int(clock[2]), int(clock[3]), (clock[4] or "").upper()
    if hh > 23 or mm > 59 or ss > 59:
        return None
    wall = datetime(int(date[3]), int(date[1]), int(date[2]), _hour24(hh, ampm), mm, ss)
    shown = f"{hh}:{mm:02d}:{ss:02d}" + (f" {ampm}" if ampm else "")
    return Reading(f"{date[0]} {shown}", wall, "ocr")


def read_dragoncam(img, expected: datetime) -> Reading | None:
    """DragonCam (Ubuntu): the top-bar system clock, e.g. "Oct 29 10:15"."""
    found = ocr_lines(img)
    for text, (x0, y0, x1, y1) in sorted(found, key=lambda f: f[1][1]):
        if y0 > img.height * 0.2 or not re.match(r"(?i)\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", text):
            continue
        for scale in (2, 3):
            m = _TOP_RE.search(_ocr_crop(img, (x0 - 40, y0 - 6, x1 + 120, y1 + 6), scale))
            if m:
                month, day = _MONTHS[m[1][:3].lower()], int(m[2])
                # Year from the capture; a reading across New Year takes the nearer one.
                wall = min(
                    (
                        datetime(year, month, day, _hour24(int(m[3]), m[5]), int(m[4]))
                        for year in (expected.year - 1, expected.year, expected.year + 1)
                    ),
                    key=lambda c: abs(c - expected),
                )
                return Reading(m[0][:128], wall, "ocr-min")
    return None


def _parse_wattbox(text: str) -> datetime | None:
    m = _WATTBOX_RE.search((text or "").translate(_DIGIT_LOOKALIKES))
    if not m:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()))
    except ValueError:  # e.g. minute 61 from a misread digit
        return None


def read_wattbox(img) -> Reading | None:
    """OvrC WattBox Time Settings: the line under "CURRENT DEVICE TIME".

    Light text on OvrC's dark theme, so OCR runs on the inverted page and the
    original: the whole page, then the value line at a few scales, also with
    its colons blanked (the engine drops a "13:51:11" it can't split, as on
    the VNC clock). The first time two passes agree on is the reading.
    """
    from PIL import ImageOps

    inverted = ImageOps.invert(img.convert("L")).convert("RGB")
    found = ocr_lines(inverted)
    labels = [box for text, box in found if "DEVICE TIME" in text.upper()]
    if not labels:
        return None
    x0, _y0, _x1, y1 = labels[0]
    h = labels[0][3] - labels[0][1]

    def value_line(lines) -> tuple[str, tuple | None]:
        """The first line below the label, left-aligned with it."""
        below = [(t, b) for t, b in lines if b[1] >= y1 - 1 and abs(b[0] - x0) < 4 * h]
        return min(below, key=lambda f: f[1][1], default=("", None))

    def passes():
        text, box = value_line(found)
        yield text
        yield value_line(ocr_lines(img))[0]
        # The value's box can stop at the date when OCR drops the time: reach past it.
        bx0, by0, by1 = (box[0], box[1], box[3]) if box else (x0, y1, y1 + 2 * h)
        for pad in (4, 10):
            crop = (bx0 - pad, by0 - pad, bx0 + 30 * h, by1 + pad)
            crop = tuple(int(v) for v in crop)
            blanked = _blank_dot_pairs(inverted, crop)
            for scale in (1, 2, 3):
                yield _ocr_crop(blanked, crop, scale)
                yield _ocr_crop(inverted, crop, scale)
                yield _ocr_crop(img, crop, scale)

    seen: dict[datetime, str] = {}
    for raw in passes():
        wall = _parse_wattbox(raw)
        if wall is None:
            continue
        if wall in seen:
            return Reading(seen[wall][:128], wall, "ocr")
        seen[wall] = raw
    return None


def read_capture(label: str, image: Path, expected: datetime) -> Reading | None:
    """The device clock in a saved capture, by its label; None when not readable.

    `expected` is the capture time on the site's wall clock; it only picks the
    year for DragonCam's "Oct 29 10:15".
    """
    if label == "cbw":
        return _read_sidecar(image)
    if label.startswith("vnc"):
        kind = "vnc"
    elif label.startswith("de_l") or label == "de_tv":
        # de_tv: the one capture of a DE site whose TeamViewer row has no lane.
        kind = "de"
    elif label.startswith("ovrc_") and label.endswith("_wattbox"):
        kind = "wattbox"
    else:
        return None
    try:
        from PIL import Image

        with Image.open(image) as im:
            img = im.convert("RGB")
        if kind == "vnc":
            return read_vnc(img)
        if kind == "wattbox":
            return read_wattbox(img)
        return read_dragoncam(img, expected)
    except Exception:  # noqa: BLE001 — OCR missing or failing only leaves the time unread
        logger.warning("[CLOCK] reading %s failed", image, exc_info=True)
        return None
