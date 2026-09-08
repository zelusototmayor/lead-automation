# Call metrics successor — t_c345faf2

Return owner: Main (technical execution), Sales (end-to-end outcome).
Independent release reviewer: existing t_4bc00d02. Not self-approved. No deploy in this task.

## OBSERVED

Recovered and preserved the clean root candidate 9fd847296c752154f502a7c13954a58403255134, directly on live ordering baseline 2bf495276324fc5e0f1376754b79595279563d1a. This successor adds only read-time conflict handling and restoration of two omitted draft dimensions, plus regression tests. The original workspace/repo and original release archive remain unchanged.

- Retained JSON could contain human/useful/decisor/first=true against no_answer/voicemail/wrong_number. Canonical submission rejects this but the metrics reader did not. Three RED cases reproduced answered=1. Reader now reuses outcome validation.
- Independent audit requires connected versus explicit nonhuman evidence to stay unknown. Four RED cases reproduced silently known negative results. Read-time validation now marks these rows unknown and exposes a review warning via blockers. Writer contract and timeline shape are unchanged.
- Actual Chromium form reload discarded contact_kind and first_conversation. RED observed first_contact become unknown. Two-line restoration preserves the operator's selections.
- Actual rendered template + leads.js + FastAPI canonical POST + disposable PostgreSQL were exercised at 1440px and 390px: explicit fields, default connected, default no_answer; six scenarios. Each verifies the exact saved row, nullable useful/decision_maker, one call only, closed advanced section and null deficit rendered Por apurar. All requests intercepted into the local TestClient, external hosts denied; no production browser submission.
- Independent Sales audit at 2026-09-08T17:08:05.792653+01:00 contains 18 unique recorded calls. Every exact outcome/summary hash/dimensions was replayed through the candidate reader in private PostgreSQL: 16 human, 2 nonhuman, 0 attendance unknown. This attendance-only replay maps to one test lead and is NOT a reconstruction of cross-channel history or first-contact counts. No useful/decisor/first conversation was inferred. Source/evidence hashes and event-wise checks are in audit-replay.json.

## INTERPRETATION / precedence

1. Validate retained structured dimensions; inconsistent/invalid non-null objects stay unknown with a review warning, not fallback certainty. Negative outcomes cannot claim human attendance. connected with explicit nonhuman dimensions also stays unknown, even though the older writer accepts that combination.
2. Valid explicit dimensions retain independent meaning. Missing/unknown answer_kind may be completed from explicit connected/no_answer/voicemail/wrong_number only. Attendance does not imply useful, decisor, first conversation or first contact.
3. Neutral legacy follow_up/not_interested imply nothing by themselves. The two previously human-reviewed notes remain bound to exact event ID + outcome + UTF-8 summary SHA256. A different ID, content/negation or outcome invalidates that note evidence. This deliberately is NOT a general NLP classifier of arbitrary future notes; no keyword heuristic or language-model backfill has been added. Independent Sales audit corroborates those two hashes.
4. Counts are computed over all operational calls within the half-open Europe/Lisbon day, without pagination cap or snapshot count constants. Tests include both DST transition days, boundaries and more than one page of calls.
5. Contact type remains a separate existing axis with strictly prior multi-channel evidence and unique-company count. Historical insufficiency remains unknown. API deficit=null is authoritative; confirmed_deficit is not substituted in the UI.
6. Full note-only automation/enrichment and REN internal obligations remain the existing t_ca4178d7 scope. Hiding advanced fields is not presented as delivering that automation. Primary outcome choice is still explicit.

## Verification and known baseline debt

- Full Python run: 1384 passed, 1 skipped, 8 failed, 2 existing dependency deprecations; full.xml records all nodeids. This is NOT a globally green suite.
- All eight failing nodeids reproduced on untouched predecessor 9fd8472 under the same disposable PG configuration (baseline-failures.xml). No new failure nodeid was introduced.
- Four failures are UTC-string expectations with the locally initialized PostgreSQL Europe/Lisbon session timezone; they pass with PGTZ=UTC (timezone-validation.xml).
- Four failures are pre-existing assertions/backup verification hardcoded to migration revision0014 rather than0017_call_day. No migration/backup repair is included. Independent review must explicitly acknowledge this baseline debt; a release rule demanding a globally green suite remains a blocker.
- 66 Node frontend tests pass. 19 added backend/browser regression cases pass in the full run; separate focused receipts also retained.
- Full suite was executed once to completion on final production code. An earlier background attempt produced no output/exit receipt and is NOT test evidence. Package materialization never invokes tests.
- Python3.11 tested, matching repository Docker base. Native macOS+PostgreSQL16 test results do not replace service-user/Linux live stage verification.

Reproduce on a dedicated local disposable PostgreSQL with schema at head:

    export DATABASE_URL=postgresql+psycopg://max@127.0.0.1:55485/crm_test_c345faf2
    export CRM_DISPOSABLE_TEST_DATABASE=1
    export PLAYWRIGHT_BROWSERS_PATH=/Users/max/.hermes/kanban/workspaces/t_084a11a3/senior-evidence/browsers
    cd /Users/max/.hermes/kanban/workspaces/t_084a11a3/senior-repo
    ../repo/.venv/bin/python -m alembic -c migrations/alembic.ini upgrade head
    ../repo/.venv/bin/python -m pytest tests/integration/api/test_call_metrics_evidence.py tests/integration/api/test_call_form_browser.py tests/integration/api/test_legacy_call_metrics.py tests/integration/api/test_hq_call_metrics.py tests/integration/api/test_call_cadence_api.py tests/unit/test_call_form_disclosure.py tests/unit/test_legacy_contact_dates.py -q
    node --test tests/frontend/*.js

The implementation task stops its local PostgreSQL before handoff. Restart ONLY this test cluster if needed: LC_ALL=C postgres -D ../senior-evidence/pgdata -h 127.0.0.1 -p 55485. Optional Playwright1.62.0 and Chromium headless shell1234 were installed locally in the workspace. No real credentials are needed for these tests.

## ACTION / exact release scope for Main after independent review

Deliverables in /Users/max/.hermes/kanban/workspaces/t_084a11a3/senior-evidence:
release.tar.gz (four-file overlay + inherited deploy.py), manifest.json, candidate.json (commit/tree/archive hash), candidate.bundle, full.xml, baseline-failures.xml, timezone-validation.xml, audit-replay.json, verification.json. The source branch is fix/call-metrics-t_c345faf2 in workspace/senior-repo.

The four runtime files are src/crm/services/call_metrics.py, src/crm/domain/call_contract.py, dashboard/app/static/leads.js and dashboard/app/templates/leads/index.html. deploy.py is byte-for-byte inherited from the root checkpoint, not executed here. The package is a REVIEW CANDIDATE, not a deployment authorization or approval. Do not use the old review-final-v2.json as approval of changed hashes.

1. Independent t_4bc00d02 reviews this successor, manifest and baseline debt, reconciles the Sales audit, and clears the existing release lane. No second review graph is needed.
2. Main must resolve the EXISTING operator approval requirement via its authorized interactive mechanism. Prior scp/raw-IP denials remain binding; do not disguise the destination, change security policy, use a different transport to evade it, or repeat an unchanged headless unblock. Root operator-gate.md explains the interactive capability. No transport attempted by this task.
3. Fresh preflight: proxy must still target crm-release-web-import-order and the exact old image sha256:e40bd1c3716bbb3a2876ce350696725997cc885fd68872782f03918c00a5f63a. Check the four before hashes and ordering/callback state. If runtime evolved, STOP for deliberate integration/re-review, do not overlay stale bytes.
4. After approved transport, transfer the NEW candidate archive from senior-evidence/release.tar.gz to the root checkpoint's exact target /root/crm-call-metrics-t_084a11a3.tar.gz on the existing production host. Compare SHA256 to candidate.json, extract privately at /root/.crm-call-metrics-t_084a11a3. Do not reuse a directory containing old stage.json/images without explicit diagnosis.
5. Run python3 /root/.crm-call-metrics-t_084a11a3/deploy.py stage. This gates old image/file hashes, keeps credential mounts read-only, builds only the overlay, launches read-only-SQL QA and compares the full untouched queue order. Its prior-day test is the original fixed 2026-09-07 checkpoint: if deploying on another day, also request actual current/previous Europe/Lisbon dates. Do not infer current success from this historical query.
6. Check actual staged UI desktop/mobile with read-only production DB and intercepted no-send POST. The local browser test is not live service-user QA. Verify advanced disclosure, day/refresh, unknown/zero/null behavior and precise payload. Reconcile all current-day calls again; concurrent human calls must survive and changing totals require another complete read.
7. Only with independent approval, service-user QA and specific deployment authority, run python3 /root/.crm-call-metrics-t_084a11a3/deploy.py activate. Script starts exact image, gates readiness, changes only leads-dashboard-web target, verifies the four container hashes and API, and writes activated.json.
8. Read back public JS hash, UI and current/previous-day APIs; reconcile all fresh event IDs to global attempts. Prior 18/16 is a cutoff observation, NEVER a deployment success criterion or data write.

## Rollback

Before activation deploy.py writes /root/.crm-call-metrics-t_084a11a3/rollback.sh. It returns only the leads-dashboard-web proxy to retained crm-release-web-import-order:8000 with existing host/TLS and /up health check. No schema/data rollback: no migration, backfill, call deletion/recreation, or data correction occurred. Preserve old image/container until post-release verification is complete. If activation's gated verification fails the script invokes the rollback; Main still verifies exact proxy, ready endpoint and old public asset hash. Do not reverse ordering2bf4952 or separately owned callbacks.

## BLOCKERS / side-effect authority

Implementation phase complete when frozen tests/source/manifest agree. Independent release verdict and production preflight/deploy/readback remain PENDING with their existing owners. The full-suite baseline debt and previously denied transport are explicit release considerations. Allowed here: isolated code/tests/artifact creation and private disposable DB replay. Actual production writes/deploy/outbound/Calendar/cron=0. No callback work, no credentials, no task duplication, no client communication. Internal evidence stays on the board/workspace; do not send logs/MD/IDs to José on Telegram.
