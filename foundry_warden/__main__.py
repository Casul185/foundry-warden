"""Command-line interface and process control for the Foundry-Warden daemon.

Subcommands:
  install / uninstall   create or remove the logon Scheduled Task
  start / stop / restart control the running daemon (detached, in user session)
  status                show daemon + task + throttle state and recent log
  run [--dry-run] [--verbose|--log-level LEVEL]
                        run in the foreground with console logging (Ctrl-C stops);
                        --dry-run logs intended throttle actions, takes none;
                        --verbose (=DEBUG) or --log-level DEBUG|INFO|WARNING raises detail
  _run                  run detached/background (used by the task and `start`)
  once                  one detection poll, printed as JSON (diagnostic)
  payload               print a sample telemetry payload (the receiving-side contract)
  version               print version
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import time

from . import __version__
from . import service
from .config import (
    load_config, write_default_config, ensure_dirs,
    PID_FILE, STOP_FLAG, THROTTLE_STATE_FILE, LOG_FILE, CONFIG_PATH,
)
from . import winapi


# ---------------------------------------------------------------------------
# process-control helpers
# ---------------------------------------------------------------------------
def _running_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    return pid if winapi.process_alive(pid) else None


def _launch_detached() -> int | None:
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    flags = 0x00000008 | 0x00000200 | 0x08000000
    subprocess.Popen(
        [service.pythonw_exe(), service.runner_script(), "_run"],
        creationflags=flags, cwd=str(service.PROJECT_ROOT), close_fds=True,
    )
    for _ in range(40):  # up to ~10s for the pid file to appear
        time.sleep(0.25)
        pid = _running_pid()
        if pid:
            return pid
    return None


def _tail(path, n: int = 12) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_install(_args) -> int:
    ensure_dirs()
    cfg_path = write_default_config()
    ok, out = service.install(highest=True)
    print(f"config: {cfg_path}")
    print(f"task install: {'OK' if ok else 'FAILED'}")
    if out:
        print(out)
    if ok:
        print("\nThe daemon will start at next logon. Start it now with:\n"
              "  python run_warden.py start")
    return 0 if ok else 1


def cmd_uninstall(_args) -> int:
    # stop first so we don't orphan a running daemon
    cmd_stop(_args)
    ok, out = service.uninstall()
    print(f"task uninstall: {'OK' if ok else 'FAILED'}")
    if out:
        print(out)
    return 0 if ok else 1


def cmd_start(_args) -> int:
    pid = _running_pid()
    if pid:
        print(f"already running (pid={pid})")
        return 0
    try:
        STOP_FLAG.unlink()
    except FileNotFoundError:
        pass
    pid = _launch_detached()
    if pid:
        print(f"started (pid={pid})")
        return 0
    print("FAILED to start (no pid file appeared) — check logs/daemon.log")
    return 1


def cmd_stop(_args) -> int:
    pid = _running_pid()
    ensure_dirs()
    STOP_FLAG.write_text("stop", encoding="utf-8")
    if not pid:
        print("not running (stop flag cleared)")
        try:
            STOP_FLAG.unlink()
        except FileNotFoundError:
            pass
        return 0
    for _ in range(60):  # up to ~15s for graceful stop + restore
        time.sleep(0.25)
        if not winapi.process_alive(pid):
            print(f"stopped (pid={pid})")
            return 0
    print(f"WARNING: pid {pid} still alive after 15s; not force-killing "
          f"(would skip restore). Check logs/daemon.log")
    return 1


def cmd_restart(_args) -> int:
    cmd_stop(_args)
    return cmd_start(_args)


def cmd_status(_args) -> int:
    pid = _running_pid()
    engaged = THROTTLE_STATE_FILE.exists()
    print(f"daemon:    {'RUNNING pid=' + str(pid) if pid else 'stopped'}")
    print(f"game-mode: {'GAMING (throttle engaged)' if engaged else 'idle'}")
    print(f"task:      {'installed' if service.is_installed() else 'not installed'}")
    print(f"config:    {CONFIG_PATH} ({'present' if CONFIG_PATH.exists() else 'defaults'})")
    print(f"log:       {LOG_FILE}")
    tail = _tail(LOG_FILE, 12)
    if tail:
        print("--- recent log ---")
        for ln in tail:
            print(ln)
    return 0


def _parse_log_level(args) -> str | None:
    """--verbose => DEBUG; --log-level LEVEL or --log-level=LEVEL => LEVEL."""
    args = args or []
    if "--verbose" in args or "-v" in args:
        return "DEBUG"
    for i, a in enumerate(args):
        if a == "--log-level" and i + 1 < len(args):
            return args[i + 1].upper()
        if a.startswith("--log-level="):
            return a.split("=", 1)[1].upper()
    return None


def cmd_run(_args) -> int:
    dry_run = "--dry-run" in (_args or [])
    log_level = _parse_log_level(_args)
    from .daemon import Daemon
    Daemon(console=True, dry_run=dry_run, log_level=log_level).run()
    return 0


def cmd__run(_args) -> int:
    from .daemon import Daemon
    Daemon(console=False).run()
    return 0


def cmd_once(_args) -> int:
    from .detection import Detector
    from .logging_setup import get_logger
    det = Detector(load_config(), get_logger(console=True))
    state = det.poll()
    d = dataclasses.asdict(state)
    d["game_tree"] = sorted(state.game_tree)  # frozenset -> list for JSON
    print(json.dumps(d, indent=2))
    return 0


def cmd_payload(_args) -> int:
    """Print a representative gaming payload — the receiving-side contract."""
    from .telemetry import TelemetryClient
    from .logging_setup import get_logger
    from .models import GameState, ThrottledProc, ThrottleResult
    gs = GameState(
        active=True, app_id=730, game_pid=4242, game_name="game.exe",
        foreground_pid=4242, foreground_name="game.exe", corroborated=True,
        reason="example",
    )
    tr = ThrottleResult(
        soft=[ThrottledProc(pid=111, name="chrome.exe", tier="soft",
                            original_priority=32, ecoqos_applied=True)],
        hard=[],
    )
    tel = TelemetryClient(load_config(), get_logger(console=False))
    print(json.dumps(tel.build_payload("state_change", "gaming", gs, tr), indent=2))
    return 0


def cmd_version(_args) -> int:
    print(f"foundry-warden {__version__}")
    return 0


COMMANDS = {
    "install": cmd_install, "uninstall": cmd_uninstall,
    "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
    "status": cmd_status, "run": cmd_run, "_run": cmd__run,
    "once": cmd_once, "payload": cmd_payload, "version": cmd_version,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        print("usage: python run_warden.py <command>")
        print("commands:", ", ".join(COMMANDS))
        return 0
    cmd = argv[0]
    fn = COMMANDS.get(cmd)
    if not fn:
        print(f"unknown command: {cmd}")
        print("commands:", ", ".join(COMMANDS))
        return 2
    return fn(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
