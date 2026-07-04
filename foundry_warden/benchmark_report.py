"""Render and persist per-session benchmark results.

Three output forms, all derived from a single :class:`BenchmarkResult`:

* a compact human-readable console/log report (``format_console_report``),
* a full JSON-serializable dict for on-disk session records
  (``benchmark_to_dict`` / ``write_session_record``), and
* a small summary dict for the telemetry performance-history payload
  (``benchmark_summary``).

Honesty is a design goal here: the headline only credits throttling with the
deltas attributed to throttled processes, system-wide numbers are explicitly
labelled as context (the game's own load moves them too), and a negligible
measured effect is reported as negligible rather than dressed up as a win.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .config import BENCHMARKS_DIR, ensure_dirs
from .models import BenchmarkResult


# Thresholds below which the measured effect is considered noise, not a win.
_NEGLIGIBLE_CPU_PCT = 1.0
_NEGLIGIBLE_WS_MB = 50.0

# Console table column widths (monospace alignment).
_W_NAME = 24
_W_TIER = 5

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _f(value: float | None) -> float:
    """Coerce a possibly-None number to a float (None -> 0.0)."""
    return float(value) if value is not None else 0.0


def format_console_report(result: BenchmarkResult) -> str:
    """Build a compact, human-readable multi-line report string.

    Logged by the daemon at game exit. Monospace alignment is assumed.
    """
    lines: list[str] = []

    # --- Header -----------------------------------------------------------
    lines.append(
        f"Foundry-Warden benchmark v{__version__} | node={result.node} | "
        f"game={result.game_name} (app_id={result.app_id})"
    )
    lines.append(
        f"session: {result.started_iso or '?'} -> {result.ended_iso or '?'}"
    )

    # --- Headline (attributed to throttling) ------------------------------
    cpu = _f(result.cpu_freed_pct)
    ws = _f(result.ws_freed_mb)
    count = result.throttled_count

    if count == 0:
        headline = "HEADLINE: no processes were throttled this session."
    elif cpu < _NEGLIGIBLE_CPU_PCT and ws < _NEGLIGIBLE_WS_MB:
        headline = (
            f"HEADLINE: Game-mode had a negligible measured effect this "
            f"session (~{cpu:.1f}% CPU and ~{ws:.1f} MB working set freed "
            f"from {count} throttled process{'es' if count != 1 else ''})."
        )
    else:
        headline = (
            f"HEADLINE: Game-mode freed ~{cpu:.1f}% CPU and ~{ws:.1f} MB "
            f"working set from {count} throttled "
            f"process{'es' if count != 1 else ''} (attributed to throttling)."
        )
    lines.append(headline)

    # --- Per-process table ------------------------------------------------
    if result.per_proc:
        lines.append("")
        header = (
            f"{'process':<{_W_NAME}} {'tier':<{_W_TIER}} "
            f"{'CPU before->after':>20} {'mem(MB) before->after':>24}"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for d in result.per_proc:
            name = (d.name or "")[:_W_NAME]
            tier = (d.tier or "")[:_W_TIER]
            cpu_cell = f"{_f(d.cpu_before):.1f}%->{_f(d.cpu_after):.1f}%"
            mem_cell = f"{_f(d.ws_before_mb):.1f}->{_f(d.ws_after_mb):.1f}"
            lines.append(
                f"{name:<{_W_NAME}} {tier:<{_W_TIER}} "
                f"{cpu_cell:>20} {mem_cell:>24}"
            )

    # --- Context (NOT attributed to throttling) ---------------------------
    base = _f(result.system_cpu_baseline)
    eng = _f(result.system_cpu_engaged)
    mem_delta = _f(result.mem_avail_delta_mb)
    lines.append("")
    lines.append(
        "context (NOT attributed to throttling; includes the game's own "
        f"load): system CPU {base:.1f}% -> {eng:.1f}%, "
        f"available memory delta {mem_delta:+.1f} MB"
    )

    # --- Notes ------------------------------------------------------------
    if result.notes:
        lines.append("")
        lines.append("notes:")
        for note in result.notes:
            lines.append(f"  - {note}")

    return "\n".join(lines)


def benchmark_to_dict(result: BenchmarkResult) -> dict:
    """Return a full JSON-serializable dict of the entire result.

    ``dataclasses.asdict`` recurses into the nested phase/delta dataclasses
    and renders ``None`` phases cleanly, so we return it directly.
    """
    return asdict(result)


def benchmark_summary(result: BenchmarkResult) -> dict:
    """Return a compact dict for the telemetry performance-history payload."""
    return {
        "node": result.node,
        "app_id": result.app_id,
        "game": result.game_name,
        "started": result.started_iso,
        "ended": result.ended_iso,
        "throttled_count": result.throttled_count,
        "cpu_freed_pct": round(_f(result.cpu_freed_pct), 2),
        "ws_freed_mb": round(_f(result.ws_freed_mb), 1),
        "system_cpu_baseline_pct": round(_f(result.system_cpu_baseline), 2),
        "system_cpu_engaged_pct": round(_f(result.system_cpu_engaged), 2),
        "mem_avail_delta_mb": round(_f(result.mem_avail_delta_mb), 1),
        "per_proc": [
            {
                "name": d.name,
                "tier": d.tier,
                "cpu_before": round(_f(d.cpu_before), 2),
                "cpu_after": round(_f(d.cpu_after), 2),
                "ws_before_mb": round(_f(d.ws_before_mb), 1),
                "ws_after_mb": round(_f(d.ws_after_mb), 1),
            }
            for d in result.per_proc
        ],
        "notes": list(result.notes),
    }


def _safe(text: str) -> str:
    """Replace any char not in [A-Za-z0-9._-] with '_'."""
    return _UNSAFE_CHARS.sub("_", text)


def write_session_record(result: BenchmarkResult, config: dict) -> Path:
    """Persist the full benchmark record as JSON under BENCHMARKS_DIR.

    Returns the path written. The filename is derived from the (made
    filesystem-safe) start timestamp and game name.
    """
    ensure_dirs()

    safe_ts = _safe(result.started_iso) if result.started_iso else "session"
    safe_game = _safe(result.game_name) if result.game_name else "game"
    filename = f"{safe_ts}_{safe_game}.json"

    path = BENCHMARKS_DIR / filename
    path.write_text(
        json.dumps(benchmark_to_dict(result), indent=2),
        encoding="utf-8",
    )
    return path
