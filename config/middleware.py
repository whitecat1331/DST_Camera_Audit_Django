"""HTTP request logging middleware (IMS/PatsPrints-style [HTTP] lines)."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("dst.http")

_SKIP_PREFIXES = (
    "/static/",
    "/media/",
    "/favicon.ico",
)

# High-frequency poll endpoints — log at DEBUG only so capture progress stays readable.
_QUIET_SUFFIXES = ("/status/",)


class RequestLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path or ""
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return self.get_response(request)

        started = time.perf_counter()
        response = self.get_response(request)
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        user = "-"
        if getattr(request, "user", None) is not None and request.user.is_authenticated:
            user = request.user.get_username()

        remote = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        if not remote:
            remote = request.META.get("REMOTE_ADDR", "-")

        line = (
            "[HTTP] %s %s status=%s ms=%.1f user=%s remote=%s",
            request.method,
            path,
            getattr(response, "status_code", "-"),
            elapsed_ms,
            user,
            remote,
        )
        if any(path.endswith(suf) for suf in _QUIET_SUFFIXES):
            logger.debug(*line)
        else:
            logger.info(*line)
        return response
