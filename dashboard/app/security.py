"""Fail-closed authorization for server-side CRM write clients."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, Request, status

from .config import get_settings

_FORBIDDEN = "Forbidden"


@dataclass(frozen=True)
class CRMPrincipal:
    """Trusted server-side identity; workspace never comes from request input."""

    workspace_id: UUID
    subject: str
    is_admin: bool = False


def _matches(provided: str | None, expected: str | None) -> bool:
    if provided is None or expected is None:
        return False
    try:
        return secrets.compare_digest(
            provided.encode("ascii"), expected.encode("ascii")
        )
    except (UnicodeEncodeError, TypeError):
        return False


async def require_write_access(request: Request) -> None:
    """Require bearer, CSRF, and (when supplied) an approved exact Origin."""
    try:
        settings = get_settings()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=_FORBIDDEN
        ) from None

    authorization = request.headers.get("authorization", "")
    scheme, separator, bearer = authorization.partition(" ")
    valid_bearer = (
        separator == " "
        and scheme.lower() == "bearer"
        and bool(bearer)
        and _matches(bearer, settings.write_token)
    )
    valid_csrf = _matches(request.headers.get("x-csrf-token"), settings.csrf_token)
    origin = request.headers.get("origin")
    valid_origin = origin is None or origin in settings.allowed_write_origins

    if not (valid_bearer and valid_csrf and valid_origin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_FORBIDDEN)

    from .feature_flags import require_legacy_sheet_writer

    require_legacy_sheet_writer()


async def require_crm_principal() -> CRMPrincipal:
    """Deny until deployment supplies a trusted authentication adapter."""

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_FORBIDDEN)
