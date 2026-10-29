"""
WSGI config for config project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = get_wsgi_application()

# Ops diagnostic (opt-in): `kill -s $((SIGRTMIN+1)) <worker pid>` dumps every
# thread's stack to stderr, to see what a hung request is waiting on.
# A real-time signal, because gunicorn workers already use USR1/USR2.
if os.environ.get("DST_DUMP_STACKS") == "1":
    import faulthandler
    import signal
    import sys
    import threading
    import time

    if hasattr(faulthandler, "register"):  # POSIX only (not Windows dev boxes)
        faulthandler.register(signal.SIGRTMIN + 1, all_threads=True)

    # Watchdog: if any request runs longer than DST_STUCK_SECONDS, dump every
    # thread's stack once, so a hang seen in a real browser leaves evidence.
    _STUCK = float(os.environ.get("DST_STUCK_SECONDS", "20"))
    _inflight: dict[int, tuple[float, str]] = {}
    _dumped: set[tuple[int, float]] = set()
    _django_app = application

    def application(environ, start_response):  # noqa: F811 — wraps Django's app
        key = threading.get_ident()
        _inflight[key] = (time.monotonic(), f"{environ.get('REQUEST_METHOD')} {environ.get('PATH_INFO')}?{environ.get('QUERY_STRING', '')}")
        try:
            return _django_app(environ, start_response)
        finally:
            _inflight.pop(key, None)

    def _watch() -> None:
        while True:
            time.sleep(5)
            now = time.monotonic()
            for tid, (started, what) in list(_inflight.items()):
                if now - started > _STUCK and (tid, started) not in _dumped:
                    _dumped.add((tid, started))
                    print(
                        f"[STUCK] pid={os.getpid()} thread={tid} {what} running {now - started:.0f}s — all stacks:",
                        file=sys.stderr,
                        flush=True,
                    )
                    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)

    threading.Thread(target=_watch, name="stuck-request-watchdog", daemon=True).start()
