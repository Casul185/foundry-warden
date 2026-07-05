#!/usr/bin/env python3
"""
run_showcase.py — end-to-end Foundry-Warden showcase harness.

Orchestrates a controlled A/B on your OWN machine:
  1. spawns synthetic background load (generate_load.py) — the "noise",
  2. waits while you launch a game so the running Warden daemon detects it,
     engages throttling, and writes a benchmark JSON when you quit,
  3. finds the newest capture and renders the A/B (analyze_capture.py).

Portable (pure stdlib, no Warden import) — it drives the *real* daemon rather
than re-implementing throttling, so the numbers you see are Warden's own.

    # with the Warden daemon already running:
    python run_showcase.py --capture-dir "%LOCALAPPDATA%\\foundry-warden\\captures" --workers 8

If you don't pass --capture-dir, it prints where to point it and exits after the
load phase so you can analyze the capture yourself with analyze_capture.py.
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _newest_capture(capture_dir: str) -> str | None:
    hits = glob.glob(os.path.join(os.path.expandvars(capture_dir), "*benchmark*.json"))
    return max(hits, key=os.path.getmtime) if hits else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Foundry-Warden showcase harness.")
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 1))
    ap.add_argument("--mem-mb", type=int, default=128)
    ap.add_argument("--duration", type=int, default=300, help="load lifetime (s)")
    ap.add_argument("--capture-dir", default="", help="Warden benchmark output dir to watch")
    args = ap.parse_args()

    load = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "generate_load.py"),
         "--workers", str(args.workers), "--mem-mb", str(args.mem_mb),
         "--duration", str(args.duration)],
    )
    print("\n>>> Background load running. Now LAUNCH A GAME (Steam). Warden will")
    print(">>> detect it, throttle the load + other background apps, and write a")
    print(">>> benchmark JSON when you quit the game.\n")

    if not args.capture_dir:
        print("No --capture-dir given: after you quit the game, run\n"
              "   python analyze_capture.py <that-json>\n"
              "Load will stop on its own; Ctrl-C to stop early.")
        try:
            load.wait()
        except KeyboardInterrupt:
            load.terminate()
        return 0

    before = _newest_capture(args.capture_dir)
    print(f"Watching {args.capture_dir} for a new capture (Ctrl-C to stop)…")
    try:
        while True:
            time.sleep(3)
            latest = _newest_capture(args.capture_dir)
            if latest and latest != before:
                print(f"\nNew capture: {latest}\n")
                subprocess.run([sys.executable, os.path.join(HERE, "analyze_capture.py"), latest])
                break
    except KeyboardInterrupt:
        pass
    finally:
        if load.poll() is None:
            load.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
