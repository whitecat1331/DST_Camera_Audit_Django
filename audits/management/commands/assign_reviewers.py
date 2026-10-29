from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import F

from audits.assignment import assign_new_cameras, assign_unassigned
from audits.models import AuditJob, AuditRound


class Command(BaseCommand):
    help = (
        "Assign a round's cameras to reviewers (x % n by arrival order, per site type). "
        "For rounds published before assignment existed; safe to re-run - existing "
        "assignments are never changed."
    )

    def add_arguments(self, parser):
        parser.add_argument("--round", required=True, dest="round_name")
        parser.add_argument("--reviewers", help="Comma list; default DST_REVIEWERS")
        default = "default" if settings.DST_ROLE == "review" else "shared"
        parser.add_argument("--database", default=default)

    def handle(self, *args, **opts):
        db = opts["database"]
        reviewers = [u.strip() for u in (opts["reviewers"] or "").split(",") if u.strip()] or settings.DST_REVIEWERS
        try:
            rnd = AuditRound.objects.using(db).get(name=opts["round_name"])
        except AuditRound.DoesNotExist as exc:
            raise CommandError(f"No round named {opts['round_name']!r} on {db}") from exc
        sites = (
            AuditJob.objects.using(db)
            .filter(round=rnd, device_type=AuditJob.DeviceType.DST_SITE)
            .order_by(F("created_at").asc(), "pk")
        )
        created = assign_new_cameras(rnd, [(j.pole_number, j.target_host) for j in sites], reviewers=reviewers, using=db)
        filled = assign_unassigned(rnd, reviewers=reviewers, using=db)
        rnd.refresh_from_db(using=db)
        self.stdout.write(self.style.SUCCESS(
            f"{rnd.name}: {len(created)} camera(s) added, {filled} unassigned filled; reviewers {rnd.reviewers}"
        ))
        for r in rnd.reviews.using(db).order_by("kind", "assignment_seq"):
            self.stdout.write(f"  {r.kind or '-':<4} #{r.assignment_seq}  {r.camera_key:<16} -> {r.assigned_to or '(unassigned)'}")
