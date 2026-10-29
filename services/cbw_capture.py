"""CBW web UI screenshot capture via Selenium (LTI-style Date/Time path)."""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Callable
from pathlib import Path

import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from services.password_hints import HintStore, first_then_rest

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]

# Retry Selenium navigation with the same password before trying the next one.
_MENU_TIMEOUT_RETRIES = 2


def _noop_progress(_: str) -> None:
    return None


def _probe_cbw_auth(host: str, username: str, password: str) -> bool:
    """True when Basic Auth is accepted (state.json or setup.html)."""
    session = requests.Session()
    try:
        session.trust_env = False
    except Exception:  # noqa: BLE001
        pass
    session.auth = (username, password)
    for path in ("/state.json", "/setup.html"):
        try:
            r = session.get(f"http://{host}{path}", timeout=8)
            if r.status_code == 401:
                return False
            if r.status_code < 500:
                return True
        except requests.RequestException as exc:
            logger.debug("[CBW] auth probe %s%s failed: %s", host, path, type(exc).__name__)
    return False


def _find_date_time_elements(driver: webdriver.Chrome):
    links = driver.find_elements(By.LINK_TEXT, "Date/Time")
    if links:
        return links
    links = driver.find_elements(By.PARTIAL_LINK_TEXT, "Date/Time")
    if links:
        return links
    return driver.find_elements(
        By.XPATH,
        "//a[normalize-space(.)='Date/Time'] | //*[normalize-space(.)='Date/Time']",
    )


def _save_current_time(driver, filepath: Path) -> None:
    """Keep the page's "Current:" clock next to the image (services/device_clock.py reads it).

    Best effort: a page that doesn't show it leaves the device time unread.
    """
    from services.device_clock import write_clock_sidecar

    try:
        text = driver.find_element(By.ID, "mainContent").text
    except Exception:  # noqa: BLE001
        return
    _, found, rest = text.partition("Current")
    rest = rest.lstrip(": \n")
    write_clock_sidecar(filepath, rest.splitlines()[0] if found and rest else "")


def screenshot_date_time(
    url: str,
    username: str,
    password: str,
    output_dir: str | Path,
    filename: str = "cbw.png",
    on_progress: ProgressFn | None = None,
) -> Path:
    """Open CBW setup UI, open General Settings → Date/Time, save screenshot."""
    progress = on_progress or _noop_progress
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / filename

    chrome_options = Options()
    chrome_options.add_argument("--start-maximized")
    # Classic headless is more reliable for these older CBW UIs than --headless=new.
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--window-size=1280,900")

    # Never log Basic-auth credentials that may be present in the URL.
    safe_host = url
    if "@" in url:
        safe_host = url.split("@", 1)[-1]
    elif "://" in url:
        safe_host = url.split("://", 1)[-1]
    target = safe_host.split("/")[0]
    logger.info("[CBW] capture start target=%s", target)
    progress(f"CBW launching Chrome for {target}")

    t0 = time.perf_counter()
    driver = webdriver.Chrome(service=Service(), options=chrome_options)
    driver.set_page_load_timeout(45)
    driver.set_script_timeout(45)
    wait = WebDriverWait(driver, 40)
    logger.info("[CBW] Chrome ready in %.1fs target=%s", time.perf_counter() - t0, target)

    auth = base64.b64encode(f"{username}:{password}".encode()).decode().strip()
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd(
            "Network.setExtraHTTPHeaders",
            {"headers": {"Authorization": f"Basic {auth}"}},
        )
        progress(f"CBW loading setup page {target}")
        logger.info("[CBW] GET setup.html target=%s", target)
        driver.get(url)
        progress(f"CBW waiting for menu ({target})")
        wait.until(EC.presence_of_element_located((By.ID, "menu-content")))
        wait.until(EC.visibility_of_element_located((By.ID, "menu-content")))
        logger.info("[CBW] menu visible after %.1fs target=%s", time.perf_counter() - t0, target)
        time.sleep(2)
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#menu-content a")))
        time.sleep(0.5)

        menu_items = driver.find_elements(By.CSS_SELECTOR, "#menu-content a")
        labels = [m.text.strip() for m in menu_items if m.text.strip()]
        logger.info(
            "[CBW] menu items=%s labels=%s target=%s",
            len(menu_items),
            labels[:20],
            target,
        )
        for item in menu_items:
            if item.text.strip() == "General Settings":
                progress("CBW opening General Settings")
                driver.execute_script("arguments[0].click();", item)
                wait.until(EC.visibility_of_element_located((By.ID, "mainContent")))
                time.sleep(1.5)
                break
        else:
            # Fallback: older CBW menus expose Date/Time at top level
            for item in menu_items:
                if item.text.strip() == "Date/Time":
                    progress("CBW opening Date/Time (top-level)")
                    driver.execute_script("arguments[0].click();", item)
                    wait.until(EC.visibility_of_element_located((By.ID, "mainContent")))
                    time.sleep(2)
                    driver.save_screenshot(str(filepath))
                    _save_current_time(driver, filepath)
                    logger.info(
                        "[CBW] Saved screenshot (top-level Date/Time) path=%s elapsed=%.1fs",
                        filepath,
                        time.perf_counter() - t0,
                    )
                    progress("CBW screenshot saved")
                    return filepath
            raise RuntimeError("General Settings tab not found")

        date_time_links = _find_date_time_elements(driver)
        if not date_time_links:
            nested = [
                el.text.strip()
                for el in driver.find_elements(
                    By.CSS_SELECTOR, "#mainContent a, #mainContent li, #mainContent span"
                )
                if el.text and el.text.strip()
            ]
            logger.warning(
                "[CBW] Date/Time not found under General Settings; mainContent texts=%s",
                nested[:40],
            )
            raise RuntimeError("Date/Time link not found under General Settings")

        progress("CBW opening Date/Time")
        driver.execute_script("arguments[0].click();", date_time_links[0])
        wait.until(EC.visibility_of_element_located((By.ID, "mainContent")))
        time.sleep(2)
        driver.save_screenshot(str(filepath))
        _save_current_time(driver, filepath)
        logger.info(
            "[CBW] Saved screenshot path=%s elapsed=%.1fs",
            filepath,
            time.perf_counter() - t0,
        )
        progress("CBW screenshot saved")
        return filepath
    finally:
        try:
            driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": {}})
        except Exception:  # noqa: BLE001
            pass
        driver.quit()
        logger.debug("[CBW] Chrome closed target=%s", target)


def capture_cbw_with_passwords(
    host: str,
    username: str,
    passwords: list[str],
    output_dir: str | Path,
    filename: str = "cbw.png",
    on_progress: ProgressFn | None = None,
    budget_seconds: float | None = None,
) -> Path:
    """Try CBW passwords until capture succeeds.

    Order: the password number that last worked on this host (a hint file holds
    the number only, never the password), then those that pass the HTTP auth
    probe, then the rest. Each password gets one try per round before any is
    retried: on some firmware a wrong password isn't a 401, it shows up as a menu
    timeout (220054: passwords 1 and 2 timed out 3× each; 3 worked in 18 s).
    budget_seconds stops starting new attempts once spent (default
    CBW_CAPTURE_BUDGET_SECONDS).
    """
    progress = on_progress or _noop_progress
    if not username:
        raise RuntimeError("CBW_USERNAME is not configured")
    if not passwords:
        raise RuntimeError("CBW_PASSWORDS is not configured")
    if budget_seconds is None:
        budget_seconds = _setting("CBW_CAPTURE_BUDGET_SECONDS", 150)
    deadline = time.monotonic() + float(budget_seconds) if budget_seconds else None

    logger.info("[CBW] trying passwords host=%s count=%s", host, len(passwords))
    progress(f"CBW probing auth @ {host}")
    hint = _read_password_hint(host)
    probed: list[int] = []
    for idx, password in enumerate(passwords, start=1):
        ok = _probe_cbw_auth(host, username, password)
        logger.info(
            "[CBW] auth probe attempt=%s/%s host=%s ok=%s",
            idx,
            len(passwords),
            host,
            ok,
        )
        if ok:
            probed.append(idx)
    if not probed:
        logger.warning(
            "[CBW] no password passed HTTP auth probe host=%s — falling back to full list",
            host,
        )
    order = first_then_rest(len(passwords), hint, *probed)

    url = f"http://{host}/setup.html"
    last_error: Exception | None = None
    for attempt in range(1, _MENU_TIMEOUT_RETRIES + 2):
        for idx in list(order):
            if deadline is not None and time.monotonic() > deadline:
                raise RuntimeError(
                    f"CBW capture failed for {host}: gave up after {budget_seconds:.0f}s ({last_error})"
                )
            progress(f"CBW password {idx}/{len(passwords)} try {attempt} @ {host}")
            logger.info(
                "[CBW] selenium attempt password=%s/%s try=%s host=%s",
                idx,
                len(passwords),
                attempt,
                host,
            )
            try:
                path = screenshot_date_time(
                    url,
                    username,
                    passwords[idx - 1],
                    output_dir,
                    filename=filename,
                    on_progress=on_progress,
                )
                if idx != hint:
                    _write_password_hint(host, idx)
                return path
            except TimeoutException as exc:
                last_error = exc
                logger.warning(
                    "[CBW] menu/timeout host=%s password=%s/%s try=%s: %s",
                    host,
                    idx,
                    len(passwords),
                    attempt,
                    type(exc).__name__,
                )
                progress(f"CBW timeout password {idx}/{len(passwords)} try {attempt}")
            except Exception as exc:  # noqa: BLE001 — not a timeout: this password is out
                last_error = exc
                order.remove(idx)
                logger.warning(
                    "[CBW] Auth/capture failed for %s password=%s/%s: %s: %s",
                    host,
                    idx,
                    len(passwords),
                    type(exc).__name__,
                    str(exc)[:200],
                )
                progress(f"CBW attempt {idx}/{len(passwords)} failed: {type(exc).__name__}")
            for leftover in Path(output_dir).glob(filename):
                try:
                    leftover.unlink()
                except OSError:
                    pass
        if not order:
            break

    raise RuntimeError(f"CBW capture failed for {host}: {last_error}")


def _setting(name: str, default):
    try:
        from django.conf import settings

        return getattr(settings, name, default)
    except Exception:  # noqa: BLE001 — usable outside Django too
        return default


# host -> number of the password that last worked (never the password).
_HINTS = HintStore("cbw_password_hints.json", "CBW_PASSWORD_HINTS_PATH")


def _read_password_hint(host: str) -> int | None:
    value = _HINTS.get(host)
    return value if isinstance(value, int) else None


def _write_password_hint(host: str, index: int) -> None:
    _HINTS.set(host, index)

