import os
import sys
import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from .base import *

DEBUG = False

# Refuse to start in production without an explicit secret key — EXCEPT during
# the build's `collectstatic`, which doesn't use the key. This lets the Heroku
# build compile even if the key isn't set yet; real requests and `migrate`
# still require a proper DJANGO_SECRET_KEY.
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    if 'collectstatic' in sys.argv:
        SECRET_KEY = 'insecure-build-only-key-do-not-use-for-serving'
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY environment variable must be set in production.")

# --- Hosts / CSRF --------------------------------------------------------
# Always allow the Heroku app domain; add custom domains via ALLOWED_HOSTS env
# (comma-separated, e.g. "shop.example.com,www.example.com").
ALLOWED_HOSTS = ['.herokuapp.com']
ALLOWED_HOSTS += [h.strip() for h in os.environ.get('ALLOWED_HOSTS', '').split(',') if h.strip()]

# Django requires the scheme for trusted CSRF origins (needed for all POST
# forms once you're behind HTTPS — login, POS, settle debt, etc.).
CSRF_TRUSTED_ORIGINS = ['https://*.herokuapp.com']
CSRF_TRUSTED_ORIGINS += [
    f"https://{h.strip()}"
    for h in os.environ.get('ALLOWED_HOSTS', '').split(',')
    if h.strip() and not h.strip().startswith('.')
]

# --- Database ------------------------------------------------------------
# Heroku Postgres provides DATABASE_URL automatically. Fall back to SQLite so
# build-time steps (collectstatic) never crash if the var isn't present yet.
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL:
    DATABASES = {'default': dj_database_url.parse(DATABASE_URL, conn_max_age=600, ssl_require=True)}
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

# Static files (WhiteNoise) are configured in base.py so the build behaves the
# same no matter which settings module runs collectstatic.

# --- Security ------------------------------------------------------------
# Heroku terminates TLS at its router and forwards the original scheme here.
# Without this, SECURE_SSL_REDIRECT below causes an infinite redirect loop.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_HSTS_SECONDS = 31536000  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# --- Email ---------------------------------------------------------------
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.environ.get('EMAIL_HOST')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER or 'no-reply@example.com')

# --- SMS gateway ---------------------------------------------------------
SMS_API_KEY = os.environ.get('SMS_API_KEY')

# --- Logging (surface errors in `heroku logs`) ---------------------------
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['console'], 'level': 'INFO'},
    'loggers': {
        'django': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'django.request': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False},
    },
}
