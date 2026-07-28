"""
aeam/security/jwt_auth.py

JWT authentication validation for the AEAM system.

Validates RS256-signed JWTs against a configured public key, enforcing
expiration, issuer, and audience claims. Contains no business logic —
purely a token verification utility.

Phase E13 (Enterprise Certification) extends the *same* verification path
with enterprise SSO: instead of a single static PEM, the verifier may
resolve signing keys from an identity provider's JWKS endpoint, selecting
the key by the token's ``kid`` header. Nothing else about the contract
changes — issuer, audience, expiry and algorithm enforcement are identical
in both modes, and AEAM remains a token *validator*, never an identity
provider. Static-PEM construction keeps byte-identical behaviour
(COMPAT-2: the new parameters default to None/no-op).

Dependencies:
- PyJWT[cryptography]: pip install PyJWT[cryptography]
"""

from __future__ import annotations

import logging
from typing import Any

import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError, PyJWKClientError

logger = logging.getLogger(__name__)

# Engine-owned defaults for expected claim values (ENG-6).
# The Settings-level fields JWT_ISSUER / JWT_AUDIENCE (Phase E3) may override
# these at construction time; when they are None, the defaults below are used
# so the value continues to live once, in its owning module.
_EXPECTED_ISSUER: str = "aeam-auth"
_EXPECTED_AUDIENCE: str = "aeam-api"

# Engine-owned default signature algorithms (ENG-6). RS256 is the pre-E13
# behaviour and stays the default; enterprise IdPs that sign with a
# different RSA/EC family declare it via Settings.OIDC_ALGORITHMS. The list
# is a strict allow-list — an unlisted algorithm is rejected, so a token
# claiming `alg: none` or a symmetric algorithm can never be accepted.
_DEFAULT_ALGORITHMS: tuple[str, ...] = ("RS256",)

# How long a JWKS document is reused before it is re-fetched. PyJWKClient
# owns the cache; this is the lifespan handed to it. Short enough that an
# IdP key rotation is picked up without a restart, long enough that normal
# request traffic does not hammer the IdP.
_JWKS_CACHE_LIFESPAN_SECONDS: int = 300


class JWTAuth:
    """
    RS256 JWT authentication validator.

    Decodes and validates a JWT using either a static RSA public key or an
    identity provider's JWKS endpoint (Phase E13 SSO). Enforces expiration,
    issuer (``"aeam-auth"`` by default), and audience (``"aeam-api"`` by
    default). Returns the decoded payload on success; raises on any
    validation failure.

    Contains no business logic — it only verifies tokens. It never issues,
    refreshes, or introspects them: AEAM validates enterprise-issued
    identity, it does not produce it.

    Args:
        public_key: PEM-encoded RSA public key string used to verify
                    the signature (e.g. the contents of a
                    ``public_key.pem`` file). May be empty ONLY when
                    ``jwks_url`` is supplied.
        jwks_url:   Phase E13. URL of the IdP's JWKS document. When set,
                    the signing key is resolved per token from its ``kid``
                    header and ``public_key`` is ignored.

    Raises:
        ValueError: If neither ``public_key`` nor ``jwks_url`` is supplied.

    Example::

        # Static key (pre-E13 posture, unchanged)
        auth = JWTAuth(public_key=open("public_key.pem").read())

        # Enterprise SSO (Phase E13)
        auth = JWTAuth(
            public_key="",
            jwks_url="https://idp.example.com/.well-known/jwks.json",
            issuer="https://idp.example.com/",
            audience="aeam-console",
        )

        payload = auth.verify(token)
        user_id = payload["sub"]
    """

    def __init__(
        self,
        public_key: str,
        issuer: str | None = None,
        audience: str | None = None,
        jwks_url: str | None = None,
        algorithms: list[str] | None = None,
    ) -> None:
        """
        Initialise JWTAuth with key material and optional claim overrides.

        Args:
            public_key: PEM-encoded RSA public key. Must be non-empty
                        unless ``jwks_url`` is supplied.
            issuer:     Overrides the engine-owned default issuer claim
                        (:data:`_EXPECTED_ISSUER`, ``"aeam-auth"``). Pass
                        None (the default) to keep the engine-owned value.
                        Phase E3 (ENG-6): the default lives once, in this
                        module; Settings.JWT_ISSUER may override it at
                        construction time.
            audience:   Overrides the engine-owned default audience claim
                        (:data:`_EXPECTED_AUDIENCE`, ``"aeam-api"``). Same
                        semantics as ``issuer``.
            jwks_url:   Phase E13 (SSO). When set, signing keys are fetched
                        from this JWKS document and selected by the token's
                        ``kid``. None (the default) keeps the exact pre-E13
                        static-PEM verification path (COMPAT-2).
            algorithms: Overrides the engine-owned default algorithm
                        allow-list (:data:`_DEFAULT_ALGORITHMS`,
                        ``["RS256"]``). None keeps the default. An empty
                        or whitespace-only list is treated as None rather
                        than as "accept nothing", so a misconfiguration
                        can never silently disable verification.

        Raises:
            ValueError: If ``public_key`` is empty/whitespace-only and no
                        ``jwks_url`` was supplied.
        """
        cleaned_jwks: str = (jwks_url or "").strip()
        if not cleaned_jwks and (not public_key or not public_key.strip()):
            raise ValueError(
                "public_key must be a non-empty PEM string (or a jwks_url "
                "must be supplied)."
            )

        self._public_key: str = public_key
        self._issuer: str = issuer if issuer and issuer.strip() else _EXPECTED_ISSUER
        self._audience: str = audience if audience and audience.strip() else _EXPECTED_AUDIENCE
        self._jwks_url: str = cleaned_jwks

        cleaned_algs: list[str] = [a.strip() for a in (algorithms or []) if a and a.strip()]
        self._algorithms: list[str] = cleaned_algs or list(_DEFAULT_ALGORITHMS)

        # PyJWKClient owns fetching, caching and kid-selection. Constructed
        # lazily-but-once here (no network I/O happens until the first
        # verify()), so an unreachable IdP fails per-request as a 401
        # rather than preventing the process from starting.
        self._jwk_client: jwt.PyJWKClient | None = (
            jwt.PyJWKClient(
                self._jwks_url,
                cache_keys=True,
                lifespan=_JWKS_CACHE_LIFESPAN_SECONDS,
            )
            if self._jwks_url
            else None
        )

    # ------------------------------------------------------------------
    # Key resolution
    # ------------------------------------------------------------------

    @property
    def uses_jwks(self) -> bool:
        """True when signing keys are resolved from an IdP's JWKS document."""
        return self._jwk_client is not None

    def _signing_key_for(self, token: str) -> Any:
        """
        Return the verification key for ``token``.

        In JWKS mode the key is selected by the token's ``kid`` header; a
        resolution failure (unreachable IdP, unknown kid, malformed JWKS)
        is converted to :class:`InvalidTokenError` so the caller's existing
        401 path handles it — an identity provider outage must never
        surface as a 500 or, worse, as an accepted request.
        """
        if self._jwk_client is None:
            return self._public_key
        try:
            return self._jwk_client.get_signing_key_from_jwt(token).key
        except PyJWKClientError as exc:
            logger.warning(
                "JWTAuth.verify | FAILED | reason=jwks_key_unresolved | detail=%s", exc
            )
            raise InvalidTokenError(f"JWKS signing key could not be resolved: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            # Declared never-raise-anything-but-InvalidTokenError boundary
            # (CODE-5): urllib/socket/JSON failures from the JWKS fetch all
            # mean the same thing to a caller — this token cannot be
            # verified right now — and are logged before conversion.
            logger.warning(
                "JWTAuth.verify | FAILED | reason=jwks_fetch_error | detail=%s", exc
            )
            raise InvalidTokenError(f"JWKS endpoint unavailable: {exc}") from exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def verify(self, token: str) -> dict[str, Any]:
        """
        Decode and validate a JWT, returning its payload on success.

        Validation steps:
        1. Resolve the verification key — the configured static PEM, or
           (Phase E13 SSO) the IdP JWKS key matching the token's ``kid``.
        2. Decode the token using the configured algorithm allow-list.
        3. Enforce that the token has not expired (``exp`` claim).
        4. Enforce the issuer claim (``iss``) — ``"aeam-auth"`` by default,
           the IdP issuer under SSO.
        5. Enforce the audience claim (``aud``) — ``"aeam-api"`` by default,
           the registered client id under SSO.
        6. Log success with the token subject (``sub``) if present.

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
                self._signing_key_for(token),
                algorithms=self._algorithms,
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
        # Never log or expose the key itself (SEC-5). The JWKS URL is a
        # public endpoint, not a secret, so naming the mode is safe and
        # tells an operator which verification path is actually live.
        mode = f"jwks={self._jwks_url!r}" if self._jwks_url else "key=static"
        return f"JWTAuth(algorithms={self._algorithms!r}, {mode})"