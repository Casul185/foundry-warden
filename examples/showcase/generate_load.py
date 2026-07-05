#!/usr/bin/env python3
"""
generate_load.py — spawn synthetic background load for a Foundry-Warden showcase.

This stands in for the "background noise" (updaters, sync clients, indexers) that
Warden throttles when a game launches. Run it, then launch a game (or run
run_showcase.py) and watch Warden drop these workers to idle/EcoQoS priority.

Fully portable (pure stdlib, no Warden import) — runs on Windows/Linux/macOS.

    python generate_load.py --workers 8 --mem-mb 128 --duration 120

Each worker burns CPU in a loop and holds a memory buffer, so throttling them
produces a *measurable* CPU/working-set delta (unlike idle background apps,
whose deltas are honestly near zero — see the README).
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import time


def _worker(mem_mb: int, stop_at: float) -> None:
    # Hold a real memory buffer so working-set deltas are visible when throttled.
    buf = bytearray(mem_mb * 1024 * 1024)
    for i in range(0, len(buf), 4096):
        buf[i] = 1  # touch pages so they're resident
    x = 0.0
    while time.time() < stop_at:
        # Busy CPU work — a throttled worker visibly yields cycles.
        for _ in range(50_000):
            x = x * 1.000001 + 1.0
    _ = (buf, x)  # keep references alive


def main() -> int:
    ap = argparse.ArgumentParser(description="Synthetic background load for a Warden showcase.")
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 1),
                    help="number of load processes (default: CPUs-1)")
    ap.add_argument("--mem-mb", type=int, default=128, help="MB held resident per worker")
    ap.add_argument("--duration", type=int, default=120, help="seconds to run")
    args = ap.parse_args()

    stop_at = time.time() + args.duration
    procs = []
    for _ in range(args.workers):
        p = mp.Process(target=_worker, args=(args.mem_mb, stop_at), daemon=True)
        p.start()
        procs.append(p)

    print(f"[generate_load] {args.workers} workers, {args.mem_mb} MB each, "
          f"{args.duration}s. PIDs: {[p.pid for p in procs]}")
    print("[generate_load] launch your game (or run run_showcase.py) now.")
    try:
        for p in procs:
            p.join()
    except KeyboardInterrupt:
        print("\n[generate_load] stopping…")
        for p in procs:
            p.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
