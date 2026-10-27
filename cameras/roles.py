"""IMS role helpers for DST Camera Audit."""

from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from cameras.models import IMS_ROLE_PRIORITY


def get_ims_role(user) -> str:
    if not user or not user.is_authenticated:
        return ""
    profile = getattr(user, "profile", None)
    if profile is None:
        return ""
    return profile.ims_role or ""


# The review instance (DST_ROLE=review) runs no captures: every capability above
# readonly (start/cancel/relays/sync/publish…) is off there for everyone, which
# hides those controls and rejects their endpoints in one place.
REVIEW_MAX_ROLE = "readonly"


def role_at_least(user, min_role: str) -> bool:
    if not user or not user.is_authenticated:
        return False
    if getattr(settings, "DST_ROLE", "capture") == "review" and (
        IMS_ROLE_PRIORITY.get(min_role, 99) > IMS_ROLE_PRIORITY[REVIEW_MAX_ROLE]
    ):
        return False
    if user.is_superuser and getattr(user, "is_staff", False):
        # Break-glass local admin
        return True
    profile = getattr(user, "profile", None)
    if profile is None:
        return False
    return profile.role_at_least(min_role)


def require_ims_role(min_role: str):
    def decorator(view):
        @login_required
        @wraps(view)
        def _wrapped(request, *args, **kwargs):
            if not role_at_least(request.user, min_role):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return _wrapped

    return decorator
