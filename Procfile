web: gunicorn cortex_pos.wsgi --bind 0.0.0.0:$PORT --log-file -
release: python manage.py migrate --noinput --settings=cortex_pos.settings.production
