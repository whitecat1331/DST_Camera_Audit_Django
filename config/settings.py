"""Django settings for DST Camera Audit."""

from pathlib import Path
import os
import sys

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _load_secret_file(env_key: str, secret_path: str) -> None:
    """Inject a Docker secret file into the environment unless already set.

    Mirrors IMS ``config.LoadSecretFile``: .env / process env wins, and outside
    Docker the file is absent so this is a no-op.
    """
    if os.getenv(env_key, "").strip():
        return
    try:
        value = Path(secret_path).read_text(encoding="utf-8").strip()
    except OSError:
        return
    if value:
        os.environ[env_key] = value


for _key, _secret in (
    ("DJANGO_SECRET_KEY", "django_secret_key"),
    ("DB_USER", "db_user"),
    ("DB_PASSWORD", "db_password"),
    ("IMS_API_TOKEN", "ims_api_token"),
    ("IMS_SSO_CLIENT_SECRET", "ims_sso_client_secret"),
):
    _load_secret_file(_key, f"/run/secrets/{_secret}")

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
    "cameras",
    "audits.apps.AuditsConfig",
    "django.contrib.staticfiles",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "config.middleware.ReviewOnlyMiddleware",
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

# DST_ROLE=capture (laptop, default): captures always land in local SQLite —
#   the camera VPN blocks Postgres egress — and DB_* (if set) is exposed as the
#   "shared" alias used only to publish finished runs.
# DST_ROLE=review (Linode): the shared Supabase DB from DB_* is the default.
# DB_* follows IMS (db.go): Supabase transaction pooler on 6543 with verify-full
# TLS. Run migrate / bulk loads over the session pooler (5432).
DST_ROLE = os.getenv("DST_ROLE", "capture").strip().lower()
if DST_ROLE not in ("capture", "review"):
    raise ImproperlyConfigured("DST_ROLE must be 'capture' or 'review'.")

_sqlite_db = {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": BASE_DIR / "db.sqlite3",
    # A scheduled run writes from its own process beside runserver.
    "OPTIONS": {"timeout": 20},
}
_shared_db = None
_db_host = os.getenv("DB_HOST", "").strip()
if _db_host:
    _db_password = os.getenv("DB_PASSWORD", "")
    if not _db_password:
        raise ImproperlyConfigured("DB_PASSWORD is required when DB_HOST is set.")
    _db_options = {
        "sslmode": os.getenv("DB_SSLMODE", "verify-full").strip(),
        # Transaction pooling cannot hold server-side prepared statements.
        "prepare_threshold": None,
        # Fail fast when Postgres egress is blocked (e.g. on the camera VPN).
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
    }
    _db_rootcert = os.getenv("DB_SSLROOTCERT", "").strip()
    if _db_rootcert:
        _rootcert_path = Path(_db_rootcert)
        if not _rootcert_path.is_absolute():
            _rootcert_path = BASE_DIR / _rootcert_path
        _db_options["sslrootcert"] = str(_rootcert_path)
    elif _db_options["sslmode"] in ("verify-ca", "verify-full"):
        raise ImproperlyConfigured(
            "DB_SSLROOTCERT is required when DB_SSLMODE is verify-ca or verify-full."
        )
    _shared_db = {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": _db_host,
        "PORT": os.getenv("DB_PORT", "6543").strip(),
        "USER": os.getenv("DB_USER", "postgres").strip(),
        "PASSWORD": _db_password,
        "NAME": os.getenv("DB_NAME", "postgres").strip(),
        "OPTIONS": _db_options,
        # Keep client connections to the pooler: a new one costs ~330 ms (TLS +
        # auth) from the Linode. Safe with transaction pooling because
        # prepared statements are off; .iterator() cursors still break under it.
        "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "300")),
        "CONN_HEALTH_CHECKS": True,
        "DISABLE_SERVER_SIDE_CURSORS": True,
    }

if DST_ROLE == "review":
    if _shared_db is None:
        raise ImproperlyConfigured("DST_ROLE=review requires DB_HOST (shared DB).")
    DATABASES = {"default": _shared_db}
else:
    DATABASES = {"default": _sqlite_db}
    if _shared_db is not None:
        DATABASES["shared"] = _shared_db

# Tests never touch Supabase: publish is exercised against a second SQLite DB.
if sys.argv[1:2] == ["test"]:
    DATABASES = {
        "default": _sqlite_db,
        "shared": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "shared-test.sqlite3"},
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

# The review instance reads published image bytes from the shared DB; the laptop
# keeps captures on disk. Static files are served by WhiteNoise (no proxy).
STORAGES = {
    "default": {
        "BACKEND": "audits.storage.DatabaseStorage"
        if DST_ROLE == "review"
        else "django.core.files.storage.FileSystemStorage"
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if DST_ROLE == "review"
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}

# Production hardening (the review instance runs with DJANGO_DEBUG=false behind
# gunicorn TLS on the IMS Linode, e.g. https://45.79.220.29:8443).
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    # No HSTS: the host is a bare IP with a self-signed certificate (shared with IMS).

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Like IMS's in-memory session cache: authenticated requests read the session
# from process memory instead of a DB round trip (~32 ms each to Supabase from
# the review Linode, and a slot in its ~16-connection pooler).
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "OPTIONS": {"MAX_ENTRIES": 5000}}}
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"

# Technicians who split each round's review (usernames as IMS SSO creates
# them). Used by publish to assign cameras; snapshotted per round.
DST_REVIEWERS = [u.strip() for u in os.getenv("DST_REVIEWERS", "").split(",") if u.strip()]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

IMS_BASE_URL = os.getenv("IMS_BASE_URL", "").rstrip("/")
IMS_SSO_CLIENT_ID = os.getenv("IMS_SSO_CLIENT_ID", "")
IMS_SSO_CLIENT_SECRET = os.getenv("IMS_SSO_CLIENT_SECRET", "")
IMS_SSO_REDIRECT_URI = os.getenv("IMS_SSO_REDIRECT_URI", "")
IMS_API_TOKEN = os.getenv("IMS_API_TOKEN", "")
IMS_TLS_VERIFY = os.getenv("IMS_TLS_VERIFY", "true").lower() in ("1", "true", "yes")
# Pin IMS's self-signed certificate (PEM) instead of disabling verification.
IMS_CA_FILE = os.getenv("IMS_CA_FILE", "").strip()
if IMS_CA_FILE and not Path(IMS_CA_FILE).is_absolute():
    IMS_CA_FILE = str(BASE_DIR / IMS_CA_FILE)
DST_LOCAL_ADMIN = os.getenv("DST_LOCAL_ADMIN", "false").lower() in ("1", "true", "yes")
# Optional override for Post-Install Site Documents root (default: auto-detect X:\Site Documents).
DE_PI_SITE_DOCUMENTS_ROOT = os.getenv("DE_PI_SITE_DOCUMENTS_ROOT", "").strip()

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
# In-session OS login: prefer the *last* TV_PASSWORD entry, then try the rest
# (e.g. TV_PASSWORD='tvConn,osLogin' → connect tries both; OS tries osLogin then tvConn).
_tv_login_source = TV_PASSWORDS or TEAMVIEWER_PASSWORDS
if _tv_login_source:
    _preferred_login = _tv_login_source[-1]
    TV_CAMERA_PASSWORDS = [_preferred_login] + [
        p for p in _tv_login_source if p != _preferred_login
    ]
else:
    TV_CAMERA_PASSWORDS = []
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
# Example: C:\Users\<you>\Blue Line Solutions, LLC\PSCU Production - Documents\VBE Daily Checks
VBE_DAILY_CHECKS_ROOT = os.getenv("VBE_DAILY_CHECKS_ROOT", "")

AUDIT_MAX_CONCURRENT = int(os.getenv("AUDIT_MAX_CONCURRENT", "2"))
# Parallel CBW + VNC lane captures within a single pole_bundle job.
AUDIT_STEP_CONCURRENT = int(os.getenv("AUDIT_STEP_CONCURRENT", "3"))
# DST fleet run: LTI sites captured at once (each runs its CBW + VNC steps), and
# CBW relay power-ons at once. DE stays one site at a time (one desktop).
DST_LTI_SITE_CONCURRENT = int(os.getenv("DST_LTI_SITE_CONCURRENT", "4"))
DST_POWER_ON_CONCURRENT = int(os.getenv("DST_POWER_ON_CONCURRENT", "10"))
# Stop starting new CBW password attempts after this many seconds (0 = no limit).
CBW_CAPTURE_BUDGET_SECONDS = int(os.getenv("CBW_CAPTURE_BUDGET_SECONDS", "150"))
# Max seconds a queued job will wait for a free worker slot.
AUDIT_SLOT_WAIT_SECONDS = int(os.getenv("AUDIT_SLOT_WAIT_SECONDS", "300"))
# After DST Audit turns cameras on, wait before starting captures (boot settle).
DST_POWER_SETTLE_SECONDS = int(os.getenv("DST_POWER_SETTLE_SECONDS", "90"))
# WatchGuard openvpn.exe management port, used when netstat can't name it.
DST_VPN_MGMT_PORT = int(os.getenv("DST_VPN_MGMT_PORT", "1337"))
# How long a held publish waits for the shared DB (VPN off) before giving up.
DST_PUBLISH_WAIT_SECONDS = int(os.getenv("DST_PUBLISH_WAIT_SECONDS", str(12 * 3600)))
# Publish each site as it finishes (once the VPN is off), so review fills in during the run.
DST_LIVE_PUBLISH = os.getenv("DST_LIVE_PUBLISH", "1").strip().lower() not in {"0", "false", "no", "off"}
# Images uploaded at once while publishing (measured: 4 = 2.5x serial, 8 no better).
DST_PUBLISH_UPLOAD_CONCURRENT = int(os.getenv("DST_PUBLISH_UPLOAD_CONCURRENT", "4"))
# Read device clocks off VNC/DragonCam captures with Windows OCR (services/device_clock.py).
DEVICE_CLOCK_OCR = os.getenv("DEVICE_CLOCK_OCR", "1").strip().lower() not in {"0", "false", "no", "off"}

from config.logging import build_logging_config  # noqa: E402

LOGGING = build_logging_config(BASE_DIR)
LOG_LEVEL = os.getenv("LOG_LEVEL", "info")
LOG_FILE = os.getenv("LOG_FILE", "logs/dst.log")
