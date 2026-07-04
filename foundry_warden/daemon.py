"""The daemon core: poll detection -> drive throttle -> report to telemetry.

Lifecycle safety is the priority here. The loop guarantees that throttled
processes are restored on ANY exit path (clean stop, stop-flag, signal, or
unhandled error), and a stop-flag file makes the daemon stoppable from another
process without an ungraceful kill that would leave processes suspended.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import threading
import time

from .config import load_config, ensure_dirs, PID_FILE, STOP_FLAG
from .logging_setup import get_logger
from .detection import Detector
from .throttle import ThrottleEngine
from .telemetry import TelemetryClient
from .benchmark import SessionBenchmark
from . import benchmark_report
from .models import GameState


class Daemon:
    def __init__(self, config: dict | None = None, console: bool = False,
                 dry_run: bool = False):
        self.config = config or load_config()
        self.dry_run = dry_run
        _lvl = str(self.config.get("log_level", "INFO")).upper()
        self.log = get_logger(
            console=console,
            level=getattr(logging, _lvl, logging.INFO),
        )
        self.detector = Detector(self.config, self.log)
        self.throttle = ThrottleEngine(self.config, self.log, dry_run=dry_run)
        self.telemetry = TelemetryClient(self.config, self.log)
        self._stop = False
        self._gaming = False
        self._last_state: GameState | None = None
        self._last_heartbeat = 0.0
        # Benchmarking state
        self._bm_enabled = bool(
            (self.config.get("benchmark", {}) or {}).get("enabled", True)
        )
        self._bm_session: SessionBenchmark | None = None
        self._bm_engaged_thread: threading.Thread | None = None
        self._bm_final_thread: threading.Thread | None = None
        self._throttled_snapshot: list = []

    # -- shutdown plumbing ---------------------------------------------------
    def request_stop(self, *_a):
        self._stop = True

    def _restore_safe(self):
        """Undo all throttling; safe to call multiple times."""
        try:
            if self.throttle.is_engaged():
                self.log.info("restoring throttled processes")
                self.throttle.restore()
        except Exception:
            self.log.exception("restore failed during shutdown")

    def _write_pid(self):
        try:
            PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        except Exception:
            self.log.exception("could not write pid file")

    def _cleanup_pid(self):
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            self.log.exception("could not remove pid file")

    # -- main loop -----------------------------------------------------------
    def run(self):
        ensure_dirs()
        self._write_pid()

        # Crash recovery: undo throttling left behind by a previous crash.
        try:
            self.throttle.recover_from_disk()
        except Exception:
            self.log.exception("crash recovery failed")

        # Clear any stale stop flag from a previous run.
        try:
            STOP_FLAG.unlink()
        except FileNotFoundError:
            pass

        try:
            signal.signal(signal.SIGINT, self.request_stop)
            signal.signal(signal.SIGTERM, self.request_stop)
        except (ValueError, OSError):
            pass  # not in main thread / not supported -> stop flag still works
        atexit.register(self._restore_safe)

        self.log.info(
            "Foundry-Warden daemon started (pid=%s node=%s poll=%ss dry_run=%s)",
            os.getpid(), self.config.get("node_name"),
            self.config.get("poll_interval_sec"), self.dry_run,
        )
        self._heartbeat(force=True)  # announce idle on startup

        poll = float(self.config.get("poll_interval_sec", 2.0))
        try:
            while not self._stop:
                if STOP_FLAG.exists():
                    self.log.info("stop flag detected -> shutting down")
                    break
                self._tick()
                self._interruptible_sleep(poll)
        except Exception:
            self.log.exception("fatal error in daemon loop")
        finally:
            self.log.info("daemon stopping")
            self._restore_safe()
            # Best-effort final idle report so the telemetry endpoint sees idle.
            try:
                self.telemetry.send_state_change("idle", GameState(active=False), None)
            except Exception:
                pass
            self._cleanup_pid()
            try:
                STOP_FLAG.unlink()
            except FileNotFoundError:
                pass
            self.log.info("daemon stopped")

    def _interruptible_sleep(self, seconds: float):
        slept = 0.0
        step = 0.25
        while slept < seconds and not self._stop and not STOP_FLAG.exists():
            time.sleep(min(step, seconds - slept))
            slept += step

    def _tick(self):
        try:
            state = self.detector.poll()
        except Exception:
            self.log.exception("detection poll failed")
            state = GameState(active=False, reason="poll error")

        if state.active and not self._gaming:
            self._enter_game(state)
        elif not state.active and self._gaming:
            self._exit_game(state)
        elif state.active:
            self._last_state = state  # keep freshest game info for heartbeats

        self._heartbeat()

    def _throttle_target_names(self) -> set[str]:
        """Lower-cased names the throttle engine may act on (soft + hard tiers)."""
        tcfg = self.config.get("throttle", {})
        names = set()
        for key in ("soft_tier", "hard_tier"):
            names |= {str(n).lower() for n in tcfg.get(key, [])}
        return names

    def _enter_game(self, state: GameState):
        self.log.info(
            "GAME DETECTED app_id=%s game=%s pid=%s :: %s",
            state.app_id, state.game_name or "?", state.game_pid, state.reason,
        )

        # BASELINE must be captured BEFORE throttling. Done inline (short window)
        # so the "before" numbers reflect background apps running normally.
        if self._bm_enabled:
            try:
                self._bm_session = SessionBenchmark(self.config, self.log)
                win = (self.config.get("benchmark", {}) or {}).get(
                    "baseline_window_sec", 3.0)
                self.log.info(
                    "benchmark: capturing %.0fs baseline before throttle...", win)
                self._bm_session.capture_baseline(self._throttle_target_names())
            except Exception:
                self.log.exception("benchmark baseline failed")
                self._bm_session = None

        result = self.throttle.engage(state)
        self._gaming = True
        self._last_state = state
        self._throttled_snapshot = list(result.soft + result.hard)
        self.log.info(
            "throttle engaged: %d soft, %d hard",
            len(result.soft), len(result.hard),
        )
        self.telemetry.send_state_change("gaming", state, result)

        # ENGAGED capture (settle + window) runs in the background so the loop
        # keeps polling detection / staying responsive to stop.
        if self._bm_enabled and self._bm_session is not None:
            throttled = list(self._throttled_snapshot)
            self._bm_engaged_thread = threading.Thread(
                target=self._bm_session.capture_engaged, args=(throttled,),
                name="bm-engaged", daemon=True,
            )
            self._bm_engaged_thread.start()

    def _exit_game(self, state: GameState):
        self.log.info("GAME EXIT :: %s -> restoring", state.reason)
        self.throttle.restore()
        self._gaming = False
        last = self._last_state
        self._last_state = None

        if self._bm_enabled and self._bm_session is not None:
            # Finalise the benchmark off the main loop: wait for the engaged
            # capture, run the restored phase, log+persist the report, and send
            # the idle state-change carrying the benchmark summary.
            sess = self._bm_session
            throttled = list(self._throttled_snapshot)
            gs = last or state
            self._bm_session = None
            self._bm_final_thread = threading.Thread(
                target=self._finalize_benchmark,
                args=(sess, throttled, gs, state),
                name="bm-final", daemon=True,
            )
            self._bm_final_thread.start()
        else:
            # No benchmarking -> announce idle immediately, as before.
            self.telemetry.send_state_change("idle", state, None)

    def _finalize_benchmark(self, sess, throttled, game_state, exit_state):
        """Background: complete engaged+restored phases, report, persist, POST."""
        try:
            bm = self.config.get("benchmark", {}) or {}
            # Make sure the engaged window finished before measuring restored.
            if self._bm_engaged_thread is not None:
                budget = (float(bm.get("engaged_settle_sec", 4.0))
                          + float(bm.get("engaged_window_sec", 4.0)) + 5.0)
                self._bm_engaged_thread.join(timeout=budget)
            sess.capture_restored(throttled)
            result = sess.build_result(
                self.config.get("node_name", ""), game_state)

            # Human-readable report -> log (one line per row).
            for line in benchmark_report.format_console_report(result).splitlines():
                self.log.info("%s", line)

            # Persisted per-session record.
            try:
                path = benchmark_report.write_session_record(result, self.config)
                self.log.info("benchmark: session record written to %s", path)
            except Exception:
                self.log.exception("benchmark: failed to write session record")

            # Idle state-change carrying the benchmark summary.
            summary = benchmark_report.benchmark_summary(result)
            self.telemetry.send_state_change("idle", exit_state, None, benchmark=summary)
        except Exception:
            self.log.exception("benchmark finalize failed")
            # Still announce idle so the telemetry endpoint sees idle.
            try:
                self.telemetry.send_state_change("idle", exit_state, None)
            except Exception:
                pass

    def _heartbeat(self, force: bool = False):
        interval = float(self.config.get("heartbeat_interval_sec", 30.0))
        now = time.monotonic()
        if not force and (now - self._last_heartbeat) < interval:
            return
        self._last_heartbeat = now
        state_str = "gaming" if self._gaming else "idle"
        gs = self._last_state if self._gaming else None
        summary = self.throttle.current() if self._gaming else None
        self.telemetry.send_heartbeat(state_str, gs, summary)
