"""One-page review of a published DST audit round.

One entry per camera (site key) in the round, built from the **newest** DST_SITE
job for that camera — the LTI run, the DE run and any re-runs merge into one
list. Each camera shows one line per capture: when it was grabbed (Eastern),
the clock the device showed, what that clock should have shown in the site's
own zone, the drift and a pass/fail. Query count is constant in the number of
cameras (each query is ~32 ms from the review Linode to Supabase).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from urllib.parse import urlencode

from django.db.models import F, Q

from audits import device_drift
from audits.models import AuditJob, AuditRound, AuditScreenshot, InstallationAuditReview
from audits.ovrc_time import EASTERN, parse_ovrc_local
from audits.site_timezone import site_timezone
from audits.thumbs import normalize_thumb_label

DRIFT_FLAG_SECONDS = device_drift.PASS_SECONDS
_LAYER_ORDER = ("cbw", "vnc_l1", "vnc_l2", "de_l1", "de_l2", "de_l3")
# Captures that show a device clock (services/device_clock.py, OvrC).
_CLOCK_PREFIXES = ("cbw", "vnc_", "de_l", "ovrc_")
KINDS = ("lti", "de")
STATUSES = ("incomplete", "complete")

_SOURCE_LABELS = {"page": "CBW page", "ocr": "read from image", "ocr-min": "read from image", "ovrc": "OvrC"}
# Sources that show minutes only: a correct clock reads up to 59 s behind.
_MINUTE_SOURCES = {"ovrc", "ocr-min"}
_ZONE_NAMES = {
    "America/New_York": "Eastern",
    "America/Chicago": "Central",
    "America/Denver": "Mountain",
    "America/Los_Angeles": "Pacific",
}
_STAMP = "%b %d, %I:%M:%S %p %Z"


def format_drift(seconds: int) -> str:
    """Signed and compact: "+5s", "-2m 03s", "+1h 00m 02s"."""
    sign = "-" if seconds < 0 else "+"
    hours, rest = divmod(abs(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{sign}{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{sign}{minutes}m {secs:02d}s"
    return f"{sign}{secs}s"


@dataclass
class Capture:
    """One capture and the time check for the device it shows."""

    key: str
    shot: AuditScreenshot
    site_tz: str = ""
    # When the image was grabbed; older runs only have the row's save time.
    captured_at: datetime | None = None
    captured_estimated: bool = False
    device_time: str = ""  # naive ISO wall clock
    device_time_raw: str = ""
    device_time_source: str = ""

    @property
    def checkable(self) -> bool:
        return self.key.startswith(_CLOCK_PREFIXES)

    @property
    def drift(self) -> int | None:
        return device_drift.drift_seconds(self.device_time, self.captured_at, self.site_tz)

    @property
    def verdict(self) -> str:
        """"pass" / "fail" / "unread", or "" for captures that show no clock."""
        return device_drift.verdict(self.drift) if self.checkable else ""

    @property
    def drift_display(self) -> str:
        d = self.drift
        return "" if d is None else format_drift(d)

    @property
    def captured_eastern(self) -> str:
        return self.captured_at.astimezone(EASTERN).strftime(_STAMP) if self.captured_at else ""

    @property
    def expected_local(self) -> str:
        """What a correct clock at the site showed at the moment of capture."""
        if not self.captured_at:
            return ""
        return self.captured_at.astimezone(device_drift.zone(self.site_tz)).strftime(_STAMP)

    @property
    def device_display(self) -> str:
        try:
            wall = datetime.fromisoformat(self.device_time) if self.device_time else None
        except ValueError:
            wall = None
        if wall is None:
            return ""
        # Minute-only clocks: don't invent seconds.
        minutes_only = self.device_time_source in _MINUTE_SOURCES
        return wall.strftime("%b %d, %I:%M %p" if minutes_only else "%b %d, %I:%M:%S %p")

    @property
    def device_note(self) -> str:
        note = _SOURCE_LABELS.get(self.device_time_source, self.device_time_source)
        if self.device_time_source == "ovrc":
            _wall, claimed_zone = parse_ovrc_local(self.device_time_raw)
            if claimed_zone:
                note += f", says {claimed_zone}"
        return note


@dataclass
class ReviewRow:
    key: str
    job: AuditJob
    installation: object | None
    kind: str
    attempts: int
    captures: list[Capture] = field(default_factory=list)
    review: InstallationAuditReview | None = None
    site_tz: str = ""  # what this camera's device clocks are checked against

    @property
    def assigned_to(self) -> str:
        return self.review.assigned_to if self.review else ""

    @property
    def completed(self) -> bool:
        return bool(self.review and self.review.completed)

    @property
    def skipped(self) -> bool:
        return self.job.status == AuditJob.Status.SKIPPED

    @property
    def failed(self) -> bool:
        if self.skipped:
            return False
        return self.job.status != AuditJob.Status.SUCCEEDED or bool(
            (self.job.error_message or "").strip()
        )

    @property
    def site_tz_name(self) -> str:
        return _ZONE_NAMES.get(self.site_tz, self.site_tz)

    @property
    def lines(self) -> list[Capture | None]:
        """One table line per capture; a camera with none still gets one."""
        return list(self.captures) or [None]

    @property
    def devices_failing(self) -> int:
        return sum(c.verdict == "fail" for c in self.captures)

    @property
    def devices_unread(self) -> int:
        return sum(c.verdict == "unread" for c in self.captures)

    @property
    def drift_flagged(self) -> bool:
        return self.devices_failing > 0


def _capture_sort(c: Capture) -> tuple[int, str]:
    if c.key in _LAYER_ORDER:
        return (_LAYER_ORDER.index(c.key), c.key)
    if c.key.startswith("de_l"):
        return (len(_LAYER_ORDER), c.key)
    if c.key.startswith("ovrc_"):
        return (len(_LAYER_ORDER) + 1, c.key)
    return (len(_LAYER_ORDER) + 2, c.key)


def _capture(key: str, shot: AuditScreenshot, job: AuditJob, site_tz: str) -> Capture:
    capture = Capture(
        key,
        shot,
        site_tz=site_tz,
        captured_at=shot.captured_at or shot.created_at,
        captured_estimated=shot.captured_at is None,
        device_time=shot.device_time,
        device_time_raw=shot.device_time_raw,
        device_time_source=shot.device_time_source,
    )
    # Runs before per-capture times kept the OvrC reading on the job only.
    if (
        not capture.device_time
        and job.ovrc_fx
        and job.ovrc_local_time_raw
        and key == f"ovrc_{job.ovrc_fx.lower()}"
    ):
        wall, _zone = parse_ovrc_local(job.ovrc_local_time_raw)
        if wall is not None:
            capture.device_time = wall.isoformat()
            capture.device_time_raw = job.ovrc_local_time_raw
            capture.device_time_source = "ovrc"
            if job.ovrc_captured_at:
                capture.captured_at = job.ovrc_captured_at
                capture.captured_estimated = False
    return capture


def _site_tz(job: AuditJob, installation) -> str:
    """The zone stored at capture; older runs fall back to the agency/state list."""
    if job.site_tz:
        return job.site_tz
    if installation is None:
        return ""
    return site_timezone(installation, use_gps=False)[0]


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
        installation = installations.get(key)
        row = ReviewRow(
            key=key,
            job=job,
            installation=installation,
            kind=kind if kind in {"lti", "de"} else "",
            attempts=attempts[key],
            review=reviews.get(key),
            site_tz=_site_tz(job, installation),
        )
        for shot in job.screenshots.all():
            if shot.image:
                label = normalize_thumb_label(shot) or shot.label
                row.captures.append(_capture(label, shot, job, row.site_tz))
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
        "skipped": sum(r.skipped for r in rows),
        "drift": sum(r.drift_flagged for r in rows),
        "devices_failing": sum(r.devices_failing for r in rows),
        "devices_unread": sum(r.devices_unread for r in rows),
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
