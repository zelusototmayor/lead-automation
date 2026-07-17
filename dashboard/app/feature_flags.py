"""Validated, import-safe cutover controls for the additive CRM migration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, cast

from fastapi import HTTPException, status

ReadModel = Literal["legacy", "shadow", "postgres"]
CommandWriter = Literal["sheet", "postgres"]


@dataclass(frozen=True, slots=True)
class CRMFeatureFlags:
    database_enabled: bool
    accounts_read_model: ReadModel
    proposals_read_model: ReadModel
    command_writer: CommandWriter
    sheets_projection_enabled: bool
    agent_events_enabled: bool


def _boolean(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ValueError(f"invalid {name} configuration")


def _choice(name: str, *, default: str, allowed: frozenset[str]) -> str:
    value = os.getenv(name, default)
    if value not in allowed:
        raise ValueError(f"invalid {name} configuration")
    return value


def load_feature_flags() -> CRMFeatureFlags:
    """Read flags without I/O and reject unsafe or internally inconsistent states."""

    database_enabled = _boolean("CRM_DB_ENABLED", default=False)
    accounts_read_model = _choice(
        "CRM_ACCOUNTS_READ_MODEL",
        default="legacy",
        allowed=frozenset({"legacy", "shadow", "postgres"}),
    )
    proposals_read_model = _choice(
        "CRM_PROPOSALS_READ_MODEL",
        default="legacy",
        allowed=frozenset({"legacy", "shadow", "postgres"}),
    )
    command_writer = _choice(
        "CRM_COMMAND_WRITER",
        default="sheet",
        allowed=frozenset({"sheet", "postgres"}),
    )
    sheets_projection_enabled = _boolean("CRM_SHEETS_PROJECTION_ENABLED", default=False)
    agent_events_enabled = _boolean("CRM_AGENT_EVENTS_ENABLED", default=False)

    if not database_enabled and (
        accounts_read_model != "legacy"
        or proposals_read_model != "legacy"
        or command_writer != "sheet"
        or sheets_projection_enabled
        or agent_events_enabled
    ):
        raise ValueError("unsafe CRM cutover configuration")
    if sheets_projection_enabled and command_writer != "postgres":
        raise ValueError("unsafe CRM cutover configuration")

    return CRMFeatureFlags(
        database_enabled=database_enabled,
        accounts_read_model=cast(ReadModel, accounts_read_model),
        proposals_read_model=cast(ReadModel, proposals_read_model),
        command_writer=cast(CommandWriter, command_writer),
        sheets_projection_enabled=sheets_projection_enabled,
        agent_events_enabled=agent_events_enabled,
    )


@lru_cache(maxsize=1)
def get_feature_flags() -> CRMFeatureFlags:
    return load_feature_flags()


def _flags_or_unavailable(*, detail: str) -> CRMFeatureFlags:
    try:
        return get_feature_flags()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail
        ) from None


def require_database_enabled(*, detail: str) -> CRMFeatureFlags:
    flags = _flags_or_unavailable(detail=detail)
    if not flags.database_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
        )
    return flags


def require_accounts_postgres_reads() -> None:
    flags = require_database_enabled(detail="Accounts unavailable")
    if flags.accounts_read_model != "postgres":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Accounts unavailable",
        )


def require_proposals_postgres_reads() -> None:
    flags = require_database_enabled(detail="Proposals unavailable")
    if flags.proposals_read_model != "postgres":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Proposals unavailable",
        )


def require_agent_events_enabled() -> None:
    try:
        flags = get_feature_flags()
    except ValueError:
        flags = None
    if flags is None or not flags.database_enabled or not flags.agent_events_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def require_legacy_sheet_writer() -> None:
    flags = _flags_or_unavailable(detail="Writer unavailable")
    if flags.command_writer != "sheet":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Writer unavailable",
        )
