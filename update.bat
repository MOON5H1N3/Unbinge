@echo off
setlocal enabledelayedexpansion
REM update.bat - double-click to pull the latest Unbinge code and rebuild.
REM Equivalent of update.sh, for Windows. Place next to docker-compose.yaml
REM (this is already where it lives if you cloned the repo normally).
REM
REM What it does, in order:
REM   1. Refuses to run if there are local changes to tracked files
REM   2. Backs up the database via the running app's own backup endpoint
REM   3. git pull
REM   4. docker compose build
REM   5. docker compose up -d
REM   6. Waits for /health to come back healthy
REM
REM Just double-click this file in Explorer, or run it from a terminal.

cd /d "%~dp0"

echo [update] checking for git...
where git >nul 2>nul
if errorlevel 1 (
    echo [update] ERROR: git is not installed or not on PATH.
    pause
    exit /b 1
)

echo [update] checking for docker...
where docker >nul 2>nul
if errorlevel 1 (
    echo [update] ERROR: docker is not installed or not on PATH.
    pause
    exit /b 1
)

if not exist docker-compose.yaml (
    echo [update] ERROR: docker-compose.yaml not found in %cd% - run this from your Unbinge install folder.
    pause
    exit /b 1
)

if not exist .git (
    echo [update] ERROR: this folder is not a git checkout of the Unbinge repo.
    pause
    exit /b 1
)

echo [update] checking for local changes...
git diff --quiet -- . ":!config"
if errorlevel 1 goto localchanges
git diff --cached --quiet -- . ":!config"
if errorlevel 1 goto localchanges
goto nochanges

:localchanges
echo [update] ERROR: local changes to tracked files detected.
echo [update] Commit, stash, or discard them first ^(git status^), then re-run.
pause
exit /b 1

:nochanges
echo [update] backing up the database before updating...
curl -fsS -X POST http://localhost:5000/api/run-backup-now >nul 2>nul
if errorlevel 1 (
    echo [update] WARNING: could not reach the app to take a pre-update backup ^(is the container running?^).
    set /p REPLY="Continue the update anyway without a fresh backup? [y/N] "
    if /i not "!REPLY!"=="y" (
        echo [update] aborted.
        pause
        exit /b 1
    )
) else (
    echo [update] backup created.
)

echo [update] pulling latest code...
for /f %%i in ('git rev-parse HEAD') do set BEFORE_SHA=%%i
git pull --ff-only
if errorlevel 1 (
    echo [update] ERROR: git pull failed - resolve manually and re-run.
    pause
    exit /b 1
)
for /f %%i in ('git rev-parse HEAD') do set AFTER_SHA=%%i

if "!BEFORE_SHA!"=="!AFTER_SHA!" (
    echo [update] already up to date ^(nothing changed^).
) else (
    echo [update] updated !BEFORE_SHA! -^> !AFTER_SHA!
)

echo [update] rebuilding image...
docker compose build
if errorlevel 1 (
    echo [update] ERROR: docker compose build failed.
    pause
    exit /b 1
)

echo [update] restarting container...
docker compose up -d
if errorlevel 1 (
    echo [update] ERROR: docker compose up failed.
    pause
    exit /b 1
)

echo [update] waiting for the app to come back healthy...
set TRIES=0
:healthloop
curl -fsS http://localhost:5000/health >nul 2>nul
if not errorlevel 1 (
    echo [update] update complete - unbinge is healthy.
    echo [update] hard-refresh your browser ^(Ctrl+Shift+R^) to see any visual changes.
    pause
    exit /b 0
)
set /a TRIES+=1
if !TRIES! geq 30 (
    echo [update] ERROR: container restarted but did not become healthy in time.
    echo [update] check: docker compose logs unbinge
    pause
    exit /b 1
)
timeout /t 2 >nul
goto healthloop
