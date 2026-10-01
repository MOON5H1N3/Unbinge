"""Shared fixtures for the Driparr test suite.

Everything here exists to guarantee one thing: **no test ever touches your real
media or your real database.** Each test gets a throwaway pool, vault, Plex
directory and SQLite file under pytest's `tmp_path`, and the module globals are
monkeypatched to point at them.

Run inside the container:

    docker compose exec unbinge python -m pytest tests/ -v
"""

import os
import sys
from contextlib import closing

# Must be set before `app` is imported. Importing the module executes its
# startup block, which would otherwise start a scheduler, reconcile real paths
# and post to the real Discord webhook.
os.environ['DRIPARR_TESTING'] = '1'
os.environ.setdefault('TZ', 'Europe/London')

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as driparr  # noqa: E402


class Harness:
    """Convenience wrapper over one isolated Driparr environment."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        # Mirrors the real docker-compose layout, including the differing
        # depths. The first version used three flat siblings, which made
        # '../x' resolve to the SAME path for both pool and vault - so the
        # "vault path already exists" check fired and the path-traversal
        # tests passed for a reason that doesn't exist in production.
        #   POOL_DIR       = /media/A/TV100                       (depth 3)
        #   VAULT_DIR      = /media/A/driparr/media/vault         (depth 5)
        #   PLEX_BASE_DIR  = /media/A/driparr/media/Plex Path     (depth 5)
        self.media = tmp_path / 'media'
        self.pool = self.media / 'A' / 'TV100'
        self.vault = self.media / 'A' / 'driparr' / 'media' / 'vault'
        self.plex = self.media / 'A' / 'driparr' / 'media' / 'Plex Path'
        for d in (self.pool, self.vault, self.plex):
            d.mkdir(parents=True, exist_ok=True)

    # -- building fake media ------------------------------------------------

    def make_episode(self, base_dir, name, season, episode, ext='mkv',
                     subdir=None, content=None):
        """Creates one fake episode file with a standard SxxExx tag."""
        target = base_dir if subdir is None else base_dir / subdir
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{name} - S{season:02d}E{episode:02d}.{ext}"
        path.write_text(content if content is not None else f"{name} S{season}E{episode}")
        return path

    def make_show_in_pool(self, name, episodes, subdir='Season 01'):
        """Creates a pool folder with the given [(season, episode), ...]."""
        show_dir = self.pool / name
        show_dir.mkdir(parents=True, exist_ok=True)
        for season, episode in episodes:
            folder = f"Season {season:02d}" if subdir else None
            self.make_episode(show_dir, name, season, episode, subdir=folder)
        return show_dir

    # -- database -----------------------------------------------------------

    def add_show(self, name, release_days='0', episodes_per_drop=1,
                 current_season=1, current_episode=0, paused=0,
                 completed_at=None, last_run_date=None):
        """Inserts a show row pointing at this harness's vault/plex dirs."""
        with closing(driparr.get_db()) as conn:
            cur = conn.execute(
                f"""INSERT INTO {driparr.TABLE_NAME}
                    (show_name, vault_path, plex_path, release_day, release_days,
                     current_season, current_episode, episodes_per_drop, paused,
                     completed_at, last_run_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, str(self.vault / name), str(self.plex / name),
                 int(release_days.split(',')[0]), release_days,
                 current_season, current_episode, episodes_per_drop, paused,
                 completed_at, last_run_date)
            )
            conn.commit()
            return cur.lastrowid

    def get_show(self, show_id):
        with closing(driparr.get_db()) as conn:
            row = conn.execute(
                f"SELECT * FROM {driparr.TABLE_NAME} WHERE id = ?", (show_id,)
            ).fetchone()
        return dict(row) if row else None

    def history_actions(self):
        events, _total = driparr.load_history(limit=500)
        return [e['action'] for e in events]

    def history_for(self, show_name):
        events, _total = driparr.load_history(limit=500)
        return [e for e in events if e['show_name'] == show_name]

    # -- assertions ---------------------------------------------------------

    def files_under(self, base_dir):
        """Relative paths of every file under base_dir, sorted."""
        out = []
        for dirpath, _dirs, filenames in os.walk(base_dir):
            for f in filenames:
                out.append(os.path.relpath(os.path.join(dirpath, f), base_dir))
        return sorted(out)

    def episode_tags(self, base_dir):
        """The set of (season, episode) tags present under base_dir."""
        return {(e['season'], e['episode']) for e in driparr.scan_episode_files(str(base_dir))}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """An isolated Driparr environment with a fresh database.

    All outbound side effects (Plex refresh, webhooks, Discord sync) are
    stubbed out, so the suite is safe to run against a live container.
    """
    h = Harness(tmp_path)

    monkeypatch.setattr(driparr, 'POOL_DIR', str(h.pool))
    monkeypatch.setattr(driparr, 'VAULT_DIR', str(h.vault))
    monkeypatch.setattr(driparr, 'PLEX_BASE_DIR', str(h.plex))
    monkeypatch.setattr(driparr, 'DB_FILE', str(tmp_path / 'config' / 'test.db'))
    monkeypatch.setattr(driparr, 'POSTERS_DIR', str(tmp_path / 'config' / 'posters'))
    monkeypatch.setattr(driparr, 'BACKUPS_DIR', str(tmp_path / 'config' / 'backups'))
    monkeypatch.setattr(driparr, 'AUTH_PASSWORD', '')

    # No network, no Plex, no Discord.
    monkeypatch.setattr(driparr, 'trigger_plex_refresh', lambda *a, **k: None)
    monkeypatch.setattr(driparr, 'send_notification', lambda *a, **k: None)
    monkeypatch.setattr(driparr, 'sync_discord_schedule_message',
                        lambda *a, **k: {'status': 'skipped', 'detail': 'test'})
    monkeypatch.setattr(driparr, 'sync_discord_schedule_message_async', lambda *a, **k: None)

    driparr.init_db()
    return h


@pytest.fixture
def client(harness, monkeypatch):
    """Flask test client bound to the isolated environment."""
    driparr.app.config['TESTING'] = True
    with driparr.app.test_client() as c:
        yield c


@pytest.fixture
def drip(harness):
    """Runs one show's drip step and returns the result string."""
    def _run(show_id):
        with closing(driparr.get_db()) as conn:
            row = conn.execute(
                f"SELECT * FROM {driparr.TABLE_NAME} WHERE id = ?", (show_id,)
            ).fetchone()
            result = driparr.process_show_drip(conn, dict(row))
            conn.commit()
        return result
    return _run
