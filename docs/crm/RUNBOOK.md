# CRM Operations Runbook

## Scope

This runbook covers the PostgreSQL-backed CRM introduced on `feat/crm-accounts-proposals-v1`. It does not authorize live activation. Rich reads, writes, connectors, workers and agent events remain fail-closed until a trusted identity adapter and the cutover gates in `MIGRATION.md` pass.

## Safe operating defaults

- `scripts/crm_reconcile.py` and `scripts/crm_worker.py` are dry-run by default.
- Do not run an outbox publisher during migrations, restores or reconciliation tests.
- Keep connectors and agent events disabled until their individual allowlists and checkpoints are verified.
- Never point tests, Alembic lifecycle checks, restore verification or fixtures at production.
- Do not print `DATABASE_URL`, Google credentials, bearer tokens, payloads or customer identifiers.

## Health surfaces

`GET /up` is the only minimal public health check. It reports only `{"status":"ok"}`.

`GET /operacoes` and `GET /api/v1/operations/metrics` require a trusted server-side `CRMPrincipal` with `is_admin=True`. They are workspace-scoped, return `Cache-Control: no-store`, and expose aggregate operational values only:

- database reachability;
- oldest pending event age;
- oldest successful checkpoint age;
- dead-letter count;
- reconciliation/review count;
- proposals with missing value;
- account invariant violations;
- oldest pending outbox age.

An account invariant violation is a lead at rank 40 or later without an account, or an associated account whose recorded highest rank trails the lead. Unknown ages are `null`.

## Triage order

1. Confirm the deployed image/commit and current feature flags.
2. Pause connectors, event consumers, command workers and outbox publishing before changing schema or replaying data.
3. Check `/up`, then the admin-only operations metrics for the affected workspace.
4. Inspect aggregate counts and redacted logs. Do not dump event payloads into tickets or chat.
5. If schema state is uncertain, run `alembic current` and `alembic check` against the intended environment through the approved secret injection path.
6. Prefer disabling a feature flag or connector over deleting data.
7. Follow `ROLLBACK.md` before reverting an image or writer.

## Backup verification

A backup is valid only after a restore test. `scripts/crm_verify_backup.py` accepts PostgreSQL custom-format archives and restores them into a new random database on an explicitly marked local PostgreSQL 16 test server. It rejects remote hosts, query-string host overrides, non-test database names and missing disposable markers.

Example with secrets supplied only through environment variables:

```bash
export CRM_DISPOSABLE_TEST_DATABASE=1
# The URL must name an existing local test database on the disposable server;
# the verifier uses it only to create and remove a random restore database.
export CRM_RESTORE_TEST_URL='postgresql+psycopg://USER:PASSWORD@127.0.0.1:PORT/crm_test'
.venv311/bin/python scripts/crm_verify_backup.py \
  --backup /secure/path/crm.dump \
  --target-url-env CRM_RESTORE_TEST_URL
```

The verifier checks archive readability, PostgreSQL major version, current Alembic head (`0010`), required tables, workspace count, orphan references and account invariants, then force-drops only its generated `crm_restore_verify_<uuid>` database.

## Observed local evidence

On 2026-07-16, a custom-format dump of a disposable PostgreSQL 16 database migrated to `0006` was restored by the verifier. The smoke result was:

```text
Backup verified by PostgreSQL 16 restore: schema=0006, tables=11, workspaces=0, invariants=0
```

The temporary archive and generated restore database were removed. This proves the local restore path only; it is not evidence that production backups exist or are restorable.

On 2026-07-18, the verifier restored a fresh custom-format dump migrated to the current head after a complete `0007 -> base -> 0007` lifecycle:

```text
Backup verified by PostgreSQL 16 restore: schema=0007, tables=15, workspaces=0, invariants=0
```

The archive and generated restore database were removed. Production backup existence and restore remain separate deployment gates.
