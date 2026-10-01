"""Known bugs, written as executable specifications.

Each test here describes what Driparr *should* do. They are marked
`xfail(strict=True)`, which means:

- **Today**, on unfixed code, they fail as expected and the suite stays green.
- **After you fix the bug**, the test starts passing, and `strict=True` turns
  that unexpected pass into a FAILURE. That's deliberate: it forces you to come
  back here and delete the marker, so a fixed bug can never quietly regress.

So the workflow for C1 is: fix the code, run the suite, watch these go XPASS,
remove the markers, run again, everything green.
"""

import os

import app as driparr


# ---------------------------------------------------------------------------
# C1 - the high-water mark
# ---------------------------------------------------------------------------

def test_episode_added_below_the_watermark_still_drips(harness, drip):
    """The core C1 failure.

    A show has dripped up to S02E04. Season 1 is then added to the vault.
    Those episodes sort *before* the stored position, so the `>` comparison
    excludes them and they can never drip.
    """
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    harness.make_episode(show, 'Test Show', 1, 2, subdir='Season 01')
    show_id = harness.add_show('Test Show', current_season=2, current_episode=4)

    drip(show_id)

    assert harness.episode_tags(harness.plex / 'Test Show'), \
        "S01 episodes should have dripped, not been ignored"


def test_episodes_below_the_watermark_prevent_cooldown(harness, drip):
    """A vault with files in it is not an empty vault, wherever those files
    sit relative to the stored position."""
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show', current_season=2, current_episode=4)

    drip(show_id)

    assert harness.get_show(show_id)['completed_at'] is None, \
        "must not start cooldown while episodes remain in the vault"


def test_below_watermark_episodes_cancel_a_cooldown(harness, drip):
    """The recheck that's meant to catch a false cooldown inherits the bug,
    which is why the White Lotus incident was silent."""
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show', current_season=2, current_episode=4,
                               completed_at='2026-09-01T03:00:00')

    drip(show_id)

    assert harness.get_show(show_id) is not None, \
        "should have resumed, not graduated with files still in the vault"


def test_a_backfilled_mid_season_gap_still_drips(harness, drip):
    """You dripped E1 and E3, then later downloaded the missing E2."""
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 2, subdir='Season 01')
    show_id = harness.add_show('Test Show', current_season=1, current_episode=3)

    drip(show_id)

    assert harness.episode_tags(harness.plex / 'Test Show') == {(1, 2)}


def test_schedule_projection_includes_below_watermark_episodes(harness):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    today = driparr.now_local().weekday()
    harness.add_show('Test Show', release_days=str(today),
                     current_season=2, current_episode=4)

    events = driparr.project_schedule(weeks_ahead=2)

    assert any(e['action'] == 'drip' for e in events), \
        "the schedule page should show these episodes as pending"


# ---------------------------------------------------------------------------
# C3 - silent deletion on collision
# ---------------------------------------------------------------------------

def test_colliding_files_are_not_silently_destroyed(harness):
    """On graduate, plex is merged first and then vault. Any same-named vault
    file currently hits `os.remove(abs_src)` with no log and no comparison."""
    src = harness.vault / 'src'
    dest = harness.pool / 'dest'
    src.mkdir(parents=True)
    dest.mkdir(parents=True)
    (src / 'notes.txt').write_text('THE ONLY COPY OF THIS CONTENT')
    (dest / 'notes.txt').write_text('different content')

    driparr.merge_directory(str(src), str(dest))

    surviving = list(harness.pool.rglob('*notes*'))
    contents = [p.read_text() for p in surviving if p.is_file()]
    assert 'THE ONLY COPY OF THIS CONTENT' in contents, \
        "colliding source file should be preserved, not deleted"


# ---------------------------------------------------------------------------
# C7 - multi-episode files
# ---------------------------------------------------------------------------

def test_double_episode_file_registers_both_episodes(harness):
    show = harness.vault / 'Test Show'
    show.mkdir(parents=True)
    (show / 'Test Show - S01E01E02.mkv').write_text('two episodes in one file')

    tags = harness.episode_tags(show)

    assert tags == {(1, 1), (1, 2)}


def test_alternative_episode_naming_is_recognised(harness):
    show = harness.vault / 'Test Show'
    show.mkdir(parents=True)
    (show / 'Test Show - 1x02.mkv').write_text('x-format')
    (show / 'Test Show - S01.E03.mkv').write_text('dot-format')

    assert harness.episode_tags(show) == {(1, 2), (1, 3)}


# ---------------------------------------------------------------------------
# C8 - specials ordering
# ---------------------------------------------------------------------------

def test_specials_do_not_drip_before_the_premiere(harness, drip):
    """PASSES TODAY - but not for the reason I assumed, and it will not keep
    passing for free.

    A new show starts at current_season=1, current_episode=0. Season 0 sorts
    below that, so the watermark filter excludes specials outright. Correct
    output, wrong mechanism.

    Once C1 replaces the watermark with a set of dripped episodes, (0, 1)
    sorts FIRST and the special becomes the premiere. This test is the guard
    that catches that. Do not delete it when fixing C1 - make it keep passing.
    """
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 0, 1, subdir='Specials')
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')

    drip(show_id)

    assert harness.episode_tags(harness.plex / 'Test Show') == {(1, 1)}, \
        "the first drip should be the premiere, not a special"


# ---------------------------------------------------------------------------
# C9 - drip-now lies about the outcome
# ---------------------------------------------------------------------------

def test_drip_now_reports_failure_when_the_drip_fails(harness, client, monkeypatch):
    harness.make_episode(harness.vault / 'Test Show', 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')

    def boom(*a, **k):
        raise OSError("simulated failure")
    monkeypatch.setattr(driparr, 'process_show_drip', boom)

    resp = client.post(f'/api/drip-now/{show_id}')

    assert resp.status_code >= 400 or resp.get_json()['status'] == 'error'


def test_drip_now_404s_on_an_unknown_show(harness, client):
    assert client.post('/api/drip-now/999999').status_code == 404


def test_drip_now_reports_what_was_actually_dripped(harness, client):
    harness.make_episode(harness.vault / 'Test Show', 'Test Show', 1, 1, subdir='Season 01')
    show_id = harness.add_show('Test Show')

    resp = client.post(f'/api/drip-now/{show_id}')

    assert 'S01E01' in resp.get_json()['message']


# ---------------------------------------------------------------------------
# C10 / C11 - show name integrity
# ---------------------------------------------------------------------------

def test_editing_a_show_cannot_create_a_duplicate_name(harness, client):
    harness.add_show('Show A')
    show_b = harness.add_show('Show B')

    client.post(f'/edit/{show_b}', data={
        'show_name': 'Show A',
        'vault_path': str(harness.vault / 'Show B'),
        'plex_path': str(harness.plex / 'Show B'),
        'release_days': '0',
        'current_season': '1', 'current_episode': '0', 'episodes_per_drop': '1',
    })

    names = [s['show_name'] for s in driparr.load_shows()]
    assert len(names) == len(set(names)), "duplicate show names should be rejected"


# ---------------------------------------------------------------------------
# S1 - path traversal
# ---------------------------------------------------------------------------

def test_inspect_rejects_path_traversal(harness, client):
    """Must target a directory that EXISTS, or the route 404s for the boring
    reason and the test proves nothing. '..' from the pool is the parent
    directory, which is always there.
    """
    resp = client.get('/api/inspect?show=..')
    assert resp.status_code >= 400, \
        "traversal outside the pool should be rejected, not scanned"


def test_promote_rejects_path_traversal(harness, client):
    """Pool and vault sit at different depths in the real layout, so a
    traversing name resolves to two DIFFERENT paths and the 'vault path
    already exists' guard does not fire. The move goes ahead.
    """
    secret = harness.media / 'secret'
    secret.mkdir(parents=True, exist_ok=True)
    (secret / 'file.txt').write_text('should not move')

    resp = client.post('/promote', data={'show_name': '../../secret', 'release_days': '0'})

    assert (secret / 'file.txt').exists(), \
        "must not move directories from outside the pool"
    assert resp.status_code >= 400, "traversal should be rejected outright"
