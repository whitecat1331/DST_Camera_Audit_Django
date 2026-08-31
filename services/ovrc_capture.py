"""OvrC portal helpers for DragonEye FX cameras.

- Local date/time from the customer dashboard (screenshot)
- WattBox "DCAM System" outlet: turn ON only when currently OFF
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]

DEFAULT_BASE_URL = "https://app.ovrc.com"
_FX_RE = re.compile(r"^FX\d+$", re.IGNORECASE)
# e.g. Sat, Aug 01, 2026 07:06 PM EDT
_LOCAL_TIME_RE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,?\s+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+"
    r"\d{1,2},?\s+\d{4}\s+\d{1,2}:\d{2}\s*(?:AM|PM)\s+[A-Z]{2,5}",
    re.IGNORECASE,
)


def _noop_progress(_: str) -> None:
    return None


@dataclass(frozen=True)
class OvrcTimeResult:
    fx_number: str
    local_time: str
    screenshot_path: Path
    dashboard_url: str


@dataclass(frozen=True)
class OvrcDcamResult:
    fx_number: str
    action: str  # "turned_on" | "already_on"
    detail: str = ""

class OvrcClient:
    """Logged-in OvrC Chrome session; reuse across many FX lookups."""

    def __init__(
        self,
        username: str,
        password: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        headless: bool = True,
        on_progress: ProgressFn | None = None,
    ) -> None:
        if not username:
            raise RuntimeError("OVRC_USERNAME is not configured")
        if not password:
            raise RuntimeError("OVRC_PASSWORD is not configured")
        self.username = username
        self.password = password
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.headless = headless
        self.progress = on_progress or _noop_progress
        self.driver: webdriver.Chrome | None = None
        self._wait: WebDriverWait | None = None

    def __enter__(self) -> OvrcClient:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        if self.driver is not None:
            return
        chrome_options = Options()
        chrome_options.add_argument("--start-maximized")
        if self.headless:
            chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--window-size=1440,1000")
        chrome_options.add_argument("--disable-dev-shm-usage")
        # Avoid "Chrome is being controlled by automated test software" noise.
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])

        self.progress("OvrC launching Chrome")
        t0 = time.perf_counter()
        self.driver = webdriver.Chrome(service=Service(), options=chrome_options)
        self.driver.set_page_load_timeout(60)
        self.driver.set_script_timeout(60)
        self._wait = WebDriverWait(self.driver, 45)
        logger.info("[OVRC] Chrome ready in %.1fs", time.perf_counter() - t0)
        self.login()

    def close(self) -> None:
        if self.driver is None:
            return
        try:
            self.driver.quit()
        except Exception:  # noqa: BLE001
            logger.debug("[OVRC] driver.quit failed", exc_info=True)
        self.driver = None
        self._wait = None
        logger.debug("[OVRC] Chrome closed")

    def login(self) -> None:
        assert self.driver is not None and self._wait is not None
        login_url = f"{self.base_url}/#/login"
        self.progress("OvrC opening login")
        logger.info("[OVRC] GET login")
        self.driver.get(login_url)
        self._dismiss_whats_new()

        email = self._wait.until(
            EC.visibility_of_element_located(
                (
                    By.XPATH,
                    "//input[@type='email' or @name='email' or @name='username' "
                    "or @formcontrolname='email' or @formcontrolname='username' "
                    "or @placeholder='Email' or @aria-label='Email']"
                    " | //label[contains(translate(., 'EMAIL', 'email'), 'email')]"
                    "/following::input[1]",
                )
            )
        )
        password = self._wait.until(
            EC.visibility_of_element_located(
                (
                    By.XPATH,
                    "//input[@type='password' or @name='password' "
                    "or @formcontrolname='password' or @aria-label='Password']"
                    " | //label[contains(translate(., 'PASSWORD', 'password'), 'password')]"
                    "/following::input[1]",
                )
            )
        )

        self._fill_angular_input(email, self.username)
        self._fill_angular_input(password, self.password)
        time.sleep(0.4)

        login_btn = self._wait_for_enabled_login_button()
        self.progress("OvrC submitting login")
        self.driver.execute_script("arguments[0].click();", login_btn)

        # Land on customers (or any authenticated shell).
        try:
            self._wait.until(
                lambda d: "#/login" not in (d.current_url or "").lower()
                or self._customers_shell_visible(d)
            )
        except TimeoutException as exc:
            hint = self._login_error_hint()
            raise RuntimeError(
                f"OvrC login did not leave the login page{hint}"
            ) from exc

        # Prefer explicit customers list.
        if "#/customers" not in (self.driver.current_url or "").lower():
            self.driver.get(f"{self.base_url}/#/customers")
        self._wait_for_customers_list()
        self._dismiss_whats_new()
        logger.info("[OVRC] logged in url=%s", self.driver.current_url)

    def capture_fx_local_time(
        self,
        fx_number: str,
        output_dir: str | Path,
        filename: str | None = None,
    ) -> OvrcTimeResult:
        """Search FX, open dashboard, scrape local time, screenshot."""
        assert self.driver is not None and self._wait is not None
        fx = self._normalize_fx(fx_number)

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        filepath = output_dir / (filename or f"ovrc_{fx.lower()}.png")

        t0 = time.perf_counter()
        self._open_fx_customer_dashboard(fx)

        self.progress(f"OvrC reading local time for {fx}")
        local_time = self._read_local_date_and_time()
        self._dismiss_whats_new()
        self.driver.save_screenshot(str(filepath))
        url = self.driver.current_url or ""
        logger.info(
            "[OVRC] fx=%s local_time=%s path=%s elapsed=%.1fs",
            fx,
            local_time,
            filepath.name,
            time.perf_counter() - t0,
        )
        self.progress(f"OvrC {fx}: {local_time}")
        return OvrcTimeResult(
            fx_number=fx,
            local_time=local_time,
            screenshot_path=filepath,
            dashboard_url=url,
        )

    def ensure_dcam_system_on(self, fx_number: str) -> OvrcDcamResult:
        """Turn WattBox outlet 'DCAM System' ON only if currently OFF."""
        assert self.driver is not None and self._wait is not None
        fx = self._normalize_fx(fx_number)
        t0 = time.perf_counter()

        self._open_fx_customer_dashboard(fx)
        self._open_devices_tab()
        self._open_wattbox_device(fx)
        result = self._ensure_dcam_outlet_on(fx)

        logger.info(
            "[OVRC] fx=%s dcam action=%s detail=%s elapsed=%.1fs",
            fx,
            result.action,
            result.detail,
            time.perf_counter() - t0,
        )
        self.progress(f"OvrC {fx} DCAM: {result.action}")
        return result

    # --- internals ---------------------------------------------------------

    @staticmethod
    def _normalize_fx(fx_number: str) -> str:
        fx = (fx_number or "").strip().upper()
        if not _FX_RE.match(fx):
            raise ValueError(f"fx_number must look like FX1234, got {fx_number!r}")
        return fx

    def _open_fx_customer_dashboard(self, fx: str) -> None:
        self.progress(f"OvrC searching {fx}")
        self._goto_customers()
        self._search_customers(fx)
        self._open_first_customer_match(fx)

    def _fill_angular_input(self, el, value: str) -> None:
        """Set an Angular/Material input so validators and ngModel update."""
        assert self.driver is not None
        el.click()
        try:
            el.clear()
        except Exception:  # noqa: BLE001
            pass
        el.send_keys(Keys.CONTROL, "a")
        el.send_keys(Keys.BACKSPACE)
        el.send_keys(value)
        # Fallback: native value setter + input/change events (Angular change detection).
        self.driver.execute_script(
            """
            const el = arguments[0], val = arguments[1];
            const proto = window.HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
            if (desc && desc.set) { desc.set.call(el, val); }
            else { el.value = val; }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
            """,
            el,
            value,
        )

    def _find_login_button(self):
        assert self.driver is not None
        candidates = self.driver.find_elements(
            By.XPATH,
            "//button[contains(translate(normalize-space(.), "
            "'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'LOG IN')]"
            " | //button[@type='submit']"
            " | //*[self::a or self::button][contains(@class,'login')]",
        )
        for el in candidates:
            try:
                if el.is_displayed():
                    return el
            except StaleElementReferenceException:
                continue
        raise RuntimeError("OvrC LOG IN button not found")

    def _wait_for_enabled_login_button(self):
        assert self.driver is not None
        deadline = time.time() + 15
        last = None
        while time.time() < deadline:
            try:
                btn = self._find_login_button()
                last = btn
                disabled = btn.get_attribute("disabled")
                aria = (btn.get_attribute("aria-disabled") or "").lower()
                cls = (btn.get_attribute("class") or "").lower()
                if (
                    btn.is_enabled()
                    and disabled in (None, "false", "0", "")
                    and aria not in {"true"}
                    and "disabled" not in cls.split()
                ):
                    return btn
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)
        if last is not None:
            return last
        raise RuntimeError("OvrC LOG IN button not found / never enabled")

    def _login_error_hint(self) -> str:
        assert self.driver is not None
        try:
            body = (self.driver.find_element(By.TAG_NAME, "body").text or "").strip()
        except Exception:  # noqa: BLE001
            return ""
        # Surface short auth error text if present; never include typed password.
        for needle in (
            "invalid",
            "incorrect",
            "failed",
            "unable",
            "locked",
            "error",
            "wrong",
        ):
            for line in body.splitlines():
                low = line.strip().lower()
                if needle in low and 3 < len(line.strip()) < 160:
                    return f" ({line.strip()})"
        return ""

    @staticmethod
    def _customers_shell_visible(driver) -> bool:
        try:
            texts = driver.find_elements(
                By.XPATH,
                "//*[contains(translate(., 'CUSTOMERS', 'customers'), 'customers')]",
            )
            return any(t.is_displayed() for t in texts[:8])
        except Exception:  # noqa: BLE001
            return False

    def _wait_for_customers_list(self) -> None:
        assert self._wait is not None
        self._wait.until(
            EC.any_of(
                EC.presence_of_element_located(
                    (
                        By.XPATH,
                        "//input[contains(translate(@placeholder,'SEARCHCUSTOMERS',"
                        "'searchcustomers'),'search') "
                        "or contains(translate(@aria-label,'SEARCHCUSTOMERS',"
                        "'searchcustomers'),'search')]",
                    )
                ),
                EC.presence_of_element_located(
                    (
                        By.XPATH,
                        "//*[contains(translate(normalize-space(.),'CUSTOMERS','customers'),"
                        "'customers')]",
                    )
                ),
            )
        )
        time.sleep(0.8)

    def _dismiss_whats_new(self) -> None:
        assert self.driver is not None
        for xpath in (
            "//button[contains(translate(normalize-space(.),"
            "'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'DISMISS')]",
            "//*[self::button or self::a][normalize-space(.)='DISMISS']",
        ):
            try:
                for btn in self.driver.find_elements(By.XPATH, xpath):
                    if btn.is_displayed():
                        self.driver.execute_script("arguments[0].click();", btn)
                        time.sleep(0.3)
                        return
            except Exception:  # noqa: BLE001
                continue

    def _goto_customers(self) -> None:
        assert self.driver is not None
        url = (self.driver.current_url or "").lower()
        # Already on list (not a nested customer dashboard).
        if url.rstrip("/").endswith("#/customers") or url.endswith("#/customers/"):
            self._dismiss_whats_new()
            return
        self.driver.get(f"{self.base_url}/#/customers")
        self._wait_for_customers_list()
        self._dismiss_whats_new()

    def _search_box(self):
        assert self.driver is not None and self._wait is not None
        return self._wait.until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//input[contains(translate(@placeholder,'SEARCHCUSTOMERS',"
                    "'searchcustomers'),'search') "
                    "or contains(translate(@aria-label,'SEARCHCUSTOMERS',"
                    "'searchcustomers'),'search') "
                    "or contains(translate(@placeholder,'CUSTOMER','customer'),'customer')]"
                    " | //label[contains(translate(.,'SEARCH','search'),'search')]"
                    "/following::input[1]",
                )
            )
        )

    def _search_customers(self, fx: str) -> None:
        assert self.driver is not None
        box = self._search_box()
        box.click()
        # Clear prior query (Ctrl+A / select-all then delete).
        box.send_keys(Keys.CONTROL, "a")
        box.send_keys(Keys.BACKSPACE)
        time.sleep(0.2)
        box.send_keys(fx.lower())
        # Debounced filter — wait for a row mentioning this FX.
        deadline = time.time() + 25
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                rows = self._customer_rows_matching(fx)
                if rows:
                    return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
            time.sleep(0.4)
        raise RuntimeError(f"OvrC search for {fx} returned no customers") from last_err

    def _customer_rows_matching(self, fx: str):
        assert self.driver is not None
        fx_u = fx.upper()
        # Prefer table body rows / list items that mention the FX id.
        xpath = (
            f"//tr[.//*[contains(translate(., 'fx', 'FX'), '{fx_u}')]]"
            f" | //*[contains(@class,'customer') or contains(@class,'row') "
            f"or self::a][contains(translate(., 'fx', 'FX'), '{fx_u}')]"
        )
        out = []
        for el in self.driver.find_elements(By.XPATH, xpath):
            try:
                if not el.is_displayed():
                    continue
                text = (el.text or "").upper()
                if fx_u in text:
                    out.append(el)
            except StaleElementReferenceException:
                continue
        return out

    def _open_first_customer_match(self, fx: str) -> None:
        assert self.driver is not None and self._wait is not None
        rows = self._customer_rows_matching(fx)
        if not rows:
            raise RuntimeError(f"OvrC: no customer row for {fx}")

        target = rows[0]
        # Prefer an inner link named like "Blue Line - FX…".
        clickable = target
        try:
            links = target.find_elements(By.XPATH, ".//a|.//*[@role='link']")
            for link in links:
                if fx.upper() in (link.text or "").upper() and link.is_displayed():
                    clickable = link
                    break
        except StaleElementReferenceException:
            pass

        self.progress(f"OvrC opening dashboard for {fx}")
        self.driver.execute_script("arguments[0].click();", clickable)

        self._wait.until(
            lambda d: "dashboard" in (d.current_url or "").lower()
            or self._local_time_label_present(d)
        )
        # Dashboard widgets hydrate after route change.
        time.sleep(1.2)
        self._dismiss_whats_new()

    @staticmethod
    def _local_time_label_present(driver) -> bool:
        try:
            els = driver.find_elements(
                By.XPATH,
                "//*[contains(translate(., 'LOCALDATEANDTIME',"
                "'localdateandtime'), 'local date and time')]",
            )
            return any(e.is_displayed() for e in els[:6])
        except Exception:  # noqa: BLE001
            return False

    def _read_local_date_and_time(self) -> str:
        assert self.driver is not None and self._wait is not None
        # Wait until Location Information section has rendered a clock-like string.
        deadline = time.time() + 30
        last_body = ""
        while time.time() < deadline:
            try:
                # Strategy 1: label sibling / following value.
                labels = self.driver.find_elements(
                    By.XPATH,
                    "//*[contains(translate(normalize-space(.),"
                    "'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),"
                    "'LOCAL DATE AND TIME')]",
                )
                for lab in labels:
                    if not lab.is_displayed():
                        continue
                    # Same node may include the value; else following sibling / parent text.
                    blob = (lab.text or "").strip()
                    m = _LOCAL_TIME_RE.search(blob)
                    if m:
                        return _normalize_time(m.group(0))
                    parent = lab.find_element(By.XPATH, "./..")
                    blob = (parent.text or "").strip()
                    m = _LOCAL_TIME_RE.search(blob)
                    if m:
                        return _normalize_time(m.group(0))
                    # Next sibling containers.
                    for sib in lab.find_elements(
                        By.XPATH, "./following-sibling::*[position()<=3]"
                    ):
                        m = _LOCAL_TIME_RE.search(sib.text or "")
                        if m:
                            return _normalize_time(m.group(0))

                # Strategy 2: scan page text for the timestamp pattern near Location Information.
                body = self.driver.find_element(By.TAG_NAME, "body").text or ""
                last_body = body
                if "LOCAL DATE AND TIME" in body.upper() or "Location Information" in body:
                    m = _LOCAL_TIME_RE.search(body)
                    if m:
                        return _normalize_time(m.group(0))
            except (NoSuchElementException, StaleElementReferenceException):
                pass
            time.sleep(0.5)

        sample = " ".join(last_body.split())[:240]
        raise RuntimeError(
            f"OvrC LOCAL DATE AND TIME not found on dashboard (page sample: {sample!r})"
        )

    def _open_devices_tab(self) -> None:
        assert self.driver is not None and self._wait is not None
        self.progress("OvrC opening Devices tab")
        self._dismiss_whats_new()
        # Prefer hash navigation when already on a customer route.
        url = self.driver.current_url or ""
        if "/dashboard" in url.lower():
            devices_url = re.sub(
                r"/dashboard/?$",
                "/devices",
                url,
                count=1,
                flags=re.IGNORECASE,
            )
            if devices_url != url:
                self.driver.get(devices_url)
            else:
                self._click_nav_label("DEVICES")
        else:
            self._click_nav_label("DEVICES")

        self._wait.until(
            lambda d: "devices" in (d.current_url or "").lower()
            or self._devices_table_visible(d)
        )
        time.sleep(1.0)
        self._dismiss_whats_new()

    def _click_nav_label(self, label: str) -> None:
        assert self.driver is not None
        needle = label.strip().upper()
        candidates = self.driver.find_elements(
            By.XPATH,
            "//a|//button|//*[@role='tab']|//*[@role='link']",
        )
        for el in candidates:
            try:
                if not el.is_displayed():
                    continue
                text = (el.text or "").strip().upper()
                if text == needle or text.startswith(needle + " "):
                    self.driver.execute_script("arguments[0].click();", el)
                    return
            except StaleElementReferenceException:
                continue
        raise RuntimeError(f"OvrC nav tab {label!r} not found")

    @staticmethod
    def _devices_table_visible(driver) -> bool:
        try:
            for el in driver.find_elements(
                By.XPATH,
                "//*[contains(translate(., 'DEVICENAME', 'devicename'), 'device name') "
                "or contains(translate(., 'WATTBOX', 'wattbox'), 'wattbox') "
                "or contains(translate(., 'ADD DEVICE', 'add device'), 'add device')]",
            ):
                if el.is_displayed():
                    return True
        except Exception:  # noqa: BLE001
            return False
        return False

    def _open_wattbox_device(self, fx: str) -> None:
        """Click the WattBox (or FX-named) device on the Devices list."""
        assert self.driver is not None and self._wait is not None
        self.progress(f"OvrC opening WattBox device for {fx}")
        deadline = time.time() + 25
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                link = self._find_wattbox_device_link(fx)
                if link is not None:
                    self.driver.execute_script("arguments[0].click();", link)
                    self._wait.until(
                        lambda d: "device/" in (d.current_url or "").lower()
                        or self._outlet_controls_visible(d)
                    )
                    time.sleep(1.0)
                    self._dismiss_whats_new()
                    return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
            time.sleep(0.4)
        raise RuntimeError(
            f"OvrC WattBox / device link not found for {fx}"
        ) from last_err

    def _find_wattbox_device_link(self, fx: str):
        assert self.driver is not None
        fx_u = fx.upper()
        # Prefer a WattBox row; fall back to a device-name link containing FX.
        rows = self.driver.find_elements(By.XPATH, "//tr|.//*[@role='row']")
        wattbox_link = None
        fx_link = None
        for row in rows:
            try:
                if not row.is_displayed():
                    continue
                text = (row.text or "").upper()
                links = row.find_elements(By.XPATH, ".//a|.//*[@role='link']")
                if not links:
                    continue
                link = links[0]
                if "WATTBOX" in text:
                    wattbox_link = link
                if fx_u in (link.text or "").upper() or fx_u in text:
                    fx_link = link
            except StaleElementReferenceException:
                continue
        if wattbox_link is not None:
            return wattbox_link
        if fx_link is not None:
            return fx_link
        # Last resort: any visible link whose text is exactly the FX id.
        for link in self.driver.find_elements(By.XPATH, "//a|.//*[@role='link']"):
            try:
                if link.is_displayed() and (link.text or "").strip().upper() == fx_u:
                    return link
            except StaleElementReferenceException:
                continue
        return None

    @staticmethod
    def _outlet_controls_visible(driver) -> bool:
        try:
            for el in driver.find_elements(
                By.XPATH,
                "//*[contains(translate(., 'OUTLETCONTROLS', 'outletcontrols'), "
                "'outlet controls') or contains(translate(., 'DCAM SYSTEM', "
                "'dcam system'), 'dcam system')]",
            ):
                if el.is_displayed():
                    return True
        except Exception:  # noqa: BLE001
            return False
        return False

    def _ensure_dcam_outlet_on(self, fx: str) -> OvrcDcamResult:
        assert self.driver is not None and self._wait is not None
        self.progress(f"OvrC checking DCAM System for {fx}")
        card = self._wait_for_dcam_card()
        if self._dcam_is_on(card):
            return OvrcDcamResult(
                fx_number=fx,
                action="already_on",
                detail="DCAM System toggle already ON",
            )

        toggle = self._find_dcam_toggle(card)
        if toggle is None:
            raise RuntimeError(f"OvrC DCAM System toggle not found for {fx}")

        self.progress(f"OvrC turning DCAM System ON for {fx}")
        self.driver.execute_script("arguments[0].click();", toggle)

        # Wait for ON state or success toast.
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                card = self._find_dcam_card() or card
                if self._dcam_is_on(card):
                    return OvrcDcamResult(
                        fx_number=fx,
                        action="turned_on",
                        detail="DCAM System turned ON",
                    )
                body = (self.driver.find_element(By.TAG_NAME, "body").text or "").lower()
                if "turned on successfully" in body or "outlet" in body and "turned on" in body:
                    return OvrcDcamResult(
                        fx_number=fx,
                        action="turned_on",
                        detail="DCAM System turn-on confirmed",
                    )
            except StaleElementReferenceException:
                pass
            time.sleep(0.4)

        # Re-check once more; some UIs lag the label.
        card = self._find_dcam_card() or card
        if self._dcam_is_on(card):
            return OvrcDcamResult(
                fx_number=fx,
                action="turned_on",
                detail="DCAM System turned ON",
            )
        raise RuntimeError(f"OvrC DCAM System did not turn ON for {fx}")

    def _wait_for_dcam_card(self):
        assert self._wait is not None
        self._wait.until(lambda d: self._find_dcam_card() is not None)
        card = self._find_dcam_card()
        if card is None:
            raise RuntimeError("OvrC DCAM System outlet card not found")
        return card

    def _find_dcam_label(self):
        assert self.driver is not None
        best = None
        best_score = 10**9
        for el in self.driver.find_elements(
            By.XPATH,
            "//*[contains(normalize-space(.), 'DCAM System')]",
        ):
            try:
                if not el.is_displayed():
                    continue
                text = (el.text or "").strip()
                if "DCAM System" not in text:
                    continue
                score = len(text)
                if score < best_score:
                    best = el
                    best_score = score
            except StaleElementReferenceException:
                continue
        return best

    def _find_dcam_card(self):
        """Return the outlet card that contains DCAM System + its MUI switch."""
        label = self._find_dcam_label()
        if label is None:
            return None
        el = label
        for _ in range(16):
            try:
                switches = el.find_elements(
                    By.XPATH,
                    './/span[contains(@class,"MuiSwitch-root")]'
                    ' | .//*[@role="switch"]'
                    ' | .//input[contains(@class,"MuiSwitch-input")]',
                )
                if any(s.is_displayed() for s in switches):
                    return el
                el = el.find_element(By.XPATH, "./..")
            except Exception:  # noqa: BLE001
                break
        return None

    def _find_dcam_toggle(self, card):
        """Prefer the MUI switch input (or switchBase) inside the outlet card."""
        if card is None:
            return None
        for xpath in (
            './/input[contains(@class,"MuiSwitch-input")]',
            './/span[contains(@class,"MuiSwitch-switchBase")]',
            './/span[contains(@class,"MuiSwitch-root")]',
            './/*[@role="switch"]',
            './/input[@type="checkbox"]',
        ):
            try:
                for el in card.find_elements(By.XPATH, xpath):
                    if el.is_displayed() or el.tag_name.lower() == "input":
                        return el
            except StaleElementReferenceException:
                continue
        return None

    def _dcam_is_on(self, card) -> bool:
        """True when the MUI switch for DCAM System is checked/ON."""
        assert self.driver is not None
        toggle = self._find_dcam_toggle(card)
        if toggle is not None:
            try:
                if toggle.tag_name.lower() == "input":
                    return bool(
                        self.driver.execute_script("return !!arguments[0].checked;", toggle)
                    )
                # switchBase / root: Mui-checked class marks ON.
                nodes = [toggle]
                try:
                    nodes.append(toggle.find_element(By.XPATH, "./.."))
                    nodes.append(
                        toggle.find_element(
                            By.XPATH,
                            './ancestor::span[contains(@class,"MuiSwitch-root")][1]',
                        )
                    )
                except Exception:  # noqa: BLE001
                    pass
                for node in nodes:
                    cls = node.get_attribute("class") or ""
                    if "MuiSwitch-switchBase" in cls:
                        return "Mui-checked" in cls
                for node in nodes:
                    if "Mui-checked" in (node.get_attribute("class") or ""):
                        return True
                inp = None
                try:
                    inp = card.find_element(
                        By.XPATH, './/input[contains(@class,"MuiSwitch-input")]'
                    )
                except Exception:  # noqa: BLE001
                    inp = None
                if inp is not None:
                    return bool(
                        self.driver.execute_script("return !!arguments[0].checked;", inp)
                    )
            except StaleElementReferenceException:
                pass
        try:
            text = (card.text or "").upper()
        except StaleElementReferenceException:
            return False
        if re.search(r"\bOFF\b", text) and not re.search(r"\bON\b", text):
            return False
        if re.search(r"\bON\b", text) and not re.search(r"\bOFF\b", text):
            return True
        return False


def _normalize_time(raw: str) -> str:
    return " ".join((raw or "").split())


def ensure_dcam_on_for_fxes(
    fx_numbers: list[str],
    username: str,
    password: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    headless: bool = True,
    on_progress: ProgressFn | None = None,
) -> list[OvrcDcamResult]:
    """Login once and ensure DCAM System is ON for each FX (no-op if already on)."""
    progress = on_progress or _noop_progress
    results: list[OvrcDcamResult] = []
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in fx_numbers:
        fx = (raw or "").strip().upper()
        if not fx or fx in seen:
            continue
        seen.add(fx)
        ordered.append(fx)
    if not ordered:
        return results

    with OvrcClient(
        username,
        password,
        base_url=base_url,
        headless=headless,
        on_progress=progress,
    ) as client:
        for fx in ordered:
            results.append(client.ensure_dcam_system_on(fx))
    return results


def capture_fx_times(
    fx_numbers: list[str],
    username: str,
    password: str,
    output_dir: str | Path,
    *,
    base_url: str = DEFAULT_BASE_URL,
    headless: bool = True,
    on_progress: ProgressFn | None = None,
) -> list[OvrcTimeResult]:
    """Login once and capture local time screenshots for each FX serial."""
    progress = on_progress or _noop_progress
    results: list[OvrcTimeResult] = []
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in fx_numbers:
        fx = (raw or "").strip().upper()
        if not fx or fx in seen:
            continue
        seen.add(fx)
        ordered.append(fx)
    if not ordered:
        return results

    with OvrcClient(
        username,
        password,
        base_url=base_url,
        headless=headless,
        on_progress=progress,
    ) as client:
        for fx in ordered:
            results.append(
                client.capture_fx_local_time(
                    fx,
                    output_dir,
                    filename=f"ovrc_{fx.lower()}.png",
                )
            )
    return results
