"""Tests for the six Sonarr/Radarr-inspired features: per-episode exclude,
series detail page, bulk actions, system health page, granular
notifications, and show succession queues.
"""

import os
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch, MagicMock

import app as driparr

_REAL_SEND_NOTIFICATION = driparr.send_notification


# ---------------------------------------------------------------------------
# Per-episode exclude
# ---------------------------------------------------------------------------

def test_excluded_episode_never_drips(harness, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    harness.make_episode(show, 'Test Show', 1, 2, subdir='Season 01')
    show_id = harness.add_show('Test Show')

    with closing(driparr.get_db()) as conn:
        driparr.exclude_episode(conn, show_id, 1, 1)
        conn.commit()

    drip(show_id)

    assert harness.episode_tags(harness.plex / 'Test Show') == {(1, 2)}, \
        "excluded episode should be skipped, the other should drip"


def test_excluded_only_vault_counts_as_empty(harness, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')

    with closing(driparr.get_db()) as conn:
        driparr.exclude_episode(conn, show_id, 1, 1)
        conn.commit()

    drip(show_id)

    assert harness.get_show(show_id)['completed_at'] is not None, \
        "a vault containing only excluded episodes should start cooldown"


def test_unexclude_makes_it_eligible_again(harness, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')

    with closing(driparr.get_db()) as conn:
        driparr.exclude_episode(conn, show_id, 1, 1)
        driparr.unexclude_episode(conn, show_id, 1, 1)
        conn.commit()

    drip(show_id)
    assert harness.episode_tags(harness.plex / 'Test Show') == {(1, 1)}


def test_exclude_route_toggles_correctly(harness, client):
    show_id = harness.add_show('Test Show')

    r = client.post(f'/api/exclude-episode/{show_id}', data={'season': 1, 'episode': 1, 'action': 'exclude'})
    assert r.status_code == 200
    with closing(driparr.get_db()) as conn:
        assert (1, 1) in driparr.get_excluded_set(conn, show_id)

    r = client.post(f'/api/exclude-episode/{show_id}', data={'season': 1, 'episode': 1, 'action': 'unexclude'})
    assert r.status_code == 200
    with closing(driparr.get_db()) as conn:
        assert (1, 1) not in driparr.get_excluded_set(conn, show_id)


def test_deleting_a_show_clears_its_exclusions(harness, client):
    show_id = harness.add_show('Test Show')
    with closing(driparr.get_db()) as conn:
        driparr.exclude_episode(conn, show_id, 1, 1)
        conn.commit()

    client.post(f'/delete/{show_id}')

    with closing(driparr.get_db()) as conn:
        assert driparr.get_excluded_set(conn, show_id) == set()


# ---------------------------------------------------------------------------
# Series detail page
# ---------------------------------------------------------------------------

def test_show_detail_page_renders(harness, client):
    show_id = harness.add_show('Test Show')
    r = client.get(f'/show/{show_id}')
    assert r.status_code == 200
    assert b'Test Show' in r.data


def test_show_detail_404s_for_unknown_show(harness, client):
    r = client.get('/show/999999')
    assert r.status_code == 404


def test_episode_grid_marks_dripped_and_waiting_correctly(harness, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    harness.make_episode(show, 'Test Show', 1, 2, subdir='Season 01')
    show_id = harness.add_show('Test Show')
    drip(show_id)

    with closing(driparr.get_db()) as conn:
        row = conn.execute(f"SELECT * FROM {driparr.TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
    grid = driparr.build_episode_grid(show_id, dict(row))

    statuses = {(1, e['episode']): e['status'] for e in grid.get(1, [])}
    assert statuses[(1, 1)] == 'dripped'
    assert statuses[(1, 2)] == 'waiting'


def test_episode_grid_marks_excluded(harness):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')
    with closing(driparr.get_db()) as conn:
        driparr.exclude_episode(conn, show_id, 1, 1)
        conn.commit()
        row = conn.execute(f"SELECT * FROM {driparr.TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()

    grid = driparr.build_episode_grid(show_id, dict(row))
    assert grid[1][0]['status'] == 'excluded'


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------

def test_bulk_pause_pauses_all_selected(harness, client):
    id1 = harness.add_show('Show One')
    id2 = harness.add_show('Show Two')

    r = client.post('/api/bulk/pause', json={'show_ids': [id1, id2], 'paused': True})
    assert r.status_code == 200
    with closing(driparr.get_db()) as conn:
        rows = conn.execute(f"SELECT paused FROM {driparr.TABLE_NAME} WHERE id IN (?, ?)", (id1, id2)).fetchall()
    assert all(r['paused'] == 1 for r in rows)


def test_bulk_pause_resume_toggles_correctly(harness, client):
    show_id = harness.add_show('Test Show')
    client.post('/api/bulk/pause', json={'show_ids': [show_id], 'paused': True})
    client.post('/api/bulk/pause', json={'show_ids': [show_id], 'paused': False})
    with closing(driparr.get_db()) as conn:
        row = conn.execute(f"SELECT paused FROM {driparr.TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
    assert row['paused'] == 0


def test_bulk_delete_returns_files_and_removes_rows(harness, client):
    harness.make_episode(harness.vault / 'Show One', 'Show One', 1, 1, subdir='Season 01')
    id1 = harness.add_show('Show One')

    r = client.post('/api/bulk/delete', json={'show_ids': [id1]})
    assert r.status_code == 200
    with closing(driparr.get_db()) as conn:
        row = conn.execute(f"SELECT id FROM {driparr.TABLE_NAME} WHERE id = ?", (id1,)).fetchone()
    assert row is None
    assert harness.episode_tags(harness.pool / 'Show One') == {(1, 1)}


def test_bulk_delete_skips_unsafe_names_without_crashing(harness, client):
    id1 = harness.add_show('Test Show')
    with closing(driparr.get_db()) as conn:
        conn.execute(f"UPDATE {driparr.TABLE_NAME} SET show_name = ? WHERE id = ?", ('../evil', id1))
        conn.commit()

    r = client.post('/api/bulk/delete', json={'show_ids': [id1]})
    assert r.status_code in (200, 400)  # must not 500
    data = r.get_json()
    assert 'could not be removed' in data['message'].lower() or data['status'] == 'error'


def test_bulk_action_with_no_ids_fails_cleanly(harness, client):
    r = client.post('/api/bulk/pause', json={'show_ids': [], 'paused': True})
    assert r.status_code == 400
    r = client.post('/api/bulk/delete', json={'show_ids': []})
    assert r.status_code == 400


def test_bulk_drip_now_runs_each_show(harness, client):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')

    r = client.post('/api/bulk/drip-now', json={'show_ids': [show_id]})
    assert r.status_code == 200
    assert harness.episode_tags(harness.plex / 'Test Show') == {(1, 1)}


# ---------------------------------------------------------------------------
# System health page
# ---------------------------------------------------------------------------

def test_system_page_renders(harness, client):
    r = client.get('/system')
    assert r.status_code == 200


def test_system_status_api_returns_expected_shape(harness, client):
    r = client.get('/api/system-status')
    data = r.get_json()
    assert data['overall'] in ('ok', 'warn', 'error')
    labels = [c['label'] for c in data['checks']]
    assert 'Database' in labels
    assert 'Scheduler' in labels
    assert 'Vault disk space' in labels


def test_system_checks_flag_missing_tvdb_as_warning_not_error(harness):
    checks = driparr.gather_system_checks()
    tvdb_check = next(c for c in checks if c['label'] == 'TVDB')
    assert tvdb_check['status'] == 'warn', "unconfigured optional integration should warn, not error"


def test_system_checks_flag_low_disk_space(harness):
    with patch.object(driparr.shutil, 'disk_usage', return_value=type('D', (), {'free': 500 * 1024 * 1024})()):
        checks = driparr.gather_system_checks()
    disk_check = next(c for c in checks if c['label'] == 'Vault disk space')
    assert disk_check['status'] == 'error'


# ---------------------------------------------------------------------------
# Granular notification triggers
# ---------------------------------------------------------------------------

def test_drip_notification_respects_notify_on_drip_setting(harness):
    driparr.set_setting('webhook_url', 'https://discord.com/api/webhooks/1/token')
    driparr.set_setting('notify_on_drip', '0')
    mock_resp = MagicMock()
    with patch.object(driparr.requests, 'post', return_value=mock_resp) as p:
        _REAL_SEND_NOTIFICATION('dripped', 'test message')
        p.assert_not_called()


def test_drip_notification_fires_when_enabled(harness):
    driparr.set_setting('webhook_url', 'https://discord.com/api/webhooks/1/token')
    driparr.set_setting('notify_on_drip', '1')
    mock_resp = MagicMock()
    with patch.object(driparr.requests, 'post', return_value=mock_resp) as p:
        _REAL_SEND_NOTIFICATION('dripped', 'test message')
        p.assert_called_once()


def test_failure_notification_gated_independently_of_drip_setting(harness):
    driparr.set_setting('webhook_url', 'https://discord.com/api/webhooks/1/token')
    driparr.set_setting('notify_on_drip', '0')
    driparr.set_setting('notify_on_failure', '1')
    mock_resp = MagicMock()
    with patch.object(driparr.requests, 'post', return_value=mock_resp) as p:
        _REAL_SEND_NOTIFICATION('failure', 'something broke')
        p.assert_called_once()


def test_missed_run_notification_gated_by_its_own_setting(harness):
    driparr.set_setting('webhook_url', 'https://discord.com/api/webhooks/1/token')
    driparr.set_setting('notify_on_missed_run', '0')
    mock_resp = MagicMock()
    with patch.object(driparr.requests, 'post', return_value=mock_resp) as p:
        _REAL_SEND_NOTIFICATION('missed_run', 'gap detected')
        p.assert_not_called()


def test_settings_page_persists_notification_toggles(harness, client):
    client.post('/settings', data={
        'drip_hour': '3', 'drip_minute': '0', 'webhook_url': '', 'discord_webhook_url': '',
        'notify_on_drip': 'on',
    })
    assert driparr.get_setting('notify_on_drip', '0') == '1'
    assert driparr.get_setting('notify_on_failure', '1') == '0'
    assert driparr.get_setting('notify_on_missed_run', '1') == '0'


# ---------------------------------------------------------------------------
# Show succession queue
# ---------------------------------------------------------------------------

def test_after_show_queue_fires_on_graduation(harness, drip):
    show_a = harness.vault / 'Show A'
    harness.make_episode(show_a, 'Show A', 1, 1, subdir='Season 01')
    show_a_id = harness.add_show('Show A', release_days='0,1,2,3,4,5,6')

    os.makedirs(str(harness.pool / 'Show B' / 'Season 01'), exist_ok=True)
    (harness.pool / 'Show B' / 'Season 01' / 'Show B - S01E01.mkv').write_text('x')

    with closing(driparr.get_db()) as conn:
        conn.execute(f"""INSERT INTO {driparr.QUEUE_TABLE}
            (show_name, trigger_type, trigger_show_id, release_days, episodes_per_drop, created_at)
            VALUES ('Show B', 'after_show', ?, '0,1,2,3,4,5,6', 1, ?)""",
            (show_a_id, driparr.now_local().isoformat()))
        conn.commit()

    drip(show_a_id)  # drips the only episode
    drip(show_a_id)  # vault now empty -> cooldown
    drip(show_a_id)  # graduates -> should fire the queue

    with closing(driparr.get_db()) as conn:
        row_a = conn.execute(f"SELECT id FROM {driparr.TABLE_NAME} WHERE show_name = 'Show A'").fetchone()
        row_b = conn.execute(f"SELECT id FROM {driparr.TABLE_NAME} WHERE show_name = 'Show B'").fetchone()
    assert row_a is None, "Show A should have graduated"
    assert row_b is not None, "Show B should have been auto-promoted"
    assert not (harness.pool / 'Show B').exists()
    assert (harness.vault / 'Show B').exists()


def test_date_trigger_does_not_fire_before_its_date(harness):
    future = (driparr.today_local() + timedelta(days=3)).isoformat()
    os.makedirs(str(harness.pool / 'Future Show'), exist_ok=True)
    with closing(driparr.get_db()) as conn:
        conn.execute(f"""INSERT INTO {driparr.QUEUE_TABLE}
            (show_name, trigger_type, trigger_date, release_days, episodes_per_drop, created_at)
            VALUES ('Future Show', 'date', ?, '0', 1, ?)""",
            (future, driparr.now_local().isoformat()))
        conn.commit()

    fired = driparr.check_and_advance_queue(trigger_show_id=None)
    assert fired == []
    assert (harness.pool / 'Future Show').exists()


def test_date_trigger_fires_once_date_arrives(harness):
    today = driparr.today_local().isoformat()
    os.makedirs(str(harness.pool / 'Today Show'), exist_ok=True)
    with closing(driparr.get_db()) as conn:
        conn.execute(f"""INSERT INTO {driparr.QUEUE_TABLE}
            (show_name, trigger_type, trigger_date, release_days, episodes_per_drop, created_at)
            VALUES ('Today Show', 'date', ?, '0', 1, ?)""",
            (today, driparr.now_local().isoformat()))
        conn.commit()

    fired = driparr.check_and_advance_queue(trigger_show_id=None)
    assert len(fired) == 1 and fired[0][1] is True
    assert not (harness.pool / 'Today Show').exists()
    with closing(driparr.get_db()) as conn:
        row = conn.execute(f"SELECT id FROM {driparr.TABLE_NAME} WHERE show_name = 'Today Show'").fetchone()
    assert row is not None


def test_deleting_anchor_show_also_advances_its_queue(harness, client):
    anchor_id = harness.add_show('Anchor Show')
    os.makedirs(str(harness.pool / 'Successor'), exist_ok=True)
    with closing(driparr.get_db()) as conn:
        conn.execute(f"""INSERT INTO {driparr.QUEUE_TABLE}
            (show_name, trigger_type, trigger_show_id, release_days, episodes_per_drop, created_at)
            VALUES ('Successor', 'after_show', ?, '0', 1, ?)""",
            (anchor_id, driparr.now_local().isoformat()))
        conn.commit()

    client.post(f'/delete/{anchor_id}')

    with closing(driparr.get_db()) as conn:
        row = conn.execute(f"SELECT id FROM {driparr.TABLE_NAME} WHERE show_name = 'Successor'").fetchone()
    assert row is not None


def test_queue_entry_for_missing_pool_folder_is_not_dropped(harness):
    """A queue entry pointing at a pool folder that doesn't exist (renamed,
    moved, deleted) should stay pending rather than vanish - the user might
    fix the folder later, and silently losing the entry would be worse."""
    with closing(driparr.get_db()) as conn:
        conn.execute(f"""INSERT INTO {driparr.QUEUE_TABLE}
            (show_name, trigger_type, trigger_date, release_days, episodes_per_drop, created_at)
            VALUES ('Nonexistent Show', 'date', ?, '0', 1, ?)""",
            (driparr.today_local().isoformat(), driparr.now_local().isoformat()))
        conn.commit()

    fired = driparr.check_and_advance_queue(trigger_show_id=None)
    assert len(fired) == 1 and fired[0][1] is False

    with closing(driparr.get_db()) as conn:
        row = conn.execute(f"SELECT triggered_at FROM {driparr.QUEUE_TABLE} WHERE show_name = 'Nonexistent Show'").fetchone()
    assert row['triggered_at'] is None, "entry should remain pending, not be marked triggered"


def test_add_and_remove_queue_entry_via_api(harness, client):
    show_id = harness.add_show('Test Show')
    os.makedirs(str(harness.pool / 'Queued Show'), exist_ok=True)

    r = client.post(f'/api/queue/{show_id}', data={
        'show_name': 'Queued Show', 'trigger_type': 'after_show',
        'release_days': '0', 'episodes_per_drop': '1',
    })
    assert r.status_code == 200

    r = client.get(f'/api/queue/{show_id}')
    entries = r.get_json()['entries']
    assert len(entries) == 1
    entry_id = entries[0]['id']

    r = client.delete(f'/api/queue/entry/{entry_id}')
    assert r.status_code == 200

    r = client.get(f'/api/queue/{show_id}')
    assert r.get_json()['entries'] == []


def test_queue_entry_requires_pool_folder_to_exist(harness, client):
    show_id = harness.add_show('Test Show')
    r = client.post(f'/api/queue/{show_id}', data={
        'show_name': 'Does Not Exist Anywhere', 'trigger_type': 'after_show',
        'release_days': '0', 'episodes_per_drop': '1',
    })
    assert r.status_code == 404


def test_date_trigger_requires_a_date(harness, client):
    show_id = harness.add_show('Test Show')
    os.makedirs(str(harness.pool / 'Some Show'), exist_ok=True)
    r = client.post(f'/api/queue/{show_id}', data={
        'show_name': 'Some Show', 'trigger_type': 'date',
        'release_days': '0', 'episodes_per_drop': '1',
    })
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Tags (UI wiring - backend parsing already covered above)
# ---------------------------------------------------------------------------

def test_dashboard_displays_and_filters_by_tag(harness, client):
    show_id = harness.add_show('Test Show')
    with closing(driparr.get_db()) as conn:
        conn.execute(f"UPDATE {driparr.TABLE_NAME} SET tags = ? WHERE id = ?", ('Anime,Kids', show_id))
        conn.commit()

    resp = client.get('/')
    assert b'Anime' in resp.data and b'Kids' in resp.data
    assert b'data-tags="anime,kids"' in resp.data


def test_edit_form_saves_and_redisplays_tags(harness, client):
    show_id = harness.add_show('Test Show')
    client.post(f'/edit/{show_id}', data={
        'show_name': 'Test Show', 'vault_path': str(harness.vault / 'Test Show'),
        'plex_path': str(harness.plex / 'Test Show'), 'release_days': '0',
        'current_season': '1', 'current_episode': '0', 'episodes_per_drop': '1',
        'tags': 'Comedy, Drama, comedy',
    })
    with closing(driparr.get_db()) as conn:
        row = conn.execute(f"SELECT tags FROM {driparr.TABLE_NAME} WHERE id = ?", (show_id,)).fetchone()
    assert driparr.parse_tags(row['tags']) == ['Comedy', 'Drama']

    resp = client.get(f'/edit/{show_id}')
    assert b'Comedy' in resp.data


# ---------------------------------------------------------------------------
# Scheduled backups (UI wiring - core logic already covered above)
# ---------------------------------------------------------------------------

def test_settings_persists_backup_toggle_and_retention(harness, client):
    client.post('/settings', data={
        'drip_hour': '3', 'drip_minute': '0', 'webhook_url': '', 'discord_webhook_url': '',
        'auto_backup_enabled': 'on', 'backup_retention_count': '5',
    })
    assert driparr.get_setting('auto_backup_enabled', '0') == '1'
    assert driparr.get_setting('backup_retention_count', '7') == '5'


def test_backup_retention_is_clamped_to_a_sane_range(harness, client):
    client.post('/settings', data={
        'drip_hour': '3', 'drip_minute': '0', 'webhook_url': '', 'discord_webhook_url': '',
        'backup_retention_count': '9999',
    })
    assert int(driparr.get_setting('backup_retention_count', '7')) <= 90


def test_settings_page_shows_existing_backups(harness, client):
    driparr.create_db_backup()
    resp = client.get('/settings')
    assert resp.status_code == 200


def test_run_backup_now_route_creates_a_listed_backup(harness, client):
    r = client.post('/api/run-backup-now')
    assert r.status_code == 200
    r2 = client.get('/api/backups')
    assert len(r2.get_json()['backups']) == 1


def test_backup_filenames_never_collide_even_in_rapid_succession(harness):
    """Regression guard: second-resolution timestamps collided when
    multiple backups were created within the same second, silently
    overwriting one with another instead of keeping both."""
    paths = [driparr.create_db_backup() for _ in range(5)]
    assert len(set(paths)) == 5, "all five backups must be distinct files"
    for p in paths:
        assert os.path.isfile(p)


def test_prune_keeps_the_most_recent_backups(harness):
    for _ in range(5):
        driparr.create_db_backup()
    before = sorted(os.listdir(driparr.BACKUPS_DIR), reverse=True)
    driparr.prune_old_backups(keep=2)
    after = sorted(os.listdir(driparr.BACKUPS_DIR), reverse=True)
    assert after == before[:2]


# ---------------------------------------------------------------------------
# Manual import matching (UI wiring - core logic already covered above)
# ---------------------------------------------------------------------------

def test_show_detail_page_includes_unmatched_files_markup(harness, client):
    show_id = harness.add_show('Test Show')
    resp = client.get(f'/show/{show_id}')
    assert b'unmatchedCard' in resp.data
    assert b'loadUnmatchedFiles' in resp.data


def test_unmatched_file_becomes_visible_after_manual_match(harness, client):
    show = harness.vault / 'Test Show'
    show.mkdir(parents=True)
    (show / 'random_name_no_pattern.mkv').write_text('x')
    show_id = harness.add_show('Test Show')

    r = client.get(f'/api/unmatched-files/{show_id}')
    files = r.get_json()['files']
    assert len(files) == 1

    r2 = client.post(f'/api/manual-match/{show_id}', data={
        'rel_path': files[0]['rel_path'], 'location': 'vault', 'season': '4', 'episode': '2',
    })
    assert r2.status_code == 200

    tags = driparr.scan_episode_files(str(show))
    assert (4, 2) in [(t['season'], t['episode']) for t in tags]

    r3 = client.get(f'/api/unmatched-files/{show_id}')
    assert r3.get_json()['files'] == []


def test_manual_match_ignores_non_video_files(harness, client):
    show = harness.vault / 'Test Show'
    show.mkdir(parents=True)
    (show / 'poster.jpg').write_text('x')
    (show / 'notes.txt').write_text('x')
    show_id = harness.add_show('Test Show')

    r = client.get(f'/api/unmatched-files/{show_id}')
    assert r.get_json()['files'] == []
