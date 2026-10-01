import os
import re
import shutil
import sqlite3
import secrets
import logging
import threading
from contextlib import closing
from datetime import datetime, date, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
import tempfile
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory, send_file, session
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)


@app.context_processor
def inject_shared_template_context():
    """Makes app_version available to every template via base.html, and
    derives active_tab from the request path so nav highlighting doesn't
    need to be set explicitly in every render_template() call."""
    path = request.path
    if path == '/':
        tab = 'dashboard'
    elif path.startswith('/schedule'):
        tab = 'schedule'
    elif path.startswith('/history'):
        tab = 'history'
    elif path.startswith('/system'):
        tab = 'system'
    elif path.startswith('/settings'):
        tab = 'settings'
    else:
        tab = None
    return {'app_version': APP_VERSION, 'active_tab': tab, 'dry_run_active': get_setting('dry_run', '0') == '1',
            'csrf_token': session.get('csrf_token', ''), 'auth_is_enabled': auth_enabled()}


# ---------------------------------------------------------------------------
# Auth (S3) and CSRF (S2)
#
# Single shared password, opt-in via the UNBINGE_PASSWORD environment
# variable. If it's unset, auth is fully disabled and the app behaves
# exactly as it always has - this is a deliberate default, not an
# oversight: shipping auth as suddenly-mandatory on upgrade would lock
# people out of their own install the moment they update, which is a worse
# outcome than staying open until someone deliberately turns it on.
#
# CSRF protection only matters once there's a session cookie to steal, so
# it's gated behind the same UNBINGE_PASSWORD check - an unauthenticated,
# fully-open install has no session for a forged request to ride on.
# ---------------------------------------------------------------------------

# Renamed from DRIPARR_PASSWORD when the app was rebranded to Unbinge - the
# old name is still read as a fallback so an existing deployment's
# compose file/.env keeps working unchanged until it's convenient to update.
AUTH_PASSWORD = os.environ.get('UNBINGE_PASSWORD', '') or os.environ.get('DRIPARR_PASSWORD', '')

AUTH_EXEMPT_PATHS = ('/login', '/logout', '/calendar.ics', '/health', '/static/', '/posters/')


def auth_enabled():
    return bool(AUTH_PASSWORD)


def is_exempt_path(path):
    return any(path == p or path.startswith(p) for p in AUTH_EXEMPT_PATHS)


@app.before_request
def _ensure_csrf_token():
    """Every session gets a CSRF token as soon as it exists, including
    before login - the login form itself needs a valid token, and
    generating one only after authenticating would be a chicken-and-egg
    problem."""
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(16)


@app.before_request
def _enforce_auth_and_csrf():
    if not auth_enabled() or is_exempt_path(request.path):
        return None

    if not session.get('authenticated'):
        if request.path.startswith('/api/'):
            return jsonify({"status": "error", "message": "Authentication required."}), 401
        return redirect(url_for('login_page', next=request.path))

    if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
        submitted = request.form.get('csrf_token') or request.headers.get('X-CSRFToken')
        if not submitted or not secrets.compare_digest(submitted, session.get('csrf_token', '')):
            log.warning("Rejected request to %s: missing or invalid CSRF token.", request.path)
            if request.path.startswith('/api/'):
                return jsonify({"status": "error", "message": "Invalid or missing CSRF token."}), 403
            return "Invalid or missing CSRF token. Go back and try again.", 403
    return None


@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if not auth_enabled():
        return redirect(url_for('index'))

    error = None
    if request.method == 'POST':
        submitted_password = request.form.get('password', '')
        submitted_csrf = request.form.get('csrf_token', '')
        if not secrets.compare_digest(submitted_csrf, session.get('csrf_token', '')):
            error = "Your session expired - try again."
        elif secrets.compare_digest(submitted_password, AUTH_PASSWORD):
            session['authenticated'] = True
            session['csrf_token'] = secrets.token_hex(16)  # rotate on login
            next_path = request.args.get('next') or url_for('index')
            if not next_path.startswith('/'):  # never redirect off-site
                next_path = url_for('index')
            return redirect(next_path)
        else:
            error = "Incorrect password."

    return render_template('login.html', error=error)


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login_page'))


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("unbinge")

# ---------------------------------------------------------------------------
# Config (all container-side paths; comes from docker-compose environment)
# ---------------------------------------------------------------------------
DB_FILE = os.environ.get('DB_PATH', '/config/drip_schedule.db')
POSTERS_DIR = os.path.join(os.path.dirname(DB_FILE), 'posters')


def _get_or_create_secret_key():
    """Flask needs a stable SECRET_KEY to sign session cookies. Generating a
    new one on every startup would silently log everyone out on every
    restart - this persists it in the settings table (bootstrapped directly
    here, ahead of init_db's own schema setup, since login has to work even
    before the rest of the app has finished initializing) the same way
    other durable app state lives there.

    Returns a fixed dummy key under UNBINGE_TESTING rather than touching
    disk at all - this runs at import time, before any test fixture has a
    chance to redirect DB_FILE to an isolated temp path, so without this
    guard every pytest collection would write a secret_key row into the
    REAL production database at whatever DB_PATH the container was started
    with. The same class of mistake _should_start_background_work() already
    exists to prevent, just at import time instead of at the bottom of the
    module."""
    if os.environ.get('UNBINGE_TESTING') == '1':
        return 'test-secret-key-not-for-production'

    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);")
    row = conn.execute("SELECT value FROM settings WHERE key = 'secret_key'").fetchone()
    if row and row['value']:
        key = row['value']
    else:
        key = secrets.token_hex(32)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('secret_key', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key,)
        )
        conn.commit()
    conn.close()
    return key


app.secret_key = _get_or_create_secret_key()
POOL_DIR = os.environ.get('POOL_DIR', '/media/A/TV100')
VAULT_DIR = os.environ.get('VAULT_DIR', '/media/vault')
PLEX_BASE_DIR = os.environ.get('PLEX_BASE_DIR', '/media/private_tv')

PLEX_URL = os.environ.get('PLEX_URL')
PLEX_TOKEN = os.environ.get('PLEX_TOKEN')
PLEX_LIBRARY_ID = os.environ.get('PLEX_LIBRARY_ID')

TABLE_NAME = 'shows'
HISTORY_TABLE = 'drip_history'
SETTINGS_TABLE = 'settings'
DRIPPED_TABLE = 'dripped_episodes'
JOB_RUNS_TABLE = 'job_runs'
DRIP_MOVES_TABLE = 'drip_moves'
EXCLUDED_TABLE = 'excluded_episodes'
QUEUE_TABLE = 'show_queue'
POOL_TVDB_CACHE_TABLE = 'pool_tvdb_cache'
TVDB_GENRE_CACHE_TABLE = 'tvdb_genre_cache'

# Season 0 is the specials folder. Specials are deliberately never dripped:
# they're skipped when choosing the next batch AND ignored when deciding
# whether the vault still has anything left, so their presence can't stall a
# show in "active" forever. They stay in the vault and rejoin the show when it
# graduates back to the pool, so nothing is lost - they just don't get a slot.
SPECIALS_SEASON = 0

SCHEMA_VERSION = 13

SCHEDULER_JOB_ID = 'daily_drip_check'
_scheduler_holder = {'scheduler': None}

APP_VERSION = '0.2.0'


def _resolve_timezone():
    """Resolves the timezone Unbinge schedules against.

    Previously everything used naive local time, which inside a container is
    UTC unless TZ is set - so a drip configured for 03:00 actually fired at
    04:00 for anyone on BST, and weekday boundaries drifted twice a year.
    Now the zone is explicit: TZ from the environment, falling back to UTC
    with a warning rather than silently guessing."""
    name = (os.environ.get('TZ') or '').strip()
    if not name:
        log.warning(
            "TZ is not set - scheduling against UTC. Set TZ (e.g. TZ=Europe/London) "
            "in docker-compose so release days and drip times match your local clock."
        )
        return ZoneInfo('UTC')
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("TZ='%s' is not a recognised timezone - falling back to UTC.", name)
        return ZoneInfo('UTC')


LOCAL_TZ = _resolve_timezone()


def now_local():
    """Current time in the configured zone. Every scheduling decision and
    every stored timestamp goes through this, so the app agrees with itself
    regardless of what the container's clock is set to."""
    return datetime.now(LOCAL_TZ)


def today_local():
    return now_local().date()

# H3 - this dict used to carry every key twice ("0" alongside 0) as a
# workaround for template code that might index it with either type. Turns
# out neither index.html nor edit.html - the only two templates it was ever
# passed to - reference it at all; both hardcode their own day-name tuples
# directly, and nothing in app.py indexes it either. It was dead weight
# solving a problem for a variable nothing reads. Removed outright.

# Matches "S01E02", "s1e2", etc. anywhere in a filename.
# C7: three formats supported. Each returns every (season, episode) tag a
# filename covers - a plain "S01E01" covers one; "S01E01E02" or "S01E01-E02"
# cover two; "1x02" and "1x02x03" are the x-format equivalents.
#
# _SXXE_RUN_RE finds a season marker followed by a RUN of one or more E-tags,
# captured as one blob (group 2) so repeated or dash/dot-separated E-tags are
# all captured together rather than only the first. _EPISODE_TOKEN_RE then
# pulls the individual episode numbers out of that blob.
_SXXE_RUN_RE = re.compile(r'[Ss](\d{1,2})((?:[\.\-_ ]?[Ee]\d{1,3})+)')
_EPISODE_TOKEN_RE = re.compile(r'[Ee](\d{1,3})')

# x-format: season and episode numbers on either side of a bare "x", e.g.
# "1x02" or the multi-episode "1x02x03". The lookbehind/lookahead stop the
# season number from being a substring of a longer digit run - this is what
# keeps it from firing on resolution tags like "1280x720" or "1920x1080":
# every valid start position inside those has a digit immediately before it,
# which the lookbehind excludes. Requiring 2-3 digits per episode (not 1)
# also rules out aspect ratios like "16x9".
_XFORMAT_RUN_RE = re.compile(r'(?<!\d)(\d{1,2})((?:[xX]\d{2,3})+)(?!\d)')
_XFORMAT_TOKEN_RE = re.compile(r'[xX](\d{2,3})')


def extract_episode_tags(filename):
    """Returns every (season, episode) tuple filename covers, or [] if none
    of the supported formats match.

    Replaces the old EPISODE_RE, which used .search() and so only ever read
    the FIRST SxxExx tag in a name - "S01E01E02.mkv" registered as E01 only,
    and E02 was silently unreachable. "1x02" and "S01.E02" weren't recognised
    at all."""
    m = _SXXE_RUN_RE.search(filename)
    if m:
        season = int(m.group(1))
        episodes = [int(e) for e in _EPISODE_TOKEN_RE.findall(m.group(2))]
        return [(season, e) for e in episodes]

    m = _XFORMAT_RUN_RE.search(filename)
    if m:
        season = int(m.group(1))
        episodes = [int(e) for e in _XFORMAT_TOKEN_RE.findall(m.group(2))]
        return [(season, e) for e in episodes]

    return []


# ---------------------------------------------------------------------------
# DB setup / migration
# ---------------------------------------------------------------------------
def get_db():
    """Establishes connection to SQLite database. A busy timeout is set so
    that brief lock contention (the background scheduler and a web request
    touching the DB at the same moment) waits and retries instead of
    immediately raising 'database is locked'."""
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL lets the background scheduler write while a web request reads,
    # instead of the two serialising behind the busy timeout. synchronous
    # NORMAL is the standard companion setting: still crash-safe under WAL,
    # without an fsync on every single commit.
    #
    # These are the first writes the app makes, so a permissions problem on
    # the mounted config directory surfaces here. Reporting it as a WAL error
    # points at the wrong thing entirely, hence the explicit message.
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    except sqlite3.OperationalError as e:
        conn.close()
        raise RuntimeError(
            f"Cannot write to the Unbinge database at {DB_FILE} ({e}). "
            f"This is almost always a permissions problem on the mounted config "
            f"directory rather than a database fault - check that the user the "
            f"container runs as can write to {os.path.dirname(DB_FILE)}."
        ) from e
    return conn


def init_db():
    """Creates the shows table if missing, migrates a legacy table if found,
    and adds any columns that older versions of the DB don't have yet.
    This makes the schema self-healing instead of relying on guessing
    which of several tables holds the real data."""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (TABLE_NAME,))
    have_shows = cursor.fetchone() is not None

    if not have_shows:
        # One-time migration path for older installs that only had the
        # legacy 'release_schedule' table.
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='release_schedule';")
        legacy = cursor.fetchone()
        if legacy:
            log.info("Migrating legacy 'release_schedule' table to '%s'.", TABLE_NAME)
            cursor.execute(f"ALTER TABLE release_schedule RENAME TO {TABLE_NAME};")
        else:
            cursor.execute(f"""
                CREATE TABLE {TABLE_NAME} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    show_name TEXT NOT NULL,
                    vault_path TEXT NOT NULL,
                    plex_path TEXT NOT NULL,
                    release_day INTEGER NOT NULL,
                    release_days TEXT,
                    current_season INTEGER NOT NULL DEFAULT 1,
                    current_episode INTEGER NOT NULL DEFAULT 0,
                    episodes_per_drop INTEGER NOT NULL DEFAULT 1,
                    completed_at TEXT,
                    last_run_date TEXT,
                    paused INTEGER NOT NULL DEFAULT 0,
                    poster_url TEXT
                );
            """)
        conn.commit()

    # Add any columns missing from older schemas.
    cursor.execute(f"PRAGMA table_info({TABLE_NAME});")
    existing_cols = {row['name'] for row in cursor.fetchall()}

    if 'completed_at' not in existing_cols:
        cursor.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN completed_at TEXT;")
        log.info("Added missing 'completed_at' column.")
    if 'last_run_date' not in existing_cols:
        cursor.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN last_run_date TEXT;")
        log.info("Added missing 'last_run_date' column.")
    if 'release_days' not in existing_cols:
        cursor.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN release_days TEXT;")
        log.info("Added missing 'release_days' column.")
        # Backfill from the old single-day column so existing shows keep working.
        cursor.execute(f"SELECT id, release_day FROM {TABLE_NAME}")
        for row in cursor.fetchall():
            cursor.execute(
                f"UPDATE {TABLE_NAME} SET release_days = ? WHERE id = ?",
                (str(row['release_day']), row['id'])
            )
    if 'paused' not in existing_cols:
        cursor.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN paused INTEGER NOT NULL DEFAULT 0;")
        log.info("Added missing 'paused' column.")
    if 'poster_url' not in existing_cols:
        cursor.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN poster_url TEXT;")
        log.info("Added missing 'poster_url' column.")

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            show_id INTEGER,
            show_name TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT,
            occurred_at TEXT NOT NULL
        );
    """)

    # History is read ordered by time and filtered by show; at seven rows this
    # is irrelevant, at seven thousand it isn't.
    cursor.execute(
        f"CREATE INDEX IF NOT EXISTS idx_history_occurred_at ON {HISTORY_TABLE}(occurred_at DESC);"
    )
    cursor.execute(
        f"CREATE INDEX IF NOT EXISTS idx_history_show_id ON {HISTORY_TABLE}(show_id);"
    )

    # C1: the authoritative record of what has actually been dripped.
    # Replaces the single (current_season, current_episode) pointer, which
    # could only express "everything up to here" and so made any episode
    # arriving below that point permanently invisible.
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {DRIPPED_TABLE} (
            show_id INTEGER NOT NULL,
            season INTEGER NOT NULL,
            episode INTEGER NOT NULL,
            dripped_at TEXT NOT NULL,
            PRIMARY KEY (show_id, season, episode)
        );
    """)
    cursor.execute(
        f"CREATE INDEX IF NOT EXISTS idx_dripped_show ON {DRIPPED_TABLE}(show_id);"
    )

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {SETTINGS_TABLE} (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    defaults = {
        'drip_hour': '3',
        'drip_minute': '0',
        'webhook_url': os.environ.get('WEBHOOK_URL', ''),
        'last_job_run': '',
        'discord_webhook_url': os.environ.get('DISCORD_SCHEDULE_WEBHOOK_URL', ''),
        'schedule_message_id': '',
        'dry_run': '0',
        'tvdb_api_key': os.environ.get('TVDB_API_KEY', ''),
        'tvdb_pin': os.environ.get('TVDB_PIN', ''),
        'tvdb_token': '',
        'tvdb_token_expires': '',
        'sonarr_url': os.environ.get('SONARR_URL', ''),
        'sonarr_api_key': os.environ.get('SONARR_API_KEY', ''),
        'notify_on_drip': '1',
        'notify_on_failure': '1',
        'notify_on_missed_run': '1',
        'auto_backup_enabled': '0',
        'backup_retention_count': '7',
        'last_auto_backup': '',
    }
    for k, v in defaults.items():
        cursor.execute(f"INSERT OR IGNORE INTO {SETTINGS_TABLE} (key, value) VALUES (?, ?)", (k, v))

    conn.commit()
    run_migrations(conn)
    conn.close()


def get_setting(key, default=''):
    conn = get_db()
    row = conn.execute(f"SELECT value FROM {SETTINGS_TABLE} WHERE key = ?", (key,)).fetchone()
    conn.close()
    if row is None or row['value'] is None:
        return default
    return row['value']


def set_setting(key, value):
    conn = get_db()
    conn.execute(
        f"INSERT INTO {SETTINGS_TABLE} (key, value) VALUES (?, ?) "
        f"ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value))
    )
    conn.commit()
    conn.close()


def log_history(conn, show_id, show_name, action, detail=''):
    cursor = conn.execute(
        f"INSERT INTO {HISTORY_TABLE} (show_id, show_name, action, detail, occurred_at) VALUES (?, ?, ?, ?, ?)",
        (show_id, show_name, action, detail, now_local().isoformat())
    )
    return cursor.lastrowid


def load_history(limit=100, offset=0, show_name=None, action=None):
    """U25 - previously hardcoded to the latest 200 rows with no way to
    filter or page through older entries. Filtering by show reuses the same
    text the dashboard's filter box already accepts, for consistency."""
    conn = get_db()
    where, params = [], []
    if show_name:
        where.append("show_name LIKE ?")
        params.append(f"%{show_name}%")
    if action:
        where.append("action = ?")
        params.append(action)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM {HISTORY_TABLE} {clause}", params
    ).fetchone()[0]

    rows = conn.execute(
        f"SELECT * FROM {HISTORY_TABLE} {clause} ORDER BY occurred_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def send_notification(event, message, extra=None, poster_url=None):
    """Fires a generic JSON webhook. Works with Discord, Slack, ntfy, or any
    custom listener - sends the message under several common keys so it's
    usable without picking one specific product. Never lets a notification
    failure interrupt the file-moving logic that already succeeded.

    Gated by event category against three settings (matching Sonarr's
    on-grab / on-import / on-health-issue split): normal drip activity,
    failures, and missed-run warnings can each be silenced independently,
    so routine "it dripped" noise can be turned off while keeping the
    alerts that actually need attention.

    poster_url, if given and the target is recognizably a Discord webhook,
    is attached as an embed thumbnail - Discord is the one target here that
    understands rich embeds, so this only activates for Discord specifically
    rather than trying to guess a generic shape every webhook might accept."""
    category_setting = {
        'dripped': 'notify_on_drip', 'cooldown': 'notify_on_drip',
        'graduated': 'notify_on_drip', 'promoted': 'notify_on_drip',
        'failure': 'notify_on_failure', 'missed_run': 'notify_on_missed_run',
    }.get(event)
    if category_setting and get_setting(category_setting, '1') != '1':
        return

    url = get_setting('webhook_url', '').strip()
    if not url:
        return
    payload = {
        'event': event,
        'message': message,
        'text': message,      # Slack-style
        'content': message,   # Discord-style
    }
    if poster_url and parse_discord_webhook(url):
        # A plain 'content' message is still sent alongside the embed, so
        # the notification degrades gracefully if the embed itself fails to
        # render for any reason (e.g. Discord can't fetch the thumbnail URL).
        payload['embeds'] = [{'thumbnail': {'url': poster_url}}]
    if extra:
        payload.update(extra)
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log.warning("Notification webhook failed (non-fatal): %s", e)


DISCORD_ACTION_EMOJI = {'drip': '📺', 'cooldown': '⏳', 'graduate': '🎉'}


TVDB_BASE_URL = 'https://api4.thetvdb.com/v4'


def tvdb_get_token(override_api_key=None, override_pin=None):
    """Logs in to TVDB v4 and returns a bearer token, reusing a cached one
    if it hasn't expired. Tokens are valid ~1 month per TVDB's docs; cached
    in settings with a conservative 25-day expiry so a stale token is
    refreshed well before it would actually fail.

    override_api_key/override_pin let a caller test credentials that
    haven't been saved yet (used by /api/test-tvdb, so "Test Connection"
    checks what's actually typed in the form rather than silently testing
    whatever was last saved - the two can differ if Save wasn't clicked
    first, and testing the wrong one gave a confusing false failure with
    no log output to explain it).

    IMPORTANT: this sandbox has no network access, so this function has
    never been executed against the real TVDB API. The request/response
    shape is written from TVDB's published v4 documentation and several
    third-party client libraries, not from a live test. If login fails,
    check the container logs for the raw response TVDB actually returned -
    that's the fastest way to spot a field-name mismatch."""
    testing_explicit_creds = override_api_key is not None
    api_key = (override_api_key if testing_explicit_creds else get_setting('tvdb_api_key', '')).strip()
    if not api_key:
        log.warning("TVDB login skipped: no API key configured.")
        return None, "No TVDB API key configured."

    if not testing_explicit_creds:
        cached_token = get_setting('tvdb_token', '').strip()
        expires_str = get_setting('tvdb_token_expires', '').strip()
        if cached_token and expires_str:
            try:
                if now_local() < datetime.fromisoformat(expires_str):
                    return cached_token, None
            except ValueError:
                pass

    payload = {'apikey': api_key}
    pin = (override_pin if testing_explicit_creds else get_setting('tvdb_pin', '')).strip()
    if pin:
        payload['pin'] = pin

    try:
        resp = requests.post(f'{TVDB_BASE_URL}/login', json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        token = data.get('data', {}).get('token') or data.get('token')
        if not token:
            log.error("TVDB login succeeded but no token found in response: %s", data)
            return None, "TVDB login response didn't contain a token - see logs for the raw response."
    except requests.RequestException as e:
        log.error("TVDB login failed: %s", e)
        return None, f"TVDB login failed: {e}"
    except ValueError as e:
        log.error("TVDB login returned non-JSON response: %s", e)
        return None, "TVDB login returned an unexpected response - see logs."

    if not testing_explicit_creds:
        set_setting('tvdb_token', token)
        set_setting('tvdb_token_expires', (now_local() + timedelta(days=25)).isoformat())
    return token, None


def tvdb_search_series(query):
    """Searches TVDB for TV series matching query. Returns
    (results: list[{'tvdb_id', 'name', 'year', 'image_url', 'overview'}], error: str|None).

    Field names are defensive - TVDB's /search endpoint has changed field
    naming between versions in third-party reports (id vs tvdb_id, image
    vs image_url), so several plausible keys are checked for each field
    rather than assuming one."""
    token, err = tvdb_get_token()
    if not token:
        return [], err

    try:
        resp = requests.get(
            f'{TVDB_BASE_URL}/search',
            params={'query': query, 'type': 'series'},
            headers={'Authorization': f'Bearer {token}'},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        log.error("TVDB search failed for '%s': %s", query, e)
        return [], f"TVDB search failed: {e}"
    except ValueError:
        return [], "TVDB search returned an unexpected response - see logs."

    raw_results = data.get('data', [])
    results = []
    for r in raw_results:
        tvdb_id = r.get('tvdb_id') or r.get('id')
        if isinstance(tvdb_id, str):
            # Some TVDB responses prefix ids like "series-12345".
            tvdb_id = tvdb_id.split('-')[-1]
        try:
            tvdb_id = int(tvdb_id)
        except (TypeError, ValueError):
            continue
        results.append({
            'tvdb_id': tvdb_id,
            'name': r.get('name') or r.get('translations', {}).get('eng', 'Unknown'),
            'year': r.get('year', ''),
            'image_url': r.get('image_url') or r.get('image', ''),
            'overview': (r.get('overview') or '')[:200],
        })
    return results, None


def cache_poster_locally(show_id, remote_url):
    """Downloads a poster once to POSTERS_DIR and returns a local URL path
    to serve it from, instead of storing TVDB's hotlink URL directly.

    Without this, every dashboard/schedule page load depends on TVDB's CDN
    being reachable and willing to serve hotlinked images indefinitely - if
    they ever change the CDN path, add hotlink protection, or rate-limit
    (some image CDNs do), every poster in the app breaks at once with no
    warning. Downloading once and serving locally means a TVDB outage or
    policy change after the fact doesn't touch anything already linked.

    Returns the local URL path (e.g. '/posters/42.jpg') on success, or the
    original remote_url unchanged if the download fails - a poster that
    still hotlinks is better than no poster at all."""
    if not remote_url:
        return remote_url
    try:
        os.makedirs(POSTERS_DIR, exist_ok=True)
        resp = requests.get(remote_url, timeout=20, stream=True)
        resp.raise_for_status()

        ext = os.path.splitext(remote_url.split('?')[0])[1] or '.jpg'
        if len(ext) > 5:  # sanity check against a malformed/absurd extension
            ext = '.jpg'
        filename = f"{show_id}{ext}"
        dest = os.path.join(POSTERS_DIR, filename)

        tmp_dest = dest + '.partial'
        with open(tmp_dest, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        os.replace(tmp_dest, dest)

        return f'/posters/{filename}'
    except (requests.RequestException, OSError) as e:
        log.warning("Could not cache poster locally for show %s (using hotlink instead): %s", show_id, e)
        return remote_url


def delete_cached_poster(poster_url):
    """Removes a locally-cached poster file when a show is unlinked, deleted,
    or re-linked to a different TVDB match, so POSTERS_DIR doesn't
    accumulate orphaned images forever."""
    if not poster_url or not poster_url.startswith('/posters/'):
        return  # not a local file (never cached, or download failed and it's still a hotlink)
    filename = os.path.basename(poster_url)
    path = os.path.join(POSTERS_DIR, filename)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as e:
        log.warning("Could not remove cached poster %s: %s", path, e)


def tvdb_get_series_details(tvdb_id):
    """Fetches poster + season/episode totals for a specific TVDB series id.
    Returns (details: dict|None, error: str|None) where details is
    {'poster_url', 'name', 'season_episode_counts': {season: count}}."""
    token, err = tvdb_get_token()
    if not token:
        return None, err

    try:
        resp = requests.get(
            f'{TVDB_BASE_URL}/series/{tvdb_id}/extended',
            headers={'Authorization': f'Bearer {token}'},
            timeout=15,
        )
        resp.raise_for_status()
        series = resp.json().get('data', {})
    except requests.RequestException as e:
        log.error("TVDB series fetch failed for id %s: %s", tvdb_id, e)
        return None, f"TVDB series fetch failed: {e}"
    except ValueError:
        return None, "TVDB series fetch returned an unexpected response - see logs."

    poster_url = series.get('image') or series.get('poster', '')

    # Episode counts, for cross-checking against what's on disk. Paginated
    # in TVDB's real API; page 0 covers the vast majority of shows, and a
    # failure here shouldn't block getting the poster, which is the more
    # commonly useful half of this feature.
    season_counts = {}
    try:
        ep_resp = requests.get(
            f'{TVDB_BASE_URL}/series/{tvdb_id}/episodes/default',
            headers={'Authorization': f'Bearer {token}'},
            timeout=15,
        )
        ep_resp.raise_for_status()
        episodes = ep_resp.json().get('data', {}).get('episodes', [])
        for ep in episodes:
            season_num = ep.get('seasonNumber')
            if season_num is not None and season_num > 0:  # skip specials (season 0)
                season_counts[season_num] = season_counts.get(season_num, 0) + 1
    except (requests.RequestException, ValueError) as e:
        log.warning("TVDB episode list fetch failed for id %s (poster still usable): %s", tvdb_id, e)

    return {
        'poster_url': poster_url,
        'name': series.get('name', ''),
        'season_episode_counts': season_counts,
        'genres': _extract_tvdb_genres(series),
    }, None


def _extract_tvdb_genres(series_data):
    """Pulls a plain list of genre names out of a TVDB extended-series
    response. Defensive about shape for the same reason as tvdb_search_series
    above (see its docstring, and the note on tvdb_get_token) - this has
    never run against the live API in this sandbox, and TVDB's published
    docs show 'genres' as a list of {'id', 'name'} objects, but a couple of
    real-world API wrappers have reported it flattened to plain strings
    instead, so both shapes are accepted here rather than assuming one."""
    raw = series_data.get('genres') or []
    genres = []
    for g in raw:
        if isinstance(g, dict):
            name = g.get('name')
        else:
            name = g
        if name:
            genres.append(str(name).strip().lower())
    return genres


def tvdb_get_series_genres(tvdb_id):
    """Genres + poster, for recommendation matching and the poster grid on
    the recommendations panel - a single call to the extended-series
    endpoint, skipping the episode-list fetch tvdb_get_series_details also
    does (irrelevant here and roughly doubles the request count when scored
    against a whole pool of candidates). Returns
    (genres: list[str], poster_url: str, error: str|None). An empty genre
    list with no error means TVDB responded but the series has no genres on
    record."""
    token, err = tvdb_get_token()
    if not token:
        return [], '', err

    try:
        resp = requests.get(
            f'{TVDB_BASE_URL}/series/{tvdb_id}/extended',
            headers={'Authorization': f'Bearer {token}'},
            timeout=15,
        )
        resp.raise_for_status()
        series = resp.json().get('data', {})
    except requests.RequestException as e:
        log.error("TVDB genre fetch failed for id %s: %s", tvdb_id, e)
        return [], '', f"TVDB genre fetch failed: {e}"
    except ValueError:
        return [], '', "TVDB genre fetch returned an unexpected response - see logs."

    poster_url = series.get('image') or series.get('poster', '')
    return _extract_tvdb_genres(series), poster_url, None


# Recommendations are matched purely against your own pool - shows already
# sitting on disk, never promoted - never against TVDB's wider catalog, so
# nothing gets suggested that you don't already have. See
# _migrate_v10_genre_recommendation_cache for why there are two caches.
GENRE_CACHE_MAX_AGE_DAYS = 30
POOL_RECOMMENDATION_SCAN_CAP = 40  # TVDB calls per request, for a not-yet-cached pool
GENERIC_GENRE_THRESHOLD = 0.5  # a genre held by more than this fraction of the scanned pool counts for less


def _genre_cache_is_fresh(cached_at_str):
    try:
        cached_at = datetime.fromisoformat(cached_at_str)
    except (TypeError, ValueError):
        return False
    return (now_local() - cached_at) < timedelta(days=GENRE_CACHE_MAX_AGE_DAYS)


def get_genre_data_for_tvdb_id(conn, tvdb_id):
    """Genres + poster for a known TVDB id, cached - shared by an
    already-linked show (its own tvdb_id) and a pool folder once
    resolve_pool_folder_tvdb_id has found one for it. Returns
    {'genres': list[str], 'poster_url': str}."""
    row = conn.execute(
        f"SELECT genres, poster_url, cached_at FROM {TVDB_GENRE_CACHE_TABLE} WHERE tvdb_id = ?", (tvdb_id,)
    ).fetchone()
    if row and _genre_cache_is_fresh(row['cached_at']):
        return {'genres': [g for g in row['genres'].split(',') if g], 'poster_url': row['poster_url'] or ''}

    genres, poster_url, _err = tvdb_get_series_genres(tvdb_id)
    conn.execute(
        f"""INSERT INTO {TVDB_GENRE_CACHE_TABLE} (tvdb_id, genres, poster_url, cached_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(tvdb_id) DO UPDATE SET genres = excluded.genres, poster_url = excluded.poster_url, cached_at = excluded.cached_at""",
        (tvdb_id, ",".join(genres), poster_url, now_local().isoformat())
    )
    conn.commit()
    return {'genres': genres, 'poster_url': poster_url}


_RELEASE_JUNK_RE = re.compile(
    r'\b(1080p|720p|2160p|4k|hdtv|webrip|web-?dl|bluray|brrip|dvdrip|x264|x265|hevc|aac|'
    r'complete|season\s?\d+|s\d{1,2}(e\d{1,3})?)\b', re.IGNORECASE
)


def _normalize_show_name_for_tvdb_search(folder_name):
    """Pool folder names often carry download/release naming, not clean
    show titles ('Show.Name.2019.S01.1080p.WEBRip.x264-GROUP') - searched
    as-is, TVDB frequently finds nothing, which silently shrinks the whole
    recommendation pool down to whichever handful of folders happened to be
    named cleanly. Strips the common junk tokens and normalizes separators
    to spaces; the ORIGINAL folder_name is still what's cached, displayed,
    and queued - only the string sent to TVDB's search changes."""
    name = folder_name.replace('.', ' ').replace('_', ' ')
    name = _RELEASE_JUNK_RE.sub(' ', name)
    name = re.sub(r'-[A-Za-z0-9]+$', ' ', name)  # trailing release-group tag, e.g. "-GROUP"
    name = re.sub(r'\s+', ' ', name).strip()
    return name or folder_name


def resolve_pool_folder_tvdb_id(conn, folder_name):
    """TVDB id for a pool folder name, cached - the fuzzy, search-based half
    of recommendation matching. Returns int|None (None if TVDB has no
    confident match, which is cached too so it isn't re-searched forever)."""
    row = conn.execute(
        f"SELECT tvdb_id, cached_at FROM {POOL_TVDB_CACHE_TABLE} WHERE folder_name = ?", (folder_name,)
    ).fetchone()
    if row and _genre_cache_is_fresh(row['cached_at']):
        return row['tvdb_id']

    results, _err = tvdb_search_series(_normalize_show_name_for_tvdb_search(folder_name))
    tvdb_id = results[0]['tvdb_id'] if results else None
    conn.execute(
        f"""INSERT INTO {POOL_TVDB_CACHE_TABLE} (folder_name, tvdb_id, cached_at) VALUES (?, ?, ?)
            ON CONFLICT(folder_name) DO UPDATE SET tvdb_id = excluded.tvdb_id, cached_at = excluded.cached_at""",
        (folder_name, tvdb_id, now_local().isoformat())
    )
    conn.commit()
    return tvdb_id


def get_recommendations_for_show(show_id):
    """The recommendation logic behind /api/recommendations/<id>: genre-
    overlap matches for show_id, drawn only from POOL_DIR (shows you
    actually have, just not promoted yet - never TVDB's wider catalog).

    Returns a dict: {'status': 'ok', 'anchor_genres': [...],
    'recommendations': [{'name', 'genres', 'matched_genres', 'poster_url'}, ...],
    'scanned': int, 'pool_total': int} on success, or
    {'status': 'no_tvdb_link' | 'no_genres', 'message': ...} when there's
    nothing to match against yet."""
    with closing(get_db()) as conn:
        show = conn.execute(f"SELECT * FROM {TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
        if not show:
            return {'status': 'not_found', 'message': 'Show not found.'}

        tvdb_id = show['tvdb_id'] if 'tvdb_id' in show.keys() else None
        if not tvdb_id:
            return {
                'status': 'no_tvdb_link',
                'message': "use 'link poster' (from the dashboard's more menu, on this show) to get genre-based recommendations.",
            }

        anchor_genres = get_genre_data_for_tvdb_id(conn, tvdb_id)['genres']
        if not anchor_genres:
            return {
                'status': 'no_genres',
                'message': "TVDB doesn't list any genres for this show, so nothing to match against yet.",
            }

        pool_shows = []
        if os.path.exists(POOL_DIR):
            try:
                pool_shows = sorted(
                    d for d in os.listdir(POOL_DIR) if os.path.isdir(os.path.join(POOL_DIR, d))
                )
            except OSError as e:
                log.error("Could not read pool directory for recommendations: %s", e)

        anchor_set = set(anchor_genres)
        candidates = []  # every resolved pool folder, whether or not it matched - needed below to tell a common genre from a rare one
        scanned = 0
        for folder in pool_shows:
            if scanned >= POOL_RECOMMENDATION_SCAN_CAP:
                # Bounds worst-case TVDB call volume on a cold cache for a
                # big pool. Whatever got resolved this call is cached, so a
                # second page load picks up where this one left off and a
                # fully-cached pool never hits this limit at all.
                break
            candidate_tvdb_id = resolve_pool_folder_tvdb_id(conn, folder)
            scanned += 1
            if not candidate_tvdb_id:
                continue
            candidate_data = get_genre_data_for_tvdb_id(conn, candidate_tvdb_id)
            candidates.append({'name': folder, 'genres': candidate_data['genres'], 'poster_url': candidate_data['poster_url']})

        # "drama"/"comedy" sit on nearly every show, so matching on them
        # alone says almost nothing about THIS anchor specifically - without
        # this, the same genre-rich shows would win regardless of what
        # you're viewing, which is exactly what was happening before. A
        # genre counts as "generic" once more than GENERIC_GENRE_THRESHOLD
        # of your resolved pool carries it; generic matches still count,
        # just after every candidate's rarer, more distinguishing matches.
        pool_size = len(candidates) or 1
        genre_pool_counts = {}
        for c in candidates:
            for g in set(c['genres']):
                genre_pool_counts[g] = genre_pool_counts.get(g, 0) + 1
        generic_genres = {g for g, n in genre_pool_counts.items() if (n / pool_size) > GENERIC_GENRE_THRESHOLD}

        scored = []
        for c in candidates:
            matched = anchor_set & set(c['genres'])
            if not matched:
                continue
            specific_matches = sorted(matched - generic_genres)
            generic_matches = sorted(matched & generic_genres)
            scored.append({
                'name': c['name'],
                'genres': sorted(c['genres']),
                'matched_genres': specific_matches + generic_matches,  # rarer matches listed first
                'poster_url': c['poster_url'],
                '_specific_count': len(specific_matches),
                '_generic_count': len(generic_matches),
            })

        scored.sort(key=lambda r: (-r['_specific_count'], -r['_generic_count'], r['name'].lower()))
        for r in scored:
            del r['_specific_count'], r['_generic_count']

        return {
            'status': 'ok',
            'anchor_genres': sorted(anchor_genres),
            'recommendations': scored[:5],
            'scanned': scanned,
            'pool_total': len(pool_shows),
        }


def sonarr_get_calendar(days_ahead=14):
    """Fetches upcoming episode air dates across the user's WHOLE Sonarr
    library (every monitored show, not just ones synced into Unbinge).

    This is a genuinely different thing from Unbinge's own schedule: Unbinge
    projects when files ALREADY ON DISK will drip into Plex on a schedule
    you set; this reports when episodes are actually airing/being released
    according to Sonarr, which may not be downloaded yet at all.

    Returns (events: list[dict], error: str|None). Each event dict has
    'show_name', 'season', 'episode', 'episode_title', 'air_date' (date),
    'air_datetime' (the raw ISO string from Sonarr, or None), 'has_file'.

    Never executed against a live Sonarr instance - written from Sonarr's
    documented v3 API shape, same caveat as sonarr_get_series."""
    base = get_setting('sonarr_url', '').strip().rstrip('/')
    api_key = get_setting('sonarr_api_key', '').strip()
    if not base or not api_key:
        return [], "Sonarr isn't configured - add a URL and API key in Settings to enable this."

    start = today_local()
    end = start + timedelta(days=max(1, days_ahead))

    try:
        resp = requests.get(
            f'{base}/api/v3/calendar',
            params={
                'start': start.isoformat(),
                'end': end.isoformat(),
                'includeSeries': 'true',
            },
            headers={'X-Api-Key': api_key},
            timeout=15,
        )
        resp.raise_for_status()
        raw_episodes = resp.json()
    except requests.RequestException as e:
        log.error("Sonarr calendar fetch failed: %s", e)
        return [], f"Could not reach Sonarr: {e}"
    except ValueError:
        return [], "Sonarr returned an unexpected response - see logs."

    events = []
    for ep in raw_episodes:
        air_dt_str = ep.get('airDateUtc') or ep.get('airDate')
        if not air_dt_str:
            continue
        try:
            # airDateUtc is a full ISO datetime; airDate (fallback) is a bare
            # date. Both parse fine via fromisoformat once 'Z' is normalised.
            air_dt = datetime.fromisoformat(air_dt_str.replace('Z', '+00:00'))
            air_date = air_dt.astimezone(LOCAL_TZ).date() if air_dt.tzinfo else air_dt.date()
        except ValueError:
            continue

        series = ep.get('series') or {}
        events.append({
            'show_name': series.get('title', 'Unknown Show'),
            'season': ep.get('seasonNumber', 0),
            'episode': ep.get('episodeNumber', 0),
            'episode_title': ep.get('title') or '',
            'air_date': air_date,
            'air_datetime': air_dt_str,
            'has_file': bool(ep.get('hasFile')),
        })

    events.sort(key=lambda e: (e['air_date'], e['show_name']))
    return events, None


def sonarr_get_series(query_or_tvdb_id):
    """Optional enrichment - looks up a series in the user's own Sonarr
    instance, by TVDB id if given (more reliable) or by title match.
    Returns (details: dict|None, error: str|None). No-ops cleanly with a
    clear message if Sonarr isn't configured, since this is meant to be
    fully optional on top of the TVDB-based flow above.

    Also never executed against a live Sonarr instance - written from
    Sonarr's documented v3 API shape."""
    base = get_setting('sonarr_url', '').strip().rstrip('/')
    api_key = get_setting('sonarr_api_key', '').strip()
    if not base or not api_key:
        return None, "Sonarr isn't configured (optional) - add a URL and API key in Settings to enable it."

    try:
        resp = requests.get(
            f'{base}/api/v3/series',
            headers={'X-Api-Key': api_key},
            timeout=15,
        )
        resp.raise_for_status()
        all_series = resp.json()
    except requests.RequestException as e:
        log.error("Sonarr lookup failed: %s", e)
        return None, f"Could not reach Sonarr: {e}"
    except ValueError:
        return None, "Sonarr returned an unexpected response - see logs."

    match = None
    if isinstance(query_or_tvdb_id, int):
        match = next((s for s in all_series if s.get('tvdbId') == query_or_tvdb_id), None)
    else:
        q = str(query_or_tvdb_id).strip().lower()
        match = next((s for s in all_series if s.get('title', '').strip().lower() == q), None)

    if not match:
        return None, "No matching show found in Sonarr."

    season_counts = {
        s['seasonNumber']: s.get('statistics', {}).get('totalEpisodeCount', 0)
        for s in match.get('seasons', [])
        if s.get('seasonNumber', 0) > 0
    }
    return {
        'sonarr_id': match.get('id'),
        'title': match.get('title'),
        'season_episode_counts': season_counts,
    }, None


def parse_discord_webhook(url):
    """Extracts (webhook_id, webhook_token) from a Discord webhook URL, or
    None if the URL isn't a recognizable Discord webhook. Needed because
    editing a message (rather than just posting a new one) requires calling
    the Discord API directly with these two values."""
    if not url:
        return None
    match = re.match(r'^https://discord(?:app)?\.com/api/webhooks/(\d+)/([^/?\s]+)', url.strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def build_next_up_events(weeks_ahead=3):
    """Returns just the single next scheduled event (drip/cooldown/graduate)
    for each active show, rather than the full multi-week projection used by
    the /schedule page - this is 'what's coming up next and when' per show.
    A 3-week horizon is plenty: even a show mid-cooldown only needs two more
    release-day occurrences to reach 'graduate', so this avoids computing
    the full remaining-episode projection that project_schedule would do for
    a much longer window (the /schedule page's own default is 6 weeks and is
    unaffected - it's passed explicitly there)."""
    next_by_show = {}
    for ev in project_schedule(weeks_ahead=weeks_ahead):
        if ev['show_name'] not in next_by_show:
            next_by_show[ev['show_name']] = ev
    return sorted(next_by_show.values(), key=lambda e: (e['date'], e['show_name']))


def build_schedule_embed():
    """Builds the Discord embed payload representing the current drip
    schedule - one line per show showing what's dripping next and when."""
    events = build_next_up_events()
    paused_shows = [s['show_name'] for s in load_shows() if s['paused']]

    lines = []
    for ev in events:
        emoji = DISCORD_ACTION_EMOJI.get(ev['action'], '•')
        when = ev['date'].strftime('%a %b %d')
        lines.append(f"{emoji} **{ev['show_name']}** — {ev['detail']} ({when})")
    for name in paused_shows:
        lines.append(f"⏸️ **{name}** — paused")

    description = "\n".join(lines) if lines else "Nothing currently scheduled."

    return {
        "embeds": [{
            "title": "📅 Unbinge Schedule",
            "description": description[:4096],
            "color": 0x5865F2,
            "timestamp": datetime.now(dt_timezone.utc).isoformat(),
            "footer": {"text": "Updates automatically as the schedule changes"},
        }]
    }


def sync_discord_schedule_message():
    """Keeps a single Discord message up to date with the current drip
    schedule, editing it in place rather than posting a new message every
    time something changes. Posts a fresh message (and remembers its id) the
    first time, or if the previously-stored message was deleted out from
    under it.

    Returns a small {'status': 'ok'|'skipped'|'error', 'detail': str} dict
    describing what happened. Internal callers (the drip job, promote/edit/
    delete, etc.) fire this and ignore the return value - a Discord failure
    should never interrupt whatever triggered the sync. The manual
    /api/sync-discord-schedule route uses the return value to give real
    feedback instead of a blind 'success'."""
    webhook_url = get_setting('discord_webhook_url', '').strip()
    parsed = parse_discord_webhook(webhook_url)
    if not parsed:
        if webhook_url:
            detail = "That doesn't look like a Discord webhook URL (expected https://discord.com/api/webhooks/...)."
            log.warning("Discord schedule sync skipped: %s", detail)
            return {'status': 'error', 'detail': detail}
        return {'status': 'skipped', 'detail': 'No Discord webhook URL configured.'}

    webhook_id, webhook_token = parsed
    payload = build_schedule_embed()
    message_id = get_setting('schedule_message_id', '').strip()

    try:
        if message_id:
            resp = requests.patch(
                f"https://discord.com/api/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}",
                json=payload, timeout=10
            )
            if resp.status_code == 404:
                message_id = ''  # stored message was deleted - recreate below
            else:
                resp.raise_for_status()
                return {'status': 'ok', 'detail': 'Existing Discord message updated.'}

        resp = requests.post(
            f"https://discord.com/api/webhooks/{webhook_id}/{webhook_token}",
            params={'wait': 'true'}, json=payload, timeout=10
        )
        resp.raise_for_status()
        new_id = resp.json().get('id')
        if new_id:
            set_setting('schedule_message_id', new_id)
        return {'status': 'ok', 'detail': 'Posted a new Discord message.'}
    except Exception as e:
        log.warning("Discord schedule sync failed (non-fatal): %s", e)
        return {'status': 'error', 'detail': str(e)}


def sync_discord_schedule_message_async():
    """Fire-and-forget wrapper around sync_discord_schedule_message() for use
    inside Flask request handlers (promote/edit/pause/delete/settings-save)
    and app startup, so a slow or unreachable Discord endpoint can't stall
    the HTTP response or the app's boot. Runs on its own daemon thread; any
    error is already caught and logged inside sync_discord_schedule_message
    itself, so there's nothing to propagate back here.

    The scheduled drip job calls sync_discord_schedule_message() directly
    instead - it already runs on the scheduler's own background thread, so
    there's no request to block, and running it inline there keeps it
    simple. The manual 'Sync Now' button also calls the synchronous version
    directly, since that click is explicitly asking to wait for a real
    result."""
    threading.Thread(target=sync_discord_schedule_message, daemon=True).start()


def parse_release_days(raw):
    """Parses a comma-separated day-number string ('0,3') into a sorted list
    of ints. Tolerant of the legacy single-value format."""
    if not raw:
        return []
    try:
        return sorted({int(x) for x in str(raw).split(',') if x.strip() != ''})
    except ValueError:
        return []


def parse_tags(raw):
    """'anime, kids' -> ['anime', 'kids']. Trims whitespace, drops empties,
    de-duplicates case-insensitively while preserving the first casing seen."""
    if not raw:
        return []
    seen, out = set(), []
    for t in raw.split(','):
        t = t.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def format_release_days(raw):
    days = parse_release_days(raw)
    names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
    return ", ".join(names[d] for d in days) if days else "—"


def next_occurrence(from_date, days_list, inclusive=True):
    """Returns the next date >= (or >, if inclusive=False) from_date whose
    weekday is in days_list."""
    if not days_list:
        return None
    for offset in range(0 if inclusive else 1, 8):
        candidate = from_date + timedelta(days=offset)
        if candidate.weekday() in days_list:
            return candidate
    return None


def safe_get(row, key, default=None):
    """Coalesces a NULL column value to a default.

    H4 - this used to also guard against KeyError/IndexError for a column
    that might not exist on the row at all, back when the schema was
    patched together with ad-hoc ALTER TABLE checks. Now that H5/H6 give
    every row a complete, migration-guaranteed set of columns (every call
    site here queries with SELECT *), that guard can never actually fire -
    the column is always present. What's left, and still genuinely useful,
    is defaulting a column that's NULL because it was added via ALTER TABLE
    to a database that already had rows, and nothing ever backfilled it
    (poster_url and tvdb_id on shows added before F3, for example)."""
    val = row[key]
    return val if val is not None else default


def reconcile_show_paths():
    """Self-heal step run on every startup (and available on-demand). For
    each show, checks whether vault_path/plex_path match the current
    convention (VAULT_DIR/PLEX_BASE_DIR + show name). If they don't, but the
    conventional path actually exists on disk, the DB is repointed to it
    automatically. This fixes shows left with stale paths from an older
    version of the app, and makes future config changes (renaming the vault
    or Plex folder, moving to a new drive, etc.) self-correcting on restart
    instead of requiring manual DB surgery per show.

    Returns a list of {show_name, field, old, new} repair records for
    anything that was changed."""
    repairs = []
    conn = get_db()
    try:
        rows = conn.execute(f"SELECT * FROM {TABLE_NAME}").fetchall()
        for row in rows:
            show = dict(row)
            expected_vault = os.path.join(VAULT_DIR, show['show_name'])
            expected_plex = os.path.join(PLEX_BASE_DIR, show['show_name'])
            updates = {}

            if show['vault_path'] != expected_vault:
                if os.path.isdir(expected_vault):
                    updates['vault_path'] = expected_vault
                elif not os.path.isdir(show['vault_path']):
                    log.warning(
                        "'%s': neither its recorded vault path (%s) nor the "
                        "expected path (%s) exist on disk - needs manual attention.",
                        show['show_name'], show['vault_path'], expected_vault
                    )

            if show['plex_path'] != expected_plex:
                if os.path.isdir(expected_plex):
                    updates['plex_path'] = expected_plex
                elif not os.path.isdir(show['plex_path']):
                    log.warning(
                        "'%s': neither its recorded Plex path (%s) nor the "
                        "expected path (%s) exist on disk - needs manual attention.",
                        show['show_name'], show['plex_path'], expected_plex
                    )

            if updates:
                for field, new_val in updates.items():
                    repairs.append({'show_name': show['show_name'], 'field': field, 'old': show[field], 'new': new_val})
                set_clause = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(f"UPDATE {TABLE_NAME} SET {set_clause} WHERE id = ?", (*updates.values(), show['id']))
                log.info("Repaired path(s) for '%s': %s", show['show_name'], updates)

        if repairs:
            conn.commit()
    finally:
        conn.close()
    return repairs


def load_shows(sort='name'):
    """Reads active show records from the SQLite database.

    sort controls ordering:
      'name'   - alphabetical (default - unchanged behavior for existing
                 callers like project_schedule/build_schedule_embed, which
                 don't care about order).
      'day'    - by earliest configured release weekday, Monday first
                 through Sunday last (a show with multiple release days
                 sorts by whichever comes first in the week).
      'status' - Active shows first, then those in cooldown, then paused.
    Ties always fall back to show name."""
    try:
        conn = get_db()
        rows = conn.execute(f"SELECT * FROM {TABLE_NAME} ORDER BY show_name COLLATE NOCASE").fetchall()
        conn.close()

        shows_list = []
        for r in rows:
            release_days_raw = safe_get(r, 'release_days', None) or str(safe_get(r, 'release_day', '0'))
            show_id = safe_get(r, 'id', 0)

            # U3/U4 - "Episode 20 of 22 - 2 left" is far more useful than the
            # bare "Season 01 - Episode 20" the dashboard showed before, which
            # also displayed as the confusing "Episode 00" for any show that
            # hadn't started yet. Cheap enough for a handful of shows; if this
            # library grows into the hundreds, cache alongside the fix for
            # R8 (project_schedule's per-show os.walk on every page load),
            # since this adds a second walk of the same shape.
            with closing(get_db()) as ep_conn:
                dripped = get_dripped_set(ep_conn, show_id)
                excluded = get_excluded_set(ep_conn, show_id)
            vault_path = safe_get(r, 'vault_path', '')
            remaining_tags = {
                (e['season'], e['episode']) for e in remaining_episode_files(vault_path, dripped, excluded)
            }
            episodes_dripped = len(dripped)
            episodes_remaining = len(remaining_tags)

            shows_list.append({
                'id': show_id,
                'show_name': safe_get(r, 'show_name', 'Unknown'),
                'vault_path': vault_path,
                'plex_path': safe_get(r, 'plex_path', ''),
                'release_day': str(safe_get(r, 'release_day', '0')),
                'release_days': release_days_raw,
                'release_days_display': format_release_days(release_days_raw),
                'current_season': safe_get(r, 'current_season', 1),
                'current_episode': safe_get(r, 'current_episode', 0),
                'episodes_per_drop': safe_get(r, 'episodes_per_drop', 1),
                'episodes_dripped': episodes_dripped,
                'episodes_remaining': episodes_remaining,
                'episodes_total': episodes_dripped + episodes_remaining,
                'poster_url': safe_get(r, 'poster_url', None),
                'tags': parse_tags(safe_get(r, 'tags', '')),
                'tvdb_id': safe_get(r, 'tvdb_id', None),
                'completed_at': safe_get(r, 'completed_at', None),
                'paused': bool(safe_get(r, 'paused', 0)),
            })

        if sort == 'day':
            def day_key(s):
                days = parse_release_days(s['release_days'])
                return (min(days) if days else 7, s['show_name'].lower())
            shows_list.sort(key=day_key)
        elif sort == 'status':
            def status_rank(s):
                if s['paused']:
                    return 2
                if s['completed_at']:
                    return 1
                return 0
            shows_list.sort(key=lambda s: (status_rank(s), s['show_name'].lower()))
        # 'name' (and any unrecognized value) keeps the alphabetical order
        # the SQL query above already produced.

        return shows_list
    except Exception as e:
        log.error("Exception while loading shows: %s", e)
        return []


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------
def scan_episode_files(root_dir):
    """Recursively walks root_dir and returns every file whose name matches
    an SxxExx pattern, sorted by (season, episode, relative path).
    Any file sharing an episode's SxxExx tag (subtitles, nfo, etc.) is
    included automatically so it travels with its video file."""
    results = []
    if not os.path.isdir(root_dir):
        return results

    for dirpath, _dirs, filenames in os.walk(root_dir):
        for fname in filenames:
            tags = extract_episode_tags(fname)
            if not tags:
                continue
            abs_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(abs_path, root_dir)
            # One entry per tag, not per file. A double-episode file produces
            # two entries sharing the same abs_path/rel_path - group_by_file
            # (used by compute_next_batch) is what recombines them into a
            # single move when it matters, but every OTHER consumer of this
            # list (api_inspect's counts, the dripped-episode set, the
            # schedule projection) wants one row per episode regardless of
            # how many episodes share a file, so the per-tag shape is kept
            # here rather than nested.
            for season, episode in tags:
                results.append({
                    'season': season,
                    'episode': episode,
                    'abs_path': abs_path,
                    'rel_path': rel_path,
                })

    results.sort(key=lambda e: (e['season'], e['episode'], e['rel_path']))
    return results


VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.m4v', '.wmv', '.ts', '.webm'}


def find_unmatched_files(root_dir):
    """Video files in root_dir that extract_episode_tags() can't parse at
    all - Sonarr's 'Manual Import' problem. These are invisible to the
    whole drip pipeline today: not counted, not shown, not moved, just
    silently ignored. Only files with a recognized video extension are
    considered, so posters/nfo/etc. in the same folder aren't flagged as
    'unmatched episodes' when they were never meant to be one."""
    unmatched = []
    if not os.path.isdir(root_dir):
        return unmatched
    for dirpath, _dirs, filenames in os.walk(root_dir):
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in VIDEO_EXTENSIONS:
                continue
            if extract_episode_tags(fname):
                continue
            abs_path = os.path.join(dirpath, fname)
            unmatched.append({'rel_path': os.path.relpath(abs_path, root_dir), 'abs_path': abs_path})
    return unmatched


def manually_match_episode(abs_path, season, episode):
    """Renames a file to insert a standard SxxExx tag, so normal scanning
    picks it up from then on. This is Sonarr's Manual Import, done via
    rename rather than a permanent override table - once the filename
    itself carries the tag, every existing function (scan_episode_files,
    compute_next_batch, the episode grid) already understands it with no
    special-casing needed anywhere else.

    Inserts the tag before the extension: 'Some File.mkv' becomes
    'Some File - S02E07.mkv'. Returns the new absolute path."""
    directory, filename = os.path.split(abs_path)
    name, ext = os.path.splitext(filename)
    new_filename = f"{name} - S{season:02d}E{episode:02d}{ext}"
    new_path = os.path.join(directory, new_filename)
    if os.path.exists(new_path):
        raise FileExistsError(f"'{new_filename}' already exists in that folder")
    os.rename(abs_path, new_path)
    return new_path


def move_file_preserving_structure(abs_src, rel_path, dest_root):
    """Moves a single file into dest_root, preserving its relative
    subfolder structure (e.g. 'Season 01/...')."""
    dest_path = os.path.join(dest_root, rel_path)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    shutil.move(abs_src, dest_path)
    return dest_path


def copy_sibling_metadata(src_dir, dest_dir):
    """Copies non-episode files (posters, nfo, fanart, .plexmatch, etc.) that
    live alongside episode files in src_dir into dest_dir, so Plex has proper
    artwork/metadata for a show or season as soon as its first episode drips.
    Copies rather than moves (cheap, idempotent, skip-if-already-present) so
    re-running this for later episodes in the same folder is harmless."""
    if not os.path.isdir(src_dir):
        return
    os.makedirs(dest_dir, exist_ok=True)
    for fname in os.listdir(src_dir):
        src_path = os.path.join(src_dir, fname)
        if not os.path.isfile(src_path):
            continue
        if extract_episode_tags(fname):
            continue  # episode files are handled by the main move step
        dest_path = os.path.join(dest_dir, fname)
        if os.path.exists(dest_path):
            continue
        try:
            shutil.copy2(src_path, dest_path)
        except Exception as e:
            log.warning("Could not copy metadata file '%s': %s", src_path, e)


def remove_empty_dirs(root_dir):
    """Cleans up now-empty directories left behind after files are moved out."""
    if not os.path.isdir(root_dir):
        return
    for dirpath, dirnames, filenames in os.walk(root_dir, topdown=False):
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass


def format_bytes_human(n):
    """1234567 -> '1.2 MB'. Used in disk-space warnings and the config
    backup download name."""
    if n is None:
        return 'unknown'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != 'B' else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def count_files(root_dir):
    """Total file count under root_dir, or 0 if it doesn't exist. Used to
    record in history how much was actually moved by a delete or graduation,
    so the log says 'returned 14 file(s)' rather than just 'deleted'."""
    if not os.path.isdir(root_dir):
        return 0
    return sum(len(files) for _dir, _subdirs, files in os.walk(root_dir))


def run_migrations(conn):
    """Sequential, idempotent schema migrations keyed off a stored version.

    Replaces the old approach of inspecting PRAGMA table_info and guessing.
    That was fine for adding columns; it can't express a data migration like
    the one below."""
    current = int(get_setting_conn(conn, 'schema_version', '0') or 0)
    if current >= SCHEMA_VERSION:
        return

    if current < 2:
        _migrate_v2_seed_dripped_episodes(conn)

    if current < 3:
        _migrate_v3_unique_show_name(conn)

    if current < 4:
        _migrate_v4_job_runs_table(conn)

    if current < 5:
        _migrate_v5_drip_moves_table(conn)

    if current < 6:
        _migrate_v6_tvdb_columns(conn)

    if current < 7:
        _migrate_v7_excluded_episodes_table(conn)

    if current < 8:
        _migrate_v8_show_queue_table(conn)

    if current < 9:
        _migrate_v9_tags_column(conn)

    if current < 10:
        _migrate_v10_genre_recommendation_cache(conn)

    if current < 11:
        _migrate_v11_recommendation_posters(conn)

    if current < 12:
        _migrate_v12_clear_poster_cache(conn)

    if current < 13:
        _migrate_v13_retry_pool_tvdb_matches(conn)

    conn.execute(
        f"INSERT INTO {SETTINGS_TABLE} (key, value) VALUES ('schema_version', ?) "
        f"ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),)
    )
    conn.commit()
    log.info("Schema migrated to version %d.", SCHEMA_VERSION)


def _migrate_v2_seed_dripped_episodes(conn):
    """Seeds dripped_episodes for shows that predate the table.

    Seeded by SCANNING plex_path - whatever is actually sitting in the Plex
    folder is what was actually dripped. The alternative was to trust the old
    (current_season, current_episode) pointer, but the entire reason for this
    migration is that the pointer could never be trusted: it can't represent
    gaps, and it silently excluded anything that arrived below it.

    Scanning self-corrects that drift. The trade-off is that an episode moved
    or deleted by hand becomes eligible to drip again, which is the safer
    direction to be wrong in - a re-drip is visible and harmless, a permanently
    skipped episode is neither."""
    rows = conn.execute(f"SELECT * FROM {TABLE_NAME}").fetchall()
    if not rows:
        return

    log.info("Migrating %d show(s) to episode-set tracking...", len(rows))
    for row in rows:
        show = dict(row)
        found = scan_episode_files(show['plex_path'])
        tags = sorted({(e['season'], e['episode']) for e in found})

        if not tags:
            log.info(
                "  '%s': nothing found in %s - starting with an empty dripped set.",
                show['show_name'], show['plex_path']
            )
            continue

        stamp = now_local().isoformat()
        conn.executemany(
            f"INSERT OR IGNORE INTO {DRIPPED_TABLE} (show_id, season, episode, dripped_at) "
            f"VALUES (?, ?, ?, ?)",
            [(show['id'], s, e, stamp) for s, e in tags]
        )

        old_pointer = (show.get('current_season'), show.get('current_episode'))
        highest = max(tags)
        note = "" if highest == old_pointer else f" (old pointer said S{old_pointer[0]:02d}E{old_pointer[1]:02d})"
        log.info(
            "  '%s': seeded %d episode(s) from disk, highest S%02dE%02d%s",
            show['show_name'], len(tags), highest[0], highest[1], note
        )
    conn.commit()


def _migrate_v3_unique_show_name(conn):
    """C10 - enforces uniqueness on show_name at the database level.

    promote() already checked for an existing name before inserting, but
    save_edit() didn't, so a rename could still produce a duplicate. Two
    shows with the same name then collided in next_up_map on the dashboard,
    which is keyed by name. A unique index closes the gap regardless of
    which code path writes to the table - including any future one.

    Skipped, with a warning, if duplicates already exist - creating the index
    would simply fail, and silently doing nothing would hide that the data
    needs manual attention first."""
    dupes = conn.execute(f"""
        SELECT show_name, COUNT(*) c FROM {TABLE_NAME}
        GROUP BY show_name HAVING c > 1
    """).fetchall()
    if dupes:
        names = ", ".join(f"'{d['show_name']}' (x{d['c']})" for d in dupes)
        log.warning(
            "Skipping unique show_name migration - duplicates already exist: %s. "
            "Rename or remove one of each pair, then restart to apply the constraint.",
            names
        )
        return
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS idx_shows_name_unique ON {TABLE_NAME}(show_name);"
    )
    log.info("Added unique constraint on show_name.")


def _migrate_v4_job_runs_table(conn):
    """R3/U11 - adds a table recording each scheduled or manual drip run,
    so the dashboard can show 'last run: 6 shows dripped, 1 failed' instead
    of nothing at all. Previously a failed job was only visible in the
    container logs."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {JOB_RUNS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            forced_show_id INTEGER,
            ran_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            detail TEXT
        );
    """)
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_job_runs_started ON {JOB_RUNS_TABLE}(started_at DESC);"
    )
    log.info("Added job_runs table.")


def _migrate_v5_drip_moves_table(conn):
    """F2 - undo support. The history table always stored a human-readable
    label ('dripped S01E01') but never the actual source/destination paths,
    so reverting a drip meant nothing to automate against. This records
    exactly what moved on each drip, keyed to the history row that
    describes it, so the paths can be walked backwards."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {DRIP_MOVES_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            history_id INTEGER NOT NULL,
            show_id INTEGER NOT NULL,
            season INTEGER NOT NULL,
            episode INTEGER NOT NULL,
            src_path TEXT NOT NULL,
            dest_path TEXT NOT NULL,
            undone INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_drip_moves_history ON {DRIP_MOVES_TABLE}(history_id);"
    )
    log.info("Added drip_moves table.")


def _migrate_v6_tvdb_columns(conn):
    """F3 - adds tvdb_id so a show's TVDB match is remembered (not
    re-searched by name every time, which is unreliable for common titles
    and wastes API calls TVDB explicitly asks integrators to avoid)."""
    existing_cols = {r['name'] for r in conn.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()}
    if 'tvdb_id' not in existing_cols:
        conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN tvdb_id INTEGER;")
        log.info("Added tvdb_id column.")


def _migrate_v7_excluded_episodes_table(conn):
    """Per-episode exclude, Sonarr's 'unmonitor' equivalent - lets a specific
    episode be permanently skipped (a clip-show, a recap episode, whatever)
    without it counting against the show's remaining/progress numbers
    forever."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {EXCLUDED_TABLE} (
            show_id INTEGER NOT NULL,
            season INTEGER NOT NULL,
            episode INTEGER NOT NULL,
            excluded_at TEXT NOT NULL,
            PRIMARY KEY (show_id, season, episode)
        );
    """)
    log.info("Added excluded_episodes table.")


def _migrate_v8_show_queue_table(conn):
    """Show succession queue - a pool show can be queued to auto-promote
    either when another (currently active) show finishes its run, or on a
    specific date. Built for the common case of an 8-episode-season show
    needing a replacement lined up for its release-day slot without manual
    re-promoting every few weeks."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {QUEUE_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            show_name TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            trigger_show_id INTEGER,
            trigger_date TEXT,
            release_days TEXT NOT NULL,
            episodes_per_drop INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            triggered_at TEXT
        );
    """)
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_queue_pending ON {QUEUE_TABLE}(triggered_at);"
    )
    log.info("Added show_queue table.")


def _migrate_v9_tags_column(conn):
    """Freeform tags per show, for organizing/filtering a larger library -
    'anime', 'kids', whatever grouping is useful. Stored as a simple
    comma-separated string rather than a separate table; tags here don't
    need their own identity (no rename-cascade, no shared metadata), so a
    join table would be unjustified complexity for what's just a filter
    label."""
    existing_cols = {r['name'] for r in conn.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()}
    if 'tags' not in existing_cols:
        conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN tags TEXT DEFAULT '';")
        log.info("Added tags column.")


def _migrate_v10_genre_recommendation_cache(conn):
    """"What should I queue next?" recommendations, matched by TVDB genre
    against the show that's finishing. Two small caches rather than one:

    - pool_tvdb_cache resolves a POOL FOLDER NAME (a show sitting on disk,
      never promoted, with no row of its own anywhere) to the TVDB id TVDB's
      search matched it to - this is the expensive, fuzzy part (one TVDB
      search call per folder name), so it's only ever done once per folder.
    - tvdb_genre_cache resolves a TVDB ID to its genre list. Kept separate
      from the above because a show's genres are looked up the same way
      whether the id came from a pool-folder search OR from a show that's
      already TVDB-linked (which has its tvdb_id already, no search
      needed) - one cache serves both instead of duplicating genre data
      under two different keys for the same series.

    Both cache "no match found" the same as a real result (tvdb_id NULL,
    or genres '') rather than leaving the row out entirely - otherwise a
    show TVDB genuinely doesn't recognize would get re-searched on every
    single recommendation request forever."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {POOL_TVDB_CACHE_TABLE} (
            folder_name TEXT PRIMARY KEY,
            tvdb_id INTEGER,
            cached_at TEXT NOT NULL
        );
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TVDB_GENRE_CACHE_TABLE} (
            tvdb_id INTEGER PRIMARY KEY,
            genres TEXT NOT NULL DEFAULT '',
            poster_url TEXT DEFAULT '',
            cached_at TEXT NOT NULL
        );
    """)
    log.info("Added pool_tvdb_cache and tvdb_genre_cache tables.")


def _migrate_v11_recommendation_posters(conn):
    """Adds poster_url to tvdb_genre_cache for anyone who already ran the v10
    migration before this column existed - v10's CREATE TABLE IF NOT EXISTS
    is a no-op on an install that already has the table, so a plain ALTER is
    needed to backfill the column there. New installs get it straight from
    v10's CREATE TABLE above and just skip this.

    Rows written before this column existed keep their original cached_at,
    which get_genre_data_for_tvdb_id treats as "fresh" for 30 days - so
    without clearing them out here, every show genre-matched before this
    update would show a blank poster for a month before ever being
    refetched with poster support. It's only a cache, so wiping it is free:
    the next recommendations request just repopulates it, same as a cold
    cache always has."""
    existing_cols = {r['name'] for r in conn.execute(f"PRAGMA table_info({TVDB_GENRE_CACHE_TABLE})").fetchall()}
    if 'poster_url' not in existing_cols:
        conn.execute(f"ALTER TABLE {TVDB_GENRE_CACHE_TABLE} ADD COLUMN poster_url TEXT DEFAULT '';")
        conn.execute(f"DELETE FROM {TVDB_GENRE_CACHE_TABLE};")
        log.info("Added poster_url column to tvdb_genre_cache and cleared existing (poster-less) cache rows.")


def _migrate_v12_clear_poster_cache(conn):
    """Catches installs that already ran v11 while it still had the bug: the
    ALTER there added poster_url but left cached_at untouched on existing
    rows, so get_genre_data_for_tvdb_id kept treating them as fresh and
    never went back to TVDB to actually fill in a poster - for up to 30
    days, until that cache naturally expired. Unconditional and idempotent:
    on an install that already got a clean v11 (or is brand new), this just
    clears an already-empty or already-correct table, which costs
    nothing."""
    conn.execute(f"DELETE FROM {TVDB_GENRE_CACHE_TABLE};")
    log.info("Cleared tvdb_genre_cache so posters get refetched.")


def _migrate_v13_retry_pool_tvdb_matches(conn):
    """resolve_pool_folder_tvdb_id now searches TVDB with release-name junk
    (1080p, x264, S01, etc.) stripped out instead of the raw folder name,
    which should let a lot more of a real download library actually match
    something on TVDB. Anything already cached in pool_tvdb_cache - match
    or "no match" alike - was resolved with the OLD, unstripped search, so
    it's cleared here to get a fair second attempt with the better query.
    Just a cache; the next recommendations request repopulates it."""
    conn.execute(f"DELETE FROM {POOL_TVDB_CACHE_TABLE};")
    log.info("Cleared pool_tvdb_cache so pool folders get re-matched with cleaned-up search terms.")


def get_setting_conn(conn, key, default=''):
    """get_setting against an existing connection, for use inside migrations
    that are already holding one."""
    try:
        row = conn.execute(f"SELECT value FROM {SETTINGS_TABLE} WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    if row is None or row['value'] is None:
        return default
    return row['value']


def get_dripped_set(conn, show_id):
    """The set of (season, episode) tuples already dripped for this show."""
    rows = conn.execute(
        f"SELECT season, episode FROM {DRIPPED_TABLE} WHERE show_id = ?", (show_id,)
    ).fetchall()
    return {(r['season'], r['episode']) for r in rows}


def mark_dripped(conn, show_id, tags):
    stamp = now_local().isoformat()
    conn.executemany(
        f"INSERT OR IGNORE INTO {DRIPPED_TABLE} (show_id, season, episode, dripped_at) "
        f"VALUES (?, ?, ?, ?)",
        [(show_id, s, e, stamp) for s, e in tags]
    )


def clear_dripped(conn, show_id):
    conn.execute(f"DELETE FROM {DRIPPED_TABLE} WHERE show_id = ?", (show_id,))
    conn.execute(f"DELETE FROM {EXCLUDED_TABLE} WHERE show_id = ?", (show_id,))


def is_special(season):
    return season == SPECIALS_SEASON


def get_excluded_set(conn, show_id):
    """The set of (season, episode) tuples permanently excluded for this
    show - Sonarr's 'unmonitor' equivalent. An excluded episode is treated
    exactly like a special: never chosen for a batch, never counted as
    remaining, so a clip-show or recap episode you don't want delivered
    can't block the season's progress count forever."""
    rows = conn.execute(
        f"SELECT season, episode FROM {EXCLUDED_TABLE} WHERE show_id = ?", (show_id,)
    ).fetchall()
    return {(r['season'], r['episode']) for r in rows}


def exclude_episode(conn, show_id, season, episode):
    conn.execute(
        f"INSERT OR IGNORE INTO {EXCLUDED_TABLE} (show_id, season, episode, excluded_at) VALUES (?, ?, ?, ?)",
        (show_id, season, episode, now_local().isoformat())
    )


def unexclude_episode(conn, show_id, season, episode):
    conn.execute(
        f"DELETE FROM {EXCLUDED_TABLE} WHERE show_id = ? AND season = ? AND episode = ?",
        (show_id, season, episode)
    )


def remaining_episode_files(vault_path, dripped, excluded=frozenset()):
    """Files in the vault still waiting to drip.

    Replaces the old `(season, episode) > (current_season, current_episode)`
    comparison. Membership in a set has no ordering, so an episode arriving
    below whatever dripped most recently is treated exactly like any other
    undripped episode.

    Specials and manually-excluded episodes are filtered out here, which
    covers every caller at once: neither is ever chosen for a batch, and
    neither ever counts as "something remaining", so either kind can't hold
    a finished show open forever."""
    return [
        e for e in scan_episode_files(vault_path)
        if not is_special(e['season'])
        and (e['season'], e['episode']) not in dripped
        and (e['season'], e['episode']) not in excluded
    ]


def distinct_episode_tags(episode_files):
    """Ordered, de-duplicated (season, episode) tags. An episode may be several
    files - video plus subtitles - which must move together and count once."""
    seen = []
    for e in episode_files:
        key = (e['season'], e['episode'])
        if key not in seen:
            seen.append(key)
    return seen


def group_by_file(entries):
    """Recombines scan_episode_files' one-entry-per-tag output back into one
    unit per physical file.

    A double-episode file like S01E01E02.mkv produces two entries sharing the
    same abs_path. Grouping them is what lets batch selection treat "deliver
    episode 1" and "deliver episode 2" as the single move they actually are -
    you cannot move half a combined file, so choosing either of its tags has
    to pull in both."""
    by_path = {}
    order = []
    for e in entries:
        key = e['abs_path']
        if key not in by_path:
            by_path[key] = {'abs_path': e['abs_path'], 'rel_path': e['rel_path'], 'tags': []}
            order.append(key)
        by_path[key]['tags'].append((e['season'], e['episode']))

    units = []
    for key in order:
        unit = by_path[key]
        unit['tags'] = sorted(set(unit['tags']))
        units.append(unit)
    units.sort(key=lambda u: (u['tags'][0], u['rel_path']))
    return units


def compute_next_batch(vault_path, dripped, episodes_per_drop, excluded=frozenset()):
    """The single source of truth for 'what drips next'.

    process_show_drip and preview_show_drip both call this. They used to
    duplicate the logic, which meant the Preview button could drift out of
    step with the real run and quietly start lying.

    Returns (target_eps, batch): target_eps is every (season, episode) tag
    committed this round, batch is the list of physical-file units to move.
    For an ordinary show, episodes_per_drop=N means exactly N units each
    contributing one new tag - identical to the old behaviour. A combined
    episode file can overshoot the requested count by one file's worth of
    extra episodes, which is unavoidable: delivering S01E01 from an
    S01E01E02.mkv delivers E02 in the same breath, whether asked for or not."""
    remaining = remaining_episode_files(vault_path, dripped, excluded)
    if not remaining:
        return [], []

    units = group_by_file(remaining)
    target = max(1, episodes_per_drop)

    # Phase 1 - decide which TAGS are committed this round. Walking units in
    # order and pulling in a whole combo file's tags at once is what makes
    # choosing S01E01 from an S01E01E02.mkv also commit E02, even though
    # that's one file contributing two tags instead of the usual one.
    committed_tags = set()
    for unit in units:
        if len(committed_tags) >= target:
            break
        new_tags = [t for t in unit['tags'] if t not in committed_tags]
        if not new_tags:
            continue
        committed_tags.update(unit['tags'])

    # Phase 2 - batch is every unit that delivers ANY committed tag, not just
    # the first one that introduced it. This is what pulls a subtitle file
    # along with its video: they're separate files (separate units) sharing
    # one tag, and the video alone reaching phase 1 first must not leave the
    # subtitle behind. Doing this as one pass (deciding AND collecting
    # together) was the bug - it skipped exactly this case.
    batch = [u for u in units if set(u['tags']) & committed_tags]

    return sorted(committed_tags), batch


CONFLICTS_DIRNAME = '_unbinge_conflicts'


def files_look_identical(path_a, path_b):
    """Cheap duplicate check: same size and same mtime to the second.

    Deliberately not a hash - these are multi-gigabyte video files on a
    network-backed mount, and hashing them would take minutes per graduation.
    Size plus mtime is what every sync tool uses for the same reason, and the
    consequence of a false negative here is a file parked in _unbinge_conflicts
    rather than anything being lost."""
    try:
        sa, sb = os.stat(path_a), os.stat(path_b)
    except OSError:
        return False
    return sa.st_size == sb.st_size and int(sa.st_mtime) == int(sb.st_mtime)


def merge_directory(src_dir, dest_dir):
    """Moves every file from src_dir into dest_dir, merging rather than
    overwriting, preserving subfolder structure, then removes the emptied-out
    src_dir.

    Returns {'moved': int, 'duplicates': int, 'conflicts': [rel_path],
             'failures': [(rel_path, error)]}.

    Two behaviours changed here (C3, C4).

    C3 - collisions are no longer deleted. The previous version called
    os.remove() on the source whenever the destination name already existed,
    with no comparison and no log line. That's correct for the artwork case it
    was written for (copied, not moved, so genuinely duplicated) but it also
    silently destroyed any genuinely different file with a matching name. Now
    identical files are dropped as before, and differing ones are parked in a
    _unbinge_conflicts folder with a loud warning.

    C4 - every file operation is wrapped. On a Windows/NTFS bind mount Plex
    takes mandatory locks while scanning or streaming, so a move can fail in
    ways it never would on Linux. Previously one locked file aborted the whole
    merge partway, leaving a show split across two directories with no record
    of how far it got. Now failures are collected and the rest of the merge
    continues, and src_dir is left in place so nothing is stranded."""
    report = {'moved': 0, 'duplicates': 0, 'conflicts': [], 'failures': []}
    if not os.path.isdir(src_dir):
        return report

    os.makedirs(dest_dir, exist_ok=True)
    for dirpath, _dirs, filenames in os.walk(src_dir):
        for fname in filenames:
            abs_src = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(abs_src, src_dir)
            dest_path = os.path.join(dest_dir, rel_path)

            try:
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)

                if os.path.exists(dest_path):
                    if files_look_identical(abs_src, dest_path):
                        # A true duplicate - the artwork case this branch was
                        # originally written for. Safe to drop.
                        os.remove(abs_src)
                        report['duplicates'] += 1
                        continue

                    # Different content, same name. Never destroy it.
                    conflict_path = os.path.join(dest_dir, CONFLICTS_DIRNAME, rel_path)
                    os.makedirs(os.path.dirname(conflict_path), exist_ok=True)
                    shutil.move(abs_src, conflict_path)
                    report['conflicts'].append(rel_path)
                    log.warning(
                        "Filename collision on '%s': destination copy kept, source "
                        "moved to %s (sizes differ, so this is not a duplicate).",
                        rel_path, os.path.join(CONFLICTS_DIRNAME, rel_path)
                    )
                    continue

                shutil.move(abs_src, dest_path)
                report['moved'] += 1

            except OSError as e:
                # Almost always a Windows file lock held by Plex.
                report['failures'].append((rel_path, str(e)))
                log.error("Could not move '%s' out of %s: %s", rel_path, src_dir, e)

    if report['failures']:
        log.error(
            "%d file(s) could not be moved out of %s - leaving the folder in "
            "place rather than half-emptying it. This is usually Plex holding "
            "a file open; retry once it has finished scanning.",
            len(report['failures']), src_dir
        )
        return report

    remove_empty_dirs(src_dir)
    try:
        os.rmdir(src_dir)
    except OSError:
        pass
    return report


def trigger_plex_refresh():
    """Best-effort Plex library scan. Never lets a Plex failure interrupt
    the file-moving logic that already succeeded."""
    if not PLEX_URL or not PLEX_TOKEN:
        return
    try:
        from plexapi.server import PlexServer
        plex = PlexServer(PLEX_URL, PLEX_TOKEN)
        if PLEX_LIBRARY_ID:
            section = plex.library.sectionByID(int(PLEX_LIBRARY_ID))
            section.update()
        else:
            plex.library.update()
        log.info("Triggered Plex library refresh.")
    except Exception as e:
        log.warning("Plex refresh failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Drip logic
# ---------------------------------------------------------------------------
def ics_escape(text):
    """Escapes text per RFC 5545 section 3.3.11 - backslash, semicolon,
    comma, and newline all need escaping inside an iCalendar text value.
    Order matters: backslash must be escaped first, or escaping the other
    characters would double-escape the backslashes just added."""
    text = str(text or '')
    text = text.replace('\\', '\\\\')
    text = text.replace(';', '\\;')
    text = text.replace(',', '\\,')
    text = text.replace('\n', '\\n')
    return text


def ics_uid_slug(text):
    """Reduces text to a UID-safe token: alphanumerics only, everything
    else collapsed to a hyphen. UIDs just need to be opaque and unique, not
    human-readable, and keeping them free of spaces/punctuation avoids
    relying on any calendar client's tolerance for unusual characters in
    what's meant to be a simple identifier."""
    return re.sub(r'[^A-Za-z0-9]+', '-', str(text or '')).strip('-').lower() or 'show'


def ics_fold_line(line):
    """RFC 5545 requires lines longer than 75 OCTETS to be 'folded' with a
    CRLF followed by a single leading space. The limit is bytes, not
    characters - a naive char-count slice undercounts anything containing a
    multi-byte UTF-8 character (the 📺/📡 emoji used in event titles are 4
    bytes each), which let lines slip through at or just under 75 characters
    while actually exceeding 75 bytes. Folds on a real byte boundary,
    backing off if a slice would land inside a multi-byte sequence rather
    than corrupting the character."""
    encoded = line.encode('utf-8')
    if len(encoded) <= 75:
        return line

    def safe_slice(data, limit):
        """The largest prefix of `data` that's <=limit bytes and decodes
        cleanly - i.e. doesn't end mid-character."""
        cut = min(limit, len(data))
        while cut > 0:
            try:
                data[:cut].decode('utf-8')
                return cut
            except UnicodeDecodeError:
                cut -= 1
        return 0

    parts = []
    first_cut = safe_slice(encoded, 75)
    parts.append(encoded[:first_cut].decode('utf-8'))
    rest = encoded[first_cut:]
    while rest:
        cut = safe_slice(rest, 74)  # 74, leaving room for the leading space
        parts.append(' ' + rest[:cut].decode('utf-8'))
        rest = rest[cut:]
    return '\r\n'.join(parts)


def build_ical_feed():
    """Builds the combined .ics calendar: Unbinge's own projected
    drip/cooldown/graduate schedule, plus - if Sonarr is configured - real
    upcoming air dates across the whole Sonarr library. Two clearly
    different event types in one feed, since Google Calendar subscribes to
    one URL and the two are more useful seen together than as separate
    subscriptions to manage.

    Drip-type events get a real time (the configured drip hour/minute) since
    that's an actual scheduled moment; Sonarr air-date events use whatever
    time Sonarr reports, falling back to an all-day event if only a bare
    date is available."""
    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//Unbinge//Schedule//EN',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        'X-WR-CALNAME:Unbinge Schedule',
        'REFRESH-INTERVAL;VALUE=DURATION:PT12H',
    ]

    now_stamp = datetime.now(dt_timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    drip_hour = int(get_setting('drip_hour', '3') or 3)
    drip_minute = int(get_setting('drip_minute', '0') or 0)

    action_labels = {'drip': 'Drip', 'cooldown': 'Cooldown', 'graduate': 'Back in library'}
    for i, ev in enumerate(project_schedule(weeks_ahead=8)):
        start_dt = datetime.combine(ev['date'], datetime.min.time(), tzinfo=LOCAL_TZ)
        start_dt = start_dt.replace(hour=drip_hour, minute=drip_minute)
        end_dt = start_dt + timedelta(minutes=30)
        label = action_labels.get(ev['action'], ev['action'].title())

        lines += [
            'BEGIN:VEVENT',
            f'UID:unbinge-{i}-{ev["date"].isoformat()}-{ics_uid_slug(ev["show_name"])}@unbinge',
            f'DTSTAMP:{now_stamp}',
            f'DTSTART;TZID={LOCAL_TZ}:{start_dt.strftime("%Y%m%dT%H%M%S")}',
            f'DTEND;TZID={LOCAL_TZ}:{end_dt.strftime("%Y%m%dT%H%M%S")}',
            f'SUMMARY:📺 {ics_escape(ev["show_name"])} - {label}',
            f'DESCRIPTION:{ics_escape(ev["detail"])}',
            'END:VEVENT',
        ]

    sonarr_events, sonarr_err = sonarr_get_calendar(days_ahead=56)
    if not sonarr_err:
        for i, ev in enumerate(sonarr_events):
            ep_label = f"S{ev['season']:02d}E{ev['episode']:02d}"
            if ev['episode_title']:
                ep_label += f" - {ics_escape(ev['episode_title'])}"
            status = 'already downloaded' if ev['has_file'] else 'not yet downloaded'
            lines += [
                'BEGIN:VEVENT',
                f'UID:unbinge-sonarr-{i}-{ev["air_date"].isoformat()}-{ics_uid_slug(ev["show_name"])}@unbinge',
                f'DTSTAMP:{now_stamp}',
                f'DTSTART;VALUE=DATE:{ev["air_date"].strftime("%Y%m%d")}',
                f'SUMMARY:📡 {ics_escape(ev["show_name"])} {ep_label} airs',
                f'DESCRIPTION:{ics_escape(f"Via Sonarr - {status}")}',
                'END:VEVENT',
            ]

    lines.append('END:VCALENDAR')
    return '\r\n'.join(ics_fold_line(l) for l in lines) + '\r\n'


def project_schedule(weeks_ahead=6):
    """Builds a forward-looking agenda of expected drip/cooldown/graduation
    events for every active, non-paused show, based on its actual remaining
    vault episodes and configured release days. This is a projection, not a
    guarantee - if a show is paused, edited, or files change on disk, the
    real outcome may differ from what's shown here."""
    events = []
    today = today_local()
    horizon = today + timedelta(days=weeks_ahead * 7)

    for show in load_shows():
        if show['paused']:
            continue
        days = parse_release_days(show['release_days'])
        if not days:
            continue

        if show['completed_at']:
            d = next_occurrence(today, days)
            if d and d <= horizon:
                events.append({'date': d, 'show_name': show['show_name'], 'action': 'graduate',
                                'detail': 'Returns to the main library', 'poster_url': show['poster_url']})
            continue

        with closing(get_db()) as conn:
            dripped = get_dripped_set(conn, show['id'])
            excluded = get_excluded_set(conn, show['id'])
        seen_eps = distinct_episode_tags(
            remaining_episode_files(show['vault_path'], dripped, excluded)
        )

        cursor_date = today
        idx = 0
        per_drop = max(1, show['episodes_per_drop'])
        while idx < len(seen_eps):
            cursor_date = next_occurrence(cursor_date, days, inclusive=(idx == 0))
            if not cursor_date or cursor_date > horizon:
                break
            batch = seen_eps[idx:idx + per_drop]
            idx += len(batch)
            label = ", ".join(f"S{s:02d}E{e:02d}" for s, e in batch)
            events.append({'date': cursor_date, 'show_name': show['show_name'], 'action': 'drip',
                            'detail': label, 'poster_url': show['poster_url']})
            cursor_date = cursor_date + timedelta(days=1)

        if idx >= len(seen_eps) and cursor_date <= horizon:
            cooldown_date = next_occurrence(cursor_date, days)
            if cooldown_date and cooldown_date <= horizon:
                events.append({'date': cooldown_date, 'show_name': show['show_name'], 'action': 'cooldown',
                                'detail': 'No episodes left - cooldown begins', 'poster_url': show['poster_url']})
                graduate_date = next_occurrence(cooldown_date + timedelta(days=1), days)
                if graduate_date and graduate_date <= horizon:
                    events.append({'date': graduate_date, 'show_name': show['show_name'], 'action': 'graduate',
                                    'detail': 'Returns to the main library', 'poster_url': show['poster_url']})

    events.sort(key=lambda e: (e['date'], e['show_name']))
    return events


def preview_show_drip(show):
    """Dry-run: reports what process_show_drip WOULD do, without touching
    the filesystem or the database. Used by the Preview button."""
    show_name = show['show_name']

    with closing(get_db()) as conn:
        dripped = get_dripped_set(conn, show['id'])
        excluded = get_excluded_set(conn, show['id'])

    if show['completed_at']:
        recheck_remaining = remaining_episode_files(show['vault_path'], dripped, excluded)
        if recheck_remaining:
            return {'action': 'resume', 'detail': f"Cooldown would be cancelled - {len(recheck_remaining)} file(s) found waiting in the vault"}
        return {'action': 'graduate', 'detail': f"Would move the completed folder back to {POOL_DIR}"}

    target_eps, batch = compute_next_batch(
        show['vault_path'], dripped, show['episodes_per_drop'], excluded
    )
    if not target_eps:
        return {'action': 'cooldown', 'detail': "No episodes left in the vault - cooldown would start"}

    label = ", ".join(f"S{s:02d}E{e:02d}" for s, e in target_eps)
    filenames = [os.path.basename(u['rel_path']) for u in batch]
    return {
        'action': 'drip',
        'detail': f"Would drip {label} ({len(batch)} file(s))",
        'files': filenames,
    }


def get_undoable_drip():
    """The most recent 'dripped' history entry that hasn't already been
    undone (and hasn't been superseded by a later drip of the same show,
    since undoing an old drip while a newer one has already built on top of
    it would be confusing at best). Returns None if there's nothing to
    undo."""
    with closing(get_db()) as conn:
        row = conn.execute(f"""
            SELECT h.id, h.show_id, h.show_name, h.detail, h.occurred_at
            FROM {HISTORY_TABLE} h
            WHERE h.action = 'dripped'
              AND EXISTS (SELECT 1 FROM {DRIP_MOVES_TABLE} m WHERE m.history_id = h.id AND m.undone = 0)
            ORDER BY h.occurred_at DESC
            LIMIT 1
        """).fetchone()
    return dict(row) if row else None


def undo_drip(history_id):
    """Reverses one drip: moves every file it moved back from plex_path to
    its original vault_path location, and un-marks those episodes as
    dripped so the next scheduled run picks them up again.

    Returns (success: bool, message: str). Refuses (rather than partially
    undoing) if the show has since graduated or been deleted - at that point
    the vault_path this move recorded may no longer be meaningful, and
    silently recreating a folder for a show that no longer exists in the
    active list would be more confusing than declining."""
    with closing(get_db()) as conn:
        moves = conn.execute(
            f"SELECT * FROM {DRIP_MOVES_TABLE} WHERE history_id = ? AND undone = 0",
            (history_id,)
        ).fetchall()
        if not moves:
            return False, "Nothing to undo - this drip was already reverted or has no recorded moves."

        show_id = moves[0]['show_id']
        show_row = conn.execute(f"SELECT * FROM {TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
        if not show_row:
            return False, "Can't undo - that show is no longer active (deleted or graduated since)."
        show = dict(show_row)

        moved_back, failed = [], []
        for m in moves:
            try:
                os.makedirs(os.path.dirname(m['src_path']), exist_ok=True)
                if os.path.exists(m['dest_path']):
                    shutil.move(m['dest_path'], m['src_path'])
                    moved_back.append(m)
                else:
                    # Already gone from plex_path (manually deleted, moved,
                    # etc.) - nothing to move back, but still un-mark it as
                    # dripped so it's eligible again, since that's the more
                    # useful outcome than silently doing nothing.
                    moved_back.append(m)
            except OSError as e:
                failed.append((m, e))
                log.error("Undo failed for '%s': could not move %s back: %s", show['show_name'], m['dest_path'], e)

        if not moved_back:
            return False, f"Undo failed - all {len(failed)} file(s) could not be moved (likely locked)."

        for m in moved_back:
            conn.execute(f"UPDATE {DRIP_MOVES_TABLE} SET undone = 1 WHERE id = ?", (m['id'],))
            conn.execute(
                f"DELETE FROM {DRIPPED_TABLE} WHERE show_id = ? AND season = ? AND episode = ?",
                (m['show_id'], m['season'], m['episode'])
            )

        # If the show had already moved on to cooldown/graduation off the
        # back of this drip emptying the vault, undoing needs to reopen it.
        conn.execute(f"UPDATE {TABLE_NAME} SET completed_at = NULL WHERE id = ?", (show_id,))

        undone_tags = sorted({(m['season'], m['episode']) for m in moved_back})
        label = ", ".join(f"S{s:02d}E{e:02d}" for s, e in undone_tags)
        detail = f"Reverted {label} ({len(moved_back)} file(s))"
        if failed:
            detail += f" - {len(failed)} file(s) could not be moved and remain in Plex"
        log_history(conn, show_id, show['show_name'], 'undone', detail)
        conn.commit()

    remove_empty_dirs(os.path.dirname(moves[0]['dest_path']) if moves else '')
    trigger_plex_refresh()
    msg = f"Undid the last drip for '{show['show_name']}': {label}"
    if failed:
        msg += f" ({len(failed)} file(s) could not be reverted)"
    return True, msg


def check_and_advance_queue(trigger_show_id=None):
    """Checks the show_queue table for entries that should fire now, and
    promotes them via promote_show_internal.

    Two independent trigger types, matching what was asked for:
    - 'after_show': fires the moment trigger_show_id's row stops existing
      (graduated, deleted, or bulk-deleted) - called explicitly with that
      id right after the row is removed, from every place a show can leave
      the active list.
    - 'date': fires once trigger_date has arrived - checked once per
      scheduled run (not on every dashboard load), since it doesn't depend
      on any other show's lifecycle event.

    A queue entry whose target pool folder no longer exists is left in
    place with a logged warning rather than silently dropped - the user
    might just be mid-reorganizing their pool, and losing the queue entry
    entirely would be a worse failure mode than asking again next time."""
    fired = []
    with closing(get_db()) as conn:
        if trigger_show_id is not None:
            rows = conn.execute(
                f"SELECT * FROM {QUEUE_TABLE} WHERE trigger_type = 'after_show' "
                f"AND trigger_show_id = ? AND triggered_at IS NULL",
                (trigger_show_id,)
            ).fetchall()
        else:
            today_str = today_local().isoformat()
            rows = conn.execute(
                f"SELECT * FROM {QUEUE_TABLE} WHERE trigger_type = 'date' "
                f"AND trigger_date <= ? AND triggered_at IS NULL",
                (today_str,)
            ).fetchall()

        for row in rows:
            entry = dict(row)
            ok, message, new_show_id, _status_code = promote_show_internal(
                entry['show_name'], entry['release_days'], entry['episodes_per_drop']
            )
            if ok:
                conn.execute(
                    f"UPDATE {QUEUE_TABLE} SET triggered_at = ? WHERE id = ?",
                    (now_local().isoformat(), entry['id'])
                )
                conn.commit()
                log.info("Queue entry fired: '%s' promoted (%s).", entry['show_name'], message)
                fired.append((entry['show_name'], True, message))

                # An after_show successor is meant to pick up the vacated
                # slot seamlessly: the predecessor's LAST drip and the
                # successor's FIRST drip should land on the same day, so
                # that the successor's normal weekly cadence then produces
                # its second drip a week later - not two weeks later.
                # promote_show_internal only creates the row (current_episode
                # = 0, last_run_date = NULL); left alone, this new row won't
                # be picked up until the next scheduled pass whose weekday
                # matches ITS OWN release days, which - since the queue form
                # defaults those to match the predecessor's - is a further 7
                # days out, on top of the week already spent in cooldown.
                # Dripping it once, right here, closes that gap. Only
                # after_show entries get this: a date-triggered entry was
                # given an explicit start date on purpose, so it should wait
                # for its own configured release day rather than jump the
                # queue the moment that date arrives.
                if entry['trigger_type'] == 'after_show':
                    new_row = conn.execute(
                        f"SELECT * FROM {TABLE_NAME} WHERE id = ?", (new_show_id,)
                    ).fetchone()
                    if new_row:
                        try:
                            drip_result = process_show_drip(conn, dict(new_row))
                            conn.commit()
                            log.info(
                                "'%s': immediate first drip after taking over the queue slot - %s",
                                entry['show_name'], drip_result
                            )
                        except Exception as e:
                            # Not fatal to the promotion itself - the row
                            # already exists and will pick up normally on its
                            # own next matching release day if this fails.
                            conn.rollback()
                            log.error(
                                "'%s': immediate first drip failed (%s) - will pick up on its own next scheduled release day instead.",
                                entry['show_name'], e
                            )
            else:
                # Left untriggered deliberately - see docstring. Logged loudly
                # so a persistently-missing pool folder doesn't go unnoticed.
                log.warning(
                    "Queued show '%s' could not be promoted yet: %s. Will retry.",
                    entry['show_name'], message
                )
                fired.append((entry['show_name'], False, message))

    return fired


def get_queue_for_show(show_id):
    """Every queue entry that will fire when this specific show finishes -
    for display on the series detail page."""
    with closing(get_db()) as conn:
        rows = conn.execute(
            f"SELECT * FROM {QUEUE_TABLE} WHERE trigger_type = 'after_show' "
            f"AND trigger_show_id = ? AND triggered_at IS NULL ORDER BY created_at",
            (show_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_pending_queue_entries():
    """Every not-yet-fired queue entry, for the settings/system page and
    for cleaning up entries pointing at a since-deleted show."""
    with closing(get_db()) as conn:
        rows = conn.execute(
            f"SELECT * FROM {QUEUE_TABLE} WHERE triggered_at IS NULL ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def process_show_drip(conn, show):
    """Runs one show's weekly step: either drip the next batch of episodes
    from vault -> plex, mark it complete when the vault runs dry, or (one
    cycle later) graduate the finished show back to the main pool."""
    show_name = show['show_name']
    cursor = conn.cursor()

    dripped = get_dripped_set(conn, show['id'])
    excluded = get_excluded_set(conn, show['id'])

    if show['completed_at']:
        # Before graduating, double-check the vault hasn't gained episodes
        # since cooldown started (e.g. paths were repaired, or someone added
        # more episodes manually). This is what should have caught the
        # White Lotus false-cooldown incident automatically - and now does,
        # because it no longer inherits the watermark comparison that made
        # below-pointer episodes invisible to this check too.
        recheck_remaining = remaining_episode_files(show['vault_path'], dripped, excluded)
        if recheck_remaining:
            log.info("'%s' was in cooldown but the vault now has episodes again - resuming instead of graduating.", show_name)
            cursor.execute(f"UPDATE {TABLE_NAME} SET completed_at = NULL WHERE id = ?", (show['id'],))
            show = dict(show)
            show['completed_at'] = None
            log_history(conn, show['id'], show_name, 'resumed', 'Episodes reappeared in vault - cooldown cancelled')
        else:
            # Already finished dripping last cycle - this scheduled run is the
            # "one week later" trigger to send the whole show back to the pool.
            log.info("'%s' finished its cooldown - graduating back to the pool.", show_name)
            dest = os.path.join(POOL_DIR, show_name)
            # Specials ride along here: they were never dripped, but
            # merge_directory sweeps the whole vault folder, so they rejoin
            # the show in the pool rather than being stranded.
            r1 = merge_directory(show['plex_path'], dest)
            r2 = merge_directory(show['vault_path'], dest)
            problems = r1['failures'] + r2['failures']
            conflicts = r1['conflicts'] + r2['conflicts']

            if problems:
                # Don't delete the row - the show still has files in the old
                # locations and needs to be retried, not forgotten.
                msg = (f"'{show_name}' could not fully graduate - "
                       f"{len(problems)} file(s) locked. Will retry next cycle.")
                log.error(msg)
                log_history(conn, show['id'], show_name, 'failed', msg)
                return msg

            detail = f"Returned to {dest}"
            if conflicts:
                detail += f" ({len(conflicts)} filename conflict(s) parked in {CONFLICTS_DIRNAME})"
            clear_dripped(conn, show['id'])
            cursor.execute(f"DELETE FROM {TABLE_NAME} WHERE id = ?", (show['id'],))
            log_history(conn, show['id'], show_name, 'graduated', detail)
            trigger_plex_refresh()
            msg = f"'{show_name}' graduated back to {dest}"
            send_notification('graduated', f"🎉 '{show_name}' finished its full run and is back in your main library.", poster_url=show.get('poster_url'))
            conn.commit()
            check_and_advance_queue(trigger_show_id=show['id'])
            return msg

    target_eps, batch = compute_next_batch(
        show['vault_path'], dripped, show['episodes_per_drop'], excluded
    )

    if not target_eps:
        log.info("'%s' has no more episodes in the vault - starting cooldown.", show_name)
        cursor.execute(
            f"UPDATE {TABLE_NAME} SET completed_at = ?, last_run_date = ? WHERE id = ?",
            (now_local().isoformat(), today_local().isoformat(), show['id'])
        )
        log_history(conn, show['id'], show_name, 'cooldown', 'No episodes left in the vault')
        send_notification('cooldown', f"⏳ '{show_name}' has no more episodes queued - it'll return to your library next cycle.", poster_url=show.get('poster_url'))
        return f"'{show_name}' has no episodes left - cooldown started"

    # episodes_per_drop counts distinct episodes, not files - an episode may
    # have several files (video + subtitles) that must move together. That
    # logic now lives in compute_next_batch, shared with preview_show_drip so
    # the two can't drift apart.
    # Moved one at a time, tracking which episodes made it in full. An
    # episode is only recorded as dripped if EVERY one of its files moved -
    # a half-moved episode (video across, subtitle locked by Plex) must stay
    # pending so the next run completes it, rather than being marked done
    # with a file left behind in the vault.
    moved_files, failed_files = [], []
    for ep in batch:
        try:
            move_file_preserving_structure(ep['abs_path'], ep['rel_path'], show['plex_path'])
            moved_files.append(ep)
        except OSError as e:
            failed_files.append((ep, e))
            log.error(
                "Could not move '%s' for '%s': %s",
                ep['rel_path'], show_name, e
            )

    failed_tags = {t for ep, _ in failed_files for t in ep['tags']}
    complete_tags = [t for t in target_eps if t not in failed_tags]

    if failed_files:
        log.warning(
            "'%s': %d file(s) failed to move (usually a Plex file lock). "
            "%d episode(s) completed and recorded; the rest stay pending.",
            show_name, len(failed_files), len(complete_tags)
        )

    if not complete_tags:
        msg = f"'{show_name}' could not drip - all {len(failed_files)} file move(s) failed"
        log_history(conn, show['id'], show_name, 'failed', msg)
        return msg

    batch = moved_files

    # Bring along show-level art/metadata, plus season-level art/metadata for
    # every season folder touched by this batch, so Plex has proper artwork
    # as soon as episodes appear rather than just bare video files.
    copy_sibling_metadata(show['vault_path'], show['plex_path'])
    touched_subdirs = {os.path.dirname(ep['rel_path']) for ep in batch if os.path.dirname(ep['rel_path'])}
    for subdir in touched_subdirs:
        copy_sibling_metadata(os.path.join(show['vault_path'], subdir), os.path.join(show['plex_path'], subdir))

    # The authoritative record. current_season/current_episode are kept in
    # step purely so the dashboard's "Current Position" column still reads
    # sensibly; nothing decides what to move from them any more.
    mark_dripped(conn, show['id'], complete_tags)
    last = max(complete_tags)
    cursor.execute(
        f"""UPDATE {TABLE_NAME}
            SET current_season = ?, current_episode = ?, last_run_date = ?
            WHERE id = ?""",
        (last[0], last[1], today_local().isoformat(), show['id'])
    )
    remove_empty_dirs(show['vault_path'])
    trigger_plex_refresh()
    label = f"S{last[0]:02d}E{last[1]:02d}"
    history_id = log_history(conn, show['id'], show_name, 'dripped', f"{label} ({len(batch)} file(s))")

    # F2 - the history row always said WHAT happened ("dripped S01E01") but
    # never WHERE the files actually went, so there was nothing to automate
    # an undo against. This records the real paths, one row per moved file,
    # linked to the history entry that describes the drip as a whole.
    for ep in batch:
        dest_path = os.path.join(show['plex_path'], ep['rel_path'])
        for season, episode in ep['tags']:
            conn.execute(
                f"""INSERT INTO {DRIP_MOVES_TABLE}
                    (history_id, show_id, season, episode, src_path, dest_path)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                (history_id, show['id'], season, episode, ep['abs_path'], dest_path)
            )

    send_notification('dripped', f"📺 '{show_name}' just dripped {label}.", poster_url=show.get('poster_url'))
    return f"'{show_name}' dripped {label} ({len(batch)} file(s))"


def run_drip_job(force_show_id=None):
    """Scheduled entry point. Processes every show whose release_days
    includes today and that hasn't already been processed today - unless
    force_show_id is given, in which case that one show is processed
    immediately regardless of day/pause/last-run gating (manual override).

    Returns a dict describing what happened:
        {'ran': [(show_id, show_name, result_str), ...],
         'skipped': [(show_id, show_name, reason), ...],
         'errors': [(show_id, show_name, error_str), ...],
         'not_found': bool}

    Previously this returned None and every per-show exception was caught,
    logged, and discarded - so /api/drip-now could not tell the caller
    whether anything had actually happened. A nonexistent force_show_id
    matched zero rows and looped zero times, which looked identical to
    success. Both are now reported."""
    log.info("Running drip job%s...", f" (forced for show {force_show_id})" if force_show_id else "")
    started_at = now_local().isoformat()
    today_weekday = now_local().weekday()  # Monday=0 ... Sunday=6
    today_str = today_local().isoformat()

    # Missed-run notification - previously detect_missed_run() only drove a
    # dashboard banner, which you'd only see by opening Unbinge. Firing it
    # here too means the scheduled job itself can tell you it noticed a gap,
    # without needing to check the dashboard proactively.
    if force_show_id is None:
        missed = detect_missed_run()
        if missed:
            send_notification('missed_run', f"⚠ {missed}")

        # Date-triggered queue entries don't depend on another show's
        # lifecycle event, so they're checked once per scheduled run rather
        # than from any single show's drip step.
        check_and_advance_queue(trigger_show_id=None)

        run_scheduled_backup_if_due()

    # F1 - global dry-run mode. Lets someone watch a full week of scheduled
    # behaviour play out with zero files touched and zero database writes,
    # which is the safest way to trust a new install before turning it loose
    # on a real library. Reuses preview_show_drip - the exact same function
    # the Preview button calls - so dry-run mode can't drift from what
    # Preview already promises to be accurate about (see H1).
    dry_run = get_setting('dry_run', '0') == '1'

    outcome = {'ran': [], 'skipped': [], 'errors': [], 'not_found': False}

    conn = get_db()
    try:
        if force_show_id is not None:
            rows = conn.execute(f"SELECT * FROM {TABLE_NAME} WHERE id = ?", (force_show_id,)).fetchall()
            if not rows:
                outcome['not_found'] = True
        else:
            rows = conn.execute(f"SELECT * FROM {TABLE_NAME}").fetchall()

        for row in rows:
            show = dict(row)
            name = show.get('show_name')

            if force_show_id is None:
                if show.get('paused'):
                    outcome['skipped'].append((show['id'], name, 'paused'))
                    continue
                days = parse_release_days(show.get('release_days') or show.get('release_day'))
                if today_weekday not in days:
                    outcome['skipped'].append((show['id'], name, 'not a release day'))
                    continue
                if show['last_run_date'] == today_str:
                    outcome['skipped'].append((show['id'], name, 'already ran today'))
                    continue

            if dry_run:
                preview = preview_show_drip(show)
                result = f"[DRY RUN] '{name}': {preview['detail']}"
                log.info(result)
                outcome['ran'].append((show['id'], name, result))
                continue

            try:
                result = process_show_drip(conn, show)
                conn.commit()
                log.info(result)
                outcome['ran'].append((show['id'], name, result))
            except Exception as e:
                conn.rollback()
                log.error("Drip failed for '%s': %s", name, e)
                outcome['errors'].append((show['id'], name, str(e)))
                send_notification('failure', f"❌ Drip failed for '{name}': {e}")

        if force_show_id is None and not dry_run:
            set_setting('last_job_run', now_local().isoformat())

        # R3/U11 - every run leaves a record, scheduled or manual, so a
        # failed 3am job is visible on the dashboard rather than only in
        # `docker compose logs`. Dry runs are recorded too, marked as such
        # in the detail text, so they don't get confused with a real one.
        detail_parts = []
        if outcome['ran']:
            detail_parts.append(f"{len(outcome['ran'])} {'previewed' if dry_run else 'dripped'}")
        if outcome['errors']:
            detail_parts.append(f"{len(outcome['errors'])} failed")
        detail_text = ", ".join(detail_parts) or "nothing due"
        if dry_run:
            detail_text = f"[DRY RUN] {detail_text}"
        conn.execute(
            f"""INSERT INTO {JOB_RUNS_TABLE}
                (started_at, finished_at, forced_show_id, ran_count, skipped_count, error_count, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (started_at, now_local().isoformat(), force_show_id,
             len(outcome['ran']), len(outcome['skipped']), len(outcome['errors']), detail_text)
        )
        conn.commit()
    finally:
        conn.close()
    if not dry_run:
        sync_discord_schedule_message()
    log.info("Drip job complete.")
    return outcome


def get_last_job_run():
    """Most recent job_runs row, for the dashboard status strip. Returns
    None if the table is empty (fresh install, or before the first run)."""
    with closing(get_db()) as conn:
        row = conn.execute(
            f"SELECT * FROM {JOB_RUNS_TABLE} ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Scheduler setup (guarded against Flask's debug-mode double reload)
# ---------------------------------------------------------------------------
def detect_missed_run():
    """F5 - if the container was down over a scheduled run, nothing drips
    and nothing says so; the next 3am run just happens on schedule as if
    yesterday never existed. This computes whether the gap since the last
    recorded run is suspiciously large - more than ~30 hours, giving normal
    single-day slack around the drip hour - and returns a message if so.

    Deliberately computed live rather than stored, so it naturally clears
    itself the moment a run happens (last_job_run updates) rather than
    needing a separate 'dismiss' flag to maintain."""
    last_run_iso = get_setting('last_job_run', '')
    if not last_run_iso:
        return None  # never run yet - nothing to compare against
    try:
        last_run = datetime.fromisoformat(last_run_iso)
    except ValueError:
        return None

    gap = now_local() - last_run
    if gap.total_seconds() < 30 * 3600:
        return None

    days_missed = int(gap.total_seconds() // 86400)
    return (
        f"The last drip check was {days_missed} day(s) ago ({last_run.strftime('%a %b %d, %H:%M')}). "
        f"If the container was down, some releases may have been missed - "
        f"use 'Run drip check now' in Settings to catch up."
    )


def start_scheduler():
    hour = int(get_setting('drip_hour', '3') or 3)
    minute = int(get_setting('drip_minute', '0') or 0)
    # The scheduler is pinned to the resolved zone rather than the container's
    # clock, and max_instances=1 stops a slow run from overlapping the next.
    scheduler = BackgroundScheduler(timezone=LOCAL_TZ)
    scheduler.add_job(
        run_drip_job, 'cron', hour=hour, minute=minute,
        id=SCHEDULER_JOB_ID, max_instances=1, coalesce=True,
    )
    scheduler.start()
    _scheduler_holder['scheduler'] = scheduler
    log.info(
        "Scheduler started - drip check runs daily at %02d:%02d %s.",
        hour, minute, LOCAL_TZ
    )
    return scheduler


def reschedule_drip_job(hour, minute):
    scheduler = _scheduler_holder.get('scheduler')
    if scheduler:
        scheduler.reschedule_job(
            SCHEDULER_JOB_ID, trigger='cron', hour=hour, minute=minute, timezone=LOCAL_TZ
        )
        log.info("Rescheduled drip job to %02d:%02d %s.", hour, minute, LOCAL_TZ)


def _should_start_background_work():
    """Whether this process owns the scheduler.

    The old guard read `not app.debug`, but app.debug is still False at import
    time (app.run sets it later), so the condition was always true and the
    reloader's parent process started a scheduler of its own alongside the
    child's. Reading the environment directly is the check that actually
    distinguishes the two.

    UNBINGE_TESTING short-circuits the whole block. Importing this module runs
    it, so without that escape hatch merely collecting the test suite would
    start a real scheduler, reconcile real paths, and post to the real Discord
    webhook. Tests call init_db() themselves against a temporary database."""
    if os.environ.get('UNBINGE_TESTING') == '1':
        return False

    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    if not debug_mode:
        return True
    return os.environ.get('WERKZEUG_RUN_MAIN') == 'true'


if _should_start_background_work():
    init_db()
    startup_repairs = reconcile_show_paths()
    if startup_repairs:
        log.info("Startup path reconciliation repaired %d path(s).", len(startup_repairs))
    start_scheduler()
    sync_discord_schedule_message_async()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    sort = request.args.get('sort', 'day')
    if sort not in ('day', 'name', 'status'):
        sort = 'day'
    shows = load_shows(sort=sort)

    # Reuses the same projection built for the Discord schedule message -
    # one lookup, keyed by show name, so the dashboard can show "what's
    # dripping next and when" without duplicating the scheduling logic.
    next_up_map = {ev['show_name']: ev for ev in build_next_up_events()}

    counts = {
        'active': sum(1 for s in shows if not s['paused'] and not s['completed_at']),
        'cooldown': sum(1 for s in shows if not s['paused'] and s['completed_at']),
        'paused': sum(1 for s in shows if s['paused']),
    }

    # U5 - "Next drip check in 8h 14m" on the dashboard itself. Settings
    # already showed the next run time, but that's not where anyone actually
    # looks first.
    scheduler = _scheduler_holder.get('scheduler')
    job = scheduler.get_job(SCHEDULER_JOB_ID) if scheduler else None
    next_run_iso = job.next_run_time.isoformat() if job and job.next_run_time else None

    last_run = get_last_job_run()
    undoable = get_undoable_drip()
    missed_run_notice = detect_missed_run()

    all_tags = sorted({t for s in shows for t in s['tags']}, key=str.lower)

    return render_template(
        'index.html', shows=shows, current_sort=sort,
        next_up_map=next_up_map, counts=counts, next_run_iso=next_run_iso,
        last_run=last_run, undoable=undoable, missed_run_notice=missed_run_notice,
        all_tags=all_tags,
    )


@app.route('/calendar.ics', methods=['GET'])
def serve_calendar_ics():
    """The subscribable calendar feed. Google Calendar (Settings -> Add
    calendar -> From URL) polls this periodically rather than accepting a
    one-time file upload, so this needs to be a live endpoint that always
    reflects the current schedule, not a static generated file.

    Deliberately exempt from auth (see AUTH_EXEMPT_PATHS) even when a
    password is configured - Google Calendar can't send custom headers or a
    password with a subscription URL, so this has to stay reachable without
    one. Anyone who has this exact URL can see your drip schedule and (if
    configured) your Sonarr library's air dates. Not sensitive information,
    but worth knowing before sharing the URL.

    This ALSO means Google's own servers need to be able to reach this URL
    over the public internet - a localhost or LAN-only address works fine
    when you open it yourself, but Google's fetcher has no way to reach
    it, which silently looks identical to "hasn't refreshed yet." If
    Unbinge isn't reachable from outside your network, use
    /calendar-download instead for a one-off file you import by hand."""
    try:
        ics_text = build_ical_feed()
    except Exception as e:
        log.error("Calendar feed generation failed: %s", e)
        return "Calendar generation failed - check the container logs.", 500

    return ics_text, 200, {
        'Content-Type': 'text/calendar; charset=utf-8',
        'Content-Disposition': 'inline; filename="unbinge-schedule.ics"',
    }


@app.route('/calendar-download', methods=['GET'])
def download_calendar_ics():
    """A one-off downloadable .ics file, for importing by hand into Google
    Calendar (Settings -> Import & export -> Import) rather than
    subscribing to a live URL. This is the answer for anyone whose Unbinge
    isn't reachable from the public internet, since Google's import feature
    accepts an uploaded file directly and never needs to fetch anything
    itself.

    Unlike /calendar.ics, this is NOT exempt from auth - a manual download
    is a real logged-in user clicking a button, not an external service
    that has no way to authenticate, so there's no reason to weaken the
    same protection every other page gets. The content is otherwise
    identical; only the response headers differ (a forced download with a
    dated filename, instead of the inline rendering a live subscription
    expects).

    This is a static snapshot, not a live sync - re-download and re-import
    whenever the schedule changes meaningfully (the settings page suggests
    monthly, which comfortably covers most shows' release cadence)."""
    try:
        ics_text = build_ical_feed()
    except Exception as e:
        log.error("Calendar file generation failed: %s", e)
        return "Calendar generation failed - check the container logs.", 500

    filename = f"unbinge-schedule-{today_local().isoformat()}.ics"
    return ics_text, 200, {
        'Content-Type': 'text/calendar; charset=utf-8',
        'Content-Disposition': f'attachment; filename="{filename}"',
    }


@app.route('/posters/<path:filename>')
def serve_poster(filename):
    """Serves locally-cached poster images (see cache_poster_locally).
    Uses send_from_directory rather than a manual open(), which handles
    path traversal safety, correct content-type, and caching headers."""
    return send_from_directory(POSTERS_DIR, filename, max_age=86400 * 30)


def gather_system_checks():
    """Consolidates every health-relevant signal that already exists
    somewhere in Unbinge - database, scheduler, disk space, TVDB, Sonarr,
    path drift - into one list, instead of them being scattered across
    separate banners and endpoints with no single place to look. Each item
    is {'label', 'status': 'ok'|'warn'|'error', 'detail'}."""
    checks = []

    try:
        with closing(get_db()) as conn:
            conn.execute(f"SELECT 1 FROM {TABLE_NAME} LIMIT 1;").fetchone()
        checks.append({'label': 'Database', 'status': 'ok', 'detail': DB_FILE})
    except Exception as e:
        checks.append({'label': 'Database', 'status': 'error', 'detail': str(e)})

    scheduler = _scheduler_holder.get('scheduler')
    job = scheduler.get_job(SCHEDULER_JOB_ID) if scheduler else None
    if scheduler and scheduler.running and job:
        next_run = job.next_run_time.strftime('%Y-%m-%d %H:%M %Z') if job.next_run_time else 'unknown'
        checks.append({'label': 'Scheduler', 'status': 'ok', 'detail': f'next run {next_run}'})
    else:
        checks.append({'label': 'Scheduler', 'status': 'error', 'detail': 'not running'})

    try:
        free = shutil.disk_usage(VAULT_DIR).free
        status = 'ok' if free > 5 * 1024**3 else ('warn' if free > 1024**3 else 'error')
        checks.append({'label': 'Vault disk space', 'status': status, 'detail': f'{format_bytes_human(free)} free'})
    except OSError as e:
        checks.append({'label': 'Vault disk space', 'status': 'warn', 'detail': f'could not check: {e}'})

    tvdb_key = get_setting('tvdb_api_key', '').strip()
    if tvdb_key:
        token, err = tvdb_get_token()
        checks.append({'label': 'TVDB', 'status': 'ok' if token else 'error',
                        'detail': 'connected' if token else (err or 'connection failed')})
    else:
        checks.append({'label': 'TVDB', 'status': 'warn', 'detail': 'not configured - posters disabled'})

    sonarr_url = get_setting('sonarr_url', '').strip()
    if sonarr_url:
        _events, err = sonarr_get_calendar(days_ahead=1)
        checks.append({'label': 'Sonarr', 'status': 'ok' if not err else 'error', 'detail': err or 'connected'})
    else:
        checks.append({'label': 'Sonarr', 'status': 'warn', 'detail': 'not configured (optional)'})

    last_run = get_last_job_run()
    if last_run and last_run['error_count'] > 0:
        checks.append({'label': 'Last drip run', 'status': 'error',
                        'detail': f"{last_run['error_count']} show(s) failed - see History"})
    elif last_run:
        checks.append({'label': 'Last drip run', 'status': 'ok', 'detail': last_run['detail']})
    else:
        checks.append({'label': 'Last drip run', 'status': 'warn', 'detail': 'never run yet'})

    missed = detect_missed_run()
    if missed:
        checks.append({'label': 'Run schedule', 'status': 'warn', 'detail': missed})

    return checks


@app.route('/system')
def system_page():
    checks = gather_system_checks()
    overall = 'error' if any(c['status'] == 'error' for c in checks) else \
              ('warn' if any(c['status'] == 'warn' for c in checks) else 'ok')
    return render_template('system.html', checks=checks, overall=overall, app_version=APP_VERSION)


@app.route('/api/system-status', methods=['GET'])
def api_system_status():
    """Lightweight version of the same checks, for the sidebar's health dot -
    polled periodically without needing the full page."""
    checks = gather_system_checks()
    overall = 'error' if any(c['status'] == 'error' for c in checks) else \
              ('warn' if any(c['status'] == 'warn' for c in checks) else 'ok')
    return jsonify({'overall': overall, 'checks': checks})


@app.route('/health', methods=['GET'])
def health():
    """Liveness probe for the container healthcheck. Reports on the two things
    that can be broken while the process is still technically running: the
    database being unreachable, and the scheduler having died. Returns 503 in
    either case so Docker marks the container unhealthy rather than leaving a
    zombie that serves pages but never drips anything."""
    checks = {'database': False, 'scheduler': False}

    try:
        with closing(get_db()) as conn:
            conn.execute(f"SELECT 1 FROM {TABLE_NAME} LIMIT 1;").fetchone()
        checks['database'] = True
    except Exception as e:
        log.warning("Health check: database unreachable: %s", e)

    scheduler = _scheduler_holder.get('scheduler')
    job = scheduler.get_job(SCHEDULER_JOB_ID) if scheduler else None
    checks['scheduler'] = bool(scheduler and scheduler.running and job)

    healthy = all(checks.values())
    payload = {
        'status': 'ok' if healthy else 'degraded',
        'version': APP_VERSION,
        'timezone': str(LOCAL_TZ),
        'local_time': now_local().isoformat(timespec='seconds'),
        'checks': checks,
    }
    if job and job.next_run_time:
        payload['next_run'] = job.next_run_time.isoformat()

    return jsonify(payload), (200 if healthy else 503)


@app.route('/api/pool', methods=['GET'])
def get_pool():
    query = request.args.get('q', '').lower()

    pool_shows = []
    if os.path.exists(POOL_DIR):
        try:
            pool_shows = [d for d in os.listdir(POOL_DIR) if os.path.isdir(os.path.join(POOL_DIR, d))]
        except Exception as e:
            log.error("Could not read pool directory: %s", e)

    if query:
        filtered = [s for s in pool_shows if query in s.lower()]
    else:
        filtered = pool_shows

    return jsonify({"shows": filtered})


@app.route('/api/recommendations/<int:show_id>', methods=['GET'])
def api_recommendations(show_id):
    """Pool shows genre-matched against show_id, for the 'up next' panel on
    its detail page. See get_recommendations_for_show for the matching
    logic and _migrate_v10_genre_recommendation_cache for the caching."""
    result = get_recommendations_for_show(show_id)
    if result['status'] == 'not_found':
        return jsonify(result), 404
    return jsonify(result), 200


def safe_show_path(root_dir, show_name):
    """Resolves show_name against root_dir and guarantees the result stays
    inside it, or raises ValueError.

    Previously /api/inspect and /promote joined the raw show_name straight
    onto POOL_DIR/VAULT_DIR with no check. A show_name of '..' resolved to
    POOL_DIR's own parent, and '../../secret' escaped further still - with no
    authentication in front of this app, that's reachable from anything on
    the LAN. os.path.basename alone isn't enough here, because a name with no
    slashes at all - just '..' - already IS its own basename and would sail
    through unchanged; the check has to be on the resolved, normalised path,
    not on the input string's shape."""
    root_dir = os.path.abspath(root_dir)
    candidate = os.path.abspath(os.path.join(root_dir, show_name))
    if os.path.commonpath([root_dir, candidate]) != root_dir:
        raise ValueError(f"'{show_name}' resolves outside the allowed directory")
    return candidate


@app.route('/api/tvdb-search', methods=['GET'])
def api_tvdb_search():
    """Searches TVDB for a show name, for the promote/edit UI to let the
    user pick the right match (important for common titles that have
    several distinct shows with the same name)."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({"status": "error", "message": "q is required"}), 400
    results, err = tvdb_search_series(query)
    if err:
        return jsonify({"status": "error", "message": err}), 502
    return jsonify({"status": "success", "results": results})


@app.route('/api/tvdb-link/<int:show_id>', methods=['POST'])
def api_tvdb_link(show_id):
    """Links a show to a specific TVDB id, fetching and storing its poster.
    Called after the user picks a match from /api/tvdb-search results."""
    tvdb_id = request.form.get('tvdb_id', type=int)
    if not tvdb_id:
        return jsonify({"status": "error", "message": "tvdb_id is required"}), 400

    details, err = tvdb_get_series_details(tvdb_id)
    if err or not details:
        return jsonify({"status": "error", "message": err or "Could not fetch series details."}), 502

    with closing(get_db()) as conn:
        row = conn.execute(f"SELECT show_name, poster_url FROM {TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
        if not row:
            return jsonify({"status": "error", "message": "Show not found"}), 404

        # Re-linking to a different show replaces the poster - clean up the
        # old cached file first so POSTERS_DIR doesn't accumulate orphans.
        delete_cached_poster(row['poster_url'])

        local_poster_url = cache_poster_locally(show_id, details['poster_url'])
        conn.execute(
            f"UPDATE {TABLE_NAME} SET tvdb_id = ?, poster_url = ? WHERE id = ?",
            (tvdb_id, local_poster_url, show_id)
        )
        conn.commit()

    return jsonify({
        "status": "success",
        "message": f"Linked to TVDB: {details['name']}",
        "poster_url": local_poster_url,
        "season_episode_counts": details['season_episode_counts'],
    })


@app.route('/api/tvdb-unlink/<int:show_id>', methods=['POST'])
def api_tvdb_unlink(show_id):
    """Clears a show's TVDB link/poster, for when a match was wrong."""
    with closing(get_db()) as conn:
        row = conn.execute(f"SELECT poster_url FROM {TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
        if row:
            delete_cached_poster(row['poster_url'])
        conn.execute(f"UPDATE {TABLE_NAME} SET tvdb_id = NULL, poster_url = NULL WHERE id = ?", (show_id,))
        conn.commit()
    return jsonify({"status": "success", "message": "Unlinked."})


@app.route('/api/test-tvdb', methods=['POST'])
def api_test_tvdb():
    """Settings page 'Test Connection' button - confirms the API key
    actually works without needing to link a real show.

    Tests whatever is currently in the form fields, sent along with the
    request, rather than only the last-SAVED value - a key typed but not
    yet saved would otherwise be silently ignored in favour of testing an
    empty or stale saved value, which is a confusing failure with no
    corresponding log line to explain it."""
    api_key = request.form.get('tvdb_api_key', '').strip()
    pin = request.form.get('tvdb_pin', '').strip()
    log.info("Testing TVDB connection (key provided: %s, pin provided: %s)...", bool(api_key), bool(pin))
    token, err = tvdb_get_token(override_api_key=api_key, override_pin=pin)
    if err:
        log.warning("TVDB test connection failed: %s", err)
        return jsonify({"status": "error", "message": err}), 400
    log.info("TVDB test connection succeeded.")
    return jsonify({"status": "success", "message": "TVDB connection OK - token acquired."})


@app.route('/api/sonarr/calendar', methods=['GET'])
def api_sonarr_calendar():
    """Upcoming real air dates from the user's whole Sonarr library, for
    the schedule page's 'Upcoming on Sonarr' panel."""
    try:
        days = int(request.args.get('days', 14))
    except (TypeError, ValueError):
        days = 14
    days = max(1, min(days, 60))

    events, err = sonarr_get_calendar(days_ahead=days)
    if err:
        # Not configured is an expected, quiet state - not an error the UI
        # should alarm about, so it still returns 200 with an empty list
        # plus the message, letting the template decide how to present it.
        return jsonify({"status": "success", "events": [], "message": err})

    return jsonify({
        "status": "success",
        "events": [
            {**e, 'air_date': e['air_date'].isoformat()} for e in events
        ],
        "message": None,
    })


@app.route('/api/test-sonarr', methods=['POST'])
def api_test_sonarr():
    """Settings page 'Test Connection' button for the optional Sonarr link.
    Same fix as TVDB's test button - uses the current form values."""
    base = request.form.get('sonarr_url', '').strip().rstrip('/')
    api_key = request.form.get('sonarr_api_key', '').strip()
    log.info("Testing Sonarr connection (url provided: %s, key provided: %s)...", bool(base), bool(api_key))
    if not base or not api_key:
        return jsonify({"status": "error", "message": "Sonarr URL and API key are both required."}), 400
    try:
        resp = requests.get(f'{base}/api/v3/system/status', headers={'X-Api-Key': api_key}, timeout=15)
        resp.raise_for_status()
        log.info("Sonarr test connection succeeded.")
        return jsonify({"status": "success", "message": "Sonarr connection OK."})
    except requests.RequestException as e:
        log.warning("Sonarr test connection failed: %s", e)
        return jsonify({"status": "error", "message": f"Could not reach Sonarr: {e}"}), 502


@app.route('/api/inspect', methods=['GET'])
def api_inspect():
    """Called when a show is selected in the promote modal, before
    committing. Scans the pool folder for SxxExx-pattern files so the user
    sees an episode/season count (or a warning if nothing matches) before
    moving anything."""
    show_name = request.args.get('show', '').strip()
    if not show_name:
        return jsonify({"status": "error", "message": "show is required"}), 400

    try:
        source_dir = safe_show_path(POOL_DIR, show_name)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    if not os.path.isdir(source_dir):
        return jsonify({"status": "error", "message": "Show not found in pool"}), 404

    episodes = scan_episode_files(source_dir)
    seasons = sorted({e['season'] for e in episodes})
    distinct_eps = len({(e['season'], e['episode']) for e in episodes})

    # U29 - promoting a 200GB show previously gave no indication of what was
    # about to move. Deduplicated by abs_path since scan_episode_files can
    # return multiple entries for one file (a combo episode covers several
    # tags - see C7); counting it once per tag would double the real size.
    unique_paths = {e['abs_path'] for e in episodes}
    total_bytes = 0
    for p in unique_paths:
        try:
            total_bytes += os.path.getsize(p)
        except OSError:
            pass

    warning = None
    if distinct_eps == 0:
        warning = "No files matching an SxxExx pattern were found - the drip job wouldn't have anything to move."

    # Disk space check on the destination. U29 showed the size of what's
    # about to move but never checked whether there was room for it -
    # a large show promoted onto a nearly-full vault drive would previously
    # fail partway through the move with a confusing error, rather than a
    # clear warning upfront. shutil.disk_usage reports the filesystem VAULT_DIR
    # lives on, which is what actually matters, not POOL_DIR's filesystem.
    try:
        os.makedirs(VAULT_DIR, exist_ok=True)
        free_bytes = shutil.disk_usage(VAULT_DIR).free
    except OSError:
        free_bytes = None

    space_warning = None
    if free_bytes is not None and total_bytes > 0 and total_bytes > free_bytes:
        space_warning = (
            f"This show is {format_bytes_human(total_bytes)} but the vault drive only has "
            f"{format_bytes_human(free_bytes)} free - promoting it may fail partway through."
        )

    return jsonify({
        "status": "success",
        "episode_count": distinct_eps,
        "seasons": seasons,
        "warning": warning,
        "total_bytes": total_bytes,
        "free_bytes": free_bytes,
        "space_warning": space_warning,
    })


def promote_show_internal(show_name, release_days_str, episodes_per_drop):
    """The actual promote logic, shared by the /promote HTTP route and the
    show-queue auto-promotion. Extracted so the two can't drift apart the
    way process_show_drip and preview_show_drip once did (H1) - a queued
    show being promoted automatically must behave identically to one
    promoted by hand.

    Returns (success: bool, message: str, new_show_id: int|None, status_code: int).
    The status code is explicit rather than left for the caller to guess
    from the message text - guessing via 'not found' in message broke once
    already, misclassifying a genuine server-side move failure (disk error)
    as a 400 client error instead of a 500, since its message happened not
    to contain that substring."""
    try:
        source_dir = safe_show_path(POOL_DIR, show_name)
        vault_path = safe_show_path(VAULT_DIR, show_name)
        plex_path = safe_show_path(PLEX_BASE_DIR, show_name)
    except ValueError as e:
        return False, str(e), None, 400

    try:
        release_day_ints = sorted({int(d) for d in release_days_str.split(',') if d != ''})
        if not release_day_ints or any(not (0 <= d <= 6) for d in release_day_ints):
            raise ValueError
    except ValueError:
        return False, "At least one valid release day is required", None, 400

    try:
        episodes_per_drop = int(episodes_per_drop)
        if episodes_per_drop < 1:
            raise ValueError
    except ValueError:
        return False, "episodes_per_drop must be a positive integer", None, 400

    with closing(get_db()) as conn:
        cursor = conn.cursor()

        existing = cursor.execute(
            f"SELECT id FROM {TABLE_NAME} WHERE show_name = ?", (show_name,)
        ).fetchone()
        if existing:
            return False, f"'{show_name}' is already an active drip", None, 400

        if not os.path.isdir(source_dir):
            return False, f"'{show_name}' was not found in the pool directory", None, 404

        if os.path.exists(vault_path):
            return False, f"Vault path already exists for '{show_name}'", None, 409

        # The row is written before the files move, so a failure during the
        # move can be rolled back to leave no record.
        cursor.execute(f"""
            INSERT INTO {TABLE_NAME}
                (show_name, vault_path, plex_path, release_day, release_days, current_season,
                 current_episode, episodes_per_drop, last_run_date)
            VALUES (?, ?, ?, ?, ?, 1, 0, ?, NULL)
        """, (show_name, vault_path, plex_path, release_day_ints[0], release_days_str, episodes_per_drop))
        new_id = cursor.lastrowid

        try:
            os.makedirs(VAULT_DIR, exist_ok=True)
            shutil.move(source_dir, vault_path)
        except Exception as move_err:
            conn.rollback()
            log.error("Promote failed while moving '%s' into the vault: %s", show_name, move_err)
            # 500, not 400/404 - the request itself was fine, the filesystem
            # operation failed, which is not the caller's fault.
            return False, f"Could not move '{show_name}' into the vault: {move_err}", None, 500

        clear_dripped(conn, new_id)
        log_history(conn, new_id, show_name, 'promoted',
                    f"Moved from pool into vault ({episodes_per_drop}/drop, {format_release_days(release_days_str)})")
        conn.commit()

    send_notification('promoted', f"➕ '{show_name}' was added to the drip schedule.")
    sync_discord_schedule_message_async()
    return True, f"{show_name} successfully promoted!", new_id, 200


@app.route('/promote', methods=['POST'])
def promote():
    show_name = request.form.get('show_name')
    release_days_raw = request.form.getlist('release_days') or [request.form.get('release_day', '0')]
    episodes_per_drop_raw = request.form.get('episodes_per_drop', '1')

    if not show_name:
        return jsonify({"status": "error", "message": "Show name is required"}), 400

    release_day_ints_raw = [d for d in release_days_raw if d != '']
    release_days_str = ",".join(release_day_ints_raw)

    try:
        ok, message, new_id, status_code = promote_show_internal(show_name, release_days_str, episodes_per_drop_raw)
        return jsonify({"status": "success" if ok else "error", "message": message}), status_code
    except Exception as e:
        log.error("Promote failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/preview/<int:show_id>', methods=['GET'])
def api_preview(show_id):
    """Dry-run preview for a single show - reports what the next drip run
    would do without touching the filesystem or database."""
    with closing(get_db()) as conn:
        row = conn.execute(f"SELECT * FROM {TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
    if not row:
        return jsonify({"status": "error", "message": "Show not found"}), 404
    result = preview_show_drip(dict(row))
    return jsonify({"status": "success", **result}), 200


@app.route('/toggle-pause/<int:show_id>', methods=['POST'])
def toggle_pause(show_id):
    flash_msg, flash_kind, new_val, ok = "Show not found", "error", None, False
    with closing(get_db()) as conn:
        row = conn.execute(f"SELECT paused, show_name FROM {TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
        if row:
            new_val = 0 if row['paused'] else 1
            conn.execute(f"UPDATE {TABLE_NAME} SET paused = ? WHERE id = ?", (new_val, show_id))
            log_history(conn, show_id, row['show_name'], 'paused' if new_val else 'resumed', '')
            conn.commit()
            flash_msg = f"'{row['show_name']}' {'paused' if new_val else 'resumed'}."
            flash_kind, ok = "success", True
    sync_discord_schedule_message_async()

    # U13 - the dashboard's pause button now calls this via fetch() and
    # swaps the row in place, rather than a full page reload that loses
    # scroll position and the filter box's contents. The plain <form> POST
    # fallback (no-JS, or the old behaviour) still works via the redirect.
    if request.headers.get('X-Requested-With') == 'fetch':
        status = 200 if ok else 404
        return jsonify({
            "status": "success" if ok else "error",
            "message": flash_msg,
            "paused": bool(new_val) if new_val is not None else None,
        }), status

    return redirect(url_for('index', flash=flash_msg, flash_kind=flash_kind))


@app.route('/history')
def history_page():
    PAGE_SIZE = 50
    show_filter = request.args.get('show', '').strip() or None
    action_filter = request.args.get('action', '').strip() or None
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1

    events, total = load_history(
        limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE,
        show_name=show_filter, action=action_filter,
    )
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)

    # For the action-type filter dropdown - every distinct action ever
    # logged, not a hardcoded list that would go stale if a new action type
    # is added later (as happened with 'deleted' and 'failed').
    with closing(get_db()) as conn:
        all_actions = sorted(r['action'] for r in conn.execute(
            f"SELECT DISTINCT action FROM {HISTORY_TABLE}"
        ).fetchall())

    return render_template(
        'history.html', events=events, total=total, page=page, total_pages=total_pages,
        show_filter=show_filter or '', action_filter=action_filter or '', all_actions=all_actions,
    )


@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        try:
            hour = int(request.form.get('drip_hour', 3))
            minute = int(request.form.get('drip_minute', 0))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError:
            hour, minute = 3, 0

        set_setting('drip_hour', hour)
        set_setting('drip_minute', minute)
        set_setting('webhook_url', request.form.get('webhook_url', '').strip())
        set_setting('dry_run', '1' if request.form.get('dry_run') == 'on' else '0')
        new_tvdb_key = request.form.get('tvdb_api_key', '').strip()
        if new_tvdb_key != get_setting('tvdb_api_key', ''):
            set_setting('tvdb_token', '')
            set_setting('tvdb_token_expires', '')
        set_setting('tvdb_api_key', new_tvdb_key)
        set_setting('tvdb_pin', request.form.get('tvdb_pin', '').strip())
        set_setting('sonarr_url', request.form.get('sonarr_url', '').strip())
        set_setting('sonarr_api_key', request.form.get('sonarr_api_key', '').strip())
        set_setting('notify_on_drip', '1' if request.form.get('notify_on_drip') == 'on' else '0')
        set_setting('notify_on_failure', '1' if request.form.get('notify_on_failure') == 'on' else '0')
        set_setting('notify_on_missed_run', '1' if request.form.get('notify_on_missed_run') == 'on' else '0')
        set_setting('auto_backup_enabled', '1' if request.form.get('auto_backup_enabled') == 'on' else '0')
        try:
            retention = max(1, min(int(request.form.get('backup_retention_count', 7)), 90))
        except (TypeError, ValueError):
            retention = 7
        set_setting('backup_retention_count', str(retention))

        new_discord_url = request.form.get('discord_webhook_url', '').strip()
        # A changed webhook points at a different message thread entirely -
        # the stored schedule_message_id belongs to the OLD one. Left in
        # place, it only worked because of the 404 fallback in
        # sync_discord_schedule_message (PATCH fails, falls through to
        # POST), which meant a wasted request and a "Live" status that
        # wasn't true until the next sync actually ran.
        if new_discord_url != get_setting('discord_webhook_url', ''):
            set_setting('schedule_message_id', '')
        set_setting('discord_webhook_url', new_discord_url)

        reschedule_drip_job(hour, minute)
        sync_discord_schedule_message_async()
        return redirect(url_for('settings_page', flash='Settings saved', flash_kind='success'))

    scheduler = _scheduler_holder.get('scheduler')
    next_run = None
    job = scheduler.get_job(SCHEDULER_JOB_ID) if scheduler else None
    if job and job.next_run_time:
        next_run = job.next_run_time.strftime('%Y-%m-%d %H:%M %Z')

    settings = {
        'drip_hour': get_setting('drip_hour', '3'),
        'drip_minute': get_setting('drip_minute', '0'),
        'webhook_url': get_setting('webhook_url', ''),
        'discord_webhook_url': get_setting('discord_webhook_url', ''),
        'last_job_run': get_setting('last_job_run', ''),
        'dry_run': get_setting('dry_run', '0') == '1',
        'tvdb_api_key': get_setting('tvdb_api_key', ''),
        'tvdb_pin': get_setting('tvdb_pin', ''),
        'sonarr_url': get_setting('sonarr_url', ''),
        'sonarr_api_key': get_setting('sonarr_api_key', ''),
        'notify_on_drip': get_setting('notify_on_drip', '1') == '1',
        'notify_on_failure': get_setting('notify_on_failure', '1') == '1',
        'notify_on_missed_run': get_setting('notify_on_missed_run', '1') == '1',
        'auto_backup_enabled': get_setting('auto_backup_enabled', '0') == '1',
        'backup_retention_count': get_setting('backup_retention_count', '7'),
        'last_auto_backup': get_setting('last_auto_backup', ''),
    }
    discord_configured = bool(parse_discord_webhook(settings['discord_webhook_url']))
    discord_message_live = discord_configured and bool(get_setting('schedule_message_id', '').strip())
    return render_template(
        'settings.html', settings=settings, next_run=next_run,
        plex_configured=bool(PLEX_URL and PLEX_TOKEN),
        discord_configured=discord_configured, discord_message_live=discord_message_live,
        container_tz=str(LOCAL_TZ),
        ical_feed_url=request.url_root.rstrip('/') + url_for('serve_calendar_ics'),
    )


@app.route('/schedule')
def schedule_page():
    # Previously an unguarded int() - '?weeks=abc' was a 500 and
    # '?weeks=99999' was a very long filesystem walk.
    try:
        weeks_ahead = int(request.args.get('weeks', 6))
    except (TypeError, ValueError):
        weeks_ahead = 6
    weeks_ahead = max(1, min(weeks_ahead, 52))

    events = project_schedule(weeks_ahead=weeks_ahead)
    grouped = {}
    for ev in events:
        grouped.setdefault(ev['date'].isoformat(), []).append(ev)
    ordered_dates = sorted(grouped.keys())

    # U23 - "2026-09-11" as a heading reads like a bug report. Rendered here
    # rather than in the template because strftime's weekday/month names
    # depend on locale, which is a server-side concern.
    day_labels = {d: date.fromisoformat(d).strftime('%A %-d %B') for d in ordered_dates}
    tomorrow_str = (today_local() + timedelta(days=1)).isoformat()

    return render_template(
        'schedule.html', grouped=grouped, ordered_dates=ordered_dates,
        weeks_ahead=weeks_ahead, today_str=today_local().isoformat(),
        day_labels=day_labels, tomorrow_str=tomorrow_str,
    )


@app.route('/api/run-drip', methods=['POST'])
def api_run_drip():
    """Manual trigger for testing the drip cycle without waiting for the
    scheduled 3am run or the actual release day."""
    try:
        dry_run = get_setting('dry_run', '0') == '1'
        outcome = run_drip_job()
        parts = []
        if outcome['ran']:
            parts.append(f"{len(outcome['ran'])} show(s) {'previewed (dry run)' if dry_run else 'dripped'}")
        if outcome['errors']:
            parts.append(f"{len(outcome['errors'])} failed")
        message = ", ".join(parts) if parts else "Nothing was due to drip"
        status = "error" if outcome['errors'] and not outcome['ran'] else "success"
        return jsonify({"status": status, "message": message}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/sync-discord-schedule', methods=['POST'])
def api_sync_discord_schedule():
    """Manually re-posts/edits the persistent Discord schedule message -
    used by the 'Sync Now' button in Settings to test the webhook URL and
    give the user real feedback (configured / not configured / failed)."""
    try:
        result = sync_discord_schedule_message()
        http_status = 200 if result['status'] in ('ok', 'skipped') else 400
        return jsonify({"status": result['status'], "message": result['detail']}), http_status
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/bulk/pause', methods=['POST'])
def api_bulk_pause():
    """Bulk pause/resume - Sonarr's Series Editor multi-select equivalent.
    'paused' in the request body means pause everything selected; false
    means resume everything selected, rather than toggling each show
    individually (which could pause some and resume others depending on
    their current state)."""
    ids = request.get_json(silent=True) or {}
    show_ids = ids.get('show_ids', [])
    new_paused = 1 if ids.get('paused', True) else 0
    if not show_ids:
        return jsonify({"status": "error", "message": "No shows selected"}), 400

    updated = 0
    with closing(get_db()) as conn:
        for show_id in show_ids:
            row = conn.execute(f"SELECT show_name FROM {TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
            if not row:
                continue
            conn.execute(f"UPDATE {TABLE_NAME} SET paused = ? WHERE id = ?", (new_paused, show_id))
            log_history(conn, show_id, row['show_name'], 'paused' if new_paused else 'resumed', 'bulk action')
            updated += 1
        conn.commit()
    sync_discord_schedule_message_async()

    verb = 'paused' if new_paused else 'resumed'
    return jsonify({"status": "success", "message": f"{updated} show(s) {verb}."})


@app.route('/api/bulk/delete', methods=['POST'])
def api_bulk_delete():
    """Bulk delete. Reuses the same per-show logic as the single delete
    route (safe_show_path check, merge_directory, history logging) rather
    than a shortcut path, so a bulk delete gets exactly the same safety
    checks a single one does."""
    ids = request.get_json(silent=True) or {}
    show_ids = ids.get('show_ids', [])
    if not show_ids:
        return jsonify({"status": "error", "message": "No shows selected"}), 400

    succeeded, failed = 0, 0
    deleted_ids = []
    with closing(get_db()) as conn:
        for show_id in show_ids:
            row = conn.execute(f"SELECT * FROM {TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
            if not row:
                continue
            show = dict(row)
            try:
                dest = safe_show_path(POOL_DIR, show['show_name'])
            except ValueError:
                failed += 1
                continue
            merge_directory(show['vault_path'], dest)
            merge_directory(show['plex_path'], dest)
            clear_dripped(conn, show_id)
            conn.execute(f"DELETE FROM {TABLE_NAME} WHERE id = ?", (show_id,))
            log_history(conn, show_id, show['show_name'], 'deleted', 'bulk action')
            succeeded += 1
            deleted_ids.append(show_id)
        conn.commit()
    sync_discord_schedule_message_async()
    for deleted_id in deleted_ids:
        check_and_advance_queue(trigger_show_id=deleted_id)

    msg = f"{succeeded} show(s) removed."
    if failed:
        msg += f" {failed} could not be removed safely."
    return jsonify({"status": "success" if succeeded else "error", "message": msg})


@app.route('/api/bulk/drip-now', methods=['POST'])
def api_bulk_drip_now():
    """Bulk 'drip now' - runs the forced-drip path for each selected show
    in turn, aggregating the outcome into one summary rather than one
    toast per show."""
    ids = request.get_json(silent=True) or {}
    show_ids = ids.get('show_ids', [])
    if not show_ids:
        return jsonify({"status": "error", "message": "No shows selected"}), 400

    ran, errors, not_found = 0, 0, 0
    for show_id in show_ids:
        outcome = run_drip_job(force_show_id=show_id)
        if outcome['not_found']:
            not_found += 1
        elif outcome['errors']:
            errors += 1
        elif outcome['ran']:
            ran += 1

    parts = []
    if ran: parts.append(f"{ran} dripped")
    if errors: parts.append(f"{errors} failed")
    if not_found: parts.append(f"{not_found} not found")
    return jsonify({"status": "success" if ran else "error", "message": ", ".join(parts) or "Nothing happened"})


@app.route('/api/undo-last-drip', methods=['GET', 'POST'])
def api_undo_last_drip():
    """GET reports whether there's anything undoable (used to decide whether
    to show the button at all); POST actually performs it."""
    undoable = get_undoable_drip()

    if request.method == 'GET':
        return jsonify({"status": "success", "undoable": undoable})

    if not undoable:
        return jsonify({"status": "error", "message": "Nothing to undo."}), 400

    try:
        ok, message = undo_drip(undoable['id'])
        return jsonify({"status": "success" if ok else "error", "message": message}), (200 if ok else 400)
    except Exception as e:
        log.error("Undo failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


BACKUPS_DIR = os.path.join(os.path.dirname(DB_FILE), 'backups')


def create_db_backup(destination_dir=None):
    """Snapshots the database via SQLite's own backup API (not a plain file
    copy, which could miss uncommitted WAL data) and returns the path
    written. Used by both the manual 'download backup' button and the
    automatic scheduled backup.

    Filenames include microseconds and fall back to a numeric suffix if a
    collision somehow still occurs - a second-resolution timestamp alone
    can collide when two backups happen within the same second (a manual
    'backup now' click landing in the same second as the scheduled one,
    for instance), which would otherwise silently overwrite one backup
    with another rather than keeping both."""
    destination_dir = destination_dir or BACKUPS_DIR
    os.makedirs(destination_dir, exist_ok=True)
    timestamp = now_local().strftime('%Y%m%d-%H%M%S-%f')
    filename = f'unbinge-backup-{timestamp}.db'
    path = os.path.join(destination_dir, filename)
    suffix = 1
    while os.path.exists(path):
        path = os.path.join(destination_dir, f'unbinge-backup-{timestamp}-{suffix}.db')
        suffix += 1

    src = sqlite3.connect(DB_FILE)
    dest = sqlite3.connect(path)
    with dest:
        src.backup(dest)
    src.close()
    dest.close()
    return path


# Old backups created before the Driparr->Unbinge rename are still named
# "driparr-backup-*.db" on disk - they're recognized here too so pruning
# and the Settings backup list don't silently orphan pre-rename backups.
BACKUP_FILENAME_PREFIXES = ('unbinge-backup-', 'driparr-backup-')


def _is_backup_filename(filename):
    return filename.endswith('.db') and filename.startswith(BACKUP_FILENAME_PREFIXES)


def prune_old_backups(keep=7):
    """Deletes all but the most recent `keep` backups in BACKUPS_DIR. Called
    after every scheduled backup so the directory doesn't grow forever -
    Sonarr's own backup retention works the same way (a fixed count, not
    a fixed age, since 'keep the last 7' is easier to reason about than
    'keep 30 days worth' when backup frequency might change)."""
    if not os.path.isdir(BACKUPS_DIR):
        return
    backups = sorted(
        (f for f in os.listdir(BACKUPS_DIR) if _is_backup_filename(f)),
        reverse=True
    )
    for old in backups[keep:]:
        try:
            os.remove(os.path.join(BACKUPS_DIR, old))
            log.info("Pruned old backup: %s", old)
        except OSError as e:
            log.warning("Could not prune backup %s: %s", old, e)


def run_scheduled_backup_if_due():
    """Runs at most once per day, right before the drip job, if automatic
    backups are enabled. Checked against a stored last-backup timestamp
    rather than relying on the scheduler firing at exactly the right
    time, so a missed day (container was down) doesn't skip the backup
    on the next run that actually happens."""
    if get_setting('auto_backup_enabled', '0') != '1':
        return
    last = get_setting('last_auto_backup', '')
    if last:
        try:
            if (now_local() - datetime.fromisoformat(last)).total_seconds() < 20 * 3600:
                return  # already backed up recently enough
        except ValueError:
            pass

    try:
        path = create_db_backup()
        keep = int(get_setting('backup_retention_count', '7') or 7)
        prune_old_backups(keep=keep)
        set_setting('last_auto_backup', now_local().isoformat())
        log.info("Automatic backup created: %s", path)
    except Exception as e:
        log.error("Automatic backup failed: %s", e)


@app.route('/api/run-backup-now', methods=['POST'])
def api_run_backup_now():
    """Manually creates a backup into BACKUPS_DIR immediately, applying the
    same retention rule as the scheduled one - distinct from the
    /api/backup-db download, which writes to a temp file for one-off
    download without touching the rotation."""
    try:
        path = create_db_backup()
        keep = int(get_setting('backup_retention_count', '7') or 7)
        prune_old_backups(keep=keep)
        set_setting('last_auto_backup', now_local().isoformat())
        return jsonify({"status": "success", "message": f"Backup created: {os.path.basename(path)}"})
    except Exception as e:
        log.error("Manual scheduled-style backup failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/backups', methods=['GET'])
def api_list_backups():
    """Lists existing rotated backups, for display in Settings."""
    if not os.path.isdir(BACKUPS_DIR):
        return jsonify({"status": "success", "backups": []})
    files = sorted(
        (f for f in os.listdir(BACKUPS_DIR) if _is_backup_filename(f)),
        reverse=True
    )
    backups = []
    for f in files:
        path = os.path.join(BACKUPS_DIR, f)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        backups.append({'filename': f, 'size_human': format_bytes_human(size)})
    return jsonify({"status": "success", "backups": backups})


@app.route('/api/backup-db', methods=['GET'])
def api_backup_db():
    """Downloads a snapshot of the database - dripped_episodes, drip_moves
    (for undo), job history, and every show's configuration all live in
    this one file, so a one-click backup is cheap insurance, especially
    right before a schema migration runs on upgrade.

    Uses sqlite3's own backup API rather than just copying DB_FILE, since
    the live file can have uncommitted WAL data that a plain file copy
    would miss - the backup API produces a complete, consistent snapshot
    regardless of what's mid-transaction at the moment of the request."""
    try:
        path = create_db_backup(destination_dir=tempfile.gettempdir())
        return send_file(path, as_attachment=True, download_name=os.path.basename(path), mimetype='application/x-sqlite3')
    except Exception as e:
        log.error("Database backup failed: %s", e)
        return jsonify({"status": "error", "message": f"Backup failed: {e}"}), 500


@app.route('/api/repair-paths', methods=['POST'])
def api_repair_paths():
    """Manually re-runs path reconciliation (the same check that happens on
    every startup) without needing to restart the container."""
    try:
        repairs = reconcile_show_paths()
        message = f"{len(repairs)} path(s) repaired." if repairs else "All paths already correct."
        return jsonify({"status": "success", "message": message, "repairs": repairs}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/drip-now/<int:show_id>', methods=['POST'])
def api_drip_now(show_id):
    """Manually triggers this one show's drip step immediately, ignoring
    its release day, pause state, and same-day gating. Useful for catching
    up a show that missed its scheduled day, or testing without waiting.

    Previously always returned "Drip executed for show" regardless of what
    actually happened - a failed drip, or an id matching no show at all,
    both reported success."""
    try:
        outcome = run_drip_job(force_show_id=show_id)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    if outcome['not_found']:
        return jsonify({"status": "error", "message": f"No show with id {show_id}"}), 404

    if outcome['errors']:
        _sid, name, err = outcome['errors'][0]
        return jsonify({"status": "error", "message": f"Drip failed for '{name}': {err}"}), 500

    if outcome['ran']:
        _sid, name, result = outcome['ran'][0]
        return jsonify({"status": "success", "message": result}), 200

    # Reached only if force_show_id matched a row but that row was somehow
    # excluded from both ran/errors, which force mode should never do.
    return jsonify({"status": "error", "message": "Drip did not run for an unknown reason"}), 500


def build_episode_grid(show_id, show):
    """Assembles the per-episode status grid for the series detail page.

    Combines three sources: what's physically in the vault, what's already
    in Plex, and the dripped/excluded tables - so a season's grid can show,
    per episode, exactly one of: dripped, waiting, excluded, or (if a TVDB
    link exists and reports more episodes than are on disk anywhere)
    missing entirely.

    Returns {season_num: [{'episode', 'status', 'title'}, ...], ...},
    ordered by season, specials (season 0) listed separately if present."""
    with closing(get_db()) as conn:
        dripped = get_dripped_set(conn, show_id)
        excluded = get_excluded_set(conn, show_id)

    vault_eps = {(e['season'], e['episode']) for e in scan_episode_files(show['vault_path'])}
    plex_eps = {(e['season'], e['episode']) for e in scan_episode_files(show['plex_path'])}
    all_known = vault_eps | plex_eps | dripped | excluded

    # If linked to TVDB, ask what SHOULD exist too, so a genuinely missing
    # episode (never downloaded at all) shows up as its own status rather
    # than just being invisible.
    tvdb_counts = {}
    if show.get('tvdb_id'):
        details, err = tvdb_get_series_details(show['tvdb_id'])
        if not err and details:
            tvdb_counts = details.get('season_episode_counts', {})

    for season_num, count in tvdb_counts.items():
        for ep_num in range(1, count + 1):
            all_known.add((season_num, ep_num))

    grid = {}
    for season, episode in sorted(all_known):
        if (season, episode) in excluded:
            status = 'excluded'
        elif (season, episode) in dripped or (season, episode) in plex_eps:
            status = 'dripped'
        elif (season, episode) in vault_eps:
            status = 'waiting'
        else:
            status = 'missing'
        grid.setdefault(season, []).append({'episode': episode, 'status': status})

    return grid


@app.route('/show/<int:show_id>')
def show_detail(show_id):
    with closing(get_db()) as conn:
        row = conn.execute(f"SELECT * FROM {TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
    if not row:
        return "Show not found", 404
    show = dict(row)
    # The inline "show settings" panel needs the same release_days_set shape
    # /edit already builds, so its day checkboxes (and the "drip daily"
    # toggle) can be pre-checked identically on both pages.
    release_days_raw = show.get('release_days') or str(show.get('release_day', '0'))
    show['release_days_set'] = set(parse_release_days(release_days_raw))

    grid = build_episode_grid(show_id, show)
    seasons_sorted = sorted(grid.keys())

    total_eps = sum(len(v) for v in grid.values())
    dripped_count = sum(1 for eps in grid.values() for e in eps if e['status'] == 'dripped')

    return render_template(
        'show_detail.html', show=show, grid=grid, seasons_sorted=seasons_sorted,
        total_eps=total_eps, dripped_count=dripped_count,
        specials_season=SPECIALS_SEASON,
        error=request.args.get('error') if request.args.get('error') in ('duplicate_name', 'invalid_name') else None,
    )


@app.route('/api/queue/<int:show_id>', methods=['GET'])
def api_get_queue(show_id):
    """Queue entries that will fire when this show finishes - for the
    series detail page."""
    return jsonify({"status": "success", "entries": get_queue_for_show(show_id)})


@app.route('/api/queue/<int:show_id>', methods=['POST'])
def api_add_to_queue(show_id):
    """Adds a pool show to this show's succession queue. Either fires the
    moment this show finishes (trigger_type=after_show, the default) or on
    a specific date instead (trigger_type=date) - the two mechanisms asked
    for: 'start after another show has finished' and 'set a date when it
    will start'."""
    show_name = request.form.get('show_name', '').strip()
    trigger_type = request.form.get('trigger_type', 'after_show')
    trigger_date = request.form.get('trigger_date', '').strip() or None
    release_days = request.form.get('release_days', '').strip()
    episodes_per_drop = request.form.get('episodes_per_drop', '1')

    if not show_name:
        return jsonify({"status": "error", "message": "show_name is required"}), 400
    if trigger_type not in ('after_show', 'date'):
        return jsonify({"status": "error", "message": "trigger_type must be after_show or date"}), 400
    if trigger_type == 'date' and not trigger_date:
        return jsonify({"status": "error", "message": "trigger_date is required for a date-triggered entry"}), 400

    try:
        safe_show_path(POOL_DIR, show_name)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    if not os.path.isdir(os.path.join(POOL_DIR, show_name)):
        return jsonify({"status": "error", "message": f"'{show_name}' was not found in the pool directory"}), 404

    with closing(get_db()) as conn:
        show_row = conn.execute(f"SELECT release_days, episodes_per_drop FROM {TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
        if not show_row:
            return jsonify({"status": "error", "message": "Show not found"}), 404

        # Default to inheriting the anchor show's own schedule, so "queue
        # this up next" doesn't require re-specifying the release day every
        # time for the common case of just continuing the same slot.
        final_release_days = release_days or show_row['release_days'] or '0'
        final_episodes_per_drop = episodes_per_drop or show_row['episodes_per_drop'] or 1

        conn.execute(f"""
            INSERT INTO {QUEUE_TABLE}
                (show_name, trigger_type, trigger_show_id, trigger_date,
                 release_days, episodes_per_drop, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (show_name, trigger_type, show_id if trigger_type == 'after_show' else None,
              trigger_date, final_release_days, final_episodes_per_drop, now_local().isoformat()))
        conn.commit()

    return jsonify({"status": "success", "message": f"'{show_name}' queued."})


@app.route('/api/queue/entry/<int:entry_id>', methods=['DELETE'])
def api_remove_queue_entry(entry_id):
    with closing(get_db()) as conn:
        row = conn.execute(f"SELECT show_name FROM {QUEUE_TABLE} WHERE id = ?", (entry_id,)).fetchone()
        if not row:
            return jsonify({"status": "error", "message": "Queue entry not found"}), 404
        conn.execute(f"DELETE FROM {QUEUE_TABLE} WHERE id = ?", (entry_id,))
        conn.commit()
    return jsonify({"status": "success", "message": f"Removed '{row['show_name']}' from the queue."})


@app.route('/api/unmatched-files/<int:show_id>', methods=['GET'])
def api_unmatched_files(show_id):
    """Video files in this show's vault or plex folder that don't match any
    recognized episode pattern - the manual-import gap. Checks both
    locations since a stray file could be sitting in either."""
    with closing(get_db()) as conn:
        row = conn.execute(f"SELECT vault_path, plex_path FROM {TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
    if not row:
        return jsonify({"status": "error", "message": "Show not found"}), 404

    vault_unmatched = [{**f, 'location': 'vault'} for f in find_unmatched_files(row['vault_path'])]
    plex_unmatched = [{**f, 'location': 'plex'} for f in find_unmatched_files(row['plex_path'])]
    return jsonify({"status": "success", "files": vault_unmatched + plex_unmatched})


@app.route('/api/manual-match/<int:show_id>', methods=['POST'])
def api_manual_match(show_id):
    """Assigns a season/episode to a file that didn't auto-parse, by
    renaming it to include a standard tag."""
    rel_path = request.form.get('rel_path', '').strip()
    location = request.form.get('location', 'vault')
    season = request.form.get('season', type=int)
    episode = request.form.get('episode', type=int)
    if not rel_path or season is None or episode is None:
        return jsonify({"status": "error", "message": "rel_path, season, and episode are required"}), 400

    with closing(get_db()) as conn:
        row = conn.execute(f"SELECT vault_path, plex_path, show_name FROM {TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
    if not row:
        return jsonify({"status": "error", "message": "Show not found"}), 404

    root = row['vault_path'] if location == 'vault' else row['plex_path']
    abs_path = os.path.normpath(os.path.join(root, rel_path))
    # Guard against rel_path escaping the intended root via '..' segments.
    if os.path.commonpath([os.path.abspath(root), os.path.abspath(abs_path)]) != os.path.abspath(root):
        return jsonify({"status": "error", "message": "Invalid file path"}), 400
    if not os.path.isfile(abs_path):
        return jsonify({"status": "error", "message": "File not found"}), 404

    try:
        new_path = manually_match_episode(abs_path, season, episode)
    except (OSError, FileExistsError) as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    with closing(get_db()) as conn:
        log_history(conn, show_id, row['show_name'], 'generic',
                    f"Manually matched '{os.path.basename(abs_path)}' as S{season:02d}E{episode:02d}")
        conn.commit()

    return jsonify({"status": "success", "message": f"Matched as S{season:02d}E{episode:02d}.", "new_path": os.path.basename(new_path)})


@app.route('/api/exclude-episode/<int:show_id>', methods=['POST'])
def api_exclude_episode(show_id):
    season = request.form.get('season', type=int)
    episode = request.form.get('episode', type=int)
    action = request.form.get('action', 'exclude')
    if season is None or episode is None:
        return jsonify({"status": "error", "message": "season and episode are required"}), 400

    with closing(get_db()) as conn:
        row = conn.execute(f"SELECT show_name FROM {TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
        if not row:
            return jsonify({"status": "error", "message": "Show not found"}), 404
        if action == 'unexclude':
            unexclude_episode(conn, show_id, season, episode)
            msg = f"S{season:02d}E{episode:02d} will drip normally again."
        else:
            exclude_episode(conn, show_id, season, episode)
            msg = f"S{season:02d}E{episode:02d} will never be dripped."
        conn.commit()

    return jsonify({"status": "success", "message": msg})


@app.route('/edit/<int:show_id>', methods=['GET'])
def render_edit(show_id):
    try:
        with closing(get_db()) as conn:
            row = conn.execute(f"SELECT * FROM {TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()

        if not row:
            return "Show not found", 404

        release_days_raw = safe_get(row, 'release_days', None) or str(safe_get(row, 'release_day', '0'))
        show_data = {
            'id': safe_get(row, 'id', show_id),
            'show_name': safe_get(row, 'show_name', ''),
            'vault_path': safe_get(row, 'vault_path', ''),
            'plex_path': safe_get(row, 'plex_path', ''),
            'release_day': str(safe_get(row, 'release_day', '0')),
            'release_days_set': set(parse_release_days(release_days_raw)),
            'current_season': safe_get(row, 'current_season', 1),
            'current_episode': safe_get(row, 'current_episode', 0),
            'episodes_per_drop': safe_get(row, 'episodes_per_drop', 1),
            'tags': safe_get(row, 'tags', '') or '',
        }

        return render_template(
            'edit.html', show=show_data,
            error=request.args.get('error') if request.args.get('error') in ('duplicate_name', 'invalid_name') else None
        )
    except Exception as e:
        log.error("Edit render failed: %s", e)
        return redirect(url_for('index'))


@app.route('/edit/<int:show_id>', methods=['POST'])
def save_edit(show_id):
    # The exact same settings form (name, paths, release days, season/episode
    # progress, episodes-per-drop, tags) is now embedded both on this show's
    # own /edit page and inline on its /show detail page, so the two no
    # longer disagree about what's editable. `return_to` just says which
    # page sent the submission, so a save made inline on the detail page
    # lands back there instead of bouncing to the dashboard.
    return_to = request.form.get('return_to')
    if return_to not in ('show_detail',):
        return_to = 'index'

    def _error_redirect(error_code):
        if return_to == 'show_detail':
            messages = {
                'invalid_name': "that name isn't valid - it can't contain path separators like \"/\" or \"..\".",
                'duplicate_name': "that name is already used by another show. choose a different one.",
            }
            return redirect(url_for('show_detail', show_id=show_id, error=error_code,
                                     flash=messages.get(error_code, 'could not save changes.'),
                                     flash_kind='error'))
        return redirect(url_for('render_edit', show_id=show_id, error=error_code))

    try:
        show_name = request.form.get('show_name')
        vault_path = request.form.get('vault_path')
        plex_path = request.form.get('plex_path')
        release_days_raw = request.form.getlist('release_days')
        current_season = int(request.form.get('current_season', 1))
        current_episode = int(request.form.get('current_episode', 0))
        episodes_per_drop = int(request.form.get('episodes_per_drop', 1))
        tags_str = ",".join(parse_tags(request.form.get('tags', '')))

        release_day_ints = sorted({int(d) for d in release_days_raw if d != ''}) or [0]
        release_days_str = ",".join(str(d) for d in release_day_ints)

        with closing(get_db()) as conn:
            # promote() has always refused a duplicate name at creation time;
            # this was the one path that could still produce one, by renaming
            # an existing show to match another. Checked explicitly, ahead of
            # the UNIQUE index, so the failure is a clean redirect rather than
            # an unhandled IntegrityError caught by the bare except below.
            try:
                safe_show_path(POOL_DIR, show_name)
            except ValueError:
                log.warning("Rejected edit: '%s' is not a safe show name.", show_name)
                return _error_redirect('invalid_name')

            clash = conn.execute(
                f"SELECT id FROM {TABLE_NAME} WHERE show_name = ? AND id != ?",
                (show_name, show_id)
            ).fetchone()
            if clash:
                log.warning(
                    "Rejected edit: '%s' is already in use by show id %d.",
                    show_name, clash['id']
                )
                return _error_redirect('duplicate_name')

            conn.execute(f"""
                UPDATE {TABLE_NAME}
                SET show_name = ?, vault_path = ?, plex_path = ?, release_day = ?, release_days = ?,
                    current_season = ?, current_episode = ?, episodes_per_drop = ?, tags = ?
                WHERE id = ?
            """, (show_name, vault_path, plex_path, release_day_ints[0], release_days_str,
                  current_season, current_episode, episodes_per_drop, tags_str, show_id))
            conn.commit()
        sync_discord_schedule_message_async()
    except sqlite3.IntegrityError:
        # Belt and braces: catches a duplicate name even if it somehow slips
        # past the explicit check above (e.g. a race between two edits).
        log.warning("Rejected edit for show %d: name collision at the database level.", show_id)
        return _error_redirect('duplicate_name')
    except Exception as e:
        log.error("Edit save failed: %s", e)
        if return_to == 'show_detail':
            return redirect(url_for('show_detail', show_id=show_id, flash=f'could not save changes: {e}', flash_kind='error'))
        return redirect(url_for('index', flash=f'Could not save changes: {e}', flash_kind='error'))

    if return_to == 'show_detail':
        return redirect(url_for('show_detail', show_id=show_id, flash=f"'{show_name}' updated.", flash_kind='success'))
    return redirect(url_for('index', flash=f"'{show_name}' updated.", flash_kind='success'))


@app.route('/delete/<int:show_id>', methods=['POST'])
def delete_show(show_id):
    """Removing a show from the active drip list returns any files it
    still has (in the vault and/or already in Plex) to the main pool,
    rather than leaving them orphaned."""
    flash_msg, flash_kind = "Show not found.", "error"
    try:
        with closing(get_db()) as conn:
            row = conn.execute(f"SELECT * FROM {TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
            if row:
                show = dict(row)
                delete_cached_poster(show.get('poster_url'))

                try:
                    dest = safe_show_path(POOL_DIR, show['show_name'])
                except ValueError:
                    log.error(
                        "Refusing to delete show %d: stored show_name '%s' does "
                        "not resolve safely under POOL_DIR. Fix the name via "
                        "the Edit page before deleting.",
                        show_id, show['show_name']
                    )
                    return redirect(url_for(
                        'index', flash=f"Could not delete '{show['show_name']}' - invalid stored name. Fix it via Edit first.",
                        flash_kind='error'
                    ))

                # Counted before the move, because afterwards the source
                # directories are gone and there's nothing left to count.
                vault_files = count_files(show['vault_path'])
                plex_files = count_files(show['plex_path'])

                r1 = merge_directory(show['vault_path'], dest)
                r2 = merge_directory(show['plex_path'], dest)
                conflicts = r1['conflicts'] + r2['conflicts']
                problems = r1['failures'] + r2['failures']
                clear_dripped(conn, show_id)
                conn.execute(f"DELETE FROM {TABLE_NAME} WHERE id = ?", (show_id,))

                # Deletions were previously the only destructive action that
                # left no trace at all - a show would simply vanish from the
                # dashboard with nothing in history explaining where it went.
                log_history(
                    conn, show_id, show['show_name'], 'deleted',
                    f"Removed from drip - returned {r1['moved'] + r2['moved']} file(s) to {dest} "
                    f"({vault_files} from vault, {plex_files} from Plex)"
                    + (f", {len(conflicts)} conflict(s) parked" if conflicts else "")
                    + (f", {len(problems)} file(s) LOCKED and left behind" if problems else "")
                )
                conn.commit()
                log.info(
                    "Deleted '%s' - returned %d file(s) to %s.",
                    show['show_name'], vault_files + plex_files, dest
                )
                check_and_advance_queue(trigger_show_id=show_id)
                # U9 - names the show and gives a real count, instead of a
                # silent redirect that looked identical whether it worked or
                # not.
                total_files = vault_files + plex_files
                flash_msg = f"'{show['show_name']}' removed - {total_files} file(s) returned to the pool."
                flash_kind = "success"
                if problems:
                    flash_msg += f" {len(problems)} file(s) were locked and left behind."
                    flash_kind = "warning"
        sync_discord_schedule_message_async()
    except Exception as e:
        log.error("Delete failed: %s", e)
        flash_msg, flash_kind = f"Delete failed: {e}", "error"

    return redirect(url_for('index', flash=flash_msg, flash_kind=flash_kind))


if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=5000, debug=debug_mode, threaded=True)
