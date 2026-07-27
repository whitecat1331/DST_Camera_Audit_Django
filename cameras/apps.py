import logging

from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger("dst.init")


class CamerasConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "cameras"

    def ready(self) -> None:
        import os
        import sys

        # Django runserver spawns a parent + child; only log once in the child.
        if "runserver" in sys.argv and os.environ.get("RUN_MAIN") != "true":
            return
        logger.info(
            "[INIT] DST Camera Audit ready debug=%s ims=%s jobs=%s steps=%s log_level=%s",
            settings.DEBUG,
            bool(settings.IMS_BASE_URL and settings.IMS_API_TOKEN),
            settings.AUDIT_MAX_CONCURRENT,
            getattr(settings, "AUDIT_STEP_CONCURRENT", 3),
            getattr(settings, "LOG_LEVEL", "info"),
        )
