"""Import-safe environment configuration for the CRM dashboard security gate."""

from __future__ import annotations

import ipaddress
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import SecretStr

_LOCAL_ENVIRONMENTS = {"dev", "development", "test"}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_CRM_PRINCIPAL_PERMISSIONS = frozenset(
    {
        "crm:read",
        "crm:lead:create",
        "crm:lead:edit",
        "crm:lead-stage:write",
        "crm:call:log",
        "crm:email:log",
        "crm:note:write",
        "crm:task:write",
        "crm:proposal:write",
    }
)


@dataclass(frozen=True)
class Settings:
    write_token: str | None
    csrf_token: str | None
    allowed_write_origins: tuple[str, ...]
    environment: str


@dataclass(frozen=True)
class AgentSettings:
    """One fail-closed server-side principal for the v1 agent ingress."""

    bearer_token: SecretStr
    workspace_id: UUID
    scopes: frozenset[str]
    source_scopes: frozenset[str]
    token_issued_at: datetime
    token_expires_at: datetime


@dataclass(frozen=True)
class PrincipalSettings:
    """One server-configured browser principal for protected rich CRM routes."""

    username: str
    password: SecretStr
    workspace_id: UUID
    actor_id: UUID
    permissions: frozenset[str]
    is_admin: bool


def _optional_secret(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _validate_hostname(hostname: str | None, *, is_ip_literal: bool = False) -> None:
    if not hostname:
        raise ValueError("missing hostname")

    try:
        ipaddress.ip_address(hostname)
        return
    except ValueError:
        if (
            is_ip_literal
            or ":" in hostname
            or ("." in hostname and hostname.replace(".", "").isdigit())
        ):
            raise ValueError("invalid IP address") from None

    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid DNS hostname") from exc

    labels = ascii_hostname.split(".")
    if len(ascii_hostname) > 253 or any(
        not label or len(label) > 63 or not _DNS_LABEL.fullmatch(label)
        for label in labels
    ):
        raise ValueError("invalid DNS hostname")


def _parse_allowed_origins(raw: str, environment: str) -> tuple[str, ...]:
    origins: list[str] = []
    for candidate in raw.split(","):
        origin = candidate.strip()
        if not origin:
            continue
        try:
            if any(
                character.isspace() or unicodedata.category(character) == "Cc"
                for character in origin
            ):
                raise ValueError("whitespace or control character in origin")
            parsed = urlsplit(origin)
            parsed.port  # Force validation of malformed/non-numeric ports.
            _validate_hostname(
                parsed.hostname, is_ip_literal=parsed.netloc.startswith("[")
            )
        except ValueError as exc:
            raise ValueError(
                "CRM_ALLOWED_WRITE_ORIGINS must contain bare HTTPS origins"
            ) from exc
        is_https = parsed.scheme == "https"
        is_local_dev = (
            environment in _LOCAL_ENVIRONMENTS
            and parsed.scheme == "http"
            and parsed.hostname in _LOCAL_HOSTS
        )
        is_bare_origin = (
            origin == f"{parsed.scheme}://{parsed.netloc}"
            and bool(parsed.netloc)
            and parsed.path == ""
            and not parsed.query
            and not parsed.fragment
            and parsed.username is None
            and parsed.password is None
        )
        if not is_bare_origin or not (is_https or is_local_dev):
            raise ValueError(
                "CRM_ALLOWED_WRITE_ORIGINS must contain bare HTTPS origins"
            )
        if origin not in origins:
            origins.append(origin)
    return tuple(origins)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read and validate settings without performing network or database I/O."""
    environment = os.getenv("CRM_ENV", "production").strip().lower() or "production"
    return Settings(
        write_token=_optional_secret("CRM_WRITE_TOKEN"),
        csrf_token=_optional_secret("CRM_CSRF_TOKEN"),
        allowed_write_origins=_parse_allowed_origins(
            os.getenv("CRM_ALLOWED_WRITE_ORIGINS", ""), environment
        ),
        environment=environment,
    )


def _required_principal_value(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError("invalid principal configuration")
    return value


def _required_basic_value(name: str) -> str:
    value = _required_principal_value(name)
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("invalid principal configuration") from None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("invalid principal configuration")
    return value


def _principal_username() -> str:
    username = _required_basic_value("CRM_PRINCIPAL_USERNAME")
    if ":" in username:
        raise ValueError("invalid principal configuration")
    return username


def _principal_permissions() -> frozenset[str]:
    raw = _required_principal_value("CRM_PRINCIPAL_PERMISSIONS")
    values = raw.split(",")
    if (
        not values
        or any(not value or value != value.strip() for value in values)
        or len(values) != len(set(values))
        or not set(values).issubset(_CRM_PRINCIPAL_PERMISSIONS)
        or "crm:read" not in values
    ):
        raise ValueError("invalid principal configuration")
    return frozenset(values)


@lru_cache(maxsize=1)
def get_principal_settings() -> PrincipalSettings:
    """Load the rich-route principal mapping without database or network I/O."""

    try:
        workspace_id = UUID(_required_principal_value("CRM_PRINCIPAL_WORKSPACE_ID"))
        actor_id = UUID(_required_principal_value("CRM_PRINCIPAL_ACTOR_ID"))
    except (TypeError, ValueError):
        raise ValueError("invalid principal configuration") from None
    raw_is_admin = _required_principal_value("CRM_PRINCIPAL_IS_ADMIN")
    if raw_is_admin not in {"true", "false"}:
        raise ValueError("invalid principal configuration")
    return PrincipalSettings(
        username=_principal_username(),
        password=SecretStr(_required_basic_value("CRM_PRINCIPAL_PASSWORD")),
        workspace_id=workspace_id,
        actor_id=actor_id,
        permissions=_principal_permissions(),
        is_admin=raw_is_admin == "true",
    )


def _required_agent_value(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError("invalid agent configuration")
    return value.strip()


def _agent_timestamp(name: str) -> datetime:
    try:
        value = datetime.fromisoformat(
            _required_agent_value(name).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        raise ValueError("invalid agent configuration") from None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid agent configuration")
    return value.astimezone(UTC)


def _agent_set(name: str) -> frozenset[str]:
    values = frozenset(item.strip() for item in _required_agent_value(name).split(","))
    if "" in values or any(
        len(item) > 255
        or any(unicodedata.category(character) in {"Cc", "Cf"} for character in item)
        for item in values
    ):
        raise ValueError("invalid agent configuration")
    return values


@lru_cache(maxsize=1)
def get_agent_settings() -> AgentSettings:
    """Load the scoped, short-lived agent principal without database I/O."""

    try:
        workspace_id = UUID(_required_agent_value("CRM_AGENT_WORKSPACE_ID"))
    except (TypeError, ValueError):
        raise ValueError("invalid agent configuration") from None
    issued_at = _agent_timestamp("CRM_AGENT_TOKEN_ISSUED_AT")
    expires_at = _agent_timestamp("CRM_AGENT_TOKEN_EXPIRES_AT")
    if expires_at <= issued_at or expires_at - issued_at > timedelta(minutes=15):
        raise ValueError("invalid agent configuration")
    return AgentSettings(
        bearer_token=SecretStr(_required_agent_value("CRM_AGENT_BEARER_TOKEN")),
        workspace_id=workspace_id,
        scopes=_agent_set("CRM_AGENT_SCOPES"),
        source_scopes=_agent_set("CRM_AGENT_SOURCE_SCOPES"),
        token_issued_at=issued_at,
        token_expires_at=expires_at,
    )
