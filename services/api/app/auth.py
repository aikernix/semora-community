from __future__ import annotations

import os
from dataclasses import dataclass

import jwt
from fastapi import HTTPException


@dataclass(frozen=True)
class Principal:
    subject: str
    username: str
    roles: frozenset[str]
    claims: dict


class OIDCVerifier:
    def __init__(self) -> None:
        self.issuer = (os.getenv("OIDC_ISSUER") or "").rstrip("/")
        self.audience = os.getenv("OIDC_AUDIENCE", "semora-web")
        jwks_url = os.getenv("OIDC_JWKS_URL") or (
            f"{self.issuer}/protocol/openid-connect/certs" if self.issuer else None
        )
        self._client = jwt.PyJWKClient(jwks_url, cache_keys=True) if jwks_url else None

    def verify(self, authorization: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        if not self.issuer or self._client is None:
            raise HTTPException(503, "身份认证未配置，请设置外部OIDC服务后重试")
        token = authorization.removeprefix("Bearer ").strip()
        try:
            key = self._client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["exp", "iat", "sub", "iss"]},
            )
        except Exception as exc:
            raise HTTPException(401, f"invalid access token: {exc}") from exc
        roles = frozenset(claims.get("realm_access", {}).get("roles", []))
        return Principal(
            subject=claims["sub"],
            username=claims.get("preferred_username", claims["sub"]),
            roles=roles,
            claims=claims,
        )


oidc_verifier = OIDCVerifier()


ALL_APPLICATION_ROLES = {
    "business_user", "analyst", "data_steward", "approver", "auditor", "admin",
}


def required_roles(method: str, path: str) -> frozenset[str]:
    """Return the least-privilege realm roles for a versioned API operation."""
    method = method.upper()
    segments = path.rstrip("/").split("/")
    if method == "DELETE" and (
        len(segments) == 5 and segments[:4] == ["", "api", "v1", "datasources"]
        or len(segments) == 6 and segments[:5] == ["", "api", "v1", "ontology", "domains"]
    ):
        return frozenset({"admin"})
    if path.startswith("/api/v1/settings"):
        return frozenset({"admin"} if method != "GET" else ALL_APPLICATION_ROLES)
    if path.startswith("/api/v1/domains"):
        return frozenset(ALL_APPLICATION_ROLES)
    if path.startswith("/api/v1/approvals"):
        return frozenset({"approver", "admin"})
    if path.startswith("/api/v1/audits"):
        return frozenset({"auditor", "admin"})
    if path.startswith(("/api/v1/datasources", "/api/v1/datasource-connection-test")):
        if method != "GET" or path.endswith("/test") or path.endswith("/scan"):
            return frozenset({"data_steward", "admin"})
        return frozenset({"analyst", "data_steward", "admin"})
    if path.startswith("/api/v1/ontology"):
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            return frozenset({"data_steward", "admin"})
        return frozenset({"analyst", "data_steward", "admin"})
    if path.startswith("/api/v1/knowledge"):
        if method in {"POST", "PUT", "PATCH", "DELETE"}:
            return frozenset({"data_steward", "admin"})
        return frozenset({"business_user", "analyst", "data_steward", "admin"})
    if path.startswith("/api/v1/executions"):
        return frozenset({"business_user", "analyst", "admin"})
    return frozenset({"business_user", "analyst", "admin"})


def enforce_roles(principal: Principal, method: str, path: str) -> None:
    allowed = required_roles(method, path)
    if not principal.roles.intersection(allowed):
        raise HTTPException(403, f"required role: one of {', '.join(sorted(allowed))}")

