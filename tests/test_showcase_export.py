"""Regression corpus for the showcase sanitizer — tricky inputs must all be scrubbed.

Run: python tests/test_showcase_export.py   (plain stdlib, no pytest needed)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "foundry_warden"))
import showcase_export as sx  # noqa: E402

# Each entry is planted into a capture's echoed free-text (notes/game) and must NOT survive.
SECRETS = [
    "192.0.2.1",                       # IPv4
    "198.51.100.7",                          # IPv4 private
    "2001:db8::1",                       # IPv6
    "fe80::1ff:fe23:4567:890a",          # IPv6 link-local
    "00:1a:2b:3c:4d:5e",                 # MAC
    r"C:\Users\alice",                    # Windows home
    "/home/alice",                       # unix home
    r"\\fileserver\share",               # UNC path (host!)
    "myhost.lan",                       # .lan host
    "device.local",                    # .local host
]
# Contexts that previously broke the regex (markdown wrappers).
WRAPPERS = ["{}", "_{}_", "**{}**", "`{}`", "text {} text", "({})"]


def _build(secret_in_note: str, secret_in_game: str) -> str:
    bench = {
        "node": "SECRET-NODE", "game_name": secret_in_game, "cpu_count": 8,
        "throttled_count": 3, "cpu_freed_pct": 1.0, "ws_freed_mb": 5.0,
        "system_cpu_baseline": 40.0, "system_cpu_engaged": 50.0,
        "per_proc": [{"name": "brave.exe", "tier": "soft"}],
        "notes": [secret_in_note],
    }
    return sx.build_showcase(bench)


def main() -> int:
    failures = []
    for secret in SECRETS:
        for wrap in WRAPPERS:
            planted = wrap.format(secret)
            out = _build(planted, planted)
            if secret in out:
                failures.append((secret, wrap))
    # node is never echoed at all
    if "SECRET-NODE" in _build("clean note", "clean game"):
        failures.append(("SECRET-NODE (node)", "field-allowlist"))
    if failures:
        print("SANITIZER LEAKS:")
        for s, w in failures:
            print(f"  LEAK: {s!r} in wrapper {w!r}")
        return 1
    print(f"OK — {len(SECRETS)}×{len(WRAPPERS)} secret/wrapper combos all scrubbed; node never echoed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
