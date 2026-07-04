"""Configuration: defaults, load/merge/save, and well-known paths.

The daemon is config-driven. Anything an operator might tune lives here with a
safe default. Loading deep-merges the on-disk config over DEFAULTS so a partial
or older config file never crashes the daemon (missing keys fall back to default).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from . import DEFAULT_NODE_NAME

# Project root = parent of this package (wherever the repo is checked out).
# Keeps runtime files (config, logs, state) together and out of the package source.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
LOG_DIR = PROJECT_ROOT / "logs"
BENCHMARKS_DIR = PROJECT_ROOT / "benchmarks"
LOG_FILE = LOG_DIR / "daemon.log"
PID_FILE = LOG_DIR / "daemon.pid"
STOP_FLAG = LOG_DIR / "stop.flag"
# Persisted throttle state -> enables crash recovery (undo on next start).
THROTTLE_STATE_FILE = LOG_DIR / "throttle_state.json"

TASK_NAME = "FoundryWardenDaemon"


DEFAULTS: dict[str, Any] = {
    "node_name": DEFAULT_NODE_NAME,
    "poll_interval_sec": 2.0,
    "heartbeat_interval_sec": 30.0,
    "detection": {
        # Require an actual game window in the foreground (not just Steam open)
        # before declaring game-mode. Robust against library/overlay focus.
        "require_foreground_corroboration": True,
        # Foreground processes that do NOT count as "a game is focused".
        "foreground_ignore": [
            "steam.exe", "steamwebhelper.exe", "explorer.exe",
            "python.exe", "pythonw.exe", "dwm.exe",
            "searchhost.exe", "shellexperiencehost.exe",
            "startmenuexperiencehost.exe", "textinputhost.exe",
            "applicationframehost.exe", "lockapp.exe",
        ],
        # If foreground corroboration fails for this many seconds while
        # RunningAppID is still non-zero, enter game-mode anyway (covers
        # borderless/alt-tabbed sessions). 0 disables the grace fallback.
        "corroboration_grace_sec": 20.0,
    },
    "throttle": {
        # SOFT tier: Idle priority + EcoQoS. Fully reversible. Edit freely.
        "soft_tier": [
            "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
            "discord.exe", "slack.exe", "teams.exe",
            "spotify.exe", "onedrive.exe", "googledrivefs.exe",
            "dropbox.exe", "code.exe", "searchindexer.exe",
        ],
        # HARD tier: full NtSuspendProcess. Disruptive -> EMPTY by default.
        "hard_tier": [],
        "apply_idle_priority": True,
        "apply_ecoqos": True,
    },
    "protect": {
        # Never throttle these, regardless of tier config. The critical-system
        # subset here is also enforced as an unconditional safety floor in the
        # throttle engine, so a bad config can never suspend the OS.
        "names": [
            "steam.exe", "steamwebhelper.exe", "steamservice.exe",
            "sunshine.exe",  # game-streaming host, if present
            "dwm.exe", "csrss.exe", "winlogon.exe", "explorer.exe",
            "wininit.exe", "services.exe", "lsass.exe", "smss.exe",
            "system", "system idle process", "registry", "memory compression",
        ],
        "protect_game_tree": True,   # protect the game process + all descendants
        "protect_self": True,        # protect the daemon's own process
    },
    # ------------------------------------------------------------------
    # TELEMETRY -- optional outbound-only reporting. DISABLED by default.
    # When enabled, the daemon POSTs JSON heartbeats and game enter/exit
    # state changes to `endpoint`; the payload's "event" field distinguishes
    # them. `token`, if set, is sent as an "Authorization: Bearer <token>"
    # header. The daemon never depends on the endpoint: delivery failures are
    # logged and the daemon carries on.
    # ------------------------------------------------------------------
    "telemetry": {
        "enabled": False,
        "endpoint": "",
        "token": "",
        "timeout_sec": 3.0,
    },
    "benchmark": {
        "enabled": True,
        # CPU% needs two snapshots over a delta; the scheduler tick is ~15.6ms,
        # so windows shorter than ~2s are noisy. These are a balance between
        # measurement quality and not delaying throttle/stealing gameplay CPU.
        "sample_interval_sec": 1.0,
        "baseline_window_sec": 3.0,    # captured BEFORE throttle engages
        "engaged_settle_sec": 4.0,     # let throttling take effect first
        "engaged_window_sec": 4.0,     # then measure while gaming
        "restored_settle_sec": 2.0,    # let things normalise after game exit
        "restored_window_sec": 3.0,
        "top_n": 8,                    # how many top processes to record per phase
    },
    # ------------------------------------------------------------------
    # USER ALLOWLIST -- apps the operator uses WHILE gaming that must never be
    # throttled. This is SEPARATE from "protect" (the system protect-list) and
    # from CRITICAL_SAFETY_FLOOR (the unconditional OS floor): those keep the
    # machine safe; this keeps the operator's own running apps responsive.
    # Matched by process name (case-insensitive) AND by process tree, so an
    # app's helper/child processes are covered too. EDIT THIS FREELY -- adding a
    # name here needs no code change.
    # ------------------------------------------------------------------
    "user_allowlist": [
        # Voice chat must never be suspended mid-game. Electron helper and
        # voice processes share the same exe name, so the name covers them.
        "discord.exe", "discordptb.exe", "discordcanary.exe",
        # GPU vendor software (overlay, metrics, driver services) -- throttling
        # these can affect the game itself. These are the AMD Adrenalin process
        # names; they are harmless to leave in place on non-AMD machines.
        "radeonsoftware.exe", "amdrsserv.exe", "amdrssrcext.exe", "rsservcmd.exe",
        "amdow.exe", "amdocapp.exe", "presentmon-x64.exe", "cncmd.exe",
        "amdfendrsr.exe", "atieclxx.exe", "atiesrxx.exe",
        # NOTE: Steam + the whole Steam tree (overlay/helpers) and the running
        # game are already shielded via config["protect"] + the game process
        # tree; they do not need to be repeated here.
    ],
}

# Critical processes that must NEVER be throttled, even if a config typo lists
# them in a tier and omits them from protect. Defence in depth.
CRITICAL_SAFETY_FLOOR = frozenset({
    "system", "system idle process", "registry", "memory compression",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "dwm.exe",
})


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load config from disk merged over DEFAULTS. Never raises on a bad file."""
    path = path or CONFIG_PATH
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                return _deep_merge(DEFAULTS, user)
        except Exception:
            # Corrupt config: fall back to defaults rather than refusing to run.
            pass
    return copy.deepcopy(DEFAULTS)


def save_config(cfg: dict[str, Any], path: Path | None = None) -> None:
    path = path or CONFIG_PATH
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def write_default_config(path: Path | None = None, force: bool = False) -> Path:
    """Write DEFAULTS to disk if absent (or force). Returns the path written."""
    path = path or CONFIG_PATH
    if force or not path.exists():
        save_config(DEFAULTS, path)
    return path


def ensure_dirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    BENCHMARKS_DIR.mkdir(parents=True, exist_ok=True)
