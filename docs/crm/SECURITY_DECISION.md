# CRM dashboard security decision

## Decision

Public, read-only access is retained only for the existing legacy dashboard surface. The new PostgreSQL-backed rich routes for Contas, Propostas, Inteligência and Operações are not covered by that legacy exposure decision. They use a scoped HTTP Basic adapter whose username, password, workspace UUID and admin role come exclusively from server configuration. The routes remain fail-closed until all four values are configured and verified.

This boundary does **not** authorize public writes, agent endpoints, deployment or broader disclosure. No deployment is authorized by this historical decision; deployment authorization is separate from the technical identity, data and rollback gates.

## Public/private boundary

Public, unauthenticated access is intentionally retained for:

- `GET /up`, returning only `{ "status": "ok" }`;
- the existing legacy browser dashboard and redirects;
- existing legacy `GET` dashboard APIs only while they remain within the previously accepted surface.

Protected rich reads include `/contas`, `/propostas`, `/inteligencia`, `/operacoes` and their `/api/v1/*` APIs. They derive workspace and role only from the trusted server-side principal. Missing or malformed server configuration returns a generic `403` without an authentication challenge. Once configuration is valid, missing, malformed or wrong browser credentials return a generic `401` with `WWW-Authenticate: Basic`; valid credentials produce a principal with the configured workspace UUID, configured username as subject, and configured admin flag. Query parameters, headers, cookies and request bodies cannot select workspace or role.

The required variables are `CRM_PRINCIPAL_USERNAME`, `CRM_PRINCIPAL_PASSWORD`, `CRM_PRINCIPAL_WORKSPACE_ID` and `CRM_PRINCIPAL_IS_ADMIN`. The admin value accepts exactly `true` or `false`. Only the password belongs in the deployment secret list; the username, workspace mapping and role are non-secret deployment configuration. The checked-in deployment values are intentionally incomplete placeholders, so they do not enable rich routes.

The public browser interface is read-only. Every existing human write endpoint is private:

- `POST /api/log-call`
- `POST /api/update-lead`
- `POST /api/mark-email-followup`
- `POST /api/mark-proposal-followup`
- `POST /api/update-proposal`
- `POST /api/mark-email-sent`
- `POST /api/refresh`

Each private write requires both a server-managed bearer token (`CRM_WRITE_TOKEN`) and a server-managed CSRF token (`CRM_CSRF_TOKEN`). If an `Origin` header is present, it must exactly match a validated entry in `CRM_ALLOWED_WRITE_ORIGINS`. Clients without an `Origin` are supported only when both credentials are valid. Credentials are never delivered to browser HTML or JavaScript and there is no credential retrieval endpoint.

## Threats and limitations

- Anyone who can reach the service can read the approved legacy CRM surface and may copy, index, correlate, or redistribute it. Security response headers and `Cache-Control: no-store` reduce some browser risks but do not make public data confidential or prevent screenshots and downstream storage.
- The write gate protects the listed human `POST` routes only. It is not SSO, user identity, authorization roles, audit attribution, rate limiting, or agent authentication.
- The rich-route adapter represents one deployment-configured human principal. HTTP Basic is transport authentication, not SSO or multi-user RBAC; TLS at the proxy is mandatory, credential sharing weakens attribution, and browser logout semantics are limited. Operations still requires the mapped admin flag to be exactly true.
- Both write credentials are shared secrets. Compromise of both permits writes; rotate both and review activity after suspected exposure.
- Origin validation is defense in depth, not authentication. Non-browser clients may omit `Origin` but still require both secrets.
- No permissive CORS policy is enabled.
- The current UI relies on inline script/style, Alpine from jsDelivr, and Google Fonts. The CSP temporarily permits `'unsafe-inline'` (and Alpine's current runtime requires `'unsafe-eval'`). Removing this debt requires nonce/hash-based templates and a CSP-compatible frontend bundle.

## Rejected alternatives

Login/SSO and multi-user RBAC were rejected for the legacy public dashboard scope. The minimal Basic adapter is approved only for the protected rich-route boundary and does not broaden legacy, write or agent authentication. Public agent endpoints and public writes were not approved.

## Rollback and incident response

`get_settings()` and `get_principal_settings()` are process-cached. Revoking, unsetting, remapping or rotating a secret-store value does **not** change the policy in an already-running process until a controlled restart/redeploy reloads settings (tests may explicitly clear the caches). For rich-route credential rotation, update the password and any approved mapping changes, restart every running instance, verify the old credentials receive `401`, verify the new credentials map to the intended workspace and role, and verify malformed/incomplete configuration receives generic `403` without a challenge. For suspected write-secret exposure, rotate/update **both** `CRM_WRITE_TOKEN` and `CRM_CSRF_TOKEN` as applicable, restart every running instance, then verify that the old credentials receive a generic 403. To withdraw public-read acceptance, restrict network/proxy access or add an approved authentication design before serving the dashboard again. Reverting this change is not a safe rollback while public write routes remain reachable. No restart, deploy, or production secret operation was performed for this task.

## Implementation verification

Task 1 was exercised locally with `.venv311/bin/python`; no production or live services were used.

- Initial RED (before the Task 1 implementation): security test collection failed with `ModuleNotFoundError: dashboard.app.config` (`2 errors`).
- Malformed-origin RED: `.venv311/bin/python -m pytest tests/security/test_csrf.py::test_settings_reject_malformed_or_unsafe_origins -q` returned `3 failed, 8 passed in 0.21s`. The new `https://:443`, `https://bad host.example`, and `https://bad..example` cases each failed because `ValueError` was not raised.
- Malformed-origin GREEN: the same focused command returned `11 passed in 0.19s` after hostname validation was added.
- Non-ASCII credential RED: the focused raw-ASGI credential test returned `2 failed in 0.41s`; both malformed bearer and CSRF bytes escaped as `TypeError: comparing strings with non-ASCII characters is not supported`.
- Non-ASCII credential GREEN: the same focused test returned `2 passed in 0.19s` after byte-based ASCII comparison was made fail-closed.
- Public read-only UI RED: the two focused route-protection tests returned `2 failed, 2 warnings in 0.24s`; the rendered model lacked `publicReadOnly: true` and the GET-only `reloadView()` action.
- Public read-only UI GREEN: the same focused tests returned `2 passed, 2 warnings in 0.29s`; public write buttons are disabled and both manual/hourly reload paths call only GET loaders.
- Rich identity initial RED: `.venv311/bin/python -m pytest tests/security/test_rich_route_identity.py::test_valid_basic_credentials_return_server_configured_principal -q` returned `1 failed in 0.15s`; valid Basic credentials still received the deny-only `403`.
- Rich identity initial GREEN: the same focused command returned `1 passed in 0.17s` after the server-configured principal adapter was added.
- Identity edge-case RED: `.venv311/bin/python -m pytest tests/security/test_rich_route_identity.py -q` returned `3 failed, 29 passed in 0.61s`; whitespace-only credentials and a Basic-incompatible username were not classified as malformed server configuration.
- Identity edge-case GREEN: the same focused file returned `32 passed in 0.59s` after fail-closed validation was completed.
- Unsupported Basic configuration RED: `.venv311/bin/python -m pytest tests/security/test_rich_route_identity.py::test_invalid_server_identity_configuration_fails_closed_generically -q` returned `4 failed, 7 passed in 0.56s`; non-ASCII/control-character username or password values produced a challenge instead of a configuration denial.
- Unsupported Basic configuration GREEN: `.venv311/bin/python -m pytest tests/security/test_rich_route_identity.py -q` returned `36 passed in 0.50s` after unsupported credential configuration was rejected before parsing request credentials.
- Deployment declaration RED: `.venv311/bin/python -m pytest tests/integration/persistence/test_database_bootstrap.py::test_deployment_secret_template_covers_all_required_secrets -q` returned `1 failed in 0.16s` because the principal password and non-secret mapping declarations were absent.
- Deployment declaration GREEN: the same focused command returned `1 passed in 0.10s` after the secret/non-secret boundary was declared.
- Targeted GREEN: `.venv311/bin/python -m pytest tests/security tests/test_crm_evolution.py -q` returned `91 passed, 4 warnings in 0.32s`.
- Final relevant regression: `.venv311/bin/python -m pytest tests/security tests/test_crm_evolution.py tests/integration/api tests/integration/test_shadow_compare.py tests/integration/persistence/test_database_bootstrap.py -q` returned `218 passed, 58 skipped in 1.05s`; skips require the documented disposable PostgreSQL fixture.
- Final lint: `.venv311/bin/ruff check dashboard/app/config.py dashboard/app/security.py tests/security/test_rich_route_identity.py tests/integration/persistence/test_database_bootstrap.py` returned `All checks passed!`.
- Full-suite baseline blocker reproduced: `.venv311/bin/python -m pytest -q` reported the pre-existing LinkedIn script's `RESULTS: 47/47 passed, 0 failed`, then `tests/test_linkedin_system.py` called `sys.exit(0)` during collection. Pytest returned exit code `3` with `1 error in 61.67s`; this is not a passing full suite.

## Required revisit

Before production enables any new rich route, verify the concrete identity/role/workspace values in staging and complete the remaining data, backup/restore, browser, secret-scan, owner and rollback gates. Revisit the protection model before adding sensitive payload, bulk export/download, attachment, evidence body, connector or multiple human principals. Agent endpoints retain their separate scoped authentication gate. Do not place real PII or credentials in this document.
