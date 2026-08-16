# app/auth/clerk_auth.py
import time
import jwt
import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from app.config import settings

# How long a fetched JWKS is trusted before we refresh on a normal (non-rotation) basis.
CACHE_TTL_SECONDS = 3600  # 1 hour is plenty; the kid-miss check below handles rotations faster than this


class UserSession(BaseModel):
    user_id: str
    email: str


security = HTTPBearer()

# In-memory JWKS cache
_jwks_cache: dict | None = None
_jwks_last_fetched: float = 0


async def fetch_jwks_from_network() -> dict:
    """Fetches the latest JWKS from Clerk with proper error handling."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(settings.CLERK_JWKS_URL, timeout=5.0)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service temporarily unavailable.",
        ) from exc


async def get_jwks(kid: str | None = None) -> dict:
    """
    Retrieves JWKS from cache or network.
    If a specific 'kid' is requested but missing, it forces a refresh
    to seamlessly handle Clerk key rotations.
    """
    global _jwks_cache, _jwks_last_fetched
    now = time.time()
    is_expired = (now - _jwks_last_fetched) > CACHE_TTL_SECONDS

    # 1. Check if we need a fresh copy due to expiration or empty cache
    if _jwks_cache is None or is_expired:
        _jwks_cache = await fetch_jwks_from_network()
        _jwks_last_fetched = now
        return _jwks_cache

    # 2. Key rotation check: if a specific key ID isn't found, try a single refresh
    if kid:
        cached_kids = {key.get("kid") for key in _jwks_cache.get("keys", [])}
        if kid not in cached_kids:
            _jwks_cache = await fetch_jwks_from_network()
            _jwks_last_fetched = now

            # Double check if it exists now; if not, the token is genuinely invalid
            cached_kids = {key.get("kid") for key in _jwks_cache.get("keys", [])}
            if kid not in cached_kids:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid authentication token (unknown key ID).",
                )

    return _jwks_cache


async def get_current_user(cred: HTTPAuthorizationCredentials = Depends(security)) -> UserSession:
    token = cred.credentials
    try:
        # 1. Inspect unverified headers to identify which key ID ("kid") was used to sign
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        # 2. Match token "kid" against our cached public keys (awaited, kid passed through
        #    so a rotated key triggers the refresh-once path above)
        jwks = await get_jwks(kid)
        public_key = None
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key)
                break

        if not public_key:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token signature key.")

        # 3. Decode & cryptographically verify signature, checking expiry and source
        payload = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=settings.CLERK_AUDIENCE,
            options={"verify_iat": True},
        )

        return UserSession(
            user_id=payload.get("sub"),  # "sub" matches Clerk's external user_id payload
            email=payload.get("email") or "",
        )

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token signature expired.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authorization token.")