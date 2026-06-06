"""
WSGI config for cortex_pos project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

# Production is the default for the deployed server (Heroku web dyno). A
# DJANGO_SETTINGS_MODULE config var, if set, still takes precedence.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'cortex_pos.settings.production')

application = get_wsgi_application()
