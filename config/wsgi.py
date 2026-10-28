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

    faulthandler.register(signal.SIGRTMIN + 1, all_threads=True)
