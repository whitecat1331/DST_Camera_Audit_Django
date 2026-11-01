"""One CBW login shared by power-on, firmware reads and capture.

The X-410 locks out after two failed Basic-auth logins: for about 20–30 s it
answers 429 to every login, the right password included (E4, 2026-10-30:
233006 and 233043 need password 3, and the loops used to see 401, 401, 429 and
skip past it). So a 429 means "wait, then retry the same password", never
"wrong password". The password number that worked is remembered per host
(`cbw_password_hints.json`, the number only, per host and path) and tried first, which normally
avoids failed logins altogether.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import requests

from services.password_hints import HintStore, first_then_rest

logger = logging.getLogger(__name__)

# host -> number of the password that last worked (never the password).
HINTS = HintStore("cbw_password_hints.json", "CBW_PASSWORD_HINTS_PATH")

LOCKOUT_WAIT_SECONDS = 35.0
LOCKOUT_RETRIES = 2


class CbwAuthError(RuntimeError):
    """The CBW answered but no login got in. reason: auth_failed | locked_out."""

    def __init__(self, host: str, reason: str, detail: str) -> None:
        super().__init__(f"CBW {reason} @ {host}: {detail}")
        self.host = host
        self.reason = reason


class CbwUnreachable(RuntimeError):
    """No HTTP answer at all (connect timeout / refused)."""


@dataclass
class CbwLogin:
    host: str
    index: int  # 1-based number of the password that worked
    session: requests.Session  # authenticated; the caller closes it
    response: requests.Response  # the GET of `path` that succeeded


def _hint_key(host: str, path: str) -> str:
    # The setup page needs the admin password and keeps the original bare-host
    # key; other paths may accept more (state.json on some firmware), so they
    # mustn't overwrite it.
    return host if path == "/setup.html" else f"{host}{path}"


def read_hint(host: str, path: str = "/setup.html") -> int | None:
    value = HINTS.get(_hint_key(host, path))
    return value if isinstance(value, int) else None


def write_hint(host: str, index: int, path: str = "/setup.html") -> None:
    HINTS.set(_hint_key(host, path), index)


def cbw_login(
    host: str,
    username: str,
    passwords: list[str],
    *,
    path: str = "/state.json",
    timeout: float = 8,
    lockout_wait: float = LOCKOUT_WAIT_SECONDS,
    lockout_retries: int = LOCKOUT_RETRIES,
    sleep: Callable[[float], None] | None = None,
) -> CbwLogin:
    """Log in with the remembered password first; wait out a 429 on the same password.

    Raises CbwAuthError (every password got a real 401, or the lockout never
    cleared) or CbwUnreachable (no HTTP answer).
    """
    if not username:
        raise RuntimeError("CBW_USERNAME is not configured")
    if not passwords:
        raise RuntimeError("CBW_PASSWORDS is not configured")
    sleep = sleep or time.sleep
    hint = read_hint(host, path)
    url = f"http://{host}{path}"
    for idx in first_then_rest(len(passwords), hint):
        waits = 0
        while True:
            session = requests.Session()
            session.trust_env = False
            session.auth = (username, passwords[idx - 1])
            try:
                r = session.get(url, timeout=timeout)
            except requests.RequestException as exc:
                session.close()
                raise CbwUnreachable(f"CBW unreachable @ {host}: {type(exc).__name__}") from exc
            if r.status_code == 429:
                session.close()
                if waits >= lockout_retries:
                    raise CbwAuthError(
                        host, "locked_out", f"still 429 on password {idx} after {waits} waits of {lockout_wait:.0f}s"
                    )
                waits += 1
                logger.info("[CBW] locked out host=%s password=%s; waiting %.0fs (%s/%s)",
                            host, idx, lockout_wait, waits, lockout_retries)
                sleep(lockout_wait)
                continue
            if r.status_code == 401:
                session.close()
                logger.info("[CBW] login 401 host=%s password=%s/%s", host, idx, len(passwords))
                break
            if idx != hint:
                write_hint(host, idx, path)
            logger.info("[CBW] login ok host=%s password=%s/%s status=%s", host, idx, len(passwords), r.status_code)
            return CbwLogin(host=host, index=idx, session=session, response=r)
    raise CbwAuthError(host, "auth_failed", f"all {len(passwords)} passwords got 401")
