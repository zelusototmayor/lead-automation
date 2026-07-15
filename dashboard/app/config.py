"""Import-safe environment configuration for the CRM dashboard security gate."""

from __future__ import annotations

import ipaddress
import os
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

_LOCAL_ENVIRONMENTS = {"dev", "development", "test"}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


@dataclass(frozen=True)
class Settings:
    write_token: str | None
    csrf_token: str | None
    allowed_write_origins: tuple[str, ...]
    environment: str


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
    if (
        len(ascii_hostname) > 253
        or any(
            not label or len(label) > 63 or not _DNS_LABEL.fullmatch(label)
            for label in labels
        )
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
            _validate_hostname(parsed.hostname, is_ip_literal=parsed.netloc.startswith("["))
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
            raise ValueError("CRM_ALLOWED_WRITE_ORIGINS must contain bare HTTPS origins")
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
