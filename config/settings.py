"""Django settings for DST Camera Audit."""

from pathlib import Path
import os

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

_secret_key = os.getenv("DJANGO_SECRET_KEY", "").strip()
if not _secret_key:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY is required. Set it in .env (see .env.example)."
    )
SECRET_KEY = _secret_key

DEBUG = os.getenv("DJANGO_DEBUG", "true").lower() in ("1", "true", "yes")

ALLOWED_HOSTS = [
    h.strip()
    for h in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
    if h.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "cameras",
    "audits",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "config.middleware.RequestLoggingMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/New_York"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

IMS_BASE_URL = os.getenv("IMS_BASE_URL", "").rstrip("/")
IMS_SSO_CLIENT_ID = os.getenv("IMS_SSO_CLIENT_ID", "")
IMS_SSO_CLIENT_SECRET = os.getenv("IMS_SSO_CLIENT_SECRET", "")
IMS_SSO_REDIRECT_URI = os.getenv("IMS_SSO_REDIRECT_URI", "")
IMS_API_TOKEN = os.getenv("IMS_API_TOKEN", "")
IMS_TLS_VERIFY = os.getenv("IMS_TLS_VERIFY", "true").lower() in ("1", "true", "yes")
DST_LOCAL_ADMIN = os.getenv("DST_LOCAL_ADMIN", "false").lower() in ("1", "true", "yes")

TF_VNC_PASSWORD = os.getenv("TF_VNC_PASSWORD", "")
CBW_USERNAME = os.getenv("CBW_USERNAME", "")
CBW_PASSWORDS = [
    p.strip().strip("'\"")
    for p in os.getenv("CBW_PASSWORDS", "").split(",")
    if p.strip().strip("'\"")
]

# DragonEye / TeamViewer (camera app login after TeamViewer connects)
TV_USERNAME = os.getenv("TV_USERNAME", "") or os.getenv("DRAGONEYE_CAMERA_USERNAME", "")
TV_PASSWORDS = [
    p.strip().strip("'\"")
    for p in (
        os.getenv("TV_PASSWORD")
        or os.getenv("TV_PASSWORDS")
        or os.getenv("DE_TV_PASSWORDS")
        or ""
    ).split(",")
    if p.strip().strip("'\"")
]
# Optional separate TeamViewer *connection* passwords; defaults to TV_PASSWORDS.
# Connection tries each entry until a session opens.
TEAMVIEWER_PASSWORDS = [
    p.strip().strip("'\"")
    for p in os.getenv("TEAMVIEWER_PASSWORDS", "").split(",")
    if p.strip().strip("'\"")
] or list(TV_PASSWORDS)
# In-session OS / DragonCam login always uses the *last* TV_PASSWORD entry
# (e.g. TV_PASSWORD='conn1,camLogin' → connect may try both; camera uses camLogin).
_tv_login_source = TV_PASSWORDS or TEAMVIEWER_PASSWORDS
TV_CAMERA_PASSWORDS = [_tv_login_source[-1]] if _tv_login_source else []
TEAMVIEWER_PATH = os.getenv(
    "TEAMVIEWER_PATH",
    r"C:\Program Files\TeamViewer\TeamViewer.exe",
)

# OvrC portal — FX customer dashboard local date/time (Selenium)
OVRC_USERNAME = (os.getenv("OVRC_USERNAME", "") or "").strip().strip("'\"")
OVRC_PASSWORD = (os.getenv("OVRC_PASSWORD", "") or "").strip().strip("'\"")
OVRC_BASE_URL = (
    os.getenv("OVRC_BASE_URL", "https://app.ovrc.com") or "https://app.ovrc.com"
).rstrip("/")

# VBE Daily Checks export root (OneDrive / shared folder).
# Example: C:\Users\<you>\Blue Line Solutions, LLC\VBE Daily Checks
VBE_DAILY_CHECKS_ROOT = os.getenv("VBE_DAILY_CHECKS_ROOT", "")

AUDIT_MAX_CONCURRENT = int(os.getenv("AUDIT_MAX_CONCURRENT", "2"))
# Parallel CBW + VNC lane captures within a single pole_bundle job.
AUDIT_STEP_CONCURRENT = int(os.getenv("AUDIT_STEP_CONCURRENT", "3"))

from config.logging import build_logging_config  # noqa: E402

LOGGING = build_logging_config(BASE_DIR)
LOG_LEVEL = os.getenv("LOG_LEVEL", "info")
LOG_FILE = os.getenv("LOG_FILE", "logs/dst.log")
