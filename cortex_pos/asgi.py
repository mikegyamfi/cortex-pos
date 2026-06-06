"""
ASGI config for cortex_pos project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

# Production is the default for the deployed server. A DJANGO_SETTINGS_MODULE
# config var, if set, still takes precedence.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'cortex_pos.settings.production')

application = get_asgi_application()
