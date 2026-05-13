"""Shared pytest fixtures.

We wire ``fakeredis.aioredis.FakeRedis`` into the app via FastAPI's
dependency-injection-by-state pattern, so the tests never need a real
Redis. fakeredis supports Lua EVAL, ZADD/ZRANGE, hashes, and EXPIRE —
everything our algorithms.py uses.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from ratebuddy.algorithms import SlidingWindow, TokenBucket
from ratebuddy.app import create_app
from ratebuddy.config import Settings


@pytest_asyncio.fixture
async def fake_redis():
    r = FakeRedis(decode_responses=False)
    try:
        yield r
    finally:
        await r.aclose()


@pytest_asyncio.fixture
async def token_bucket(fake_redis):
    return TokenBucket(fake_redis, key_prefix="test:tb")


@pytest_asyncio.fixture
async def sliding_window(fake_redis):
    return SlidingWindow(fake_redis, key_prefix="test:sw")


@pytest_asyncio.fixture
async def client(fake_redis):
    """Wire the API up against the fake redis. Skips the real lifespan."""
    app = create_app(Settings(redis_url="redis://unused", ttl_seconds=60))
    # Manually populate app.state so we don't run the real lifespan
    app.state.redis = fake_redis
    app.state.token_bucket = TokenBucket(fake_redis)
    app.state.sliding_window = SlidingWindow(fake_redis)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
