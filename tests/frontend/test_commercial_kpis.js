"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");

test("KPI refresh clears old yes before fetch and ignores superseded responses", async () => {
  const { createController } = require("../../dashboard/app/static/commercial-kpis.js");
  const pending = [], frames = [];
  const controller = createController({ request: (path) => new Promise(resolve => pending.push({path, resolve})), render: frame => frames.push(frame) });
  const first = controller.load("2026-09-15", "week");
  assert.deepEqual(frames.at(-1), {state: "loading"});
  const second = controller.invalidate();
  assert.equal(pending[1].path, "/api/v1/pipeline/call-metrics?date=2026-09-15&period=week");
  pending[1].resolve({ commercial_kpis_v1: { relevance: { confirmed: 0, unknown: 1 } } });
  await second;
  pending[0].resolve({ commercial_kpis_v1: { relevance: { confirmed: 53 } } });
  await first;
  assert.equal(frames.at(-1).data.relevance.confirmed, 0);
  assert.equal(frames.at(-1).data.relevance.unknown, 1);
});
