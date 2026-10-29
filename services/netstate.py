"""Which network the laptop is on, and releasing the user's camera VPN.

A DST audit needs two networks that can't be up at once: LTI cameras are only
reachable through the WatchGuard camera VPN, while TeamViewer (DE) and the
shared DB (publish) need it off. The user connects the VPN beforehand (MFA);
DST never connects it, it only releases it once, between the LTI and DE halves.

The WatchGuard client is OpenVPN underneath: the tray app (wgsslvpnc.exe, the
user's process) drives openvpn.exe (run by the wgsslvpnsrc service). On connect
the tray has the service add WFP "LAN filters" and restart the DNS Client; only
the tray's own disconnect/exit removes them. So the release asks the tray to
exit through its own ``Global\\wgsslvpnc_ExitEvent`` (it closes the tunnel and
cleans up, then stays open showing "Connection Closed"). Force-closing the tray
leaves those filters behind and the laptop has no network until a reboot
(issue I4 of plans/in-progress-plans/UNATTENDED_NIGHTLY_DST_AUDIT.md), so DST
never does that. ``signal SIGTERM`` on OpenVPN's management interface is only
used when the tray isn't running. Neither needs admin or MFA.
"""

from __future__ import annotations

import logging
import ctypes
import socket
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)

TRAY_EXE = "wgsslvpnc.exe"
OPENVPN_EXE = "openvpn.exe"
TRAY_EXIT_EVENT = r"Global\wgsslvpnc_ExitEvent"
EVENT_MODIFY_STATE = 0x0002
TUNNEL_GONE_SECONDS = 30.0
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


def internet_ok(timeout: float = 4.0) -> bool:
    """DNS resolves and a public HTTPS host answers (the Wi-Fi is really usable)."""
    host = getattr(settings, "DST_NET_CHECK_HOST", "www.google.com")
    try:
        socket.getaddrinfo(host, 443)
    except OSError:
        return False
    return tcp_ok(host, 443, timeout)


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


def signal_tray_exit() -> bool:
    """Ask the WatchGuard tray app to disconnect through its own exit event."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenEventW.restype = ctypes.c_void_p
    handle = k32.OpenEventW(EVENT_MODIFY_STATE, False, TRAY_EXIT_EVENT)
    if not handle:
        logger.warning("[NET] tray exit event not found (error %s)", ctypes.get_last_error())
        return False
    try:
        return bool(k32.SetEvent(ctypes.c_void_p(handle)))
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))


@dataclass
class ReleaseResult:
    released: bool
    detail: str
    network_ok: bool = True


def _wait(check: Callable[[], bool], seconds: float, poll: float = 2.0) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def release_vpn(
    *,
    wait_seconds: float = 60.0,
    progress: Callable[[str], None] = lambda msg: None,
) -> ReleaseResult:
    """Drop the user's WatchGuard VPN, then check the laptop still has internet.

    Never raises and never force-closes the tray app. The result says whether
    the VPN is gone, and network_ok whether DNS + internet work afterwards.
    """
    if not vpn_up():
        return ReleaseResult(True, "VPN was not connected")
    progress("Releasing the camera VPN…")
    if _pids(TRAY_EXE):
        if not signal_tray_exit():
            return ReleaseResult(False, "the WatchGuard app did not accept the disconnect request")
        how = "WatchGuard app disconnect"
    else:
        try:
            how = send_sigterm(management_port())
        except ReleaseError as exc:
            logger.warning("[NET] VPN release failed: %s", exc)
            return ReleaseResult(False, str(exc))
    logger.info("[NET] VPN release requested: %s", how)
    if not _wait(lambda: not vpn_up(), TUNNEL_GONE_SECONDS):
        return ReleaseResult(False, f"VPN still connected {TUNNEL_GONE_SECONDS:.0f} s after the disconnect request")
    progress("VPN released; checking the internet…")
    if not _wait(internet_ok, wait_seconds):
        logger.warning("[NET] no internet %ss after the VPN release", wait_seconds)
        return ReleaseResult(
            True,
            "VPN released, but the laptop has no internet (DNS/HTTPS fail)",
            network_ok=False,
        )
    if not direct_up(timeout=3):
        return ReleaseResult(True, "VPN released; internet OK; shared DB not answering yet")
    return ReleaseResult(True, "VPN released; internet OK")
