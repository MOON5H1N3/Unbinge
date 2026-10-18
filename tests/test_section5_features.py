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
        if tvdb_id in ids.values():
            name = next(n for n, i in ids.items() if i == tvdb_id)
        else:
            name = f'ID{tvdb_id}' if f'ID{tvdb_id}' in genres_by_name else 'ANCHOR'
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
    recs = june['recommendations']
    assert recs[0]['name'] == 'Sunny Days'
    # the horror show is only ever the "something different" slot, never a seasonal pick
    assert all(r['season'] is None for r in recs if r['name'] == 'Spooky Hollow')


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
    assert 'nothing queued to follow' in client.get('/').get_data(as_text=True)
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"""INSERT INTO {unbinge.QUEUE_TABLE}
            (show_name, trigger_type, trigger_show_id, release_days, episodes_per_drop, created_at)
            VALUES ('Next One', 'after_show', ?, '0', 1, ?)""", (sid, unbinge.now_local().isoformat()))
        conn.commit()
    assert unbinge.shows_ending_without_successor() == []
    assert 'nothing queued to follow' not in client.get('/').get_data(as_text=True)


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


# ---------------------------------------------------------------------------
# Animation pool: animated shows go back to the animation folder
# ---------------------------------------------------------------------------

def _with_animation_pool(harness, monkeypatch):
    anim = harness.media / 'A' / 'animation'
    anim.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(unbinge, 'ANIMATION_POOL_DIR', str(anim))
    return anim


def _make_in(base, name, eps=2):
    for e in range(1, eps + 1):
        d = base / name / 'Season 01'
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name} - S01E{e:02d}.mkv").write_text('x')


def test_each_show_returns_to_the_pool_it_came_from(harness, monkeypatch, drip):
    anim = _with_animation_pool(harness, monkeypatch)
    monkeypatch.setattr(unbinge, 'series_known_ended', lambda show: (True, 'Sonarr'))
    _make_in(anim, 'Cartoon Show', 1)
    _make_in(harness.pool, 'Live Show', 1)
    ok1, _m, cid, _c = unbinge.promote_show_internal('Cartoon Show', '0,1,2,3,4,5,6', 1)
    ok2, _m, lid, _c = unbinge.promote_show_internal('Live Show', '0,1,2,3,4,5,6', 1)
    assert ok1 and ok2
    assert not (anim / 'Cartoon Show').exists() and not (harness.pool / 'Live Show').exists()
    for sid in (cid, lid):
        drip(sid)          # releases the only episode
        drip(sid)          # vault empty + series ended -> straight back
    assert (anim / 'Cartoon Show').exists() and not (harness.pool / 'Cartoon Show').exists()
    assert (harness.pool / 'Live Show').exists() and not (anim / 'Live Show').exists()


def test_older_shows_without_a_stored_pool_use_the_genre_rule(harness, monkeypatch):
    anim = _with_animation_pool(harness, monkeypatch)
    monkeypatch.setattr(unbinge, 'get_genre_data_for_tvdb_id',
                        lambda conn, tid: {'genres': ['animation'] if tid == 7 else ['drama'], 'poster_url': ''})
    cartoon = {'show_name': 'C', 'tvdb_id': 7, 'tags': '', 'pool_dir': None}
    live = {'show_name': 'L', 'tvdb_id': 8, 'tags': '', 'pool_dir': None}
    tagged = {'show_name': 'T', 'tvdb_id': None, 'tags': 'anime, kids', 'pool_dir': None}
    assert unbinge.return_pool_dir(cartoon) == str(anim)
    assert unbinge.return_pool_dir(live) == unbinge.POOL_DIR
    assert unbinge.return_pool_dir(tagged) == str(anim)


def test_without_an_animation_pool_everything_uses_pool_dir(harness, monkeypatch):
    monkeypatch.setattr(unbinge, 'ANIMATION_POOL_DIR', '')
    assert unbinge.pool_dirs() == [unbinge.POOL_DIR]
    assert unbinge.return_pool_dir({'show_name': 'C', 'tags': 'anime', 'pool_dir': None}) == unbinge.POOL_DIR


def test_pool_page_and_search_list_both_folders(harness, monkeypatch, client):
    anim = _with_animation_pool(harness, monkeypatch)
    _make_in(anim, 'Cartoon Show')
    _make_in(harness.pool, 'Live Show')
    body = client.get('/pool').get_data(as_text=True)
    assert 'Cartoon Show' in body and 'Live Show' in body and 'animation' in body
    names = client.get('/api/pool').get_json()
    flat = names if isinstance(names, list) else names.get('shows', names)
    assert 'Cartoon Show' in str(flat) and 'Live Show' in str(flat)


def test_upgrading_a_v15_database_adds_the_pool_dir_column(harness):
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"ALTER TABLE {unbinge.TABLE_NAME} DROP COLUMN pool_dir")
        conn.execute(f"UPDATE {unbinge.SETTINGS_TABLE} SET value = '15' WHERE key = 'schema_version'")
        conn.commit()
    unbinge.init_db()
    with closing(unbinge.get_db()) as conn:
        cols = {r['name'] for r in conn.execute(f"PRAGMA table_info({unbinge.TABLE_NAME})")}
    assert 'pool_dir' in cols


# ---------------------------------------------------------------------------
# Suggestions: line-up awareness, dismiss, fatigue, length
# ---------------------------------------------------------------------------

def _names(rec):
    return [r['name'] for r in rec['recommendations']]


def test_suggestions_avoid_lookalikes_of_whats_already_dripping(harness, monkeypatch):
    # Genres here are deliberately outside every seasonal theme.
    for n in ('Another Western', 'Gentle Western Musical', 'Filler One', 'Filler Two'):
        harness.make_show_in_pool(n, [(1, 1)])
    genres = {'ANCHOR': ['western', 'sport'], 'Another Western': ['western', 'biography'],
              'Gentle Western Musical': ['western', 'sport', 'musical'],
              'Filler One': ['drama'], 'Filler Two': ['drama'], 'ID5000': ['western', 'biography']}
    _fake_tvdb(monkeypatch, genres)
    sid = _anchor_show(harness, genres)
    other = harness.add_show('Running Western')   # already dripping, same genres as 'Another Western'
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET tvdb_id = 5000 WHERE id = ?", (other,))
        conn.commit()
    names = _names(unbinge.get_recommendations_for_show(sid))
    assert 'Another Western' in names and names[0] != 'Another Western', names


def test_not_interested_hides_a_show_and_restore_brings_it_back(harness, client, monkeypatch):
    for n in ('Keep Me', 'Hide Me'):
        harness.make_show_in_pool(n, [(1, 1)])
    genres = {'ANCHOR': ['crime'], 'Keep Me': ['crime'], 'Hide Me': ['crime']}
    _fake_tvdb(monkeypatch, genres)
    sid = _anchor_show(harness, genres)
    assert 'Hide Me' in _names(unbinge.get_recommendations_for_show(sid))
    assert client.post('/api/suggestions/dismiss', json={'name': 'Hide Me'}).status_code == 200
    rec = unbinge.get_recommendations_for_show(sid)
    assert 'Hide Me' not in _names(rec) and rec['dismissed'] == 1
    assert [d['name'] for d in client.get('/api/suggestions/dismissed').get_json()['dismissed']] == ['Hide Me']
    client.post('/api/suggestions/restore', json={'name': 'Hide Me'})
    assert 'Hide Me' in _names(unbinge.get_recommendations_for_show(sid))


def test_a_dismissal_expires_after_90_days(harness, client, monkeypatch):
    from datetime import date, timedelta
    harness.make_show_in_pool('Old News', [(1, 1)])
    genres = {'ANCHOR': ['crime'], 'Old News': ['crime']}
    _fake_tvdb(monkeypatch, genres)
    sid = _anchor_show(harness, genres)
    client.post('/api/suggestions/dismiss', json={'name': 'Old News'})
    later = unbinge.today_local() + timedelta(days=unbinge.SUGGEST_DISMISS_DAYS + 1)
    assert 'Old News' in _names(unbinge.get_recommendations_for_show(sid, today=later))


def test_repeatedly_shown_picks_sink_but_reloading_in_a_week_changes_nothing(harness, monkeypatch):
    from datetime import date, timedelta
    for i in range(8):
        harness.make_show_in_pool(f"Equal {i}", [(1, 1)])
    genres = {'ANCHOR': ['crime'], **{f"Equal {i}": ['crime'] for i in range(8)}}
    _fake_tvdb(monkeypatch, genres)
    sid = _anchor_show(harness, genres)
    d = date(2026, 3, 2)
    first = _names(unbinge.get_recommendations_for_show(sid, today=d))
    assert _names(unbinge.get_recommendations_for_show(sid, today=d + timedelta(days=2))) == first
    seen = set(first)
    for wk in range(1, 4):
        seen |= set(_names(unbinge.get_recommendations_for_show(sid, today=d + timedelta(weeks=wk))))
    assert len(seen) > 5        # picks rotate instead of the same five every week


def test_short_series_beat_endless_ones_when_otherwise_equal(harness, monkeypatch):
    harness.make_show_in_pool('Short Run', [(1, e) for e in range(1, 9)])
    harness.make_show_in_pool('Endless', [(s, e) for s in range(1, 9) for e in range(1, 21)])
    genres = {'ANCHOR': ['crime'], 'Short Run': ['crime'], 'Endless': ['crime']}
    _fake_tvdb(monkeypatch, genres)
    sid = _anchor_show(harness, genres)
    rec = unbinge.get_recommendations_for_show(sid)
    assert _names(rec)[0] == 'Short Run'
    assert next(r for r in rec['recommendations'] if r['name'] == 'Short Run')['episodes'] == 8


# ---------------------------------------------------------------------------
# Suggestions: shared plan, ended boost, taste, watched memory
# ---------------------------------------------------------------------------

def _fake_profiles(monkeypatch, by_name_or_id):
    """Stub the TVDB profile lookup: {tvdb_id|folder name: dict}. Pool folders
    get ids from _fake_tvdb, so match on name through the search cache."""
    seen = []

    def prof(tvdb_id):
        seen.append(tvdb_id)
        return dict({'status': '', 'year': None, 'runtime': None, 'network': ''}, **by_name_or_id.get(tvdb_id, {})), None

    monkeypatch.setattr(unbinge, 'tvdb_get_series_profile', prof)
    return seen


def _ending_anchor(harness, name, tvdb_id):
    sid = harness.add_show(name)
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET tvdb_id = ?, completed_at = ? WHERE id = ?",
                     (tvdb_id, unbinge.now_local().isoformat(), sid))
        conn.commit()
    return sid


def test_two_ending_shows_are_planned_together_with_no_repeats(harness, monkeypatch):
    for i in range(8):
        harness.make_show_in_pool(f"Crime {i}", [(1, 1)])
    genres = {'ANCHOR': ['crime'], 'ID7001': ['crime'], **{f"Crime {i}": ['crime'] for i in range(8)}}
    _fake_tvdb(monkeypatch, genres)
    a = _ending_anchor(harness, 'Ending A', 7000)
    b = _ending_anchor(harness, 'Ending B', 7001)
    ra, rb = unbinge.get_recommendations_for_show(a), unbinge.get_recommendations_for_show(b)
    na, nb = _names(ra), _names(rb)
    assert na and nb and not (set(na) & set(nb))      # nothing offered twice
    assert ra['planned_with'] == 1 and rb['planned_with'] == 1


def test_a_show_that_is_not_ending_is_planned_on_its_own(harness, monkeypatch):
    harness.make_show_in_pool('Solo Pick', [(1, 1)])
    genres = {'ANCHOR': ['crime'], 'Solo Pick': ['crime']}
    _fake_tvdb(monkeypatch, genres)
    _ending_anchor(harness, 'Other Ending', 7000)
    sid = _anchor_show(harness, genres)
    rec = unbinge.get_recommendations_for_show(sid)
    assert rec['planned_with'] == 0 and _names(rec) == ['Solo Pick']


def test_a_list_is_varied_not_five_of_the_same(harness, monkeypatch):
    for i in range(5):
        harness.make_show_in_pool(f"Cop Drama {i}", [(1, 1)])
    harness.make_show_in_pool('Cosy Baking', [(1, 1)])
    genres = {'ANCHOR': ['crime', 'comedy'], **{f"Cop Drama {i}": ['crime', 'thriller'] for i in range(5)},
              'Cosy Baking': ['crime', 'reality']}
    _fake_tvdb(monkeypatch, genres)
    sid = _anchor_show(harness, genres)
    names = _names(unbinge.get_recommendations_for_show(sid, today=__import__('datetime').date(2026, 3, 2)))
    assert 'Cosy Baking' in names[:3]      # the odd one out is pulled up once a cop drama is already in the list


def test_finished_series_beat_continuing_ones(harness, monkeypatch):
    harness.make_show_in_pool('Done And Dusted', [(1, e) for e in range(1, 7)])
    harness.make_show_in_pool('Still Airing', [(1, e) for e in range(1, 7)])
    genres = {'ANCHOR': ['crime'], 'Done And Dusted': ['crime'], 'Still Airing': ['crime']}
    _fake_tvdb(monkeypatch, genres)
    sid = _anchor_show(harness, genres)
    with closing(unbinge.get_db()) as conn:      # resolve ids so the profiles can be keyed
        pass
    unbinge.get_recommendations_for_show(sid)
    with closing(unbinge.get_db()) as conn:
        ids = {r['folder_name']: r['tvdb_id'] for r in conn.execute(f"SELECT * FROM {unbinge.POOL_TVDB_CACHE_TABLE}")}
        conn.execute(f"DELETE FROM {unbinge.TVDB_PROFILE_CACHE_TABLE}")
        conn.commit()
    _fake_profiles(monkeypatch, {ids['Done And Dusted']: {'status': 'ended'}, ids['Still Airing']: {'status': 'continuing'}})
    rec = unbinge.get_recommendations_for_show(sid)
    assert _names(rec)[0] == 'Done And Dusted'
    assert 'finished airing' in rec['recommendations'][0]['why']


def test_profiles_are_cached_and_capped_per_request(harness, monkeypatch):
    for i in range(30):
        harness.make_show_in_pool(f"Many {i:02d}", [(1, 1)])
    genres = {'ANCHOR': ['crime'], **{f"Many {i:02d}": ['crime'] for i in range(30)}}
    _fake_tvdb(monkeypatch, genres)
    seen = _fake_profiles(monkeypatch, {})
    sid = _anchor_show(harness, genres)
    first = unbinge.get_recommendations_for_show(sid)
    assert len(seen) <= unbinge.SUGGEST_PROFILE_FETCH_CAP
    assert first['refining'] >= 0
    unbinge.get_recommendations_for_show(sid)
    # a failed (empty) lookup is remembered for a day rather than retried every load
    again = len(seen)
    unbinge.get_recommendations_for_show(sid)
    assert len(seen) - again <= unbinge.SUGGEST_PROFILE_FETCH_CAP


def test_shows_you_just_finished_are_not_suggested_again(harness, monkeypatch):
    harness.make_show_in_pool('Just Watched', [(1, 1)])
    harness.make_show_in_pool('Fresh Pick', [(1, 1)])
    genres = {'ANCHOR': ['crime'], 'Just Watched': ['crime'], 'Fresh Pick': ['crime']}
    _fake_tvdb(monkeypatch, genres)
    sid = _anchor_show(harness, genres)
    with closing(unbinge.get_db()) as conn:
        unbinge.log_history(conn, None, 'Just Watched', 'graduated', 'Returned to the pool')
        conn.commit()
    assert _names(unbinge.get_recommendations_for_show(sid)) == ['Fresh Pick']
    later = unbinge.today_local() + __import__('datetime').timedelta(days=unbinge.SUGGEST_WATCHED_EXCLUDE_DAYS + 5)
    assert 'Just Watched' in _names(unbinge.get_recommendations_for_show(sid, today=later))   # allowed again, if discouraged


def test_turning_a_show_down_cools_similar_ones(harness, client, monkeypatch):
    for n in ('Gritty Cop', 'Gritty Cop 2', 'Quiet Garden'):
        harness.make_show_in_pool(n, [(1, 1)])
    genres = {'ANCHOR': ['crime', 'drama'], 'Gritty Cop': ['crime', 'noir'], 'Gritty Cop 2': ['crime', 'noir'],
              'Quiet Garden': ['crime', 'family']}
    _fake_tvdb(monkeypatch, genres)
    sid = _anchor_show(harness, genres)
    client.post('/api/suggestions/dismiss', json={'name': 'Gritty Cop'})
    names = _names(unbinge.get_recommendations_for_show(sid, today=__import__('datetime').date(2026, 3, 2)))
    assert 'Gritty Cop' not in names and names.index('Quiet Garden') < names.index('Gritty Cop 2')


def test_a_big_pool_with_several_ending_shows_stays_quick(harness, monkeypatch):
    import time, random
    rnd = random.Random(3)
    pool_genres = ['crime', 'drama', 'comedy', 'sci-fi', 'fantasy', 'horror', 'romance', 'reality', 'history', 'western', 'sport']
    genres = {'ANCHOR': ['crime', 'drama']}
    for i in range(150):
        harness.make_show_in_pool(f"Big {i:03d}", [(1, 1)])
        genres[f"Big {i:03d}"] = rnd.sample(pool_genres, 3)
    _fake_tvdb(monkeypatch, genres)
    ids = [_ending_anchor(harness, f"Ending {k}", 7000 + k) for k in range(4)]
    unbinge.get_recommendations_for_show(ids[0])      # warm the caches
    start = time.time()
    unbinge.get_recommendations_for_show(ids[1])
    assert time.time() - start < 3.0


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
from datetime import datetime as _dt, timedelta as _td


def _drip(sid, season, episode, days_ago):
    stamp = (unbinge.now_local() - _td(days=days_ago)).isoformat()
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"INSERT OR REPLACE INTO {unbinge.DRIPPED_TABLE} (show_id, season, episode, dripped_at) VALUES (?, ?, ?, ?)",
                     (sid, season, episode, stamp))
        conn.commit()


# ---------------------------------------------------------------------------
# Weekly digest
# ---------------------------------------------------------------------------

def test_weekly_digest_summarises_the_week(harness, monkeypatch):
    sid = harness.add_show('Busy Show')
    for e in range(1, 6):
        _drip(sid, 1, e, 2)
    text = unbinge.build_weekly_digest()
    assert text.startswith('weekly digest')
    assert 'dripped: 5 episode(s) — Busy Show ×5' in text
    assert 'watched' not in text and 'behind' not in text


def test_no_plex_watch_tracking_remains(harness, client):
    sid = harness.add_show('Plain Show')
    _drip(sid, 1, 1, 3)
    assert client.post('/api/watch-sync').status_code in (404, 405)
    assert 'watching progress' not in client.get('/system').get_data(as_text=True)
    assert ' behind' not in client.get('/').get_data(as_text=True)


def test_weekly_digest_names_ending_shows_and_what_could_follow(harness, monkeypatch):
    harness.make_show_in_pool('Next Up', [(1, 1)])
    genres = {'ANCHOR': ['crime'], 'Next Up': ['crime']}
    _fake_tvdb(monkeypatch, genres)
    sid = harness.add_show('Wrapping Up')
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET tvdb_id = 1, completed_at = ? WHERE id = ?", (unbinge.now_local().isoformat(), sid))
        conn.commit()
    text = unbinge.build_weekly_digest()
    assert 'ending soon with nothing queued' in text and 'Wrapping Up' in text and 'try Next Up' in text


def test_a_quiet_week_sends_nothing_and_the_button_explains(harness, client, monkeypatch):
    assert unbinge.build_weekly_digest() is None
    assert client.post('/api/weekly-digest/send').get_json()['text'] == ''


def test_send_it_now_posts_to_the_webhook_when_there_is_one(harness, client, monkeypatch):
    sid = harness.add_show('Sent Show')
    _drip(sid, 1, 1, 1)
    sent = []
    monkeypatch.setattr(unbinge, 'send_notification', lambda event, message, **kw: sent.append((event, message)))
    r = client.post('/api/weekly-digest/send').get_json()
    assert 'webhook' in r['message'] and not sent                    # no webhook configured: shows the text only
    unbinge.set_setting('webhook_url', 'http://hook.test/x')
    client.post('/api/weekly-digest/send')
    assert sent and sent[0][0] == 'digest' and 'Sent Show' in sent[0][1]


# ---------------------------------------------------------------------------
# One-tap queue and auto-queue
# ---------------------------------------------------------------------------

def _dripping_on(harness, name, days, per_drop=1):
    sid = harness.add_show(name)
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET release_days = ?, release_day = ?, episodes_per_drop = ? WHERE id = ?",
                     (days, int(days.split(',')[0]), per_drop, sid))
        conn.commit()
    return sid


def test_one_tap_queue_picks_the_lightest_day_and_frees_the_ending_shows_day(harness, client, monkeypatch):
    harness.make_show_in_pool('Newcomer', [(1, 1)])
    _fake_tvdb(monkeypatch, {'ANCHOR': ['crime'], 'Newcomer': ['crime']})
    # Mon: Busy A and Busy B. Tue: Ending (about to finish). Wed-Sun: empty.
    _dripping_on(harness, 'Busy A', '0'); _dripping_on(harness, 'Busy B', '0')
    ending = _dripping_on(harness, 'Ending', '1')
    r = client.post(f'/api/queue/{ending}/quick', data={'show_name': 'Newcomer'}).get_json()
    assert r['status'] == 'success'
    entries = unbinge.get_queue_for_show(ending)
    assert len(entries) == 1 and entries[0]['show_name'] == 'Newcomer'
    # Tuesday is free once Ending finishes and it is the ending show's own day, so it wins the tie
    assert entries[0]['release_days'] == '1' and entries[0]['auto'] == 0
    assert 'tue' in r['message']


def test_one_tap_queue_keeps_the_number_of_drop_days_and_the_pace(harness, client, monkeypatch):
    harness.make_show_in_pool('Newcomer', [(1, 1)])
    _fake_tvdb(monkeypatch, {'ANCHOR': ['crime'], 'Newcomer': ['crime']})
    ending = _dripping_on(harness, 'Twice Weekly', '0,3', per_drop=2)
    client.post(f'/api/queue/{ending}/quick', data={'show_name': 'Newcomer'})
    entry = unbinge.get_queue_for_show(ending)[0]
    assert entry['release_days'] == '0,3' and entry['episodes_per_drop'] == 2


def test_two_queued_shows_do_not_land_on_the_same_day(harness, client, monkeypatch):
    for n in ('First', 'Second'):
        harness.make_show_in_pool(n, [(1, 1)])
    _fake_tvdb(monkeypatch, {'ANCHOR': ['crime']})
    a, b = _dripping_on(harness, 'End A', '2'), _dripping_on(harness, 'End B', '2')
    client.post(f'/api/queue/{a}/quick', data={'show_name': 'First'})
    client.post(f'/api/queue/{b}/quick', data={'show_name': 'Second'})
    assert unbinge.get_queue_for_show(a)[0]['release_days'] != unbinge.get_queue_for_show(b)[0]['release_days']


def test_quick_queue_rejects_unknown_shows(harness, client):
    sid = _dripping_on(harness, 'Anchor X', '0')
    assert client.post(f'/api/queue/{sid}/quick', data={'show_name': 'Not In Pool'}).status_code == 404
    assert client.post(f'/api/queue/{sid}/quick', data={}).status_code == 400


def test_auto_queue_is_off_by_default_and_queues_when_enabled(harness, monkeypatch):
    harness.make_show_in_pool('Great Followup', [(1, 1)])
    genres = {'ANCHOR': ['crime'], 'Great Followup': ['crime']}
    _fake_tvdb(monkeypatch, genres)
    sid = harness.add_show('Wrapping Up')
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET tvdb_id = 1, completed_at = ? WHERE id = ?", (unbinge.now_local().isoformat(), sid))
        conn.commit()
    sent = []
    monkeypatch.setattr(unbinge, 'send_notification', lambda event, message, **kw: sent.append((event, message)))
    assert unbinge.run_auto_queue() == [] and not unbinge.get_queue_for_show(sid)
    unbinge.set_setting('auto_queue_enabled', '1')
    done = unbinge.run_auto_queue()
    assert [(a, b) for a, b, _d in done] == [('Wrapping Up', 'Great Followup')]
    entry = unbinge.get_queue_for_show(sid)[0]
    assert entry['auto'] == 1
    assert sent and sent[0][0] == 'queued' and 'Great Followup' in sent[0][1]
    assert unbinge.run_auto_queue() == []              # it does not queue a second one
    with closing(unbinge.get_db()) as conn:
        assert conn.execute(f"SELECT COUNT(*) n FROM {unbinge.HISTORY_TABLE} WHERE action = 'queued'").fetchone()['n'] == 1


def test_auto_queue_only_acts_within_a_week_of_the_end(harness, monkeypatch):
    harness.make_show_in_pool('Something', [(1, 1)])
    _fake_tvdb(monkeypatch, {'ANCHOR': ['crime'], 'Something': ['crime']})
    sid = harness.add_show('Far From Done')
    unbinge.set_setting('auto_queue_enabled', '1')
    monkeypatch.setattr(unbinge, 'shows_ending_without_successor', lambda days=21: [] if days <= 7 else [{'id': sid, 'name': 'x', 'state': 'finishing', 'ends': '2099-01-01'}])
    assert unbinge.run_auto_queue() == []


# ---------------------------------------------------------------------------
# Undo any drip from the last week; backups and restore
# ---------------------------------------------------------------------------

def _two_drips(harness, drip):
    show = harness.vault / 'Test Show'
    for e in (1, 2):
        harness.make_episode(show, 'Test Show', 1, e, subdir='Season 01')
    sid = harness.add_show('Test Show')
    drip(sid)
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET last_run_date = NULL WHERE id = ?", (sid,))
        conn.commit()
    drip(sid)
    return sid


def test_an_older_drip_can_be_undone_while_a_newer_one_stands(harness, client, drip):
    sid = _two_drips(harness, drip)
    drips = unbinge.get_undoable_drips()
    assert len(drips) == 2
    oldest = drips[-1]
    r = client.post(f"/api/undo-drip/{oldest['id']}")
    assert r.status_code == 200
    assert len(unbinge.get_undoable_drips()) == 1
    assert len(harness.episode_tags(harness.plex / 'Test Show')) == 1


def test_drips_older_than_a_week_cannot_be_undone(harness, client, drip):
    sid = _two_drips(harness, drip)
    old = unbinge.get_undoable_drips()[-1]
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.HISTORY_TABLE} SET occurred_at = ? WHERE id = ?",
                     ((unbinge.now_local() - _td(days=9)).isoformat(), old['id']))
        conn.commit()
    assert old['id'] not in {d['id'] for d in unbinge.get_undoable_drips()}
    assert client.post(f"/api/undo-drip/{old['id']}").status_code == 400


def test_history_page_offers_undo_on_recent_drips(harness, client, drip):
    _two_drips(harness, drip)
    html = client.get('/history').get_data(as_text=True)
    assert html.count('undoDrip(') >= 2


def test_restore_from_a_listed_backup_brings_data_back_and_keeps_a_safety_copy(harness, client):
    harness.add_show('Keep Me')
    path = unbinge.create_db_backup()
    harness.add_show('Added After')
    r = client.post('/api/restore-backup', data={'filename': os.path.basename(path)}).get_json()
    assert r['status'] == 'success' and 'saved as' in r['message']
    names = {s['show_name'] for s in unbinge.load_shows()}
    assert names == {'Keep Me'}
    assert len(os.listdir(unbinge.BACKUPS_DIR)) >= 2           # the original plus the safety copy


def test_restore_from_an_uploaded_file(harness, client, tmp_path):
    import io
    harness.add_show('Uploaded Era')
    path = unbinge.create_db_backup(destination_dir=str(tmp_path))
    harness.add_show('Later')
    with open(path, 'rb') as f:
        data = {'file': (io.BytesIO(f.read()), 'mine.db')}
    r = client.post('/api/restore-backup', data=data, content_type='multipart/form-data')
    assert r.status_code == 200
    assert {s['show_name'] for s in unbinge.load_shows()} == {'Uploaded Era'}


def test_restore_refuses_junk_and_path_tricks(harness, client):
    import io
    harness.add_show('Safe')
    bad = client.post('/api/restore-backup', data={'file': (io.BytesIO(b'not a database at all'), 'x.db')},
                      content_type='multipart/form-data')
    assert bad.status_code == 400
    assert client.post('/api/restore-backup', data={'filename': '../../etc/passwd'}).status_code == 400
    assert client.post('/api/restore-backup', data={'filename': 'unbinge-backup-nothere.db'}).status_code == 404
    assert client.post('/api/restore-backup', data={}).status_code == 400
    assert {s['show_name'] for s in unbinge.load_shows()} == {'Safe'}


def test_automatic_backups_are_on_by_default_and_migration_enables_unused_ones(harness):
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.SETTINGS_TABLE} SET value = '0' WHERE key = 'auto_backup_enabled'")
        conn.execute(f"UPDATE {unbinge.SETTINGS_TABLE} SET value = '20' WHERE key = 'schema_version'")
        conn.commit()
    unbinge.init_db()
    assert unbinge.get_setting('auto_backup_enabled') == '1'
    unbinge.set_setting('auto_backup_enabled', '0')
    unbinge.set_setting('last_auto_backup', unbinge.now_local().isoformat())
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.SETTINGS_TABLE} SET value = '20' WHERE key = 'schema_version'")
        conn.commit()
    unbinge.init_db()
    assert unbinge.get_setting('auto_backup_enabled') == '0'      # someone who used them and turned them off keeps that


# ---------------------------------------------------------------------------
# Catch-up mode after downtime
# ---------------------------------------------------------------------------

def test_catch_up_owes_missed_release_days(harness, client):
    from datetime import timedelta
    for e in range(1, 9):
        harness.make_episode(harness.vault / 'Daily Show', 'Daily Show', 1, e, subdir='Season 01')
    show_id = harness.add_show('Daily Show')
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET release_days = '0,1,2,3,4,5,6', release_day = '0' WHERE id = ?", (show_id,))
        conn.commit()
    unbinge.set_setting('last_job_run', (unbinge.now_local() - timedelta(days=3)).isoformat())

    plan = client.get('/api/catch-up').get_json()['plan']
    assert plan and plan['items'][0]['name'] == 'Daily Show'
    assert plan['items'][0]['owed_drops'] == 3

    unbinge.set_setting('dry_run', '0')
    client.post('/api/catch-up')
    with closing(unbinge.get_db()) as conn:
        n = conn.execute(f"SELECT COUNT(*) c FROM {unbinge.DRIPPED_TABLE} WHERE show_id = ?", (show_id,)).fetchone()['c']
    assert n == 3
    assert client.get('/api/catch-up').get_json()['plan'] is None


def test_catch_up_none_when_no_gap(harness, client):
    unbinge.set_setting('last_job_run', unbinge.now_local().isoformat())
    assert client.get('/api/catch-up').get_json()['plan'] is None


# ---------------------------------------------------------------------------
# Library health check
# ---------------------------------------------------------------------------

def test_library_health_flags_gaps_duplicates_and_odd_names(harness, client):
    for e in (1, 2, 5):
        harness.make_episode(harness.vault / 'Gappy', 'Gappy', 1, e, subdir='Season 01')
    harness.make_episode(harness.plex / 'Gappy', 'Gappy', 1, 2, subdir='Season 01')
    harness.add_show('Gappy')
    (harness.pool / 'Some.Show.2019.S01.1080p.WEBRip.x264-GRP').mkdir()

    issues = client.get('/api/library-health').get_json()['issues']
    kinds = {i['kind'] for i in issues}
    assert {'gap', 'duplicate', 'odd_name'} <= kinds
    gap = next(i for i in issues if i['kind'] == 'gap')
    assert '3-4' in gap['detail']


def test_library_health_clean_library_is_empty(harness, client):
    for e in (1, 2):
        harness.make_episode(harness.vault / 'Tidy', 'Tidy', 1, e, subdir='Season 01')
    harness.add_show('Tidy')
    assert client.get('/api/library-health').get_json()['count'] == 0


# ---------------------------------------------------------------------------
# Pool labels, filters and bulk tools
# ---------------------------------------------------------------------------

def test_pool_marks_bulk_set_and_clear(harness, client):
    (harness.pool / 'Alpha').mkdir()
    (harness.pool / 'Beta').mkdir()
    r = client.post('/api/pool-marks', json={'names': ['Alpha', 'Beta'], 'mark': 'shortlist', 'value': True})
    assert r.status_code == 200
    with closing(unbinge.get_db()) as conn:
        assert unbinge.get_pool_marks(conn)['alpha']['shortlist']
    html = client.get('/pool?mark=shortlist').get_data(as_text=True)
    assert 'Alpha' in html and 'Beta' in html
    client.post('/api/pool-marks', json={'names': ['Beta'], 'mark': 'shortlist', 'value': False})
    html = client.get('/pool?mark=shortlist').get_data(as_text=True)
    assert 'Alpha' in html and 'value="Beta"' not in html
    assert client.post('/api/pool-marks', json={'names': [], 'mark': 'shortlist'}).status_code == 400
    assert client.post('/api/pool-marks', json={'names': ['x'], 'mark': 'bogus'}).status_code == 400


def test_animation_mark_returns_show_to_animation_pool(harness, monkeypatch):
    anim = harness.pool.parent / 'animation'
    anim.mkdir()
    monkeypatch.setattr(unbinge, 'ANIMATION_POOL_DIR', str(anim))
    with closing(unbinge.get_db()) as conn:
        unbinge.set_pool_marks(conn, ['Cartoon'], 'animation', True)
    assert unbinge.show_is_animated({'show_name': 'Cartoon'})
    assert not unbinge.show_is_animated({'show_name': 'Drama'})
    assert unbinge.return_pool_dir({'show_name': 'Cartoon'}) == str(anim)


def test_pool_length_filter(harness, client):
    for e in range(1, 4):
        harness.make_episode(harness.pool / 'Tiny', 'Tiny', 1, e, subdir='Season 01')
    for e in range(1, 31):
        harness.make_episode(harness.pool / 'Medium', 'Medium', 1, e, subdir='Season 01')
    html = client.get('/pool?length=short').get_data(as_text=True)
    assert 'value="Tiny"' in html and 'value="Medium"' not in html


def test_show_detail_scripts_have_valid_syntax(harness, client, tmp_path):
    import re, subprocess, shutil
    if not shutil.which('node'):
        return
    sid = harness.add_show('Cooling Show', completed_at=unbinge.now_local().isoformat())
    html = client.get(f'/show/{sid}').get_data(as_text=True)
    assert 'heroReturnBtn' in html
    for i, block in enumerate(re.findall(r'<script>(.*?)</script>', html, re.S)):
        f = tmp_path / f's{i}.js'
        f.write_text(block)
        r = subprocess.run(['node', '--check', str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[:500]


def test_return_to_pool_handler_does_not_read_current_target_after_awaiting(harness, client):
    """event.currentTarget is null once the handler has awaited the confirm
    dialog, which made the 'return to pool now' button silently do nothing."""
    sid = harness.add_show('Cooling Show 2', completed_at=unbinge.now_local().isoformat())
    html = client.get(f'/show/{sid}').get_data(as_text=True)
    start = html.index("heroReturnBtn.addEventListener")
    handler = html[start:html.index("heroDripBtn", start)]
    after_await = handler.split("await unbingeConfirm", 1)[1]
    assert 'currentTarget' not in after_await


def test_dashboard_notifications_live_in_the_bell(harness, client):
    sid = harness.add_show('Wrapping Up B')
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET completed_at = ? WHERE id = ?", (unbinge.now_local().isoformat(), sid))
        conn.commit()
    html = client.get('/').get_data(as_text=True)
    assert 'id="bellBtn"' in html and 'nothing queued to follow' in html
    assert 'class="banner banner-warning">' not in html.split('bellWrap', 1)[0].split('<main', 1)[-1]


# ---------------------------------------------------------------------------
# Speed: compression, per-request settings memo
# ---------------------------------------------------------------------------

def test_large_pages_are_gzipped_when_the_browser_accepts_it(harness, client):
    for i in range(30):
        (harness.pool / f'Folder {i}').mkdir()
    plain = client.get('/pool').get_data()
    r = client.get('/pool', headers={'Accept-Encoding': 'gzip'})
    assert r.headers.get('Content-Encoding') == 'gzip'
    import gzip
    assert gzip.decompress(r.get_data()) == plain
    assert len(r.get_data()) < len(plain)
    assert client.get('/api/pool', headers={'Accept-Encoding': 'gzip'}).status_code == 200


def test_a_setting_saved_during_a_request_is_read_back_fresh(harness, client):
    unbinge.set_setting('behind_probe', 'one')
    with unbinge.app.test_request_context('/'):
        assert unbinge.get_setting('behind_probe') == 'one'
        unbinge.set_setting('behind_probe', 'two')
        assert unbinge.get_setting('behind_probe') == 'two'
    assert unbinge.get_setting('behind_probe') == 'two'


def test_a_daily_show_is_never_blamed_for_a_clash(harness, monkeypatch):
    spec = [("Coven Daily", '0,1,2,3,4,5,6', ['fantasy']), ("Witches", 5, ['fantasy']),
            ("Other A", 5, []), ("Other B", 5, [])]
    _balance_setup(harness, monkeypatch, spec)
    plan = unbinge.plan_week_balance()
    assert plan['moves'], "three shows on Saturday should still be spread out"
    assert not any('Coven Daily' in m['why'] for m in plan['moves'])


# ---------------------------------------------------------------------------
# Suggestions load quickly: TVDB lookups run in parallel and can be pre-warmed
# ---------------------------------------------------------------------------

def _threadsafe_slow_tvdb(monkeypatch, delay=0.05):
    import threading, time as _t
    lock, ids, calls = threading.Lock(), {}, {'search': 0, 'genres': 0, 'profile': 0}

    def search(query):
        _t.sleep(delay)
        with lock:
            calls['search'] += 1
            ids.setdefault(query, 5000 + len(ids))
            return [{'tvdb_id': ids[query], 'name': query, 'year': '', 'image_url': '', 'overview': ''}], None

    def genres(tvdb_id):
        _t.sleep(delay)
        with lock:
            calls['genres'] += 1
        return ['crime', 'thriller'], '', None

    def profile(tvdb_id):
        _t.sleep(delay)
        with lock:
            calls['profile'] += 1
        return {'status': 'ended', 'year': 2010, 'runtime': 45, 'network': 'X'}, None

    monkeypatch.setattr(unbinge, 'tvdb_search_series', search)
    monkeypatch.setattr(unbinge, 'tvdb_get_series_genres', genres)
    monkeypatch.setattr(unbinge, 'tvdb_get_series_profile', profile)
    monkeypatch.setattr(unbinge, 'tvdb_get_token', lambda *a, **k: ('tok', None))
    return calls


def test_a_cold_pool_is_indexed_in_parallel_not_one_by_one(harness, monkeypatch):
    import time as _t
    for i in range(24):
        harness.make_show_in_pool(f'Pool Show {i}', [(1, 1)])
    calls = _threadsafe_slow_tvdb(monkeypatch)
    monkeypatch.setattr(unbinge, 'TVDB_PARALLEL_WORKERS', 8)
    with closing(unbinge.get_db()) as conn:
        t = _t.time()
        unbinge._prefetch_pool_lookups(conn, [f'Pool Show {i}' for i in range(24)], 40)
        took = _t.time() - t
        assert all(unbinge._pool_folder_is_cached(conn, f'Pool Show {i}') for i in range(24))
    assert calls['search'] == 24 and calls['genres'] == 24
    assert took < 48 * 0.05 * 0.6          # well under what 48 serial calls would take


def test_warming_fills_every_cache_and_only_one_run_at_a_time(harness, monkeypatch):
    for i in range(12):
        harness.make_show_in_pool(f'Warm {i}', [(1, 1)])
    calls = _threadsafe_slow_tvdb(monkeypatch, delay=0.01)
    monkeypatch.setattr(unbinge, 'TVDB_PARALLEL_WORKERS', 6)
    unbinge.set_setting('tvdb_api_key', 'k')
    unbinge.warm_suggestion_caches()
    assert calls['search'] == 12 and calls['genres'] == 12 and calls['profile'] == 12
    before = dict(calls)
    unbinge.warm_suggestion_caches()                    # everything cached: no more web calls
    assert calls == before
    unbinge._warm_lock.acquire()
    try:
        unbinge.warm_suggestion_caches()                # another run in progress: returns at once
    finally:
        unbinge._warm_lock.release()
    assert calls == before


def test_warming_does_nothing_without_a_tvdb_key(harness, monkeypatch):
    harness.make_show_in_pool('No Key', [(1, 1)])
    calls = _threadsafe_slow_tvdb(monkeypatch, delay=0)
    unbinge.warm_suggestion_caches()
    assert calls['search'] == 0


# ---------------------------------------------------------------------------
# Seasonal picks follow the date the new show will start, not today's
# ---------------------------------------------------------------------------

def test_start_date_is_the_end_plus_cooldown_unless_the_series_has_ended(harness):
    from datetime import date, timedelta
    today = date(2026, 10, 18)
    sh = {'completed_at': None, 'estimated_end': date(2026, 12, 1)}
    grace = unbinge.get_cooldown_grace_days()
    assert unbinge._suggestion_start_date(sh, {'status': 'continuing'}, today) == date(2026, 12, 1) + timedelta(days=grace)
    assert unbinge._suggestion_start_date(sh, {'status': 'ended'}, today) == date(2026, 12, 1)
    assert unbinge._suggestion_start_date({'completed_at': None, 'estimated_end': None}, None, today) == today


def test_a_show_that_finishes_after_halloween_gets_no_halloween_picks(harness, monkeypatch):
    from datetime import date
    today = date(2026, 10, 18)
    monkeypatch.setattr(unbinge, 'today_local', lambda: today)
    for i in range(60):
        harness.make_episode(harness.vault / 'Long Runner', 'Long Runner', 1 + i // 20, 1 + i % 20, subdir=f'Season {1 + i // 20:02d}')
    for n in ('Haunted House', 'Plain Crime'):
        harness.make_show_in_pool(n, [(1, 1)])
    genres = {'ANCHOR': ['crime'], 'Haunted House': ['horror'], 'Plain Crime': ['crime']}
    _fake_tvdb(monkeypatch, genres)
    sid = harness.add_show('Long Runner', release_days='0,1,2,3,4,5,6')
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET tvdb_id = 1 WHERE id = ?", (sid,))
        conn.commit()
    res = unbinge.get_recommendations_for_show(sid, today=today)
    assert res['starts'] >= '2026-12-01'
    assert res['season'] in ('Christmas', 'Winter')
    assert not any(r['season'] == 'Halloween' for r in res['recommendations'])


def test_a_show_finishing_this_month_still_gets_halloween_picks(harness, monkeypatch):
    from datetime import date
    today = date(2026, 10, 18)
    monkeypatch.setattr(unbinge, 'today_local', lambda: today)
    harness.make_episode(harness.vault / 'Short One', 'Short One', 1, 1, subdir='Season 01')
    harness.make_show_in_pool('Haunted House', [(1, 1)])
    _fake_tvdb(monkeypatch, {'ANCHOR': ['crime'], 'Haunted House': ['horror']})
    sid = harness.add_show('Short One', release_days='0,1,2,3,4,5,6')
    with closing(unbinge.get_db()) as conn:
        conn.execute(f"UPDATE {unbinge.TABLE_NAME} SET tvdb_id = 1 WHERE id = ?", (sid,))
        conn.commit()
    res = unbinge.get_recommendations_for_show(sid, today=today)
    assert res['starts'] <= '2026-11-01'
    assert res['season'] == 'Halloween'
    assert any(r['season'] == 'Halloween' for r in res['recommendations'])
