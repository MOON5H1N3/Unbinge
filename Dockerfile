FROM python:3.11-slim

# tzdata so ZoneInfo can resolve TZ (e.g. Europe/London); curl for HEALTHCHECK.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:5000/health || exit 1

# Waitress rather than the Werkzeug development server. Single process:
# the APScheduler job starts at import, so more than one worker would mean
# more than one drip job racing on the same files.
#
# NOTE: runs as root. A non-root USER was tried and reverted - the bind-mounted
# /config directory is owned by the host user, so UID 1000 got
# "attempt to write a readonly database" on startup. See Dockerfile.nonroot
# for a version that drops privileges correctly via an entrypoint.
CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "--threads=8", "app:app"]
