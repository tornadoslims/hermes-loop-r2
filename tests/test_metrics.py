"""Tests for loop.metrics — Prometheus exposition formatting (REA-113)."""
from __future__ import annotations

import pytest

from loop.metrics import (
    format_prometheus,
    make_metrics_provider,
)


class TestFormatPrometheus:
    """AC-1: /metrics returns valid Prometheus exposition format."""

    def test_empty_snapshot_produces_valid_prometheus(self):
        """Empty snapshot should produce valid Prometheus format with defaults."""
        output = format_prometheus({}).decode("utf-8")

        # Must end with trailing newline (Prometheus convention).
        assert output.endswith("\n")

        # Must contain HELP/TYPE lines for every metric.
        assert "# HELP loop_uptime_seconds" in output
        assert "# TYPE loop_uptime_seconds gauge" in output
        assert "# HELP loop_passes_total" in output
        assert "# TYPE loop_passes_total counter" in output
        assert "# HELP loop_pass_duration_seconds" in output
        assert "# TYPE loop_pass_duration_seconds histogram" in output
        assert "# HELP loop_queue_depth" in output
        assert "# TYPE loop_queue_depth gauge" in output
        assert "# HELP loop_queue_wait_seconds_avg" in output
        assert "# TYPE loop_queue_wait_seconds_avg gauge" in output

    def test_returns_bytes(self):
        """format_prometheus must return bytes, not str."""
        output = format_prometheus({})
        assert isinstance(output, bytes)


class TestPassesTotalCounter:
    """AC-2: passes_total{outcome="success|failure"}."""

    def test_labeled_counter_format(self):
        output = format_prometheus({
            "passes_completed": 5,
            "passes_failed": 2,
        }).decode("utf-8")

        # Must be a single counter metric with outcome label.
        assert 'loop_passes_total{outcome="success"} 5' in output
        assert 'loop_passes_total{outcome="failure"} 2' in output

        # Must NOT have the old separate counter names.
        assert "loop_passes_completed_total" not in output
        assert "loop_passes_failed_total" not in output

    def test_zero_values(self):
        output = format_prometheus({
            "passes_completed": 0,
            "passes_failed": 0,
        }).decode("utf-8")
        assert 'loop_passes_total{outcome="success"} 0' in output
        assert 'loop_passes_total{outcome="failure"} 0' in output


class TestPassDurationHistogram:
    """AC-3: histogram for pass duration in seconds."""

    def test_empty_samples_produces_valid_histogram(self):
        output = format_prometheus({
            "pass_duration_samples": [],
        }).decode("utf-8")

        assert "loop_pass_duration_seconds_sum 0" in output
        assert "loop_pass_duration_seconds_count 0" in output
        assert 'loop_pass_duration_seconds_bucket{le="+Inf"} 0' in output

    def test_histogram_buckets(self):
        samples = [45.0, 120.0, 600.0, 3600.0, 7200.0]
        output = format_prometheus({
            "pass_duration_samples": samples,
        }).decode("utf-8")

        # 45.0 falls into le=60 (first bucket)
        assert 'loop_pass_duration_seconds_bucket{le="30.0"} 0' in output
        assert 'loop_pass_duration_seconds_bucket{le="60.0"} 1' in output
        # 45.0 + 120.0 fall into le=120
        assert 'loop_pass_duration_seconds_bucket{le="120.0"} 2' in output
        # all 5 fall into +Inf
        assert 'loop_pass_duration_seconds_bucket{le="+Inf"} 5' in output

        # Sum and count
        expected_sum = sum(samples)
        assert f"loop_pass_duration_seconds_sum {expected_sum}" in output
        assert f"loop_pass_duration_seconds_count {len(samples)}" in output


class TestQueueMetrics:
    """AC-4: queue_depth gauge + queue_wait average."""

    def test_queue_depth_gauge(self):
        output = format_prometheus({"queue_depth": 7}).decode("utf-8")
        assert "loop_queue_depth 7" in output

    def test_queue_depth_none_treated_as_zero(self):
        output = format_prometheus({"queue_depth": None}).decode("utf-8")
        assert "loop_queue_depth 0" in output

    def test_queue_wait_avg_present(self):
        output = format_prometheus({"queue_wait_avg": 123.5}).decode("utf-8")
        assert "loop_queue_wait_seconds_avg 123.5" in output

    def test_queue_wait_avg_none_treated_as_zero(self):
        output = format_prometheus({"queue_wait_avg": None}).decode("utf-8")
        assert "loop_queue_wait_seconds_avg 0.0" in output


class TestFullSnapshot:
    """Integration: a complete snapshot produces valid, parseable Prometheus."""

    def test_full_snapshot(self):
        output = format_prometheus({
            "uptime_seconds": 3600.0,
            "passes_completed": 10,
            "passes_failed": 3,
            "pass_duration_samples": [120.0, 300.0, 45.0, 900.0],
            "queue_depth": 4,
            "queue_wait_avg": 250.0,
        }).decode("utf-8")

        # Parseable as Prometheus exposition (basic structural check).
        lines = [l for l in output.strip().split("\n") if l and not l.startswith("#")]
        metric_names = set()
        for line in lines:
            name = line.split("{")[0].split(" ")[0]
            metric_names.add(name)

        assert "loop_uptime_seconds" in metric_names
        assert "loop_passes_total" in metric_names
        assert "loop_pass_duration_seconds_bucket" in metric_names
        assert "loop_pass_duration_seconds_sum" in metric_names
        assert "loop_pass_duration_seconds_count" in metric_names
        assert "loop_queue_depth" in metric_names
        assert "loop_queue_wait_seconds_avg" in metric_names


class TestMakeMetricsProvider:
    """make_metrics_provider wraps a snapshot callable."""

    def test_provider_returns_bytes(self):
        snapshot = {"passes_completed": 1, "passes_failed": 0}
        provider = make_metrics_provider(lambda: snapshot)
        result = provider()
        assert isinstance(result, bytes)
        assert b"loop_passes_total" in result

    def test_provider_calls_snapshot_each_time(self):
        """Each call to the provider should re-invoke the snapshot function
        (live data, not cached)."""
        calls = []

        def snapshot():
            calls.append(1)
            return {"passes_completed": 1, "passes_failed": 0}

        provider = make_metrics_provider(snapshot)
        provider()
        provider()
        assert len(calls) == 2