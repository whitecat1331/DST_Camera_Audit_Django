"""DST logging config — PatsScraper/PatsPrints-style levels + optional rotating file."""

from __future__ import annotations

import logging
import os
from pathlib import Path


def resolve_log_level(default: int = logging.INFO) -> int:
    raw = os.getenv("LOG_LEVEL", "").strip().lower()
    match raw:
        case "debug":
            return logging.DEBUG
        case "warn" | "warning":
            return logging.WARNING
        case "error":
            return logging.ERROR
        case "critical":
            return logging.CRITICAL
        case "info" | "":
            return default if raw == "" else logging.INFO
        case _:
            return default


def build_logging_config(base_dir: Path | None = None) -> dict:
    """Return a Django LOGGING dictConfig.

    Env:
      LOG_LEVEL — debug|info|warn|error (default info)
      LOG_FILE  — optional path (default logs/dst.log when unset uses project logs/)
                  set LOG_FILE= to empty and LOG_TO_FILE=false to disable file handler
    """
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent

    level = resolve_log_level()
    level_name = logging.getLevelName(level)

    log_to_file = os.getenv("LOG_TO_FILE", "true").lower() in ("1", "true", "yes")
    log_file_env = os.getenv("LOG_FILE", "").strip()
    if log_to_file:
        if log_file_env:
            log_path = Path(log_file_env)
            if not log_path.is_absolute():
                log_path = base_dir / log_path
        else:
            log_path = base_dir / "logs" / "dst.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        log_path = None

    formatters = {
        "standard": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    }

    handlers: dict = {
        "console": {
            "class": "logging.StreamHandler",
            "level": level_name,
            "formatter": "standard",
        },
    }
    root_handlers = ["console"]

    if log_path is not None:
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "level": level_name,
            "formatter": "standard",
            "filename": str(log_path),
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf-8",
        }
        root_handlers.append("file")

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": formatters,
        "handlers": handlers,
        "root": {
            "level": level_name,
            "handlers": root_handlers,
        },
        "loggers": {
            "dst": {
                "level": level_name,
                "handlers": root_handlers,
                "propagate": False,
            },
            "django.request": {
                "level": "WARNING",
                "handlers": root_handlers,
                "propagate": False,
            },
            "django.server": {
                "level": "INFO",
                "handlers": root_handlers,
                "propagate": False,
            },
            # Keep Selenium/http noise down unless LOG_LEVEL=debug
            "selenium": {"level": "WARNING", "propagate": True},
            "urllib3": {"level": "WARNING", "propagate": True},
            "httpx": {"level": "WARNING", "propagate": True},
            "httpcore": {"level": "WARNING", "propagate": True},
            "WDM": {"level": "WARNING", "propagate": True},
        },
    }
