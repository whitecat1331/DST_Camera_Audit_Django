"""Read CBW (ControlByWeb X-410) firmware version over plain HTTP.

Discovery (2026-10-06): firmware revision + part number are served by the
"General Information" JSON — ``GET /overview.json`` → ``x spc1_firmRev`` /
``x spc1_partNum``. This endpoint exists across every firmware generation
observed in the fleet (2.2 → 3.13.7.9). Newer firmware (v3.12+) also serves
``/firmware.json`` (the "Firmware" tab added for remote field updates); it is
kept as a fallback only.

``state.json`` exposes ``serialNumber`` (MAC) as a best-effort extra.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from services.cbw_auth import CbwAuthError, CbwUnreachable, cbw_login

logger = logging.getLogger(__name__)

# overview.json is the universal source; firmware.json only exists on v3.12+.
FIRMWARE_ENDPOINTS = ("/overview.json", "/firmware.json")


@dataclass(frozen=True)
class CbwFirmwareResult:
    host: str
    status: str  # ok | auth_failed | locked_out | unreachable | no_firmware | no_cbw
    firmware: str = ""
    part_number: str = ""
    serial: str = ""
    error: str = ""


def _extract(data) -> tuple[str, str]:
    if not isinstance(data, dict):
        return "", ""
    fw = (data.get("x spc1_firmRev") or [""])[0]
    part = (data.get("x spc1_partNum") or [""])[0]
    return fw, part


def read_cbw_firmware(
    host: str,
    username: str,
    passwords: list[str],
    timeout: int = 8,
) -> CbwFirmwareResult:
    """Read firmware revision + part number (+ serial) from a CBW X-410.

    Logs in through `cbw_login` (remembered password first, a 429 waited out on
    the same password). Never raises — failures are reported via `status`.
    """
    host = (host or "").strip()
    if not host:
        return CbwFirmwareResult(host=host, status="no_cbw")

    base = f"http://{host}"
    try:
        # Lockout-aware login on overview.json itself: some devices only give
        # it to the manager password.
        login = cbw_login(host, username, passwords, path=FIRMWARE_ENDPOINTS[0], timeout=timeout)
    except CbwAuthError as exc:
        return CbwFirmwareResult(host=host, status=exc.reason, error=str(exc))
    except CbwUnreachable as exc:
        return CbwFirmwareResult(host=host, status="unreachable", error=str(exc))
    except RuntimeError as exc:  # credentials not configured
        return CbwFirmwareResult(host=host, status="auth_failed", error=str(exc))

    session = login.session
    saw_response = False
    try:
        for path in FIRMWARE_ENDPOINTS:
            try:
                r = login.response if path == FIRMWARE_ENDPOINTS[0] else session.get(f"{base}{path}", timeout=timeout)
            except requests.RequestException as exc:
                return CbwFirmwareResult(host=host, status="unreachable", error=f"{type(exc).__name__}: {exc}")
            if r.status_code >= 400:
                continue  # not found / forbidden → try next endpoint

            saw_response = True
            try:
                data = r.json()
            except ValueError:
                continue  # non-JSON (e.g. firmware.json on pre-3.12 fw)

            fw, part = _extract(data)
            if not fw:
                continue  # no firmware field → try next endpoint

            serial = ""
            try:
                sr = session.get(f"{base}/state.json", timeout=timeout)
                if sr.status_code == 200:
                    serial = (sr.json() or {}).get("serialNumber", "") or ""
            except Exception:  # noqa: BLE001 — serial is best-effort only
                pass

            return CbwFirmwareResult(host=host, status="ok", firmware=fw, part_number=part, serial=serial)
    finally:
        session.close()

    if saw_response:
        return CbwFirmwareResult(
            host=host,
            status="no_firmware",
            error=f"no firmware field in {'/'.join(FIRMWARE_ENDPOINTS)}",
        )
    return CbwFirmwareResult(host=host, status="unreachable", error="no response")
