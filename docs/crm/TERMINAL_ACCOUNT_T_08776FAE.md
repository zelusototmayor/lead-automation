# Terminal Account transition fix — t_08776fae

Return owner: Main/HQ via Engineering Reviewer same-card review. Risk: R2.
This is an implementation/review handoff, NOT deployment or release approval.

## OBSERVED

Canonical create deliberately writes Account + Contact + Lead(new, rank10, v1), including when the contact has neither email nor phone. HumanCommandService passed account existence as `persisted_terminal_requires_account` for every rank. For new -> lost/not_a_fit, this asserted True while the original rank correctly implied False, so stage_policy raised ConflictingAccountEvidenceError, translated to HTTP409. Authorization and optimistic-version validation were not the cause.

The exact API regression was written and failed twice before modifying runtime code (evidence/red.xml), then passed twice (green.xml). The expanded 14-case file on untouched baseline611f387 with the exact live package versions has eight409 failures and six passing history cases (baseline-regression.xml). All14 pass on the candidate, within the345-case focused gate.

Read-only SSH/proxy/source preflight identified the CURRENT public target, not the September7 snapshot:

- Public service: leads-dashboard-web, leads.zelusottomayor.com.
- Container: crm-release-web-callback-immediate-20260910-v2.
- Image: sha256:1f23954373e6cb0ee13613625b41faa390161b99b28cdd2f478d97f0585f2231.
- Command: uvicorn dashboard.app.main:app --host 0.0.0.0 --port 8000.
- Runtime Python3.11.15;45 installed package versions captured in evidence/live-packages.txt.
- Authoritative source lineage:611f387c6a4bc9a0b0fefd4349b9c6564936e3be, isolated branch fix/terminal-account-t_08776fae. The integrations/crm/code snapshot is NOT the release repo.
- Source preflight examined145 tracked paths:119 byte matches. All src/crm, dashboard/app and migration Python paths match. The26 reported differences are24 source files absent from the dashboard image plus the two requirements paths: dashboard/requirements.txt is installed as /app/requirements.txt, rather than the automation requirements file. No mismatching deployed CRM/UI/migration bytes were found in the inspected set. It is not an inventory of untracked image files.
- Before-hash command_service.py:063863dc2176a28e24fb69795cee55572705fa65feb33cbbb486cb2b934eda25.
- Before-hash unchanged stage_policy.py:b567692c99be0ec78398df766914209a2164ce03fc841451d2209dc0a27439fe.

## INTERPRETATION / smallest fix

Only `src/crm/services/command_service.py` changes at runtime. When the recorded highest rank is nonterminal, the caller now supplies no persisted terminal decision: the original rank is authoritative and an early optional Account is permitted. When terminal rank masks the original rank, the existing account-linkage fallback remains unchanged, including after an explicitly reviewed backward correction.

The domain policy and its genuine conflict/missing-history protections are unchanged. Existing Account/Contact validation, linkage and highest-rank updates, terminal-correction guard, stale-version rejection, actor-bound idempotent replay, audit/outbox atomicity and suppression logic remain intact. No intermediate stage, synthesized meeting, auto-refreshed version, identity detachment, migration, data correction, auth or schema change is included.

Tests exercise canonical creation with no contact channels and one-step close to each terminal; unchanged contact/account IDs and contact fields; notes/audit/outbox/replay counts; no tasks; early ranks20/30; meeting/negotiation -> backward -> terminal; reviewed terminal-to-terminal and reopen/reclose with rank90 retained; normal terminal exits/same-stage/stale-version rejection; retained inactive-contact suppression; and accountless terminal histories without fabricating an Account. Existing policy/account/transaction/create suites cover contradictory persisted decisions, identity conflicts, CSRF/origin/principal scope, suppressed source imports and duplicate creation protections.

## Verified gates and limitations

Evidence directory: /Users/max/.hermes/kanban/workspaces/t_08776fae/evidence.

- Initial focused.xml:345 passed using the existing test environment. Initial red/green receipts use that environment too.
- focused-runtime.xml:345 passed,0 skipped, using an isolated Python3.11.15 venv pinned to ALL45 live packages, plus test-only dependencies. PostgreSQL16 is an isolated local Docker container on127.0.0.1:55476, database crm_test_t08776fae. No production data or credentials were used.
- Single full candidate suite full.xml/full.log:1813 cases =1794 passed,18 failed,1 skipped. NOT globally green.
- baseline-debt.xml:all18 exact failing nodeids reproduce on unchanged611f387 with the same local environment. No newly failing nodeid. Old failures cover two UI expectations, four browser unknown-vs-zero expectations, callback task-response shape, date-sensitive import-order assertion, four old0014 migration/backup assumptions, and six callback fixtures missing company. The reviewer must explicitly dispose of this baseline debt; it was not silently fixed or waived.
- One skip requires a specifically documented disposable local PostgreSQL URL; it is recorded, not claimed executed. All focused integration cases ran. No missing-DB bulk skips.
- Frontend:73 passed,0 failed/skip (frontend.json, original tool receipt).
- Ruff on both changed Python files, Python compilation and git diff --check pass. Added-line security scan has no findings. Bounded independent precommit diff review has no security/logic concerns, but is NOT the required Engineering Reviewer release verdict.
- Exact runtime package pins improve compatibility evidence but macOS tests are not Linux/service-user staging QA. That gate and any live writes remain after independent review.

Reproduce from /Users/max/.hermes/kanban/workspaces/t_08776fae:

    docker start crm-terminal-test-t08776fae
    docker exec crm-terminal-test-t08776fae pg_isready -U postgres -d crm_test_t08776fae
    repo/.venv/bin/python gates.py focused
    repo/.venv/bin/python gates.py baseline
    repo/.venv/bin/python gates.py baseline-regression
    repo/.venv/bin/python gates.py summary

The baseline commands intentionally exit1:18 existing failures and8 regression failures respectively. The baseline worktree contains only an untracked copy of the new regression test; all tracked source remains611f387. The full gate ran once and its script refuses to overwrite the receipt. Test-generated .task-evidence is preserved separately, not in the runtime package. Stop only this task's disposable container after testing; do not disturb other local/remote services. The implementation attempted to stop it, but headless approval policy refused `docker stop` before execution; a fresh read confirmed the isolated container remains running. No alternative stop route or permission change was attempted. An authorized operator must stop it when review is finished (it has only synthetic local test data and is published on127.0.0.1 only).

## ACTION / freeze, review, then release preflight

Frozen commit/tree, source/bundle/archive SHA256 and one-file before/after manifest are in evidence/frozen.json and evidence/release-manifest.json. The source bundle contains the commit and its base prerequisite; release.tar.gz contains ONLY the changed runtime file and manifest. It is an overlay on the verified current image, not a replacement using the whole source checkout. Do not publish the old13a021d or an earlier callback image. Packaging does not run tests, launch containers or deploy.

The existing same card must move to Engineering Reviewer; no new project or review DAG. Reviewer should verify frozen bytes, focused tests, baseline-debt equivalence, policy edge cases, release scope and rollback. Main/HQ owns end-to-end release and the four previously failed Sales outcomes. No deployment before that independent verdict.

If an authorized executor deploys after approval:

1. Re-read exact live proxy target, container/image and changed/dependent source hashes, credentials/config names (never print values), mounts, process user and rollback readiness. This preflight is time-bound; abort/review any drift, including another release. Do not use whole-checkout replacement or overwrite another agent's delta.
2. Build only the one-file overlay from the frozen manifest, retaining the prior image/container and existing config/mounts/auth/flags. No worker/sender/cron, DB/Sheets writer, migration or auth change. Perform target-compatible Linux/service-user staging checks before activation. No unsolicited outbound capability may be exercised.
3. Only explicitly labelled NEW QA fixtures without email/phone may prove canonical create -> not_a_fit and create -> lost, one terminal per fixture; preserve their IDs, real expected versions, idempotency IDs and exact canonical GET readbacks. Verify account/contact linkage, rank, audit history, no tasks/channels and effective suppression. Never invent a meeting or retry a stale version silently.
4. Existing QA c1d689a7-39f7-59df-881e-4c797d811fe5 is protected: do not mutate it. Its parent handoff says v5/not_a_fit/suppressed/no tasks; this implementation did not reread or change the record and does not claim a fresh QA readback.
5. After approval/deployment, Main's reconciliation authority is limited to the four failed commands from2026-09-11T13-02-23: index15 new->not_a_fit (28de970e prefix), indices16-18 new->lost (050b3ea6,799b5a02,8e7381f4 prefixes). Retrieve their exact IDs/payloads from the original receipt, never expand these prefixes by guessing; take fresh canonical state/version reads and preserve human notes/history. Never replay the14 successes. No customer record is a smoke fixture. Reconciliation/live latest receipt is NOT completed by this code handoff.
6. After activation, verify exact current proxy/image/file hash, health and isolated QA canonical readbacks. Successful process execution is not proof of success. Return O/I/A/B to Main/HQ through the card, never contact Jose directly.

## ROLLBACK / BLOCKERS

Before deployment: keep existing live image/container unchanged; abandon the candidate. There are no production changes to roll back in this run. For source-only rollback after merge, revert the frozen fix commit, not unrelated releases.

After deployment, ONLY if the captured predecessor remains verified and no intervening release exists, the authorized executor can restore the original public proxy target:

    docker exec kamal-proxy kamal-proxy deploy leads-dashboard-web --target crm-release-web-callback-immediate-20260910-v2:8000 --host leads.zelusottomayor.com --tls --health-check-path /up

This is a REMOTE/operator command for the verified CRM host, not a command executed by this implementation. Verify proxy target, image digest, health and canonical reads afterward. Preserve all human/QA records, audit, outbox and notes; never use DB rollback/delete/backfill to undo release. Data already legitimately transitioned must not be reverted merely because runtime was rolled back. Re-baseline rollback if target drift occurs.

Definition of done at this boundary: frozen bounded implementation, real RED/GREEN regression and history coverage, focused/full relevant gates with honest baseline disposition, current runtime compatibility inventory, rollback instructions and same-card independent review request. Pending: reviewer approval, fresh release preflight/Linux staging, authorized deployment if chosen, QA live readbacks and four-target Sales reconciliation. Side effects actually exercised: isolated code/tests/artifacts, disposable local PostgreSQL and read-only live container/source/package/proxy inspection. Zero CRM/customer/QA writes, production deployment, external sends, credential/auth/config/worker/cron changes, financial/legal actions.
