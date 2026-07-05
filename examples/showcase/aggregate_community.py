#!/usr/bin/env python3
"""
aggregate_community.py — pull community showcase submissions into one table.

Reads PUBLIC GitHub issues labeled `showcase` (the showcase issue template applies
that label) via the REST API — **no auth needed for public data**, pure stdlib.
It parses the `export-showcase` blocks people posted and prints an aggregate table.

    python aggregate_community.py                 # default repo (casul185/foundry-warden)
    python aggregate_community.py owner/repo

Design notes:
* No auth => the unauthenticated GitHub rate limit (60 req/hr) is plenty for this.
* Reads only what users VOLUNTARILY posted publicly; sends nothing.
* Best-effort parser: it extracts the machine line, throttled count, and CPU/WS
  freed from the fenced block; malformed posts are skipped, not crashed on.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request

_API = "https://api.github.com/repos/%s/issues?labels=showcase&state=all&per_page=100"
_UA = {"User-Agent": "foundry-warden-aggregate", "Accept": "application/vnd.github+json"}

_MACHINE = re.compile(r"\*\*Machine:\*\*\s*(.+)")
_THROTTLED = re.compile(r"\*\*Processes throttled:\*\*\s*(\d+)")
_CPU = re.compile(r"CPU freed.*?:\*\*\s*([\d.]+)%")
_WS = re.compile(r"Working set freed.*?:\*\*\s*([\d.]+)\s*MB")


def _fetch(repo: str) -> list[dict]:
    req = urllib.request.Request(_API % repo, headers=_UA)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse(body: str) -> dict | None:
    m = _MACHINE.search(body or "")
    t = _THROTTLED.search(body or "")
    if not (m and t):
        return None
    return {
        "machine": m.group(1).strip()[:48],
        "throttled": int(t.group(1)),
        "cpu_freed": (_CPU.search(body).group(1) if _CPU.search(body) else "?"),
        "ws_freed": (_WS.search(body).group(1) if _WS.search(body) else "?"),
    }


def main() -> int:
    repo = sys.argv[1] if len(sys.argv) > 1 else "casul185/foundry-warden"
    try:
        issues = _fetch(repo)
    except Exception as exc:
        print(f"could not reach GitHub: {exc}", file=sys.stderr)
        return 1
    rows = [r for r in (_parse(i.get("body", "")) for i in issues if "pull_request" not in i) if r]
    if not rows:
        print("No parseable showcase submissions yet.")
        return 0
    print(f"# Community showcase — {len(rows)} submission(s) from {repo}\n")
    print(f"| {'Machine':<48} | Throttled | CPU freed | WS freed |")
    print(f"|{'-'*50}|-----------|-----------|----------|")
    for r in sorted(rows, key=lambda x: -x["throttled"]):
        print(f"| {r['machine']:<48} | {r['throttled']:>9} | {r['cpu_freed']:>8}% | {r['ws_freed']:>6} MB |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
