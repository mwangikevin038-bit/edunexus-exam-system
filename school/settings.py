from dotenv import load_dotenv
load_dotenv()

"""
Django settings for EduNexus Exam System.
"""
import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env_csv(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]

# ==============================================================================
# SECURITY — READ FROM ENVIRONMENT, NEVER HARDCODE
# ==============================================================================
# In production, set these as environment variables.
# For local dev, create a .env file and load with python-dotenv.
SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-CHANGE-THIS-BEFORE-PRODUCTION'  # dev fallback only
)

import hashlib, hmac
SUPERUSER_SECRET_TOKEN = hmac.new(
    SECRET_KEY.encode(), b'superuser-login-salt', hashlib.sha256
).hexdigest()[:20]

DEBUG = os.environ.get('DJANGO_DEBUG', 'True') == 'True'

if not DEBUG and SECRET_KEY.startswith('django-insecure-'):
    raise ImproperlyConfigured('DJANGO_SECRET_KEY must be set to a secure value when DJANGO_DEBUG=False.')

LOCAL_ALLOWED_HOSTS = [
    'localhost',
    '.localhost',
    '127.0.0.1',
    '192.168.36.186',
    '192.168.52.230',
    '192.168.30.230',
    '192.168.29.91',
    '10.161.194.230',
    '192.168.112.230',
    '192.168.1.101',
    '192.168.1.100',
    '192.168.39.127',
    'edunexus.local',
]

ALLOWED_HOSTS = env_csv('ALLOWED_HOSTS', ','.join(LOCAL_ALLOWED_HOSTS) if DEBUG else '')
if not DEBUG and not ALLOWED_HOSTS:
    raise ImproperlyConfigured('ALLOWED_HOSTS must be set when DJANGO_DEBUG=False.')


# ==============================================================================
# APPS
# ==============================================================================
INSTALLED_APPS = [
    'daphne',
    'channels',
    'unfold',
    'students.apps.StudentsConfig',
    'superuser',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'axes',  # Brute-force login protection — pip install django-axes
]


# ==============================================================================
# MIDDLEWARE — ORDER MATTERS
# ==============================================================================
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'students.security.middleware.SecurityHeadersMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    # Messages must be set up before any middleware that may add a
    # message (ForcePasswordChangeMiddleware, tenant blockers, etc.).
    'django.contrib.messages.middleware.MessageMiddleware',
    'students.security.middleware.ForcePasswordChangeMiddleware',
    'axes.middleware.AxesMiddleware',
    'students.security.middleware.SessionSchoolValidator',
    'students.school_scope.CurrentSchoolMiddleware',
    'students.security.middleware.TenantIsolationMiddleware',
    'students.security.middleware.SecurityAuditMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Close stale DB connections at the end of every request so the
    # auto-save endpoints + async audit logger don't accumulate idle
    # connections until Postgres' max_connections is hit.
    'students.security.middleware.CloseOldConnectionsMiddleware',
]

# ==============================================================================
# AUTHENTICATION
# ==============================================================================
AUTHENTICATION_BACKENDS = [
    # django-axes must wrap the real backend
    'axes.backends.AxesStandaloneBackend',
    'students.backends.SchoolScopedAuthBackend',
]

# ==============================================================================
# BRUTE-FORCE PROTECTION (django-axes)
# ==============================================================================
AXES_FAILURE_LIMIT = 10          # Lock after 10 failed attempts
AXES_COOLOFF_TIME = 0.25         # Locked for 15 minutes
AXES_RESET_ON_SUCCESS = True     # Reset fail counter after successful login
AXES_LOCKOUT_PARAMETERS = ['username', 'ip_address']

# ==============================================================================
# FIELD ENCRYPTION & DATA INTEGRITY
# ==============================================================================
FIELD_ENCRYPTION_KEY = os.environ.get('FIELD_ENCRYPTION_KEY', SECRET_KEY)
DATA_INTEGRITY_KEY = os.environ.get('DATA_INTEGRITY_KEY', SECRET_KEY)

# ==============================================================================
# RATE LIMITING (django cache backend)
# ==============================================================================
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'edunexus-security-ratelimit',
    },
    'csv_upload': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': os.environ.get("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/1"),
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
        },
        'KEY_PREFIX': 'csv_upload',
        'TIMEOUT': 600,
    },
}
RATELIMIT_DISABLE = os.environ.get('RATELIMIT_DISABLE', 'False') == 'True'

# ==============================================================================
# SESSION SECURITY
# ==============================================================================
SESSION_ENGINE = 'django.contrib.sessions.backends.db'  # Store sessions in DB
SESSION_COOKIE_HTTPONLY = True        # JS cannot read session cookie
SESSION_COOKIE_SAMESITE = 'Lax'      # CSRF protection
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_AGE = 60 * 60 * 8     # 8 hours


# ==============================================================================
# DATA-IMPORT / MIGRATION DEFAULTS
# ------------------------------------------------------------------------------
# These are the defaults used by the management commands that import /
# restore student rosters from external sources (e.g. scanned mark-entry
# sheets, CSVs).  Override any of them with the matching env var in
# your deployment.
# ==============================================================================
# Phone used to identify a placeholder Guardian row attached to a
# student who has been freshly imported but whose real parent has not
# yet been linked.  Import commands will reuse the same Guardian row
# across all unlinked students of one import run, so the user can
# then re-link them in the admin without ending up with a pile of
# throwaway guardians.
UNLINKED_GUARDIAN_PHONE = os.environ.get(
    'EDUNEXUS_UNLINKED_GUARDIAN_PHONE', '0700000000',
)
# Name written to the placeholder Guardian row so it's easy to spot
# in the admin and re-link via the link button.
UNLINKED_GUARDIAN_NAME = os.environ.get(
    'EDUNEXUS_UNLINKED_GUARDIAN_NAME', 'Unlinked — link in Faculty admin',
)

# Development: HTTP is fine. Production: set both to True.
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_HTTPONLY = False       # JS must read csrftoken cookie for AJAX CSRF

# ==============================================================================
# SECURITY HEADERS
# ==============================================================================
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'strict-origin-when-cross-origin'
SECURE_CROSS_ORIGIN_OPENER_POLICY = 'same-origin'

# Production only — don't enable in dev (breaks HTTP)
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
else:
    # Prepared for production HTTPS without breaking local HTTP development.
    SECURE_SSL_REDIRECT = os.environ.get('SECURE_SSL_REDIRECT', 'False') == 'True'


# ==============================================================================
# CSRF TRUSTED ORIGINS (local network dev)
# ==============================================================================
CSRF_TRUSTED_ORIGINS = [
    'http://*.localhost:8000',
    'http://192.168.36.186:8000',
    'http://192.168.30.230:8000',
    'http://192.168.29.91:8000',
    'http://10.161.194.230:8000',
    'http://192.168.112.230:8000',
    'http://192.168.1.100:8000',
    'http://192.168.39.127:8000',
    'http://localhost:8000',
    'http://127.0.0.1:8000',
]


# ==============================================================================
# DATABASE — credentials from environment
# ==============================================================================
# CONN_MAX_AGE:
#   * Production (DEBUG=False): persistent connections (60s) so a busy
#     worker can reuse them — saves the ~3-15 ms TCP+TLS handshake per
#     request.
#   * Development (DEBUG=True): force connections closed after every
#     request (CONN_MAX_AGE=0). The auto-save endpoints + async audit
#     logger can otherwise open enough connections to exhaust Postgres's
#     default max_connections=100, producing intermittent
#     "OperationalError: too many clients already" errors during local
#     testing.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('DB_NAME', 'school_exam_db'),
        'USER': os.environ.get('DB_USER', 'postgres'),
        'PASSWORD': os.environ.get('DB_PASSWORD', ''),   # never hardcode
        'HOST': os.environ.get('DB_HOST', '127.0.0.1'),
        'PORT': os.environ.get('DB_PORT', '5432'),
        'CONN_MAX_AGE': 60 if not DEBUG else 0,
    },
}


# ==============================================================================
# PASSWORD VALIDATION
# ==============================================================================
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# ==============================================================================
# UNFOLD ADMIN
# ==============================================================================
UNFOLD = {
    "SITE_TITLE": "EDUNEXUS Superadmin Engine",
    "SITE_HEADER": "EDUNEXUS",
    "SHOW_METRICS": True,
    "DASHBOARD_CALLBACK": "students.dashboard.get_dashboard_context",
    "SIDEBAR": {
        "show_search": True,
        "navigation": [
            {
                "title": "Core Operations",
                "separator": True,
                "items": [
                    {"title": "Dashboard Overview", "link": "/admin/", "icon": "dashboard"},
                    {"title": "Subscribed Schools", "link": "/admin/students/school/",
                     "icon": "corporate_fare", "description": "Manage school accounts"},
                ],
            },
            {
                "title": "Access & Security",
                "separator": True,
                "items": [
                    {"title": "Global System Users", "link": "/admin/auth/user/", "icon": "manage_accounts"},
                    {"title": "Permission Groups", "link": "/admin/auth/group/", "icon": "gavel"},
                    {"title": "Login Attempts", "link": "/admin/axes/accessattempt/", "icon": "security"},
                ],
            },
        ],
    },
}


ROOT_URLCONF = 'school.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [
            BASE_DIR / 'school' / 'templates',
            BASE_DIR / 'students' / 'templates',
            BASE_DIR / 'superuser' / 'templates',
        ],
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'students.context_processors.school_context',
            ],
            'loaders': [
                'django.template.loaders.filesystem.Loader',
                'django.template.loaders.app_directories.Loader',
            ],
        },
    },
]

WSGI_APPLICATION = 'school.wsgi.application'
ASGI_APPLICATION = 'school.asgi.application'

# ── Celery (background queue) ────────────────────────────────────────────
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/1")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "Africa/Nairobi"
CELERY_ENABLE_UTC = True
CELERY_WORKER_CONCURRENCY = 2
CELERY_WORKER_MAX_TASKS_PER_CHILD = 200
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 300
CELERY_TASK_SOFT_TIME_LIMIT = 240
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "visibility_timeout": 3600,
    "socket_connect_timeout": 5,
    "socket_timeout": 5,
    "retry_on_timeout": True,
}

# ── Django Channels (WebSocket) ──────────────────────────────────────────
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [("127.0.0.1", 6379)],
            "capacity": 1000,
        },
    },
}

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Nairobi'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

DJANGO_ADMIN_LOGS_ENABLED = False

# ==============================================================================
# EMAIL — credentials from environment
# ==============================================================================
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.gmail.com'
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = f'EDUNEXUS Portal <{EMAIL_HOST_USER}>'
EMAIL_CHARSET = 'utf-8'
DEFAULT_CHARSET = 'utf-8'


# ==============================================================================
# SECURITY LOGGING
# ==============================================================================
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'security': {
            'format': '[%(asctime)s] %(levelname)s %(name)s: %(message)s',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
    },
    'handlers': {
        'security_file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs' / 'security.log',
            'maxBytes': 5 * 1024 * 1024,  # 5 MB
            'backupCount': 5,
            'formatter': 'security',
        },
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'security',
        },
    },
    'loggers': {
        'students.backends': {
            'handlers': ['security_file', 'console'],
            'level': 'INFO',
            'propagate': False,
        },
        'students.school_scope': {
            'handlers': ['security_file'],
            'level': 'WARNING',
            'propagate': False,
        },
        'students.security': {
            'handlers': ['security_file', 'console'],
            'level': 'INFO',
            'propagate': False,
        },
        'students.models': {
            'handlers': ['security_file'],
            'level': 'WARNING',
            'propagate': False,
        },
        'axes': {
            'handlers': ['security_file'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}
