"""Runs every script in tests/checks/ in its own process. Each one builds its
own throwaway database, stubs the scheduler and (where needed) a fake Sonarr,
so none of them touch real data or the network.

    python3 tests/run_checks.py

The pytest suite in tests/ is separate (run it in the container as described
in tests/conftest.py). bench_pages.py is a timing tool, not a check, so it is
skipped here: run it directly to profile page loads.
Exit code is non-zero if any check fails."""
import glob
import os
import subprocess
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
failed = []
files = sorted([f for f in glob.glob(os.path.join(here, 'checks', '*.py')) if not os.path.basename(f).startswith('bench')])
for path in files:
    name = os.path.basename(path)
    started = time.time()
    result = subprocess.run([sys.executable, path], capture_output=True, text=True)
    ok = result.returncode == 0
    print(f"{'ok  ' if ok else 'FAIL'} {name} ({time.time() - started:.1f}s)")
    if not ok:
        failed.append(name)
        print((result.stdout + result.stderr)[-1500:])
print(f"\n{len(files) - len(failed)}/{len(files)} passed")
sys.exit(1 if failed else 0)
