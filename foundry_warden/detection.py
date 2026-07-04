"""Game detection: turn OS/Steam signals into a single GameState per poll.

The authoritative trigger is Steam's ``RunningAppID`` registry value: non-zero
means Steam launched something. Because that flag stays set for launchers and
overlays too, an optional foreground corroboration step confirms an actual game
window has focus (with a timed grace fallback for borderless / alt-tabbed play).

``Detector.poll()`` never raises -- any failure degrades to "no game" so the
daemon loop stays crash-resistant. The daemon logs state transitions itself, so
this module stays quiet except for genuine errors (at debug level).
"""

from __future__ import annotations

import os
import re
import time
import winreg
from typing import Optional

from . import winapi
from .models import GameState, ProcInfo

_STEAM_KEY = r"Software\Valve\Steam"
_STEAM_VALUE = "RunningAppID"
_STEAM_EXE = "steam.exe"
# Steam's own helper processes are descendants of steam.exe but are NOT the game.
_STEAM_HELPERS = frozenset({
    "steam.exe", "steamwebhelper.exe", "steamservice.exe",
    "gameoverlayui.exe", "gameoverlayui64.exe",
    "steamerrorreporter.exe", "steamerrorreporter64.exe", "crashhandler.exe",
})


class Detector:
    """Stateful per-poll game detector. One instance per daemon run."""

    def __init__(self, config: dict, logger) -> None:
        self._log = logger
        det = (config or {}).get("detection", {})
        self._require_corroboration: bool = bool(
            det.get("require_foreground_corroboration", True)
        )
        self._grace_sec: float = float(det.get("corroboration_grace_sec", 0.0) or 0.0)
        # Normalise the ignore list to a lower-cased set for cheap membership tests.
        self._ignore: frozenset[str] = frozenset(
            str(n).lower() for n in det.get("foreground_ignore", [])
        )
        # Timing state: monotonic timestamp when RunningAppID first became
        # non-zero in the current continuous run (None when appid == 0).
        self._appid_since: Optional[float] = None
        # Caches for authoritative game-name lookup via Steam appmanifests.
        self._name_cache: dict[int, Optional[str]] = {}
        self._steamapps: Optional[list[str]] = None
        # Sticky latch: once we have entered game-mode we STAY active until
        # RunningAppID returns to 0. Foreground corroboration gates ENTRY only;
        # losing focus (alt-tab) must not drop game-mode and flap the throttle.
        self._active: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def poll(self) -> GameState:
        """Return one GameState. Never raises."""
        try:
            return self._poll()
        except Exception as exc:  # pragma: no cover - defensive last resort
            self._log.debug("detection poll failed: %r", exc)
            self._appid_since = None
            return GameState(active=False, reason="poll error")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _poll(self) -> GameState:
        app_id = self._read_running_appid()

        if app_id == 0:
            # Steam reports nothing running -> exit game-mode and reset state.
            self._appid_since = None
            self._active = False
            return GameState(active=False, app_id=0, reason="RunningAppID=0")

        # Track how long RunningAppID has been continuously non-zero.
        now = time.monotonic()
        if self._appid_since is None:
            self._appid_since = now

        # One process snapshot, reused for foreground resolution and the tree.
        procs = winapi.enum_processes()
        by_pid = {p.pid: p for p in procs}

        fg_pid = winapi.get_foreground_pid()
        fg_proc = by_pid.get(fg_pid) if fg_pid > 0 else None
        fg_name = fg_proc.name if fg_proc is not None else ""

        # Corroborated == a real (non-ignored) game window is focused.
        corroborated = (
            fg_pid > 0
            and fg_name != ""
            and fg_name not in self._ignore
        )

        # Decide whether we are active. Once latched active, STAY active until
        # RunningAppID hits 0 (handled above) -- alt-tabbing must not flap.
        if self._active:
            active = True
            reason = f"RunningAppID={app_id} sustained"
        elif not self._require_corroboration:
            active = True
            reason = f"RunningAppID={app_id}"
        elif corroborated:
            active = True
            reason = f"RunningAppID={app_id} + foreground '{fg_name}'"
        else:
            # Corroboration currently fails -> consult the grace fallback.
            elapsed = now - self._appid_since
            if self._grace_sec > 0 and elapsed >= self._grace_sec:
                active = True
                reason = (
                    f"RunningAppID={app_id} grace fallback "
                    f"({elapsed:.0f}s >= {self._grace_sec:.0f}s)"
                )
            else:
                active = False
                if self._grace_sec > 0:
                    reason = (
                        f"RunningAppID={app_id} awaiting corroboration "
                        f"({elapsed:.0f}/{self._grace_sec:.0f}s)"
                    )
                else:
                    reason = f"RunningAppID={app_id} but foreground not corroborated"

        # Latch on entry so subsequent polls stay active regardless of focus.
        if active:
            self._active = True

        if not active:
            return GameState(
                active=False,
                app_id=app_id,
                foreground_pid=fg_pid,
                foreground_name=fg_name,
                corroborated=corroborated,
                reason=reason,
            )

        # Active: identify the running game and the full Steam process tree.
        game_pid, fallback_name, descendants, steam_pids = self._identify_game(
            procs, fg_pid, fg_name
        )
        # Authoritative name from Steam's appmanifest; fall back to the detected
        # process name, then a corroborating foreground name.
        game_name = (
            self._game_name_from_appid(app_id)
            or fallback_name
            or (fg_name if fg_name not in self._ignore else "")
        )
        # Protect the WHOLE Steam tree (game + overlay + any Steam-spawned
        # helper), not just one pid -- robust against helper processes.
        game_tree = frozenset(descendants | steam_pids)

        return GameState(
            active=True,
            app_id=app_id,
            game_pid=game_pid,
            game_name=game_name,
            foreground_pid=fg_pid,
            foreground_name=fg_name,
            corroborated=corroborated,
            reason=reason,
            game_tree=game_tree,
        )

    def _identify_game(
        self,
        procs: list[ProcInfo],
        fg_pid: int,
        fg_name: str,
    ) -> tuple[int, str, set[int], set[int]]:
        """Identify the game process + the whole Steam tree.

        Returns (game_pid, fallback_name, steam_descendants, steam_pids). A Steam
        game is launched as a descendant of steam.exe, so we identify it from the
        process tree rather than trusting whatever window happens to be focused
        (which may be a non-game window like a terminal at the instant we enter
        game-mode). Steam's own helpers (overlay, webhelper, ...) are excluded as
        game candidates but are still returned in the descendant set so the
        throttle engine can protect the entire Steam tree.
        """
        steam_pids = {p.pid for p in procs if p.name == _STEAM_EXE}

        # All descendants of any steam.exe (walk the ppid tree).
        descendants: set[int] = set()
        if steam_pids:
            children: dict[int, list[int]] = {}
            for p in procs:
                children.setdefault(p.ppid, []).append(p.pid)
            stack = list(steam_pids)
            while stack:
                cur = stack.pop()
                for c in children.get(cur, []):
                    if c not in descendants:
                        descendants.add(c)
                        stack.append(c)

        # Candidate games = Steam descendants that are not Steam's own helpers
        # and not on the ignore list.
        candidates = [
            p for p in procs
            if p.pid in descendants
            and p.name not in _STEAM_HELPERS
            and p.name not in self._ignore
        ]
        game_pid, fallback_name = 0, ""
        if candidates:
            chosen = None
            for p in candidates:  # prefer the focused candidate (usual case)
                if p.pid == fg_pid:
                    chosen = p
                    break
            if chosen is None:
                chosen = candidates[0]
            game_pid, fallback_name = chosen.pid, chosen.name
        elif fg_pid in descendants and fg_pid > 0 and fg_name not in self._ignore:
            game_pid, fallback_name = fg_pid, fg_name

        return game_pid, fallback_name, descendants, steam_pids

    # ------------------------------------------------------------------
    # Authoritative game name from Steam's appmanifest_<appid>.acf
    # ------------------------------------------------------------------
    def _game_name_from_appid(self, app_id: int) -> Optional[str]:
        """Return the human game name for an app id from Steam's manifests."""
        if app_id <= 0:
            return None
        if app_id in self._name_cache:
            return self._name_cache[app_id]
        name: Optional[str] = None
        try:
            for d in self._steamapps_dirs():
                acf = os.path.join(d, f"appmanifest_{app_id}.acf")
                if os.path.isfile(acf):
                    with open(acf, encoding="utf-8", errors="replace") as fh:
                        txt = fh.read()
                    m = re.search(r'"name"\s*"([^"]*)"', txt)
                    if m:
                        name = m.group(1)
                        break
        except Exception as exc:
            self._log.debug("appmanifest lookup failed for %s: %s", app_id, exc)
        self._name_cache[app_id] = name
        return name

    def _steamapps_dirs(self) -> list[str]:
        """All steamapps library dirs (main install + extra libraries). Cached."""
        if self._steamapps is not None:
            return self._steamapps
        dirs: list[str] = []
        try:
            steam_path = self._read_steam_path()
            if steam_path:
                main = os.path.join(steam_path, "steamapps")
                dirs.append(main)
                vdf = os.path.join(main, "libraryfolders.vdf")
                if os.path.isfile(vdf):
                    with open(vdf, encoding="utf-8", errors="replace") as fh:
                        txt = fh.read()
                    for m in re.finditer(r'"path"\s*"([^"]*)"', txt):
                        p = m.group(1).replace("\\\\", "\\")
                        dirs.append(os.path.join(p, "steamapps"))
        except Exception as exc:
            self._log.debug("steamapps discovery failed: %s", exc)
        seen: set[str] = set()
        out: list[str] = []
        for d in dirs:
            dl = d.lower()
            if dl not in seen:
                seen.add(dl)
                out.append(d)
        self._steamapps = out
        return out

    @staticmethod
    def _read_steam_path() -> Optional[str]:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STEAM_KEY) as key:
                val, _ = winreg.QueryValueEx(key, "SteamPath")
            return str(val)
        except Exception:
            return None

    @staticmethod
    def _read_running_appid() -> int:
        """Read HKCU\\Software\\Valve\\Steam\\RunningAppID. Any error -> 0."""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STEAM_KEY) as key:
                value, regtype = winreg.QueryValueEx(key, _STEAM_VALUE)
            return int(value)
        except Exception:
            return 0
