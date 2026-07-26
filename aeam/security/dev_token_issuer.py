"""
aeam/security/dev_token_issuer.py

Dev-only JWT minting utility (Phase E10).

AEAM validates enterprise-issued tokens; it never becomes an identity
provider (see Constitution, Phase E3/E10 design notes). Real SSO/OIDC
federation is scheduled for Phase E13. Until then, local development and
demo/staging environments need *some* way to obtain a bearer token to drive
the E10 console session layer end-to-end without hand-crafting a JWT.

This module is that documented stop-gap: it generates a throwaway RSA
keypair at process start and signs short-lived RS256 tokens with it. It is
wired into exactly one place — the ``/api/v1/auth/dev-token`` route in
``aeam/api/auth.py`` — which is itself gated to only respond outside of
production the same way every other dev-only surface in this codebase is
gated (``SecurityMiddleware``'s own placeholder-key fallback is the
precedent). It must never be reachable in a production posture.

Contains no business logic and performs no I/O beyond key generation.
"""

from __future__ import annotations

import logging
import time
import uuid

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

logger = logging.getLogger(__name__)

_ISSUER: str = "aeam-dev-issuer"
_AUDIENCE: str = "aeam-api"
_DEFAULT_TTL_SECONDS: int = 3600


class DevTokenIssuer:
    """
    Mints short-lived RS256 JWTs for local/demo use only.

    A fresh RSA keypair is generated once, at construction (process
    lifetime). Tokens minted by this issuer are self-signed by that
    ephemeral key — they are NOT verifiable by :class:`JWTAuth` unless
    that instance happens to be configured with the same public key, which
    it never is in a real deployment. Their purpose is purely to give the
    frontend session layer a realistic, structurally valid, expiring JWT
    to exercise bearer-attachment, role-aware navigation, and expiry UX
    against, in an environment where ``SecurityMiddleware`` bypasses
    verification entirely (``ENVIRONMENT=development``).
    """

    def __init__(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._private_key = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        self._public_key = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        logger.warning(
            "DevTokenIssuer initialised with an ephemeral in-memory keypair. "
            "This is a Phase E10 dev-only utility -- it must never be reachable "
            "outside a development/demo posture."
        )

    def mint(
        self,
        sub: str,
        roles: list[str],
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> dict[str, object]:
        """
        Issue a signed dev token.

        Args:
            sub:         Subject/principal identifier to embed (``sub`` claim).
            roles:       Role list to embed (``roles`` claim) — must match the
                         RBAC vocabulary in ``aeam.security.rbac``.
            ttl_seconds: Token lifetime in seconds (default 1 hour).

        Returns:
            Dict with ``access_token``, ``token_type``, ``expires_in``,
            ``expires_at`` (unix seconds), ``sub``, and ``roles``.
        """
        now = int(time.time())
        expires_at = now + max(60, ttl_seconds)
        payload = {
            "sub": sub,
            "roles": list(roles),
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "iat": now,
            "exp": expires_at,
            "jti": str(uuid.uuid4()),
            "dev_token": True,
        }
        token = jwt.encode(payload, self._private_key, algorithm="RS256")
        logger.info("DevTokenIssuer.mint | sub=%s | roles=%s | exp=%s", sub, roles, expires_at)
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": expires_at - now,
            "expires_at": expires_at,
            "sub": sub,
            "roles": list(roles),
        }
