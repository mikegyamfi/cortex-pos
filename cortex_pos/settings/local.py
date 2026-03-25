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

