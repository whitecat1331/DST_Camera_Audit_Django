import logging

from django.core.management.base import BaseCommand, CommandError

from services.ims_client import IMSClientError
from services.ims_sync import sync_installations_from_ims

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Sync ASE installations from IMS into local SQLite."

    def handle(self, *args, **options):
        logger.info("[SYNC] manage.py sync_installations starting")
        try:
            created, updated, deactivated = sync_installations_from_ims()
        except IMSClientError as exc:
            logger.error("[SYNC] manage.py sync_installations failed: %s", type(exc).__name__)
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Sync complete: {created} created, {updated} updated, {deactivated} deactivated"
            )
        )
