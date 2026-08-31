"""IMS HTTP client for SSO token exchange and ASE installation sync."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


class IMSClientError(RuntimeError):
    pass


def _base_url() -> str:
    base = (settings.IMS_BASE_URL or "").rstrip("/") + "/"
    if not settings.IMS_BASE_URL:
        raise IMSClientError("IMS_BASE_URL is not configured")
    return base


def exchange_sso_code(code: str) -> dict[str, str]:
    url = urljoin(_base_url(), "api/external/sso/token")
    payload = {
        "client_id": settings.IMS_SSO_CLIENT_ID,
        "client_secret": settings.IMS_SSO_CLIENT_SECRET,
        "code": code,
    }
    if not settings.IMS_SSO_CLIENT_ID or not settings.IMS_SSO_CLIENT_SECRET:
        raise IMSClientError("IMS SSO client credentials are not configured")
    logger.info("[IMS] SSO token exchange request")
    with httpx.Client(timeout=30.0, verify=settings.IMS_TLS_VERIFY) as client:
        try:
            resp = client.post(url, json=payload)
        except httpx.HTTPError as exc:
            logger.warning(
                "[IMS] SSO token exchange connection error url=%s exc=%s",
                url,
                type(exc).__name__,
            )
            raise IMSClientError(
                f"Could not reach IMS at {settings.IMS_BASE_URL} "
                f"({type(exc).__name__}). If IMS runs with --https, use the "
                "https:// scheme (e.g. https://127.0.0.1), not http://."
            )
    if 300 <= resp.status_code < 400:
        loc = resp.headers.get("location", "")
        logger.warning(
            "[IMS] SSO token exchange redirected status=%s location=%s",
            resp.status_code,
            loc,
        )
        raise IMSClientError(
            f"IMS redirected the token request (HTTP {resp.status_code}"
            f"{' to ' + loc if loc else ''}). Point --ims at the HTTPS app URL, "
            "not the HTTP redirect port."
        )
    if resp.status_code >= 400:
        logger.warning("[IMS] SSO token exchange failed status=%s", resp.status_code)
        raise IMSClientError(f"SSO token exchange failed ({resp.status_code})")
    try:
        data = resp.json()
    except ValueError:
        logger.warning(
            "[IMS] SSO token response not JSON status=%s body=%r",
            resp.status_code,
            resp.text[:200],
        )
        raise IMSClientError("SSO token exchange returned a non-JSON response")
    if "username" not in data or "role" not in data:
        logger.warning("[IMS] SSO token response missing username/role")
        raise IMSClientError("SSO token response missing username/role")
    logger.info("[IMS] SSO token exchange ok user=%s role=%s", data["username"], data["role"])
    return {"username": data["username"], "role": data["role"]}


def check_ims_connection() -> dict[str, Any]:
    """Return connection status for the dashboard KPI.

    Keys: status (connected|auth_error|unreachable|not_configured), detail
    """
    if not settings.IMS_BASE_URL:
        return {"status": "not_configured", "detail": "IMS_BASE_URL missing"}
    if not settings.IMS_API_TOKEN:
        return {"status": "not_configured", "detail": "IMS_API_TOKEN missing"}

    base = settings.IMS_BASE_URL.rstrip("/") + "/"
    headers = {"Authorization": f"Bearer {settings.IMS_API_TOKEN}"}
    try:
        with httpx.Client(timeout=5.0, verify=settings.IMS_TLS_VERIFY, headers=headers) as client:
            # Prefer lightweight health, then auth-gated ASE probe.
            try:
                health = client.get(urljoin(base, "healthz"))
                if health.status_code >= 500:
                    logger.warning("[IMS] healthz status=%s", health.status_code)
                    return {"status": "unreachable", "detail": f"healthz {health.status_code}"}
            except httpx.HTTPError as exc:
                logger.debug("[IMS] healthz probe failed: %s", type(exc).__name__)

            resp = client.get(
                urljoin(base, "api/external/ase-installations"),
                params={"page": 1, "pageSize": 1},
            )
            if resp.status_code in (401, 403):
                logger.warning("[IMS] ASE probe auth_error status=%s", resp.status_code)
                return {"status": "auth_error", "detail": f"HTTP {resp.status_code}"}
            if resp.status_code >= 400:
                logger.warning("[IMS] ASE probe unreachable status=%s", resp.status_code)
                return {"status": "unreachable", "detail": f"HTTP {resp.status_code}"}
            logger.debug("[IMS] connection probe ok")
            return {"status": "connected", "detail": "OK"}
    except httpx.HTTPError as exc:
        logger.warning("[IMS] connection probe failed: %s", type(exc).__name__)
        return {"status": "unreachable", "detail": type(exc).__name__}


def fetch_ase_installations(*, include_devices: bool = False) -> list[dict[str, Any]]:
    if not settings.IMS_API_TOKEN:
        raise IMSClientError("IMS_API_TOKEN is not configured")
    base = _base_url()
    headers = {"Authorization": f"Bearer {settings.IMS_API_TOKEN}"}
    page = 1
    page_size = 200
    all_rows: list[dict[str, Any]] = []
    logger.info(
        "[IMS] ASE fetch start include_devices=%s page_size=%s",
        include_devices,
        page_size,
    )
    with httpx.Client(timeout=120.0, verify=settings.IMS_TLS_VERIFY, headers=headers) as client:
        while True:
            params: dict[str, Any] = {"page": page, "pageSize": page_size}
            if include_devices:
                params["include"] = "devices"
            resp = client.get(
                urljoin(base, "api/external/ase-installations"),
                params=params,
            )
            if resp.status_code >= 400:
                logger.error("[IMS] ASE fetch failed page=%s status=%s", page, resp.status_code)
                raise IMSClientError(f"ASE sync failed ({resp.status_code})")
            data = resp.json()
            rows = data.get("installations") or []
            all_rows.extend(rows)
            logger.debug(
                "[IMS] ASE page=%s rows=%s total_so_far=%s hasNext=%s",
                page,
                len(rows),
                len(all_rows),
                data.get("hasNext"),
            )
            if not data.get("hasNext"):
                break
            page += 1
            if page > 500:
                logger.warning("[IMS] ASE sync stopped at page cap")
                break
    logger.info("[IMS] ASE fetch complete rows=%s pages=%s", len(all_rows), page)
    return all_rows
