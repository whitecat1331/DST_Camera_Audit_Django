"""Split a round's cameras among reviewers: x % n by arrival order, per site type.

Each camera is assigned once, when it first enters the round, and the result
is stored (InstallationAuditReview), so publishing more runs later never moves
a camera between techs. The k-th camera of a site type goes to
``reviewers[(start + k) % n]``; ``start`` for a type is the round's total
assigned count when that type first appears, so both each type and the overall
total stay within +-1 per tech.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Max

from audits.models import AuditRound, InstallationAuditReview


def reviewer_for(reviewers: list[str], start: int, seq: int) -> str:
    return reviewers[(start + seq) % len(reviewers)] if reviewers else ""


def assign_new_cameras(
    round_: AuditRound,
    cameras: list[tuple[str, str]],
    *,
    reviewers: list[str],
    using: str = "default",
) -> list[InstallationAuditReview]:
    """Create review rows for cameras new to the round, in the given arrival order.

    ``cameras`` is [(camera_key, kind)]. Existing rows are never touched. The
    round's reviewer list is snapshotted on first use; an empty list leaves new
    cameras unassigned (a later ``assign_reviewers`` can fill them).
    """
    created: list[InstallationAuditReview] = []
    with transaction.atomic(using=using):
        # One writer at a time per round, so two publishes can't hand out the
        # same sequence number.
        round_ = AuditRound.objects.using(using).select_for_update().get(pk=round_.pk)
        if not round_.reviewers and reviewers:
            round_.reviewers = list(reviewers)
        team = list(round_.reviewers)
        rows = InstallationAuditReview.objects.using(using).filter(round=round_)
        existing = set(rows.values_list("camera_key", flat=True))
        total = rows.count()
        next_seq = {
            r["kind"]: (r["m"] + 1 if r["m"] is not None else 0)
            for r in rows.values("kind").annotate(m=Max("assignment_seq"))
        }
        offsets = dict(round_.assignment_offsets or {})
        seen: set[str] = set()
        for key, kind in cameras:
            key, kind = (key or "").strip(), (kind or "").strip().lower()
            if not key or key in existing or key in seen:
                continue
            seen.add(key)
            seq = next_seq.get(kind, 0) if team else None
            if team and kind not in offsets:
                offsets[kind] = total % len(team)
            row = InstallationAuditReview(
                round=round_,
                camera_key=key,
                kind=kind,
                assignment_seq=seq,
                assigned_to=reviewer_for(team, offsets.get(kind, 0), seq) if team else "",
            )
            row.save(using=using)
            created.append(row)
            total += 1
            if team:
                next_seq[kind] = seq + 1
        round_.assignment_offsets = offsets
        round_.save(using=using, update_fields=["reviewers", "assignment_offsets"])
    return created


def assign_unassigned(round_: AuditRound, *, reviewers: list[str], using: str = "default") -> int:
    """Backfill: give unassigned rows (e.g. published before a team was set) a reviewer."""
    with transaction.atomic(using=using):
        round_ = AuditRound.objects.using(using).select_for_update().get(pk=round_.pk)
        if not round_.reviewers and reviewers:
            round_.reviewers = list(reviewers)
        team = list(round_.reviewers)
        if not team:
            return 0
        rows = InstallationAuditReview.objects.using(using).filter(round=round_)
        assigned = rows.exclude(assigned_to="")
        total = assigned.count()
        next_seq = {
            r["kind"]: (r["m"] + 1 if r["m"] is not None else 0)
            for r in assigned.values("kind").annotate(m=Max("assignment_seq"))
        }
        offsets = dict(round_.assignment_offsets or {})
        n = 0
        for row in rows.filter(assigned_to="").order_by("created_at", "pk"):
            kind = row.kind
            if kind not in offsets:
                offsets[kind] = total % len(team)
            seq = next_seq.get(kind, 0)
            row.assignment_seq, row.assigned_to = seq, reviewer_for(team, offsets[kind], seq)
            row.save(using=using, update_fields=["assignment_seq", "assigned_to"])
            next_seq[kind], total, n = seq + 1, total + 1, n + 1
        round_.assignment_offsets = offsets
        round_.save(using=using, update_fields=["reviewers", "assignment_offsets"])
    return n
