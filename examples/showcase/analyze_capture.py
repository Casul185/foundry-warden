#!/usr/bin/env python3
"""
analyze_capture.py — render a Foundry-Warden benchmark capture as a readable A/B table.

Warden writes a benchmark JSON at the end of a game session (see the daemon's
telemetry/benchmark output). Point this at one to get a plain-English summary.

Fully portable (pure stdlib, no Warden import).

    python analyze_capture.py sample_capture.json

A sanitized real capture (`sample_capture.json`, 47 processes throttled on a real
game session) ships alongside this script so you can try it before your own run.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description="Summarize a Warden benchmark capture.")
    ap.add_argument("capture", help="path to a Warden benchmark JSON")
    args = ap.parse_args()

    try:
        d = json.load(open(args.capture, encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read capture: {exc}", file=sys.stderr)
        return 2

    procs = d.get("per_proc", [])
    tiers = collections.Counter(p.get("tier", "?") for p in procs)

    print(f"Game        : {d.get('game_name') or d.get('game') or '?'}")
    print(f"Node        : {d.get('node', '?')}")
    print("-" * 52)
    print(f"Processes throttled : {d.get('throttled_count', len(procs))}")
    print("  by tier           : " + ", ".join(f"{k}={v}" for k, v in tiers.items()))
    print(f"CPU freed (attributed to throttling) : {d.get('cpu_freed_pct', 0.0):.2f}%")
    print(f"Working set freed                    : {d.get('ws_freed_mb', 0.0):.1f} MB")
    print("-" * 52)
    print("Context (NOT attributed to throttling — includes the game's own load):")
    print("  system CPU baseline -> engaged : "
          f"{d.get('system_cpu_baseline_pct', 0.0):.1f}% -> {d.get('system_cpu_engaged_pct', 0.0):.1f}%")

    top = sorted(procs, key=lambda p: p.get("ws_freed_mb", 0.0), reverse=True)[:10]
    if top:
        print("\nTop throttled processes:")
        for p in top:
            print(f"  {p.get('name', '?'):<24} tier={p.get('tier', '?'):<5} "
                  f"ws_freed={p.get('ws_freed_mb', 0.0):.1f} MB")

    notes = d.get("notes", [])
    if notes:
        print("\nHonest notes from the capture:")
        for n in notes:
            print(f"  - {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
