from django.core.management.base import BaseCommand, CommandError

from audits.models import ScheduledAudit
from audits.scheduling import run_schedule


class Command(BaseCommand):
    help = (
        "Run one scheduled DST audit in this process (Windows Task Scheduler calls "
        "this at the set time). A schedule runs at most once."
    )

    def add_arguments(self, parser):
        parser.add_argument("schedule_id", type=int)

    def handle(self, *args, schedule_id, **options):
        if not ScheduledAudit.objects.filter(pk=schedule_id).exists():
            raise CommandError(f"No scheduled audit #{schedule_id}")
        sched = run_schedule(schedule_id)
        self.stdout.write(f"Scheduled audit #{sched.pk}: {sched.status} — {sched.message}")
