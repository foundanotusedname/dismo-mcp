"""Authentication helpers for network transports."""

from __future__ import annotations

import hmac
import ipaddress
import os
from dataclasses import dataclass

from fastmcp.server.auth import AccessToken, TokenVerifier

from .errors import ConfigurationError

READ_SCOPE = "dismo:read"
WRITE_SCOPE = "dismo:write"
_MIN_TOKEN_LENGTH = 32


@dataclass(frozen=True, slots=True)
class _Credential:
    token: str
    client_id: str
    scopes: tuple[str, ...]


class StaticTokenVerifier(TokenVerifier):
    """Verify deployment-managed opaque Bearer tokens in constant time."""

    def __init__(
        self,
        *,
        read_token: str | None = None,
        write_token: str | None = None,
        full_token: str | None = None,
        base_url: str | None = None,
    ) -> None:
        credentials: list[_Credential] = []
        if read_token:
            credentials.append(_Credential(read_token, "dismo-read", (READ_SCOPE,)))
        if write_token:
            credentials.append(
                _Credential(write_token, "dismo-write", (READ_SCOPE, WRITE_SCOPE))
            )
        if full_token:
            credentials.append(
                _Credential(full_token, "dismo-full", (READ_SCOPE, WRITE_SCOPE))
            )
        if not credentials:
            raise ConfigurationError("At least one HTTP Bearer token must be configured")
        for credential in credentials:
            if len(credential.token) < _MIN_TOKEN_LENGTH:
                raise ConfigurationError(
                    f"HTTP Bearer tokens must contain at least {_MIN_TOKEN_LENGTH} characters"
                )
        self._credentials = tuple(credentials)
        super().__init__(base_url=base_url)

    @property
    def scopes_supported(self) -> list[str]:
        return [READ_SCOPE, WRITE_SCOPE]

    async def verify_token(self, token: str) -> AccessToken | None:
        matched: _Credential | None = None
        for credential in self._credentials:
            if hmac.compare_digest(token, credential.token):
                matched = credential
        if matched is None:
            return None
        return AccessToken(
            token=token,
            client_id=matched.client_id,
            subject=matched.client_id,
            scopes=list(matched.scopes),
        )


def is_loopback_host(host: str) -> bool:
    """Return whether a bind host is explicitly limited to the local machine."""
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def auth_from_env(*, base_url: str) -> StaticTokenVerifier | None:
    """Build the configured HTTP verifier without accepting secrets on the CLI."""
    read_token = os.getenv("DISMO_MCP_READ_TOKEN")
    write_token = os.getenv("DISMO_MCP_WRITE_TOKEN")
    full_token = os.getenv("DISMO_MCP_BEARER_TOKEN")
    if not any((read_token, write_token, full_token)):
        return None
    return StaticTokenVerifier(
        read_token=read_token,
        write_token=write_token,
        full_token=full_token,
        base_url=base_url,
    )
