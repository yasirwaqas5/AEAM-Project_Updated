"""
aeam/security/jwt_auth.py

JWT authentication validation for the AEAM system.

Validates RS256-signed JWTs against a configured public key, enforcing
expiration, issuer, and audience claims. Contains no business logic —
purely a token verification utility.

Dependencies:
- PyJWT[cryptography]: pip install PyJWT[cryptography]
"""

from __future__ import annotations

import logging
from typing import Any

import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError

logger = logging.getLogger(__name__)

# Engine-owned defaults for expected claim values (ENG-6).
# The Settings-level fields JWT_ISSUER / JWT_AUDIENCE (Phase E3) may override
# these at construction time; when they are None, the defaults below are used
# so the value continues to live once, in its owning module.
_EXPECTED_ISSUER: str = "aeam-auth"
_EXPECTED_AUDIENCE: str = "aeam-api"


class JWTAuth:
    """
    RS256 JWT authentication validator.

    Decodes and validates a JWT using the provided RSA public key.
    Enforces expiration, issuer (``"aeam-auth"``), and audience
    (``"aeam-api"``). Returns the decoded payload on success; raises
    on any validation failure.

    Contains no business logic — it only verifies tokens.

    Args:
        public_key: PEM-encoded RSA public key string used to verify
                    the RS256 signature (e.g. the contents of a
                    ``public_key.pem`` file).

    Raises:
        ValueError: If ``public_key`` is empty or whitespace-only.

    Example::

        auth = JWTAuth(public_key=open("public_key.pem").read())
        payload = auth.verify(token)
        user_id = payload["sub"]
    """

    def __init__(
        self,
        public_key: str,
        issuer: str | None = None,
        audience: str | None = None,
    ) -> None:
        """
        Initialise JWTAuth with an RSA public key and optional claim overrides.

        Args:
            public_key: PEM-encoded RSA public key. Must not be empty.
            issuer:     Overrides the engine-owned default issuer claim
                        (:data:`_EXPECTED_ISSUER`, ``"aeam-auth"``). Pass
                        None (the default) to keep the engine-owned value.
                        Phase E3 (ENG-6): the default lives once, in this
                        module; Settings.JWT_ISSUER may override it at
                        construction time.
            audience:   Overrides the engine-owned default audience claim
                        (:data:`_EXPECTED_AUDIENCE`, ``"aeam-api"``). Same
                        semantics as ``issuer``.

        Raises:
            ValueError: If ``public_key`` is empty or whitespace-only.
        """
        if not public_key or not public_key.strip():
            raise ValueError("public_key must be a non-empty PEM string.")
        self._public_key: str = public_key
        self._issuer: str = issuer if issuer and issuer.strip() else _EXPECTED_ISSUER
        self._audience: str = audience if audience and audience.strip() else _EXPECTED_AUDIENCE

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def verify(self, token: str) -> dict[str, Any]:
        """
        Decode and validate a JWT, returning its payload on success.

        Validation steps:
        1. Decode the token using RS256 and the configured public key.
        2. Enforce that the token has not expired (``exp`` claim).
        3. Enforce issuer claim equals ``"aeam-auth"`` (``iss``).
        4. Enforce audience claim equals ``"aeam-api"`` (``aud``).
        5. Log success with the token subject (``sub``) if present.

        Args:
            token: Encoded JWT string (``"<header>.<payload>.<signature>"``).

        Returns:
            Decoded payload dict containing all JWT claims
            (e.g. ``sub``, ``iss``, ``aud``, ``exp``, ``iat``, and any
            custom claims).

        Raises:
            ExpiredSignatureError: If the token's ``exp`` claim is in the past.
                                   Re-raised after logging.
            InvalidTokenError:     If the signature is invalid, the issuer or
                                   audience do not match, required claims are
                                   missing, or the token is malformed.
                                   Re-raised after logging.
            ValueError:            If ``token`` is empty or whitespace-only.

        Example::

            try:
                payload = auth.verify(token)
                print(payload["sub"])
            except ExpiredSignatureError:
                # Token has expired — prompt re-authentication.
                ...
            except InvalidTokenError:
                # Token is invalid — reject the request.
                ...
        """
        if not token or not token.strip():
            raise ValueError("token must be a non-empty string.")

        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                self._public_key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "require": ["exp", "iss", "aud"],
                    "verify_exp": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )

            subject: str = payload.get("sub", "<no-sub>")
            logger.info(
                "JWTAuth.verify | SUCCESS | sub=%s | iss=%s | aud=%s",
                subject,
                payload.get("iss"),
                payload.get("aud"),
            )
            return payload

        except ExpiredSignatureError as exc:
            logger.warning(
                "JWTAuth.verify | FAILED | reason=token_expired | detail=%s", exc
            )
            raise

        except InvalidTokenError as exc:
            logger.warning(
                "JWTAuth.verify | FAILED | reason=invalid_token | detail=%s", exc
            )
            raise

    def __repr__(self) -> str:
        # Never log or expose the key itself.
        return "JWTAuth(algorithm='RS256')"