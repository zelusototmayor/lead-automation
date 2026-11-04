"""Lazy PostgreSQL configuration and SQLAlchemy factories.

Importing this module never reads configuration or creates database resources.
"""

from __future__ import annotations

from functools import lru_cache
import ipaddress
import unicodedata
from urllib.parse import unquote

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker


_DATABASE_URL_ERROR = "DATABASE_URL must be a postgresql+psycopg URL"
_LIBPQ_IDENTITY_QUERY_KEYS = frozenset(
    {
        "host",
        "hostaddr",
        "port",
        "dbname",
        "database",
        "user",
        "password",
        "service",
        "servicefile",
    }
)


def _contains_whitespace_or_control(value: str) -> bool:
    decoded_value = unquote(value)
    return any(
        character.isspace() or unicodedata.category(character) == "Cc"
        for character in decoded_value
    )


def _is_valid_database_host(host: str) -> bool:
    """Validate an IP address or deployment-compatible DNS/container name."""

    if ":" in host:
        try:
            return ipaddress.ip_address(host).version == 6
        except ValueError:
            return False

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        return address.version == 4

    rootless_host = host[:-1] if host.endswith(".") else host
    if "." in rootless_host and all(label.isdigit() for label in rootless_host.split(".")):
        return False

    try:
        ascii_host = rootless_host.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if not ascii_host or len(ascii_host) > 253:
        return False

    for label in ascii_host.split("."):
        if (
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(character.isalnum() or character in "-_" for character in label)
        ):
            return False

    # Underscores are intentionally supported for internal container/service hostnames.
    return True


class DatabaseSettings(BaseSettings):
    """Database settings loaded explicitly from the process environment."""

    model_config = SettingsConfigDict(
        case_sensitive=True,
        extra="ignore",
        hide_input_in_errors=True,
        validate_default=True,
    )

    database_url: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="DATABASE_URL",
    )

    @model_validator(mode="before")
    @classmethod
    def redact_database_url_before_validation(cls, data: object) -> object:
        """Wrap environment input before failures can retain its raw value."""

        if not isinstance(data, dict):
            return data
        redacted_data = data.copy()
        for key in ("database_url", "DATABASE_URL"):
            value = redacted_data.get(key)
            if isinstance(value, str):
                redacted_data[key] = SecretStr(value)
        return redacted_data

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        raw_url = value.get_secret_value()
        if not raw_url.strip():
            raise ValueError(_DATABASE_URL_ERROR)
        try:
            parsed = make_url(raw_url)
            port = parsed.port
        except (ArgumentError, TypeError, ValueError):
            raise ValueError(_DATABASE_URL_ERROR) from None
        if (
            parsed.drivername != "postgresql+psycopg"
            or not parsed.host
            or not parsed.database
            or _contains_whitespace_or_control(parsed.host)
            or not _is_valid_database_host(parsed.host)
            or _contains_whitespace_or_control(parsed.database)
            or (port is not None and not 1 <= port <= 65535)
            or any(
                unquote(str(key)).casefold() in _LIBPQ_IDENTITY_QUERY_KEYS
                for key in parsed.query
            )
        ):
            raise ValueError(_DATABASE_URL_ERROR)
        return value


@lru_cache(maxsize=1)
def get_database_settings() -> DatabaseSettings:
    """Load and cache database settings on first explicit call."""

    return DatabaseSettings()


def create_database_engine(settings: DatabaseSettings | None = None) -> Engine:
    """Create a PostgreSQL engine without opening a connection eagerly."""

    resolved_settings = settings or get_database_settings()
    return create_engine(
        resolved_settings.database_url.get_secret_value(),
        pool_pre_ping=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create the application's predictable SQLAlchemy session factory."""

    return sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )
