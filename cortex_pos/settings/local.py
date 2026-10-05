from .base import *

DEBUG = True

ALLOWED_HOSTS = ['localhost', '127.0.0.1']

# Database for development
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# Email Backend for development (prints to console)
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# Static files for local development
STATICFILES_DIRS = [BASE_DIR / 'static']

# Set Dummy keys for SMS/External APIs during local dev
SMS_API_KEY = "local_dev_dummy_key"


# The test suite creates several users per test; the production hasher makes
# that take seconds each. Tests only need *a* hasher, not a slow one.
import sys  # noqa: E402
if len(sys.argv) > 1 and sys.argv[1] == "test":
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
