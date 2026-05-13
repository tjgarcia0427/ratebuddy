"""End-to-end API tests via httpx.ASGITransport (no real HTTP server)."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_metrics_exposed(client):
    r = await client.get("/metrics")
    assert r.status_code == 200
    # prometheus_client default exposition format
    assert "ratebuddy_requests_total" in r.text or "TYPE" in r.text


@pytest.mark.asyncio
async def test_token_bucket_endpoint_allows(client):
    r = await client.post(
        "/v1/limits/token-bucket/alice/consume",
        json={"capacity": 3, "refill_per_second": 1.0, "cost": 1},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["allowed"] is True
    assert body["algorithm"] == "token_bucket"
    assert body["key"] == "alice"


@pytest.mark.asyncio
async def test_token_bucket_endpoint_denies_after_drain(client):
    payload = {"capacity": 2, "refill_per_second": 0.001, "cost": 1}
    # Two allowed
    for _ in range(2):
        r = await client.post("/v1/limits/token-bucket/drain/consume", json=payload)
        assert r.json()["allowed"] is True
    # Third denied (refill is essentially zero on this timescale)
    r = await client.post("/v1/limits/token-bucket/drain/consume", json=payload)
    assert r.json()["allowed"] is False
    assert r.json()["retry_after_seconds"] > 0


@pytest.mark.asyncio
async def test_sliding_window_endpoint(client):
    payload = {"limit": 2, "window_seconds": 60.0}
    r1 = await client.post("/v1/limits/sliding-window/bob/consume", json=payload)
    r2 = await client.post("/v1/limits/sliding-window/bob/consume", json=payload)
    r3 = await client.post("/v1/limits/sliding-window/bob/consume", json=payload)
    assert r1.json()["allowed"] is True
    assert r2.json()["allowed"] is True
    assert r3.json()["allowed"] is False


@pytest.mark.asyncio
async def test_inspect_endpoint(client):
    await client.post(
        "/v1/limits/token-bucket/insp/consume",
        json={"capacity": 5, "refill_per_second": 1.0},
    )
    r = await client.get("/v1/limits/token-bucket/insp")
    assert r.status_code == 200
    body = r.json()
    assert body["key"] == "insp"
    assert "tokens" in body


@pytest.mark.asyncio
async def test_inspect_unknown_algorithm_404(client):
    r = await client.get("/v1/limits/foo-bar/anything")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_validation_error_on_negative_capacity(client):
    r = await client.post(
        "/v1/limits/token-bucket/x/consume",
        json={"capacity": -1, "refill_per_second": 1.0},
    )
    assert r.status_code == 422  # pydantic validation


@pytest.mark.asyncio
async def test_defaults_apply_when_request_omits_values(client):
    # capacity=0 means "use service default" (60)
    r = await client.post(
        "/v1/limits/token-bucket/defaults/consume",
        json={"capacity": 0, "refill_per_second": 0.0, "cost": 1},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["allowed"] is True
    # remaining is capacity - cost = 60 - 1 = 59
    assert body["remaining"] == pytest.approx(59.0, abs=0.001)
