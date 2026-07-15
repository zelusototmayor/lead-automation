"""Strict versioned contracts for inbound CRM events."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
import unicodedata
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import CoreSchema, core_schema


MAX_CANONICAL_EVENT_BYTES = 1024 * 1024


def _reject_control_characters(value: Any) -> Any:
    """Reject Unicode control/format characters in schema-owned identifiers."""

    if isinstance(value, str) and any(
        unicodedata.category(character) in {"Cc", "Cf"} for character in value
    ):
        raise ValueError("invalid identifier")
    return value


IdentifierText = BeforeValidator(_reject_control_characters)
NonBlank = Annotated[
    str,
    IdentifierText,
    Field(strict=True, min_length=1, max_length=255, pattern=r".*\S.*"),
]
EmailText = Annotated[
    str,
    IdentifierText,
    Field(strict=True, min_length=1, max_length=320, pattern=r".*\S.*"),
]
DomainText = Annotated[
    str,
    IdentifierText,
    Field(strict=True, min_length=1, max_length=253, pattern=r".*\S.*"),
]
ExternalId = Annotated[
    str,
    IdentifierText,
    Field(strict=True, min_length=1, max_length=512, pattern=r".*\S.*"),
]
UriText = Annotated[
    str,
    IdentifierText,
    Field(strict=True, min_length=1, max_length=2048, pattern=r".*\S.*"),
]
ContentHash = Annotated[
    str,
    IdentifierText,
    Field(strict=True, min_length=1, max_length=512, pattern=r".*\S.*"),
]
SourceSystem = Literal[
    "google_sheets", "gmail", "google_calendar", "granola", "manual", "agent"
]
EntityKind = Literal[
    "lead", "account", "contact", "message", "thread", "meeting", "proposal", "document"
]
EvidenceType = Literal[
    "sheet_cell",
    "email_message",
    "attachment",
    "calendar_event",
    "meeting_note",
    "manual_confirmation",
    "contract",
    "payment",
]

_SAFE_ERROR_LOCATION_FIELDS = frozenset(
    {
        "account_hint",
        "account_id",
        "causation_id",
        "company_name",
        "contact_email",
        "content_hash",
        "correlation_id",
        "domain",
        "event_type",
        "evidence",
        "external_event_id",
        "external_id",
        "facts",
        "kind",
        "occurred_at",
        "schema_version",
        "scope",
        "source",
        "subject",
        "system",
        "type",
        "uri",
    }
)
_REDACTED_ERROR_LOCATION = "<redacted>"


def _safe_error_location(location: tuple[str | int, ...]) -> tuple[str | int, ...]:
    """Retain schema fields and only the real top-level evidence sequence index."""

    safe: list[str | int] = []
    for position, token in enumerate(location):
        is_evidence_index = (
            type(token) is int
            and position == 1
            and len(location) > 1
            and location[0] == "evidence"
        )
        if is_evidence_index or (
            isinstance(token, str) and token in _SAFE_ERROR_LOCATION_FIELDS
        ):
            safe.append(token)
        else:
            safe.append(_REDACTED_ERROR_LOCATION)
    return tuple(safe)


def _safe_error_context(error: dict[str, Any]) -> dict[str, Any] | None:
    """Retain schema diagnostics while replacing exception text that may contain input."""

    context = error.get("ctx")
    if not context:
        return None
    sanitized: dict[str, Any] = {}
    for key, value in context.items():
        if key == "error":
            sanitized[key] = ValueError("invalid value")
        elif isinstance(value, (int, float, bool, type(None))):
            sanitized[key] = value
        elif key in {"expected", "pattern", "field_type", "class_name", "method_name"}:
            # These values originate in the static schema, not in caller input.
            sanitized[key] = value
        else:
            sanitized[key] = "redacted"
    return sanitized


def _sanitized_validation_error(
    model_name: str, exc: ValidationError
) -> ValidationError:
    lines: list[dict[str, Any]] = []
    for error in exc.errors(include_url=False):
        line: dict[str, Any] = {
            "type": error["type"],
            "loc": _safe_error_location(error["loc"]),
            "input": None,
        }
        context = _safe_error_context(error)
        if context is not None:
            line["ctx"] = context
        lines.append(line)
    try:
        return ValidationError.from_exception_data(model_name, lines, hide_input=True)
    except (KeyError, TypeError):
        fallback = [
            {
                "type": "value_error",
                "loc": _safe_error_location(error["loc"]),
                "input": None,
                "ctx": {"error": ValueError("invalid value")},
            }
            for error in exc.errors(include_url=False)
        ]
        return ValidationError.from_exception_data(
            model_name, fallback, hide_input=True
        )


class StrictContract(BaseModel):
    """Base that rejects unknown/coerced fields and is shallowly immutable."""

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, hide_input_in_errors=True
    )

    def __setattr__(self, name: str, value: Any) -> None:
        raise TypeError("contract is immutable")

    def __delattr__(self, name: str) -> None:
        raise TypeError("contract is immutable")

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        schema = handler(source)

        def validate_without_input_leak(
            value: Any, validator: core_schema.ValidatorFunctionWrapHandler
        ) -> Any:
            try:
                return validator(value)
            except ValidationError as exc:
                raise _sanitized_validation_error(cls.__name__, exc) from None

        return core_schema.no_info_wrap_validator_function(
            validate_without_input_leak, schema
        )


class EventSource(StrictContract):
    system: SourceSystem
    scope: NonBlank
    external_event_id: ExternalId | None = None


class EventSubject(StrictContract):
    kind: EntityKind
    external_id: ExternalId


class AccountHint(StrictContract):
    account_id: Annotated[UUID, Field(strict=False)] | None = None
    contact_email: EmailText | None = None
    domain: DomainText | None = None
    company_name: NonBlank | None = None

    @model_validator(mode="after")
    def require_one_hint(self) -> AccountHint:
        if all(
            value is None
            for value in (
                self.account_id,
                self.contact_email,
                self.domain,
                self.company_name,
            )
        ):
            raise ValueError("account hint must contain at least one identifier")
        return self


class Evidence(StrictContract):
    type: EvidenceType
    external_id: ExternalId
    uri: UriText | None = None
    content_hash: ContentHash | None = None


class EventEnvelope(StrictContract):
    """Schema-v1 event payload, bounded to 1 MiB of canonical UTF-8 JSON."""

    schema_version: Literal[1]
    event_type: NonBlank
    source: EventSource
    occurred_at: Annotated[datetime, Field(strict=False)]
    subject: EventSubject
    account_hint: AccountHint | None = None
    facts: dict[str, JsonValue]
    evidence: tuple[Evidence, ...] = Field(default_factory=tuple, max_length=1000)
    correlation_id: Annotated[UUID, Field(strict=False)] | None = None
    causation_id: Annotated[UUID, Field(strict=False)] | None = None

    @field_validator("evidence", mode="before")
    @classmethod
    def freeze_evidence_collection(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("occurred_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_json_payload_within_limit(self) -> EventEnvelope:
        try:
            canonical = self.canonical_json()
        except (TypeError, ValueError):
            raise ValueError("event payload must be JSON serializable") from None
        if len(canonical.encode("utf-8")) > MAX_CANONICAL_EVENT_BYTES:
            raise ValueError("event payload exceeds canonical size limit")
        return self

    def canonical_json(self) -> str:
        """Return deterministic UTF-8 JSON independent of mapping insertion order."""

        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    def payload_hash(self) -> str:
        """Return lowercase SHA-256 of the canonical payload."""

        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def persistence_payload(self) -> dict[str, Any]:
        """Return the validated JSON-compatible representation for JSONB storage."""

        return self.model_dump(mode="json")
