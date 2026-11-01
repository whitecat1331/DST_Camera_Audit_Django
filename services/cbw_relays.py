"""CBW relay power-on (from LTI-DateTime-Validator).

Never on a scheduled (solar) CBW: forced relays block its own start-up (E7).
DST power-on goes through services.cbw_logic.power_on_cbw, which picks this path
only for CBWs with no "on" schedule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from services.cbw_auth import cbw_login
from services.ip_map import DeviceType, pole_to_ip_address

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CbwRelayResult:
    action: str  # "turned_on" | "already_on"
    relays: list[str]


def _relay_is_on(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) == 1
    text = str(value).strip().lower()
    return text in {"1", "on", "true", "yes"}


def turn_all_relays_on(
    pole: str,
    username: str,
    passwords: list[str],
) -> CbwRelayResult:
    """Turn on all relays for a pole's CBW. Reports already_on when all were ON."""
    if not username:
        raise RuntimeError("CBW_USERNAME is not configured")
    if not passwords:
        raise RuntimeError("CBW_PASSWORDS is not configured")

    ip = pole_to_ip_address(str(pole).strip(), DeviceType.CBW)
    # Lockout-aware (a 429 waits and retries the same password); CbwAuthError
    # means the CBW answered but wouldn't log in, CbwUnreachable that it didn't answer.
    login = cbw_login(ip, username, passwords, timeout=5)
    session = login.session
    try:
        r = login.response
        r.raise_for_status()
        return relays_on(session, ip, r.json(), timeout=5, pole=pole)
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to turn on relays for pole {pole} ({ip}): {type(exc).__name__}") from exc
    finally:
        session.close()


def relays_on(session, ip: str, data: dict, *, timeout: float = 5, pole: str = "") -> CbwRelayResult:
    """Every relay in `data` (a state.json read) on, on an already logged-in session.

    Only for CBWs whose logic doesn't own power (services.cbw_logic decides).
    """
    relays = [key for key in data if str(key).lower().startswith("relay")]
    if not relays:
        raise RuntimeError(f"No relays found on CBW for pole {pole or ip} ({ip})")

    if all(_relay_is_on(data.get(relay)) for relay in relays):
        logger.info("[CBW] Relays already on for %s: %s", ip, relays)
        return CbwRelayResult(action="already_on", relays=relays)

    update_url = f"http://{ip}/state.json?" + "&".join(f"{relay}=1" for relay in relays)
    r = session.get(update_url, timeout=timeout)
    r.raise_for_status()
    logger.info("[CBW] Relays on for %s: %s", ip, relays)
    return CbwRelayResult(action="turned_on", relays=relays)
