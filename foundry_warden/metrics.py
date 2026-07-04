"""Sampling of system + per-process CPU/memory over a timed window.

This module turns the raw native counters in :mod:`foundry_warden.winapi`
into averaged :class:`PhaseMetrics`. It is deliberately the only place that
knows how to combine cumulative tick counters into rates, so the benchmark
layer above stays pure bookkeeping.

------------------------------------------------------------------------------
Metric provenance and HONEST accuracy limits
------------------------------------------------------------------------------
system_cpu_pct
    Source: GetSystemTimes (kernel32) via winapi.get_system_times(), which
    returns cumulative (idle, kernel, user) 100ns ticks summed over ALL logical
    processors, with `kernel` INCLUDING `idle`. Busy fraction over an interval
    is (dKernel + dUser - dIdle) / (dKernel + dUser).
    Limits: GetSystemTimes is updated on the scheduler clock tick, which is
    ~15.6 ms (64 Hz) by default on Windows. Over a sub-2-second window only a
    few dozen ticks accumulate, so the result is quantised and noisy; the
    docstrings in config.py reflect why windows are kept >= ~2-3 s. The number
    is a time-average of busy CPU, not an instantaneous load, and it cannot
    distinguish per-core saturation (one pegged core on an 8-core box reads
    ~12.5%).

per-process cpu_pct  (ProcMetric.cpu_pct, top_cpu, focus)
    Source: GetProcessTimes (kernel32) via winapi.get_process_cpu_ticks(),
    cumulative (kernel+user) 100ns ticks for the process. Rate over an interval
    is dProcTicks / (dKernel + dUser of the SYSTEM) * 100. Using the system
    kernel+user delta as the denominator puts per-process and system CPU on the
    SAME "share of total machine capacity (all cores)" scale, so the per-process
    maximum is 100% across the WHOLE machine (a process fully using one of N
    cores reads ~100/N %). Values are clamped to [0, 100].
    Limits: same ~15.6 ms tick granularity as above -> noisy on short windows.
    A process needs two readings in an interval to contribute CPU for that
    interval; processes that start/exit mid-window contribute only over the
    intervals where they had two readings (0.0 if they never did). Requires
    PROCESS_QUERY_LIMITED_INFORMATION; protected/elevated processes we cannot
    open are silently omitted from that snapshot (not an error).

working_set_mb  (ProcMetric.working_set_mb, top_mem, focus)
    Source: K32GetProcessMemoryInfo (kernel32) via
    winapi.get_process_working_set(), the WorkingSetSize field, in bytes.
    This is an INSTANTANEOUS value (resident physical pages mapped by the
    process AT the final snapshot), NOT a window average, so it is read only
    once, on the last snapshot. It is "resident memory", NOT "total memory
    used": it excludes paged-out (committed but not resident) memory, and it
    COUNTS shared pages (DLLs, shared mappings) in every process that maps them,
    so summing working sets across processes double-counts shared memory. Bytes
    are converted with /1048576.0.

mem_total_mb / mem_avail_mb / mem_committed_mb
    Source: GlobalMemoryStatusEx (kernel32) via winapi.get_memory_status():
    ullTotalPhys / ullAvailPhys, and commit charge in use
    (ullTotalPageFile - ullAvailPageFile). Taken from the LAST snapshot
    (instantaneous, like working set). Bytes -> MB via /1048576.0.

------------------------------------------------------------------------------
Cost (this runs DURING gameplay, so it must be cheap)
------------------------------------------------------------------------------
Per snapshot: exactly ONE CreateToolhelp32Snapshot (enum_processes) to build
the pid->name map and pid list, then one OpenProcess+GetProcessTimes+CloseHandle
per pid (get_process_cpu_ticks). So a window of S snapshots costs S toolhelp
snapshots and ~S*P lightweight OpenProcess round-trips (P = process count).
Working set is read ONCE total, on the final snapshot, only for the pids we
actually rank/report (all live pids for top-mem + focus pids). Names are
resolved from the per-snapshot map, never by re-enumerating. GetSystemTimes /
GlobalMemoryStatusEx are O(1) once per snapshot.
"""

from __future__ import annotations

import time
from typing import Optional

from .models import PhaseMetrics, ProcMetric
from . import winapi

_BYTES_PER_MB = 1048576.0


class Sampler:
    """Captures averaged CPU/memory metrics over a timed window.

    Stateless between calls apart from config/logger; each capture_window() is
    fully self-contained.
    """

    def __init__(self, config: dict, logger) -> None:
        self.config = config
        self.log = logger
        bench = (config or {}).get("benchmark", {}) or {}
        # top_n governs how many processes we rank into top_cpu / top_mem.
        self.top_n = int(bench.get("top_n", 8) or 8)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------
    def _snapshot(self) -> Optional[tuple]:
        """Take one snapshot.

        Returns (monotonic_t, system_times, mem, {pid: (name, cpu_ticks)}) or
        None if even the basic enumeration failed. Never raises: a failed
        per-pid read just omits that pid; system_times/mem may be None and the
        caller copes (a None system_times pair is skipped).
        """
        try:
            t = time.monotonic()
            sys_times = winapi.get_system_times()   # (idle, kernel, user) or None
            mem = winapi.get_memory_status()        # dict or None
            procs = winapi.enum_processes()         # one toolhelp snapshot
            cpu: dict[int, tuple[str, int]] = {}
            for p in procs:
                ticks = winapi.get_process_cpu_ticks(p.pid)
                if ticks is None:
                    # Could not open / query this process this round -> omit it.
                    continue
                cpu[p.pid] = (p.name, ticks)
            return (t, sys_times, mem, cpu)
        except Exception as exc:  # pragma: no cover - defensive, never raise
            self.log.warning("metrics: snapshot failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Window capture
    # ------------------------------------------------------------------
    def capture_window(self, phase: str, duration_sec: float,
                       interval_sec: float, focus_pids: set[int]) -> PhaseMetrics:
        """Sample for ~duration_sec and return averaged PhaseMetrics.

        Takes an initial snapshot, then sleeps interval_sec and snapshots again,
        repeating for floor(duration/interval) intervals (minimum 1). CPU% is
        derived from consecutive snapshot pairs, so at least two snapshots are
        always taken.
        """
        focus_pids = set(focus_pids or set())
        interval = max(float(interval_sec), 0.001)
        # Aim for floor(duration/interval) intervals, at least 1 -> >= 2 snapshots.
        n_intervals = max(1, int(float(duration_sec) // interval))

        snapshots: list[tuple] = []
        start = time.monotonic()
        snapshots.append(self._snapshot() or (start, None, None, {}))

        for _ in range(n_intervals):
            time.sleep(interval)
            snapshots.append(self._snapshot() or (time.monotonic(), None, None, {}))

        elapsed = time.monotonic() - start

        # --- Accumulate per-interval CPU rates ---------------------------------
        # sys_busy_sum / sys_busy_count -> averaged system_cpu_pct.
        sys_busy_sum = 0.0
        sys_busy_count = 0
        # Per-pid: sum of cpu% over intervals where it had two readings, + count.
        proc_cpu_sum: dict[int, float] = {}
        proc_cpu_count: dict[int, int] = {}
        # Last-known name for every pid we ever saw (for naming focus/top entries).
        last_name: dict[int, str] = {}

        for a, b in zip(snapshots, snapshots[1:]):
            _, sa, _, ca = a
            _, sb, _, cb = b
            for pid, (nm, _ticks) in cb.items():
                last_name[pid] = nm
            for pid, (nm, _ticks) in ca.items():
                last_name.setdefault(pid, nm)

            if sa is None or sb is None:
                # No system times for this pair -> can't compute any rate; skip.
                continue
            d_idle = sb[0] - sa[0]
            d_kern = sb[1] - sa[1]
            d_user = sb[2] - sa[2]
            denom = d_kern + d_user  # kernel includes idle => total machine ticks
            if denom <= 0:
                continue

            busy = (d_kern + d_user - d_idle) / denom * 100.0
            if busy < 0.0:
                busy = 0.0
            elif busy > 100.0:
                busy = 100.0
            sys_busy_sum += busy
            sys_busy_count += 1

            # Per-process: only pids present in BOTH snapshots of this pair.
            for pid, (_nm, t_b) in cb.items():
                prev = ca.get(pid)
                if prev is None:
                    continue
                d_proc = t_b - prev[1]
                if d_proc < 0:
                    d_proc = 0
                pct = d_proc / denom * 100.0
                if pct < 0.0:
                    pct = 0.0
                elif pct > 100.0:
                    pct = 100.0
                proc_cpu_sum[pid] = proc_cpu_sum.get(pid, 0.0) + pct
                proc_cpu_count[pid] = proc_cpu_count.get(pid, 0) + 1

        system_cpu_pct = (sys_busy_sum / sys_busy_count) if sys_busy_count else 0.0

        def avg_cpu(pid: int) -> float:
            c = proc_cpu_count.get(pid, 0)
            return (proc_cpu_sum[pid] / c) if c else 0.0

        # --- Final-snapshot instantaneous data (mem + working sets) -----------
        last = snapshots[-1]
        last_mem = last[2]
        last_cpu_map = last[3]

        # Working sets: read once, only for pids we need (rank candidates + focus).
        need_ws: set[int] = set(last_cpu_map.keys()) | focus_pids
        ws_bytes: dict[int, int] = {}
        for pid in need_ws:
            wb = winapi.get_process_working_set(pid)
            if wb is not None:
                ws_bytes[pid] = wb

        def ws_mb(pid: int) -> float:
            return ws_bytes.get(pid, 0) / _BYTES_PER_MB

        def name_of(pid: int) -> str:
            return last_name.get(pid, "")

        # --- top_cpu: rank by averaged cpu% over all pids that ever had a rate -
        cpu_pids = set(proc_cpu_count.keys()) | set(last_cpu_map.keys())
        cpu_ranked = sorted(
            cpu_pids, key=lambda p: avg_cpu(p), reverse=True
        )[:self.top_n]
        top_cpu = [
            ProcMetric(pid=p, name=name_of(p), cpu_pct=avg_cpu(p),
                       working_set_mb=ws_mb(p), tier="")
            for p in cpu_ranked
        ]

        # --- top_mem: rank by working set (only pids we actually measured) -----
        mem_ranked = sorted(
            ws_bytes.keys(), key=lambda p: ws_bytes[p], reverse=True
        )[:self.top_n]
        top_mem = [
            ProcMetric(pid=p, name=name_of(p), cpu_pct=avg_cpu(p),
                       working_set_mb=ws_mb(p), tier="")
            for p in mem_ranked
        ]

        # --- focus: one entry per requested pid, always present ----------------
        focus = [
            ProcMetric(pid=p, name=name_of(p), cpu_pct=avg_cpu(p),
                       working_set_mb=ws_mb(p), tier="")
            for p in sorted(focus_pids)
        ]

        # --- Memory headline figures from the LAST snapshot -------------------
        if last_mem:
            mem_total_mb = last_mem["total_phys"] / _BYTES_PER_MB
            mem_avail_mb = last_mem["avail_phys"] / _BYTES_PER_MB
            mem_committed_mb = last_mem["committed"] / _BYTES_PER_MB
        else:
            mem_total_mb = mem_avail_mb = mem_committed_mb = 0.0

        return PhaseMetrics(
            phase=phase,
            duration_sec=elapsed,
            samples=sys_busy_count,
            system_cpu_pct=system_cpu_pct,
            mem_total_mb=mem_total_mb,
            mem_avail_mb=mem_avail_mb,
            mem_committed_mb=mem_committed_mb,
            top_cpu=top_cpu,
            top_mem=top_mem,
            focus=focus,
        )
