"""Parse and store DragonEye TeamViewer ID CSV mappings."""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction

logger = logging.getLogger(__name__)

# FX1051 Apollo… | FX1070 L1 Sheridan… | FX1074L1
_FX_LANE_RE = re.compile(
    r"^(FX\d+)\s*(L\d+)?\s*(.*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedTvRow:
    fx_number: str
    lane: str  # "" or L1/L2/…
    teamviewer_id: str
    label: str


def parse_fx_label(raw: str) -> tuple[str, str, str] | None:
    """Return (FX####, lane, remainder) or None."""
    text = (raw or "").strip()
    if not text:
        return None
    m = _FX_LANE_RE.match(text)
    if not m:
        return None
    fx = m.group(1).upper()
    lane = (m.group(2) or "").upper()
    rest = (m.group(3) or "").strip()
    return fx, lane, rest


def parse_dragoneye_csv(text: str | bytes) -> list[ParsedTvRow]:
    """Parse DragonEye Teamviewer IDs.csv content into rows."""
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig", errors="replace")
    else:
        text = text.lstrip("\ufeff")
    reader = csv.reader(io.StringIO(text))
    rows: list[ParsedTvRow] = []
    for raw in reader:
        if not raw:
            continue
        label_cell = (raw[0] or "").strip().lstrip("\ufeff")
        tv_id = (raw[1] if len(raw) > 1 else "").strip()
        if not label_cell or not tv_id:
            continue
        if not tv_id.isdigit():
            continue
        parsed = parse_fx_label(label_cell)
        if not parsed:
            logger.warning("[DE] skip CSV row without FX: %s", label_cell[:80])
            continue
        fx, lane, rest = parsed
        rows.append(
            ParsedTvRow(
                fx_number=fx,
                lane=lane,
                teamviewer_id=tv_id,
                label=rest or label_cell,
            )
        )
    return rows


@transaction.atomic
def replace_mappings_from_csv(
    text: str | bytes,
    *,
    source_filename: str = "",
) -> dict[str, int]:
    """Replace all DragonEye TeamViewer mappings from CSV content."""
    from cameras.models import DragonEyeTeamViewerId

    parsed = parse_dragoneye_csv(text)
    # CSV can list the same FX/lane more than once — keep last wins.
    by_key: dict[tuple[str, str], ParsedTvRow] = {}
    for r in parsed:
        by_key[(r.fx_number, r.lane)] = r
    unique = list(by_key.values())
    DragonEyeTeamViewerId.objects.all().delete()
    objs = [
        DragonEyeTeamViewerId(
            fx_number=r.fx_number,
            lane=r.lane,
            teamviewer_id=r.teamviewer_id,
            label=r.label[:255],
            source_filename=(source_filename or "")[:255],
        )
        for r in unique
    ]
    DragonEyeTeamViewerId.objects.bulk_create(objs, batch_size=200)
    lanes = sum(1 for r in unique if r.lane)
    logger.info(
        "[DE] imported TeamViewer IDs count=%s with_lane=%s file=%s (raw_rows=%s)",
        len(unique),
        lanes,
        source_filename or "(upload)",
        len(parsed),
    )
    return {"count": len(unique), "with_lane": lanes, "raw_rows": len(parsed)}


def import_csv_file(path: str | Path) -> dict[str, int]:
    path = Path(path)
    return replace_mappings_from_csv(path.read_bytes(), source_filename=path.name)


def mappings_for_fx(fx_number: str) -> list:
    from cameras.models import DragonEyeTeamViewerId

    fx = (fx_number or "").strip().upper()
    if not fx:
        return []
    return list(DragonEyeTeamViewerId.objects.filter(fx_number=fx).order_by("lane"))


def ensure_default_csv_loaded(base_dir: Path) -> dict[str, int] | None:
    """If DB empty, load project-root DragonEye Teamviewer IDs.csv when present."""
    from cameras.models import DragonEyeTeamViewerId

    if DragonEyeTeamViewerId.objects.exists():
        return None
    candidates = [
        base_dir / "DragonEye Teamviewer IDs.csv",
        base_dir / "DragonEye Teamviewer Ids.csv",
        base_dir / "data" / "DragonEye Teamviewer IDs.csv",
    ]
    for path in candidates:
        if path.is_file():
            return import_csv_file(path)
    return None
