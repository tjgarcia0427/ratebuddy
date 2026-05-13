"""Rate-limiting algorithms, implemented as atomic Redis Lua scripts.

Two algorithms are exposed:

- :class:`TokenBucket` — classic leaky-bucket-flipped-upside-down. Each key
  has a capacity and refill rate; a request consumes N tokens if available.
- :class:`SlidingWindow` — counts events in a rolling window. Useful when
  "no more than X requests per Y seconds" is the requirement and a burstable
  bucket is the wrong shape.

Both are implemented as Lua scripts run via ``EVAL`` so that the "read
state → decide → write state" sequence is a single atomic step on the
Redis side. Without that, two concurrent consumers can both observe the
same available token count and both consume it.

Returned by both: a :class:`Decision` describing whether the request was
allowed, how many tokens (or requests) remain, and how long until the
caller could retry on a deny.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from redis.asyncio import Redis

# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------
#
# Storage: one Redis hash per key with two fields:
#   tokens      : float — current token balance
#   timestamp   : float — last refill timestamp (seconds since epoch)
#
# Refill rule on every consume:
#   elapsed = now - timestamp
#   tokens  = min(capacity, tokens + elapsed * refill_per_second)
#   if tokens >= cost: tokens -= cost; allow
#   else:              deny, retry_after = (cost - tokens) / refill_per_second

_TOKEN_BUCKET_LUA = """
local key       = KEYS[1]
local now       = tonumber(ARGV[1])
local capacity  = tonumber(ARGV[2])
local refill    = tonumber(ARGV[3])  -- tokens per second
local cost      = tonumber(ARGV[4])
local ttl       = tonumber(ARGV[5])

local data = redis.call("HMGET", key, "tokens", "timestamp")
local tokens = tonumber(data[1])
local ts     = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    ts     = now
end

local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * refill)

local allowed = 0
local retry_after = 0
if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
else
    if refill > 0 then
        retry_after = (cost - tokens) / refill
    else
        retry_after = -1
    end
end

redis.call("HMSET", key, "tokens", tokens, "timestamp", now)
redis.call("EXPIRE", key, ttl)

return {allowed, tokens, retry_after}
"""


# ---------------------------------------------------------------------------
# Sliding window
# ---------------------------------------------------------------------------
#
# Storage: one Redis sorted-set per key. Each element score == request
# timestamp, member == a unique-per-request token (we use the timestamp
# itself plus a random suffix to avoid duplicate-member collisions).
#
# On every consume:
#   remove members with score < now - window  (drop expired)
#   if zcard < limit: add new member; allow
#   else:             deny, retry_after = (oldest_score + window) - now

_SLIDING_WINDOW_LUA = """
local key       = KEYS[1]
local now       = tonumber(ARGV[1])
local window    = tonumber(ARGV[2])  -- seconds
local limit     = tonumber(ARGV[3])
local member    = ARGV[4]
local ttl       = tonumber(ARGV[5])

-- evict expired
redis.call("ZREMRANGEBYSCORE", key, 0, now - window)

local count = redis.call("ZCARD", key)
local allowed = 0
local retry_after = 0
if count < limit then
    redis.call("ZADD", key, now, member)
    redis.call("EXPIRE", key, ttl)
    allowed = 1
    count = count + 1
else
    local oldest = redis.call("ZRANGE", key, 0, 0, "WITHSCORES")
    if oldest[2] then
        retry_after = (tonumber(oldest[2]) + window) - now
        if retry_after < 0 then
            retry_after = 0
        end
    end
end

return {allowed, count, retry_after}
"""


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """The outcome of one ``consume`` call."""

    allowed: bool
    remaining: float
    retry_after_seconds: float
    algorithm: str
    key: str


class TokenBucket:
    def __init__(self, redis: Redis, *, key_prefix: str = "rb:tb") -> None:
        self.redis = redis
        self.prefix = key_prefix

    async def consume(
        self,
        key: str,
        *,
        capacity: int,
        refill_per_second: float,
        cost: int = 1,
        ttl_seconds: int = 600,
        now: float | None = None,
    ) -> Decision:
        full_key = f"{self.prefix}:{key}"
        ts = now if now is not None else time.time()
        result = await self.redis.eval(
            _TOKEN_BUCKET_LUA,
            1,
            full_key,
            ts,
            capacity,
            refill_per_second,
            cost,
            ttl_seconds,
        )
        allowed, tokens_remaining, retry_after = result
        return Decision(
            allowed=bool(int(allowed)),
            remaining=float(tokens_remaining),
            retry_after_seconds=float(retry_after),
            algorithm="token_bucket",
            key=key,
        )

    async def state(self, key: str) -> dict[str, float]:
        full_key = f"{self.prefix}:{key}"
        data = await self.redis.hgetall(full_key)
        if not data:
            return {}
        decoded = {
            (k.decode() if isinstance(k, bytes) else k): float(
                v.decode() if isinstance(v, bytes) else v
            )
            for k, v in data.items()
        }
        return decoded


class SlidingWindow:
    def __init__(self, redis: Redis, *, key_prefix: str = "rb:sw") -> None:
        self.redis = redis
        self.prefix = key_prefix

    async def consume(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: float,
        ttl_seconds: int = 600,
        now: float | None = None,
        member: str | None = None,
    ) -> Decision:
        full_key = f"{self.prefix}:{key}"
        ts = now if now is not None else time.time()
        m = member or f"{ts:.6f}:{id(self)}"
        result = await self.redis.eval(
            _SLIDING_WINDOW_LUA,
            1,
            full_key,
            ts,
            window_seconds,
            limit,
            m,
            ttl_seconds,
        )
        allowed, count, retry_after = result
        remaining = max(0, limit - int(count))
        return Decision(
            allowed=bool(int(allowed)),
            remaining=float(remaining),
            retry_after_seconds=float(retry_after),
            algorithm="sliding_window",
            key=key,
        )

    async def state(self, key: str) -> dict[str, float]:
        full_key = f"{self.prefix}:{key}"
        count = await self.redis.zcard(full_key)
        return {"count": float(count)}
