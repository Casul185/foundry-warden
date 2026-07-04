"""Lifecycle via a per-user Scheduled Task (start at logon, survives reboot).

A SYSTEM service runs in session 0 and cannot read the user's Steam
RunningAppID, see the foreground window, or set EcoQoS on the interactive game.
So lifecycle is a logon-triggered Scheduled Task that runs the daemon inside the
user's own session, elevated (/RL HIGHEST) so the HARD tier can suspend elevated
processes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import PROJECT_ROOT, TASK_NAME


def pythonw_exe() -> str:
    """Windowless interpreter alongside the current python.exe (fallback: python)."""
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.exists() else exe)


def runner_script() -> str:
    return str(PROJECT_ROOT / "run_warden.py")


def task_command() -> str:
    return f'"{pythonw_exe()}" "{runner_script()}" _run'


def _schtasks(args: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["schtasks", *args], capture_output=True, text=True
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, out.strip()
    except Exception as e:  # schtasks missing / blocked
        return False, f"failed to invoke schtasks: {e}"


def install(highest: bool = True) -> tuple[bool, str]:
    """Create/replace the logon task. Requires admin for /RL HIGHEST."""
    args = [
        "/Create", "/TN", TASK_NAME, "/TR", task_command(),
        "/SC", "ONLOGON", "/F", "/IT",
    ]
    if highest:
        args += ["/RL", "HIGHEST"]
    user = os.environ.get("USERNAME")
    if user:
        domain = os.environ.get("USERDOMAIN", "")
        args += ["/RU", f"{domain}\\{user}" if domain else user]
    return _schtasks(args)


def uninstall() -> tuple[bool, str]:
    return _schtasks(["/Delete", "/TN", TASK_NAME, "/F"])


def run_now() -> tuple[bool, str]:
    return _schtasks(["/Run", "/TN", TASK_NAME])


def end_now() -> tuple[bool, str]:
    return _schtasks(["/End", "/TN", TASK_NAME])


def status() -> tuple[bool, str]:
    return _schtasks(["/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"])


def is_installed() -> bool:
    ok, _ = _schtasks(["/Query", "/TN", TASK_NAME])
    return ok
