#!/usr/bin/env python3
"""Fail-closed verifier for custom-format CRM PostgreSQL backups.

The verifier restores into a newly created database on an explicitly marked local,
disposable PostgreSQL 16 server. Credentials are accepted only through an
environment variable and are never placed in a subprocess argument.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import psycopg
from psycopg import sql


_DISPOSABLE_MARKER = "CRM_DISPOSABLE_TEST_DATABASE"
_RESTORE_PREFIX = "crm_restore_verify_"
EXPECTED_SCHEMA_REVISION = "0010"
REQUIRED_TABLES = frozenset(
    {
        "workspaces",
        "accounts",
        "contacts",
        "leads",
        "activities",
        "ingest_events",
        "sync_checkpoints",
        "proposals",
        "outbox_events",
        "audit_events",
        "email_messages",
        "meetings",
        "tasks",
        "reconciliation_runs",
        "alembic_version",
    }
)
REQUIRED_CONSTRAINTS = frozenset(
    {
        "ck_email_messages_mailbox_identity",
        "ck_email_messages_to_addresses_minimized",
        "ck_email_messages_body_preview_bounded",
        "ck_meetings_next_steps_minimized",
        "ck_reconciliation_runs_report_minimized",
        "fk_email_messages_workspace_account",
        "fk_email_messages_workspace_mailbox_identity",
        "fk_email_messages_workspace_account_evidence",
        "fk_meetings_workspace_account",
        "fk_meetings_workspace_account_notes_evidence",
        "fk_tasks_workspace_account",
        "fk_tasks_workspace_account_lead",
        "fk_tasks_workspace_account_proposal",
        "ck_activities_outcome_code_nonblank",
        "uq_evidence_workspace_account_id_type",
        "uq_source_identities_workspace_id_semantics",
    }
)
REQUIRED_INDEXES = frozenset(
    {
        "uq_email_messages_workspace_mailbox_provider",
        "ix_email_messages_account_sent_at",
        "uq_meetings_workspace_provider_occurrence",
        "ix_meetings_account_scheduled",
        "ix_tasks_account_status_due",
        "ix_tasks_workspace_lead_status_due",
        "ix_reconciliation_runs_workspace_connector_started",
    }
)


class BackupVerificationError(RuntimeError):
    """Generic, non-secret-bearing verification failure."""


@dataclass(frozen=True, repr=False)
class SafeTarget:
    host: str
    port: int
    user: str
    password: str | None
    database: str

    @property
    def hostaddr(self) -> str:
        return "::1" if self.host == "::1" else "127.0.0.1"

    def connection_kwargs(self, *, database: str | None = None) -> dict[str, object]:
        values: dict[str, object] = {
            "host": self.host,
            "hostaddr": self.hostaddr,
            "port": self.port,
            "user": self.user,
            "dbname": database or self.database,
            "connect_timeout": 10,
        }
        if self.password is not None:
            values["password"] = self.password
        return values

    def subprocess_environment(self, database: str) -> dict[str, str]:
        environment = {
            "PGHOST": self.host,
            "PGHOSTADDR": self.hostaddr,
            "PGPORT": str(self.port),
            "PGUSER": self.user,
            "PGDATABASE": database,
        }
        if self.password is not None:
            environment["PGPASSWORD"] = self.password
        else:
            environment.pop("PGPASSWORD", None)
        return environment


def validate_safe_target(raw_url: str, *, disposable_marker: bool) -> SafeTarget:
    """Accept only an explicitly marked local database whose name says test."""

    try:
        parsed = urlsplit(raw_url)
        port = parsed.port or 5432
    except (TypeError, ValueError):
        raise BackupVerificationError("unsafe verification target") from None
    database = unquote(parsed.path.lstrip("/"))
    host = (parsed.hostname or "").casefold()
    user = unquote(parsed.username or "")
    if (
        disposable_marker is not True
        or parsed.scheme not in {"postgresql", "postgresql+psycopg"}
        or host not in {"127.0.0.1", "localhost", "::1"}
        or not user
        or not database
        or "test" not in database.casefold()
        or parsed.query
        or parsed.fragment
        or not 1 <= port <= 65535
    ):
        raise BackupVerificationError("unsafe verification target")
    return SafeTarget(
        host=host,
        port=port,
        user=user,
        password=unquote(parsed.password) if parsed.password is not None else None,
        database=database,
    )


def validate_backup_file(path: Path) -> Path:
    """Require a regular, non-empty PostgreSQL custom-format archive."""

    try:
        resolved = path.expanduser().resolve(strict=True)
        with resolved.open("rb") as handle:
            magic = handle.read(5)
    except (OSError, RuntimeError):
        raise BackupVerificationError("invalid backup input") from None
    if not resolved.is_file() or magic != b"PGDMP":
        raise BackupVerificationError("invalid backup input")
    return resolved


def _run(
    command: list[str], *, timeout: int, environment: dict[str, str] | None = None
) -> None:
    try:
        subprocess.run(
            command,
            check=True,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        raise BackupVerificationError("backup tool failed") from None


def _assert_postgres_16(connection: psycopg.Connection) -> None:
    version = int(connection.execute("SHOW server_version_num").fetchone()[0])
    if not 160000 <= version < 170000:
        raise BackupVerificationError("verification requires PostgreSQL 16")


def _drop_restore_database(target: SafeTarget, database: str) -> None:
    if not re.fullmatch(r"crm_restore_verify_[0-9a-f]{32}", database):
        raise BackupVerificationError("unsafe cleanup target")
    with psycopg.connect(**target.connection_kwargs(), autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(database)
            )
        )


def _smoke_restored_database(target: SafeTarget, database: str) -> dict[str, object]:
    with psycopg.connect(**target.connection_kwargs(database=database)) as connection:
        _assert_postgres_16(connection)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public'"
            )
        }
        if not REQUIRED_TABLES.issubset(tables):
            raise BackupVerificationError("restored schema is incomplete")
        revisions = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchall()
        if revisions != [(EXPECTED_SCHEMA_REVISION,)]:
            raise BackupVerificationError("restored migration revision is invalid")
        constraints = {
            row[0]
            for row in connection.execute(
                "SELECT conname FROM pg_catalog.pg_constraint "
                "WHERE connamespace = 'public'::regnamespace"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT indexname FROM pg_catalog.pg_indexes WHERE schemaname = 'public'"
            )
        }
        if not REQUIRED_CONSTRAINTS.issubset(
            constraints
        ) or not REQUIRED_INDEXES.issubset(indexes):
            raise BackupVerificationError("restored schema invariants are incomplete")
        workspace_count = int(
            connection.execute("SELECT count(*) FROM workspaces").fetchone()[0]
        )
        invariant_violations = int(
            connection.execute(
                """
                SELECT count(*)
                FROM accounts a
                WHERE a.merged_into_account_id IS NULL
                  AND EXISTS (
                    SELECT 1 FROM leads l
                    WHERE l.workspace_id = a.workspace_id
                      AND l.account_id = a.id
                      AND l.highest_stage_rank > a.highest_stage_rank
                  )
                """
            ).fetchone()[0]
        )
        orphan_count = int(
            connection.execute(
                """
                SELECT count(*) FROM (
                  SELECT a.workspace_id FROM accounts a
                  LEFT JOIN workspaces w ON w.id = a.workspace_id WHERE w.id IS NULL
                  UNION ALL
                  SELECT p.workspace_id FROM proposals p
                  LEFT JOIN workspaces w ON w.id = p.workspace_id WHERE w.id IS NULL
                  UNION ALL
                  SELECT e.workspace_id FROM ingest_events e
                  LEFT JOIN workspaces w ON w.id = e.workspace_id WHERE w.id IS NULL
                  UNION ALL
                  SELECT em.workspace_id FROM email_messages em
                  LEFT JOIN evidence ev
                    ON ev.workspace_id = em.workspace_id
                   AND ev.account_id = em.account_id
                   AND ev.id = em.evidence_id
                   AND ev.evidence_type = em.evidence_type
                  WHERE em.evidence_id IS NOT NULL AND ev.id IS NULL
                  UNION ALL
                  SELECT m.workspace_id FROM meetings m
                  LEFT JOIN evidence ev
                    ON ev.workspace_id = m.workspace_id
                   AND ev.account_id = m.account_id
                   AND ev.id = m.notes_evidence_id
                   AND ev.evidence_type = m.notes_evidence_type
                  WHERE m.notes_evidence_id IS NOT NULL AND ev.id IS NULL
                  UNION ALL
                  SELECT em.workspace_id FROM email_messages em
                  LEFT JOIN source_identities si
                    ON si.workspace_id = em.workspace_id
                   AND si.id = em.mailbox_identity_id
                   AND si.source_system = 'gmail'
                   AND si.entity_kind = 'mailbox'
                  WHERE si.id IS NULL
                ) AS orphans
                """
            ).fetchone()[0]
        )
        if invariant_violations or orphan_count:
            raise BackupVerificationError("restored CRM invariants failed")
        connection.execute("SET TRANSACTION READ ONLY")
        return {
            "status": "verified",
            "postgres_major": 16,
            "schema_revision": EXPECTED_SCHEMA_REVISION,
            "required_tables": len(REQUIRED_TABLES),
            "workspace_count": workspace_count,
            "invariant_violations": 0,
        }


def verify_backup(
    backup: Path, target: SafeTarget, *, timeout: int = 120
) -> dict[str, object]:
    """List, restore, smoke-test, and remove a backup verification database."""

    if not 10 <= timeout <= 900:
        raise BackupVerificationError("invalid verification timeout")
    archive = validate_backup_file(backup)
    pg_restore = shutil.which("pg_restore")
    if pg_restore is None:
        raise BackupVerificationError("backup tool unavailable")
    _run([pg_restore, "--list", str(archive)], timeout=timeout)

    restore_database = f"{_RESTORE_PREFIX}{uuid4().hex}"
    created = False
    try:
        with psycopg.connect(
            **target.connection_kwargs(), autocommit=True
        ) as connection:
            _assert_postgres_16(connection)
            connection.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(restore_database)
                )
            )
            created = True
        _run(
            [
                pg_restore,
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--dbname",
                restore_database,
                str(archive),
            ],
            timeout=timeout,
            environment=target.subprocess_environment(restore_database),
        )
        return _smoke_restored_database(target, restore_database)
    except BackupVerificationError:
        raise
    except (OSError, psycopg.Error, TypeError, ValueError):
        raise BackupVerificationError("backup verification failed") from None
    finally:
        if created:
            try:
                _drop_restore_database(target, restore_database)
            except (BackupVerificationError, psycopg.Error):
                raise BackupVerificationError("backup cleanup failed") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Restore-test a CRM PostgreSQL backup")
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument(
        "--target-url-env",
        required=True,
        help="name of the environment variable containing the disposable target URL",
    )
    parser.add_argument("--timeout", type=int, default=120)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        raw_url = os.environ.get(arguments.target_url_env, "")
        target = validate_safe_target(
            raw_url,
            disposable_marker=os.environ.get(_DISPOSABLE_MARKER) == "1",
        )
        result = verify_backup(arguments.backup, target, timeout=arguments.timeout)
    except BackupVerificationError as error:
        print(f"Backup verification failed: {error}", file=sys.stderr)
        return 1
    print(
        "Backup verified by PostgreSQL 16 restore: "
        f"schema={result['schema_revision']}, tables={result['required_tables']}, "
        f"workspaces={result['workspace_count']}, invariants=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
