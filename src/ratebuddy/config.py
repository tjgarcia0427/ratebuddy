"""Runtime configuration — single source of truth, env-var driven."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    redis_url: str = "redis://localhost:6379/0"
    # Default per-key parameters; overridable per request.
    default_capacity: int = 60
    default_refill_per_second: float = 1.0
    default_window_seconds: float = 60.0
    default_limit: int = 60
    # How long a fully-recovered bucket sticks around in Redis.
    ttl_seconds: int = 600
    # Prometheus scrape path.
    metrics_path: str = "/metrics"
    # API token; if set, /v1/* endpoints require Authorization: Bearer <token>.
    api_token: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        def _int(name: str, default: int) -> int:
            return int(os.environ.get(name, default))

        def _float(name: str, default: float) -> float:
            return float(os.environ.get(name, default))

        return cls(
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            default_capacity=_int("DEFAULT_CAPACITY", 60),
            default_refill_per_second=_float("DEFAULT_REFILL", 1.0),
            default_window_seconds=_float("DEFAULT_WINDOW_SECONDS", 60.0),
            default_limit=_int("DEFAULT_LIMIT", 60),
            ttl_seconds=_int("TTL_SECONDS", 600),
            metrics_path=os.environ.get("METRICS_PATH", "/metrics"),
            api_token=os.environ.get("API_TOKEN"),
        )
