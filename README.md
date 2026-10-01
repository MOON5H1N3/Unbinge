# Unbinge

Unbinge turns binge-watching back into appointment TV. Pick a show from your library, choose which days it "airs" and how many episodes drop each time, and Unbinge releases it into a Plex library a few episodes at a time, like a weekly broadcast schedule.

It's a small self-hosted Flask app that runs in Docker alongside the rest of your *arr stack.

## How it works

Unbinge moves show folders between three directories:

| Directory | What lives there |
|---|---|
| **Pool** (`POOL_DIR`) | Your full collection of shows, untouched |
| **Vault** (`VAULT_DIR`) | Episodes of a dripping show that haven't been released yet |
| **Plex path** (`PLEX_BASE_DIR`) | Episodes that have been released, watched by a Plex library |

Each show goes through a simple lifecycle:

1. **Promote** – you pick a show from the pool, set its release day(s) and episodes per drop. Its files move into the vault.
2. **Drip** – on each release day, at the scheduled time, the next batch of episodes moves from the vault into the Plex path and Plex is told to rescan.
3. **Cooldown** – once the vault runs dry, the show sits for one more cycle so you can catch up.
4. **Graduate** – the whole show (including specials, which are never dripped) moves back into the pool and the slot is freed.

If episodes reappear in the vault during cooldown, the show resumes instead of graduating.

## Features

- **Dashboard** of active, cooling-down and paused shows, with pause/resume, drip-now and bulk actions
- **Schedule view** – a forward-looking agenda of upcoming drips, cooldowns and graduations
- **Calendar feed** – subscribe at `/calendar.ics` or download it, so drops show up in your calendar app
- **Show queue** – line up the next show to promote automatically when another finishes, or on a date
- **Per-show controls** – preview the next drop, exclude episodes, manually match oddly named files
- **History** with one-click **undo** of the last drip
- **Dry-run mode** – watch what the scheduler *would* do without moving any files
- **Notifications** via webhook, plus a self-updating **Discord** schedule message
- **TVDB** integration for posters, genres and recommendations from your pool
- **Sonarr** integration to show real upcoming air dates
- **Database backups** – on demand or automatic, with configurable retention
- **Missed-run detection** if the container was down at drip time
- **Optional password login** with CSRF protection
- `/health` endpoint for Docker healthchecks

## Quick start (Docker)

1. Clone the repo:

   ```bash
   git clone https://github.com/MOON5H1N3/Unbinge.git
   cd Unbinge
   ```

2. Create a `.env` file next to `docker-compose.yaml`:

   ```env
   PLEX_URL=http://192.168.1.10:32400
   PLEX_TOKEN=your-plex-token
   PLEX_LIBRARY_ID=5
   ```

3. Edit the `volumes` and `*_DIR` paths in `docker-compose.yaml` to match where your media lives. The `*_DIR` values must be **container-side** paths (inside the mounted volume), not host paths.

4. Start it:

   ```bash
   docker compose up -d --build
   ```

5. Open **http://localhost:5000**.

Point a Plex library at the same folder as `PLEX_BASE_DIR` so released episodes appear there.

## Updating

Once you've cloned the repo as above, pull in new releases with the bundled update script instead of doing it by hand:

```bash
./update.sh
```

It backs up the database, refuses to run if you've got uncommitted local edits, `git pull`s, rebuilds the image, restarts the container, and waits for `/health` to come back before declaring success. Run it from the same directory as `docker-compose.yaml`.

If you'd rather update manually:

```bash
git pull
docker compose build
docker compose up -d
```

## Configuration

All configuration is through environment variables. Integration settings can also be changed later from the **Settings** page.

| Variable | Required | Default | Description |
|---|---|---|---|
| `PLEX_URL` | Yes | – | URL of your Plex server |
| `PLEX_TOKEN` | Yes | – | Plex auth token |
| `PLEX_LIBRARY_ID` | Yes | – | Section ID of the library to rescan after a drip |
| `POOL_DIR` | Yes | `/media/A/TV100` | Full show collection |
| `VAULT_DIR` | Yes | `/media/vault` | Holding area for unreleased episodes |
| `PLEX_BASE_DIR` | Yes | `/media/private_tv` | Folder the Plex library watches |
| `DB_PATH` | No | `/config/drip_schedule.db` | SQLite database location (posters are stored beside it) |
| `TZ` | No | UTC | Timezone the drip schedule runs in, e.g. `Europe/London` |
| `UNBINGE_PASSWORD` | No | – | Set to enable the login page. Leave unset for no auth. (`DRIPARR_PASSWORD` still works as a fallback if you're upgrading from Driparr) |
| `WEBHOOK_URL` | No | – | Webhook for drip / failure / missed-run notifications |
| `DISCORD_SCHEDULE_WEBHOOK_URL` | No | – | Discord webhook for the live schedule message |
| `TVDB_API_KEY` / `TVDB_PIN` | No | – | TVDB credentials for posters and metadata |
| `SONARR_URL` / `SONARR_API_KEY` | No | – | Sonarr connection for real air dates |

The daily drip time defaults to **03:00** and can be changed in Settings.

> **Set `TZ`.** Without it the container runs on UTC, so drips fire an hour off during BST and day boundaries drift twice a year.

## Running tests

The test suite runs inside the container:

```bash
docker compose exec unbinge pytest
```

## Notes

- Unbinge runs as a **single process** on purpose. The scheduler starts with the app, so multiple workers would mean multiple drip jobs racing over the same files.
- The container currently runs as root because the bind-mounted `/config` folder is owned by the host user.
- Keep `.env` out of git and out of the image. Both `.gitignore` and `.dockerignore` already exclude it.

## License

[MIT](LICENSE)
