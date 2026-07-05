# Changelog

## v0.1.6 — 2026-07-05

Harden the sanitizer (durable no-leak guarantee).

* `tests/test_showcase_export.py`: a regression corpus (10 secret types × 6 markdown wrappers) — **caught two real leaks** the ad-hoc check missed: **IPv6 addresses** and **UNC paths** (`\host\share`). Both now scrubbed. Wired into CI so the no-leak claim can't regress.
* export-showcase sanitizer: added IPv6 (full + compressed) and UNC-path scrubbing.
* Verified log rotation actually rotates (RotatingFileHandler 2 MB × 5) and the updater degrades cleanly offline (message, not traceback).

## v0.1.5 — 2026-07-05

Community showcase infrastructure (still zero-network).

* `.github/ISSUE_TEMPLATE/showcase-result.yml` + `.github/SHARE_YOUR_RESULTS.md` (pinned-Discussion body) so `export-showcase` output has a home; README Contribute links both.
* `examples/showcase/aggregate_community.py`: pulls **public** showcase submissions (issues labeled `showcase`, no auth, stdlib) into one aggregate table. Reads only what users voluntarily posted; sends nothing.

## v0.1.4 — 2026-07-05

Self-update (stdlib, public GitHub, no auth).

* New `check-update` (read-only) and `update [--yes]` commands: compare the running version to the newest GitHub tag (semver-max across tags) and, only on explicit `--yes`, install in place — backing up first and **never overwriting `config.json` or `logs/`**, with automatic rollback on failure. Pure `urllib`, keeping the zero-dependency property.
* README **Updating** section, honest about the no-code-signing limit.

## v0.1.3 — 2026-07-05

Contribute-your-results (zero network).

* New `export-showcase [--redact-game]` command: reads your latest benchmark and prints a **pre-sanitized**, copy-pasteable summary (throttle counts, generalized machine class, process names, honest notes) with hostnames/usernames/home-paths/IPs/MACs stripped (allowlist build + second-pass scrub). Writes `showcase_export.md`. **Sends nothing** — sharing is 100%% manual and reviewable.
* README **Contribute your results** section stating the no-telemetry design explicitly.

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
