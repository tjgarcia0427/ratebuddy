"""Algorithm-level tests against fakeredis.

These bypass the HTTP layer entirely so the algorithms can be reasoned
about in isolation.
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_token_bucket_first_request_allowed(token_bucket):
    decision = await token_bucket.consume(
        "alice", capacity=5, refill_per_second=1.0, cost=1, now=1000.0
    )
    assert decision.allowed is True
    assert decision.remaining == pytest.approx(4.0)
    assert decision.retry_after_seconds == 0
    assert decision.algorithm == "token_bucket"


@pytest.mark.asyncio
async def test_token_bucket_drains_then_denies(token_bucket):
    # 3 of 3 tokens consumed back-to-back at the same instant.
    for _ in range(3):
        d = await token_bucket.consume(
            "drain", capacity=3, refill_per_second=1.0, cost=1, now=2000.0
        )
        assert d.allowed
    d = await token_bucket.consume(
        "drain", capacity=3, refill_per_second=1.0, cost=1, now=2000.0
    )
    assert d.allowed is False
    assert d.remaining == pytest.approx(0.0)
    assert d.retry_after_seconds == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_token_bucket_refills_over_time(token_bucket):
    # Drain a 2-token bucket at t=0
    for _ in range(2):
        await token_bucket.consume("ref", capacity=2, refill_per_second=1.0, now=0.0)
    # Wait one virtual second → one token regenerated.
    d = await token_bucket.consume("ref", capacity=2, refill_per_second=1.0, now=1.0)
    assert d.allowed is True
    assert d.remaining == pytest.approx(0.0, abs=1e-6)


@pytest.mark.asyncio
async def test_token_bucket_caps_at_capacity(token_bucket):
    # Idle bucket for a long time should not exceed capacity.
    await token_bucket.consume("cap", capacity=2, refill_per_second=1.0, now=0.0)
    d = await token_bucket.consume("cap", capacity=2, refill_per_second=1.0, now=10_000.0)
    # We consumed 1 token after a huge idle → remaining should be capacity-1 = 1
    assert d.remaining == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_token_bucket_state_persists(token_bucket):
    await token_bucket.consume("state", capacity=5, refill_per_second=1.0, now=500.0)
    state = await token_bucket.state("state")
    assert "tokens" in state
    assert "timestamp" in state
    assert state["timestamp"] == pytest.approx(500.0)


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sliding_window_admits_until_limit(sliding_window):
    # limit=3 per 60s window
    for i in range(3):
        d = await sliding_window.consume(
            "swin", limit=3, window_seconds=60, now=1000.0 + i, member=f"m{i}"
        )
        assert d.allowed
    # 4th call inside the window → denied
    d = await sliding_window.consume(
        "swin", limit=3, window_seconds=60, now=1003.0, member="m4"
    )
    assert d.allowed is False
    assert d.remaining == 0


@pytest.mark.asyncio
async def test_sliding_window_expires_old_entries(sliding_window):
    # Fill the window at t=0
    for i in range(3):
        await sliding_window.consume(
            "exp", limit=3, window_seconds=10, now=float(i), member=f"m{i}"
        )
    # Jump past the window — all 3 expired.
    d = await sliding_window.consume(
        "exp", limit=3, window_seconds=10, now=100.0, member="fresh"
    )
    assert d.allowed
    assert d.remaining == 2


@pytest.mark.asyncio
async def test_sliding_window_retry_after_is_window_offset(sliding_window):
    # Fill at t=0 (limit=2, window=60)
    await sliding_window.consume(
        "ret", limit=2, window_seconds=60, now=0.0, member="a"
    )
    await sliding_window.consume(
        "ret", limit=2, window_seconds=60, now=10.0, member="b"
    )
    # Now at t=20 → denied; oldest member is at t=0, expires at t=60.
    d = await sliding_window.consume(
        "ret", limit=2, window_seconds=60, now=20.0, member="c"
    )
    assert d.allowed is False
    assert d.retry_after_seconds == pytest.approx(40.0)
