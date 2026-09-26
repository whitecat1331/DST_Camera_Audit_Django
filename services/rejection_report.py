"""Build the rejection-report spreadsheet (.xlsx) with embedded screenshots."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

HEADERS = ["Row Label", "Count of Reason", "Rejection Reason", "Result", "Screenshot"]
_MAX_WIDTH = 640
_MAX_HEIGHT = 480


@dataclass
class ReportRow:
    serial: str
    count: str
    reason: str
    result: str
    screenshot_path: str = ""  # absolute path to a PNG


def _downscale(src: Path) -> Path | None:
    """Return a temp PNG downscaled to fit a spreadsheet cell, or None."""
    try:
        from PIL import Image
    except ImportError:
        return src if src.exists() else None

    try:
        with Image.open(src) as image:
            width, height = image.size
            scale = min(1.0, _MAX_WIDTH / width, _MAX_HEIGHT / height)
            if scale >= 1.0:
                return src
            new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
            converted = image.convert("RGB")
            tmp = Path(tempfile.mkstemp(suffix=".png")[1])
            converted.resize(new_size, Image.LANCZOS).save(tmp, "PNG")
            return tmp
    except Exception:  # noqa: BLE001 — image embedding is best-effort
        return None


def build_rejection_report(
    rows: list[ReportRow],
    xlsx_path: str | Path,
) -> Path:
    """Write the .xlsx with the original three columns plus Result and Screenshot."""
    xlsx_path = Path(xlsx_path)
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)

    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Rejection Report"
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for row_index, row in enumerate(rows, start=2):
        ws.cell(row=row_index, column=1, value=row.serial)
        ws.cell(row=row_index, column=2, value=row.count)
        ws.cell(row=row_index, column=3, value=row.reason)
        result_cell = ws.cell(row=row_index, column=4, value=row.result)
        result_cell.alignment = Alignment(wrap_text=True, vertical="top")

        image_path = Path(row.screenshot_path) if row.screenshot_path else None
        embed = _downscale(image_path) if (image_path and image_path.exists()) else None
        if embed is not None:
            image = XLImage(str(embed))
            ws.add_image(image, f"E{row_index}")
            height_points = max(15.0, image.height * 0.75)
            ws.row_dimensions[row_index].height = height_points

    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 28
    ws.column_dimensions["D"].width = 32
    ws.column_dimensions["E"].width = _MAX_WIDTH / 7 + 4
    wb.save(xlsx_path)

    return xlsx_path
