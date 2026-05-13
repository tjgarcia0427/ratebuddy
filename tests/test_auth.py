"""Bearer-token auth gate (off by default, enabled when API_TOKEN is set)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from ratebuddy.algorithms import SlidingWindow, TokenBucket
from ratebuddy.app import create_app
from ratebuddy.config import Settings


@pytest_asyncio.fixture
async def authed_client():
    cfg = Settings(redis_url="redis://unused", api_token="sekret123")
    app = create_app(cfg)
    redis = FakeRedis(decode_responses=False)
    app.state.redis = redis
    app.state.token_bucket = TokenBucket(redis)
    app.state.sliding_window = SlidingWindow(redis)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_missing_token_rejected(authed_client):
    r = await authed_client.post(
        "/v1/limits/token-bucket/x/consume",
        json={"capacity": 5, "refill_per_second": 1.0},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_wrong_token_rejected(authed_client):
    r = await authed_client.post(
        "/v1/limits/token-bucket/x/consume",
        json={"capacity": 5, "refill_per_second": 1.0},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_right_token_passes(authed_client):
    r = await authed_client.post(
        "/v1/limits/token-bucket/x/consume",
        json={"capacity": 5, "refill_per_second": 1.0},
        headers={"Authorization": "Bearer sekret123"},
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_metrics_does_not_require_token(authed_client):
    # /metrics is unauthenticated by design — Prometheus scrapes it on-network.
    r = await authed_client.get("/metrics")
    assert r.status_code == 200
