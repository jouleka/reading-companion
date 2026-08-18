"""Strict OIDC Authorization Code + PKCE client for the hosted browser flow."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from app.hosted.auth.models import IdentityClaims

_MAX_DOCUMENT_BYTES = 1024 * 1024


class OIDCError(RuntimeError):
    """A deliberately detail-free public authentication failure."""


@dataclass(frozen=True, slots=True)
class OIDCMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    signing_algorithms: frozenset[str]


class OIDCClient:
    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: str,
        signing_algorithms: tuple[str, ...],
        clock_skew_seconds: int,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.issuer = issuer
        self.client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self.signing_algorithms = signing_algorithms
        self.clock_skew_seconds = clock_skew_seconds
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )
        self._metadata: OIDCMetadata | None = None
        self._jwks: KeySet | None = None
        self._metadata_lock = asyncio.Lock()
        self._jwks_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    async def metadata(self) -> OIDCMetadata:
        if self._metadata is not None:
            return self._metadata
        async with self._metadata_lock:
            if self._metadata is not None:
                return self._metadata
            document = await self._get_json(
                self.issuer + "/.well-known/openid-configuration"
            )
            if document.get("issuer") != self.issuer:
                raise OIDCError("OIDC discovery failed")
            endpoints = {}
            for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                value = document.get(name)
                if not isinstance(value, str) or not self._is_https_url(value):
                    raise OIDCError("OIDC discovery failed")
                endpoints[name] = value
            methods = document.get("token_endpoint_auth_methods_supported")
            if methods is not None and (
                not isinstance(methods, list) or "client_secret_basic" not in methods
            ):
                raise OIDCError("OIDC discovery failed")
            challenge_methods = document.get("code_challenge_methods_supported")
            if not isinstance(challenge_methods, list) or "S256" not in challenge_methods:
                raise OIDCError("OIDC discovery failed")
            response_types = document.get("response_types_supported")
            if not isinstance(response_types, list) or "code" not in response_types:
                raise OIDCError("OIDC discovery failed")
            advertised = document.get("id_token_signing_alg_values_supported")
            if not isinstance(advertised, list):
                raise OIDCError("OIDC discovery failed")
            algorithms = frozenset(self.signing_algorithms).intersection(
                value for value in advertised if isinstance(value, str)
            )
            if not algorithms or "none" in {value.lower() for value in algorithms}:
                raise OIDCError("OIDC discovery failed")
            self._metadata = OIDCMetadata(
                issuer=self.issuer,
                signing_algorithms=algorithms,
                **endpoints,
            )
            return self._metadata

    async def authorization_url(
        self, *, state: str, nonce: str, code_challenge: str
    ) -> str:
        metadata = await self.metadata()
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": self.scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        separator = "&" if urlsplit(metadata.authorization_endpoint).query else "?"
        return metadata.authorization_endpoint + separator + query

    async def exchange_and_validate(
        self,
        *,
        code: str,
        code_verifier: str,
        nonce: str,
        now: datetime,
    ) -> IdentityClaims:
        if not code or len(code) > 4096:
            raise OIDCError("OIDC callback failed")
        metadata = await self.metadata()
        try:
            response = await self._http.post(
                metadata.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "code_verifier": code_verifier,
                },
                auth=(self.client_id, self._client_secret),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise OIDCError("OIDC callback failed") from exc
        if response.status_code != 200:
            raise OIDCError("OIDC callback failed")
        token_response = self._response_json(response)
        id_token = token_response.get("id_token")
        if not isinstance(id_token, str) or len(id_token) > 64 * 1024:
            raise OIDCError("OIDC callback failed")
        claims = await self._decode_id_token(id_token, metadata)
        self._validate_claims(claims, nonce=nonce, now=now)
        return self._identity_claims(claims)

    async def _decode_id_token(
        self, id_token: str, metadata: OIDCMetadata
    ) -> dict[str, Any]:
        key_set = await self._key_set(metadata.jwks_uri, refresh=False)
        try:
            token = jwt.decode(id_token, key_set, algorithms=metadata.signing_algorithms)
        except JoseError:
            key_set = await self._key_set(metadata.jwks_uri, refresh=True)
            try:
                token = jwt.decode(id_token, key_set, algorithms=metadata.signing_algorithms)
            except JoseError as exc:
                raise OIDCError("OIDC callback failed") from exc
        if not isinstance(token.claims, dict):
            raise OIDCError("OIDC callback failed")
        return token.claims

    def _validate_claims(self, claims: dict[str, Any], *, nonce: str, now: datetime) -> None:
        required = ("iss", "sub", "aud", "exp", "iat", "nonce")
        if any(name not in claims for name in required):
            raise OIDCError("OIDC callback failed")
        if claims["iss"] != self.issuer:
            raise OIDCError("OIDC callback failed")
        subject = claims["sub"]
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise OIDCError("OIDC callback failed")
        audience = claims["aud"]
        if isinstance(audience, str):
            audiences = [audience]
        elif isinstance(audience, list) and all(isinstance(item, str) for item in audience):
            audiences = audience
        else:
            raise OIDCError("OIDC callback failed")
        if self.client_id not in audiences:
            raise OIDCError("OIDC callback failed")
        authorized_party = claims.get("azp")
        if (len(audiences) > 1 or authorized_party is not None) and authorized_party != self.client_id:
            raise OIDCError("OIDC callback failed")
        if not isinstance(claims["nonce"], str) or not secrets.compare_digest(
            claims["nonce"], nonce
        ):
            raise OIDCError("OIDC callback failed")
        timestamp = now.timestamp()
        expiry = claims["exp"]
        issued = claims["iat"]
        not_before = claims.get("nbf")
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
            raise OIDCError("OIDC callback failed")
        if isinstance(issued, bool) or not isinstance(issued, (int, float)):
            raise OIDCError("OIDC callback failed")
        if expiry <= timestamp - self.clock_skew_seconds:
            raise OIDCError("OIDC callback failed")
        if issued > timestamp + self.clock_skew_seconds:
            raise OIDCError("OIDC callback failed")
        if not_before is not None and (
            isinstance(not_before, bool)
            or not isinstance(not_before, (int, float))
            or not_before > timestamp + self.clock_skew_seconds
        ):
            raise OIDCError("OIDC callback failed")

    def _identity_claims(self, claims: dict[str, Any]) -> IdentityClaims:
        email_value = claims.get("email")
        email = email_value.strip() if isinstance(email_value, str) else None
        if email is not None and (not email or len(email) > 320 or any(ord(ch) < 32 for ch in email)):
            email = None
        display = next(
            (
                value.strip()
                for value in (claims.get("name"), claims.get("preferred_username"), email)
                if isinstance(value, str) and value.strip()
            ),
            claims["sub"],
        )
        display = "".join(ch for ch in display if ord(ch) >= 32)[:200] or "Reader"
        return IdentityClaims(
            issuer=self.issuer,
            subject=claims["sub"],
            display_name=display,
            email=email,
            email_verified=claims.get("email_verified") is True,
        )

    async def _key_set(self, uri: str, *, refresh: bool) -> KeySet:
        if self._jwks is not None and not refresh:
            return self._jwks
        async with self._jwks_lock:
            if self._jwks is not None and not refresh:
                return self._jwks
            document = await self._get_json(uri)
            keys = document.get("keys")
            if not isinstance(keys, list) or not keys or len(keys) > 20:
                raise OIDCError("OIDC key discovery failed")
            try:
                self._jwks = KeySet.import_key_set(document)
            except (TypeError, ValueError) as exc:
                raise OIDCError("OIDC key discovery failed") from exc
            return self._jwks

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = await self._http.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise OIDCError("OIDC discovery failed") from exc
        if response.status_code != 200:
            raise OIDCError("OIDC discovery failed")
        return self._response_json(response)

    @staticmethod
    def _response_json(response: httpx.Response) -> dict[str, Any]:
        if len(response.content) > _MAX_DOCUMENT_BYTES:
            raise OIDCError("OIDC response was invalid")
        try:
            value = response.json()
        except ValueError as exc:
            raise OIDCError("OIDC response was invalid") from exc
        if not isinstance(value, dict):
            raise OIDCError("OIDC response was invalid")
        return value

    @staticmethod
    def _is_https_url(value: str) -> bool:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
        )
