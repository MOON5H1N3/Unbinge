"""Tests for F1 (dry-run), F2 (undo), F5 (missed-run detection), and the
TVDB/Sonarr integration (F3/F7).

These were verified live during development with ad-hoc scripts, but those
scripts never made it into the permanent suite - meaning nothing would have
caught a regression in any of them until now. This file closes that gap.

TVDB/Sonarr tests use unittest.mock, since hitting the real APIs isn't
something a test suite should do (rate limits, requires live credentials,
non-deterministic). They verify the request/response HANDLING is correct
against a response shaped like TVDB/Sonarr's documented output - they
cannot verify TVDB/Sonarr actually respond that way, which is what the
"Test Connection" buttons in Settings are for.
"""

import os
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch, MagicMock

import app as unbinge

# The harness fixture in conftest.py stubs send_notification to a no-op for
# every test, to stop any test from accidentally firing a real webhook. That's
# the right default - but it means the tests below, which specifically verify
# send_notification's OWN embed logic, need the real implementation. Captured
# here, at import time, before any fixture has a chance to patch the module
# attribute.
_REAL_SEND_NOTIFICATION = unbinge.send_notification


# ---------------------------------------------------------------------------
# F1 - dry-run mode
# ---------------------------------------------------------------------------

def test_dry_run_does_not_move_files(harness, client):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')

    unbinge.set_setting('dry_run', '1')
    unbinge.run_drip_job(force_show_id=show_id)

    assert harness.episode_tags(harness.vault / 'Test Show') == {(1, 1)}, \
        "dry run must not move the file"
    assert harness.episode_tags(harness.plex / 'Test Show') == set()


def test_dry_run_does_not_mark_episodes_dripped(harness):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')

    unbinge.set_setting('dry_run', '1')
    unbinge.run_drip_job(force_show_id=show_id)

    with closing(unbinge.get_db()) as conn:
        assert unbinge.get_dripped_set(conn, show_id) == set()


def test_dry_run_result_is_labelled(harness):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')

    unbinge.set_setting('dry_run', '1')
    outcome = unbinge.run_drip_job(force_show_id=show_id)

    assert outcome['ran'], "a dry run should still report what it WOULD do"
    _id, _name, result = outcome['ran'][0]
    assert '[DRY RUN]' in result


def test_disabling_dry_run_allows_real_drips_again(harness):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')

    unbinge.set_setting('dry_run', '1')
    unbinge.run_drip_job(force_show_id=show_id)
    unbinge.set_setting('dry_run', '0')
    unbinge.run_drip_job(force_show_id=show_id)

    assert harness.episode_tags(harness.plex / 'Test Show') == {(1, 1)}, \
        "a real drip after disabling dry run should actually move the file"


def test_run_drip_job_summary_matches_dry_run_state(harness, client):
    """Regression guard for the bug caught during development: the
    /api/run-drip summary once said 'dripped' during a dry run while the
    job_runs log correctly said 'previewed', contradicting itself."""
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')

    unbinge.set_setting('dry_run', '1')
    resp = client.post('/api/run-drip')
    message = resp.get_json()['message'].lower()
    assert 'dry run' in message or 'previewed' in message, \
        "summary must not claim a real drip happened while dry_run is on"


# ---------------------------------------------------------------------------
# F5 - missed-run detection
# ---------------------------------------------------------------------------

def test_no_notice_when_never_run(harness):
    assert unbinge.detect_missed_run() is None


def test_no_notice_shortly_after_a_run(harness):
    unbinge.set_setting('last_job_run', unbinge.now_local().isoformat())
    assert unbinge.detect_missed_run() is None


def test_notice_appears_after_a_long_gap(harness):
    old = (unbinge.now_local() - timedelta(hours=40)).isoformat()
    unbinge.set_setting('last_job_run', old)
    notice = unbinge.detect_missed_run()
    assert notice is not None
    assert 'day' in notice.lower()


def test_notice_clears_after_a_fresh_run(harness):
    old = (unbinge.now_local() - timedelta(hours=40)).isoformat()
    unbinge.set_setting('last_job_run', old)
    assert unbinge.detect_missed_run() is not None

    unbinge.set_setting('last_job_run', unbinge.now_local().isoformat())
    assert unbinge.detect_missed_run() is None


def test_missed_run_banner_renders_on_dashboard(harness, client):
    old = (unbinge.now_local() - timedelta(hours=40)).isoformat()
    unbinge.set_setting('last_job_run', old)
    resp = client.get('/')
    assert b'may have been missed' in resp.data


# ---------------------------------------------------------------------------
# F2 - undo last drip
# ---------------------------------------------------------------------------

def test_nothing_undoable_before_any_drip(harness):
    assert unbinge.get_undoable_drip() is None


def test_undo_moves_the_file_back(harness, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')
    drip(show_id)

    undoable = unbinge.get_undoable_drip()
    assert undoable is not None and undoable['show_name'] == 'Test Show'

    ok, _msg = unbinge.undo_drip(undoable['id'])
    assert ok
    assert harness.episode_tags(harness.vault / 'Test Show') == {(1, 1)}
    assert harness.episode_tags(harness.plex / 'Test Show') == set()


def test_undo_clears_the_dripped_marker(harness, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')
    drip(show_id)

    undoable = unbinge.get_undoable_drip()
    unbinge.undo_drip(undoable['id'])

    with closing(unbinge.get_db()) as conn:
        assert unbinge.get_dripped_set(conn, show_id) == set()


def test_redrip_works_cleanly_after_undo(harness, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')
    drip(show_id)
    unbinge.undo_drip(unbinge.get_undoable_drip()['id'])

    drip(show_id)

    assert harness.episode_tags(harness.plex / 'Test Show') == {(1, 1)}


def test_second_undo_reports_nothing_to_undo(harness, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')
    drip(show_id)

    first = unbinge.get_undoable_drip()
    unbinge.undo_drip(first['id'])

    assert unbinge.get_undoable_drip() is None
    ok, msg = unbinge.undo_drip(first['id'])
    assert not ok
    assert 'already' in msg.lower() or 'nothing' in msg.lower()


def test_undo_reopens_cooldown_if_that_drip_emptied_the_vault(harness, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')
    drip(show_id)  # moves the only episode
    drip(show_id)  # vault now empty -> starts cooldown

    assert harness.get_show(show_id)['completed_at'] is not None

    # The undoable drip is still the original episode move (cooldown itself
    # isn't a 'dripped' action, so it's not what get_undoable_drip returns).
    undoable = unbinge.get_undoable_drip()
    unbinge.undo_drip(undoable['id'])

    assert harness.get_show(show_id)['completed_at'] is None, \
        "undoing the drip that emptied the vault should reopen cooldown"


def test_undo_refuses_for_a_deleted_show(harness, client, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')
    drip(show_id)
    undoable = unbinge.get_undoable_drip()

    client.post(f'/delete/{show_id}')

    ok, msg = unbinge.undo_drip(undoable['id'])
    assert not ok
    assert 'no longer active' in msg.lower() or 'deleted' in msg.lower() or 'graduated' in msg.lower()


def test_undo_api_route_end_to_end(harness, client, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')
    drip(show_id)

    r = client.get('/api/undo-last-drip')
    assert r.get_json()['undoable'] is not None

    r = client.post('/api/undo-last-drip')
    assert r.status_code == 200

    r = client.get('/api/undo-last-drip')
    assert r.get_json()['undoable'] is None


# ---------------------------------------------------------------------------
# F3 - TVDB integration (mocked - see module docstring)
# ---------------------------------------------------------------------------

def test_tvdb_search_with_no_api_key_fails_cleanly(harness, client):
    resp = client.get('/api/tvdb-search?q=Test')
    assert resp.status_code == 502
    assert 'API key' in resp.get_json()['message']


def test_tvdb_test_connection_uses_form_value_not_saved_setting(harness, client):
    """Regression guard: Test Connection originally read the saved setting,
    not the live form field, so a typed-but-unsaved key silently tested an
    empty string with zero corresponding log output."""
    assert unbinge.get_setting('tvdb_api_key', '') == ''

    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = lambda: {'data': {'token': 'test.token'}}

    with patch.object(unbinge.requests, 'post', return_value=mock_resp) as mock_post:
        resp = client.post('/api/test-tvdb', data={'tvdb_api_key': 'typed-not-saved', 'tvdb_pin': ''})
        assert resp.status_code == 200
        assert mock_post.call_args.kwargs['json']['apikey'] == 'typed-not-saved'

    # Testing must not have side effects on saved settings
    assert unbinge.get_setting('tvdb_api_key', '') == ''
    assert unbinge.get_setting('tvdb_token', '') == ''


def test_tvdb_search_parses_mocked_results(harness, client):
    unbinge.set_setting('tvdb_api_key', 'fake-key')
    mock_login = MagicMock()
    mock_login.raise_for_status = lambda: None
    mock_login.json = lambda: {'data': {'token': 'fake.token'}}
    mock_search = MagicMock()
    mock_search.raise_for_status = lambda: None
    mock_search.json = lambda: {'data': [
        {'tvdb_id': '999', 'name': 'Mocked Show', 'year': '2021', 'image_url': 'http://example/x.jpg'},
    ]}
    with patch.object(unbinge.requests, 'post', return_value=mock_login), \
         patch.object(unbinge.requests, 'get', return_value=mock_search):
        resp = client.get('/api/tvdb-search?q=Mocked')
        data = resp.get_json()
        assert data['status'] == 'success'
        assert data['results'][0]['tvdb_id'] == 999
        assert data['results'][0]['name'] == 'Mocked Show'


def test_tvdb_link_stores_poster_and_id(harness, client):
    show_id = harness.add_show('Test Show')
    unbinge.set_setting('tvdb_api_key', 'fake-key')

    mock_login = MagicMock()
    mock_login.raise_for_status = lambda: None
    mock_login.json = lambda: {'data': {'token': 'fake.token'}}
    mock_series = MagicMock()
    mock_series.raise_for_status = lambda: None
    mock_series.json = lambda: {'data': {'name': 'Test Show', 'image': 'http://example/poster.jpg'}}
    mock_episodes = MagicMock()
    mock_episodes.raise_for_status = lambda: None
    mock_episodes.json = lambda: {'data': {'episodes': [
        {'seasonNumber': 1, 'number': 1}, {'seasonNumber': 0, 'number': 1},
    ]}}
    # Local poster caching (added after this test was first written) means
    # api_tvdb_link makes a THIRD get call to actually download the image,
    # beyond the series-details and episode-list calls above.
    mock_image = MagicMock()
    mock_image.raise_for_status = lambda: None
    mock_image.iter_content = lambda chunk_size: [b'\xff\xd8\xff\xe0FAKE']

    with patch.object(unbinge.requests, 'post', return_value=mock_login), \
         patch.object(unbinge.requests, 'get', side_effect=[mock_series, mock_episodes, mock_image]):
        resp = client.post(f'/api/tvdb-link/{show_id}', data={'tvdb_id': '555'})
        data = resp.get_json()
        assert data['status'] == 'success'
        # poster_url in the response is now the LOCAL cached path, not the
        # original TVDB hotlink - that's the whole point of caching it.
        assert data['poster_url'].startswith('/posters/')
        assert '0' not in data['season_episode_counts'], "specials must be excluded"

    with closing(unbinge.get_db()) as conn:
        row = conn.execute(f"SELECT tvdb_id, poster_url FROM {unbinge.TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
    assert row['tvdb_id'] == 555
    assert row['poster_url'].startswith('/posters/')


def test_tvdb_unlink_clears_both_fields(harness, client):
    show_id = harness.add_show('Test Show')
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET tvdb_id = 1, poster_url = 'x' WHERE id = ?", (show_id,))
        conn.commit()

    client.post(f'/api/tvdb-unlink/{show_id}')

    with closing(unbinge.get_db()) as conn:
        row = conn.execute(f"SELECT tvdb_id, poster_url FROM {unbinge.TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
    assert row['tvdb_id'] is None and row['poster_url'] is None


def test_dashboard_renders_linked_poster(harness, client):
    show_id = harness.add_show('Test Show')
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET poster_url = ? WHERE id = ?",
                     ('http://example/mypic.jpg', show_id))
        conn.commit()

    resp = client.get('/')
    assert b'http://example/mypic.jpg' in resp.data


# ---------------------------------------------------------------------------
# F7 - Sonarr integration (mocked, optional feature)
# ---------------------------------------------------------------------------

def test_sonarr_not_configured_fails_cleanly(harness, client):
    resp = client.post('/api/test-sonarr')
    assert resp.status_code == 400
    assert 'required' in resp.get_json()['message'].lower()


def test_sonarr_test_connection_mocked_success(harness, client):
    def fake_get(url, **kw):
        r = MagicMock()
        r.raise_for_status = lambda: None
        r.json = (lambda: {'version': '4.0.0'}) if url.endswith('/system/status') else (lambda: [])
        return r
    with patch.object(unbinge.requests, 'get', side_effect=fake_get):
        resp = client.post('/api/test-sonarr', data={
            'sonarr_url': 'http://fake-sonarr:8989', 'sonarr_api_key': 'fake-key',
        })
        assert resp.status_code == 200


def test_sonarr_get_series_excludes_specials(harness):
    unbinge.set_setting('sonarr_url', 'http://fake-sonarr:8989')
    unbinge.set_setting('sonarr_api_key', 'fake-key')

    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = lambda: [{
        'id': 42, 'title': 'Test Show', 'tvdbId': 555,
        'seasons': [
            {'seasonNumber': 0, 'statistics': {'totalEpisodeCount': 3}},
            {'seasonNumber': 1, 'statistics': {'totalEpisodeCount': 10}},
        ],
    }]
    with patch.object(unbinge.requests, 'get', return_value=mock_resp):
        details, err = unbinge.sonarr_get_series(555)
        assert err is None
        assert details['season_episode_counts'] == {1: 10}


# ---------------------------------------------------------------------------
# New: local poster caching
# ---------------------------------------------------------------------------

def test_cache_poster_locally_downloads_and_saves(harness):
    show_id = harness.add_show('Test Show')
    fake_bytes = b'\xff\xd8\xff\xe0FAKEJPEG'
    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.iter_content = lambda chunk_size: [fake_bytes]

    with patch.object(unbinge.requests, 'get', return_value=mock_resp):
        local_url = unbinge.cache_poster_locally(show_id, 'http://example.com/poster.jpg')

    assert local_url.startswith('/posters/')
    cached_path = os.path.join(unbinge.POSTERS_DIR, os.path.basename(local_url))
    assert os.path.isfile(cached_path)
    assert open(cached_path, 'rb').read() == fake_bytes


def test_cache_poster_locally_falls_back_on_failure(harness):
    show_id = harness.add_show('Test Show')
    with patch.object(unbinge.requests, 'get', side_effect=unbinge.requests.RequestException("boom")):
        result = unbinge.cache_poster_locally(show_id, 'http://example.com/poster.jpg')
    assert result == 'http://example.com/poster.jpg', \
        "a failed download should fall back to the original hotlink, not lose the poster entirely"


def test_cached_poster_is_served_by_its_route(harness, client):
    show_id = harness.add_show('Test Show')
    fake_bytes = b'\xff\xd8\xff\xe0FAKEJPEG'
    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.iter_content = lambda chunk_size: [fake_bytes]
    with patch.object(unbinge.requests, 'get', return_value=mock_resp):
        local_url = unbinge.cache_poster_locally(show_id, 'http://example.com/poster.jpg')

    resp = client.get(local_url)
    assert resp.status_code == 200
    assert resp.data == fake_bytes


def test_delete_cached_poster_removes_the_file(harness):
    show_id = harness.add_show('Test Show')
    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.iter_content = lambda chunk_size: [b'x']
    with patch.object(unbinge.requests, 'get', return_value=mock_resp):
        local_url = unbinge.cache_poster_locally(show_id, 'http://example.com/poster.jpg')
    cached_path = os.path.join(unbinge.POSTERS_DIR, os.path.basename(local_url))
    assert os.path.isfile(cached_path)

    unbinge.delete_cached_poster(local_url)
    assert not os.path.isfile(cached_path)


def test_delete_cached_poster_ignores_hotlink_urls(harness):
    """A poster that failed to cache (still a raw hotlink) shouldn't cause
    delete_cached_poster to try deleting something that isn't a local file."""
    unbinge.delete_cached_poster('http://example.com/never-cached.jpg')  # must not raise


def test_unlinking_a_show_removes_its_cached_poster_file(harness, client):
    show_id = harness.add_show('Test Show')
    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.iter_content = lambda chunk_size: [b'x']
    with patch.object(unbinge.requests, 'get', return_value=mock_resp):
        local_url = unbinge.cache_poster_locally(show_id, 'http://example.com/poster.jpg')
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET poster_url = ? WHERE id = ?", (local_url, show_id))
        conn.commit()
    cached_path = os.path.join(unbinge.POSTERS_DIR, os.path.basename(local_url))

    client.post(f'/api/tvdb-unlink/{show_id}')

    assert not os.path.isfile(cached_path)


# ---------------------------------------------------------------------------
# New: disk space check on promote
# ---------------------------------------------------------------------------

def test_inspect_warns_when_show_exceeds_free_space(harness, client):
    show = harness.pool / 'BigShow'
    show.mkdir(parents=True)
    (show / 'BigShow - S01E01.mkv').write_bytes(b'x' * 5000)

    with patch.object(unbinge.shutil, 'disk_usage', return_value=type('D', (), {'free': 1000})()):
        resp = client.get('/api/inspect?show=BigShow')
        data = resp.get_json()
        assert data['space_warning'] is not None
        assert 'free' in data['space_warning'].lower()


def test_inspect_does_not_warn_when_space_is_sufficient(harness, client):
    show = harness.pool / 'SmallShow'
    show.mkdir(parents=True)
    (show / 'SmallShow - S01E01.mkv').write_bytes(b'x' * 100)

    with patch.object(unbinge.shutil, 'disk_usage', return_value=type('D', (), {'free': 999999999})()):
        resp = client.get('/api/inspect?show=SmallShow')
        data = resp.get_json()
        assert data['space_warning'] is None


# ---------------------------------------------------------------------------
# New: database backup
# ---------------------------------------------------------------------------

def test_backup_db_returns_a_valid_sqlite_file(harness, client):
    harness.add_show('Backup Test Show')

    resp = client.get('/api/backup-db')
    assert resp.status_code == 200
    assert resp.mimetype == 'application/x-sqlite3'

    tmp_path = os.path.join(str(harness.root), 'backup_check.db')
    with open(tmp_path, 'wb') as f:
        f.write(resp.data)
    import sqlite3
    check_conn = sqlite3.connect(tmp_path)
    names = [r[0] for r in check_conn.execute(f"SELECT show_name FROM {unbinge.TABLE_NAME}").fetchall()]
    check_conn.close()
    assert 'Backup Test Show' in names


# ---------------------------------------------------------------------------
# New: Discord poster embeds
# ---------------------------------------------------------------------------

def test_discord_notification_includes_poster_embed(harness):
    unbinge.set_setting('webhook_url', 'https://discord.com/api/webhooks/12345/abcdefTOKEN')
    mock_resp = MagicMock()
    with patch.object(unbinge.requests, 'post', return_value=mock_resp) as p:
        _REAL_SEND_NOTIFICATION('dripped', "test message", poster_url='http://example/poster.jpg')
        payload = p.call_args.kwargs['json']
    assert 'embeds' in payload
    assert payload['embeds'][0]['thumbnail']['url'] == 'http://example/poster.jpg'
    assert payload['content'] == "test message", "plain content should still be sent alongside the embed"


def test_non_discord_webhook_gets_no_embed(harness):
    unbinge.set_setting('webhook_url', 'https://hooks.slack.com/services/FAKE')
    mock_resp = MagicMock()
    with patch.object(unbinge.requests, 'post', return_value=mock_resp) as p:
        _REAL_SEND_NOTIFICATION('dripped', "test", poster_url='http://example/poster.jpg')
        payload = p.call_args.kwargs['json']
    assert 'embeds' not in payload


def test_discord_webhook_without_poster_sends_plain_message(harness):
    unbinge.set_setting('webhook_url', 'https://discord.com/api/webhooks/12345/abcdefTOKEN')
    mock_resp = MagicMock()
    with patch.object(unbinge.requests, 'post', return_value=mock_resp) as p:
        _REAL_SEND_NOTIFICATION('dripped', "test", poster_url=None)
        payload = p.call_args.kwargs['json']
    assert 'embeds' not in payload


def test_dripped_notification_carries_the_shows_poster(harness, drip):
    """End-to-end: a real drip on a show with a linked poster should pass
    that poster through to send_notification."""
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET poster_url = ? WHERE id = ?",
                     ('/posters/1.jpg', show_id))
        conn.commit()

    captured = {}
    def fake_notify(event, message, extra=None, poster_url=None):
        captured['poster_url'] = poster_url
    original = unbinge.send_notification
    unbinge.send_notification = fake_notify
    try:
        drip(show_id)
    finally:
        unbinge.send_notification = original

    assert captured.get('poster_url') == '/posters/1.jpg'


# ---------------------------------------------------------------------------
# New: schedule page poster propagation
# ---------------------------------------------------------------------------

def test_project_schedule_carries_poster_url(harness):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET poster_url = ? WHERE id = ?",
                     ('/posters/99.jpg', show_id))
        conn.commit()

    events = unbinge.project_schedule(weeks_ahead=2)
    assert events, "expected at least one projected event"
    assert all(e.get('poster_url') == '/posters/99.jpg' for e in events if e['show_name'] == 'Test Show')


def test_schedule_page_renders_poster(harness, client):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET poster_url = ? WHERE id = ?",
                     ('/posters/77.jpg', show_id))
        conn.commit()

    resp = client.get('/schedule?weeks=2')
    assert resp.status_code == 200
    assert b'/posters/77.jpg' in resp.data


# ---------------------------------------------------------------------------
# Next-run preview
# ---------------------------------------------------------------------------

def test_next_run_plan_lists_due_shows_without_moving_files(harness, client):
    show_dir = harness.vault / 'Plan Show'
    harness.make_episode(show_dir, 'Plan Show', 1, 1, subdir='Season 01')
    harness.add_show('Plan Show', release_days='0,1,2,3,4,5,6')
    harness.add_show('Paused Show', release_days='0,1,2,3,4,5,6', paused=1)
    before = harness.files_under(harness.vault)
    plan = unbinge.build_next_run_plan()
    assert [i['name'] for i in plan['items']] == ['Plan Show']
    assert plan['items'][0]['action'] == 'drip'
    assert plan['idle'] == 1
    assert harness.files_under(harness.vault) == before
    assert client.get('/api/next-run').get_json()['items'][0]['name'] == 'Plan Show'
    assert client.get('/system').status_code == 200


# ---------------------------------------------------------------------------
# Sonarr: stop monitoring waiting episodes, undo last path change
# ---------------------------------------------------------------------------

class _FakeSonarr:
    """Just enough of Sonarr's v3 API: series list/get/put, episode list, monitor."""
    def __init__(self, path):
        self.series = {7: {'id': 7, 'title': 'Slow Show', 'tvdbId': 99, 'path': path}}
        self.episodes = [
            {'id': 1, 'seasonNumber': 1, 'episodeNumber': 1, 'monitored': True},
            {'id': 2, 'seasonNumber': 1, 'episodeNumber': 2, 'monitored': True},
            {'id': 3, 'seasonNumber': 1, 'episodeNumber': 3, 'monitored': False},
        ]
        self.monitor_calls = []

    def _resp(self, data):
        r = MagicMock(); r.raise_for_status = lambda: None; r.json = lambda: data; return r

    def get(self, url, **kw):
        if url.endswith('/api/v3/series'): return self._resp(list(self.series.values()))
        if '/api/v3/series/' in url: return self._resp(dict(self.series[int(url.rsplit('/', 1)[1])]))
        if url.endswith('/api/v3/episode'): return self._resp(self.episodes)
        raise AssertionError(url)

    def put(self, url, json=None, **kw):
        if url.endswith('/api/v3/episode/monitor'):
            self.monitor_calls.append(json)
            for ep in self.episodes:
                if ep['id'] in json['episodeIds']: ep['monitored'] = json['monitored']
        else:
            self.series[int(url.rsplit('/', 1)[1])]['path'] = json['path']
        return self._resp({})


def _sonarr_form(confirm=False):
    d = {'sonarr_url': 'http://fake:8989', 'sonarr_api_key': 'k', 'sonarr_target': 'vault'}
    if confirm: d['confirm'] = '1'
    return d


def test_sonarr_unmonitor_only_touches_waiting_monitored_episodes(harness, client):
    show_dir = harness.vault / 'Slow Show'
    harness.make_episode(show_dir, 'Slow Show', 1, 2, subdir='Season 01')   # waiting
    harness.make_episode(show_dir, 'Slow Show', 1, 3, subdir='Season 01')   # waiting, already unmonitored
    harness.add_show('Slow Show')
    fake = _FakeSonarr(str(show_dir))
    with patch.object(unbinge.requests, 'get', side_effect=fake.get), \
         patch.object(unbinge.requests, 'put', side_effect=fake.put):
        preview = client.post('/api/sonarr/unmonitor', data=_sonarr_form()).get_json()
        assert preview['plan'] == [{'name': 'Slow Show', 'show_id': 1, 'series_id': 7, 'count': 1}]
        assert fake.monitor_calls == []                       # preview changes nothing
        done = client.post('/api/sonarr/unmonitor', data=_sonarr_form(True)).get_json()
    assert done['done'] == 1
    assert fake.monitor_calls == [{'episodeIds': [2], 'monitored': False}]
    assert [e['monitored'] for e in fake.episodes] == [True, False, False]   # ep 1 (not waiting) untouched
    assert 'sonarr' in harness.history_actions()


def test_sonarr_undo_path_restores_previous_path_once(harness, client):
    sid = harness.add_show('Slow Show')
    with closing(unbinge.get_db()) as conn:
        unbinge.log_history(conn, sid, 'Slow Show', 'sonarr',
                            "Sonarr series path /tv/pool/Slow Show -> /tv/plex/Slow Show (matched by path, no files moved)")
        conn.commit()
    fake = _FakeSonarr('/tv/plex/Slow Show')
    with patch.object(unbinge.requests, 'get', side_effect=fake.get), \
         patch.object(unbinge.requests, 'put', side_effect=fake.put):
        plan = client.post('/api/sonarr/undo-path', data=_sonarr_form()).get_json()['plan']
        assert plan[0]['from'] == '/tv/pool/Slow Show'
        assert client.post('/api/sonarr/undo-path', data=_sonarr_form(True)).get_json()['status'] == 'success'
        assert fake.series[7]['path'] == '/tv/pool/Slow Show'
        # the undo is recorded, so a second press has nothing left to undo
        assert client.post('/api/sonarr/undo-path', data=_sonarr_form()).get_json()['plan'] == []


# ---------------------------------------------------------------------------
# Bulk edit and history filters
# ---------------------------------------------------------------------------

def test_bulk_edit_changes_day_and_per_drop_and_logs_it(harness, client):
    a = harness.add_show('Show A', release_days='0')
    b = harness.add_show('Show B', release_days='1,2', episodes_per_drop=1)
    r = client.post('/api/bulk/edit', json={'show_ids': [a, b], 'release_day': '4', 'episodes_per_drop': '3'})
    assert r.get_json()['status'] == 'success'
    for sid in (a, b):
        row = harness.get_show(sid)
        assert row['release_days'] == '4' and row['episodes_per_drop'] == 3
    assert harness.history_actions().count('edited') == 2


def test_bulk_edit_validates_and_leaves_unspecified_fields_alone(harness, client):
    a = harness.add_show('Show A', release_days='2', episodes_per_drop=2)
    assert client.post('/api/bulk/edit', json={'show_ids': [a], 'release_day': '9'}).status_code == 400
    assert client.post('/api/bulk/edit', json={'show_ids': [a]}).status_code == 400
    assert client.post('/api/bulk/edit', json={'show_ids': [a], 'episodes_per_drop': '0'}).status_code == 400
    client.post('/api/bulk/edit', json={'show_ids': [a], 'episodes_per_drop': '5'})
    row = harness.get_show(a)
    assert row['release_days'] == '2' and row['episodes_per_drop'] == 5


def test_history_days_and_text_filters(harness, client):
    with closing(unbinge.get_db()) as conn:
        old = (unbinge.now_local() - timedelta(days=30)).isoformat()
        conn.execute(f"INSERT INTO {unbinge.HISTORY_TABLE} (show_id, show_name, action, detail, occurred_at) VALUES (1,'Old Show','dripped','old thing',?)", (old,))
        unbinge.log_history(conn, 2, 'New Show', 'dripped', 'fresh thing')
        conn.commit()
    body = client.get('/history?action=dripped&days=7').get_data(as_text=True)
    assert 'New Show' in body and 'Old Show' not in body
    body = client.get('/history?q=old+thing').get_data(as_text=True)
    assert 'Old Show' in body and 'New Show' not in body


def test_static_files_are_fingerprinted_and_cacheable(harness, client):
    html = client.get('/').get_data(as_text=True)
    import re as _re
    m = _re.search(r'/static/style\.css\?v=(\d+)', html)
    assert m, "stylesheet link should carry a modification-time fingerprint"
    r = client.get(f'/static/style.css?v={m.group(1)}')
    assert r.status_code == 200 and 'max-age=31536000' in r.headers.get('Cache-Control', '')


# ---------------------------------------------------------------------------
# "Up next" recommendations
# ---------------------------------------------------------------------------

def _fake_tvdb(monkeypatch, genres_by_name):
    """Pool folder 'X' -> TVDB id from its position; genres from genres_by_name
    (default drama). Counts network lookups so cache behaviour is visible."""
    ids = {}
    calls = {'search': 0, 'genres': 0}

    def search(query):
        calls['search'] += 1
        ids.setdefault(query, 1000 + len(ids))
        return [{'tvdb_id': ids[query], 'name': query, 'year': '', 'image_url': '', 'overview': ''}], None

    def genres(tvdb_id):
        calls['genres'] += 1
        name = next(n for n, i in ids.items() if i == tvdb_id) if tvdb_id in ids.values() else 'ANCHOR'
        return genres_by_name.get(name, ['drama']), '', None

    monkeypatch.setattr(unbinge, 'tvdb_search_series', search)
    monkeypatch.setattr(unbinge, 'tvdb_get_series_genres', genres)
    return calls


def _anchor_show(harness, genres_by_name, tvdb_id=1):
    genres_by_name  # anchor genres come from the id lookup in _fake_tvdb ('ANCHOR')
    sid = harness.add_show('Anchor Show')
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET tvdb_id = ? WHERE id = ?", (tvdb_id, sid))
        conn.commit()
    return sid


def test_recommendations_reach_past_the_first_40_pool_folders(harness, monkeypatch):
    names = [f"Pool Show {i:02d}" for i in range(60)]
    for n in names:
        harness.make_show_in_pool(n, [(1, 1)])
    genres = {'ANCHOR': ['crime', 'drama'], 'Pool Show 59': ['crime', 'drama']}
    _fake_tvdb(monkeypatch, genres)
    sid = _anchor_show(harness, genres)
    found = []
    for _ in range(4):  # the cap limits fresh lookups per request, so it fills in over a few loads
        rec = unbinge.get_recommendations_for_show(sid)
        found = [r['name'] for r in rec['recommendations']]
        if 'Pool Show 59' in found:
            break
    assert 'Pool Show 59' in found, "a match late in the alphabet must still be found"
    assert rec['unindexed'] == 0


def test_cached_pool_folders_cost_no_further_lookups(harness, monkeypatch):
    for n in ('Alpha', 'Beta'):
        harness.make_show_in_pool(n, [(1, 1)])
    calls = _fake_tvdb(monkeypatch, {'ANCHOR': ['crime']})
    sid = _anchor_show(harness, {})
    unbinge.get_recommendations_for_show(sid)
    before = dict(calls)
    unbinge.get_recommendations_for_show(sid)
    assert calls == before


def test_halloween_nudge_applies_only_in_season(harness, monkeypatch):
    harness.make_show_in_pool('Spooky Hollow', [(1, 1)])
    harness.make_show_in_pool('Sunny Days', [(1, 1)])
    genres = {'ANCHOR': ['comedy'], 'Spooky Hollow': ['horror'], 'Sunny Days': ['comedy']}
    _fake_tvdb(monkeypatch, genres)
    sid = _anchor_show(harness, genres)
    from datetime import date
    october = unbinge.get_recommendations_for_show(sid, today=date(2026, 10, 18))
    names = [r['name'] for r in october['recommendations']]
    assert october['season'] == 'Halloween' and 'Spooky Hollow' in names
    assert next(r for r in october['recommendations'] if r['name'] == 'Spooky Hollow')['season'] == 'Halloween'
    june = unbinge.get_recommendations_for_show(sid, today=date(2026, 6, 1))
    assert june['season'] == 'Summer'                       # a broad season, not the Halloween event
    assert [r['name'] for r in june['recommendations']] == ['Sunny Days']   # the horror show isn't pulled in


def test_strong_genre_match_can_still_beat_the_seasonal_bonus(harness, monkeypatch):
    harness.make_show_in_pool('Spooky Hollow', [(1, 1)])
    harness.make_show_in_pool('Crime Saga', [(1, 1)])
    harness.make_show_in_pool('Filler One', [(1, 1)])
    genres = {'ANCHOR': ['crime', 'mystery', 'noir'], 'Spooky Hollow': ['horror'],
              'Crime Saga': ['crime', 'mystery', 'noir'], 'Filler One': ['sports']}
    _fake_tvdb(monkeypatch, genres)
    sid = _anchor_show(harness, genres)
    from datetime import date
    rec = unbinge.get_recommendations_for_show(sid, today=date(2026, 10, 18))
    assert [r['name'] for r in rec['recommendations']][0] == 'Crime Saga'


def test_ties_reshuffle_week_to_week_not_alphabetically(harness, monkeypatch):
    names = [f"Tie {c}" for c in 'ABCDEFGH']
    for n in names:
        harness.make_show_in_pool(n, [(1, 1)])
    _fake_tvdb(monkeypatch, {'ANCHOR': ['crime'], **{n: ['crime'] for n in names}})
    sid = _anchor_show(harness, {})
    from datetime import date
    wk1 = [r['name'] for r in unbinge.get_recommendations_for_show(sid, today=date(2026, 3, 2))['recommendations']]
    same_week = [r['name'] for r in unbinge.get_recommendations_for_show(sid, today=date(2026, 3, 4))['recommendations']]
    wk2 = [r['name'] for r in unbinge.get_recommendations_for_show(sid, today=date(2026, 3, 9))['recommendations']]
    assert wk1 == same_week          # stable within a week
    assert wk1 != wk2                # changes the next week
    assert wk1 != sorted(wk1)        # not just the alphabet


def test_already_queued_shows_are_not_suggested(harness, monkeypatch):
    for n in ('Alpha', 'Beta'):
        harness.make_show_in_pool(n, [(1, 1)])
    _fake_tvdb(monkeypatch, {'ANCHOR': ['crime'], 'Alpha': ['crime'], 'Beta': ['crime']})
    sid = _anchor_show(harness, {})
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"""INSERT INTO {unbinge.QUEUE_TABLE}
            (show_name, trigger_type, trigger_show_id, release_days, episodes_per_drop, created_at)
            VALUES ('Alpha', 'after_show', ?, '0', 1, ?)""", (sid, unbinge.now_local().isoformat()))
        conn.commit()
    names = [r['name'] for r in unbinge.get_recommendations_for_show(sid)['recommendations']]
    assert names == ['Beta']


def test_warns_about_shows_ending_with_nothing_queued(harness, client):
    show_dir = harness.vault / 'Ending Show'
    harness.make_episode(show_dir, 'Ending Show', 1, 1, subdir='Season 01')
    sid = harness.add_show('Ending Show', release_days='0,1,2,3,4,5,6')
    assert [x['name'] for x in unbinge.shows_ending_without_successor()] == ['Ending Show']
    assert 'nothing is queued to follow' in client.get('/').get_data(as_text=True)
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"""INSERT INTO {unbinge.QUEUE_TABLE}
            (show_name, trigger_type, trigger_show_id, release_days, episodes_per_drop, created_at)
            VALUES ('Next One', 'after_show', ?, '0', 1, ?)""", (sid, unbinge.now_local().isoformat()))
        conn.commit()
    assert unbinge.shows_ending_without_successor() == []
    assert 'nothing is queued to follow' not in client.get('/').get_data(as_text=True)


def test_seasonal_picks_take_at_most_two_of_five_slots(harness, monkeypatch):
    for i in range(6):
        harness.make_show_in_pool(f"Haunted {i}", [(1, 1)])
    for i in range(4):
        harness.make_show_in_pool(f"Plain Crime {i}", [(1, 1)])
    genres = {'ANCHOR': ['crime'], **{f"Haunted {i}": ['horror'] for i in range(6)},
              **{f"Plain Crime {i}": ['crime'] for i in range(4)}}
    _fake_tvdb(monkeypatch, genres)
    sid = _anchor_show(harness, genres)
    from datetime import date
    recs = unbinge.get_recommendations_for_show(sid, today=date(2026, 10, 18))['recommendations']
    assert len(recs) == 5
    assert sum(1 for r in recs if r['season']) == 2


# ---------------------------------------------------------------------------
# Ended series skip the cooldown
# ---------------------------------------------------------------------------

def _finish_show(harness, drip, name='Done Show'):
    show_dir = harness.vault / name
    harness.make_episode(show_dir, name, 1, 1, subdir='Season 01')
    sid = harness.add_show(name, release_days='0,1,2,3,4,5,6')
    drip(sid)  # releases the only episode
    return sid


def test_ended_series_graduates_straight_away_instead_of_cooling_down(harness, drip, monkeypatch):
    monkeypatch.setattr(unbinge, 'series_known_ended', lambda show: (True, 'Sonarr'))
    sid = _finish_show(harness, drip)
    result = drip(sid)                       # vault empty -> would normally start a 7-day cooldown
    assert 'graduated back' in result
    assert harness.get_show(sid) is None
    assert (harness.pool / 'Done Show').exists()
    assert 'graduated' in harness.history_actions()


def test_continuing_series_still_cools_down(harness, drip, monkeypatch):
    monkeypatch.setattr(unbinge, 'series_known_ended', lambda show: (False, 'Sonarr'))
    sid = _finish_show(harness, drip)
    assert 'cooldown started' in drip(sid)
    assert harness.get_show(sid)['completed_at']


def test_nightly_sweep_returns_already_cooling_ended_shows(harness, drip, monkeypatch):
    sid = _finish_show(harness, drip)
    monkeypatch.setattr(unbinge, 'series_known_ended', lambda show: (False, None))
    drip(sid)                                # now cooling down
    assert harness.get_show(sid)['completed_at']
    monkeypatch.setattr(unbinge, 'series_known_ended', lambda show: (True, 'TVDB'))
    with closing(unbinge.get_db()) as conn:
        assert unbinge.graduate_ended_cooldown_shows(conn) == ['Done Show']
    assert harness.get_show(sid) is None


def test_return_to_pool_now_button_endpoint(harness, drip, client, monkeypatch):
    monkeypatch.setattr(unbinge, 'series_known_ended', lambda show: (False, None))
    sid = _finish_show(harness, drip)
    assert client.post(f'/api/return-to-pool/{sid}').status_code == 400   # still dripping -> refused
    drip(sid)                                                              # cooldown starts
    r = client.post(f'/api/return-to-pool/{sid}').get_json()
    assert r['status'] == 'success' and r['graduated'] is True
    assert (harness.pool / 'Done Show').exists()


def test_sonarr_ended_status_detected(harness, monkeypatch):
    unbinge.set_setting('sonarr_url', 'http://fake:8989')
    unbinge.set_setting('sonarr_api_key', 'k')
    series = [{'id': 1, 'title': 'Done Show', 'tvdbId': 0, 'path': '/x', 'status': 'ended'}]
    monkeypatch.setattr(unbinge, 'sonarr_fetch_series', lambda *a, **k: series)
    show = {'show_name': 'Done Show', 'tvdb_id': None, 'vault_path': '/v/Done Show', 'plex_path': '/p/Done Show'}
    assert unbinge.series_known_ended(show) == (True, 'Sonarr')
    series[0]['status'] = 'continuing'
    assert unbinge.series_known_ended(show) == (False, 'Sonarr')


def test_broad_seasons_only_nudge_shows_that_already_match(harness, monkeypatch):
    for n in ('Beach Capers', 'Dark Docket', 'Unrelated Cooking'):
        harness.make_show_in_pool(n, [(1, 1)])
    genres = {'ANCHOR': ['crime', 'drama'], 'Beach Capers': ['comedy', 'crime'],
              'Dark Docket': ['crime', 'drama'], 'Unrelated Cooking': ['comedy']}
    _fake_tvdb(monkeypatch, genres)
    sid = _anchor_show(harness, genres)
    from datetime import date
    summer = unbinge.get_recommendations_for_show(sid, today=date(2026, 7, 10))
    names = [r['name'] for r in summer['recommendations']]
    assert summer['season'] == 'Summer'
    assert 'Unrelated Cooking' not in names            # no genre overlap: a season alone doesn't add it
    assert next(r for r in summer['recommendations'] if r['name'] == 'Beach Capers')['season'] == 'Summer'


def test_season_windows(harness):
    from datetime import date
    label = lambda m, d: (unbinge.current_seasonal_theme(date(2026, m, d)) or {}).get('label')
    assert label(10, 25) == 'Halloween' and label(9, 5) == 'Autumn' and label(11, 10) == 'Autumn'
    assert label(12, 25) == 'Christmas' and label(1, 1) == 'Christmas' and label(1, 20) == 'Winter'
    assert label(2, 10) == "Valentine's" and label(2, 20) == 'Winter'
    assert label(4, 15) == 'Spring' and label(7, 4) == 'Summer'


# ---------------------------------------------------------------------------
# Balance the week
# ---------------------------------------------------------------------------

def _balance_setup(harness, monkeypatch, spec):
    """spec: [(name, day, genres)] -> shows on those days with those genres."""
    genres_by_id = {}
    for n, (name, day, genres) in enumerate(spec, start=100):
        sid = harness.add_show(name, release_days=str(day))
        with closing(unbinge.get_db()) as conn:
            conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET tvdb_id = ? WHERE id = ?", (n, sid))
            conn.commit()
        genres_by_id[n] = genres
    monkeypatch.setattr(unbinge, 'get_genre_data_for_tvdb_id',
                        lambda conn, tid: {'genres': genres_by_id.get(tid, []), 'poster_url': ''})


def test_balance_spreads_a_pile_up(harness, monkeypatch):
    _balance_setup(harness, monkeypatch, [(f"Show {c}", 0, []) for c in 'ABCDEF'])
    plan = unbinge.plan_week_balance()
    assert plan['before'][0] == 6
    assert max(plan['after']) == 1 and sum(plan['after']) == 6
    assert len(plan['moves']) == 5            # one show can stay put
    assert all('Mon had 6' in m['why'] or m['why'] for m in plan['moves'])


def test_balance_leaves_an_even_week_alone(harness, monkeypatch):
    _balance_setup(harness, monkeypatch, [(f"Show {d}", d, []) for d in range(7)])
    assert unbinge.plan_week_balance()['moves'] == []


def test_balance_separates_similar_shows(harness, monkeypatch):
    spec = [("Crime A", 0, ['crime', 'noir']), ("Crime B", 1, ['crime', 'noir']),
            ("Cooking", 2, ['food']), ("Sport", 3, ['sports']), ("Space", 4, ['scifi']),
            ("Kids", 5, ['animation']), ("News", 6, ['news'])]
    _balance_setup(harness, monkeypatch, spec)
    plan = unbinge.plan_week_balance()
    assert plan['moves'], "two near-identical shows on adjacent days should be pulled apart"
    final = {m['name']: m['to'] for m in plan['moves']}
    day = {'Crime A': 0, 'Crime B': 1, **final}
    gap = abs(day['Crime A'] - day['Crime B'])
    assert min(gap, 7 - gap) >= 2
    assert all(sum(plan['after']) == 7 and x == 1 for x in plan['after'])   # still one a day
    assert any('crime' in m['why'] for m in plan['moves'])


def test_balance_ignores_paused_and_keeps_multiday_shows_fixed(harness, monkeypatch):
    _balance_setup(harness, monkeypatch, [("Mon One", 0, []), ("Mon Two", 0, []), ("Paused", 0, [])])
    harness.add_show('Daily Show', release_days='0,1,2,3,4,5,6')
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET paused = 1 WHERE show_name = 'Paused'")
        conn.commit()
    plan = unbinge.plan_week_balance()
    assert plan['movable'] == 2
    assert 'Daily Show' not in [m['name'] for m in plan['moves']]
    assert 'Paused' not in [m['name'] for m in plan['moves']]
    assert plan['before'][0] == 3             # Mon One + Mon Two + the daily show; paused doesn't count


def test_balance_endpoint_previews_then_applies(harness, monkeypatch, client):
    _balance_setup(harness, monkeypatch, [(f"Show {c}", 0, []) for c in 'ABCD'])
    preview = client.post('/api/balance-week').get_json()
    assert preview['moves'] and harness.get_show(1)['release_days'] == '0'   # preview changes nothing
    applied = client.post('/api/balance-week', data={'confirm': '1'}).get_json()
    assert applied['applied'] == len(preview['moves'])
    days = sorted(harness.get_show(i)['release_days'] for i in range(1, 5))
    assert len(set(days)) == 4
    logged = [e['detail'] for n in ('Show A', 'Show B', 'Show C', 'Show D') for e in harness.history_for(n)]
    assert sum('balanced the week' in d for d in logged) == applied['applied']
