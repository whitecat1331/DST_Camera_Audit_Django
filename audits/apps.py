import logging
import os
import sys
import threading

from django.apps import AppConfig

logger = logging.getLogger("dst.init")


class AuditsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "audits"

    def ready(self) -> None:
        # Django runserver spawns a parent + child; only reap once in the child.
        if "runserver" in sys.argv and os.environ.get("RUN_MAIN") != "true":
            return

        def _reap() -> None:
            try:
                from audits.runner import reap_orphaned_audit_jobs

                n = reap_orphaned_audit_jobs()
                if n:
                    logger.info("[INIT] cleared %s orphaned audit job(s)", n)
            except Exception:  # noqa: BLE001 — migrations / first boot may lack tables
                logger.exception("[INIT] orphaned audit job reap skipped")

        # Defer past AppConfig.ready() so Django does not warn about DB access.
        threading.Timer(0.5, _reap).start()
