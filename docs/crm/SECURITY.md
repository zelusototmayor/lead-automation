# CRM Security and Privacy Controls

## Exposure policy

The approved implementation boundary is public minimal health/legacy aggregate compatibility plus protected rich detail and protected writes. Rich Contas, Propostas, Inteligência and operations use a minimal HTTP Basic adapter for one server-configured principal. Agent and write surfaces retain their separate authentication contracts. Request parameters, headers and browser cookies do not select a workspace or role.

## Required controls

- derive workspace and role from a trusted server-side principal;
- require all of `CRM_PRINCIPAL_USERNAME`, `CRM_PRINCIPAL_PASSWORD`, `CRM_PRINCIPAL_WORKSPACE_ID` and `CRM_PRINCIPAL_IS_ADMIN`; accept exactly `true` or `false` for the role flag;
- return generic `403` with no Basic challenge for malformed/missing principal configuration, and generic `401` with `WWW-Authenticate: Basic` for missing, malformed or wrong credentials after configuration is valid;
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

## Rich-route identity boundary

The Basic adapter is invoked only by the existing `require_crm_principal` dependency on protected rich HTML and `/api/v1/*` routes. It is not middleware and does not alter `/up`, the legacy dashboard, legacy APIs, human write authentication or agent bearer authentication. It creates no cookie, session or token endpoint and sends no configured value to HTML or JavaScript. Credential comparison is constant-time over safely encoded ASCII bytes; malformed/non-ASCII credential bytes are treated as a mismatch rather than raising.

The deployment password is secret. Username, workspace UUID and admin flag are an explicit non-secret server mapping. Settings are process-cached: rotate/remap through the deployment store, restart every instance, confirm old credentials fail, and confirm the new principal maps to the intended workspace/role. Incomplete or malformed mapping must continue to return generic `403` without a browser challenge loop.

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

Production activation remains blocked until concrete principal values, retention/access policy, real backup/restore, secret scan, staging security/browser checks and owner validation are complete. The repository now contains the scoped adapter, but checked-in deployment mapping placeholders are intentionally incomplete, so protected rich routes remain fail-closed by default. No production secret was read or written and no deployment was performed.
