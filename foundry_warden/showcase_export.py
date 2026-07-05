"""Build a pre-sanitized, copy-pasteable showcase block from a benchmark capture.

ZERO NETWORK. This module reads a local benchmark JSON and emits a summary the
user can *review and then choose to paste* into a GitHub Discussion. Nothing is
sent anywhere — sharing is 100% the user's manual action.

Sanitization is defence-in-depth: we build the block from a known field allowlist
(never echoing the raw capture), then scrub the result for any stray hostname/path/
IP/MAC as a second pass.
"""
from __future__ import annotations

import glob
import json
import os
import re

# Second-pass scrubbers — applied to the finished block, belt-and-suspenders.
# No \b anchors — they break against surrounding markdown (underscore is a word char).
# MAC runs before IPv6 (a MAC is a subset of the IPv6 colon-hex shape).
_SCRUB = [
    (re.compile(r"(?:\d{1,3}\.){3}\d{1,3}"), "[ip]"),
    (re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}"), "[mac]"),
    # IPv6: full (2+ groups) and compressed (::) forms.
    (re.compile(r"(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}", re.I), "[ipv6]"),
    (re.compile(r"(?:[0-9a-fA-F]{1,4}:)+:[0-9a-fA-F:]*", re.I), "[ipv6]"),
    (re.compile(r"::[0-9a-fA-F][0-9a-fA-F:]*", re.I), "[ipv6]"),
    # UNC path \\host\share — leaks the host.
    (re.compile(r"\\\\[\w.$-]+(?:\\[\w.$-]*)*"), "[unc-path]"),
    (re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.I), r"C:\\Users\\[user]"),
    (re.compile(r"/home/[^/\s_]+"), "/home/[user]"),
    (re.compile(r"\w+\.local|\w+\.lan", re.I), "[host]"),
]


def _generalize_machine(cpu_count: int) -> str:
    if cpu_count <= 0:
        return "unknown CPU"
    if cpu_count <= 4:
        cls = "entry (≤4 threads)"
    elif cpu_count <= 8:
        cls = "mainstream (5–8 threads)"
    elif cpu_count <= 16:
        cls = "high (9–16 threads)"
    else:
        cls = "enthusiast (16+ threads)"
    return f"{cpu_count}-thread CPU, {cls}"


def _get(d: dict, *keys, default=0.0):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def build_showcase(bench: dict, redact_game: bool = False) -> str:
    """Return a sanitized markdown block from a benchmark dict (field allowlist only)."""
    procs = bench.get("per_proc", []) or []
    soft = [p for p in procs if p.get("tier") == "soft"]
    hard = [p for p in procs if p.get("tier") == "hard"]
    names = sorted({str(p.get("name", "")) for p in procs if p.get("name")})

    game = "[redacted]" if redact_game else (bench.get("game_name") or bench.get("game") or "a game")
    base = _get(bench, "system_cpu_baseline", "system_cpu_baseline_pct")
    eng = _get(bench, "system_cpu_engaged", "system_cpu_engaged_pct")

    lines = [
        "### Foundry-Warden throttle result",
        "",
        f"- **Machine:** {_generalize_machine(int(bench.get('cpu_count', 0)))} · GPU: _add yours_",
        f"- **Game:** {game}",
        f"- **Processes throttled:** {bench.get('throttled_count', len(procs))} "
        f"({len(soft)} soft / Idle+EcoQoS, {len(hard)} hard / suspended)",
        f"- **CPU freed (attributed to throttling):** {float(_get(bench, 'cpu_freed_pct')):.2f}%",
        f"- **Working set freed:** {float(_get(bench, 'ws_freed_mb')):.1f} MB",
        f"- **System CPU baseline → engaged:** {float(base):.1f}% → {float(eng):.1f}% "
        "_(context — includes the game's own load, not attributed to throttling)_",
    ]
    if names:
        lines.append(f"- **Throttled processes:** {', '.join(names)}")
    for n in bench.get("notes", []) or []:
        lines.append(f"  - _{n}_")

    block = "\n".join(lines)
    for pat, repl in _SCRUB:
        block = pat.sub(repl, block)
    return block


def latest_capture(benchmarks_dir) -> str | None:
    hits = glob.glob(os.path.join(str(benchmarks_dir), "*.json"))
    return max(hits, key=os.path.getmtime) if hits else None


def export_showcase(benchmarks_dir, out_path, redact_game: bool = False):
    """Read the newest capture, build the block, write it, and return (block, path)."""
    cap = latest_capture(benchmarks_dir)
    if not cap:
        return None, None
    with open(cap, encoding="utf-8") as fh:
        bench = json.load(fh)
    block = build_showcase(bench, redact_game=redact_game)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(block + "\n")
    return block, out_path
