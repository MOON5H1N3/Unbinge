#!/usr/bin/env bash
#
# update.sh - pulls the latest Unbinge code and rebuilds/restarts the
# container. Run this from the same directory as docker-compose.yaml.
#
# What it does, in order:
#   1. Refuses to run if there are local changes to tracked files (so it
#      never silently clobbers a local tweak you made to app.py etc.)
#   2. Backs up the database via the running app's own backup endpoint,
#      so an update that goes wrong still leaves you with a restorable copy.
#   3. git pull
#   4. docker compose build
#   5. docker compose up -d
#   6. Waits for /health to come back healthy before declaring success.
#
# Usage:
#   ./update.sh
#
set -euo pipefail

cd "$(dirname "$0")"

SERVICE="unbinge"
HEALTH_URL="http://localhost:5000/health"
BACKUP_URL="http://localhost:5000/api/run-backup-now"

log() { printf '[update] %s\n' "$1"; }
die() { printf '[update] ERROR: %s\n' "$1" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git is not installed"
command -v docker >/dev/null 2>&1 || die "docker is not installed"
[ -f docker-compose.yaml ] || die "docker-compose.yaml not found in $(pwd) - run this script from your Unbinge install directory"

# --- 1. refuse to overwrite local edits -------------------------------------
if [ -d .git ]; then
    if ! git diff --quiet -- . ':!config' 2>/dev/null || ! git diff --cached --quiet -- . ':!config' 2>/dev/null; then
        die "local changes to tracked files detected - commit, stash, or discard them before updating (git status)"
    fi
else
    die "this directory is not a git checkout of the Unbinge repo - clone it properly first (see README)"
fi

# --- 2. back up the database, if the app is currently running --------------
log "backing up the database before updating..."
if curl -fsS -X POST "$BACKUP_URL" >/tmp/unbinge-update-backup.json 2>/dev/null; then
    log "backup created: $(grep -o '"message":"[^"]*"' /tmp/unbinge-update-backup.json | cut -d'"' -f4)"
else
    log "WARNING: couldn't reach $BACKUP_URL to take a pre-update backup (is the container running?)."
    read -r -p "Continue the update anyway without a fresh backup? [y/N] " reply
    case "$reply" in
        [yY]*) log "continuing without a fresh backup" ;;
        *) die "aborted - start the container and re-run, or back up manually first" ;;
    esac
fi
rm -f /tmp/unbinge-update-backup.json

# --- 3. pull latest code ----------------------------------------------------
log "pulling latest code..."
BEFORE_SHA="$(git rev-parse HEAD)"
git pull --ff-only
AFTER_SHA="$(git rev-parse HEAD)"

if [ "$BEFORE_SHA" = "$AFTER_SHA" ]; then
    log "already up to date (nothing changed)."
else
    log "updated $BEFORE_SHA -> $AFTER_SHA"
fi

# --- 4/5. rebuild and restart ------------------------------------------------
log "rebuilding image..."
docker compose build

log "restarting container..."
docker compose up -d

# --- 6. wait for health ------------------------------------------------------
log "waiting for the app to come back healthy..."
for i in $(seq 1 30); do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
        log "update complete - $SERVICE is healthy."
        exit 0
    fi
    sleep 2
done

die "container restarted but $HEALTH_URL never came back healthy - check 'docker compose logs $SERVICE'"
