"""Optional outbound-only telemetry (disabled by default).

If you run several machines and want game-mode state visible on a dashboard,
point ``telemetry.endpoint`` at any HTTP endpoint that accepts a JSON POST.
The daemon sends periodic heartbeats and immediate state changes on game
enter/exit (the exit event carries the session benchmark summary); the
payload's ``event`` field distinguishes the two.

Every POST is best-effort: this module must NEVER raise and never crash the
daemon if the endpoint is down. When a POST cannot be delivered, the exact
JSON payload is logged with the greppable marker "TELEMETRY-PAYLOAD" so the
receiving side can be built against the real schema produced here.
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from logging import Logger
from typing import Any, Optional

from . import __version__


class TelemetryClient:
    """Builds state/heartbeat payloads and POSTs them to the endpoint (outbound only)."""

    def __init__(self, config: dict, logger: Logger) -> None:
        tel = config.get("telemetry", {}) or {}
        self.enabled: bool = bool(tel.get("enabled", False))
        self.endpoint: str = str(tel.get("endpoint", "")).rstrip("/")
        self.token: str = str(tel.get("token", "") or "")
        self.timeout_sec: float = float(tel.get("timeout_sec", 3.0))
        self.node_name: str = str(config.get("node_name", ""))
        self.logger = logger

    # ------------------------------------------------------------------ payload

    def build_payload(
        self,
        event: str,
        state: str,
        game_state: Any,
        throttle_summary: Any,
        benchmark: dict | None = None,
    ) -> dict:
        """Build the canonical telemetry payload (the receiving-side contract)."""
        return {
            "node": self.node_name,
            "event": event,
            "state": state,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "daemon": {"version": __version__, "pid": os.getpid()},
            "game": self._build_game(state, game_state),
            "throttle": self._build_throttle(throttle_summary),
            "benchmark": benchmark,
        }

    @staticmethod
    def _build_game(state: str, game_state: Any) -> Optional[dict]:
        """game is null unless we are actively gaming with a known game."""
        if state != "gaming" or game_state is None:
            return None
        if not getattr(game_state, "active", False):
            return None
        return {
            "app_id": int(getattr(game_state, "app_id", 0) or 0),
            "name": str(getattr(game_state, "game_name", "") or ""),
            "pid": int(getattr(game_state, "game_pid", 0) or 0),
        }

    @staticmethod
    def _proc_entry(proc: Any) -> dict:
        return {
            "name": str(getattr(proc, "name", "") or ""),
            "pid": int(getattr(proc, "pid", 0) or 0),
        }

    def _build_throttle(self, throttle_summary: Any) -> dict:
        """Accepts a ThrottleResult, a flat list of ThrottledProc, or None."""
        soft: list[Any] = []
        hard: list[Any] = []

        if throttle_summary is None:
            pass
        elif hasattr(throttle_summary, "soft") or hasattr(throttle_summary, "hard"):
            # ThrottleResult-like object.
            soft = list(getattr(throttle_summary, "soft", []) or [])
            hard = list(getattr(throttle_summary, "hard", []) or [])
        else:
            # Assume an iterable of ThrottledProc; split by .tier.
            try:
                for proc in throttle_summary:
                    if getattr(proc, "tier", "") == "hard":
                        hard.append(proc)
                    else:
                        soft.append(proc)
            except TypeError:
                pass

        soft_entries = [self._proc_entry(p) for p in soft]
        hard_entries = [self._proc_entry(p) for p in hard]
        return {
            "soft": soft_entries,
            "hard": hard_entries,
            "soft_count": len(soft_entries),
            "hard_count": len(hard_entries),
        }

    # --------------------------------------------------------------- transport

    def send_state_change(
        self,
        state: str,
        game_state: Any,
        throttle_summary: Any,
        benchmark: dict | None = None,
    ) -> bool:
        payload = self.build_payload(
            "state_change", state, game_state, throttle_summary, benchmark
        )
        return self._post(payload)

    def send_heartbeat(
        self,
        state: str,
        game_state: Any,
        throttle_summary: Any,
        benchmark: dict | None = None,
    ) -> bool:
        payload = self.build_payload(
            "heartbeat", state, game_state, throttle_summary, None
        )
        return self._post(payload)

    def _post(self, payload: dict) -> bool:
        """POST payload to the configured endpoint. True only on a 2xx response.

        Wrapped end-to-end in try/except: this method must NEVER raise, because
        a down endpoint must not crash the daemon. On any failure it logs the
        full payload with the "TELEMETRY-PAYLOAD" marker so the schema stays
        inspectable.
        """
        # Telemetry disabled (the default) or unconfigured: skip the network
        # entirely but still emit the payload at debug so it remains
        # inspectable, then report "not sent" via False.
        if not self.enabled or not self.endpoint:
            try:
                self.logger.debug(
                    "telemetry disabled -- TELEMETRY-PAYLOAD (not sent):\n%s",
                    json.dumps(payload, indent=2),
                )
            except Exception:
                pass
            return False

        try:
            body = json.dumps(payload).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            req = urllib.request.Request(
                self.endpoint, data=body, headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                status = getattr(resp, "status", None)
                if status is None:
                    status = resp.getcode()
                if 200 <= int(status) < 300:
                    self.logger.debug(
                        "telemetry POST ok (%s -> %s)", self.endpoint, status)
                    return True
                # Non-2xx (shouldn't normally reach here; urllib raises on >=400).
                raise RuntimeError(f"non-2xx response: {status}")
        except Exception as exc:
            try:
                self.logger.info(
                    "TELEMETRY ENDPOINT UNREACHABLE -- TELEMETRY-PAYLOAD that "
                    "WOULD have been sent to %s (%s):\n%s",
                    self.endpoint,
                    exc,
                    json.dumps(payload, indent=2),
                )
            except Exception:
                # Even logging must not be allowed to crash the daemon.
                pass
            return False
