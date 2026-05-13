"""Prometheus metrics surface.

Three counters / one histogram are exposed:

- ``ratebuddy_requests_total{algorithm, decision}`` — every consume call,
  split by allowed/denied.
- ``ratebuddy_redis_errors_total`` — Lua / connection errors.
- ``ratebuddy_consume_latency_seconds`` — server-side latency, used to
  prove we're within the rate-limiting service's own SLO.

These are deliberately small. A real production deployment would add
per-key labels, but cardinality blows up fast — leave that to a
deployment-specific opt-in.
"""
from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest


def render() -> tuple[bytes, str]:
    """Return ``(payload, content_type)`` suitable for a /metrics route."""
    return generate_latest(), CONTENT_TYPE_LATEST

REQUESTS = Counter(
    "ratebuddy_requests_total",
    "Number of consume() calls, by algorithm and decision.",
    ["algorithm", "decision"],
)

REDIS_ERRORS = Counter(
    "ratebuddy_redis_errors_total",
    "Number of failed Redis operations (Lua errors, timeouts, connection drops).",
)

CONSUME_LATENCY = Histogram(
    "ratebuddy_consume_latency_seconds",
    "Wall-clock seconds spent in consume() from request start to response.",
    buckets=(0.001, 0.002, 0.005, 0.010, 0.020, 0.050, 0.100, 0.250, 0.500, 1.0),
)
