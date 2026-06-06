"""Compatibility shim.

If the Heroku config var ``DJANGO_SETTINGS_MODULE`` is set to the bare value
``production`` (instead of the full path ``cortex_pos.settings.production``),
Django would fail with ``ModuleNotFoundError: No module named 'production'``.
This top-level module makes that bare value resolve to the real production
settings, so the build/runtime work either way.

The recommended config is to NOT set DJANGO_SETTINGS_MODULE at all (wsgi/asgi
default to production, the Procfile release passes it explicitly), but this
keeps things working if it is set.
"""
from cortex_pos.settings.production import *  # noqa: F401,F403
