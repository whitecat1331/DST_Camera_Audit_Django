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
) -> Path:
    """Try CBW passwords until capture succeeds.

    HTTP-probes auth first so a flaky Selenium menu wait does not burn past the
    correct password. TimeoutException retries the same password a few times.
    """
    progress = on_progress or _noop_progress
    if not username:
        raise RuntimeError("CBW_USERNAME is not configured")
    if not passwords:
        raise RuntimeError("CBW_PASSWORDS is not configured")

    logger.info("[CBW] trying passwords host=%s count=%s", host, len(passwords))
    progress(f"CBW probing auth @ {host}")
    ordered: list[str] = []
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
            ordered.append(password)
    if not ordered:
        logger.warning(
            "[CBW] no password passed HTTP auth probe host=%s — falling back to full list",
            host,
        )
        ordered = list(passwords)
    else:
        for password in passwords:
            if password not in ordered:
                ordered.append(password)

    url = f"http://{host}/setup.html"
    last_error: Exception | None = None
    for idx, password in enumerate(ordered, start=1):
        for attempt in range(1, _MENU_TIMEOUT_RETRIES + 2):
            progress(f"CBW password {idx}/{len(ordered)} try {attempt} @ {host}")
            logger.info(
                "[CBW] selenium attempt password=%s/%s try=%s host=%s",
                idx,
                len(ordered),
                attempt,
                host,
            )
            try:
                return screenshot_date_time(
                    url,
                    username,
                    password,
                    output_dir,
                    filename=filename,
                    on_progress=on_progress,
                )
            except TimeoutException as exc:
                last_error = exc
                logger.warning(
                    "[CBW] menu/timeout host=%s password=%s/%s try=%s: %s",
                    host,
                    idx,
                    len(ordered),
                    attempt,
                    type(exc).__name__,
                )
                progress(f"CBW timeout password {idx}/{len(ordered)} try {attempt}")
                if attempt <= _MENU_TIMEOUT_RETRIES:
                    continue
                break
            except Exception as exc:  # noqa: BLE001 — try next password
                last_error = exc
                logger.warning(
                    "[CBW] Auth/capture failed for %s password=%s/%s: %s: %s",
                    host,
                    idx,
                    len(ordered),
                    type(exc).__name__,
                    str(exc)[:200],
                )
                progress(f"CBW attempt {idx}/{len(ordered)} failed: {type(exc).__name__}")
                break

        for leftover in Path(output_dir).glob(filename):
            try:
                leftover.unlink()
            except OSError:
                pass

    raise RuntimeError(f"CBW capture failed for {host}: {last_error}")
