# CRM dashboard security decision

## Decision

The user rejected login/SSO and role-based access control in order to keep browser read access simple. The user explicitly accepts the risk that the dashboard and its rich CRM read APIs are public. Public payloads may include names, email addresses, contacts, meeting notes, proposal values, proposal history, and evidence references.

That acceptance is narrow: it does **not** authorize public writes, agent endpoints, deployment, or broader disclosure. No deployment is authorized by this decision.

## Public/private boundary

Public, unauthenticated access is intentionally retained for:

- `GET /up`, returning only `{ "status": "ok" }`;
- the browser dashboard and redirects;
- existing `GET` dashboard, CRM data, and intelligence APIs.

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

- Anyone who can reach the service can read the approved rich CRM data and may copy, index, correlate, or redistribute it. Security response headers and `Cache-Control: no-store` reduce some browser risks but do not make public data confidential or prevent screenshots and downstream storage.
- The write gate protects the listed human `POST` routes only. It is not SSO, user identity, authorization roles, audit attribution, rate limiting, or agent authentication.
- Both write credentials are shared secrets. Compromise of both permits writes; rotate both and review activity after suspected exposure.
- Origin validation is defense in depth, not authentication. Non-browser clients may omit `Origin` but still require both secrets.
- No permissive CORS policy is enabled.
- The current UI relies on inline script/style, Alpine from jsDelivr, and Google Fonts. The CSP temporarily permits `'unsafe-inline'` (and Alpine's current runtime requires `'unsafe-eval'`). Removing this debt requires nonce/hash-based templates and a CSP-compatible frontend bundle.

## Rejected alternatives

The user explicitly rejected login/SSO and RBAC roles for this scope. They must not be silently introduced as a substitute for the approved public-read/private-write boundary. Public agent endpoints and public writes were not approved.

## Rollback and incident response

`get_settings()` is process-cached. Revoking, unsetting, or rotating a secret-store value does **not** change the policy in an already-running process until a controlled restart/redeploy reloads settings (tests may explicitly call `get_settings.cache_clear()`). For suspected exposure, rotate/update **both** `CRM_WRITE_TOKEN` and `CRM_CSRF_TOKEN` as applicable, restart every running instance, then verify that the old credentials receive a generic 403 and that the intended new-credential or fail-closed policy works. To withdraw public-read acceptance, restrict network/proxy access or add an approved authentication design before serving the dashboard again. Reverting this change is not a safe rollback while public write routes remain reachable. No restart, deploy, or production secret operation was performed for this task.

## Implementation verification

Task 1 was exercised locally with `.venv311/bin/python`; no production or live services were used.

- Initial RED (before the Task 1 implementation): security test collection failed with `ModuleNotFoundError: dashboard.app.config` (`2 errors`).
- Malformed-origin RED: `.venv311/bin/python -m pytest tests/security/test_csrf.py::test_settings_reject_malformed_or_unsafe_origins -q` returned `3 failed, 8 passed in 0.21s`. The new `https://:443`, `https://bad host.example`, and `https://bad..example` cases each failed because `ValueError` was not raised.
- Malformed-origin GREEN: the same focused command returned `11 passed in 0.19s` after hostname validation was added.
- Non-ASCII credential RED: the focused raw-ASGI credential test returned `2 failed in 0.41s`; both malformed bearer and CSRF bytes escaped as `TypeError: comparing strings with non-ASCII characters is not supported`.
- Non-ASCII credential GREEN: the same focused test returned `2 passed in 0.19s` after byte-based ASCII comparison was made fail-closed.
- Public read-only UI RED: the two focused route-protection tests returned `2 failed, 2 warnings in 0.24s`; the rendered model lacked `publicReadOnly: true` and the GET-only `reloadView()` action.
- Public read-only UI GREEN: the same focused tests returned `2 passed, 2 warnings in 0.29s`; public write buttons are disabled and both manual/hourly reload paths call only GET loaders.
- Targeted GREEN: `.venv311/bin/python -m pytest tests/security tests/test_crm_evolution.py -q` returned `91 passed, 4 warnings in 0.32s`.
- Full-suite baseline blocker reproduced: `.venv311/bin/python -m pytest -q` reported the pre-existing LinkedIn script's `RESULTS: 47/47 passed, 0 failed`, then `tests/test_linkedin_system.py` called `sys.exit(0)` during collection. Pytest returned exit code `3` with `1 error in 61.67s`; this is not a passing full suite.

## Required revisit

Before production adds any further sensitive payload, bulk export/download, attachment, evidence body, or new endpoint, revisit this explicit risk acceptance and the protection model. A separate authorization decision is required for agent endpoints and for any deployment. Do not place real PII or credentials in this document.
