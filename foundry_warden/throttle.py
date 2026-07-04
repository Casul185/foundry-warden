"""Throttle engine: apply and reverse process throttling for game-mode.

This module owns the *mutation* side of the daemon. When detection declares
game-mode, ``engage()`` walks one process snapshot and applies two tiers:

  * SOFT -- Idle priority class + EcoQoS power throttling (fully reversible).
  * HARD -- full NtSuspendProcess (disruptive; empty tier by default).

Every change is recorded as a :class:`~foundry_warden.models.ThrottledProc`
and persisted to ``THROTTLE_STATE_FILE`` so that a crashed daemon can undo its
changes on the next launch (see :meth:`ThrottleEngine.recover_from_disk`).

In dry-run mode the engine walks the exact same decision path but logs the
actions it WOULD take instead of taking them (nothing is tracked or persisted).

A multi-layered protection set guarantees we never touch the OS core, the game
and its descendants, or the daemon itself -- by name *or* by pid. The
``CRITICAL_SAFETY_FLOOR`` is enforced unconditionally so a bad config can never
suspend critical system processes.

No public method ever raises: native operations legitimately fail on protected
or already-exited processes, and the daemon must stay alive regardless.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict

from .config import CRITICAL_SAFETY_FLOOR, THROTTLE_STATE_FILE
from .models import GameState, ThrottledProc, ThrottleResult
from . import winapi


class ThrottleEngine:
    """Applies, tracks, persists, and reverses process throttling."""

    def __init__(self, config: dict, logger, dry_run: bool = False) -> None:
        self._config = config
        self._log = logger
        self._dry_run = dry_run
        self._engaged = False
        self._tracked: list[ThrottledProc] = []

    # -- public state ------------------------------------------------------
    def is_engaged(self) -> bool:
        """True if throttling is currently applied."""
        return self._engaged

    def current(self) -> list[ThrottledProc]:
        """Return the list of currently throttled processes (for telemetry/heartbeat)."""
        return list(self._tracked)

    # -- engage ------------------------------------------------------------
    def engage(self, game_state: GameState) -> ThrottleResult:
        """Apply throttling for the given game state. Idempotent while engaged."""
        result = ThrottleResult()
        try:
            if self._engaged:
                self._log.debug("throttle.engage: already engaged, skipping re-apply")
                # Reflect current tracked state back to the caller.
                for tp in self._tracked:
                    if tp.tier == "hard":
                        result.hard.append(tp)
                    else:
                        result.soft.append(tp)
                return result

            tcfg = self._config.get("throttle", {})
            soft_tier = {n.lower() for n in tcfg.get("soft_tier", [])}
            hard_tier = {n.lower() for n in tcfg.get("hard_tier", [])}
            apply_idle = bool(tcfg.get("apply_idle_priority", True))
            apply_eco = bool(tcfg.get("apply_ecoqos", True))

            snapshot = winapi.enum_processes()
            protected_names, protected_pids = self._build_protect_set(
                game_state, snapshot)
            tracked: list[ThrottledProc] = []

            for proc in snapshot:
                name = proc.name
                pid = proc.pid

                if name in protected_names or pid in protected_pids:
                    if name in hard_tier or name in soft_tier:
                        result.skipped_protected.append(name)
                    continue

                # HARD takes precedence over SOFT.
                if name in hard_tier:
                    if self._dry_run:
                        self._log.info(
                            "[dry-run] would HARD-suspend %s (pid %d)", name, pid)
                        result.hard.append(
                            ThrottledProc(pid=pid, name=name, tier="hard"))
                        continue
                    tp = self._engage_hard(name, pid, result)
                    if tp is not None:
                        tracked.append(tp)
                elif name in soft_tier:
                    if self._dry_run:
                        self._log.info(
                            "[dry-run] would SOFT-throttle %s (pid %d) "
                            "(idle_priority=%s ecoqos=%s)",
                            name, pid, apply_idle, apply_eco)
                        result.soft.append(
                            ThrottledProc(pid=pid, name=name, tier="soft"))
                        continue
                    tp = self._engage_soft(name, pid, apply_idle, apply_eco, result)
                    if tp is not None:
                        tracked.append(tp)

            if self._dry_run:
                # Log intent only: nothing tracked, persisted, or engaged.
                self._log.info(
                    "[dry-run] throttle NOT engaged: would affect %d soft, "
                    "%d hard (%d protected-skips)",
                    len(result.soft), len(result.hard),
                    len(result.skipped_protected),
                )
                return result

            self._tracked = tracked
            self._engaged = True
            for tp in tracked:
                if tp.tier == "hard":
                    result.hard.append(tp)
                else:
                    result.soft.append(tp)

            self._persist()
            self._log.info(
                "throttle engaged: %d soft, %d hard (%d protected-skips, %d errors)",
                len(result.soft), len(result.hard),
                len(result.skipped_protected), len(result.errors),
            )
        except Exception as exc:  # never raise out of a public method
            self._log.debug("throttle.engage: unexpected error: %s", exc)
            result.errors.append(str(exc))
        return result

    def _engage_hard(
        self, name: str, pid: int, result: ThrottleResult
    ) -> ThrottledProc | None:
        try:
            if winapi.suspend_process(pid):
                return ThrottledProc(pid=pid, name=name, tier="hard", suspended=True)
            self._log.debug("throttle: suspend failed for %s (pid %d)", name, pid)
        except Exception as exc:
            self._log.debug("throttle: suspend error for %s (pid %d): %s", name, pid, exc)
            result.errors.append(f"suspend {name}({pid}): {exc}")
        return None

    def _engage_soft(
        self, name: str, pid: int, apply_idle: bool, apply_eco: bool,
        result: ThrottleResult,
    ) -> ThrottledProc | None:
        try:
            original_priority = None
            ecoqos_applied = False
            touched = False

            if apply_idle:
                original_priority = winapi.get_priority_class(pid)
                if winapi.set_priority_class(pid, winapi.IDLE_PRIORITY_CLASS):
                    touched = True
                else:
                    # Could not lower priority; don't claim we did.
                    original_priority = None
                    self._log.debug(
                        "throttle: set idle priority failed for %s (pid %d)", name, pid
                    )

            if apply_eco:
                if winapi.enable_ecoqos(pid):
                    ecoqos_applied = True
                    touched = True
                else:
                    self._log.debug(
                        "throttle: enable EcoQoS failed for %s (pid %d)", name, pid
                    )

            if not touched:
                return None
            return ThrottledProc(
                pid=pid,
                name=name,
                tier="soft",
                original_priority=original_priority,
                ecoqos_applied=ecoqos_applied,
            )
        except Exception as exc:
            self._log.debug("throttle: soft error for %s (pid %d): %s", name, pid, exc)
            result.errors.append(f"soft {name}({pid}): {exc}")
        return None

    # -- restore -----------------------------------------------------------
    def restore(self) -> ThrottleResult:
        """Undo all tracked throttling. Robust to exited processes."""
        result = ThrottleResult()
        if self._dry_run:
            # Nothing was applied in dry-run; also never touch state persisted
            # by a previous real run from inside a dry-run.
            self._log.info("[dry-run] restore: nothing to undo")
            return result
        try:
            procs = self._tracked
            if not procs:
                procs = self._load_from_disk()

            for tp in procs:
                self._undo_one(tp, result)

            self._delete_state_file()
            self._tracked = []
            self._engaged = False
            self._log.info(
                "throttle restored: %d soft, %d hard (%d errors)",
                len(result.soft), len(result.hard), len(result.errors),
            )
        except Exception as exc:
            self._log.debug("throttle.restore: unexpected error: %s", exc)
            result.errors.append(str(exc))
        return result

    def _undo_one(self, tp: ThrottledProc, result: ThrottleResult) -> None:
        try:
            if not winapi.process_alive(tp.pid):
                # Dead process needs no restore.
                return
            if tp.suspended:
                if not winapi.resume_process(tp.pid):
                    self._log.debug(
                        "throttle: resume failed for %s (pid %d)", tp.name, tp.pid
                    )
            if tp.ecoqos_applied:
                if not winapi.disable_ecoqos(tp.pid):
                    self._log.debug(
                        "throttle: disable EcoQoS failed for %s (pid %d)",
                        tp.name, tp.pid,
                    )
            if tp.original_priority is not None:
                if not winapi.set_priority_class(tp.pid, tp.original_priority):
                    self._log.debug(
                        "throttle: restore priority failed for %s (pid %d)",
                        tp.name, tp.pid,
                    )
            if tp.tier == "hard":
                result.hard.append(tp)
            else:
                result.soft.append(tp)
        except Exception as exc:
            self._log.debug(
                "throttle: undo error for %s (pid %d): %s", tp.name, tp.pid, exc
            )
            result.errors.append(f"restore {tp.name}({tp.pid}): {exc}")

    # -- crash recovery ----------------------------------------------------
    def recover_from_disk(self) -> None:
        """On startup, undo any throttling left over from a crashed run."""
        try:
            if not THROTTLE_STATE_FILE.exists():
                return
            if self._dry_run:
                self._log.info(
                    "[dry-run] leftover throttle state found at %s; would "
                    "restore it (taking no action in dry-run)",
                    THROTTLE_STATE_FILE,
                )
                return
            procs = self._load_from_disk()
            result = ThrottleResult()
            for tp in procs:
                self._undo_one(tp, result)
            self._delete_state_file()
            self._tracked = []
            self._engaged = False
            self._log.info(
                "crash recovery: restored %d processes (%d errors)",
                len(result.soft) + len(result.hard), len(result.errors),
            )
        except Exception as exc:
            self._log.debug("throttle.recover_from_disk: error: %s", exc)

    # -- protect set -------------------------------------------------------
    def _build_protect_set(
        self, game_state: GameState, snapshot: list | None = None,
    ) -> tuple[set[str], set[int]]:
        """Compute the names + pids that must never be throttled.

        THREE distinct sources are unioned here (kept separate by concept):
          1. CRITICAL_SAFETY_FLOOR  -- unconditional OS safety floor (cannot be
             disabled by config); a bad config can never suspend the OS.
          2. config["protect"]      -- the system protect-list (Steam, the game
             tree, the daemon, Sunshine, shell/system processes).
          3. config["user_allowlist"] -- the OPERATOR's editable list of apps used
             WHILE gaming that must not be throttled (Discord, AMD Adrenalin,
             music client, ...). Not system-critical, fully editable, separate on
             purpose so the safety floor stays minimal.
        Allowlisted apps are matched by name AND by process tree, so an app's
        differently-named child/helper processes are covered too.
        """
        pcfg = self._config.get("protect", {})
        names: set[str] = {n.lower() for n in pcfg.get("names", [])}
        names |= {n.lower() for n in CRITICAL_SAFETY_FLOOR}

        # (3) user allowlist names.
        allowlist: set[str] = {
            str(n).lower() for n in self._config.get("user_allowlist", [])
        }
        names |= allowlist

        pids: set[int] = set()
        if pcfg.get("protect_self", True):
            self_pid = os.getpid()
            pids.add(self_pid)
            try:
                pids |= winapi.get_process_tree(self_pid)
            except Exception as exc:
                self._log.debug("throttle: protect_self tree failed: %s", exc)

        if pcfg.get("protect_game_tree", True):
            if game_state.game_pid:
                pids.add(game_state.game_pid)
            pids |= set(game_state.game_tree)

        # Cover the whole process tree of every running allowlisted app, so a
        # helper process with a DIFFERENT exe name (e.g. an Electron/voice child)
        # is shielded as well, not just the top-level exe.
        if allowlist:
            try:
                procs = snapshot if snapshot is not None else winapi.enum_processes()
                for p in procs:
                    if p.name in allowlist:
                        pids.add(p.pid)
                        pids |= winapi.get_process_tree(p.pid, procs=procs)
            except Exception as exc:
                self._log.debug("throttle: allowlist tree expand failed: %s", exc)

        return names, pids

    # -- persistence -------------------------------------------------------
    def _persist(self) -> None:
        try:
            THROTTLE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = [asdict(tp) for tp in self._tracked]
            THROTTLE_STATE_FILE.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            self._log.debug("throttle: could not persist state: %s", exc)

    def _load_from_disk(self) -> list[ThrottledProc]:
        out: list[ThrottledProc] = []
        try:
            if not THROTTLE_STATE_FILE.exists():
                return out
            raw = json.loads(THROTTLE_STATE_FILE.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return out
            for item in raw:
                if not isinstance(item, dict):
                    continue
                try:
                    out.append(ThrottledProc(
                        pid=int(item["pid"]),
                        name=str(item.get("name", "")),
                        tier=str(item.get("tier", "soft")),
                        original_priority=item.get("original_priority"),
                        ecoqos_applied=bool(item.get("ecoqos_applied", False)),
                        suspended=bool(item.get("suspended", False)),
                    ))
                except Exception as exc:
                    self._log.debug("throttle: skipping bad state entry: %s", exc)
        except Exception as exc:
            self._log.debug("throttle: could not load state file: %s", exc)
        return out

    def _delete_state_file(self) -> None:
        try:
            if THROTTLE_STATE_FILE.exists():
                THROTTLE_STATE_FILE.unlink()
        except Exception as exc:
            self._log.debug("throttle: could not delete state file: %s", exc)
