"""FastAPI surface for ratebuddy.

Endpoints
---------
- ``POST /v1/limits/token-bucket/{key}/consume``  — consume N tokens.
- ``POST /v1/limits/sliding-window/{key}/consume`` — admit one event.
- ``GET  /v1/limits/{algorithm}/{key}``           — inspect current state.
- ``GET  /healthz``                               — liveness.
- ``GET  /metrics``                               — Prometheus scrape.

Auth: if ``API_TOKEN`` is set in the environment, every ``/v1`` endpoint
requires ``Authorization: Bearer <token>``.
"""
from __future__ import annotations

import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from . import __version__, metrics
from .algorithms import Decision, SlidingWindow, TokenBucket
from .config import Settings


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    redis = Redis.from_url(settings.redis_url, encoding="utf-8", decode_responses=False)
    app.state.redis = redis
    app.state.token_bucket = TokenBucket(redis)
    app.state.sliding_window = SlidingWindow(redis)
    try:
        yield
    finally:
        await redis.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app. Pass an explicit ``Settings`` for tests."""
    cfg = settings or Settings.from_env()
    app = FastAPI(
        title="ratebuddy",
        version=__version__,
        summary="Rate-limit microservice over Redis (token bucket + sliding window).",
        lifespan=lifespan,
    )
    app.state.settings = cfg
    _register_routes(app)
    return app


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class TokenBucketRequest(BaseModel):
    capacity: int = Field(default=0, ge=0, description="Bucket capacity. 0 → use service default.")
    refill_per_second: float = Field(default=0.0, ge=0.0)
    cost: int = Field(default=1, ge=1)


class SlidingWindowRequest(BaseModel):
    limit: int = Field(default=0, ge=0)
    window_seconds: float = Field(default=0.0, ge=0.0)


class DecisionResponse(BaseModel):
    allowed: bool
    remaining: float
    retry_after_seconds: float
    algorithm: str
    key: str

    @classmethod
    def from_decision(cls, d: Decision) -> "DecisionResponse":
        return cls(
            allowed=d.allowed,
            remaining=d.remaining,
            retry_after_seconds=d.retry_after_seconds,
            algorithm=d.algorithm,
            key=d.key,
        )


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


def _require_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = request.app.state.settings.api_token
    if expected is None:
        return  # auth disabled
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bearer token required")
    presented = authorization.removeprefix("Bearer ").strip()
    # Constant-time compare to avoid token-length leakage via timing.
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid bearer token")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _register_routes(app: FastAPI) -> None:
    cfg: Settings = app.state.settings

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        try:
            pong = await app.state.redis.ping()
            return {"status": "ok", "redis": "ok" if pong else "down"}
        except RedisError:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "redis unreachable")

    @app.get(cfg.metrics_path, tags=["meta"], include_in_schema=False)
    async def metrics_endpoint() -> Response:
        payload, content_type = metrics.render()
        return Response(content=payload, media_type=content_type)

    @app.post(
        "/v1/limits/token-bucket/{key}/consume",
        response_model=DecisionResponse,
        tags=["limits"],
        dependencies=[Depends(_require_token)],
    )
    async def consume_token_bucket(key: str, body: TokenBucketRequest) -> DecisionResponse:
        capacity = body.capacity or cfg.default_capacity
        refill = body.refill_per_second or cfg.default_refill_per_second
        cost = body.cost
        start = time.perf_counter()
        try:
            decision = await app.state.token_bucket.consume(
                key,
                capacity=capacity,
                refill_per_second=refill,
                cost=cost,
                ttl_seconds=cfg.ttl_seconds,
            )
        except RedisError as exc:
            metrics.REDIS_ERRORS.inc()
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"redis error: {exc}")
        finally:
            metrics.CONSUME_LATENCY.observe(time.perf_counter() - start)
        metrics.REQUESTS.labels(
            algorithm="token_bucket",
            decision="allowed" if decision.allowed else "denied",
        ).inc()
        return DecisionResponse.from_decision(decision)

    @app.post(
        "/v1/limits/sliding-window/{key}/consume",
        response_model=DecisionResponse,
        tags=["limits"],
        dependencies=[Depends(_require_token)],
    )
    async def consume_sliding_window(key: str, body: SlidingWindowRequest) -> DecisionResponse:
        limit = body.limit or cfg.default_limit
        window = body.window_seconds or cfg.default_window_seconds
        start = time.perf_counter()
        try:
            decision = await app.state.sliding_window.consume(
                key,
                limit=limit,
                window_seconds=window,
                ttl_seconds=cfg.ttl_seconds,
            )
        except RedisError as exc:
            metrics.REDIS_ERRORS.inc()
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"redis error: {exc}")
        finally:
            metrics.CONSUME_LATENCY.observe(time.perf_counter() - start)
        metrics.REQUESTS.labels(
            algorithm="sliding_window",
            decision="allowed" if decision.allowed else "denied",
        ).inc()
        return DecisionResponse.from_decision(decision)

    @app.get(
        "/v1/limits/{algorithm}/{key}",
        tags=["limits"],
        dependencies=[Depends(_require_token)],
    )
    async def inspect(algorithm: str, key: str) -> dict[str, float | str]:
        if algorithm == "token-bucket":
            state = await app.state.token_bucket.state(key)
        elif algorithm == "sliding-window":
            state = await app.state.sliding_window.state(key)
        else:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown algorithm {algorithm}")
        return {"algorithm": algorithm, "key": key, **state}


# Entry point for uvicorn / gunicorn.
app = create_app()
