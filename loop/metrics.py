"""Prometheus /metrics endpoint for hermes-loop-r2 (REA-127).

Exposes pass success rate, duration, queue depth, and daemon uptime in
the Prometheus exposition format. The caller passes a snapshot dict
(produced by `SelfHealer.snapshot()`) and this module formats it as
``text/plain; version=0.0.4`` lines, one metric per line with HELP and
TYPE metadata.

Pure stdlib — no external dependencies beyond what the daemon already
uses.
"""

from __future__ import annotations

from typing import Any, Callable, Dict

# Type for a callback that returns a Prometheus-format byte string.
MetricsProvider = Callable[[], bytes]

# Internal registry of metrics the daemon tracks.
_METRICS = [
    {
        "name": "loop_uptime_seconds",
        "type": "gauge",
        "help": "Seconds since the daemon started.",
        "key": "uptime_seconds",
        "fallback": 0.0,
    },
    {
        "name": "loop_passes_completed_total",
        "type": "counter",
        "help": "Total number of completed passes.",
        "key": "passes_completed",
        "fallback": 0,
    },
    {
        "name": "loop_passes_failed_total",
        "type": "counter",
        "help": "Total number of failed passes.",
        "key": "passes_failed",
        "fallback": 0,
    },
    {
        "name": "loop_last_pass_duration_seconds",
        "type": "gauge",
        "help": "Duration of the most recent pass in seconds.",
        "key": "last_pass_duration",
        "fallback": 0.0,
    },
    {
        "name": "loop_queue_depth",
        "type": "gauge",
        "help": "Number of issues currently in the ready queue.",
        "key": "queue_depth",
        "fallback": 0,
    },
]


def format_prometheus(snapshot: Dict[str, Any]) -> bytes:
    """Render a ``SelfHealer.snapshot()`` dict as Prometheus exposition text.

    Metrics exposed:

    * ``loop_uptime_seconds``       — daemon uptime (gauge)
    * ``loop_passes_completed_total`` — cumulative completed passes (counter)
    * ``loop_passes_failed_total``    — cumulative failed passes (counter)
    * ``loop_last_pass_duration_seconds`` — last pass wall-clock time (gauge)
    * ``loop_queue_depth``           — ready-queue depth (gauge)

    ``loop_passes_completed_total`` + ``loop_passes_failed_total`` give
    the total pass count; dividing completed / total yields the pass
    success rate for dashboard queries.

    Returns a UTF-8 byte string ready to send as ``text/plain``.
    """
    lines: list[str] = []
    for m in _METRICS:
        lines.append(f"# HELP {m['name']} {m['help']}")
        lines.append(f"# TYPE {m['name']} {m['type']}")
        value = snapshot.get(m["key"])
        if value is None:
            value = m["fallback"]
        lines.append(f"{m['name']} {value}")
    # Prometheus exposition format wants a trailing newline.
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def make_metrics_provider(snapshot_fn: Callable[[], Dict[str, Any]]) -> MetricsProvider:
    """Wrap a snapshot callable so it returns Prometheus bytes.

    ``snapshot_fn`` is typically ``healer.snapshot``, bound to the
    process-lifetime ``SelfHealer`` instance.
    """
    def _provider() -> bytes:
        return format_prometheus(snapshot_fn())
    return _provider