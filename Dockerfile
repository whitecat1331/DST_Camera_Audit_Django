# DST Camera Audit — review instance (DST_ROLE=review).
# Serves published DST audits from the shared Supabase DB; never captures.
# Build/run via deploy/docker-compose.yml — see deploy/README.md.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DST_ROLE=review \
    DJANGO_DEBUG=false \
    LOG_TO_FILE=false \
    DB_SSLROOTCERT=certs/supabase_ca.crt

WORKDIR /app
COPY requirements-review.txt .
RUN pip install --no-cache-dir -r requirements-review.txt

COPY . .
# Settings need a secret key and (review role) a DB host to import; collectstatic
# never connects, so build-only placeholders are enough.
RUN DJANGO_SECRET_KEY=build-only DB_HOST=build-only DB_PASSWORD=build-only \
    python manage.py collectstatic --noinput

RUN useradd --system --uid 10001 dst
USER dst

EXPOSE 8443
# TLS is terminated by gunicorn with IMS's certificate (mounted at /certs).
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8443", \
     "--certfile", "/certs/cert.pem", "--keyfile", "/certs/key.pem", \
     "--workers", "2", "--threads", "4", "--timeout", "60", \
     "--access-logfile", "-"]
