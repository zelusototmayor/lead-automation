# CRM Decisions and Fail-Closed Defaults

This record separates approved implementation boundaries from business and operational decisions that still require real evidence before activation. A fail-closed default is not an approval to enable a feature.

## ADR-001: CRM exposure

Status: implemented fail-closed; deployment mapping not configured

- Keep only `GET /up` public and minimal.
- Protect the legacy dashboard, legacy GET APIs, same-origin static assets, Contas, Propostas, Inteligência, Operações and their APIs with the same server-configured browser principal.
- Derive username, workspace and admin role only from server configuration.
- Keep every protected read deny-only when the mapping is incomplete or malformed, before Sheet or PostgreSQL access.
- Keep human writes and agent ingress under their separate authorization boundaries; the browser principal does not replace bearer/CSRF or agent scope checks.

Evidence: `SECURITY_DECISION.md`, `SECURITY.md`, security tests and the safe deployment defaults.

## ADR-002: Proof of Won

Status: provisional fail-closed policy; business proof not approved for production

- Never infer `won` from `meeting_booked`, a proposal date or a probability.
- Automated connectors and agents do not generate `won` transitions in this release.
- The generic human command service is not exposed as an HTTP command route. Its audit record is not, by itself, official proof of `won`; do not activate that path for `won` until the commercial owner selects the required evidence and the service enforces it with tests.
- Contract, payment, manual confirmation or another proof remains unresolved until the commercial owner selects the official policy and the corresponding retention/access rules.

## ADR-003: Identity and account matching

Status: implemented for safe automatic matching; corporate-group policy unresolved

- Match by canonical ID, existing source identity, exact contact email, exact verified domain or another exact linked external identity.
- Never merge automatically by company name or fuzzy similarity.
- Duplicate, missing or ambiguous source identities enter review and remain outside automatic parity claims.
- Subsidiary and corporate-group consolidation remains disabled until an explicit policy exists.

## ADR-004: Commercial values and currencies

Status: implemented

- Unknown values remain `NULL/missing`; zero is valid only when explicitly confirmed with provenance.
- Keep one-off, MRR and ARR separate.
- Keep totals separated by ISO currency and do not perform implicit exchange-rate conversion.
- Candidate values do not enter confirmed portfolio totals.
- Mutually exclusive options contribute only when selected.

Tax treatment, discount policy and exchange-rate reporting remain unresolved and therefore are not inferred.

## ADR-005: Email, Calendar and meeting-note retention

Status: connectors disabled for production pending policy

- Store canonical metadata and minimized/redacted evidence references, not raw email bodies, full transcripts or attachments.
- Keep connector allowlists empty and workers disabled until mailbox/calendar scopes, retention periods, access basis and deletion procedures are approved.
- Missing policy blocks connector activation; local fixtures and non-delivering mocks do not substitute for it.

## ADR-006: Agent and outbound authority

Status: implemented as ingest-only and non-delivering

- Agent event ingress may persist an authenticated, scoped, idempotent ledger event only when explicitly enabled.
- Consequential commands remain human-controlled and version-checked.
- Outbox creation does not publish or send.
- CRM workers, reconcilers and tests must not send commercial/customer email.

## ADR-007: Cutover and legacy retirement

Status: blocked by external gates

- PostgreSQL becomes canonical only after an isolated staging deployment, real backup restore, repeatable real-data backfill/reconciliation, owner-validated sample, security/browser smoke and soak.
- Read and writer flags advance one dimension at a time and retain an image/flag rollback path.
- Legacy v0 contracts remain available while telemetry shows consumers.
- Task 19 requires two stable releases, an export path, absence of v0 consumers and stakeholder acceptance. Authorization to execute autonomously does not waive these technical and data-quality gates.

## ADR-008: Operational parity before canonical cutover

Status: contract recorded; implementation and owner acceptance incomplete

- The legacy dashboard is the minimum behavioral specification for daily pipeline management.
- PostgreSQL read visibility does not establish operational parity.
- Canonical cutover requires the queues, commands, task lifecycle, timeline, proposal updates, desktop flow and mobile flow recorded in `PIPELINE_PARITY.md`.
- Human browser writes must use narrow authenticated command endpoints with server-derived actor/workspace/permissions, optimistic versions, idempotency, immutable activity, audit and outbox in one transaction.
- The temporary `/leads` validation list is not the final daily workspace.
- No legacy removal or production writer cutover occurs before every matrix item is implemented, exercised in staging and accepted by José.

## Open activation evidence

The following must be recorded before production activation:

- concrete principal username, workspace and role mapping through the deployment secret/configuration path;
- official `won` proof and commercial owner reference;
- mailbox/calendar scopes and retention/access policy;
- production PostgreSQL capacity, automatic backup identifier and restore result for the exact archive;
- isolated staging URL/environment, deployed SHA/image digest and rollback image;
- first/second real backfill and reconciliation counts with conflicts resolved or explicitly accepted;
- owner validation reference for a representative account/proposal sample;
- browser/security smoke, operational metrics and soak interval;
- two-release evidence and v0-consumer telemetry before legacy removal.

Do not place credentials, personal data, customer payloads or raw source values in this file.
