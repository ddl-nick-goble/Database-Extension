"""v1 release gate — fails if ANY engine isn't green.

This is the script the bank-rollout checklist points at. Run from the
wizard workspace; it spawns each e2e_<engine>.py + the cross-engine
roundtrip + the tunnel fidelity check, captures their exit codes, and
prints a final pass/fail summary.

Each test still cleans up its own App on success and (by default) leaves
it alive on failure for debugging — DD_E2E_KEEP_ON_FAIL=0 makes the
runner aggressive.

Usage:
  python3 tests/run_all.py
  python3 tests/run_all.py --skip mysql,redis     # subset
  python3 tests/run_all.py --only postgres        # one engine
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).parent

# Order matters: cheapest/most-foundational engine first so a fundamental
# regression fails fast.
PER_ENGINE_TESTS = [
    ("postgres", "e2e_postgres.py"),
    ("mongo",    "e2e_mongo.py"),
    ("mysql",    "e2e_mysql.py"),
    ("redis",    "e2e_redis.py"),
]

CROSS_ENGINE_TESTS = [
    ("snapshot_restore", "test_snapshot_restore.py"),
    ("tunnel_fidelity",  "test_tunnel_fidelity.py"),
]


def run(script: Path, label: str, log_dir: Path) -> tuple[int, float]:
    log_file = log_dir / f"{label}.log"
    t0 = time.monotonic()
    print(f"\n>>> {label}  (log: {log_file})", flush=True)
    with log_file.open("w") as f:
        proc = subprocess.run(
            [sys.executable, str(script)],
            stdout=f, stderr=subprocess.STDOUT,
        )
    dt = time.monotonic() - t0
    status = "PASS" if proc.returncode == 0 else f"FAIL (rc={proc.returncode})"
    print(f"<<< {label}: {status}  ({dt:.1f}s)")
    return proc.returncode, dt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="comma-separated test labels to run")
    parser.add_argument("--skip", help="comma-separated test labels to skip")
    args = parser.parse_args()

    only = set(args.only.split(",")) if args.only else None
    skip = set(args.skip.split(",")) if args.skip else set()

    all_tests = PER_ENGINE_TESTS + CROSS_ENGINE_TESTS
    todo = [
        (label, script) for label, script in all_tests
        if (only is None or label in only) and label not in skip
    ]
    if not todo:
        print("nothing to run")
        return 0

    log_dir = TESTS_DIR / "_runs" / time.strftime("%Y%m%dT%H%M%S")
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"logs → {log_dir}")

    results: list[tuple[str, int, float]] = []
    for label, script_name in todo:
        script = TESTS_DIR / script_name
        if not script.exists():
            print(f"!! skipping {label}: {script} missing")
            results.append((label, 99, 0.0))
            continue
        rc, dt = run(script, label, log_dir)
        results.append((label, rc, dt))

    print("\n" + "=" * 60)
    print("RELEASE GATE SUMMARY")
    print("=" * 60)
    for label, rc, dt in results:
        marker = "✓" if rc == 0 else "✗"
        print(f"  {marker} {label:24s}  rc={rc}  ({dt:.1f}s)")
    failed = [l for l, rc, _ in results if rc != 0]
    if failed:
        print(f"\nFAILED: {failed}")
        return 1
    print("\nALL GREEN ✓  — release gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
