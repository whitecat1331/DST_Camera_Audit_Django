"""CBW web UI screenshot capture via Selenium (LTI-style Date/Time path)."""

from __future__ import annotations

import base64
import json
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

from services.cbw_auth import CbwUnreachable, cbw_login, read_hint, write_hint
from services.password_hints import first_then_rest

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]

# Retry Selenium navigation with the same password before trying the next one.
_MENU_TIMEOUT_RETRIES = 2
# Clicks on Date/Time before giving up and saving whatever page is shown.
_DATE_TIME_CLICKS = 3
_DATE_TIME_WAIT_SECONDS = 12
# How long to keep re-clicking while the menu's scripts aren't handling clicks yet.
_MENU_READY_SECONDS = 20


def _noop_progress(_: str) -> None:
    return None


def _probe_cbw_auth(host: str, username: str, passwords: list[str]) -> int | None:
    """Number of the password the CBW accepts on setup.html, or None if HTTP can't tell.

    One lockout-aware login (`cbw_login`): the remembered password first, and a
    429 is waited out on the same password, never counted as accepted (it used
    to be, and the loop's own 401s caused it). Raises CbwAuthError when the CBW
    answers but rejects every password: D3 skips the capture then.
    """
    try:
        login = cbw_login(host, username, passwords, path="/setup.html")
    except CbwUnreachable as exc:
        logger.debug("[CBW] auth probe %s: %s", host, exc)
        return None
    login.session.close()
    return login.index


def _find_date_time_elements(driver: webdriver.Chrome):
    """The Date/Time menu link; the <a> itself, shown or not.

    Clicking General Settings toggles its submenu, so Date/Time is sometimes
    hidden. Link-text lookups only see shown links, and the old text XPath
    then returned the hidden <li class="adminOnly"> before its <a>: a click on
    the <li> does nothing (233201, 2026-10-29: 3 of 5 captures). A JS click on
    the hidden <a> loads the page in under a second. Only the menu's own link:
    clicked anywhere the menu doesn't handle it, it is a plain link to the bare
    date-time.html template (233127, 2026-10-29: "Current: Init...").
    """
    links = driver.find_elements(By.CSS_SELECTOR, "#menu-content a[href*='date-time']")
    if links:
        return links
    links = driver.find_elements(By.LINK_TEXT, "Date/Time")
    if links:
        return links
    links = driver.find_elements(By.PARTIAL_LINK_TEXT, "Date/Time")
    if links:
        return links
    links = driver.find_elements(By.XPATH, "//a[normalize-space(.)='Date/Time']")
    if links:
        return links
    return driver.find_elements(By.XPATH, "//*[normalize-space(.)='Date/Time']")


def _save_current_time(driver, filepath: Path) -> None:
    """Keep the page's "Current:" clock next to the image (services/device_clock.py reads it).

    Best effort: a page that doesn't show it leaves the device time unread.
    """
    from services.device_clock import write_clock_sidecar

    write_clock_sidecar(filepath, _current_clock_text(driver))


def _current_clock_text(driver) -> str:
    """The line under "Current:" in #mainContent ("" when not shown)."""
    try:
        text = driver.find_element(By.ID, "mainContent").text
    except Exception:  # noqa: BLE001
        return ""
    _, found, rest = text.partition("Current")
    rest = rest.lstrip(": \n")
    return rest.splitlines()[0] if found and rest else ""


def _wait_for_menu_settled(driver, target: str, timeout: float = 20, quiet: float = 2) -> int:
    """Wait until the menu's link count stops changing; returns the count.

    A slow CBW builds its menu in steps after #menu-content is visible. On
    2026-10-29 (233201) we read it at 21 of 27 links ("Logging", not yet
    "Logging & Cloud"), and the Date/Time link we clicked was replaced under
    us: the click did nothing and we saved General Information twice.
    """
    deadline = time.monotonic() + timeout
    count = len(driver.find_elements(By.CSS_SELECTOR, "#menu-content a"))
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.5)
        now = len(driver.find_elements(By.CSS_SELECTOR, "#menu-content a"))
        if now != count:
            count, stable_since = now, time.monotonic()
        elif time.monotonic() - stable_since >= quiet:
            return count
    logger.warning("[CBW] menu still changing after %.0fs links=%s target=%s", timeout, count, target)
    return count


# Click a link the way the menu does, but never let the browser follow it: a
# window listener runs after the page's own handlers, and when none of them
# took the click (preventDefault) it stops the navigation and says so.
_CLICK_IN_PAGE_JS = """
const link = arguments[0];
let followed = false;
const guard = (e) => { if (!e.defaultPrevented) { e.preventDefault(); followed = true; } };
window.addEventListener("click", guard);
try { link.click(); } finally { window.removeEventListener("click", guard); }
return followed;
"""


def _open_date_time(driver, find_links, target: str) -> bool:
    """Click Date/Time until its "Current:" clock shows; re-find the link each time.

    The link can be replaced by a late menu rebuild, and a click on the old
    element is silently ignored, so a page that doesn't come gets a fresh
    element and another click. A click the menu doesn't handle yet would
    open the bare date-time.html template; it is blocked and retried instead.
    """
    wait = WebDriverWait(driver, _DATE_TIME_WAIT_SECONDS)
    ready_by = time.monotonic() + _MENU_READY_SECONDS
    attempt = 0
    while attempt < _DATE_TIME_CLICKS:
        links = find_links()
        if not links:
            return False
        if driver.execute_script(_CLICK_IN_PAGE_JS, links[0]):
            # Not a real try: the menu isn't listening yet (233127 forced early:
            # handled on the third click, ~5 s after the link appeared).
            if time.monotonic() > ready_by:
                logger.warning("[CBW] menu never handled the Date/Time click in %ss target=%s", _MENU_READY_SECONDS, target)
                return False
            logger.info("[CBW] menu not handling clicks yet; retrying target=%s", target)
            time.sleep(1)
            continue
        attempt += 1
        if "setup.html" not in driver.current_url:
            # A handler let the browser leave the menu page anyway: go back.
            logger.warning("[CBW] Date/Time click left setup.html url=%s target=%s", driver.current_url, target)
            driver.back()
            _wait_for_menu_settled(driver, target)
            continue
        if _wait_for_date_time_page(wait, target, warn=attempt == _DATE_TIME_CLICKS):
            if attempt > 1:
                logger.info("[CBW] Date/Time loaded on click %s target=%s", attempt, target)
            return True
    return False


# What Chrome saw when Date/Time didn't load (no credentials: URLs only).
DATE_TIME_DEBUG_DIR = Path(__file__).resolve().parents[1] / "logs" / "cbw_debug"


def network_problems(perf_log: list[dict]) -> dict:
    """Requests in Chrome's performance log that failed, errored, or never finished.

    233201 (2026-10-29) loaded a short menu slowly and then never showed the
    Date/Time page, twice; the log alone couldn't say which request stalled.
    """
    reqs: dict[str, dict] = {}
    for entry in perf_log:
        try:
            msg = json.loads(entry["message"])["message"]
        except (KeyError, TypeError, ValueError):
            continue
        method, params = msg.get("method", ""), msg.get("params", {})
        rid = params.get("requestId")
        if not rid:
            continue
        if method == "Network.requestWillBeSent":
            reqs[rid] = {"url": params["request"]["url"].split("?")[0], "t": params.get("timestamp"), "state": "pending"}
        elif rid in reqs:
            r = reqs[rid]
            if method == "Network.responseReceived":
                r["status"] = params["response"].get("status")
            elif method == "Network.loadingFinished":
                r["state"] = "done"
                r["ms"] = round((params.get("timestamp", 0) - (r["t"] or 0)) * 1000)
            elif method == "Network.loadingFailed":
                r["state"] = "failed"
                r["error"] = params.get("errorText", "")
    problems = [
        {k: v for k, v in r.items() if k != "t"}
        for r in reqs.values()
        if r["state"] != "done" or (r.get("status") or 0) >= 400
    ]
    slow = sorted((r for r in reqs.values() if r["state"] == "done"), key=lambda r: -r.get("ms", 0))[:5]
    return {
        "requests": len(reqs),
        "problems": problems,
        "slowest": [{"url": r["url"], "ms": r.get("ms")} for r in slow],
    }


def _save_date_time_debug(driver, target: str) -> None:
    """Keep what the browser saw when Date/Time never loaded; never fails the capture."""
    try:
        try:
            net = network_problems(driver.get_log("performance"))
        except Exception as exc:  # noqa: BLE001 — logging not enabled on this driver
            net = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        main = driver.find_elements(By.ID, "mainContent")
        info = {
            "target": target,
            "url": driver.current_url.split("@")[-1],
            "menu_links": [m.text.strip() for m in driver.find_elements(By.CSS_SELECTOR, "#menu-content a")],
            "main_content": (main[0].text if main else "")[:2000],
            "network": net,
        }
        DATE_TIME_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path = DATE_TIME_DEBUG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{target.replace(':', '_')}.json"
        path.write_text(json.dumps(info, indent=2), encoding="utf-8")
        problems = net.get("problems", [])
        logger.warning(
            "[CBW] Date/Time debug target=%s requests=%s problems=%s %s saved=%s",
            target,
            net.get("requests"),
            len(problems),
            [(p["url"].rsplit("/", 1)[-1], p.get("error") or p.get("status") or p["state"]) for p in problems[:6]],
            path.name,
        )
    except Exception:  # noqa: BLE001 — diagnostics only
        logger.warning("[CBW] could not save Date/Time debug target=%s", target, exc_info=True)


def _wait_for_date_time_page(wait: WebDriverWait, target: str, warn: bool = True) -> bool:
    """Wait for the Date/Time content ("Current:" clock) to replace the previous page.

    The click swaps #mainContent in place, so waiting for it to be visible is
    satisfied by the page already there. A slow (cellular) CBW took >2 s on
    2026-10-29 (220036) and we screenshotted General Information instead.
    """
    from services.device_clock import parse_cbw_current

    try:
        # The label shows first with "Init..." under it; the time fills in after.
        wait.until(lambda d: parse_cbw_current(_current_clock_text(d)) is not None)
        return True
    except TimeoutException:
        if warn:
            logger.warning("[CBW] Date/Time page did not load; saving what is shown target=%s", target)
        return False


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
    # Network events, read only when Date/Time doesn't load (_save_date_time_debug).
    chrome_options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

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
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#menu-content a")))
        _wait_for_menu_settled(driver, target)

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
                    opened = _open_date_time(
                        driver,
                        lambda: [
                            m
                            for m in driver.find_elements(By.CSS_SELECTOR, "#menu-content a")
                            if m.text.strip() == "Date/Time"
                        ],
                        target,
                    )
                    if not opened:
                        _save_date_time_debug(driver, target)
                    time.sleep(1)
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
        if not _open_date_time(driver, lambda: _find_date_time_elements(driver), target):
            _save_date_time_debug(driver, target)
        time.sleep(1)
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

    Order: the password one lockout-aware HTTP login accepts (it tries the
    remembered number first; the hint file holds the number only, never the
    password), then the rest. A CBW that rejects every password raises
    CbwAuthError before Selenium starts. Each password gets one try per round before any is
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
    probed = _probe_cbw_auth(host, username, passwords)  # CbwAuthError: no password gets in
    logger.info("[CBW] auth probe host=%s accepted=%s", host, probed)
    if probed is None:
        logger.warning("[CBW] HTTP auth probe got no answer host=%s — trying the full list", host)
    order = first_then_rest(len(passwords), probed, hint)

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
                if not _has_clock(path) and (deadline is None or time.monotonic() < deadline):
                    _recapture_for_clock(url, username, passwords[idx - 1], path, on_progress)
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


def _has_clock(image: Path) -> bool:
    """Whether the capture's sidecar holds a readable "Current:" clock."""
    from services.device_clock import CLOCK_SIDECAR_SUFFIX, parse_cbw_current

    try:
        text = Path(str(image) + CLOCK_SIDECAR_SUFFIX).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return parse_cbw_current(text) is not None


def _recapture_for_clock(url, username, password, image: Path, on_progress) -> bool:
    """One fresh capture when the first one has no clock; keep it only if it has one.

    No clock means the Date/Time page never loaded (every unread CBW on
    2026-10-29: 220036 once, 233201 twice), not a misread: the clock is page
    text. A new Chrome session usually gets the page; if it doesn't either,
    the first capture stays.
    """
    from services.device_clock import CLOCK_SIDECAR_SUFFIX

    image = Path(image)
    retry_name = f"retry_{image.name}"
    retry = image.with_name(retry_name)
    logger.warning("[CBW] no Date/Time clock in capture; recapturing once path=%s", image)
    (on_progress or _noop_progress)("CBW Date/Time did not load; capturing again")
    try:
        screenshot_date_time(url, username, password, image.parent, filename=retry_name, on_progress=on_progress)
        if _has_clock(retry):
            retry.replace(image)
            Path(str(retry) + CLOCK_SIDECAR_SUFFIX).replace(str(image) + CLOCK_SIDECAR_SUFFIX)
            logger.info("[CBW] recapture read the clock path=%s", image)
            return True
        logger.warning("[CBW] recapture had no clock either; keeping the first path=%s", image)
    except Exception as exc:  # noqa: BLE001 — the first capture still stands
        logger.warning("[CBW] recapture failed path=%s: %s: %s", image, type(exc).__name__, str(exc)[:200])
    finally:
        for leftover in (retry, Path(str(retry) + CLOCK_SIDECAR_SUFFIX)):
            leftover.unlink(missing_ok=True)
    return False


def _setting(name: str, default):
    try:
        from django.conf import settings

        return getattr(settings, name, default)
    except Exception:  # noqa: BLE001 — usable outside Django too
        return default


def _read_password_hint(host: str) -> int | None:
    return read_hint(host)


def _write_password_hint(host: str, index: int) -> None:
    write_hint(host, index)

