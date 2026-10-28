"""One-page review of a published DST audit round.

One row per camera (site key) in the round, built from the **newest** DST_SITE
job for that camera — the LTI run, the DE run and any re-runs merge into one
list. Query count is constant in the number of cameras (each query is ~32 ms
from the review Linode to Supabase).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from urllib.parse import urlencode

from django.db.models import F, Q

from audits.models import AuditJob, AuditRound, AuditScreenshot, InstallationAuditReview
from audits.ovrc_time import EASTERN
from audits.thumbs import normalize_thumb_label

# OvrC shows minutes only (±60 s), so flag only clear drift.
DRIFT_FLAG_SECONDS = 120
_LAYER_ORDER = ("cbw", "vnc_l1", "vnc_l2", "de_l1", "de_l2", "de_l3")
KINDS = ("lti", "de")
STATUSES = ("incomplete", "complete")


@dataclass
class Capture:
    key: str
    shot: AuditScreenshot


@dataclass
class ReviewRow:
    key: str
    job: AuditJob
    installation: object | None
    kind: str
    attempts: int
    captures: list[Capture] = field(default_factory=list)
    review: InstallationAuditReview | None = None

    @property
    def assigned_to(self) -> str:
        return self.review.assigned_to if self.review else ""

    @property
    def completed(self) -> bool:
        return bool(self.review and self.review.completed)

    @property
    def failed(self) -> bool:
        return self.job.status != AuditJob.Status.SUCCEEDED or bool(
            (self.job.error_message or "").strip()
        )

    @property
    def drift_flagged(self) -> bool:
        d = self.job.drift_seconds
        return d is not None and abs(d) >= DRIFT_FLAG_SECONDS

    @property
    def drift_minutes(self) -> float | None:
        d = self.job.drift_seconds
        return None if d is None else round(d / 60, 1)

    @property
    def eastern_at_read(self) -> str:
        """Eastern wall clock when OvrC was read — what the device should show."""
        at = self.job.ovrc_captured_at
        return at.astimezone(EASTERN).strftime("%b %d, %Y %I:%M %p %Z") if at else ""


def _capture_sort(c: Capture) -> tuple[int, str]:
    if c.key in _LAYER_ORDER:
        return (_LAYER_ORDER.index(c.key), c.key)
    if c.key.startswith("de_l"):
        return (len(_LAYER_ORDER), c.key)
    if c.key.startswith("ovrc_"):
        return (len(_LAYER_ORDER) + 1, c.key)
    return (len(_LAYER_ORDER) + 2, c.key)


def _installations_by_key(keys: list[str]) -> dict[str, object]:
    from cameras.models import Installation

    found: dict[str, object] = {}
    rows = Installation.objects.filter(Q(pole_number__in=keys) | Q(identifier__in=keys)).order_by(
        "-is_active", "-last_synced_at"
    )
    for inst in rows:
        for k in (inst.pole_number, inst.identifier):
            if k and k in keys:
                found.setdefault(k, inst)
    return found


def build_round_rows(round_: AuditRound) -> list[ReviewRow]:
    jobs = (
        AuditJob.objects.filter(round=round_, device_type=AuditJob.DeviceType.DST_SITE)
        .prefetch_related("screenshots")
        .order_by(
            "pole_number",
            F("finished_at").desc(nulls_last=True),
            F("created_at").desc(),
        )
    )
    newest: dict[str, AuditJob] = {}
    attempts: Counter[str] = Counter()
    for job in jobs:
        key = (job.pole_number or "").strip() or f"job-{job.pk}"
        newest.setdefault(key, job)
        attempts[key] += 1

    installations = _installations_by_key(list(newest))
    reviews = {r.camera_key: r for r in InstallationAuditReview.objects.filter(round=round_)}
    rows: list[ReviewRow] = []
    for key, job in newest.items():
        kind = (job.target_host or "").strip().lower()
        row = ReviewRow(
            key=key,
            job=job,
            installation=installations.get(key),
            kind=kind if kind in {"lti", "de"} else "",
            attempts=attempts[key],
            review=reviews.get(key),
        )
        for shot in job.screenshots.all():
            if shot.image:
                row.captures.append(Capture(normalize_thumb_label(shot) or shot.label, shot))
        row.captures.sort(key=_capture_sort)
        rows.append(row)
    rows.sort(key=lambda r: (r.kind != "lti", getattr(r.installation, "identifier", "") or r.key))
    return rows


def summarize(rows: list[ReviewRow]) -> dict[str, int]:
    return {
        "cameras": len(rows),
        "lti": sum(r.kind == "lti" for r in rows),
        "de": sum(r.kind == "de" for r in rows),
        "failed": sum(r.failed for r in rows),
        "drift": sum(r.drift_flagged for r in rows),
        "complete": sum(r.completed for r in rows),
        "unassigned": sum(not r.assigned_to for r in rows),
    }


def tech_progress(rows: list[ReviewRow], reviewers: list[str]) -> list[dict]:
    """[{name, total, complete}] in the round's reviewer order, then any others."""
    names = list(reviewers) + sorted({r.assigned_to for r in rows if r.assigned_to} - set(reviewers))
    return [
        {
            "name": n,
            "total": sum(r.assigned_to == n for r in rows),
            "complete": sum(r.assigned_to == n and r.completed for r in rows),
        }
        for n in names
    ]


@dataclass(frozen=True)
class Filters:
    """The review page's filter panel (IMS-style: independent fields, "" = All)."""

    tech: str = ""  # "" | "mine" | "unassigned" | a username
    kind: str = ""  # "" | "lti" | "de"
    status: str = ""  # "" | "incomplete" | "complete"
    drift: bool = False
    failed: bool = False

    @property
    def active(self) -> bool:
        return self != Filters()

    def query(self, **changes) -> str:
        """Query string for these filters with ``changes`` applied (active fields only)."""
        f = replace(self, **changes)
        params = {k: v for k, v in (("tech", f.tech), ("kind", f.kind), ("status", f.status)) if v}
        params.update({k: "1" for k in ("drift", "failed") if getattr(f, k)})
        return "?" + urlencode(params) if params else "?"


def filter_rows(rows: list[ReviewRow], f: Filters, username: str = "") -> list[ReviewRow]:
    if f.tech == "mine":
        rows = [r for r in rows if r.assigned_to == username]
    elif f.tech == "unassigned":
        rows = [r for r in rows if not r.assigned_to]
    elif f.tech:
        rows = [r for r in rows if r.assigned_to == f.tech]
    if f.kind:
        rows = [r for r in rows if r.kind == f.kind]
    if f.status == "incomplete":
        rows = [r for r in rows if not r.completed]
    elif f.status == "complete":
        rows = [r for r in rows if r.completed]
    if f.drift:
        rows = [r for r in rows if r.drift_flagged]
    if f.failed:
        rows = [r for r in rows if r.failed]
    return rows
