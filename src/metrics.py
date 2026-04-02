"""Prometheus-compatible metrics for observability.

Tracks scan counts, alert counts, latency, and WebSocket connection status.
Exports in both Prometheus text format and JSON for the dashboard API.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any


class Metrics:
    """Simple in-memory metrics collector."""

    def __init__(self) -> None:
        self._scans_total: int = 0
        self._scans_by_source: dict[str, int] = defaultdict(int)
        self._alerts_sent: int = 0
        self._alerts_by_type: dict[str, int] = defaultdict(int)
        self._scan_duration_ms: list[float] = []
        self._score_histogram: dict[str, int] = defaultdict(int)
        self._ws_connected: dict[str, bool] = {}
        self._start_time: float = time.time()

    def record_scan(
        self, source: str, duration_ms: float, score: float
    ) -> None:
        """Record a completed scan."""
        self._scans_total += 1
        self._scans_by_source[source] += 1
        self._scan_duration_ms.append(duration_ms)
        # Keep last 1000 durations
        if len(self._scan_duration_ms) > 1000:
            self._scan_duration_ms = self._scan_duration_ms[-500:]

        # Score bucket
        if score >= 75:
            self._score_histogram["75-100"] += 1
        elif score >= 50:
            self._score_histogram["50-75"] += 1
        elif score >= 25:
            self._score_histogram["25-50"] += 1
        else:
            self._score_histogram["0-25"] += 1

    def record_alert(self, alert_type: str) -> None:
        """Record an alert sent."""
        self._alerts_sent += 1
        self._alerts_by_type[alert_type] += 1

    def set_ws_status(self, name: str, connected: bool) -> None:
        """Update WebSocket connection status."""
        self._ws_connected[name] = connected

    def export_prometheus(self) -> str:
        """Export metrics in Prometheus text exposition format."""
        lines = []

        lines.append(f"# HELP forensics_scans_total Total scans performed")
        lines.append(f"# TYPE forensics_scans_total counter")
        lines.append(f"forensics_scans_total {self._scans_total}")

        for source, count in self._scans_by_source.items():
            lines.append(f'forensics_scans_by_source{{source="{source}"}} {count}')

        lines.append(f"# HELP forensics_alerts_total Total alerts sent")
        lines.append(f"# TYPE forensics_alerts_total counter")
        lines.append(f"forensics_alerts_total {self._alerts_sent}")

        if self._scan_duration_ms:
            avg = sum(self._scan_duration_ms) / len(self._scan_duration_ms)
            lines.append(f"# HELP forensics_scan_duration_ms Average scan duration")
            lines.append(f"# TYPE forensics_scan_duration_ms gauge")
            lines.append(f"forensics_scan_duration_ms {avg:.1f}")

        uptime = time.time() - self._start_time
        lines.append(f"# HELP forensics_uptime_seconds Bot uptime")
        lines.append(f"# TYPE forensics_uptime_seconds gauge")
        lines.append(f"forensics_uptime_seconds {uptime:.0f}")

        return "\n".join(lines) + "\n"

    def export_json(self) -> dict[str, Any]:
        """Export metrics as JSON for the dashboard API."""
        avg_duration = (
            sum(self._scan_duration_ms) / len(self._scan_duration_ms)
            if self._scan_duration_ms
            else 0
        )
        return {
            "scans_total": self._scans_total,
            "scans_by_source": dict(self._scans_by_source),
            "alerts_total": self._alerts_sent,
            "alerts_by_type": dict(self._alerts_by_type),
            "avg_scan_duration_ms": round(avg_duration, 1),
            "score_distribution": dict(self._score_histogram),
            "ws_connections": dict(self._ws_connected),
            "uptime_seconds": int(time.time() - self._start_time),
        }


# Module-level singleton
metrics = Metrics()


def track_scan(source: str, duration_ms: float, score: float) -> None:
    """Convenience function to record a scan."""
    metrics.record_scan(source, duration_ms, score)


def track_alert_sent(alert_type: str) -> None:
    """Convenience function to record an alert."""
    metrics.record_alert(alert_type)


def set_ws_connected(name: str, connected: bool) -> None:
    """Convenience function to update WS status."""
    metrics.set_ws_status(name, connected)
