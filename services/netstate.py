"""Which network the laptop is on, and releasing the user's camera VPN.

A DST audit needs two networks that can't be up at once: LTI cameras are only
reachable through the WatchGuard camera VPN, while TeamViewer (DE) and the
shared DB (publish) need it off. The user connects the VPN beforehand (MFA);
DST never connects it, it only releases it once, between the LTI and DE halves.

The WatchGuard client is OpenVPN underneath: the tray app (wgsslvpnc.exe, the
user's process) drives openvpn.exe (run by the wgsslvpnsrc service) through
OpenVPN's management interface on 127.0.0.1. The interface takes one client
at a time, so the tray app is closed first; then ``signal SIGTERM`` ends the
tunnel. Neither step needs admin or MFA (Phase 0 of
plans/in-progress-plans/UNATTENDED_NIGHTLY_DST_AUDIT.md).
"""

from __future__ import annotations

import logging
import socket
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)

TRAY_EXE = "wgsslvpnc.exe"
OPENVPN_EXE = "openvpn.exe"
CREATE_NO_WINDOW = 0x08000000


class ReleaseError(Exception):
    pass


def tcp_ok(host: str, port: int, timeout: float = 4.0) -> bool:
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def any_reachable(targets: Iterable[tuple[str, int]], timeout: float = 4.0) -> bool:
    """True when any (host, port) answers; probes run in parallel."""
    from concurrent.futures import ThreadPoolExecutor

    targets = [t for t in targets if t[0]]
    if not targets:
        return False
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        return any(pool.map(lambda t: tcp_ok(t[0], t[1], timeout), targets))


def camera_network_up(cbw_hosts: Iterable[str], timeout: float = 4.0) -> bool:
    """The LTI camera network answers (a CBW web port on any of these hosts)."""
    return any_reachable([(h, 80) for h in cbw_hosts], timeout)


def shared_db_target() -> tuple[str, int]:
    db = settings.DATABASES.get("shared") or {}
    return db.get("HOST", ""), int(db.get("PORT") or 5432)


def direct_up(timeout: float = 4.0) -> bool:
    """The shared DB answers, i.e. the camera VPN is not in the way."""
    return tcp_ok(*shared_db_target(), timeout=timeout)


def _run(args: list[str]) -> str:
    try:
        out = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("[NET] %s failed: %s", args[0], exc)
        return ""
    return out.stdout or ""


def _pids(image: str) -> list[int]:
    out = _run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"])
    pids = []
    for line in out.splitlines():
        cols = [c.strip('"') for c in line.split('","')]
        if len(cols) > 1 and cols[0].lower() == image.lower():
            try:
                pids.append(int(cols[1]))
            except ValueError:
                pass
    return pids


def vpn_up() -> bool:
    """The WatchGuard tunnel process is running."""
    return bool(_pids(OPENVPN_EXE))


def parse_listen_ports(netstat: str, pids: Iterable[int]) -> list[int]:
    """Loopback TCP ports LISTENING by any of pids, from `netstat -ano -p TCP`."""
    want = {str(p) for p in pids}
    ports = []
    for line in netstat.splitlines():
        cols = line.split()
        if len(cols) == 5 and cols[0] == "TCP" and cols[3] == "LISTENING" and cols[4] in want:
            host, _, port = cols[1].rpartition(":")
            if host == "127.0.0.1" and port.isdigit():
                ports.append(int(port))
    return ports


def management_port() -> int:
    """openvpn.exe's management port; the configured port when it can't be read."""
    ports = parse_listen_ports(_run(["netstat", "-ano", "-p", "TCP"]), _pids(OPENVPN_EXE))
    return ports[0] if ports else int(getattr(settings, "DST_VPN_MGMT_PORT", 1337))


def send_sigterm(port: int, *, connect_seconds: float = 15.0) -> str:
    """Send `signal SIGTERM` over the management interface; return its reply line."""
    deadline = time.monotonic() + connect_seconds
    while True:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            break
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise ReleaseError(f"management port {port} not reachable: {exc}") from exc
            time.sleep(1)
    with sock:
        sock.settimeout(10)
        buf = b""
        try:
            # The banner only arrives once no other client holds the interface.
            while b">INFO:" not in buf:
                if b"ENTER PASSWORD" in buf:
                    raise ReleaseError("management interface asks for a password")
                chunk = sock.recv(1024)
                if not chunk:
                    raise ReleaseError("management interface closed before its banner")
                buf += chunk
            sock.sendall(b"signal SIGTERM\r\n")
            reply = b""
            while b"\n" not in reply:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                reply += chunk
        except socket.timeout as exc:
            raise ReleaseError("management interface timed out") from exc
    line = next(
        (ln for ln in reply.decode(errors="replace").splitlines() if ln.startswith(("SUCCESS", "ERROR"))),
        reply.decode(errors="replace").strip(),
    )
    if not line.startswith("SUCCESS"):
        raise ReleaseError(f"SIGTERM refused: {line[:200]}")
    return line


@dataclass
class ReleaseResult:
    released: bool
    detail: str


def release_vpn(
    *,
    wait_seconds: float = 60.0,
    progress: Callable[[str], None] = lambda msg: None,
) -> ReleaseResult:
    """Drop the user's WatchGuard VPN, then wait until the shared DB answers.

    Never raises; the result says whether the VPN is gone and why not.
    """
    if not vpn_up():
        return ReleaseResult(True, "VPN was not connected")
    progress("Releasing the camera VPN…")
    _run(["taskkill", "/F", "/IM", TRAY_EXE])  # frees the one management client slot
    port = management_port()
    try:
        reply = send_sigterm(port)
    except ReleaseError as exc:
        logger.warning("[NET] VPN release failed: %s", exc)
        return ReleaseResult(False, str(exc))
    logger.info("[NET] VPN release on port %s: %s", port, reply)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not vpn_up() and direct_up(timeout=3):
            return ReleaseResult(True, "VPN released")
        time.sleep(2)
    if vpn_up():
        return ReleaseResult(False, "VPN still connected after SIGTERM")
    return ReleaseResult(True, "VPN released; shared DB not answering yet")
