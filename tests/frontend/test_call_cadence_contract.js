"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
global.document = { addEventListener() {} };

const {
  buildCallPayload,
  buildNextActionPayload,
  createCallCommandBehavior,
  createCallDraftStore,
  createCallMetricsBehavior,
  formatDateInTimezone,
  queueMetricValues,
  renderCallMetrics,
} = require("../../dashboard/app/static/leads.js");

const memoryStorage = () => {
  const data = new Map();
  return {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => data.set(key, value),
    removeItem: (key) => data.delete(key),
  };
};

class FakeElement {
  constructor(tagName = "div") {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.className = "";
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this._textContent = "";
  }
  set textContent(value) { this._textContent = String(value); this.children = []; }
  get textContent() { return this._textContent + this.children.map((child) => child.textContent).join(""); }
  set innerHTML(_value) { throw new Error("innerHTML must not be used for call metrics"); }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = [...children]; this._textContent = ""; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
}
const fakeDocument = { createElement: (tagName) => new FakeElement(tagName) };

const structuredCallFields = (overrides = {}) => ({
  outcome_code: "connected",
  summary: " Falámos com a receção ",
  occurred_at: "2026-09-08T10:15:00+01:00",
  answer_kind: "human_counterparty",
  useful: "unknown",
  decision_maker: "no",
  interlocutor_role: "reception",
  repeat_reason: "",
  ...overrides,
});

test("call payload carries explicit v1 phone dimensions, actual aware time, and independent unknowns", () => {
  const payload = buildCallPayload(structuredCallFields());

  assert.deepEqual(payload, {
    outcome_code: "connected",
    occurred_at: "2026-09-08T09:15:00.000Z",
    summary: "Falámos com a receção",
    call_details: {
      schema_version: 1,
      attempted: true,
      answer_kind: "human_counterparty",
      useful: null,
      decision_maker: false,
      interlocutor_role: "reception",
      repeat_reason: null,
    },
  });
});

test("call dimension validation refuses inferred usefulness or decision-maker contradictions", () => {
  assert.throws(
    () => buildCallPayload(structuredCallFields({ useful: "yes", answer_kind: "voicemail" })),
    /útil.*atendimento humano/i,
  );
  assert.throws(
    () => buildCallPayload(structuredCallFields({ decision_maker: "yes", interlocutor_role: "reception" })),
    /receção.*decisor/i,
  );
  assert.throws(
    () => buildCallPayload(structuredCallFields({ outcome_code: "no_answer", answer_kind: "human_counterparty" })),
    /resultado.*atendimento/i,
  );
});

test("agreed callbacks require explicit agreement and carry none calendar policy", () => {
  assert.throws(
    () => buildCallPayload(structuredCallFields({ callback_enabled: true, callback_due_at: "2026-09-09T09:30:00+01:00" })),
    /callback.*combinado/i,
  );

  const payload = buildCallPayload(structuredCallFields({
    callback_enabled: true,
    callback_agreed: true,
    callback_due_at: "2026-09-09T09:30:00+01:00",
    callback_title: " Retomar proposta ",
  }));

  assert.deepEqual(payload.next_action, {
    task_type: "call",
    title: "Retomar proposta",
    due_at: "2026-09-09T08:30:00.000Z",
    call_intent: {
      schema_version: 1,
      purpose: "agreed_callback",
      agreed_with_client: true,
      calendar_policy: "none",
      obligation_key: "agreed-callback:2026-09-09T08:30:00.000Z",
      evidence_refs: [],
      gate_reason: null,
    },
  });
});

test("manual next call action is internal preparation, not a client agreement", () => {
  assert.deepEqual(buildNextActionPayload({
    task_type: "call",
    title: " Preparar bloco de prospeção ",
    due_at: "2026-09-08T10:15:00+01:00",
  }), {
    task_type: "call",
    title: "Preparar bloco de prospeção",
    due_at: "2026-09-08T09:15:00.000Z",
    call_intent: {
      schema_version: 1,
      purpose: "internal_preparation",
      agreed_with_client: false,
      calendar_policy: "none",
      obligation_key: "internal-preparation:2026-09-08T09:15:00.000Z",
      evidence_refs: [],
      gate_reason: null,
    },
  });
});

test("call command lost-response retry preserves dimensions, occurred_at, UUID, version, and selected task completion", async () => {
  const storage = memoryStorage();
  const store = createCallDraftStore(storage);
  const requests = [];
  let attempt = 0;
  const command = createCallCommandBehavior({
    store,
    createId: () => "fixed-command-id",
    send: async (_, body) => {
      requests.push(body);
      if (++attempt === 1) throw new Error("lost response");
      return { replayed: true };
    },
  });
  const payload = {
    ...buildCallPayload(structuredCallFields()),
    completed_task: { id: "task-1", expected_version: 4 },
  };

  await assert.rejects(command.submit("lead-1", 7, payload), /lost response/);
  await command.submit("lead-1", 99, payload);

  assert.deepEqual(requests[0], requests[1]);
  assert.equal(requests[1].command_id, "fixed-command-id");
  assert.equal(requests[1].expected_version, 7);
  assert.equal(requests[1].completed_task.id, "task-1");
  assert.equal(requests[1].call_details.answer_kind, "human_counterparty");
  assert.equal(requests[1].occurred_at, "2026-09-08T09:15:00.000Z");
});

test("queue metrics expose phone cohorts and actionable calls without folding them into untouched", () => {
  assert.deepEqual(queueMetricValues({ queues: {
    all: 100,
    touched_today: 5,
    untouched: 21,
    calls_overdue: 16,
    calls_today: 0,
    calls_actionable: 9,
    phone_new: 12,
    phone_unknown: 4,
    emails_overdue: 1,
    emails_today: 2,
    proposal_followups_overdue: 0,
    proposal_followups_today: 3,
  } }), {
    all: 100,
    touchedToday: 5,
    callsDue: 9,
    emailsDue: 3,
    proposalFollowupsDue: 3,
    phoneNew: 12,
    phoneUnknown: 4,
    callsActionable: 9,
  });
});

test("call metrics request uses the Lisbon work date across DST instead of UTC date", async () => {
  assert.equal(formatDateInTimezone(new Date("2026-03-29T23:30:00Z"), "Europe/Lisbon"), "2026-03-30");
  const requests = [];
  const behavior = createCallMetricsBehavior({
    requestJson: async (url) => {
      requests.push(url);
      return { schema_version: 1, date: "2026-03-30", timezone: "Europe/Lisbon", source_status: "available", counts: { answered: 0 }, target_first_answered: 10, deficit: 10, coverage: {}, blockers: [] };
    },
    renderMetrics: () => {},
    onFailure: () => assert.fail("available metrics must not fail"),
    now: () => new Date("2026-03-29T23:30:00Z"),
  });

  assert.equal(await behavior.load(), true);
  assert.deepEqual(requests, ["/api/v1/pipeline/call-metrics?date=2026-03-30"]);
});

test("call metrics unavailable 503/404 becomes unavailable with nullable counts, never zero", async () => {
  const rendered = [];
  const behavior = createCallMetricsBehavior({
    requestJson: async () => { const error = new Error("503 down"); error.status = 503; throw error; },
    renderMetrics: (payload) => rendered.push(payload),
    onFailure: () => assert.fail("rollout unavailable is rendered, not a generic failure"),
    now: () => new Date("2026-09-08T07:00:00Z"),
  });

  assert.equal(await behavior.load(), false);
  assert.equal(rendered[0].source_status, "unavailable");
  assert.equal(rendered[0].counts.answered, null);
  assert.equal(rendered[0].deficit, null);
  assert.match(rendered[0].blockers[0], /indispon/i);
});

test("call metrics rendering keeps zero distinct from unknown and treats malformed partial payload safely", () => {
  const root = new FakeElement("section");
  renderCallMetrics({
    document: fakeDocument,
    root,
    metrics: {
      schema_version: 1,
      date: "2026-09-08",
      timezone: "Europe/Lisbon",
      source_status: "partial",
      counts: { attempts: 0, answered: null, useful: 2, decision_maker: "bad" },
      target_first_answered: 10,
      deficit: null,
      coverage: { answer_unknown: 3 },
      blockers: ["<img src=x onerror=alert(1)>"],
    },
  });

  assert.match(root.textContent, /Tentativas0/);
  assert.match(root.textContent, /Atendidas—/);
  assert.match(root.textContent, /Úteis2/);
  assert.match(root.textContent, /Decisores—/);
  assert.match(root.textContent, /Fonte partial/);
  assert.match(root.textContent, /<img src=x onerror=alert\(1\)>/);
});
