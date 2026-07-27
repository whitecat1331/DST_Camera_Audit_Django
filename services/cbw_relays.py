"""CBW relay power-on (from LTI-DateTime-Validator)."""

from __future__ import annotations

import logging
import time

import requests

from services.ip_map import DeviceType, pole_to_ip_address

logger = logging.getLogger(__name__)


def turn_all_relays_on(
    pole: str,
    username: str,
    passwords: list[str],
) -> list[str]:
    """Turn on all relays for a pole's CBW. Returns relay names that were set."""
    if not username:
        raise RuntimeError("CBW_USERNAME is not configured")
    if not passwords:
        raise RuntimeError("CBW_PASSWORDS is not configured")

    ip = pole_to_ip_address(str(pole).strip(), DeviceType.CBW)
    base_url = f"http://{ip}"
    state_url = f"{base_url}/state.json"

    for password in passwords:
        session = requests.Session()
        session.trust_env = False
        session.auth = (username, password)
        try:
            r = session.get(state_url, timeout=5)
            if r.status_code == 401:
                time.sleep(0.2)
                continue
            if r.status_code == 429:
                time.sleep(5)
                continue
            r.raise_for_status()
            data = r.json()
            relays = [key for key in data if str(key).lower().startswith("relay")]
            if not relays:
                return []
            update_url = f"{base_url}/state.json?" + "&".join(f"{relay}=1" for relay in relays)
            r = session.get(update_url, timeout=5)
            r.raise_for_status()
            logger.info("[CBW] Relays on for %s: %s", ip, relays)
            return relays
        except requests.RequestException as exc:
            logger.warning("[CBW] Relay attempt failed for %s: %s", ip, type(exc).__name__)
            time.sleep(0.2)
        finally:
            session.close()

    raise RuntimeError(f"Failed to turn on relays for pole {pole} ({ip})")
