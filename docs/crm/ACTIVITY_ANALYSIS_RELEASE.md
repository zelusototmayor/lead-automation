# t_27268ee4 — release handoff (not deployed)

Return owner: Main/HQ. Risk: R2 (new read-only contract + bounded operational UI).
Baseline: 6171eb8e52625ac7c0b2a35d5c75eafc1da9e531, matched deployed assets/router/template.
Target: https://leads.zelusottomayor.com/leads

## OBSERVED

- Canonical daily plan is now the default existing left queue; no duplicated list or page-load writer.
- The operational daily metrics block is removed. Analysis is a secondary view, loaded only on request.
- Four series use GET /api/v1/pipeline/activity-analysis?days=7|30|90, default30, Europe/Lisbon; old metrics API retained.
- Email initials remain explicitly unavailable: current producers do not certify first commercial contact. Manual email records without message identity increase coverage gaps, not sent totals. No history was fabricated/imported.
- Current selected callback task status/version is reconciled before completing the exact obligation. Completed/cancelled/missing tasks cannot complete another obligation. Lost-response retries preserve command identity and payload.
- Mounted Chromium desktop1440/mobile390 tested with disposable PostgreSQL and existing writer; no live test writes.
- 52 focused API/browser tests passed; 73 frontend tests passed; git diff --check passed.
- Broad suite attempt:1205 passed,576 skipped,6 failed. Skips reflect absent DB environment in the background process (focused suite rerun with explicit DB). All six callback-company failures were independently reproduced in extracted unchanged6171eb8 baseline:6 failed/10 passed. Broad suite is NOT claimed green.
- Independent review initially found R1 callback completion; a third-context fix and bounded independent re-review passed. Evidence: .task-evidence/independent-review*.json.

## INTERPRETATION

Implementation and focused local verification are ready. Runtime compatibility against the exact deployed image and live readback remain UNVERIFIED because transport was blocked. Local Python3.12/Chromium/PostgreSQL execution does not substitute for target-image QA.

## ACTION / mechanical gate

The native single-query security gate refused the first SCP command before execution (raw-IP MEDIUM). No upload, stage, activation, route switch or live mutation occurred. No alternate address/transport, approval configuration change or retry was attempted.

Operator/HQ must approve the exact intended transport through the native mechanism. Do not broadly disable safety or request new product authority. After approval, the operator can use the frozen .task-evidence/release.tar.gz and its manifest; verify hashes before use. The package contains only seven runtime files, manifest.json and deploy.py.

Intended approved operational sequence, NOT EXECUTED:
1. Transfer release.tar.gz to root@143.110.169.251:/root/crm-analysis-t27268.tar.gz.
2. Extract into /root/.crm-analysis-t27268 (private directory).
3. Run python3 /root/.crm-analysis-t27268/deploy.py stage.
4. Review stage.json; test read-only QA at server loopback18541 using an approved tunnel. Check actual desktop/mobile plan/detail and analysis, exact plan order, all four source gaps/counts and readiness.
5. Only after successful QA run python3 /root/.crm-analysis-t27268/deploy.py activate.
6. Read exact public URL/assets and API back, capture final desktop/mobile screenshots; verify container/manifest and retained old target.

The helper fails closed on baseline file/image/route mismatch. Old target is crm-release-web-call-metrics-hq; exact base image sha256:918ff1852f5bf66995d1edfae202139a374af50814a3407fba84c7bf57905655. Stage clones deployment configuration without printing secrets and applies database transaction read-only mode. No migration or data backfill.
Rollback is generated at /root/.crm-analysis-t27268/rollback.sh immediately before activation; it routes leads-dashboard-web back to retained old target. This path is PLANNED, not already created remotely. Activation exceptions restore that route. Old image/container are not removed.

## BLOCKERS / definition of done

One blocker: approved native production transport capability. DoD still requires target-image QA, release publication, exact runtime/assets/functionality readback and final live screenshots. Do not mark task done or describe local after-screenshots as live.

Side-effect authority remains bounded code/tests/commits and this release only. No sends, drafts, Calendar invitations, real leads/deals mutations, credentials/auth changes or financial commitments. No child task graph or notification subscriptions created. Existing unrelated work stays with its owners.
