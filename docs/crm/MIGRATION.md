# CRM Migration and Cutover

## Principle

The migration is additive, shadow-first and reversible. Google Sheets remains untouched during snapshot/backfill. PostgreSQL becomes canonical only after the data, security, restore, staging and soak gates pass.

## Pre-flight gates

Before any staging or production activation, record all of the following with real evidence:

- trusted server-side identity/session adapter for rich pages and APIs;
- approved users/roles and workspace mapping;
- approved proof of `Won`, retention policy and connector scopes;
- production PostgreSQL provisioning, automatic backup policy and successful restore test;
- current production commit/image and a compatible rollback image;
- outbound email jobs and outbox publishers paused for migration windows;
- no secrets or customer payloads in commands, logs or reports.

A missing gate blocks activation. Local passing tests do not substitute for real-data owner validation.

## Shadow migration sequence

1. Back up the live database, if one exists, and restore-test that exact archive.
2. Pause connector consumers, agent-event processing, command workers and outbox publishing.
3. Apply additive migrations to a staging copy and record duration/locks.
4. Run the account and proposal backfills in dry-run mode.
5. Review aggregate duplicate, conflict, unmapped-stage, missing-evidence and missing-value reports.
6. Run apply against staging only, then repeat the identical input. The second run must create zero domain duplicates.
7. Run reconciliation twice and confirm stable counts/checkpoints.
8. Verify every lead at rank 40 or later has a valid account.
9. Validate a representative real sample with the commercial owner. Unknown values remain `NULL/missing`; `Won` never becomes `Meeting Booked`.
10. Run security, unit, integration, contract, migration and browser smoke suites.
11. Enable PostgreSQL reads in staging one area at a time: Contas, Propostas, Inteligência.
12. Soak with connectors disabled, then enable one allowlisted connector at a time in shadow mode.
13. Only after stable shadow evidence, switch the command writer to PostgreSQL while keeping the Sheets projection reversible.

## Required cutover flags

The implementation uses validated flags documented by Task 18:

- `CRM_DB_ENABLED`
- `CRM_ACCOUNTS_READ_MODEL=legacy|shadow|postgres`
- `CRM_PROPOSALS_READ_MODEL=legacy|shadow|postgres`
- `CRM_COMMAND_WRITER=sheet|postgres`
- `CRM_SHEETS_PROJECTION_ENABLED`
- `CRM_AGENT_EVENTS_ENABLED`

Unknown or inconsistent combinations must fail closed. Staging starts with DB/connectors/agent events/writes disabled and legacy reads selected.

## Verification record

For each environment, record without secrets or PII:

- environment and timestamp;
- git SHA and image digest;
- Alembic revision and migration duration;
- backup identifier and restore-test result;
- first and second backfill/reconcile aggregate counts;
- security/test commands and exit status;
- sampled-record approval reference;
- flag values before and after cutover;
- smoke/browser result, operational metrics and soak duration;
- rollback image and tested rollback steps.

## Current status

As of 2026-07-16, migrations through `0006`, backfills, connectors and restore tooling have been exercised only against disposable local PostgreSQL. No production/staging database, real connector, real-data sample, live identity adapter, deployment or cutover has been verified from this branch.
