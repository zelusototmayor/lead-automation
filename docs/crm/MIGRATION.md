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

## Legacy Sheet identity and schema adapter

The observed `PT Logistics` tab has a declared `ID` column but all 1,247 values were blank. The snapshot command therefore remains strict by default and supports an explicit, ordered fallback only when the operator supplies `--fallback-identity` groups. The observed safe shadow command uses:

```text
--stable-id-column ID \
--fallback-identity Email \
--fallback-identity Phone \
--fallback-identity Website,Company \
--fallback-identity Company,Contact
```

Derived IDs are source-scoped SHA-256 identifiers. They survive row movement and unrelated note edits; ambiguous duplicate evidence and rows without a complete fallback group remain conflicts and are never guessed from row number or company name alone. Snapshot files contain commercial data and must remain mode `0600`, outside the repository, with deletion after the migration evidence has been retained.

The real tab uses `Stage` and `Contact`; the migration adapter accepts them as explicit legacy column names for `Status` and `Contact Name`. Unknown stage values remain review items. `YYYY/MM/DD` is accepted for the observed `Proposal Sent` dates. A nonblank legacy proposal date elevates the effective account-backfill stage to at least `proposal_sent`, while preserving the raw stage and keeping the proposal `legacy_unverified` with `NULL/missing` value until evidence review.

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

`dashboard.app.feature_flags` parses the six controls without database or network I/O. Only the literal boolean strings `true` and `false` are accepted. Unknown values and inconsistent combinations stop application startup; request-level gates return a generic unavailable/not-found response without opening PostgreSQL or parsing an agent payload.

Safe deployment baseline:

```text
CRM_DB_ENABLED=false
CRM_ACCOUNTS_READ_MODEL=legacy
CRM_PROPOSALS_READ_MODEL=legacy
CRM_COMMAND_WRITER=sheet
CRM_SHEETS_PROJECTION_ENABLED=false
CRM_AGENT_EVENTS_ENABLED=false
```

Activation invariants:

- `shadow` and `postgres` read models require `CRM_DB_ENABLED=true`;
- `shadow` is comparison-only and does not serve PostgreSQL records to user traffic;
- `/contas` and `/propostas` use PostgreSQL only when their read model is exactly `postgres`;
- `CRM_COMMAND_WRITER=postgres` disables every legacy Sheet mutation before the Sheet adapter is called;
- Sheets projection requires both PostgreSQL enabled and `CRM_COMMAND_WRITER=postgres`;
- agent ingress requires both PostgreSQL and `CRM_AGENT_EVENTS_ENABLED=true`; when disabled it is hidden as `404` before auth, body parsing or session creation;
- any invalid combination fails closed rather than silently reverting one flag.

Progression is one dimension at a time: enable DB, select one area as `shadow`, obtain stable compare evidence, switch that area to `postgres`, then repeat for the next area. Writer and agent flags remain off until their independent gates pass.

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

As of 2026-07-17, migrations through `0006`, backfills, connectors, restore tooling and guarded cutover flags have been exercised against disposable local PostgreSQL. The relevant CRM suite passes locally. A read-only real-Sheet shadow run proved replay idempotency for applicable rows but still has unresolved identity/account conflicts. The observed live dashboard still runs commit/image `7622a2b` without PostgreSQL; no staging environment, owner-approved sample, identity adapter, soak or production cutover has been verified from this branch. Aggregate proxy telemetry also shows active v0 API consumers, so Task 19 retirement is blocked independently of the cutover gates. These are blocking gates, not optional follow-up work.
