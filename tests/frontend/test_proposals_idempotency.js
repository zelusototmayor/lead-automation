"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

global.document = { addEventListener() {} };
const { createProposalCommandBehavior } = require("../../dashboard/app/static/proposals.js");

const payload = (overrides = {}) => ({
  expected_version: 7,
  status: "sent",
  probability: "60",
  forecast_category: "pipeline",
  next_action: "Follow up",
  next_action_due_at: "2026-07-22T09:00:00.000Z",
  lost_reason: null,
  ...overrides,
});

test("an exact retry after a network failure reuses the command identity without automatic retries", async () => {
  const requests = [];
  const ids = ["command-1", "command-2"];
  const behavior = createProposalCommandBehavior({
    createId: () => ids.shift(),
    sendCommand: async (request) => {
      requests.push(request);
      if (requests.length === 1) throw new TypeError("Failed to fetch");
      return { ok: true, json: async () => ({ status: "sent" }) };
    },
  });

  await assert.rejects(behavior.submit(payload()), /Failed to fetch/);
  assert.equal(requests.length, 1);

  assert.deepEqual(await behavior.submit(payload()), { status: "sent" });
  assert.equal(requests.length, 2);
  assert.equal(requests[0].commandId, "command-1");
  assert.equal(requests[1].commandId, "command-1");
  assert.equal(requests[0].body.command_id, "command-1");
  assert.equal(requests[1].body.command_id, "command-1");
});

test("an unreadable success response retains identity for the same semantic payload", async () => {
  const requests = [];
  const ids = ["command-1", "command-2"];
  const behavior = createProposalCommandBehavior({
    createId: () => ids.shift(),
    sendCommand: async (request) => {
      requests.push(request);
      if (requests.length === 1) {
        return { ok: true, json: async () => { throw new SyntaxError("invalid JSON"); } };
      }
      return { ok: true, json: async () => ({ status: "sent" }) };
    },
  });
  const firstPayload = payload();
  const sameSemanticPayload = Object.fromEntries(Object.entries(firstPayload).reverse());

  await assert.rejects(behavior.submit(firstPayload), /invalid JSON/);
  assert.deepEqual(await behavior.submit(sameSemanticPayload), { status: "sent" });
  assert.deepEqual(requests.map((request) => request.commandId), ["command-1", "command-1"]);
});

test("a changed semantic payload gets a new identity after an ambiguous failure", async () => {
  const requests = [];
  const ids = ["command-1", "command-2"];
  const behavior = createProposalCommandBehavior({
    createId: () => ids.shift(),
    sendCommand: async (request) => {
      requests.push(request);
      if (requests.length === 1) throw new TypeError("Failed to fetch");
      return { ok: true, json: async () => ({ status: "won" }) };
    },
  });

  await assert.rejects(behavior.submit(payload()), /Failed to fetch/);
  await behavior.submit(payload({ status: "won" }));
  assert.deepEqual(requests.map((request) => request.commandId), ["command-1", "command-2"]);
});

test("confirmed success clears the identity for a later intentional submit", async () => {
  const requests = [];
  const ids = ["command-1", "command-2"];
  const behavior = createProposalCommandBehavior({
    createId: () => ids.shift(),
    sendCommand: async (request) => {
      requests.push(request);
      return { ok: true, json: async () => ({ status: "sent" }) };
    },
  });

  await behavior.submit(payload());
  await behavior.submit(payload());
  assert.deepEqual(requests.map((request) => request.commandId), ["command-1", "command-2"]);
});

test("a definitive HTTP rejection clears the identity for a later intentional submit", async () => {
  const requests = [];
  const ids = ["command-1", "command-2"];
  const behavior = createProposalCommandBehavior({
    createId: () => ids.shift(),
    sendCommand: async (request) => {
      requests.push(request);
      if (requests.length === 1) return { ok: false, status: 409 };
      return { ok: true, json: async () => ({ status: "sent" }) };
    },
  });

  await assert.rejects(behavior.submit(payload()), /proposal command failed/);
  assert.deepEqual(await behavior.submit(payload()), { status: "sent" });
  assert.deepEqual(requests.map((request) => request.commandId), ["command-1", "command-2"]);
});
