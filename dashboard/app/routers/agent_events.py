"""Authenticated, scoped and idempotent agent event ingress."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
import json
import secrets
from threading import Lock
import time
from typing import Annotated, Mapping
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
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
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_RATE_LIMIT_MAX_REQUESTS = 60
_rate_limit_buckets: dict[tuple[UUID, str], deque[float]] = {}
_rate_limit_lock = Lock()
_UNAUTHORIZED = {"detail": "Unauthorized"}
_FORBIDDEN = {"detail": "Forbidden"}
_INVALID = {"detail": "Invalid request"}


def _response(
    status_code: int,
    content: Mapping[str, object],
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=content, headers=headers)


def _consume_rate_limit(workspace_id: UUID, endpoint: str) -> bool:
    """Consume one request from the process-local principal/endpoint window."""

    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
    key = (workspace_id, endpoint)
    with _rate_limit_lock:
        for bucket_key, bucket in tuple(_rate_limit_buckets.items()):
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if not bucket:
                _rate_limit_buckets.pop(bucket_key, None)

        bucket = _rate_limit_buckets.setdefault(key, deque())
        if len(bucket) >= _RATE_LIMIT_MAX_REQUESTS:
            return False
        bucket.append(now)
        return True


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


def require_agent_event_principal(request: Request) -> AgentSettings:
    """Authenticate and authorize ingress before any database dependency runs."""

    principal = _authenticate(request)
    if isinstance(principal, JSONResponse):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if "agent-events:write" not in principal.scopes:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not _consume_rate_limit(principal.workspace_id, request.url.path):
        raise HTTPException(
            status_code=429,
            detail="Too many requests",
            headers={"Retry-After": str(int(_RATE_LIMIT_WINDOW_SECONDS))},
        )
    return principal


def get_agent_event_session(
    _principal: Annotated[AgentSettings, Depends(require_agent_event_principal)],
):
    """Yield an uncommitted session after agent authorization succeeds."""

    factory = create_session_factory(create_database_engine())
    with factory() as session:
        yield session


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
    principal: Annotated[AgentSettings, Depends(require_agent_event_principal)],
    session: Annotated[Session, Depends(get_agent_event_session)],
) -> JSONResponse:
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
