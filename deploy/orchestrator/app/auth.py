"""Identity extraction.

Production: verify Cloudflare Access's `Cf-Access-Jwt-Assertion` JWT against the
team JWKS and read the `email` claim — this is the authN Lursor itself lacks, so
it must not be bypassable. Local dev: an explicit, loudly-flagged escape hatch
trusts a header so the stack can be exercised without Cloudflare in front.
"""

from __future__ import annotations

import hashlib
import logging

import jwt
from jwt import PyJWKClient

from .config import settings

log = logging.getLogger("orchestrator.auth")


class AuthError(Exception):
    """Raised when the caller's identity cannot be established."""


_jwks_client: PyJWKClient | None = None


def _jwks() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        url = f"https://{settings.access_team_domain}/cdn-cgi/access/certs"
        # PyJWKClient caches keys and refreshes on unknown kid.
        _jwks_client = PyJWKClient(url)
    return _jwks_client


def email_from_request(headers) -> str:
    """Return the authenticated developer email, or raise AuthError.

    `headers` is any case-insensitive mapping (Starlette Headers).
    """
    if settings.dev_trust_header:
        email = headers.get("cf-access-authenticated-user-email") or headers.get("x-dev-email")
        if not email:
            raise AuthError("dev-trust mode: no Cf-Access-Authenticated-User-Email / X-Dev-Email header")
        log.warning("DEV-TRUST identity (unverified): %s", email)
        return email.strip().lower()

    if not settings.access_team_domain or not settings.access_aud:
        raise AuthError("Cloudflare Access not configured (ORCH_ACCESS_TEAM_DOMAIN / ORCH_ACCESS_AUD)")

    token = headers.get("cf-access-jwt-assertion")
    if not token:
        raise AuthError("missing Cf-Access-Jwt-Assertion header (request did not pass Cloudflare Access)")

    try:
        signing_key = _jwks().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.access_aud,
            issuer=f"https://{settings.access_team_domain}",
        )
    except Exception as exc:  # noqa: BLE001 — any verification failure denies
        raise AuthError(f"Access JWT verification failed: {exc}") from exc

    email = claims.get("email")
    if not email:
        raise AuthError("Access JWT has no email claim")
    return str(email).strip().lower()


def instance_id_for(email: str) -> str:
    """Deterministic, filesystem/DNS-safe instance id for a developer email.

    Stable across restarts so a user always maps to the same container + volume.
    A hash (not the raw email) keeps container/volume names clean and non-PII.
    """
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]
