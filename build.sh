#!/usr/bin/env bash
# Build step for the host (Render: Build Command = "bash build.sh")
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate --noinput


if [ -n "${DJANGO_SUPERUSER_EMAIL:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
  python manage.py createsuperuser --noinput || echo "Admin already exists - skipped"
fi