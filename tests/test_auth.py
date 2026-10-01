"""Tests for S2 (CSRF) and S3 (single shared password auth).

Auth is opt-in via the UNBINGE_PASSWORD environment variable (or the older
DRIPARR_PASSWORD) - the default
(unset) must leave the app exactly as it always behaved, since making auth
suddenly mandatory on upgrade would lock people out of their own install.

These tests set/clear driparr.AUTH_PASSWORD directly rather than relying on
environment variables, since the module is already imported by the time any
test runs - the harness fixture in conftest.py resets it to '' before each
test so auth-off is the default test environment, matching the 170+ existing
tests that were written with no concept of auth at all.
"""

import re
from contextlib import closing

import app as driparr


# ---------------------------------------------------------------------------
# Default (no password configured) - must be a complete no-op
# ---------------------------------------------------------------------------

def test_auth_disabled_by_default(harness):
    assert driparr.auth_enabled() is False


def test_all_pages_reachable_with_no_login_when_auth_disabled(harness, client):
    for path in ('/', '/schedule', '/settings', '/history', '/system'):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} should not require login when auth is off"


def test_mutating_request_needs_no_csrf_token_when_auth_disabled(harness, client):
    show_id = harness.add_show('Test Show')
    resp = client.post(f'/toggle-pause/{show_id}')
    assert resp.status_code == 302, "should succeed with no CSRF token when auth is disabled"


def test_login_page_redirects_away_when_auth_not_configured(harness, client):
    resp = client.get('/login', follow_redirects=False)
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Auth enabled - the actual login/logout/CSRF flow
# ---------------------------------------------------------------------------

def _enable_auth(password='hunter2'):
    driparr.AUTH_PASSWORD = password


def _disable_auth():
    driparr.AUTH_PASSWORD = ''


def test_protected_page_redirects_to_login_when_not_authenticated(harness, client):
    _enable_auth()
    try:
        resp = client.get('/', follow_redirects=False)
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']
    finally:
        _disable_auth()


def test_protected_api_returns_401_not_a_redirect(harness, client):
    _enable_auth()
    try:
        resp = client.get('/api/system-status')
        assert resp.status_code == 401
    finally:
        _disable_auth()


def test_login_page_itself_is_reachable_unauthenticated(harness, client):
    _enable_auth()
    try:
        resp = client.get('/login')
        assert resp.status_code == 200
    finally:
        _disable_auth()


def test_wrong_password_is_rejected(harness, client):
    _enable_auth()
    try:
        r = client.get('/login')
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', r.data.decode()).group(1)
        resp = client.post('/login', data={'password': 'wrong', 'csrf_token': csrf})
        assert b'Incorrect password' in resp.data
    finally:
        _disable_auth()


def test_correct_password_logs_in_and_grants_access(harness, client):
    _enable_auth()
    try:
        r = client.get('/login')
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', r.data.decode()).group(1)
        resp = client.post('/login', data={'password': 'hunter2', 'csrf_token': csrf}, follow_redirects=False)
        assert resp.status_code == 302

        r2 = client.get('/')
        assert r2.status_code == 200
    finally:
        _disable_auth()


def test_login_rejects_a_stale_csrf_token(harness, client):
    _enable_auth()
    try:
        resp = client.post('/login', data={'password': 'hunter2', 'csrf_token': 'not-the-real-token'})
        assert b'session expired' in resp.data.lower() or resp.status_code == 200
        r2 = client.get('/')
        assert r2.status_code == 302, "login must not have succeeded with a forged csrf token"
    finally:
        _disable_auth()


def test_login_never_redirects_off_site(harness, client):
    """The 'next' parameter is user-controlled - it must not be usable to
    redirect a freshly-authenticated session to an external URL."""
    _enable_auth()
    try:
        r = client.get('/login?next=' + 'http://evil.example.com')
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', r.data.decode()).group(1)
        resp = client.post('/login?next=http://evil.example.com',
                           data={'password': 'hunter2', 'csrf_token': csrf}, follow_redirects=False)
        location = resp.headers.get('Location', '')
        assert not location.startswith('http://evil.example.com')
    finally:
        _disable_auth()


def test_mutating_request_without_csrf_token_is_rejected(harness, client):
    _enable_auth()
    try:
        r = client.get('/login')
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', r.data.decode()).group(1)
        client.post('/login', data={'password': 'hunter2', 'csrf_token': csrf})

        show_id = harness.add_show('Test Show')
        resp = client.post(f'/toggle-pause/{show_id}')
        assert resp.status_code == 403
    finally:
        _disable_auth()


def test_mutating_request_with_correct_csrf_header_succeeds(harness, client):
    _enable_auth()
    try:
        r = client.get('/login')
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', r.data.decode()).group(1)
        client.post('/login', data={'password': 'hunter2', 'csrf_token': csrf})

        r2 = client.get('/')
        new_csrf = re.search(r'DRIPARR_CSRF_TOKEN = "([^"]+)"', r2.data.decode()).group(1)

        show_id = harness.add_show('Test Show')
        resp = client.post(f'/toggle-pause/{show_id}', headers={'X-CSRFToken': new_csrf})
        assert resp.status_code == 302
    finally:
        _disable_auth()


def test_logout_clears_the_session(harness, client):
    _enable_auth()
    try:
        r = client.get('/login')
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', r.data.decode()).group(1)
        client.post('/login', data={'password': 'hunter2', 'csrf_token': csrf})
        assert client.get('/').status_code == 200

        client.post('/logout')
        resp = client.get('/', follow_redirects=False)
        assert resp.status_code == 302 and '/login' in resp.headers['Location']
    finally:
        _disable_auth()


# ---------------------------------------------------------------------------
# Exempt paths must always work, logged in or not
# ---------------------------------------------------------------------------

def test_calendar_feed_exempt_from_auth(harness, client):
    _enable_auth()
    try:
        resp = client.get('/calendar.ics')
        assert resp.status_code == 200
    finally:
        _disable_auth()


def test_static_assets_exempt_from_auth(harness, client):
    _enable_auth()
    try:
        assert client.get('/static/style.css').status_code == 200
        assert client.get('/static/driparr.js').status_code == 200
    finally:
        _disable_auth()


def test_health_endpoint_exempt_from_auth(harness, client):
    _enable_auth()
    try:
        resp = client.get('/health')
        assert resp.status_code in (200, 503)  # never 302/401 - a healthcheck must never require login
    finally:
        _disable_auth()


# ---------------------------------------------------------------------------
# Secret key
# ---------------------------------------------------------------------------

def test_secret_key_bootstrap_does_not_touch_disk_under_testing(harness):
    """Regression guard: this used to run unconditionally at import time,
    before any test fixture could redirect DB_FILE - meaning importing
    app.py for test collection would write to whatever DB_PATH the real
    container was started with, before test isolation ever kicked in."""
    key = driparr._get_or_create_secret_key()
    assert key == 'test-secret-key-not-for-production'


# ---------------------------------------------------------------------------
# Password variable: UNBINGE_PASSWORD, with DRIPARR_PASSWORD kept working
# for installs from before the rename.
# ---------------------------------------------------------------------------

def _auth_password_with_env(tmp_path, **env_vars):
    import os, subprocess, sys
    env = {k: v for k, v in os.environ.items() if k not in ('UNBINGE_PASSWORD', 'DRIPARR_PASSWORD')}
    env.update(DRIPARR_TESTING='1', DB_PATH=str(tmp_path / 'env_test.db'), **env_vars)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, '-c', 'import app; print(repr(app.AUTH_PASSWORD))'],
                         cwd=root, env=env, capture_output=True, text=True, check=True)
    return out.stdout.strip().splitlines()[-1]


def test_unbinge_password_variable_enables_auth(tmp_path):
    assert _auth_password_with_env(tmp_path, UNBINGE_PASSWORD='new') == "'new'"


def test_old_driparr_password_variable_still_works(tmp_path):
    assert _auth_password_with_env(tmp_path, DRIPARR_PASSWORD='old') == "'old'"


def test_unbinge_password_wins_when_both_are_set(tmp_path):
    assert _auth_password_with_env(tmp_path, UNBINGE_PASSWORD='new', DRIPARR_PASSWORD='old') == "'new'"
