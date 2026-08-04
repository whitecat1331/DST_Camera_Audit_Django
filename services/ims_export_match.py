"""Match IMS installation Excel exports to local DST Installation rows.

Accepts the Access Replacement / IMS multi-sheet export (Installations + Devices)
or a simple sheet with Identifier / Pole / Serial / Installation ID columns.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import pandas as pd

_SERIAL_SPLIT_RE = re.compile(r"[,;/|\s]+")
_SKIP_SERIALS = {"", "n/a", "na", "?", "-", "none", "null"}


def _cell(value: Any, default: str = "") -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _optional_int(value: Any) -> int | None:
    text = _cell(value)
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _norm_key(value: str) -> str:
    return (value or "").strip().upper()


def _serial_tokens(raw: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for part in _SERIAL_SPLIT_RE.split(raw or ""):
        tok = part.strip()
        if not tok or tok.lower() in _SKIP_SERIALS:
            continue
        key = tok.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
    return out


def _col_map(columns: list[Any]) -> dict[str, str]:
    """Map normalized header → original column name."""
    out: dict[str, str] = {}
    for col in columns:
        name = str(col).strip()
        if not name:
            continue
        key = re.sub(r"\s+", " ", name).strip().lower()
        out[key] = name
    return out


def _pick(cmap: dict[str, str], *aliases: str) -> str | None:
    for alias in aliases:
        if alias in cmap:
            return cmap[alias]
    return None


@dataclass
class IMSExportRow:
    excel_row: int
    ims_id: int | None = None
    identifier: str = ""
    pole: str = ""
    serial: str = ""
    name: str = ""
    agency: str = ""
    state: str = ""
    source_sheet: str = ""


@dataclass
class MatchResult:
    row: IMSExportRow
    installation_id: int | None = None
    ims_id: int | None = None
    identifier: str = ""
    pole: str = ""
    kind: str = ""  # lti | de | ""
    match_by: str = ""
    status: str = "unmatched"  # matched | ineligible | unmatched | duplicate
    reason: str = ""


@dataclass
class MatchReport:
    rows: list[MatchResult] = field(default_factory=list)
    matched_installation_ids: list[int] = field(default_factory=list)
    sheet_names: list[str] = field(default_factory=list)

    @property
    def matched_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "matched")

    @property
    def unmatched_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "unmatched")

    @property
    def ineligible_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "ineligible")


def parse_ims_export_rows(source: str | Path | BinaryIO) -> tuple[list[IMSExportRow], list[str]]:
    """Parse Installations / Devices sheets into distinct candidate rows."""
    xl = pd.ExcelFile(source)
    sheet_names = list(xl.sheet_names)
    by_key: dict[str, IMSExportRow] = {}

    def add(row: IMSExportRow) -> None:
        if row.ims_id:
            key = f"ims:{row.ims_id}"
        elif row.identifier:
            key = f"id:{_norm_key(row.identifier)}"
        elif row.pole:
            key = f"pole:{_norm_key(row.pole)}|{_norm_key(row.serial)}"
        elif row.serial:
            key = f"serial:{_norm_key(row.serial)}"
        else:
            return
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = row
            return
        # Prefer richer Installations-sheet rows over Devices-only stubs.
        if not existing.identifier and row.identifier:
            existing.identifier = row.identifier
        if not existing.pole and row.pole:
            existing.pole = row.pole
        if not existing.serial and row.serial:
            existing.serial = row.serial
        if not existing.agency and row.agency:
            existing.agency = row.agency
        if not existing.state and row.state:
            existing.state = row.state
        if not existing.name and row.name:
            existing.name = row.name
        if existing.ims_id is None and row.ims_id is not None:
            existing.ims_id = row.ims_id

    preferred = [n for n in sheet_names if str(n).strip().lower() == "installations"]
    preferred += [n for n in sheet_names if str(n).strip().lower() == "devices"]
    preferred += [n for n in sheet_names if n not in preferred]

    for sheet in preferred:
        df = pd.read_excel(xl, sheet_name=sheet)
        if df.empty:
            continue
        cmap = _col_map(list(df.columns))
        ims_col = _pick(cmap, "installation id", "installation_id", "ims id", "ims_id")
        # Avoid matching bare "id" on Devices (could collide); prefer explicit names.
        if ims_col is None and "id" in cmap and str(sheet).strip().lower() != "devices":
            ims_col = cmap["id"]
        ident_col = _pick(cmap, "identifier", "installation identifier", "site id")
        pole_col = _pick(cmap, "pole", "pole number", "pole_number")
        serial_col = _pick(cmap, "serial", "serial number", "serial_number", "unit serial")
        name_col = _pick(cmap, "name", "installation name")
        agency_col = _pick(cmap, "agency")
        state_col = _pick(cmap, "state")
        if not any([ims_col, ident_col, pole_col, serial_col]):
            continue

        for excel_row, (_, series) in enumerate(df.iterrows(), start=2):
            def get(col: str | None) -> str:
                if not col:
                    return ""
                return _cell(series.get(col, ""))

            row = IMSExportRow(
                excel_row=excel_row,
                ims_id=_optional_int(series.get(ims_col)) if ims_col else None,
                identifier=get(ident_col),
                pole=get(pole_col),
                serial=get(serial_col),
                name=get(name_col),
                agency=get(agency_col),
                state=get(state_col),
                source_sheet=str(sheet),
            )
            if not any([row.ims_id, row.identifier, row.pole, row.serial]):
                continue
            add(row)

    return list(by_key.values()), sheet_names


def match_ims_export_to_installations(
    source: str | Path | BinaryIO,
    *,
    kind_fn=None,
) -> MatchReport:
    """Resolve Excel rows to active DST installations eligible for confirm capture."""
    from cameras.models import Installation

    if kind_fn is None:
        from audits.runner import _dst_site_kind as kind_fn

    rows, sheet_names = parse_ims_export_rows(source)
    report = MatchReport(sheet_names=sheet_names)
    if not rows:
        return report

    active = list(Installation.objects.filter(is_active=True))
    by_ims: dict[int, Any] = {inst.ims_id: inst for inst in active if inst.ims_id}
    by_ident: dict[str, Any] = {}
    by_pole: dict[str, list[Any]] = {}
    by_serial: dict[str, list[Any]] = {}

    for inst in active:
        ident = _norm_key(inst.identifier or "")
        if ident and ident not in by_ident:
            by_ident[ident] = inst
        pole = _norm_key(inst.pole_number or "")
        if pole:
            by_pole.setdefault(pole, []).append(inst)
        for tok in _serial_tokens(inst.serial_number or ""):
            by_serial.setdefault(_norm_key(tok), []).append(inst)
        for cam in (inst.camera_a, inst.camera_b, getattr(inst, "camera_c", "")):
            for tok in _serial_tokens(cam or ""):
                by_serial.setdefault(_norm_key(tok), []).append(inst)
        for fx in inst.fx_numbers or []:
            by_serial.setdefault(_norm_key(fx), []).append(inst)

    seen_pks: set[int] = set()
    matched_ids: list[int] = []

    for row in rows:
        result = MatchResult(row=row)
        inst = None
        match_by = ""

        if row.ims_id is not None and row.ims_id in by_ims:
            inst = by_ims[row.ims_id]
            match_by = "installation_id"
        elif row.identifier and _norm_key(row.identifier) in by_ident:
            inst = by_ident[_norm_key(row.identifier)]
            match_by = "identifier"
        else:
            pole_hits = by_pole.get(_norm_key(row.pole), []) if row.pole else []
            if len(pole_hits) == 1:
                inst = pole_hits[0]
                match_by = "pole"
            elif len(pole_hits) > 1 and row.serial:
                serial_keys = {_norm_key(t) for t in _serial_tokens(row.serial)}
                narrowed = []
                for candidate in pole_hits:
                    c_serials = {
                        _norm_key(t)
                        for t in _serial_tokens(candidate.serial_number or "")
                    }
                    c_serials.update(
                        _norm_key(t)
                        for cam in (
                            candidate.camera_a,
                            candidate.camera_b,
                            getattr(candidate, "camera_c", ""),
                        )
                        for t in _serial_tokens(cam or "")
                    )
                    c_serials.update(_norm_key(fx) for fx in (candidate.fx_numbers or []))
                    if serial_keys & c_serials:
                        narrowed.append(candidate)
                if len(narrowed) == 1:
                    inst = narrowed[0]
                    match_by = "pole+serial"
            if inst is None and row.serial:
                serial_hits: list[Any] = []
                seen_hit: set[int] = set()
                for tok in _serial_tokens(row.serial):
                    for candidate in by_serial.get(_norm_key(tok), []):
                        if candidate.pk in seen_hit:
                            continue
                        seen_hit.add(candidate.pk)
                        serial_hits.append(candidate)
                if len(serial_hits) == 1:
                    inst = serial_hits[0]
                    match_by = "serial"

        if inst is None:
            result.status = "unmatched"
            result.reason = "No matching DST installation"
            report.rows.append(result)
            continue

        result.installation_id = inst.pk
        result.ims_id = inst.ims_id
        result.identifier = (inst.identifier or "").strip()
        result.pole = (inst.pole_number or "").strip()
        result.match_by = match_by

        if inst.pk in seen_pks:
            result.status = "duplicate"
            result.reason = "Already matched earlier in this file"
            report.rows.append(result)
            continue

        kind = kind_fn(inst) or ""
        result.kind = kind
        if not kind:
            result.status = "ineligible"
            result.reason = (
                "Matched but not eligible for confirm capture "
                "(need DE/FX TeamViewer or LTI pole)"
            )
            report.rows.append(result)
            continue

        seen_pks.add(inst.pk)
        matched_ids.append(inst.pk)
        result.status = "matched"
        result.reason = f"Matched by {match_by}"
        report.rows.append(result)

    report.matched_installation_ids = matched_ids
    return report
