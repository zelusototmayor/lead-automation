# CRM Rollback Runbook

## First response

1. Stop expansion immediately.
2. Pause connectors, event consumers, command workers, outbox publishers and agent-event processing.
3. Capture the deployed git SHA/image digest, feature flags, Alembic revision, UTC incident window and aggregate operational metrics.
4. Do not delete the event ledger, audit log, checkpoints, evidence, proposal versions or outbox rows.

## Read rollback

Switch Contas and Propostas reads back to `legacy`. Disable PostgreSQL-backed rich surfaces if the trusted identity boundary is uncertain. Keep additive PostgreSQL data for diagnosis.

## Connector rollback

Disable the affected connector and preserve its last committed checkpoint. Do not rewind a checkpoint or replay a range until duplicate/conflict behavior has been verified in staging. Existing materialized data may remain visible with a stale-data indication.

## Writer rollback

If `CRM_COMMAND_WRITER=postgres` has ever accepted writes, do not switch blindly back to Sheet writes. First:

1. pause all writers;
2. inspect pending/failed outbox counts without exposing payloads;
3. project or reconcile committed PostgreSQL changes to the legacy surface;
4. compare aggregate state;
5. resume a single writer only after divergence is resolved.

## Image rollback

An earlier image may be deployed only if it is compatible with the current additive schema. Do not downgrade the production schema during the incident. The migrations in this release are additive and their down revisions are for disposable lifecycle verification, not routine production rollback.

## Database recovery

Restore only from a backup whose exact archive has passed `scripts/crm_verify_backup.py` on PostgreSQL 16. Restore into a new database/service first, run smoke and invariants, then use the infrastructure-approved promotion path. Never overwrite the only copy of the affected database.

## Legacy Sheet export and recovery

Before any legacy retirement release, create a read-only export outside the repository with owner-only permissions and record its timestamp, source spreadsheet ID, row/column counts and SHA-256 without recording cell values. Prefer the native Google workbook export when the approved OAuth principal has Drive export scope; otherwise retain a complete Sheets API values export and record that formulas/formatting are not preserved.

Treat every export as commercial data: keep it outside Git, use mode `0600`, do not print or attach its contents to logs/tickets, and follow the approved retention policy. Verify the checksum before use.

Never restore over the live Sheet. Create a new restricted spreadsheet, import into that disposable recovery copy, compare headers, row counts and a representative owner-validated sample, then explicitly decide whether to promote the copy or keep the live Sheet. Pause CRM writers/outbox projection throughout the exercise. A successful API parse is not a stakeholder-approved restore.

## Exit criteria

Resume only after:

- the divergence interval and affected aggregates are known;
- account, idempotency, checkpoint, outbox and audit invariants pass;
- rich routes remain protected;
- the selected writer/read model is explicit;
- a fresh backup and restore test succeed;
- staging smoke and the relevant regression suites pass.
