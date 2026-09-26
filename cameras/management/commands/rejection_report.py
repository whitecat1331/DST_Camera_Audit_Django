"""Run a rejection CSV report headlessly from the command line."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Run a rejection CSV report: power on, capture, and emit an .xlsx/.csv."

    def add_arguments(self, parser):
        parser.add_argument("input", help="Path to the rejection .csv file")
        parser.add_argument(
            "--output",
            help="Output .xlsx path (default: media/rejection_report/<job_id>/report.xlsx)",
        )

    def handle(self, *args, **options):
        input_path = Path(options["input"]).expanduser()
        if not input_path.is_file():
            raise CommandError(f"Rejection CSV not found: {input_path}")

        from audits.models import AuditJob
        from audits.runner import (
            _rejection_report_xlsx_path,
            _run_rejection_report,
            write_rejection_selection,
        )
        from services.rejection_csv import parse_rejection_csv
        from services.rejection_match import match_rejection_rows

        rows = parse_rejection_csv(input_path.read_bytes())
        if not rows:
            raise CommandError("CSV contained no rows")
        report = match_rejection_rows(rows)

        serialized = [
            {
                "serial": item.row.serial,
                "count": item.row.count,
                "reason": item.row.reason,
                "source_row": item.row.source_row,
                "status": item.status,
                "installation_id": item.installation_id,
                "identifier": item.identifier,
                "pole": item.pole,
                "kind": item.kind,
                "match_by": item.match_by,
                "reason_note": item.reason,
            }
            for item in report.rows
        ]

        job = AuditJob.objects.create(
            pole_number="REJECTION",
            target_host=f"0/{len(serialized)}",
            device_type=AuditJob.DeviceType.REJECTION_REPORT,
            progress_message=f"Queued — {len(serialized)} row(s)",
        )
        write_rejection_selection(job.pk, serialized)

        output_dir = Path(settings.MEDIA_ROOT) / "audits" / str(job.pk)
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            errors, captured = _run_rejection_report(job.pk, output_dir)
            xlsx_path = _rejection_report_xlsx_path(job.pk)
            if not xlsx_path.exists():
                raise CommandError("; ".join(errors) or "Report was not produced")
            job.status = AuditJob.Status.SUCCEEDED
            if errors:
                job.error_message = "; ".join(errors)[:2000]
            job.progress_message = (
                f"Done with warnings ({len(errors)})" if errors else "Done"
            )
            job.finished_at = timezone.now()
            job.save(
                update_fields=[
                    "status",
                    "error_message",
                    "progress_message",
                    "finished_at",
                    "target_host",
                ]
            )
        except Exception as exc:  # noqa: BLE001
            job.status = AuditJob.Status.FAILED
            job.error_message = str(exc)[:2000]
            job.progress_message = f"Failed: {type(exc).__name__}"
            job.finished_at = timezone.now()
            job.save(
                update_fields=[
                    "status",
                    "error_message",
                    "progress_message",
                    "finished_at",
                ]
            )
            raise CommandError(str(exc)) from exc

        final_xlsx = Path(options["output"]) if options.get("output") else xlsx_path
        if final_xlsx != xlsx_path:
            final_xlsx.parent.mkdir(parents=True, exist_ok=True)
            final_xlsx.write_bytes(xlsx_path.read_bytes())

        self.stdout.write(self.style.SUCCESS(f"Captured {captured}/{len(serialized)} row(s)"))
        self.stdout.write(f"XLSX: {final_xlsx}")
