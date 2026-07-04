"""Per-session benchmarking: three phases, honestly-attributed deltas.

A :class:`SessionBenchmark` captures system + per-process metrics at three
points around a gaming session and turns them into a :class:`BenchmarkResult`:

    baseline   -- BEFORE throttling is applied (the "do nothing" reference)
    engaged    -- AFTER throttling is engaged, while the game runs
    restored   -- AFTER throttling is undone (game exited)

The headline figures (cpu_freed_pct / ws_freed_mb) are summed over ONLY the
processes we actually throttled, comparing their baseline vs engaged numbers.
That is the part honestly attributable to the daemon. System-wide CPU and
memory are recorded too, but kept strictly as context: the game itself is
launching and running through every phase, so those figures move for reasons
that have nothing to do with throttling and are NOT attributed to it. The
``notes`` field spells out these confounds for every result.

------------------------------------------------------------------------------
HONEST attribution and accuracy limits
------------------------------------------------------------------------------
* Only per-throttled-process before/after deltas are attributed to throttling.
  System CPU/memory include the game's own load and are context only.
* Suspended (HARD) and idled+EcoQoS (SOFT) processes need not release their
  working set, so a small or zero ws_freed is expected, not a failure.
* CPU% is the share of TOTAL machine capacity across all cores (see
  :mod:`foundry_warden.metrics`); a process pegging one of N cores reads
  ~100/N %.
* The sampler's short windows are quantised by the ~15.6 ms scheduler tick, so
  individual numbers are noisy; see metrics.py for the full provenance notes.

This module is pure bookkeeping on top of :class:`metrics.Sampler`; every
public method is wrapped so it can never raise into the daemon loop.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional

from .metrics import Sampler
from .models import (
    BenchmarkResult,
    GameState,
    PhaseMetrics,
    ProcDelta,
    ProcMetric,
    ThrottledProc,
)
from . import winapi


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string (normal program -> datetime is fine)."""
    return datetime.now(timezone.utc).isoformat()


class SessionBenchmark:
    """Captures three phases of a session and computes attributed deltas."""

    def __init__(self, config: dict, logger) -> None:
        self.config = config or {}
        self.log = logger
        self.bm = (self.config.get("benchmark", {}) or {})
        self.sampler = Sampler(config, logger)

        # Phase results, filled in as we go.
        self.baseline: Optional[PhaseMetrics] = None
        self.engaged: Optional[PhaseMetrics] = None
        self.restored: Optional[PhaseMetrics] = None

        # Session bookkeeping, filled in by the capture_* methods.
        self._started_iso: str = ""
        self._ended_iso: str = ""
        self._target_names: set[str] = set()
        self._throttled: list[ThrottledProc] = []
        self._tier_by_pid: dict[int, str] = {}
        self._name_by_pid: dict[int, str] = {}

    # ------------------------------------------------------------------
    # Phase 1: baseline (BEFORE throttling)
    # ------------------------------------------------------------------
    def capture_baseline(self, target_names: set[str]) -> None:
        """Capture the reference window before any throttling is applied."""
        try:
            self._started_iso = _now_iso()
            # Caller already lower-cases these; lower() again defensively.
            names = {str(n).lower() for n in (target_names or set())}
            self._target_names = names
            focus_pids = {p.pid for p in winapi.enum_processes() if p.name in names}
            self.baseline = self.sampler.capture_window(
                "baseline",
                self.bm.get("baseline_window_sec", 3.0),
                self.bm.get("sample_interval_sec", 1.0),
                focus_pids,
            )
        except Exception as exc:  # pragma: no cover - never raise into daemon
            self.log.debug("benchmark: capture_baseline failed: %s", exc)

    # ------------------------------------------------------------------
    # Phase 2: engaged (AFTER throttle engaged)
    # ------------------------------------------------------------------
    def capture_engaged(self, throttled: list[ThrottledProc]) -> None:
        """Capture the window while throttling is in effect."""
        try:
            self._throttled = list(throttled or [])
            self._tier_by_pid = {tp.pid: tp.tier for tp in self._throttled}
            self._name_by_pid = {tp.pid: tp.name for tp in self._throttled}
            # Let throttling settle before we measure (blocking is fine: this
            # runs on a background thread).
            time.sleep(float(self.bm.get("engaged_settle_sec", 0.0) or 0.0))
            focus_pids = {tp.pid for tp in self._throttled}
            self.engaged = self.sampler.capture_window(
                "engaged",
                self.bm.get("engaged_window_sec", 4.0),
                self.bm.get("sample_interval_sec", 1.0),
                focus_pids,
            )
        except Exception as exc:  # pragma: no cover - never raise into daemon
            self.log.debug("benchmark: capture_engaged failed: %s", exc)

    # ------------------------------------------------------------------
    # Phase 3: restored (AFTER throttle restored / game exited)
    # ------------------------------------------------------------------
    def capture_restored(self, throttled: list[ThrottledProc]) -> None:
        """Capture the window after throttling has been undone."""
        try:
            # Let things normalise after restore before we measure.
            time.sleep(float(self.bm.get("restored_settle_sec", 0.0) or 0.0))
            focus_pids = {tp.pid for tp in (throttled or [])}
            self.restored = self.sampler.capture_window(
                "restored",
                self.bm.get("restored_window_sec", 3.0),
                self.bm.get("sample_interval_sec", 1.0),
                focus_pids,
            )
            self._ended_iso = _now_iso()
        except Exception as exc:  # pragma: no cover - never raise into daemon
            self.log.debug("benchmark: capture_restored failed: %s", exc)

    # ------------------------------------------------------------------
    # Build the final result
    # ------------------------------------------------------------------
    @staticmethod
    def _focus_metric(phase: Optional[PhaseMetrics], pid: int) -> Optional[ProcMetric]:
        """Find the ProcMetric for `pid` in a phase's focus list, or None."""
        if phase is None:
            return None
        for m in phase.focus:
            if m.pid == pid:
                return m
        return None

    def build_result(self, node: str, game_state: GameState) -> BenchmarkResult:
        """Assemble the BenchmarkResult from the captured phases. Never raises."""
        try:
            notes: list[str] = []
            per_proc: list[ProcDelta] = []

            interval = self.bm.get("sample_interval_sec", 1.0)

            # --- Per-throttled-process before/after deltas --------------------
            missing_baseline: list[str] = []
            missing_restored: list[str] = []
            for tp in self._throttled:
                pid = tp.pid
                name = self._name_by_pid.get(pid, getattr(tp, "name", ""))
                tier = self._tier_by_pid.get(pid, getattr(tp, "tier", ""))

                b = self._focus_metric(self.baseline, pid)
                e = self._focus_metric(self.engaged, pid)
                r = self._focus_metric(self.restored, pid)

                cpu_before = b.cpu_pct if b else 0.0
                cpu_after = e.cpu_pct if e else 0.0
                cpu_restored = r.cpu_pct if r else 0.0
                ws_before_mb = b.working_set_mb if b else 0.0
                ws_after_mb = e.working_set_mb if e else 0.0
                ws_restored_mb = r.working_set_mb if r else 0.0

                if b is None:
                    missing_baseline.append("%s(%d)" % (name, pid))
                if r is None:
                    missing_restored.append("%s(%d)" % (name, pid))

                per_proc.append(
                    ProcDelta(
                        pid=pid,
                        name=name,
                        tier=tier,
                        cpu_before=cpu_before,
                        cpu_after=cpu_after,
                        cpu_restored=cpu_restored,
                        ws_before_mb=ws_before_mb,
                        ws_after_mb=ws_after_mb,
                        ws_restored_mb=ws_restored_mb,
                        cpu_freed=cpu_before - cpu_after,
                        ws_freed_mb=ws_before_mb - ws_after_mb,
                    )
                )

            # --- Headline: attributed to throttling (sum over throttled) ------
            cpu_freed_pct = sum(d.cpu_freed for d in per_proc)
            ws_freed_mb = sum(d.ws_freed_mb for d in per_proc)
            throttled_count = len(per_proc)

            # --- Context: NOT attributed to throttling ------------------------
            system_cpu_baseline = self.baseline.system_cpu_pct if self.baseline else 0.0
            system_cpu_engaged = self.engaged.system_cpu_pct if self.engaged else 0.0
            mem_avail_delta_mb = (
                (self.engaged.mem_avail_mb - self.baseline.mem_avail_mb)
                if (self.engaged and self.baseline) else 0.0
            )

            cpu_count = os.cpu_count() or 0

            # --- Notes: be honest ---------------------------------------------
            notes.append(
                "System-wide CPU and memory figures include the game's own load "
                "(the game is launching/running during all phases) and are NOT "
                "attributed to throttling; only per-throttled-process deltas are."
            )
            notes.append(
                "Suspended (HARD) and idled+EcoQoS (SOFT) processes do not "
                "necessarily release working-set memory, so a small or zero "
                "ws_freed is expected and is not a failure."
            )

            if self.baseline is None:
                notes.append("Baseline phase failed to capture (no baseline metrics).")
            if self.engaged is None:
                notes.append("Engaged phase failed to capture (no engaged metrics).")
            if self.restored is None:
                notes.append("Restored phase failed to capture (no restored metrics).")

            if not self._throttled:
                notes.append(
                    "No throttled processes were recorded (engaged phase never "
                    "ran or threw); per-process deltas are empty."
                )

            if missing_baseline:
                notes.append(
                    "The following were not measured at baseline (started after "
                    "the baseline window); their before-numbers are 0: "
                    + ", ".join(missing_baseline) + "."
                )
            if missing_restored:
                notes.append(
                    "The following were missing from the restored window and "
                    "likely exited before it: " + ", ".join(missing_restored) + "."
                )

            notes.append(
                "Sampler used a %s-second interval across each phase window; CPU%% "
                "is the share of total machine capacity across %d cores "
                "(os.cpu_count())." % (interval, cpu_count)
            )

            return BenchmarkResult(
                node=node,
                app_id=getattr(game_state, "app_id", 0),
                game_name=getattr(game_state, "game_name", ""),
                started_iso=self._started_iso,
                ended_iso=self._ended_iso,
                cpu_count=cpu_count,
                baseline=self.baseline,
                engaged=self.engaged,
                restored=self.restored,
                per_proc=per_proc,
                throttled_count=throttled_count,
                cpu_freed_pct=cpu_freed_pct,
                ws_freed_mb=ws_freed_mb,
                system_cpu_baseline=system_cpu_baseline,
                system_cpu_engaged=system_cpu_engaged,
                mem_avail_delta_mb=mem_avail_delta_mb,
                notes=notes,
            )
        except Exception as exc:  # pragma: no cover - never raise into daemon
            self.log.debug("benchmark: build_result failed: %s", exc)
            # Return a minimal, valid result rather than propagating.
            return BenchmarkResult(
                node=node,
                started_iso=self._started_iso,
                ended_iso=self._ended_iso,
                cpu_count=os.cpu_count() or 0,
                notes=["build_result failed: %s" % exc],
            )
