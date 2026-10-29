"""``runserver`` with ``--http`` / ``--https`` / ``--ims`` flags for IMS pairing.

Overrides Django's built-in runserver (same command name) so DST Camera Audit
can start the same way PatsScraper does:

    python manage.py runserver --http 8050 --ims http://127.0.0.1:8051
    python manage.py runserver --https 8050

``--ims`` points the app at IMS (``IMS_BASE_URL``) and derives the SSO callback
redirect URI from the chosen scheme/port. ``--https`` uses a self-signed dev
certificate generated on first use (never in production).
"""

from __future__ import annotations

import ssl

from django.conf import settings
from django.contrib.staticfiles.management.commands.runserver import (
    Command as BaseRunserverCommand,
)
from django.core.management.base import CommandError
from django.core.servers.basehttp import WSGIServer

from services.devcert import ensure_dev_cert


class SecureWSGIServer(WSGIServer):
    """WSGIServer that terminates TLS using a dev self-signed certificate."""

    certfile = ""
    keyfile = ""

    def __init__(self, *args, **kwargs):
        self._ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ssl_context.load_cert_chain(self.certfile, self.keyfile)
        super().__init__(*args, **kwargs)

    def get_request(self):
        sock, addr = super().get_request()
        try:
            return self._ssl_context.wrap_socket(sock, server_side=True), addr
        except Exception:
            sock.close()
            raise


class Command(BaseRunserverCommand):
    help = (
        "Starts a lightweight development server (adds --http/--https/--ims "
        "and serves static files)."
    )

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--http",
            metavar="PORT",
            help="Serve plain HTTP on PORT (mutually exclusive with --https).",
        )
        parser.add_argument(
            "--https",
            metavar="PORT",
            help="Serve HTTPS on PORT using a self-signed dev certificate.",
        )
        parser.add_argument(
            "--ims",
            metavar="URL",
            dest="ims_base_url",
            help=(
                "Point at IMS: sets IMS_BASE_URL and the SSO redirect URI. "
                "Use the HTTPS app URL (e.g. https://127.0.0.1:443) when IMS "
                "runs with --http/--https, NOT the HTTP redirect port."
            ),
        )

    def handle(self, *args, **options):
        http_port = options.get("http")
        https_port = options.get("https")
        if http_port and https_port:
            raise CommandError("--http and --https are mutually exclusive; choose one.")

        ims_url = (options.get("ims_base_url") or "").rstrip("/")

        scheme = "http"
        port = http_port
        if https_port:
            scheme = "https"
            port = https_port
            cert_path, key_path = ensure_dev_cert(settings.BASE_DIR / "certs")
            SecureWSGIServer.certfile = cert_path
            SecureWSGIServer.keyfile = key_path
            self.server_cls = SecureWSGIServer
            self.protocol = "https"

        if ims_url:
            settings.IMS_BASE_URL = ims_url

        if port:
            host = "127.0.0.1"
            options["addrport"] = f"{host}:{port}"
            if ims_url:
                settings.IMS_SSO_REDIRECT_URI = (
                    f"{scheme}://{host}:{port}/accounts/ims/callback/"
                )
        elif ims_url:
            # No explicit port: SSO redirect follows the runserver default.
            settings.IMS_SSO_REDIRECT_URI = (
                "http://127.0.0.1:8000/accounts/ims/callback/"
            )

        super().handle(*args, **options)
