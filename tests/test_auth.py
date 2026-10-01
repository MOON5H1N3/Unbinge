"""Tests for S2 (CSRF) and S3 (single shared password auth).

Auth is opt-in via the UNBINGE_PASSWORD environment variable - the default
(unset) must leave the app exactly as it always behaved, since making auth
suddenly mandatory on upgrade would lock people out of their own install.

These tests set/clear unbinge.AUTH_PASSWORD directly rather than relying on
environment variables, since the module is already imported by the time any
test runs - the harness fixture in conftest.py resets it to '' before each
test so auth-off is the default test environment, matching the 170+ existing
tests that were written with no concept of auth at all.
"""

import re
from contextlib import closing

import app as unbinge


# ---------------------------------------------------------------------------
# Default (no password configured) - must be a complete no-op
# ---------------------------------------------------------------------------

def test_auth_disabled_by_default(harness):
    assert unbinge.auth_enabled() is False


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
    unbinge.AUTH_PASSWORD = password


def _disable_auth():
    unbinge.AUTH_PASSWORD = ''


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
        new_csrf = re.search(r'UNBINGE_CSRF_TOKEN = "([^"]+)"', r2.data.decode()).group(1)

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
        assert client.get('/static/unbinge.js').status_code == 200
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
    key = unbinge._get_or_create_secret_key()
    assert key == 'test-secret-key-not-for-production'
