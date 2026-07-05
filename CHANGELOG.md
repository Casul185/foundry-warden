# Changelog

## v0.1.2 — 2026-07-05

Adoptability: logging + docs.

* Throttle-engage now logs the **process names** at INFO (soft/hard tiers + protected count), not just counts — the line users screenshot and share.
* `run --verbose` (=DEBUG) / `--log-level LEVEL` CLI flag overrides the config log level.
* README: **Viewing logs** (path, rotation, how to read an engage line, counters, verbosity) and **Why it needs admin** (elevation explained).

## v0.1.1 — 2026-07-05

Showcase + reproducibility.

* Real-session throttle showcase in the README (three real games; 47/54/39 background processes throttled), with honest attribution — measured CPU/working-set deltas are near zero for idle background apps and the docs say why (the win is preventive).
* `examples/showcase/`: portable `generate_load.py` (synthetic busy/memory load), `run_showcase.py` (end-to-end A/B harness driving the real daemon), `analyze_capture.py` (renders a benchmark JSON as a plain A/B table), and `sample_capture.json` — a real, sanitized 47-process capture to try immediately.

## v0.1.0 — 2026-07-04

Initial public release.

* Steam game detection (RunningAppID + appmanifest + foreground corroboration with grace fallback and entry latch)
* SOFT throttle tier (Idle priority + EcoQoS) and opt-in HARD tier (NtSuspendProcess), with a code-level critical-process safety floor, config protect-list, and process-tree-aware user allowlist
* Crash recovery: persisted throttle state is undone on next start
* Per-session BASELINE / ENGAGED / RESTORED benchmarking with honest attribution
* Optional outbound-only telemetry (disabled by default)
* `--dry-run` mode: logs intended actions, takes none
