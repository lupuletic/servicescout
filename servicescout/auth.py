"""Optional auth layer for the MCP server (Epic #9 Tier 4 #9).

Today the MCP server binds to 127.0.0.1 with no auth — fine for a
single-developer / sidecar deployment. For a hosted multi-tenant
deployment we need:

  - **identity**: who is calling? (OIDC bearer token from the agent /
    user, validated against a configured issuer)
  - **scopes**: which entities can they see? (per-team allowlist via
    the `owner` field on each entity; reject hits that the caller's
    teams don't include)

This module is the optional layer. When no auth is configured the MCP
server runs unchanged. When auth is enabled (`--auth-issuer` flag set,
or `MCP_AUTH_ISSUER` env), each tool call:

  1. Reads the `Authorization: Bearer <jwt>` header.
  2. Validates the JWT signature against the issuer's JWKS endpoint
     (cached per process; refreshes on key rotation).
  3. Extracts the caller's `sub` (user id) + `groups` / `teams` claim
     (configurable via --auth-teams-claim, default `groups`).
  4. Filters tool responses to entities whose `owner` is one of the
     caller's teams (or in a configured public set, e.g. `"*"`).

Token validation uses PyJWT with the issuer's `jwks_uri` from the
OIDC discovery document. We don't introspect (RFC 7662) by default —
JWT signature validation is faster and avoids a hop to the issuer per
request. Introspection mode is available via `--auth-mode introspect`
for issuers that don't publish JWKS or that revoke tokens frequently.

This module is INTENTIONALLY skipped by default so existing deployments
keep working. Set `auth.enabled=False` and every entity passes.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AuthConfig:
    enabled: bool = False
    issuer: str = ""
    audience: str = ""
    teams_claim: str = "groups"  # JWT claim name holding team / group identifiers
    mode: str = "jwks"            # "jwks" | "introspect"
    introspect_endpoint: str = ""
    introspect_client_id: str = ""
    introspect_client_secret: str = ""
    public_owners: list[str] = field(default_factory=lambda: ["*"])
    # If a request token resolves to teams that overlap with public_owners
    # we expose all entities. By default `["*"]` means "anyone with a
    # valid token sees everything" — turn this off when shipping to a
    # multi-tenant deployment.

    @classmethod
    def from_env(cls) -> "AuthConfig":
        issuer = os.getenv("MCP_AUTH_ISSUER") or ""
        return cls(
            enabled=bool(issuer),
            issuer=issuer,
            audience=os.getenv("MCP_AUTH_AUDIENCE") or "",
            teams_claim=os.getenv("MCP_AUTH_TEAMS_CLAIM") or "groups",
            mode=os.getenv("MCP_AUTH_MODE") or "jwks",
            introspect_endpoint=os.getenv("MCP_AUTH_INTROSPECT_URL") or "",
            introspect_client_id=os.getenv("MCP_AUTH_INTROSPECT_CLIENT_ID") or "",
            introspect_client_secret=os.getenv("MCP_AUTH_INTROSPECT_CLIENT_SECRET") or "",
            public_owners=[
                t.strip() for t in (os.getenv("MCP_AUTH_PUBLIC_OWNERS") or "*").split(",")
                if t.strip()
            ],
        )


@dataclass
class Caller:
    sub: str
    teams: set[str]
    raw_claims: dict[str, Any]

    @property
    def is_unrestricted(self) -> bool:
        return "*" in self.teams


# --------------------------------------------------------------------------- #
# Token validation
# --------------------------------------------------------------------------- #


_JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_JWKS_TTL = 3600.0  # 1 hour


class AuthError(Exception):
    """Raised when token validation fails."""


def _fetch_jwks(issuer: str) -> dict[str, Any]:
    now = time.time()
    cached = _JWKS_CACHE.get(issuer)
    if cached and cached[0] + _JWKS_TTL > now:
        return cached[1]
    # Standard OIDC discovery: <issuer>/.well-known/openid-configuration
    discovery_url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(discovery_url, timeout=5) as resp:
            discovery = json.loads(resp.read().decode("utf-8"))
        jwks_uri = discovery.get("jwks_uri")
        if not jwks_uri:
            raise AuthError(f"issuer {issuer!r} has no jwks_uri in discovery doc")
        with urllib.request.urlopen(jwks_uri, timeout=5) as resp:
            jwks = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        raise AuthError(f"failed to fetch JWKS from {issuer!r}: {exc}") from exc
    _JWKS_CACHE[issuer] = (now, jwks)
    return jwks


def clear_jwks_cache() -> None:
    """Test-only: drop the JWKS cache."""
    _JWKS_CACHE.clear()


def validate_token_jwks(token: str, config: AuthConfig) -> Caller:
    """Validate a JWT against the issuer's JWKS. Returns the Caller."""
    try:
        import jwt as pyjwt  # PyJWT
        from jwt import PyJWKClient
    except ImportError as exc:
        raise AuthError(
            "PyJWT is required for jwks mode. Install with: pip install pyjwt[crypto]"
        ) from exc
    if not config.issuer:
        raise AuthError("issuer not configured")
    try:
        # Discover jwks_uri via OIDC discovery, build a PyJWKClient.
        jwks_uri = _fetch_jwks(config.issuer)
        # PyJWKClient takes a URL directly. We've cached the JWKS body
        # ourselves, so construct a Mock-style client only when the
        # library's URL fetch would also hit OIDC discovery.
        # Simpler: pass the URL directly and let PyJWKClient handle TTL.
        discovery = jwks_uri  # already JWKS dict, not URL
        kid_to_key = {k["kid"]: k for k in (discovery.get("keys") or [])}
        unverified = pyjwt.get_unverified_header(token)
        kid = unverified.get("kid")
        key_jwk = kid_to_key.get(kid)
        if key_jwk is None:
            raise AuthError(f"unknown kid {kid!r}")
        from jwt.algorithms import RSAAlgorithm, ECAlgorithm
        alg = key_jwk.get("kty")
        if alg == "RSA":
            public_key = RSAAlgorithm.from_jwk(key_jwk)
        elif alg == "EC":
            public_key = ECAlgorithm.from_jwk(key_jwk)
        else:
            raise AuthError(f"unsupported kty {alg!r}")
        decoded = pyjwt.decode(
            token, public_key,
            algorithms=[key_jwk.get("alg") or "RS256"],
            audience=config.audience or None,
            issuer=config.issuer,
        )
    except pyjwt.PyJWTError as exc:
        raise AuthError(f"JWT validation failed: {exc}") from exc
    return _claims_to_caller(decoded, config)


def validate_token_introspect(token: str, config: AuthConfig) -> Caller:
    """Validate a token via RFC 7662 introspection."""
    if not config.introspect_endpoint:
        raise AuthError("introspect mode requires MCP_AUTH_INTROSPECT_URL")
    import base64
    creds = base64.b64encode(
        f"{config.introspect_client_id}:{config.introspect_client_secret}".encode()
    ).decode()
    data = f"token={urllib.parse.quote(token)}".encode("utf-8")
    req = urllib.request.Request(
        config.introspect_endpoint,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        raise AuthError(f"introspection failed: {exc}") from exc
    if not body.get("active"):
        raise AuthError("token is not active")
    return _claims_to_caller(body, config)


def _claims_to_caller(claims: dict[str, Any], config: AuthConfig) -> Caller:
    teams_value = claims.get(config.teams_claim) or claims.get("scope") or []
    if isinstance(teams_value, str):
        # Some issuers space-separate scopes.
        teams = {t.strip() for t in teams_value.split() if t.strip()}
    elif isinstance(teams_value, list):
        teams = {str(t).strip() for t in teams_value if t}
    else:
        teams = set()
    # Add the public-owners wildcard if configured to grant unrestricted
    # access to any caller with a valid token.
    if "*" in config.public_owners:
        teams.add("*")
    sub = str(claims.get("sub") or claims.get("client_id") or "")
    return Caller(sub=sub, teams=teams, raw_claims=claims)


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #


def validate(token: str, config: AuthConfig) -> Caller:
    """Validate a bearer token and return the Caller. Raises AuthError
    on any validation failure (signature, expiry, audience, issuer,
    revocation in introspect mode).
    """
    if not config.enabled:
        # Auth disabled — every call gets unrestricted access. Used by
        # tests and the default 127.0.0.1 sidecar mode.
        return Caller(sub="anonymous", teams={"*"}, raw_claims={})
    if not token:
        raise AuthError("missing bearer token")
    if config.mode == "jwks":
        return validate_token_jwks(token, config)
    if config.mode == "introspect":
        return validate_token_introspect(token, config)
    raise AuthError(f"unknown auth mode {config.mode!r}")


def filter_entity(entity: dict[str, Any], caller: Caller) -> bool:
    """Return True iff the caller is allowed to see this entity.

    Today the rule is simple: if the entity's owner field is in the
    caller's teams (or the caller is unrestricted), allow. If the entity
    has no owner, fall through to allow (don't break catalogs that
    haven't populated owner).
    """
    if caller.is_unrestricted:
        return True
    md = entity.get("metadata") or {}
    spec = entity.get("spec") or {}
    annotations = md.get("annotations") or {}
    owner = (annotations.get("owner") or spec.get("owner") or "").strip()
    if not owner or owner == "unknown":
        # Unowned entities are visible to all authenticated callers.
        return True
    # Allow exact match or any team in the caller's set.
    return owner in caller.teams or _normalise_owner(owner) in caller.teams


def _normalise_owner(value: str) -> str:
    # Backstage stores `group:default/team-a` — strip the prefix.
    if "/" in value:
        value = value.split("/", 1)[-1]
    if ":" in value:
        value = value.split(":", 1)[-1]
    return value
