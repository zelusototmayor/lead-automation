# CRM Security and Privacy Controls

## Exposure policy

The approved implementation boundary is public minimal health/legacy aggregate compatibility plus protected rich detail and protected writes. Rich Contas, Propostas, Inteligência, operations and agent surfaces fail closed until a trusted server-side identity adapter is configured. Request parameters, headers and browser cookies do not select a workspace.

## Required controls

- derive workspace and role from a trusted server-side principal;
- require admin role for operations metrics;
- require authorization and CSRF for human writes;
- require short-lived scoped server credentials, timestamp freshness and idempotency for agent events;
- return `404`/`403` for cross-workspace access without revealing entity existence;
- add `Cache-Control: no-store` to protected HTML and APIs;
- keep `/up` minimal;
- use strict pagination and bounded evidence/timeline detail;
- store and expose evidence references, not raw email bodies, notes, attachments or event payloads;
- keep database, Google, registry, write and CSRF credentials in the deployment secret store only;
- redact errors and logs; never include connection URLs, tokens, payloads or external identity values;
- keep audit/evidence append-only and use optimistic concurrency for consequential commands;
- keep outbound sending outside this release.

## Operations endpoint

`/operacoes` and `/api/v1/operations/metrics` require `CRMPrincipal.is_admin is True`, use only that principal's workspace, return aggregate metrics, and do not accept request-controlled tenant selection. Database/configuration failures return a generic `503`.

## Backup verifier boundary

The restore verifier:

- requires `CRM_DISPOSABLE_TEST_DATABASE=1`;
- accepts only loopback PostgreSQL URLs with a database name containing `test`;
- rejects URL queries/fragments that could override the effective destination;
- accepts only custom-format archives;
- passes credentials through subprocess environment, never command arguments;
- creates and removes only a random `crm_restore_verify_<uuid>` database;
- suppresses tool output that could contain object names or data.

It must never be used against a production host.

## Deployment blockers

Production activation remains blocked until the identity adapter, roles, retention/access policy, real backup/restore, secret scan, staging security/browser checks and owner validation are complete. The repository contains no production session adapter and therefore protected rich routes remain deny-only by default.
