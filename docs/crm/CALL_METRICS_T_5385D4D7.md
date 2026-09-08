# Legacy contact remediation — t_5385d4d7

Return owner: Engineering Reviewer t_4bc00d02, then Main t_4cd15e99 / Sales.
Scope: isolated source/tests/artifacts and disposable local PostgreSQL only.
No production, CRM, outbound, Calendar, cron, backfill, migration or deploy.
This is an implementation handoff, NOT a release approval.

## OBSERVED

The rejected predecessor 3b5b699a138ed0bc54cc887d1a1d0e31ac67d184 remains intact in senior-repo, on fix/call-metrics-t_c345faf2. Its original archive/bundle checksums still match its handoff. This successor is a separate worktree remediation-repo on fix/call-metrics-t_5385d4d7, directly descended from that commit. It changes one runtime file, src/crm/services/call_metrics.py, plus two regression test files and this document. The older four-file release overlay remains the full deployment unit.

The external reviewer test reproduced 8/8 failures before the fix. The independent schema-derived regression matrix additionally detected omitted FU2 Sent, FU3 Sent and Reactivation Sent. The complete date matrix was RED with 176 failed / 219 passed, then GREEN with 403 passed including the eight external cases. All 17 contact-bearing headers are now evaluated:

- Initial Email Sent; Outreach FU1 Sent; Outreach FU2 Sent; Outreach FU3 Sent; Outreach Reactivation Sent.
- Proposal Sent; Proposal FU1 Sent; Proposal FU2 Sent; Proposal FU3 Sent; Proposal Reactivation Sent.
- FU1 Sent; FU2 Sent; FU3 Sent; Reactivation Sent; Meeting Date.
- Older import aliases Proposal Email Sent and Last Contact.

## INTERPRETATION / bounded design

The bug was an incomplete whitelist, not a date parser or ingestion failure. Only the whitelist changes; date parsing, strict pre-event comparison, canonical activity precedence, all attendance logic and aggregation remain unchanged.

The runtime list is explicit to avoid importing the Sheets adapter and its Calendar dependencies into the metrics service. Tests derive their independent matrix from pt_logistics_sheet.DATE_FIELDS and FIELD_ALIASES. New contact-bearing date columns therefore cause regression failures rather than being silently ignored. Due, Proposal Next Action Due and Dashboard Touched are deliberately excluded: planned actions and UI touches do not prove contact. No adapter, callback, pipeline-order, CallIntent, browser runtime or writer changes are in this delta.

A valid prior date/time forces follow_up even if the operator claimed first_contact. Date-only evidence on the same Lisbon day, timezone-naive timestamps and invalid/non-string values block that claim to unknown. Future or equal timestamps do not prove prior contact. Absence, empty/incomplete history, stage alone and scheduling dates never infer first_contact when explicit contact facts are absent or unknown. An explicit human first_contact assertion with no contradictory/uncertain evidence is still supported, as required by the inherited submission contract; an empty history is not itself a certificate. Attendance does not imply useful, decisor or first conversation.

## Verification

Evidence root: /Users/max/.hermes/kanban/workspaces/t_084a11a3/remediation-evidence

- focused.xml: 461 PASS, 0 SKIP, including eight external reviewer cases, all 17 date headers, existing canonical channel/DST tests and six Chromium scenarios (1440px/390px, explicit/default connected/default no_answer).
- frontend.tap: 66 PASS, 0 FAIL, 0 SKIP.
- Browser scenarios exercise actual template + leads.js -> FastAPI canonical writer -> disposable PostgreSQL, verify exact saved row/payload and one call only, nullable useful/decisor, restored draft dimensions and deficit=null displayed as Por apurar. All browser requests are intercepted locally; no real outbound submission.
- audit-replay.json/xml: all 18 unique independently audited IDs and note hashes reconciled, 16 human / 2 nonhuman / 0 attendance unknown. No useful/decisor/first conversation inferred. This maps to one private test lead and is an attendance-only replay, NOT reconstruction of historical cross-channel contact or fresh production readback. Source cutoff remains 2026-09-08T17:08:05.792653+01:00.
- Full-suite coverage: 1777 unique cases, 1768 PASS / 1 SKIP / 8 known FAIL. It is NOT globally green. All eight exact nodeids and causes reproduce on both this candidate (candidate-failures.xml) and untouched immediate predecessor 3b5b699 (baseline-failures.xml). Four timezone-sensitive API cases pass with PGTZ=UTC; four are existing migration/backup assumptions for revision0014 instead of0017_call_day. No debt repair is included.
- Transparent gate recovery: the initial background invocation did not inherit the exported PG environment, producing 1211 PASS / 566 SKIP. Preserve full-incomplete-background.xml as that actual receipt. Only the omitted cases were rerun in foreground with explicit local environment, producing full-omitted.xml (557 PASS / 1 SKIP / 8 FAIL). full.xml is an aggregation of the disjoint executed cases, NOT the output of a second full-suite run. An initial omitted-case selection failed collection because two existing parameter IDs contain datetime.now; omitted-collection-error.xml preserves that non-test attempt. gates.py selects that one function and reconciles its two parameters by HTTP-status suffix; all other IDs are exact and coverage/cardinality is asserted.
- Python compilation, node --check and git diff --check passed. Added-line security scan found no concerns. Independent bounded patch review found no security/logic errors; its optional suggestion was mixed-field/order coverage. It does not approve release.
- verification.json checks the canonical date list, source hashes, exact baseline debt, browser/frontend/replay counts, and protected source bytes/CallIntent/JS callback sections against ordering baseline 2bf495276324fc5e0f1376754b79595279563d1a. No new failure nodeids observed.

### Reproduce safely

From the workspace root, restart ONLY the dedicated disposable cluster if stopped:

    LC_ALL=C pg_ctl -D senior-evidence/pgdata -l remediation-evidence/pg.log -o '-h 127.0.0.1 -p 55485' -w start
    export DATABASE_URL=postgresql+psycopg://max@127.0.0.1:55485/crm_test_5385d4d7
    export CRM_DISPOSABLE_TEST_DATABASE=1
    export PLAYWRIGHT_BROWSERS_PATH=/Users/max/.hermes/kanban/workspaces/t_084a11a3/senior-evidence/browsers
    cd remediation-repo
    ../repo/.venv/bin/python -m alembic -c migrations/alembic.ini upgrade head
    ../repo/.venv/bin/python -m pytest tests/integration/api/test_call_metrics_evidence.py tests/integration/api/test_call_form_browser.py tests/integration/api/test_legacy_call_metrics.py tests/integration/api/test_hq_call_metrics.py tests/integration/api/test_call_cadence_api.py tests/unit/test_call_form_disclosure.py tests/unit/test_legacy_contact_dates.py tests/integration/api/test_legacy_contact_metrics.py ../reviewer-evidence/test_reviewer_acceptance.py -q
    node --test tests/frontend/*.js
    ../repo/.venv/bin/python -m pytest ../remediation-evidence/test_audit_replay.py -q

For debt-only reruns use ../remediation-evidence/gates.py candidate, predecessor, timezone. The first two intentionally return pytest exit1 for the eight known failures, not success. Prefer foreground with explicit environment; do not trust skips or background environment inheritance. The implementation stops the cluster before releasing the review dependency.

## ACTION / frozen artifacts and release scope

New artifacts are remediation-evidence/candidate.json, manifest.json, candidate.bundle and release.tar.gz. candidate.json records the exact successor commit/tree/archive/bundle hashes. The release archive contains only deploy.py, manifest.json and these four runtime files:

    src/crm/services/call_metrics.py
    src/crm/domain/call_contract.py
    dashboard/app/static/leads.js
    dashboard/app/templates/leads/index.html

Only the first after-hash changes from predecessor to this successor:
47aae4f9d747c6f8f83c719afe062001ecd385ff020314da887fa9313414631f.
All baseline before-hashes, the other three after-hashes and deploy.py remain unchanged. The packager only materializes/checks artifacts, never launches a test suite or deploy.

The exact release/preflight/rollback procedure remains docs/crm/CALL_METRICS_T_C345FAF2.md, with the artifact source changed from senior-evidence to remediation-evidence and the new candidate.json/manifest hashes authoritative. Do not publish either rejected archive. Do not treat the original review-final-v2 or patch-only review as approval of this successor.

After independent t_4bc00d02 review, Main must still resolve the existing operator-approved transport gate, confirm the current target/old image and four before-hashes, preserve ordering/callbacks, perform Linux/service-user stage QA and current/previous Lisbon-day checks, then activate only with specific authority. The original target paths remain /root/crm-call-metrics-t_084a11a3.tar.gz and /root/.crm-call-metrics-t_084a11a3; no transfer was attempted here. Fresh public API/UI/hash readback and full current-day reconciliation are mandatory after activation. The historical 18/16 snapshot is never a deployment success constant.

Rollback remains proxy-only to retained crm-release-web-import-order:8000 through the generated rollback.sh, preserving old image/container, host/TLS and /up readiness. No schema/data rollback or callback reversal. Before deployment, abandoning this candidate means keeping the existing live baseline; reverting only this local remediation to rejected3b5b699 is not a release-approved rollback.

## BLOCKERS / definition of done

Local implementation done when the clean successor commit, exact source/package hashes, complete test receipts and schema coverage agree. Independent review is the existing child t_4bc00d02, then Main t_4cd15e99; no duplicate review/release cards. Baseline debt disposition, authorized transport, fresh live compatibility/stage QA and post-deploy readback remain release-owner work, not claims of this local task. Actual production/CRM/outbound/Calendar/cron/deploy effects are zero.
