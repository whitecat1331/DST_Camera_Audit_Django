"""IMS SSO login views."""

from __future__ import annotations

import logging
import secrets
from urllib.parse import urlencode, urljoin, urlparse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from cameras.models import UserProfile
from services.ims_client import IMSClientError, exchange_sso_code


User = get_user_model()
logger = logging.getLogger(__name__)

_SSO_COOKIE = "dst_sso"
_SSO_COOKIE_PATH = "/accounts/ims/"
_SSO_MAX_AGE = 600
_SSO_SIGNING_SALT = "ims-sso"


def _callback_url(request) -> str:
    if settings.IMS_SSO_REDIRECT_URI:
        return settings.IMS_SSO_REDIRECT_URI
    return request.build_absolute_uri(reverse("ims_callback"))


def _callback_netloc(request) -> str:
    """Host:port for the SSO callback (must match the browser URL during SSO)."""
    parsed = urlparse(_callback_url(request))
    return parsed.netloc.lower()


def _ims_start_path(request) -> str:
    path = reverse("ims_login_start")
    query = request.GET.urlencode()
    if query:
        return f"{path}?{query}"
    return path


def _canonical_ims_start_url(request) -> str:
    """Absolute IMS-start URL on the same host as IMS_SSO_REDIRECT_URI."""
    parsed = urlparse(_callback_url(request))
    if not parsed.scheme or not parsed.netloc:
        return request.build_absolute_uri(_ims_start_path(request))
    return f"{parsed.scheme}://{parsed.netloc}{_ims_start_path(request)}"


def _redirect_to_canonical_sso_host(request):
    """Session cookies are host-scoped — localhost != 127.0.0.1."""
    if request.get_host().lower() == _callback_netloc(request):
        return None
    target = _canonical_ims_start_url(request)
    logger.info(
        "[AUTH] IMS SSO host align %s -> %s",
        request.get_host(),
        _callback_netloc(request),
    )
    return redirect(target)


def _login_redirect(*, error: str = "") -> redirect:
    url = reverse("login")
    if error:
        url = f"{url}?error={error}"
    return redirect(url)


def _attach_sso_cookie(response, *, state: str, next_url: str) -> None:
    payload = {"state": state, "next": next_url}
    response.set_cookie(
        _SSO_COOKIE,
        signing.dumps(payload, salt=_SSO_SIGNING_SALT),
        max_age=_SSO_MAX_AGE,
        httponly=True,
        samesite="Lax",
        path=_SSO_COOKIE_PATH,
    )


def _clear_sso_cookie(response) -> None:
    response.delete_cookie(_SSO_COOKIE, path=_SSO_COOKIE_PATH)


def _load_sso_pending(request) -> tuple[str | None, str]:
    """Return (expected_state, next_url) from signed cookie or session."""
    cookie_val = request.COOKIES.get(_SSO_COOKIE)
    if cookie_val:
        try:
            data = signing.loads(
                cookie_val,
                salt=_SSO_SIGNING_SALT,
                max_age=_SSO_MAX_AGE,
            )
            state = (data.get("state") or "").strip()
            next_url = (data.get("next") or "").strip() or "dashboard"
            if state:
                return state, next_url
        except signing.BadSignature:
            logger.warning("[AUTH] IMS SSO cookie invalid or expired")

    state = (request.session.pop("ims_sso_state", None) or "").strip() or None
    next_url = request.session.pop("ims_sso_next", None) or "dashboard"
    return state, next_url


@require_GET
def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if settings.DST_LOCAL_ADMIN and request.GET.get("local") == "1":
        return render(request, "registration/login_local.html")
    error = request.GET.get("error", "")
    return render(
        request,
        "registration/login.html",
        {
            "ims_configured": bool(
                settings.IMS_BASE_URL
                and settings.IMS_SSO_CLIENT_ID
                and settings.IMS_SSO_CLIENT_SECRET
            ),
            "local_admin_enabled": settings.DST_LOCAL_ADMIN,
            "ims_start_url": _canonical_ims_start_url(request),
            "error": error,
        },
    )


@require_GET
def ims_login_start(request):
    if not (
        settings.IMS_BASE_URL
        and settings.IMS_SSO_CLIENT_ID
        and settings.IMS_SSO_CLIENT_SECRET
    ):
        logger.warning("[AUTH] IMS SSO start rejected: not configured")
        messages.error(request, "IMS SSO is not configured.")
        return redirect("login")

    host_redirect = _redirect_to_canonical_sso_host(request)
    if host_redirect is not None:
        return host_redirect

    state = secrets.token_urlsafe(24)
    next_url = request.GET.get("next") or ""
    if not next_url.startswith("/"):
        next_url = ""

    request.session["ims_sso_state"] = state
    if next_url:
        request.session["ims_sso_next"] = next_url
    request.session.modified = True

    params = {
        "client_id": settings.IMS_SSO_CLIENT_ID,
        "redirect_uri": _callback_url(request),
        "state": state,
    }
    authorize = urljoin(settings.IMS_BASE_URL.rstrip("/") + "/", "sso/authorize")
    logger.info("[AUTH] IMS SSO redirect started host=%s", request.get_host())
    response = redirect(f"{authorize}?{urlencode(params)}")
    _attach_sso_cookie(response, state=state, next_url=next_url or "/")
    return response


@require_GET
def ims_callback(request):
    code = request.GET.get("code", "").strip()
    state = request.GET.get("state", "").strip()
    expected, next_url = _load_sso_pending(request)

    logger.info(
        "[AUTH] IMS SSO callback host=%s has_code=%s has_state=%s has_expected=%s",
        request.get_host(),
        bool(code),
        bool(state),
        bool(expected),
    )

    if not code or not state:
        logger.warning("[AUTH] IMS SSO callback rejected: missing code/state")
        messages.error(request, "Invalid IMS SSO response.")
        response = _login_redirect(error="sso_missing")
        _clear_sso_cookie(response)
        return response
    if not expected:
        logger.warning(
            "[AUTH] IMS SSO callback rejected: pending state missing host=%s",
            request.get_host(),
        )
        messages.error(
            request,
            "SSO session lost. Clear cookies for 127.0.0.1, open "
            "http://127.0.0.1:8000/, and sign in again.",
        )
        response = _login_redirect(error="sso_session")
        _clear_sso_cookie(response)
        return response
    if state != expected:
        logger.warning("[AUTH] IMS SSO callback rejected: state mismatch")
        messages.error(request, "Invalid IMS SSO response.")
        response = _login_redirect(error="sso_state")
        _clear_sso_cookie(response)
        return response

    try:
        identity = exchange_sso_code(code)
    except IMSClientError as exc:
        logger.warning("[AUTH] IMS SSO token exchange failed: %s", exc)
        messages.error(request, f"IMS SSO failed: {exc}")
        response = redirect("login")
        _clear_sso_cookie(response)
        return response

    username = identity["username"]
    role = identity["role"]
    user, created = User.objects.get_or_create(
        username=username,
        defaults={"is_active": True},
    )
    if created:
        user.set_unusable_password()
        user.save()
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.ims_role = role
    profile.save(update_fields=["ims_role", "updated_at"])

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    logger.info(
        "[AUTH] IMS SSO login ok user=%s role=%s created=%s",
        username,
        role,
        created,
    )
    messages.success(request, f"Signed in as {username} ({role})")
    if isinstance(next_url, str) and next_url.startswith("/"):
        response = redirect(next_url)
    else:
        response = redirect("dashboard")
    _clear_sso_cookie(response)
    return response


@require_POST
@login_required
def logout_view(request):
    username = request.user.get_username()
    logout(request)
    logger.info("[AUTH] logout user=%s", username)
    return redirect("login")
