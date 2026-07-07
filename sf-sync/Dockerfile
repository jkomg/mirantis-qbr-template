# Salesforce sync sidecar — pulls account data into qbr.data.json
# -----------------------------------------------------------------
# Runs as a second container next to the nginx static-server. Exposes
# a small Flask endpoint (POST /pull) plus a CLI for batch use.

FROM python:3.12-slim

WORKDIR /app

# Install deps as a separate layer for caching
COPY sf-sync/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY sf-sync/oauth.py   /app/oauth.py
COPY sf-sync/sync.py    /app/sync.py
COPY sf-sync/server.py  /app/server.py

# Ensure /data/accounts exists for written JSON
RUN mkdir -p /data/accounts

ENV PYTHONUNBUFFERED=1 \
    OUTPUT_DIR=/data/accounts \
    SYNC_VERSION=v0.1

EXPOSE 8081

# Gunicorn for production, but `python server.py` is fine for local
CMD ["gunicorn", "--bind", "0.0.0.0:8081", "--workers", "2", "--access-logfile", "-", "server:app"]
