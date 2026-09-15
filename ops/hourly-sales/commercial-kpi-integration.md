# Commercial-call KPIs v1 — owner integration recipe

Status: CANDIDATE ONLY. Main/HQ owns cutover and live readback in its existing supervisor. Main's 2026-09-15 simplification decision supersedes the older contract's just-in-time release-child requirement: no new release/coordination card. One independent engineering-reviewer review on t_6078e494; findings return on that card. Nothing here authorizes this implementer to publish or change installed routines.

## Exact boundaries

The server uses commercial-call-kpis/v1 audit/checkpoint projections. Legacy CallDetails/useful/contact_kind/PhoneHistory, notes, callback/Calendar and FU writers are unchanged. New router registration is inside pipeline.router, so dashboard/app/main.py is unchanged and does not compete with FU's followups include. Preserve concurrent owner changes by applying only this candidate's exact delta, never copying an old whole repository over a release.

The Mac client is `ops/hourly-sales/commercial_kpi_reconcile.py`. It reuses only the existing adapter's authenticated HTTP Client/config handling. Its callable HTTP surface permits GET KPI sources/receipts and POST KPI assessments/reconciliations only. It does not invoke the Sales worker, drafts, SEND, Calendar or note/task writers. The contextual model has tools=[], tool_choice=none, store=false. Complete bounded source context is data, never instructions. No Linux model SDK installation is needed or authorized.

## After exact-candidate review, Main's preflight

1. Verify the candidate commit, delta archive and SHA manifest from implementation-handoff.json. Re-read owner comments and current target/proxy/source hashes. Baseline is 5e10b73dd544ac6dd9248a9683fcc9ab15f9c823, NOT the older remote main. Preserve any later Main/FU changes; material integration changes require focused verification and exact approval.
2. Verify Python 3.11, the candidate server with its dependencies on Linux under the actual service UID, PostgreSQL 16 and existing schema constraints. This implementer's target preflight is read-only metadata/source equivalence, not execution of candidate code inside the live container. Read rollback.md before publication.
3. Install the reviewed server and exact Mac client/DTO together only under Main's publication authority. Keep the existing adapter alongside the client and the matching DTO at the repository-relative path; copying the client alone is invalid. Select the real installed candidate path as CANDIDATE_ROOT below.
4. Execute as the existing Mac Sales runtime user, not an unrelated/root user. Reuse its existing credential configuration and profile context. Supported overrides are CRM_AGENT_BASE_URL, CRM_AUTOMATION_BEARER_TOKEN or CRM_AUTOMATION_BEARER_TOKEN_FILE; do not put token bytes in prompts, command lines, output or this document. Token files must remain owner-only. Verify existing work:read/work:write authorization and timestamp/Origin protections. Missing authority is a blocker for Main, not permission to redesign auth or change credentials.
5. Re-read both existing PROMPT jobs using the supported scheduler interface: d579ac888657 (13:00 Europe/Lisbon) and 64dedf399635 (17:30 Europe/Lisbon), originally script=null. Verify their identity, schedules, owner and latest prompt first. Do not invoke or replace the entire Sales routine as a KPI test.

## Shared guide and both prompt additions (owner-only)

Shared guide: /Users/max/clawd/mission-control/ops/head-of-sales/twice-daily-reconciliation.md.

Change its obsolete heading `# Sales Max — execução temporária CRM às 13h e 18h` to `# Sales Max — execução temporária CRM às 13h e 17h30`; reconcile the introductory 18:00 mention to the already scheduled 17:30. Do not change either cron cadence or unrelated task/Calendar/draft responsibilities.

Remove/replace any rule in either prompt or the shared guide that infers `new` from the first call/answer that day. Use this exact policy block in all three surfaces:

> KPIs comerciais são separados dos campos legados. Nova = primeira tentativa telefónica comprovada na história canónica da empresa, incluindo tentativas sem resposta. Um email anterior não é tentativa telefónica. A primeira chamada do dia ou primeira atendida NÃO prova nova. Tentativa telefónica anterior comprovada = follow_up; histórico ausente, incompleto ou ordem ambígua = unknown explícito. Conversa relevante exige troca comercial substantiva com decisor/responsável, incluindo recusa fundamentada; receção, nome/cargo ou logística não bastam. Não classificar por palavras-chave. Executar o cliente contextual sem ferramentas, apenas classificação; nunca reescrever notas nem adaptar useful/contact_kind legados. Meta independente: 50 conversas relevantes por semana, segunda–domingo Europe/Lisbon; desconhecidos/pendentes não são zeros certificados. Histórico fora de cobertura permanece parcial.

Add the bounded KPI step to the 13h prompt with `sales_13h`, and to the 17h30 prompt with `sales_1730`. CANDIDATE_ROOT must be the verified installed release tree; do not literally run a placeholder:

```sh
/Users/max/.hermes/hermes-agent/venv/bin/python "$CANDIDATE_ROOT/ops/hourly-sales/commercial_kpi_reconcile.py" --entrypoint sales_13h --classification-only
/Users/max/.hermes/hermes-agent/venv/bin/python "$CANDIDATE_ROOT/ops/hourly-sales/commercial_kpi_reconcile.py" --entrypoint sales_1730 --classification-only
```

The commands use the current Lisbon anchor date and the same monthly inventory/full historical-context routine. Callable `run_reconciliation(..., anchor_date=..., entrypoint=..., classification_only=True)` is exercised by the candidate tests. Both entrypoints share the same checkpoint/lease. Do not run them concurrently deliberately in production or guess/reset a checkpoint. On 409 preserve the current owner; on failure inspect the bounded error and pending counts, then retry through the same interface. Lost responses are read back before acknowledgements; successful prefix work is reusable. Do not claim success from a configured schedule or a process starting.

Read back the exact shared document and both jobs after the authorized owner edit, checking IDs, cadence, prompt block and unchanged unrelated responsibilities. This document itself does not modify them.

## Main's mandatory A11/A12 live proof

After review/preflight/rollback/publication, preserve the original 17 source IDs for 2026-09-15 from protected canonical evidence and reconcile them against a fresh dynamic full inventory (including new, edited, terminal and account-only sources). No hard-coded 17 loop and no synthetic live calls. Account for duplicates, exclusions and supersession rather than adding fake replacement events.

Run BOTH bounded classification-only commands against the actual CRM, separately; capture exit codes and real persisted run receipts. GET each assessment and final run by its actual ID, verify source/context digests and exact readback, all pages acknowledged and eligible inventory accounted for. Repeat cold to prove no duplicate logical audits. Record before/after aggregate, source coverage, historical unknown, processing pending/stale/conflict/failed and exclusions. Protect source IDs/notes; publish only aggregate evidence. Save→GET must invalidate prior current yes before any worker runs. Inspect the live desktop/mobile UI after cutover.

A successful processing sweep is not proof that missing earlier phone history is complete. coverage.source.complete is bounded processing coverage, separate from coverage.history.incomplete/ambiguous_order and attempt-type unknown. Periods outside the receipt's coverage remain partial. A11/A12 are PENDING_LIVE until these real CRM readbacks exist. Candidate synthetic tests and actual zero-tool model evidence do not substitute for live receipts.
