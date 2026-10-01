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

import app as driparr

# The harness fixture in conftest.py stubs send_notification to a no-op for
# every test, to stop any test from accidentally firing a real webhook. That's
# the right default - but it means the tests below, which specifically verify
# send_notification's OWN embed logic, need the real implementation. Captured
# here, at import time, before any fixture has a chance to patch the module
# attribute.
_REAL_SEND_NOTIFICATION = driparr.send_notification


# ---------------------------------------------------------------------------
# F1 - dry-run mode
# ---------------------------------------------------------------------------

def test_dry_run_does_not_move_files(harness, client):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')

    driparr.set_setting('dry_run', '1')
    driparr.run_drip_job(force_show_id=show_id)

    assert harness.episode_tags(harness.vault / 'Test Show') == {(1, 1)}, \
        "dry run must not move the file"
    assert harness.episode_tags(harness.plex / 'Test Show') == set()


def test_dry_run_does_not_mark_episodes_dripped(harness):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')

    driparr.set_setting('dry_run', '1')
    driparr.run_drip_job(force_show_id=show_id)

    with closing(driparr.get_db()) as conn:
        assert driparr.get_dripped_set(conn, show_id) == set()


def test_dry_run_result_is_labelled(harness):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')

    driparr.set_setting('dry_run', '1')
    outcome = driparr.run_drip_job(force_show_id=show_id)

    assert outcome['ran'], "a dry run should still report what it WOULD do"
    _id, _name, result = outcome['ran'][0]
    assert '[DRY RUN]' in result


def test_disabling_dry_run_allows_real_drips_again(harness):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')

    driparr.set_setting('dry_run', '1')
    driparr.run_drip_job(force_show_id=show_id)
    driparr.set_setting('dry_run', '0')
    driparr.run_drip_job(force_show_id=show_id)

    assert harness.episode_tags(harness.plex / 'Test Show') == {(1, 1)}, \
        "a real drip after disabling dry run should actually move the file"


def test_run_drip_job_summary_matches_dry_run_state(harness, client):
    """Regression guard for the bug caught during development: the
    /api/run-drip summary once said 'dripped' during a dry run while the
    job_runs log correctly said 'previewed', contradicting itself."""
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')

    driparr.set_setting('dry_run', '1')
    resp = client.post('/api/run-drip')
    message = resp.get_json()['message'].lower()
    assert 'dry run' in message or 'previewed' in message, \
        "summary must not claim a real drip happened while dry_run is on"


# ---------------------------------------------------------------------------
# F5 - missed-run detection
# ---------------------------------------------------------------------------

def test_no_notice_when_never_run(harness):
    assert driparr.detect_missed_run() is None


def test_no_notice_shortly_after_a_run(harness):
    driparr.set_setting('last_job_run', driparr.now_local().isoformat())
    assert driparr.detect_missed_run() is None


def test_notice_appears_after_a_long_gap(harness):
    old = (driparr.now_local() - timedelta(hours=40)).isoformat()
    driparr.set_setting('last_job_run', old)
    notice = driparr.detect_missed_run()
    assert notice is not None
    assert 'day' in notice.lower()


def test_notice_clears_after_a_fresh_run(harness):
    old = (driparr.now_local() - timedelta(hours=40)).isoformat()
    driparr.set_setting('last_job_run', old)
    assert driparr.detect_missed_run() is not None

    driparr.set_setting('last_job_run', driparr.now_local().isoformat())
    assert driparr.detect_missed_run() is None


def test_missed_run_banner_renders_on_dashboard(harness, client):
    old = (driparr.now_local() - timedelta(hours=40)).isoformat()
    driparr.set_setting('last_job_run', old)
    resp = client.get('/')
    assert b'may have been missed' in resp.data


# ---------------------------------------------------------------------------
# F2 - undo last drip
# ---------------------------------------------------------------------------

def test_nothing_undoable_before_any_drip(harness):
    assert driparr.get_undoable_drip() is None


def test_undo_moves_the_file_back(harness, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')
    drip(show_id)

    undoable = driparr.get_undoable_drip()
    assert undoable is not None and undoable['show_name'] == 'Test Show'

    ok, _msg = driparr.undo_drip(undoable['id'])
    assert ok
    assert harness.episode_tags(harness.vault / 'Test Show') == {(1, 1)}
    assert harness.episode_tags(harness.plex / 'Test Show') == set()


def test_undo_clears_the_dripped_marker(harness, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')
    drip(show_id)

    undoable = driparr.get_undoable_drip()
    driparr.undo_drip(undoable['id'])

    with closing(driparr.get_db()) as conn:
        assert driparr.get_dripped_set(conn, show_id) == set()


def test_redrip_works_cleanly_after_undo(harness, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')
    drip(show_id)
    driparr.undo_drip(driparr.get_undoable_drip()['id'])

    drip(show_id)

    assert harness.episode_tags(harness.plex / 'Test Show') == {(1, 1)}


def test_second_undo_reports_nothing_to_undo(harness, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')
    drip(show_id)

    first = driparr.get_undoable_drip()
    driparr.undo_drip(first['id'])

    assert driparr.get_undoable_drip() is None
    ok, msg = driparr.undo_drip(first['id'])
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
    undoable = driparr.get_undoable_drip()
    driparr.undo_drip(undoable['id'])

    assert harness.get_show(show_id)['completed_at'] is None, \
        "undoing the drip that emptied the vault should reopen cooldown"


def test_undo_refuses_for_a_deleted_show(harness, client, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')
    drip(show_id)
    undoable = driparr.get_undoable_drip()

    client.post(f'/delete/{show_id}')

    ok, msg = driparr.undo_drip(undoable['id'])
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
    assert driparr.get_setting('tvdb_api_key', '') == ''

    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = lambda: {'data': {'token': 'test.token'}}

    with patch.object(driparr.requests, 'post', return_value=mock_resp) as mock_post:
        resp = client.post('/api/test-tvdb', data={'tvdb_api_key': 'typed-not-saved', 'tvdb_pin': ''})
        assert resp.status_code == 200
        assert mock_post.call_args.kwargs['json']['apikey'] == 'typed-not-saved'

    # Testing must not have side effects on saved settings
    assert driparr.get_setting('tvdb_api_key', '') == ''
    assert driparr.get_setting('tvdb_token', '') == ''


def test_tvdb_search_parses_mocked_results(harness, client):
    driparr.set_setting('tvdb_api_key', 'fake-key')
    mock_login = MagicMock()
    mock_login.raise_for_status = lambda: None
    mock_login.json = lambda: {'data': {'token': 'fake.token'}}
    mock_search = MagicMock()
    mock_search.raise_for_status = lambda: None
    mock_search.json = lambda: {'data': [
        {'tvdb_id': '999', 'name': 'Mocked Show', 'year': '2021', 'image_url': 'http://example/x.jpg'},
    ]}
    with patch.object(driparr.requests, 'post', return_value=mock_login), \
         patch.object(driparr.requests, 'get', return_value=mock_search):
        resp = client.get('/api/tvdb-search?q=Mocked')
        data = resp.get_json()
        assert data['status'] == 'success'
        assert data['results'][0]['tvdb_id'] == 999
        assert data['results'][0]['name'] == 'Mocked Show'


def test_tvdb_link_stores_poster_and_id(harness, client):
    show_id = harness.add_show('Test Show')
    driparr.set_setting('tvdb_api_key', 'fake-key')

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

    with patch.object(driparr.requests, 'post', return_value=mock_login), \
         patch.object(driparr.requests, 'get', side_effect=[mock_series, mock_episodes, mock_image]):
        resp = client.post(f'/api/tvdb-link/{show_id}', data={'tvdb_id': '555'})
        data = resp.get_json()
        assert data['status'] == 'success'
        # poster_url in the response is now the LOCAL cached path, not the
        # original TVDB hotlink - that's the whole point of caching it.
        assert data['poster_url'].startswith('/posters/')
        assert '0' not in data['season_episode_counts'], "specials must be excluded"

    with closing(driparr.get_db()) as conn:
        row = conn.execute(f"SELECT tvdb_id, poster_url FROM {driparr.TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
    assert row['tvdb_id'] == 555
    assert row['poster_url'].startswith('/posters/')


def test_tvdb_unlink_clears_both_fields(harness, client):
    show_id = harness.add_show('Test Show')
    with closing(driparr.get_db()) as conn:
        conn.execute(f"UPDATE {driparr.TABLE_NAME} SET tvdb_id = 1, poster_url = 'x' WHERE id = ?", (show_id,))
        conn.commit()

    client.post(f'/api/tvdb-unlink/{show_id}')

    with closing(driparr.get_db()) as conn:
        row = conn.execute(f"SELECT tvdb_id, poster_url FROM {driparr.TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
    assert row['tvdb_id'] is None and row['poster_url'] is None


def test_dashboard_renders_linked_poster(harness, client):
    show_id = harness.add_show('Test Show')
    with closing(driparr.get_db()) as conn:
        conn.execute(f"UPDATE {driparr.TABLE_NAME} SET poster_url = ? WHERE id = ?",
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
    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    with patch.object(driparr.requests, 'get', return_value=mock_resp):
        resp = client.post('/api/test-sonarr', data={
            'sonarr_url': 'http://fake-sonarr:8989', 'sonarr_api_key': 'fake-key',
        })
        assert resp.status_code == 200


def test_sonarr_get_series_excludes_specials(harness):
    driparr.set_setting('sonarr_url', 'http://fake-sonarr:8989')
    driparr.set_setting('sonarr_api_key', 'fake-key')

    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = lambda: [{
        'id': 42, 'title': 'Test Show', 'tvdbId': 555,
        'seasons': [
            {'seasonNumber': 0, 'statistics': {'totalEpisodeCount': 3}},
            {'seasonNumber': 1, 'statistics': {'totalEpisodeCount': 10}},
        ],
    }]
    with patch.object(driparr.requests, 'get', return_value=mock_resp):
        details, err = driparr.sonarr_get_series(555)
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

    with patch.object(driparr.requests, 'get', return_value=mock_resp):
        local_url = driparr.cache_poster_locally(show_id, 'http://example.com/poster.jpg')

    assert local_url.startswith('/posters/')
    cached_path = os.path.join(driparr.POSTERS_DIR, os.path.basename(local_url))
    assert os.path.isfile(cached_path)
    assert open(cached_path, 'rb').read() == fake_bytes


def test_cache_poster_locally_falls_back_on_failure(harness):
    show_id = harness.add_show('Test Show')
    with patch.object(driparr.requests, 'get', side_effect=driparr.requests.RequestException("boom")):
        result = driparr.cache_poster_locally(show_id, 'http://example.com/poster.jpg')
    assert result == 'http://example.com/poster.jpg', \
        "a failed download should fall back to the original hotlink, not lose the poster entirely"


def test_cached_poster_is_served_by_its_route(harness, client):
    show_id = harness.add_show('Test Show')
    fake_bytes = b'\xff\xd8\xff\xe0FAKEJPEG'
    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.iter_content = lambda chunk_size: [fake_bytes]
    with patch.object(driparr.requests, 'get', return_value=mock_resp):
        local_url = driparr.cache_poster_locally(show_id, 'http://example.com/poster.jpg')

    resp = client.get(local_url)
    assert resp.status_code == 200
    assert resp.data == fake_bytes


def test_delete_cached_poster_removes_the_file(harness):
    show_id = harness.add_show('Test Show')
    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.iter_content = lambda chunk_size: [b'x']
    with patch.object(driparr.requests, 'get', return_value=mock_resp):
        local_url = driparr.cache_poster_locally(show_id, 'http://example.com/poster.jpg')
    cached_path = os.path.join(driparr.POSTERS_DIR, os.path.basename(local_url))
    assert os.path.isfile(cached_path)

    driparr.delete_cached_poster(local_url)
    assert not os.path.isfile(cached_path)


def test_delete_cached_poster_ignores_hotlink_urls(harness):
    """A poster that failed to cache (still a raw hotlink) shouldn't cause
    delete_cached_poster to try deleting something that isn't a local file."""
    driparr.delete_cached_poster('http://example.com/never-cached.jpg')  # must not raise


def test_unlinking_a_show_removes_its_cached_poster_file(harness, client):
    show_id = harness.add_show('Test Show')
    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.iter_content = lambda chunk_size: [b'x']
    with patch.object(driparr.requests, 'get', return_value=mock_resp):
        local_url = driparr.cache_poster_locally(show_id, 'http://example.com/poster.jpg')
    with closing(driparr.get_db()) as conn:
        conn.execute(f"UPDATE {driparr.TABLE_NAME} SET poster_url = ? WHERE id = ?", (local_url, show_id))
        conn.commit()
    cached_path = os.path.join(driparr.POSTERS_DIR, os.path.basename(local_url))

    client.post(f'/api/tvdb-unlink/{show_id}')

    assert not os.path.isfile(cached_path)


# ---------------------------------------------------------------------------
# New: disk space check on promote
# ---------------------------------------------------------------------------

def test_inspect_warns_when_show_exceeds_free_space(harness, client):
    show = harness.pool / 'BigShow'
    show.mkdir(parents=True)
    (show / 'BigShow - S01E01.mkv').write_bytes(b'x' * 5000)

    with patch.object(driparr.shutil, 'disk_usage', return_value=type('D', (), {'free': 1000})()):
        resp = client.get('/api/inspect?show=BigShow')
        data = resp.get_json()
        assert data['space_warning'] is not None
        assert 'free' in data['space_warning'].lower()


def test_inspect_does_not_warn_when_space_is_sufficient(harness, client):
    show = harness.pool / 'SmallShow'
    show.mkdir(parents=True)
    (show / 'SmallShow - S01E01.mkv').write_bytes(b'x' * 100)

    with patch.object(driparr.shutil, 'disk_usage', return_value=type('D', (), {'free': 999999999})()):
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
    names = [r[0] for r in check_conn.execute(f"SELECT show_name FROM {driparr.TABLE_NAME}").fetchall()]
    check_conn.close()
    assert 'Backup Test Show' in names


# ---------------------------------------------------------------------------
# New: Discord poster embeds
# ---------------------------------------------------------------------------

def test_discord_notification_includes_poster_embed(harness):
    driparr.set_setting('webhook_url', 'https://discord.com/api/webhooks/12345/abcdefTOKEN')
    mock_resp = MagicMock()
    with patch.object(driparr.requests, 'post', return_value=mock_resp) as p:
        _REAL_SEND_NOTIFICATION('dripped', "test message", poster_url='http://example/poster.jpg')
        payload = p.call_args.kwargs['json']
    assert 'embeds' in payload
    assert payload['embeds'][0]['thumbnail']['url'] == 'http://example/poster.jpg'
    assert payload['content'] == "test message", "plain content should still be sent alongside the embed"


def test_non_discord_webhook_gets_no_embed(harness):
    driparr.set_setting('webhook_url', 'https://hooks.slack.com/services/FAKE')
    mock_resp = MagicMock()
    with patch.object(driparr.requests, 'post', return_value=mock_resp) as p:
        _REAL_SEND_NOTIFICATION('dripped', "test", poster_url='http://example/poster.jpg')
        payload = p.call_args.kwargs['json']
    assert 'embeds' not in payload


def test_discord_webhook_without_poster_sends_plain_message(harness):
    driparr.set_setting('webhook_url', 'https://discord.com/api/webhooks/12345/abcdefTOKEN')
    mock_resp = MagicMock()
    with patch.object(driparr.requests, 'post', return_value=mock_resp) as p:
        _REAL_SEND_NOTIFICATION('dripped', "test", poster_url=None)
        payload = p.call_args.kwargs['json']
    assert 'embeds' not in payload


def test_dripped_notification_carries_the_shows_poster(harness, drip):
    """End-to-end: a real drip on a show with a linked poster should pass
    that poster through to send_notification."""
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')
    with closing(driparr.get_db()) as conn:
        conn.execute(f"UPDATE {driparr.TABLE_NAME} SET poster_url = ? WHERE id = ?",
                     ('/posters/1.jpg', show_id))
        conn.commit()

    captured = {}
    def fake_notify(event, message, extra=None, poster_url=None):
        captured['poster_url'] = poster_url
    original = driparr.send_notification
    driparr.send_notification = fake_notify
    try:
        drip(show_id)
    finally:
        driparr.send_notification = original

    assert captured.get('poster_url') == '/posters/1.jpg'


# ---------------------------------------------------------------------------
# New: schedule page poster propagation
# ---------------------------------------------------------------------------

def test_project_schedule_carries_poster_url(harness):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')
    with closing(driparr.get_db()) as conn:
        conn.execute(f"UPDATE {driparr.TABLE_NAME} SET poster_url = ? WHERE id = ?",
                     ('/posters/99.jpg', show_id))
        conn.commit()

    events = driparr.project_schedule(weeks_ahead=2)
    assert events, "expected at least one projected event"
    assert all(e.get('poster_url') == '/posters/99.jpg' for e in events if e['show_name'] == 'Test Show')


def test_schedule_page_renders_poster(harness, client):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')
    with closing(driparr.get_db()) as conn:
        conn.execute(f"UPDATE {driparr.TABLE_NAME} SET poster_url = ? WHERE id = ?",
                     ('/posters/77.jpg', show_id))
        conn.commit()

    resp = client.get('/schedule?weeks=2')
    assert resp.status_code == 200
    assert b'/posters/77.jpg' in resp.data
