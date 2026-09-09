# Activity analysis v1 — frozen contract for t_27268ee4

GET /api/v1/pipeline/activity-analysis?days=7|30|90 (default 30).
Existing AccountRequestContext/auth/workspace isolation. Read-only. No migration.
Europe/Lisbon daily buckets including today, exclusive next midnight. Response:
- schema_version=1, timezone, days, start_date, end_date, generated_at
- source_status=partial: registered CRM facts, not exhaustive mailbox coverage.
- series: exactly email_initial, email_follow_up, call_initial, call_follow_up.
  Each: label, total (classified recorded count, null if no classifiable evidence),
  points [{date, value (recorded count, null where classification unknown)}].
- coverage: email_unknown, email_without_message_identity, call_unknown;
  notes: short concrete source limitations, not additional KPIs.

Emails require operational outbound email_sent Activity, matched CRM lead/account,
and gmail message SourceIdentity (scope + external_id is canonical identity).
Duplicate observations of the same message count once; drafts/received/personal or
unmatched events never count as outbound. Manual email logs without message identity
are reported as a coverage gap, not counted twice. Prior canonical commercial contact
or dated legacy contact before the event proves follow-up. The current producers do
not certify absence of all previous contact for emails: first observed is NOT initial;
email_initial stays unavailable until source evidence supports that classification.
Do not invent a new writer or historical import in this UI correction.

Calls count recorded attempts once per Activity ID, irrespective of attendance.
Reuse validated CallDetails contact_kind and existing legacy_contact_state semantics:
prior account/lead-level call, email, meeting or proposal overrides first_contact;
uncertain legacy timestamps prevent first-contact certification. Invalid dimensions
remain unknown. No current stage or planned queue serves as historical evidence.
Unknown events stay outside both classified series, recorded in coverage. Null is
not zero; positive totals are explicitly labelled registered/partial lower bounds.

UI consumes only this aggregate (agents can GET the same endpoint). Defaults to
Chamadas / canonical persisted call-day plan in the existing left list. No automatic
prepare command. Compact prepare only when unprepared; refresh is read-only. Existing
queue navigation/search remains reachable. Selecting a row uses existing detail and
writer pathways. Analysis is a secondary separate view, 30-day default, four totals
and four named temporal series; no goals, attendance or deficit KPIs. No extra PII.
