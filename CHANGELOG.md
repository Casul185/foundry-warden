# Changelog

## v0.1.0 — 2026-07-04

Initial public release.

* Steam game detection (RunningAppID + appmanifest + foreground corroboration with grace fallback and entry latch)
* SOFT throttle tier (Idle priority + EcoQoS) and opt-in HARD tier (NtSuspendProcess), with a code-level critical-process safety floor, config protect-list, and process-tree-aware user allowlist
* Crash recovery: persisted throttle state is undone on next start
* Per-session BASELINE / ENGAGED / RESTORED benchmarking with honest attribution
* Optional outbound-only telemetry (disabled by default)
* `--dry-run` mode: logs intended actions, takes none
