# ratebuddy

> Rate-limit microservice over Redis. Token bucket + sliding window,
> atomic Lua scripts, FastAPI surface, Prometheus metrics, one-command
> demo stack.

Rate-limiting is one of those quietly-load-bearing pieces of
infrastructure that you don't think about until two production teams
ship the same naive `if redis.incr(key) > 5: deny` and then spend a
week debugging why it lets through bursts that "should be" capped.
`ratebuddy` is the centralized service version: one HTTP API, two
well-known algorithms, atomic decisions in a single Redis round trip.

```bash
docker compose up -d
curl -X POST http://localhost:8000/v1/limits/token-bucket/alice/consume \
     -H 'content-type: application/json' \
     -d '{"capacity": 10, "refill_per_second": 1, "cost": 1}'
# → {"allowed": true, "remaining": 9.0, "retry_after_seconds": 0,
#    "algorithm": "token_bucket", "key": "alice"}
```

Grafana dashboard is provisioned automatically at `localhost:3000`.

## Algorithms

Two, picked because they cover ~90% of real-world rate-limit problems
between them. Both implemented as Redis Lua scripts so that "read state →
decide → write state" is a single atomic Redis operation. Without that,
two concurrent consumers can both observe the same available capacity
and both burn through it — a class of race that's hard to spot in
load-testing because it only fires under coincident-millisecond load.

### Token bucket
The right shape for "X requests per second, with bursts of up to Y."
Each key holds a `tokens` balance and a `last refill timestamp`. A
consume call:
1. Computes refill since the last touch: `min(capacity, tokens + elapsed * rate)`.
2. If enough tokens, decrement and allow; otherwise deny with a
   `retry_after_seconds` that says exactly how long until the
   requested cost is available.
3. Writes the new `tokens` + `timestamp` atomically.

Stored as a Redis hash (`HMSET`), so it's one round trip and one
`EXPIRE` to keep idle keys from leaking.

### Sliding window
The right shape for "no more than X requests per Y seconds, period."
Each key is a sorted set; members are unique-per-request, scores are
timestamps. A consume call:
1. `ZREMRANGEBYSCORE 0 (now - window)` evicts expired requests.
2. If `ZCARD < limit`, `ZADD` the new request and allow.
3. Otherwise read the oldest member's score; `retry_after = (oldest + window) - now`.

Sliding window costs a bit more storage (one member per request) but
gives exact per-window enforcement — token bucket can let a clever
attacker burst at the boundary of two adjacent buckets.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/limits/token-bucket/{key}/consume` | Consume `cost` tokens. |
| `POST` | `/v1/limits/sliding-window/{key}/consume` | Admit one event. |
| `GET`  | `/v1/limits/{algorithm}/{key}` | Inspect bucket state. |
| `GET`  | `/healthz` | Liveness (pings Redis). |
| `GET`  | `/metrics` | Prometheus scrape endpoint. |

Set `API_TOKEN=<secret>` in the environment to require
`Authorization: Bearer <secret>` on every `/v1/*` endpoint. `/healthz`
and `/metrics` stay unauthenticated by design — Prometheus scrapes
on the cluster network.

Full OpenAPI / Swagger UI is mounted at `http://localhost:8000/docs`.

## Run it

### Docker Compose (recommended for trying it out)

```bash
docker compose up -d
# wait ~5s for healthchecks…
open http://localhost:8000/docs        # API surface
open http://localhost:3000             # Grafana, anonymous-admin
open http://localhost:9090             # Prometheus
```

The stack: `redis` → `ratebuddy` (FastAPI app) → `prometheus` scrapes
ratebuddy → `grafana` reads prometheus + auto-provisions the
`ratebuddy` dashboard.

### Bare Python

```bash
pip install -e ".[dev]"
REDIS_URL=redis://localhost:6379/0 uvicorn ratebuddy.app:app --reload
```

## Configuration

All knobs are env-driven:

| Variable | Default | Purpose |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Where to connect. |
| `DEFAULT_CAPACITY` | `60` | Fallback for token-bucket capacity when the request omits it. |
| `DEFAULT_REFILL` | `1.0` | Fallback refill rate (tokens / sec). |
| `DEFAULT_WINDOW_SECONDS` | `60.0` | Fallback sliding-window size. |
| `DEFAULT_LIMIT` | `60` | Fallback sliding-window limit. |
| `TTL_SECONDS` | `600` | How long an idle key sits in Redis before expiry. |
| `METRICS_PATH` | `/metrics` | Where Prometheus scrapes. |
| `API_TOKEN` | _unset_ | If set, `/v1/*` requires Bearer auth. |

## Observability

The `/metrics` endpoint emits Prometheus metrics:

- `ratebuddy_requests_total{algorithm, decision}` — counter of every
  consume call. Labels stay coarse (no per-key cardinality blow-up).
- `ratebuddy_redis_errors_total` — Lua / connection errors.
- `ratebuddy_consume_latency_seconds` — histogram of end-to-end
  decision latency.

The provisioned Grafana dashboard surfaces:
- requests/sec, broken down by algorithm
- allowed-vs-denied rate
- p50 / p95 / p99 latency
- Redis errors over the last 5 min

## Tests

```bash
pip install -e ".[dev]"
pytest
```

21 tests across:
- Token-bucket: first-request, drain-and-deny, refill-over-time,
  capacity-cap, state-persistence.
- Sliding-window: admit-until-limit, evict-old-entries,
  retry-after-is-window-offset.
- API: each route's happy path + 422 validation, 404 unknown
  algorithm, 401 missing/wrong token, 200 right token.
- Metrics & healthz reachable.

Tests use `fakeredis[lua]` so they're hermetic — no real Redis, no
docker, sub-second wall time.

## What this is NOT

- A general-purpose Redis abstraction. Use a real Redis client.
- A distributed coordinator. Single-Redis instance assumption — if
  you need HA, run Redis Sentinel / Cluster and point `REDIS_URL` at
  the proxy.
- A library. ratebuddy is a service. If you want library-style
  in-process rate limiting, ``slowapi`` is solid.

## Roadmap

- Per-tenant defaults stored in Redis hash so capacity / refill don't
  have to ride on every request.
- Leaky-bucket and fixed-window algorithms (for completeness).
- gRPC surface alongside HTTP, for lower-latency in-cluster traffic.
- Optional in-process LRU cache for hot keys (read-side only).

PRs welcome — open an issue first for anything bigger than a small bug fix.

## License

MIT.
