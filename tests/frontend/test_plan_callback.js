"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
global.document = { addEventListener() {} };
const { reconcilePlanCallTask, buildCallPayload } = require("../../dashboard/app/static/leads.js");
const row = { lead_id: "lead-a", cohort: "calls_actionable", task: { id: "selected", type: "call", version: 1 } };
const values = { outcome_code: "connected", summary: "Callback" };

test("plan reconciliation paginates current task reads and preserves exact selected task and snapshot", async () => {
  const snapshot = JSON.stringify(row), urls = [];
  const current = { id: "selected", type: "call", status: "open", version: 7 };
  const request = async url => {
    urls.push(url);
    return urls.length === 1
      ? { items: Array.from({ length: 50 }, (_, i) => ({ id: `other-${i}`, type: "call", status: "open", version: 2 })), total: 51 }
      : { items: [current], total: 51 };
  };
  const selected = await reconcilePlanCallTask(request, "lead-a", "plan", row);
  assert.equal(selected.queue, "calls_actionable");
  assert.deepEqual(buildCallPayload(values, selected).completed_task, { id: "selected", expected_version: 7 });
  assert.deepEqual(urls, ["/api/v1/leads/lead-a/tasks?limit=50&offset=0", "/api/v1/leads/lead-a/tasks?limit=50&offset=50"]);
  assert.equal(JSON.stringify(row), snapshot);
});

for (const status of ["completed", "cancelled", "missing", "changed_type"]) {
  test(`plan ${status} task never completes a fallback obligation or blocks a new call`, async () => {
    const items = [{ id: "other", type: "call", status: "open", version: 10 }];
    if (status !== "missing") items.push({ id: "selected", type: status === "changed_type" ? "email" : "call", status: status === "changed_type" ? "open" : status, version: 9 });
    const selected = await reconcilePlanCallTask(async () => ({ items, total: items.length }), "lead-a", "plan", row);
    assert.equal(selected, null);
    assert.deepEqual(buildCallPayload(values, selected), values);
  });
}

test("non-callback plan cohorts and different leads cannot supply completion context", async () => {
  const noRead = async () => { assert.fail("must not read unrelated tasks"); };
  for (const candidate of [null, { ...row, cohort: "phone_new" }, { ...row, lead_id: "lead-b" }, { ...row, task: { ...row.task, type: "email" } }]) {
    assert.equal(await reconcilePlanCallTask(noRead, "lead-a", "plan", candidate), null);
  }
  assert.equal(buildCallPayload(values, await reconcilePlanCallTask(noRead, "lead-a", "all", row)).completed_task, undefined);
});

test("a failed current-task read does not authorize the stale snapshot", async () => {
  await assert.rejects(reconcilePlanCallTask(async () => { throw Error("read failed"); }, "lead-a", "plan", row), /read failed/);
});
