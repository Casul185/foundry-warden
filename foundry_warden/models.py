"""Shared data contracts used across detection, throttle, and telemetry modules.

These dataclasses are the integration interface between subsystems. Keep them
stable: the throttle engine, telemetry client, and daemon loop all depend on these
exact field names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ProcInfo:
    """A snapshot of a single OS process."""

    pid: int
    name: str          # executable file name, lower-cased, e.g. "chrome.exe"
    ppid: int = 0      # parent process id (0 if unknown)


@dataclass
class GameState:
    """Result of one detection poll.

    `active` is the authoritative "are we gaming?" signal. When active, the
    game-identifying fields are populated as best they can be determined.
    """

    active: bool
    app_id: int = 0                       # Steam RunningAppID (0 == none)
    game_pid: int = 0                     # best-guess pid of the running game
    game_name: str = ""                   # best-guess exe of the running game
    foreground_pid: int = 0               # current foreground window's pid
    foreground_name: str = ""             # current foreground window's exe
    corroborated: bool = False            # foreground corroboration satisfied?
    reason: str = ""                      # human-readable detection explanation
    game_tree: frozenset[int] = field(default_factory=frozenset)  # game + descendants


@dataclass
class ThrottledProc:
    """Record of one process we modified, plus everything needed to restore it.

    This is what gets persisted to disk so a crashed daemon can recover and
    undo its changes on the next start.
    """

    pid: int
    name: str
    tier: str                              # "soft" or "hard"
    original_priority: Optional[int] = None  # priority class to restore (None = untouched)
    ecoqos_applied: bool = False           # did we enable EcoQoS? (restore = disable)
    suspended: bool = False                # did we suspend it? (restore = resume)


@dataclass
class ThrottleResult:
    """Outcome of an engage() call, summarised for logging and the telemetry payload."""

    soft: list[ThrottledProc] = field(default_factory=list)
    hard: list[ThrottledProc] = field(default_factory=list)
    skipped_protected: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Performance measurement / benchmarking
# ---------------------------------------------------------------------------
@dataclass
class ProcMetric:
    """One process's resource usage, averaged over a sampling window.

    cpu_pct is the share of TOTAL machine CPU capacity (all cores summed), so a
    process pegging one core on an 8-core box reads ~12.5%, and system_cpu_pct
    and per-process cpu_pct are on the same scale and directly comparable.
    """

    pid: int
    name: str
    cpu_pct: float            # % of total machine CPU over the window
    working_set_mb: float     # resident working set, MB
    tier: str = ""            # "soft" / "hard" / "" (set for throttle targets)


@dataclass
class PhaseMetrics:
    """Averaged system + process metrics for one benchmark phase."""

    phase: str                                  # "baseline" / "engaged" / "restored"
    duration_sec: float = 0.0
    samples: int = 0
    system_cpu_pct: float = 0.0                  # avg system-wide CPU% over window
    mem_total_mb: float = 0.0
    mem_avail_mb: float = 0.0
    mem_committed_mb: float = 0.0               # commit charge in use
    top_cpu: list[ProcMetric] = field(default_factory=list)   # top-N by cpu
    top_mem: list[ProcMetric] = field(default_factory=list)   # top-N by working set
    focus: list[ProcMetric] = field(default_factory=list)     # the throttle targets


@dataclass
class ProcDelta:
    """Before/after numbers for a single throttled process."""

    pid: int
    name: str
    tier: str
    cpu_before: float = 0.0
    cpu_after: float = 0.0       # measured in the ENGAGED phase
    cpu_restored: float = 0.0    # measured in the RESTORED phase
    ws_before_mb: float = 0.0
    ws_after_mb: float = 0.0
    ws_restored_mb: float = 0.0
    cpu_freed: float = 0.0       # cpu_before - cpu_after (>=0 means freed)
    ws_freed_mb: float = 0.0     # ws_before_mb - ws_after_mb


@dataclass
class BenchmarkResult:
    """A complete per-session benchmark: three phases + attributed deltas.

    Headline figures (cpu_freed_pct / ws_freed_mb) are the sum over THROTTLED
    processes only -- the part honestly attributable to the daemon. System-wide
    figures are kept as context and are NOT attributed to throttling (the game
    loading also moves them). `notes` records confounds and caveats.
    """

    node: str = ""
    app_id: int = 0
    game_name: str = ""
    started_iso: str = ""
    ended_iso: str = ""
    cpu_count: int = 0
    baseline: PhaseMetrics | None = None
    engaged: PhaseMetrics | None = None
    restored: PhaseMetrics | None = None
    per_proc: list[ProcDelta] = field(default_factory=list)
    throttled_count: int = 0
    cpu_freed_pct: float = 0.0                  # attributed to throttling
    ws_freed_mb: float = 0.0                    # attributed to throttling
    system_cpu_baseline: float = 0.0           # context only (incl. game load)
    system_cpu_engaged: float = 0.0            # context only
    mem_avail_delta_mb: float = 0.0            # context only
    notes: list[str] = field(default_factory=list)
