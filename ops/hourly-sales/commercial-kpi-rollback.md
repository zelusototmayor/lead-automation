# Rollback — commercial-call KPIs v1 candidate

No deployment has been performed. Main/HQ owns any publication and rollback in the existing supervisor, after exact review. This document is not an instruction to execute rollback now.

Before publishing, Main must retain its then-current known-good image/proxy and owner configuration. Observed reference in target-preflight.json: crm-release-web-notes-20260914-v2, image sha256:5621879f63655b8225dbfa4db6e620a5ff578cc311f7b9854c4e54c06e084db7. Re-verify at cutover; do not stop/delete this reference during candidate work.

If rollback is needed:
1. Owner removes/disables only the newly installed KPI invocation in both prompts; preserve 13h/17h30 schedules and unrelated Sales/FU/callback work. Let the bounded KPI process finish or stop only that owned process; never clear another lease blindly.
2. Main switches back to its verified pre-KPI release/config using the established release mechanism and fresh authorization. If other owner work has landed since the reference above, revert only the KPI delta in an isolated integration branch, not the entire old tree. Review/test that exact delta before cutover.
3. Preserve append-only commercial_kpi.* AuditEvent records and the crm-commercial-kpis/commercial-call-kpis/v1 checkpoint. There is no migration, table deletion or raw-source repair. The prior version ignores the new namespace; do not delete audit evidence or reinterpret legacy CallDetails/useful/contact_kind.
4. Read back live health, proxy target, legacy call metrics, note save/readback and callback/FU behavior. Verify the component/new namespace are absent or restored as intended and there are no new drafts, SEND or Calendar operations from the KPI client. Report actual failures and pending historical coverage.

Local candidate rollback is simply not integrating branch kpi/t_6078e494. Preserve its frozen archive/patch for review and audit. Never reset or overwrite the original baseline worktree or another owner's branch.
