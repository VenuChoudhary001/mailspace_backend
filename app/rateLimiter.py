import time
from collections import defaultdict
from dataclasses import dataclass
from fastapi import Depends, HTTPException, status
from app.services.auth import UserSession, get_current_user


@dataclass
class TokenBucket:
    tokens: float
    last_update: float


class InMemoryUserRateLimiter:
    def __init__(self, requests_per_minute: int = 10, burst: int = 5):
        self.rate = requests_per_minute / 60.0  # Tokens per second
        self.capacity = burst
        # Stores user_id -> TokenBucket mapping
        self._buckets = self._new_bucket_store()

    def _new_bucket_store(self):
        """Factory for a fresh defaultdict, so cleanup can rebuild without losing the default factory."""
        return defaultdict(lambda: TokenBucket(tokens=float(self.capacity), last_update=time.time()))

    async def __call__(self, user: UserSession = Depends(get_current_user)):
        user_id = user.user_id
        now = time.time()
        bucket = self._buckets[user_id]

        # Calculate leaked tokens based on time delta
        elapsed = now - bucket.last_update
        bucket.tokens = min(self.capacity, bucket.tokens + (elapsed * self.rate))
        bucket.last_update = now

        # Check if user has available capacity
        if bucket.tokens < 1.0:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. You are modifying infrastructure resources too quickly."
            )

        bucket.tokens -= 1.0

        # Periodic cleanup memory optimization (keeps memory footprint low)
        if len(self._buckets) > 5000:
            # Drop buckets that haven't been active in over an hour.
            # NOTE: rebuild as a defaultdict (not a plain dict) so new user_ids
            # occurring after cleanup still get an initial bucket auto-created,
            # rather than raising KeyError.
            cutoff = now - 3600
            fresh_store = self._new_bucket_store()
            fresh_store.update(
                {k: v for k, v in self._buckets.items() if v.last_update > cutoff}
            )
            self._buckets = fresh_store


# Instantiate distinct limits for sensitive actions
infra_onboard_limiter = InMemoryUserRateLimiter(requests_per_minute=3, burst=3)
infra_rule_limiter = InMemoryUserRateLimiter(requests_per_minute=15, burst=10)