# Foundry-Warden

A Windows game-mode daemon in pure-stdlib Python. It watches Steam, and the moment a game launches it throttles your background apps — browser, chat, sync clients — then restores every one of them, exactly as they were, when you quit. It also benchmarks itself each session and tells you honestly whether it helped.

Zero pip dependencies. One config file. ~2,900 lines of `ctypes` + `winreg` + standard library.

```
> python run_warden.py run
2026-06-27T00:20:11 INFO    warden: GAME DETECTED app_id=1388770 game=Cruelty Squad :: RunningAppID=1388770 + foreground 'crueltysquad.exe'
2026-06-27T00:20:14 INFO    warden: throttle engaged: 3 soft, 0 hard
...
2026-06-27T00:24:30 INFO    warden: HEADLINE: Game-mode freed ~4.7% CPU and ~0.0 MB working set from 3 throttled processes (attributed to throttling).
```

(Real session output. Note the honest 0.0 MB — soft-throttled processes keep their working set, and the report says so instead of inventing a win.)

<!-- SCREENSHOT PLACEHOLDER
     Capture: a terminal running `python run_warden.py run`, showing the
     GAME DETECTED line, the "throttle engaged" line, and the end-of-session
     benchmark table (the per-process CPU before->after rows are the money shot).
     A plain dark-theme terminal at ~100 columns reads best. -->

## Why this exists

Tools like Process Lasso can set process priorities by rule, but they cannot *see Steam game state* — they don't know a game just launched, which process **is** the game (as opposed to the launcher, the overlay, or `steamwebhelper.exe`), or when the session ended and everything should be put back. Windows' own Game Mode is opaque and does nothing about *your* background apps.

Foundry-Warden closes that gap with three ideas:

1. **Steam is the source of truth.** `HKCU\Software\Valve\Steam\RunningAppID` flips to the app id the instant Steam launches anything, and `appmanifest_<appid>.acf` gives the real human game name. No process-name guessing lists to maintain.
2. **Throttling must be reversible and safe by construction** — every mutation is recorded and persisted before it matters, and a multi-layer protect set makes it impossible to suspend the OS out from under yourself.
3. **Claims must be measured.** Every session produces a baseline/engaged/restored benchmark, and only the per-throttled-process deltas are credited to the daemon — never system-wide numbers the game itself moved.

## Showcase — real sessions

Three real game sessions on a 4-core/8-thread machine (32 GB RAM, GTX-1650-class GPU), captured by the daemon's own baseline/engaged/restored benchmark:

| Game | Background processes throttled | Tier |
|---|---|---|
| Call of Duty: Modern Warfare II | 47 | soft (Idle + EcoQoS) |
| Cyberpunk 2077 | 54 | soft |
| Cruelty Squad | 39 | soft |

The instant each game launched, Warden dropped dozens of background processes — Windows Search (`searchapp.exe`), a sync client (`syncthing.exe`), a swarm of `runtimebroker.exe` and `taskhostw.exe` — to idle/EcoQoS priority, then restored every one, exactly as it was, on quit.

**Read this honestly:** in these captures the *measured* CPU-freed and working-set-freed are near zero, because those apps were idle at capture time — an idle process yields little when throttled. The value there is preventive: a throttled updater or indexer simply **cannot** wake up and spike mid-match. To see the mechanism's ceiling as a large, measurable delta, `examples/showcase/` throttles a *busy* synthetic load instead:

```
python examples/showcase/analyze_capture.py examples/showcase/sample_capture.json
```

That ships a real (sanitized) 47-process capture, plus `generate_load.py` + `run_showcase.py` to reproduce a full before/after A/B on your own hardware. See [`examples/showcase/`](examples/showcase/).

## ⚠️ Warning: process suspension is sharp

The **HARD tier suspends processes outright** (`NtSuspendProcess`). A suspended process is frozen mid-instruction:

* **Audio/voice apps** stop responding — a suspended Discord drops you from the call.
* **Anything with unsaved work** (editors, IDEs) cannot autosave while frozen.
* **Anti-cheat and DRM services** may treat their suspended helper processes as tampering. Do not put anti-cheat components in any tier.
* Apps holding locks/IPC can stall *other* programs waiting on them.

For these reasons the hard tier ships **empty**, and the SOFT tier (Idle priority + EcoQoS — fully reversible, no freezing) is the default mechanism. If you do use the hard tier, the safety design below is what stands between you and a bad evening — but the list you put in `hard_tier` is your responsibility.

### The safety floor

Three separate layers are unioned before anything is touched, by name *and* by pid:

1. `CRITICAL_SAFETY_FLOOR` — a **frozen, code-level set** of OS-critical processes (`csrss.exe`, `winlogon.exe`, `lsass.exe`, `dwm.exe`, ...). Enforced unconditionally; a bad config cannot override it.
2. `protect` (config) — Steam and its whole process tree, the running game and all its descendants, the daemon itself, shell/system processes.
3. `user_allowlist` (config) — *your* apps that must stay responsive while gaming (voice chat, GPU vendor software, music). Matched by name **and** by process tree, so differently-named helper/child processes are covered.

On top of that, all applied throttling is persisted to `logs/throttle_state.json` **before** the daemon continues, so if the daemon crashes or is killed, the next start finds the state file and undoes everything (`recover_from_disk`). Clean shutdown paths (Ctrl-C, `stop`, stop-flag file, fatal error) all restore via the same code.

## Architecture

```
run_warden.py  ->  __main__.py (CLI)  ->  daemon.py (poll loop)
                                            |-- detection.py   RunningAppID + appmanifest + foreground corroboration
                                            |-- throttle.py    engage/restore, protect sets, crash recovery, --dry-run
                                            |-- benchmark.py   baseline/engaged/restored phases
                                            |     '-- metrics.py   GetSystemTimes/GetProcessTimes sampling
                                            |-- telemetry.py   optional outbound POSTs (disabled by default)
                                            '-- winapi.py      ALL ctypes lives here
```

**Detection** (`detection.py`). Each poll reads `RunningAppID`. Non-zero alone isn't enough (it's set for launchers/overlays too), so entry into game-mode also requires *foreground corroboration*: a real, non-ignored window has focus. A timed grace fallback (default 20 s) covers borderless/alt-tabbed launches. Once entered, game-mode **latches** — alt-tabbing must not flap the throttle — and only releases when `RunningAppID` returns to 0. The game process is identified structurally: it's the descendant of `steam.exe` that isn't one of Steam's own helpers, with the manifest name (`appmanifest_<appid>.acf`, plus `libraryfolders.vdf` for multi-drive libraries) as the authoritative title.

**Throttling** (`throttle.py`, `winapi.py`). SOFT = `SetPriorityClass(IDLE_PRIORITY_CLASS)` + EcoQoS (`SetProcessInformation` / `ProcessPowerThrottling`), the same mechanism Windows 11 uses for background efficiency — the scheduler deprioritises and the CPU runs those processes at efficient clocks. HARD = `NtSuspendProcess`. Every touched process is recorded with what was done (original priority class, EcoQoS applied, suspended) so restore is exact, not "set everything to Normal".

**Benchmarking** (`benchmark.py`, `metrics.py`, `benchmark_report.py`). Three phases per session: **baseline** (before throttling), **engaged** (during play), **restored** (after exit). CPU% is derived from `GetSystemTimes`/`GetProcessTimes` deltas on a single machine-wide scale, honestly documented down to the ~15.6 ms scheduler-tick quantisation. The headline number sums *only* per-throttled-process deltas; system-wide movement (which includes the game's own load) is reported as context and never attributed. Full JSON records land in `benchmarks/`.

**Lifecycle** (`service.py`). Not a Windows service on purpose: a session-0 service can't read the user's `RunningAppID`, see the foreground window, or set EcoQoS on interactive processes. Instead, a logon-triggered Scheduled Task (`/RL HIGHEST`) runs the daemon inside the user's session. `install`/`start`/`stop`/`status`/`run` subcommands manage it; `stop` restores everything before exiting.

**Telemetry** (`telemetry.py`) — optional, **disabled by default**. If you run a dashboard, the daemon can POST JSON heartbeats and game enter/exit state changes (the exit event carries the benchmark summary) to one endpoint you configure, with an optional bearer token. Strictly outbound, best-effort: an unreachable endpoint is logged (payload included, marked `TELEMETRY-PAYLOAD`) and never crashes or blocks the daemon.

## Quick start

```
git clone https://github.com/casul185/foundry-warden.git && cd foundry-warden
copy config.example.json config.json     # edit to taste
python run_warden.py run --dry-run       # watch what it WOULD do (no changes)
python run_warden.py run                 # foreground, Ctrl-C to stop
python run_warden.py install             # logon Scheduled Task (run as admin)
python run_warden.py start               # start detached now
```

`--dry-run` walks the full detection + protect-set + tier decision path and logs every action it would take, taking none.

## Configuration

`config.json` next to `run_warden.py`; anything omitted falls back to a safe default. Key reference:

| Key | Default | Meaning |
| --- | --- | --- |
| `node_name` | `"gaming-pc"` | Label used in logs/telemetry payloads |
| `poll_interval_sec` | `2.0` | Detection poll cadence |
| `heartbeat_interval_sec` | `30.0` | Telemetry heartbeat cadence |
| `detection.require_foreground_corroboration` | `true` | Require a focused game window to *enter* game-mode |
| `detection.foreground_ignore` | shell/Steam procs | Foreground names that don't count as "a game is focused" |
| `detection.corroboration_grace_sec` | `20.0` | Enter anyway after this long with RunningAppID set (0 = off) |
| `throttle.soft_tier` | browsers, chat, sync | Names to Idle-priority + EcoQoS |
| `throttle.hard_tier` | `[]` (**empty**) | Names to fully suspend — read the warning above |
| `throttle.apply_idle_priority` / `apply_ecoqos` | `true` | Toggle each soft mechanism |
| `protect.names` | Steam, OS, shell | Never touched, on top of the code-level safety floor |
| `protect.protect_game_tree` / `protect_self` | `true` | Shield the game's process tree / the daemon |
| `user_allowlist` | Discord, AMD software | *Your* while-gaming apps; matched by name + process tree |
| `telemetry.enabled` | `false` | Master switch for outbound reporting |
| `telemetry.endpoint` / `token` | `""` | POST target; optional `Authorization: Bearer` token |
| `benchmark.enabled` | `true` | Per-session baseline/engaged/restored measurement |
| `benchmark.*_window_sec` / `*_settle_sec` | 2–4 s | Sampling windows (shorter = noisier; see `metrics.py`) |

## Viewing logs

The daemon writes to **`logs/daemon.log`** next to `run_warden.py` (created automatically, rotated at 2 MB × 5 files, so it can't grow unbounded). `python run_warden.py status` prints the tail.

A throttle-engage looks like this (INFO — this is the line worth screenshotting):

```
2026-07-05T01:00:14 INFO    warden: GAME DETECTED app_id=1938090 game=Call of Duty
2026-07-05T01:00:14 INFO    warden: throttle engaged: 47 soft, 0 hard
2026-07-05T01:00:14 INFO    warden:   soft (Idle+EcoQoS): runtimebroker.exe, searchapp.exe, syncthing.exe, taskhostw.exe, ...
2026-07-05T01:24:30 INFO    warden: throttle restored: 47 processes back to normal
```

* **`throttle engaged: N soft, M hard`** — N processes dropped to Idle priority + EcoQoS (reversible), M fully suspended (only if you opted into the hard tier).
* **`soft (…)` / `hard (…)`** — the actual process names touched, so you can see exactly what happened.
* **`protected (untouched): K`** — how many candidates the safety layers spared.
* **`throttle restored`** — every process put back, on quit.

Raise detail with **`python run_warden.py run --verbose`** (= DEBUG; shows per-process decisions, protect-set expansion, and why anything was skipped) or `--log-level DEBUG|INFO|WARNING`. The persistent default is the `log_level` field in `config.json`.

## Why it needs admin (elevation)

Changing **another** process's scheduling priority or EcoQoS, and suspending/resuming processes, requires a token privilege Windows only grants an elevated process — especially for processes owned by other users or running at higher integrity. The `install` command creates the logon Scheduled Task with `/RL HIGHEST` so the daemon starts elevated at login without a UAC prompt each time. Running un-elevated still works for same-integrity processes but will silently skip the ones it can't touch (you'll see them logged as errors at DEBUG).

## Limitations (honest)

* **Steam only.** Detection is built on Steam's registry state; Epic/GOG/Xbox launches are invisible to it.
* **Windows only.** All mutation goes through Win32/NT APIs; there is no Linux/macOS mode.
* CPU numbers are quantised by the ~15.6 ms scheduler tick; short windows are noisy by nature, and the benchmark report says so in its own notes.
* SOFT-throttled and suspended processes don't necessarily release memory — a small `ws_freed` is expected, not a failure.
* Elevated processes can only be throttled when the daemon itself runs elevated (the Scheduled Task uses `/RL HIGHEST` for this).
* On machines with mostly-idle background apps, the honest benchmark will tell you the daemon barely mattered. That is the point of measuring.

## Requirements

* Windows 10/11, Steam.
* Python ≥ 3.7 (syntactic minimum, verified with `vermin`; developed and run on 3.11+). No packages — stdlib only.

## Contribute your results

The `export-showcase` command **sends nothing** — and Foundry-Warden has no telemetry running by default (the only outbound feature is opt-in, disabled unless you configure it yourself). If you want to help build a public picture of what it does across different machines, sharing is **100% your manual choice**:

```
python run_warden.py export-showcase
```

That reads your latest session and prints a **pre-sanitized** block (throttle counts, generalized machine class, process names, honest notes) — with hostnames, usernames, home paths, IPs, and MACs stripped. It shows you **exactly** what would be shared and writes it to `showcase_export.md`. Nothing leaves your machine. Review it, and *if you choose*, paste it into the pinned [**Share your throttle results** Discussion](https://github.com/casul185/foundry-warden/discussions) (or open a [showcase issue](https://github.com/casul185/foundry-warden/issues/new?template=showcase-result.yml)). Add `--redact-game` to hide the title.

That the program sends nothing, and sharing is entirely opt-in and reviewable, is by design.

## Updating

Pure stdlib, checks public GitHub — no auth, no dependencies:

```
python run_warden.py check-update      # read-only: is there a newer version?
python run_warden.py update            # shows current -> latest, changes nothing
python run_warden.py update --yes      # installs it: backs up first, PRESERVES your config.json
```

It never auto-updates silently — `check-update` is read-only and `update` only installs with `--yes`. The current install is backed up to `../foundry-warden.backup-<version>` and restored automatically if anything fails; your `config.json` and `logs/` are never touched. **Honest limit:** updates come from public GitHub over TLS with **no code signing yet** — you are trusting github.com + the repo owner; review the release diff if that matters to you.

## Support

If this project is useful to you, you can support it at [ko-fi.com/casul185](https://ko-fi.com/casul185).

## License

MIT — see [LICENSE](LICENSE).
