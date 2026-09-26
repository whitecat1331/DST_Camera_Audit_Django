"""Parse the three-column rejection-service CSV.

Expected shape (headers may vary slightly):

    Row Label,Count of Reason,Rejection Reason
    DS012167,1,No FIM Video
    FX1263,1,No FIM Video
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

_SERIAL_HEADERS = {
    "row label",
    "row_label",
    "serial",
    "serial number",
    "serial_number",
    "label",
    "camera serial",
    "camera_serial",
    "unit serial",
    "unit_serial",
}
_COUNT_HEADERS = {"count of reason", "count_of_reason", "count", "count of"}
_REASON_HEADERS = {"rejection reason", "rejection_reason", "reason", "rejection"}


@dataclass(frozen=True)
class RejectionRow:
    serial: str
    count: str = ""
    reason: str = ""
    source_row: int = 0


def _norm_header(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def _index_by_header(headers: list[str], aliases: set[str]) -> int | None:
    for idx, raw in enumerate(headers):
        if _norm_header(raw) in aliases:
            return idx
    return None


def _decode(text: str | bytes) -> str:
    if isinstance(text, bytes):
        return text.decode("utf-8-sig", errors="replace")
    return text.lstrip("\ufeff")


def _is_header_cell(value: str) -> bool:
    return _norm_header(value) in (_SERIAL_HEADERS | _COUNT_HEADERS | _REASON_HEADERS)


def parse_rejection_csv(text: str | bytes) -> list[RejectionRow]:
    """Parse CSV content into ordered rejection rows."""
    content = _decode(text)
    reader = csv.reader(io.StringIO(content))
    raw_rows: list[list[str]] = [row for row in reader if any((c or "").strip() for c in row)]
    if not raw_rows:
        return []

    serial_idx = 0
    count_idx = 1
    reason_idx = 2
    first = raw_rows[0]
    start = 0
    if _is_header_cell(first[0]):
        headers = [c for c in first]
        serial_idx = _index_by_header(headers, _SERIAL_HEADERS) or 0
        count_idx = _index_by_header(headers, _COUNT_HEADERS)
        if count_idx is None:
            count_idx = 1
        reason_idx = _index_by_header(headers, _REASON_HEADERS)
        if reason_idx is None:
            reason_idx = 2
        start = 1

    def cell(row: list[str], idx: int) -> str:
        return (row[idx] if 0 <= idx < len(row) else "").strip()

    rows: list[RejectionRow] = []
    for source_row, row in enumerate(raw_rows[start:], start=start + 1):
        serial = cell(row, serial_idx)
        if not serial:
            continue
        rows.append(
            RejectionRow(
                serial=serial.strip().upper(),
                count=cell(row, count_idx),
                reason=cell(row, reason_idx),
                source_row=source_row,
            )
        )
    return rows
