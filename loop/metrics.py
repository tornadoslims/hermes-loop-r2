"""Prometheus /metrics endpoint for hermes-loop-r2 (REA-113).

Exposes pass success rate, duration histogram, queue depth, and average
queue wait time in the Prometheus exposition format. The caller passes a
snapshot dict (produced by ``SelfHealer.snapshot()``) and this module
formats it as ``text/plain; version=0.0.4`` lines, one metric per line
with HELP and TYPE metadata.

AC-1: ``/metrics`` HTTP endpoint returns Prometheus text-exposition format.
AC-2: Exposes counters: ``passes_total{outcome=\"success|failure\"}``.
AC-3: Exposes a histogram for pass duration in seconds.
AC-4: Exposes a gauge for current ready-queue depth and average queue
      wait time for the last N completed passes.

Pure stdlib — no external dependencies beyond what the daemon already
uses.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# Type for a callback that returns a Prometheus-format byte string.
MetricsProvider = Callable[[], bytes]

# Histogram bucket boundaries for pass durations (seconds).
# Covers typical pass lengths: sub-minute, 1-2m, 2-5m, 5-10m,
# 10-15m, 15-30m, 30m-1h, and overflow.
_PASS_DURATION_BUCKETS = [30.0, 60.0, 120.0, 300.0, 600.0, 900.0, 1800.0, 3600.0]


def _render_labeled(name: str, value: Any, labels: Dict[str, str]) -> str:
    """Render a Prometheus metric line with labels."""
    label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return f"{name}{{{label_str}}} {value}"


def format_prometheus(snapshot: Dict[str, Any]) -> bytes:
    """Render a ``SelfHealer.snapshot()`` dict as Prometheus exposition text.

    Metrics exposed (REA-113):

    * ``loop_uptime_seconds``              — daemon uptime (gauge)
    * ``loop_passes_total``                — cumulative passes, labeled by outcome (counter)
      ``{outcome="success"}`` / ``{outcome="failure"}``
    * ``loop_pass_duration_seconds``       — pass wall-clock duration (histogram)
      with ``_bucket``, ``_sum``, ``_count``
    * ``loop_queue_depth``                 — ready-queue depth (gauge)
    * ``loop_queue_wait_seconds_avg``      — avg queue wait time over recent passes (gauge)

    Returns a UTF-8 byte string ready to send as ``text/plain``.
    """
    lines: list[str] = []

    # --- uptime gauge ---
    lines.append("# HELP loop_uptime_seconds Seconds since the daemon started.")
    lines.append("# TYPE loop_uptime_seconds gauge")
    uptime = snapshot.get("uptime_seconds", 0.0)
    lines.append(f"loop_uptime_seconds {uptime}")

    # --- passes_total counter (labeled) ---
    lines.append("# HELP loop_passes_total Total number of completed passes.")
    lines.append("# TYPE loop_passes_total counter")
    completed = snapshot.get("passes_completed", 0)
    failed = snapshot.get("passes_failed", 0)
    lines.append(_render_labeled("loop_passes_total", completed, {"outcome": "success"}))
    lines.append(_render_labeled("loop_passes_total", failed, {"outcome": "failure"}))

    # --- pass_duration histogram ---
    lines.append("# HELP loop_pass_duration_seconds Wall-clock duration of completed passes.")
    lines.append("# TYPE loop_pass_duration_seconds histogram")
    duration_samples: List[float] = snapshot.get("pass_duration_samples", [])
    _sum = sum(duration_samples)
    _count = len(duration_samples)
    for bucket in _PASS_DURATION_BUCKETS:
        bucket_count = sum(1 for d in duration_samples if d <= bucket)
        lines.append(
            _render_labeled(
                "loop_pass_duration_seconds_bucket",
                bucket_count,
                {"le": str(bucket)},
            )
        )
    # +Inf bucket
    lines.append(
        _render_labeled(
            "loop_pass_duration_seconds_bucket",
            _count,
            {"le": "+Inf"},
        )
    )
    lines.append(f"loop_pass_duration_seconds_sum {_sum}")
    lines.append(f"loop_pass_duration_seconds_count {_count}")

    # --- queue_depth gauge ---
    lines.append("# HELP loop_queue_depth Number of issues currently in the ready queue.")
    lines.append("# TYPE loop_queue_depth gauge")
    queue_depth = snapshot.get("queue_depth")
    if queue_depth is None:
        queue_depth = 0
    lines.append(f"loop_queue_depth {queue_depth}")

    # --- queue_wait average gauge ---
    lines.append("# HELP loop_queue_wait_seconds_avg Average time issues spend in the ready queue before being claimed.")
    lines.append("# TYPE loop_queue_wait_seconds_avg gauge")
    wait_avg = snapshot.get("queue_wait_avg")
    if wait_avg is None:
        wait_avg = 0.0
    lines.append(f"loop_queue_wait_seconds_avg {wait_avg}")

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