import logging

from django.core.management.base import BaseCommand, CommandError

from audits.models import AuditJob
from audits.publish import PublishError, publish_runs

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Publish finished DST audit runs (parent + site jobs + screenshots + images) "
        "to the shared DB under an audit round. Turn the camera VPN off first. "
        "Safe to re-run: rows upsert by public_id and unchanged images are skipped."
    )

    def add_arguments(self, parser):
        parser.add_argument("--round", required=True, dest="round_name")
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--runs", nargs="+", type=int, help="DST_AUDIT parent job ids")
        group.add_argument(
            "--latest", type=int, help="The N most recent finished DST_AUDIT runs"
        )

    def handle(self, *args, **options):
        run_ids = options["runs"]
        if run_ids is None:
            run_ids = list(
                AuditJob.objects.filter(
                    device_type=AuditJob.DeviceType.DST_AUDIT,
                    status__in=[
                        AuditJob.Status.SUCCEEDED,
                        AuditJob.Status.FAILED,
                        AuditJob.Status.CANCELLED,
                    ],
                )
                .order_by("-created_at")
                .values_list("pk", flat=True)[: options["latest"]]
            )
        try:
            report = publish_runs(
                run_ids, options["round_name"], progress=lambda m: self.stdout.write(m)
            )
        except PublishError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(report.summary()))
        for name in report.missing_files:
            self.stdout.write(self.style.WARNING(f"missing locally: {name}"))
