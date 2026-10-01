"""Behaviour that currently works correctly.

These are the regression net for C1. The watermark rewrite touches the four
comparisons that decide which files move, so everything the app gets right
today needs to be pinned down before that change lands.

All of these should be GREEN on the current code.
"""

import os

import app as unbinge


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def test_scan_finds_tagged_files_and_sorts_them(harness):
    show = harness.pool / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 3, subdir='Season 01')
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    harness.make_episode(show, 'Test Show', 2, 1, subdir='Season 02')

    found = unbinge.scan_episode_files(str(show))

    assert [(e['season'], e['episode']) for e in found] == [(1, 1), (1, 3), (2, 1)]


def test_scan_ignores_untagged_files(harness):
    show = harness.pool / 'Test Show'
    show.mkdir(parents=True)
    (show / 'poster.jpg').write_text('art')
    (show / 'readme.txt').write_text('notes')
    harness.make_episode(show, 'Test Show', 1, 1)

    found = unbinge.scan_episode_files(str(show))

    assert len(found) == 1


def test_scan_returns_empty_for_missing_directory(harness):
    assert unbinge.scan_episode_files(str(harness.pool / 'nope')) == []


# ---------------------------------------------------------------------------
# Release-day parsing
# ---------------------------------------------------------------------------

def test_parse_release_days_handles_multiple_and_legacy_single():
    assert unbinge.parse_release_days('0,3') == [0, 3]
    assert unbinge.parse_release_days('5') == [5]
    assert unbinge.parse_release_days('') == []
    assert unbinge.parse_release_days(None) == []
    assert unbinge.parse_release_days('3,0,3') == [0, 3]


def test_parse_release_days_survives_garbage():
    assert unbinge.parse_release_days('not,a,day') == []


def test_next_occurrence_respects_inclusive_flag():
    from datetime import date
    monday = date(2026, 9, 7)
    assert monday.weekday() == 0
    assert unbinge.next_occurrence(monday, [0], inclusive=True) == monday
    assert unbinge.next_occurrence(monday, [0], inclusive=False) == date(2026, 9, 14)


# ---------------------------------------------------------------------------
# The core drip cycle
# ---------------------------------------------------------------------------

def test_drip_moves_one_episode_to_plex(harness, drip):
    harness.make_show_in_pool('Test Show', [(1, 1), (1, 2), (1, 3)])
    os.rename(harness.pool / 'Test Show', harness.vault / 'Test Show')
    show_id = harness.add_show('Test Show')

    drip(show_id)

    assert harness.episode_tags(harness.plex / 'Test Show') == {(1, 1)}
    assert harness.episode_tags(harness.vault / 'Test Show') == {(1, 2), (1, 3)}


def test_drip_advances_the_stored_position(harness, drip):
    harness.make_show_in_pool('Test Show', [(1, 1), (1, 2)])
    os.rename(harness.pool / 'Test Show', harness.vault / 'Test Show')
    show_id = harness.add_show('Test Show')

    drip(show_id)

    show = harness.get_show(show_id)
    assert (show['current_season'], show['current_episode']) == (1, 1)


def test_episodes_per_drop_counts_episodes_not_files(harness, drip):
    """An episode with a subtitle is one episode, two files. A drop of 2
    should move 2 episodes (4 files), not 2 files."""
    show = harness.vault / 'Test Show'
    for ep in (1, 2, 3):
        harness.make_episode(show, 'Test Show', 1, ep, ext='mkv', subdir='Season 01')
        harness.make_episode(show, 'Test Show', 1, ep, ext='srt', subdir='Season 01')
    show_id = harness.add_show('Test Show', episodes_per_drop=2)

    drip(show_id)

    assert harness.episode_tags(harness.plex / 'Test Show') == {(1, 1), (1, 2)}
    assert len(harness.files_under(harness.plex / 'Test Show')) == 4


def test_sidecar_files_travel_with_their_episode(harness, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, ext='mkv', subdir='Season 01')
    harness.make_episode(show, 'Test Show', 1, 1, ext='srt', subdir='Season 01')
    harness.make_episode(show, 'Test Show', 1, 2, ext='mkv', subdir='Season 01')
    show_id = harness.add_show('Test Show')

    drip(show_id)

    moved = harness.files_under(harness.plex / 'Test Show')
    assert any(f.endswith('.srt') for f in moved)
    assert len([f for f in moved if 'S01E01' in f]) == 2


def test_season_folder_structure_is_preserved(harness, drip):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 2, 1, subdir='Season 02')
    show_id = harness.add_show('Test Show')

    drip(show_id)

    moved = harness.files_under(harness.plex / 'Test Show')
    assert moved and moved[0].startswith('Season 02')


def test_show_level_artwork_is_copied_not_moved(harness, drip):
    show = harness.vault / 'Test Show'
    show.mkdir(parents=True)
    (show / 'poster.jpg').write_text('art')
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    harness.make_episode(show, 'Test Show', 1, 2, subdir='Season 01')
    show_id = harness.add_show('Test Show')

    drip(show_id)

    assert (harness.plex / 'Test Show' / 'poster.jpg').exists()
    assert (harness.vault / 'Test Show' / 'poster.jpg').exists(), \
        "artwork should be copied so later drips still have it"


# ---------------------------------------------------------------------------
# Cooldown and graduation
# ---------------------------------------------------------------------------

def test_empty_vault_starts_cooldown(harness, drip):
    (harness.vault / 'Test Show').mkdir(parents=True)
    show_id = harness.add_show('Test Show', current_season=1, current_episode=10)

    drip(show_id)

    assert harness.get_show(show_id)['completed_at'] is not None
    assert 'cooldown' in harness.history_actions()


def test_cooldown_then_graduate_returns_show_to_pool(harness, drip):
    show = harness.vault / 'Test Show'
    show.mkdir(parents=True)
    plex_show = harness.plex / 'Test Show'
    harness.make_episode(plex_show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show', current_season=1, current_episode=1,
                               completed_at='2026-09-01T03:00:00')

    drip(show_id)

    assert harness.get_show(show_id) is None, "graduated shows are removed from the table"
    assert harness.episode_tags(harness.pool / 'Test Show') == {(1, 1)}
    assert 'graduated' in harness.history_actions()


def test_episodes_reappearing_cancels_cooldown(harness, drip):
    """The safety net that should have caught the White Lotus incident."""
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 2, 5, subdir='Season 02')
    show_id = harness.add_show('Test Show', current_season=2, current_episode=4,
                               completed_at='2026-09-01T03:00:00')

    drip(show_id)

    assert harness.get_show(show_id) is not None, "must not graduate"
    assert harness.get_show(show_id)['completed_at'] is None
    assert 'resumed' in harness.history_actions()


# ---------------------------------------------------------------------------
# Job gating
# ---------------------------------------------------------------------------

def test_paused_shows_are_skipped(harness, monkeypatch):
    from datetime import datetime
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show', release_days='0,1,2,3,4,5,6', paused=1)

    unbinge.run_drip_job()

    assert harness.episode_tags(harness.vault / 'Test Show') == {(1, 1)}, \
        "paused show should not have moved anything"


def test_show_is_skipped_when_today_is_not_a_release_day(harness):
    today = unbinge.now_local().weekday()
    other_day = (today + 3) % 7
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    harness.add_show('Test Show', release_days=str(other_day))

    unbinge.run_drip_job()

    assert harness.episode_tags(harness.vault / 'Test Show') == {(1, 1)}


def test_same_day_double_run_does_not_double_drip(harness):
    today = unbinge.now_local().weekday()
    show = harness.vault / 'Test Show'
    for ep in (1, 2, 3):
        harness.make_episode(show, 'Test Show', 1, ep, subdir='Season 01')
    harness.add_show('Test Show', release_days=str(today))

    unbinge.run_drip_job()
    unbinge.run_drip_job()

    assert harness.episode_tags(harness.plex / 'Test Show') == {(1, 1)}, \
        "last_run_date should gate the second run"


def test_force_show_id_ignores_day_and_pause_gating(harness):
    today = unbinge.now_local().weekday()
    other_day = (today + 3) % 7
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show', release_days=str(other_day), paused=1)

    unbinge.run_drip_job(force_show_id=show_id)

    assert harness.episode_tags(harness.plex / 'Test Show') == {(1, 1)}


# ---------------------------------------------------------------------------
# Preview must agree with the real thing (guards H1)
# ---------------------------------------------------------------------------

def test_preview_matches_what_the_drip_actually_does(harness, drip):
    show = harness.vault / 'Test Show'
    for ep in (1, 2, 3):
        harness.make_episode(show, 'Test Show', 1, ep, subdir='Season 01')
    show_id = harness.add_show('Test Show')

    preview = unbinge.preview_show_drip(harness.get_show(show_id))
    assert preview['action'] == 'drip'
    assert 'S01E01' in preview['detail']

    drip(show_id)
    assert harness.episode_tags(harness.plex / 'Test Show') == {(1, 1)}


def test_preview_does_not_touch_the_filesystem(harness):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')

    before = harness.files_under(harness.vault)
    unbinge.preview_show_drip(harness.get_show(show_id))

    assert harness.files_under(harness.vault) == before
    assert harness.files_under(harness.plex) == []


# ---------------------------------------------------------------------------
# Sprint 1 changes
# ---------------------------------------------------------------------------

def test_delete_writes_a_history_row(harness, client):
    """C2 - deletions used to vanish without a trace."""
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 2, subdir='Season 01')
    harness.make_episode(harness.plex / 'Test Show', 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')

    client.post(f'/delete/{show_id}')

    entries = harness.history_for('Test Show')
    assert any(e['action'] == 'deleted' for e in entries)
    deleted = [e for e in entries if e['action'] == 'deleted'][0]
    assert '2 file(s)' in deleted['detail']


def test_delete_returns_files_to_the_pool(harness, client):
    harness.make_episode(harness.vault / 'Test Show', 'Test Show', 1, 2, subdir='Season 01')
    harness.make_episode(harness.plex / 'Test Show', 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')

    client.post(f'/delete/{show_id}')

    assert harness.episode_tags(harness.pool / 'Test Show') == {(1, 1), (1, 2)}


def test_promote_rolls_back_when_the_move_fails(harness, client, monkeypatch):
    """C5 - the row must not survive a failed move."""
    harness.make_show_in_pool('Test Show', [(1, 1)])

    def boom(*a, **k):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(unbinge.shutil, 'move', boom)

    resp = client.post('/promote', data={'show_name': 'Test Show', 'release_days': '0'})

    assert resp.status_code == 500
    with unbinge.get_db() as conn:
        rows = conn.execute(f"SELECT * FROM {unbinge.TABLE_NAME}").fetchall()
    assert len(rows) == 0, "failed promote must leave no orphaned row"


def test_weeks_parameter_is_clamped_not_fatal(harness, client):
    """R9"""
    assert client.get('/schedule?weeks=abc').status_code == 200
    assert client.get('/schedule?weeks=99999').status_code == 200
    assert client.get('/schedule?weeks=-5').status_code == 200


def test_wal_mode_is_enabled(harness):
    """R6"""
    from contextlib import closing
    with closing(unbinge.get_db()) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == 'wal'


def test_timestamps_are_timezone_aware(harness):
    """R2 - naive timestamps were the whole bug."""
    assert unbinge.now_local().tzinfo is not None


# ---------------------------------------------------------------------------
# C1 - episode-set tracking
# ---------------------------------------------------------------------------

def test_dripped_episodes_are_recorded(harness, drip):
    from contextlib import closing
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    harness.make_episode(show, 'Test Show', 1, 2, subdir='Season 01')
    show_id = harness.add_show('Test Show')

    drip(show_id)

    with closing(unbinge.get_db()) as conn:
        assert unbinge.get_dripped_set(conn, show_id) == {(1, 1)}


def test_dripped_rows_are_cleared_on_delete(harness, client, drip):
    from contextlib import closing
    harness.make_episode(harness.vault / 'Test Show', 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')
    drip(show_id)

    client.post(f'/delete/{show_id}')

    with closing(unbinge.get_db()) as conn:
        assert unbinge.get_dripped_set(conn, show_id) == set(), \
            "a re-promoted show must not inherit a stale dripped set"


def test_specials_never_drip_and_never_block_cooldown(harness, drip):
    """The agreed specials policy: skipped, and invisible to the
    'is the vault empty?' question."""
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 0, 1, subdir='Specials')
    show_id = harness.add_show('Test Show')

    drip(show_id)

    assert harness.episode_tags(harness.plex / 'Test Show') == set()
    assert harness.get_show(show_id)['completed_at'] is not None, \
        "a specials-only vault counts as empty"


def test_specials_survive_graduation(harness, drip):
    """Never dripped, but not stranded either - they rejoin the show."""
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 0, 1, subdir='Specials')
    show_id = harness.add_show('Test Show', current_season=1, current_episode=1,
                               completed_at='2026-09-01T03:00:00')

    drip(show_id)

    assert (0, 1) in harness.episode_tags(harness.pool / 'Test Show')


def test_migration_seeds_from_plex_path(harness):
    """The agreed migration: what's on disk in plex_path is what was dripped."""
    from contextlib import closing
    harness.make_episode(harness.plex / 'Test Show', 'Test Show', 2, 3, subdir='Season 02')
    harness.make_episode(harness.plex / 'Test Show', 'Test Show', 2, 4, subdir='Season 02')
    show_id = harness.add_show('Test Show', current_season=2, current_episode=4)

    with closing(unbinge.get_db()) as conn:
        conn.execute(f"DELETE FROM {unbinge.DRIPPED_TABLE} WHERE show_id = ?", (show_id,))
        conn.execute(f"DELETE FROM {unbinge.SETTINGS_TABLE} WHERE key = 'schema_version'")
        conn.commit()
        unbinge.run_migrations(conn)
        assert unbinge.get_dripped_set(conn, show_id) == {(2, 3), (2, 4)}


def test_preview_and_drip_share_one_implementation(harness, drip):
    """H1 - they used to duplicate the batching logic and could drift."""
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 5, subdir='Season 01')
    show_id = harness.add_show('Test Show', current_season=2, current_episode=9)

    preview = unbinge.preview_show_drip(harness.get_show(show_id))
    assert preview['action'] == 'drip' and 'S01E05' in preview['detail']

    drip(show_id)
    assert harness.episode_tags(harness.plex / 'Test Show') == {(1, 5)}


# ---------------------------------------------------------------------------
# C3 / C4 - file safety
# ---------------------------------------------------------------------------

def test_identical_duplicates_are_still_dropped(harness):
    """The artwork case the old delete branch was written for. A true
    duplicate should still be removed, not parked as a conflict."""
    import shutil as sh
    src = harness.vault / 'src'
    dest = harness.pool / 'dest'
    src.mkdir(parents=True); dest.mkdir(parents=True)
    (src / 'poster.jpg').write_text('same art')
    sh.copy2(src / 'poster.jpg', dest / 'poster.jpg')

    report = unbinge.merge_directory(str(src), str(dest))

    assert report['duplicates'] == 1
    assert report['conflicts'] == []
    assert not (dest / unbinge.CONFLICTS_DIRNAME).exists()


def test_differing_collision_is_parked_not_deleted(harness):
    src = harness.vault / 'src'
    dest = harness.pool / 'dest'
    src.mkdir(parents=True); dest.mkdir(parents=True)
    (src / 'notes.txt').write_text('THE ONLY COPY')
    (dest / 'notes.txt').write_text('completely different content here')

    report = unbinge.merge_directory(str(src), str(dest))

    parked = dest / unbinge.CONFLICTS_DIRNAME / 'notes.txt'
    assert parked.exists() and parked.read_text() == 'THE ONLY COPY'
    assert report['conflicts'] == ['notes.txt']
    assert (dest / 'notes.txt').read_text() == 'completely different content here'


def test_merge_survives_a_locked_file_and_reports_it(harness, monkeypatch):
    """C4 - one unmovable file must not abort the whole merge."""
    import shutil as sh
    src = harness.vault / 'src'
    dest = harness.pool / 'dest'
    src.mkdir(parents=True); dest.mkdir(parents=True)
    (src / 'a.txt').write_text('a')
    (src / 'locked.txt').write_text('locked')
    (src / 'c.txt').write_text('c')

    real_move = sh.move
    def flaky(s, d, *a, **k):
        # Match the FILENAME only, not the full path. pytest names tmp_path
        # after the test function - this test's own directory is literally
        # ".../test_merge_survives_a_locked_f0/..." - so a substring check
        # against the full path matched every file in the fixture, not just
        # the one meant to be locked.
        if os.path.basename(str(s)) == 'locked.txt':
            raise OSError(32, "The process cannot access the file")
        return real_move(s, d, *a, **k)
    monkeypatch.setattr(unbinge.shutil, 'move', flaky)

    report = unbinge.merge_directory(str(src), str(dest))

    assert report['moved'] == 2, "the other two files should still have moved"
    assert len(report['failures']) == 1
    assert (src / 'locked.txt').exists(), "the locked file stays put"
    assert src.exists(), "source dir must survive so nothing is stranded"


def test_partial_batch_failure_leaves_the_episode_pending(harness, drip, monkeypatch):
    """C4 + C1 - an episode whose subtitle failed to move must NOT be recorded
    as dripped, or the next run would skip the leftover file."""
    from contextlib import closing
    import shutil as sh
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, ext='mkv', subdir='Season 01')
    harness.make_episode(show, 'Test Show', 1, 1, ext='srt', subdir='Season 01')
    show_id = harness.add_show('Test Show')

    real_move = sh.move
    def flaky(s, d, *a, **k):
        if str(s).endswith('.srt'):
            raise OSError(32, "The process cannot access the file")
        return real_move(s, d, *a, **k)
    monkeypatch.setattr(unbinge.shutil, 'move', flaky)

    drip(show_id)

    with closing(unbinge.get_db()) as conn:
        assert unbinge.get_dripped_set(conn, show_id) == set(), \
            "a half-moved episode must stay pending"


def test_graduation_aborts_cleanly_when_files_are_locked(harness, drip, monkeypatch):
    import shutil as sh
    harness.make_episode(harness.plex / 'Test Show', 'Test Show', 1, 1, subdir='Season 01')
    (harness.vault / 'Test Show').mkdir(parents=True)
    show_id = harness.add_show('Test Show', current_season=1, current_episode=1,
                               completed_at='2026-09-01T03:00:00')

    def always_locked(*a, **k):
        raise OSError(32, "The process cannot access the file")
    monkeypatch.setattr(unbinge.shutil, 'move', always_locked)

    drip(show_id)

    assert harness.get_show(show_id) is not None, \
        "must not delete the row while files are still stuck"
    assert any(e['action'] == 'failed' for e in harness.history_for('Test Show'))


# ---------------------------------------------------------------------------
# S1 - path traversal (rename vector)
# ---------------------------------------------------------------------------

def test_edit_rejects_a_traversing_show_name(harness, client):
    """save_edit's own vector: delete_show later reconstructs
    os.path.join(POOL_DIR, show_name) straight from stored data, so a rename
    to a traversing name plants the attack for later rather than firing it
    immediately."""
    show_id = harness.add_show('Test Show')

    resp = client.post(f'/edit/{show_id}', data={
        'show_name': '../../escape',
        'vault_path': str(harness.vault / 'Test Show'),
        'plex_path': str(harness.plex / 'Test Show'),
        'release_days': '0',
        'current_season': '1', 'current_episode': '0', 'episodes_per_drop': '1',
    })

    show = harness.get_show(show_id)
    assert show['show_name'] == 'Test Show', "the traversing rename must not be saved"


def test_safe_show_path_rejects_various_traversal_shapes(harness):
    import pytest as pt
    for bad in ('..', '../x', '../../x', 'a/../../b', '/etc/passwd'):
        with pt.raises(ValueError):
            unbinge.safe_show_path(str(harness.pool), bad)


def test_safe_show_path_accepts_ordinary_names(harness):
    # Must not reject legitimate names that merely contain dots or spaces.
    for ok in ('Show Name', 'Mr. Robot', "Marvel's Daredevil (2015)"):
        result = unbinge.safe_show_path(str(harness.pool), ok)
        assert result == os.path.join(str(harness.pool), ok)


# ---------------------------------------------------------------------------
# C7 - multi-episode files and alternative naming
# ---------------------------------------------------------------------------

def test_dash_range_syntax_is_recognised(harness):
    show = harness.vault / 'Test Show'
    show.mkdir(parents=True)
    (show / 'Test Show - S01E01-E02.mkv').write_text('dash range')

    assert harness.episode_tags(show) == {(1, 1), (1, 2)}


def test_triple_episode_file_registers_all_three(harness):
    show = harness.vault / 'Test Show'
    show.mkdir(parents=True)
    (show / 'Test Show - S01E01E02E03.mkv').write_text('triple')

    assert harness.episode_tags(show) == {(1, 1), (1, 2), (1, 3)}


def test_resolution_and_aspect_ratio_are_not_mistaken_for_episodes(harness):
    """The x-format matcher added for '1x02' must not fire on '1280x720',
    '1920x1080', or aspect ratios like '16x9' sitting in the same filename."""
    show = harness.vault / 'Test Show'
    show.mkdir(parents=True)
    (show / 'Test Show S01E01 1920x1080.mkv').write_text('a')
    (show / 'Test Show S01E02 1280x720 16x9.mkv').write_text('b')

    assert harness.episode_tags(show) == {(1, 1), (1, 2)}


def test_combo_file_drips_as_one_physical_move(harness, drip):
    """The behaviour C7 exists for: a double-episode file must move once,
    delivering both episodes together, even when episodes_per_drop=1."""
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 3, subdir='Season 01')
    combo = show / 'Season 01' / 'Test Show - S01E01E02.mkv'
    combo.write_text('two episodes, one file')
    show_id = harness.add_show('Test Show', episodes_per_drop=1)

    drip(show_id)

    assert harness.episode_tags(harness.plex / 'Test Show') == {(1, 1), (1, 2)}
    assert harness.episode_tags(harness.vault / 'Test Show') == {(1, 3)}


def test_sidecar_still_travels_with_a_combo_file(harness, drip):
    """Regression guard: an early version of the C7 fix grouped files by tag
    in a way that left a subtitle behind when its video was a combo file,
    because the subtitle's tag was already 'claimed' by the video."""
    show = harness.vault / 'Test Show'
    show.mkdir(parents=True)
    season = show / 'Season 01'
    season.mkdir()
    (season / 'Test Show - S01E01E02.mkv').write_text('video')
    (season / 'Test Show - S01E01E02.srt').write_text('subs')
    show_id = harness.add_show('Test Show', episodes_per_drop=1)

    drip(show_id)

    moved = harness.files_under(harness.plex / 'Test Show')
    assert any(f.endswith('.mkv') for f in moved)
    assert any(f.endswith('.srt') for f in moved), \
        "the subtitle must not be left behind because its tag was already committed"
