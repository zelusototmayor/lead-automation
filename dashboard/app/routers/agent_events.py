"""Authenticated, scoped and idempotent agent event ingress."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from dashboard.app.config import AgentSettings, get_agent_settings
from dashboard.app.db import create_database_engine, create_session_factory
from dashboard.app.feature_flags import require_agent_events_enabled
from src.crm.ingestion.checkpoints import (
    IdempotencyConflictError,
    InvalidIngestionInputError,
    record_ingest_event,
)
from src.crm.ingestion.contracts import EventEnvelope, MAX_CANONICAL_EVENT_BYTES


router = APIRouter()
_AUTH_WINDOW = timedelta(minutes=5)
_UNAUTHORIZED = {"detail": "Unauthorized"}
_FORBIDDEN = {"detail": "Forbidden"}
_INVALID = {"detail": "Invalid request"}


def get_agent_event_session():
    """Yield an uncommitted session; request handling owns its transaction."""

    factory = create_session_factory(create_database_engine())
    with factory() as session:
        yield session


def _response(status_code: int, content: dict[str, object]) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=content)


def _authenticate(request: Request) -> AgentSettings | JSONResponse:
    try:
        settings = get_agent_settings()
    except ValueError:
        return _response(401, _UNAUTHORIZED)

    authorization = request.headers.get("authorization", "")
    scheme, separator, provided = authorization.partition(" ")
    try:
        token_matches = (
            separator == " "
            and scheme.lower() == "bearer"
            and secrets.compare_digest(
                provided.encode("ascii"),
                settings.bearer_token.get_secret_value().encode("ascii"),
            )
        )
    except (TypeError, UnicodeEncodeError):
        token_matches = False

    now = datetime.now(UTC)
    try:
        request_time = datetime.fromisoformat(
            request.headers.get("x-agent-timestamp", "").replace("Z", "+00:00")
        )
        timestamp_valid = (
            request_time.tzinfo is not None
            and request_time.utcoffset() is not None
            and abs(now - request_time.astimezone(UTC)) <= _AUTH_WINDOW
        )
    except (TypeError, ValueError, OverflowError):
        timestamp_valid = False

    if not (
        token_matches
        and timestamp_valid
        and settings.token_issued_at - _AUTH_WINDOW <= now < settings.token_expires_at
    ):
        return _response(401, _UNAUTHORIZED)
    return settings


async def _bounded_json(request: Request) -> object:
    content_length = request.headers.get("content-length")
    try:
        if (
            content_length is not None
            and int(content_length) > MAX_CANONICAL_EVENT_BYTES
        ):
            raise ValueError
    except ValueError:
        raise ValueError("invalid request") from None

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_CANONICAL_EVENT_BYTES:
            raise ValueError("invalid request")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid request") from None


@router.post(
    "/api/v1/agent-events",
    dependencies=[Depends(require_agent_events_enabled)],
)
async def create_agent_event(
    request: Request,
    session: Annotated[Session, Depends(get_agent_event_session)],
) -> JSONResponse:
    principal = _authenticate(request)
    if isinstance(principal, JSONResponse):
        return principal
    if "agent-events:write" not in principal.scopes:
        return _response(403, _FORBIDDEN)

    try:
        raw_payload = await _bounded_json(request)
        envelope = EventEnvelope.model_validate(raw_payload)
        idempotency_key = request.headers.get("idempotency-key")
        if not idempotency_key:
            raise ValueError("invalid request")
    except (ValidationError, TypeError, ValueError, RecursionError):
        return _response(422, _INVALID)

    if (
        envelope.source.system != "agent"
        or envelope.source.scope not in principal.source_scopes
    ):
        return _response(403, _FORBIDDEN)

    try:
        with session.begin():
            result = record_ingest_event(
                session,
                principal.workspace_id,
                idempotency_key,
                envelope,
            )
    except InvalidIngestionInputError:
        return _response(422, _INVALID)
    except IdempotencyConflictError:
        return _response(409, {"detail": "Idempotency conflict"})
    except Exception:
        session.rollback()
        return _response(500, {"detail": "Internal server error"})

    return _response(
        200 if result.duplicate else 202,
        {
            "event_id": str(result.event_id),
            "status": result.processing_status,
            "duplicate": result.duplicate,
        },
    )
