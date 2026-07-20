# CRM Migration and Cutover

## Principle

The migration is additive, shadow-first and reversible. Google Sheets remains untouched during snapshot/backfill. PostgreSQL becomes canonical only after the data, security, restore, staging and soak gates pass.

## Pre-flight gates

Before any staging or production activation, record all of the following with real evidence. `DECISIONS.md` is the canonical status record for approved boundaries, fail-closed defaults and unresolved activation decisions:

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
4. Create or verify the single canonical workspace row and record its non-secret UUID. The `--workspace-id` passed to backfills must already exist; a random UUID alone is not sufficient and foreign-key failure must not be mistaken for a data conflict.
5. Run the account and proposal backfills in dry-run mode.
6. Review aggregate duplicate, conflict, unmapped-stage, missing-evidence and missing-value reports.
7. Run apply against staging only, then repeat the identical input. The second run must create zero domain duplicates.
8. Run reconciliation twice and confirm stable counts/checkpoints.
9. Verify every lead at rank 40 or later has a valid account.
10. Validate a representative real sample with the commercial owner. Unknown values remain `NULL/missing`; `Won` never becomes `Meeting Booked`.
11. Run security, unit, integration, contract, migration and browser smoke suites.
12. Enable PostgreSQL reads in staging one area at a time: Contas, Propostas, Inteligência.
13. Soak with connectors disabled, then enable one allowlisted connector at a time in shadow mode.
14. Only after stable shadow evidence, switch the command writer to PostgreSQL while keeping the Sheets projection reversible.

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

As of 2026-07-20, migrations through `0007`, backfills, connectors, restore tooling, guarded cutover flags and the scoped browser-read identity adapter have passed the safe suite, PostgreSQL lifecycle, real-Sheet replay, backup restore and local/container smoke gates.

A persistent, logically isolated staging environment is now running at the final TLS/proxy hostname on the shared droplet. PostgreSQL 16 uses a dedicated container, volume and network; the application image is built from the exact feature-branch SHA recorded in `CURRENT_STATE.md`. Contas and Propostas read PostgreSQL, while the command writer remains `sheet` and Sheets projection, Agent ingress, connectors, workers and outbox publishing remain disabled. The real snapshot was applied twice: the second account and proposal backfills created zero new records. Authenticated HTTP and browser smoke pass, unauthenticated rich routes return `401`, `/up` remains public, and an exact staging backup has been copied off-host with mode `0600` and successfully restore-verified on a separate PostgreSQL 16 instance.

The protected PostgreSQL read surface now also includes a compact `/leads` operational list backed by `/api/v1/leads`; the root route redirects there when the legacy adapter is unavailable. The exact deployed SHA, browser evidence, aggregate counts and additional soak are recorded in `CURRENT_STATE.md`.

Staging does not satisfy the remaining data and production gates by itself. Real-data comparison is still non-parity because unresolved identities/accounts remain in review; the representative sample has not been approved by the commercial owner; official `Won` evidence and connector retention/scopes remain unresolved; production principal/workspace mapping and an automatic off-host production backup do not exist; and production cutover/soak has not started. Current aggregate proxy telemetry still shows active v0 consumers. Commit `9e6a95c` therefore adds only non-destructive coexistence markers: legacy `/api/*` responses advertise `Deprecation: true` and emit route-template/status telemetry without query strings, dynamic unmatched paths, headers or payloads. It does not invent a Sunset date, remove a contract or change auth/status/body behavior. Task 19 retirement remains blocked until two stable post-cutover releases, absence of v0 consumers, export availability and stakeholder acceptance are real evidence rather than assumptions.
