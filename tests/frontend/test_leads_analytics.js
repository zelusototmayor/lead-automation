"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

global.document = { addEventListener() {} };
const {
  createLatestQueueLoader,
  createLeadAnalyticsBehavior,
  createLeadQueueBehavior,
  renderLeadAnalytics,
  revealDetailOnMobile,
} = require("../../dashboard/app/static/leads.js");

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

  set textContent(value) {
    this._textContent = String(value);
    this.children = [];
  }

  get textContent() {
    return this._textContent + this.children.map((child) => child.textContent).join("");
  }

  set innerHTML(_value) {
    throw new Error("innerHTML must not be used for analytics");
  }

  append(...children) {
    this.children.push(...children);
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  replaceChildren(...children) {
    this.children = [...children];
    this._textContent = "";
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  addEventListener(name, callback) {
    this.listeners[name] = callback;
  }

  click() {
    return this.listeners.click?.({ currentTarget: this });
  }
}

const fakeDocument = { createElement: (tagName) => new FakeElement(tagName) };

const findAll = (root, predicate) => {
  const matches = predicate(root) ? [root] : [];
  return matches.concat(...root.children.flatMap((child) => findAll(child, predicate)));
};

const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
};

const analyticsFixture = () => ({
  period: { start_date: "2026-06-22", end_date: "2026-07-21", days: 30 },
  daily: [
    {
      date: "2026-07-21",
      activity_types: { call: 2, "<img src=x onerror=alert(1)>": 1 },
      outcomes: { connected: 1, "<script>alert(1)</script>": 2 },
      distinct_touched_leads: 2,
      contacts: [{ email: "buyer@example.test", company: "PRIVATE COMPANY" }],
    },
  ],
  stages: { by_status: { contacted: 4, "<svg onload=alert(1)>": 1 }, total: 5 },
  proposals: { by_status: { draft: 2, sent: 1 }, total: 3 },
  tasks: {
    by_status: { open: 6, completed: 2 },
    open_by_type: { call: 3, email: 2, other: 1 },
    total: 8,
  },
  queues: {
    counts: {
      calls_overdue: 2,
      calls_today: 1,
      emails_overdue: 1,
      emails_today: 0,
      proposal_followups_overdue: 3,
      proposal_followups_today: 1,
    },
    unit: "task",
  },
  time_in_stage: {
    status: "not_available",
    coverage: {
      structured_transitions: 0,
      legacy_transitions: 3,
      usable_intervals: 0,
      uncovered_transitions: 0,
    },
    stages: [],
  },
});

test("analytics requests the bounded 30 day aggregate and renders the response", async () => {
  const requests = [];
  const rendered = [];
  const behavior = createLeadAnalyticsBehavior({
    fetchJson: async (url) => {
      requests.push(url);
      return analyticsFixture();
    },
    renderAnalytics: (payload) => rendered.push(payload),
    filterByStage: async () => {},
    openQueue: async () => {},
    onFailure: () => assert.fail("successful analytics must not warn"),
  });

  assert.equal(await behavior.load(), true);
  assert.deepEqual(requests, ["/api/v1/pipeline/analytics?days=30"]);
  assert.equal(rendered.length, 1);
  assert.equal(rendered[0].period.days, 30);
});

test("analytics rendering exposes aggregates and keeps untrusted labels as text", () => {
  const root = new FakeElement("section");
  const actions = { stages: [], queues: [] };

  renderLeadAnalytics({
    document: fakeDocument,
    root,
    analytics: analyticsFixture(),
    filterByStage: (stage) => actions.stages.push(stage),
    openQueue: (queue) => actions.queues.push(queue),
  });

  const text = root.textContent;
  for (const expected of [
    "Últimos 30 dias",
    "Atividades 3",
    "Contactados 2",
    "Resultados",
    "Leads por fase 5",
    "Propostas 3",
    "Tarefas 8",
    "Em aberto 6",
    "Filas com prazo",
    "Tempo em fase indisponível",
    "<img src=x onerror=alert(1)>",
    "<script>alert(1)</script>",
  ]) assert.match(text, new RegExp(expected.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));

  assert.doesNotMatch(text, /buyer@example\.test|PRIVATE COMPANY/);
  assert.equal(findAll(root, (element) => element.tagName === "IMG").length, 0);
  assert.equal(findAll(root, (element) => element.tagName === "SCRIPT").length, 0);
  assert.equal(findAll(root, (element) => element.tagName === "SVG").length, 0);
  assert.equal(findAll(root, (element) => element.tagName === "A")[0].attributes.href, "/propostas");
});

test("time-in-stage rendering reports available aggregates and coverage safely", () => {
  const root = new FakeElement("section");
  const analytics = analyticsFixture();
  analytics.time_in_stage = {
    status: "available",
    coverage: {
      structured_transitions: 2,
      legacy_transitions: 4,
      usable_intervals: 1,
      uncovered_transitions: 1,
    },
    stages: [
      {
        stage: "contacted",
        completed_intervals: 1,
        average_hours: 12.5,
        median_hours: 12.5,
        p90_hours: 12.5,
        private_contacts: ["buyer@example.test"],
      },
    ],
  };

  renderLeadAnalytics({
    document: fakeDocument,
    root,
    analytics,
    filterByStage: () => {},
    openQueue: () => {},
  });

  assert.match(root.textContent, /Tempo em fase/);
  assert.match(root.textContent, /Contactado/);
  assert.match(root.textContent, /12[,.]5 h/);
  assert.match(root.textContent, /1 intervalo concluído/);
  assert.match(root.textContent, /Cobertura 1 de 2 transições estruturadas/);
  assert.match(root.textContent, /4 transições legadas/);
  assert.doesNotMatch(root.textContent, /buyer@example\.test/);
});


test("stage and due metrics perform canonical workspace actions", () => {
  const root = new FakeElement("section");
  const stages = [];
  const queues = [];
  renderLeadAnalytics({
    document: fakeDocument,
    root,
    analytics: analyticsFixture(),
    filterByStage: (stage) => stages.push(stage),
    openQueue: (queue) => queues.push(queue),
  });

  findAll(root, (element) => element.dataset.analyticsStage === "contacted")[0].click();
  findAll(root, (element) => element.dataset.analyticsQueue === "calls_overdue")[0].click();

  assert.deepEqual(stages, ["contacted"]);
  assert.deepEqual(queues, ["calls_overdue"]);
});

test("analytics stage drill-down requests the selected stage on the current queue", async () => {
  const requests = [];
  const loader = createLatestQueueLoader({
    requestJson: async (url) => {
      requests.push(url);
      return { items: [], total: 0, limit: 50, offset: 0 };
    },
  });
  await loader.load({ queue: "calls_today" });
  requests.length = 0;

  const root = new FakeElement("section");
  renderLeadAnalytics({
    document: fakeDocument,
    root,
    analytics: analyticsFixture(),
    filterByStage: (stage) => loader.load({ stage, offset: 0 }),
    openQueue: () => {},
  });

  await findAll(root, (element) => element.dataset.analyticsStage === "contacted")[0].click();
  assert.deepEqual(requests, [
    "/api/v1/pipeline/items?queue=calls_today&limit=50&offset=0&stage=contacted",
  ]);
});

test("analytics failure is isolated from queue loading and save-next with a bounded generic warning", async () => {
  let selection = { leadId: "A", lead: { version: 1 } };
  let queueLoads = 0;
  let saves = 0;
  const loadedLeads = [];
  const warnings = [];
  const analytics = createLeadAnalyticsBehavior({
    fetchJson: async () => { throw new Error("PII company buyer@example.test database detail"); },
    renderAnalytics: () => assert.fail("failed analytics must not render"),
    filterByStage: async () => {},
    openQueue: async () => {},
    onFailure: (message) => warnings.push(message),
  });
  const queue = createLeadQueueBehavior({
    getVisibleLeadIds: () => ["A", "B"],
    getSelection: () => selection,
    clearSelection: (leadId) => { selection = { leadId, lead: null }; },
    requestLead: async (leadId) => {
      loadedLeads.push(leadId);
      return { detail: { version: 2 } };
    },
    commitSelection: (leadId, result) => { selection = { leadId, lead: result.detail }; },
    postLead: async () => { saves += 1; },
    refreshSummary: async () => {},
    refreshQueue: async () => { queueLoads += 1; },
  });

  const results = await Promise.all([analytics.load(), queue.save("edit", {}, true)]);

  assert.deepEqual(results, [false, true]);
  assert.equal(queueLoads, 1);
  assert.equal(saves, 1);
  assert.deepEqual(loadedLeads, ["B"]);
  assert.deepEqual(selection, { leadId: "B", lead: { version: 2 } });
  assert.deepEqual(warnings, ["Não foi possível sincronizar os indicadores."]);
  assert.doesNotMatch(warnings[0], /buyer|database|company/i);
  assert.ok(warnings[0].length < 80);
});

test("queue loader sends canonical server filters and paginates from response total and offset", async () => {
  const requests = [];
  const starts = [];
  const pages = [];
  const responses = [
    { items: [{ lead_id: "A", stage: "contacted" }], total: 51, limit: 50, offset: 0 },
    { items: [{ lead_id: "B", stage: "contacted" }], total: 51, limit: 50, offset: 50 },
    { items: [{ lead_id: "A", stage: "contacted" }], total: 51, limit: 50, offset: 0 },
  ];
  const loader = createLatestQueueLoader({
    requestJson: async (url) => {
      requests.push(url);
      return responses.shift();
    },
    onStart: (state) => starts.push(state),
    onPage: (page, state) => pages.push({ page, state }),
    onFailure: () => assert.fail("successful queue must not fail"),
  });

  assert.equal(await loader.load({
    queue: "calls_future",
    stage: "contacted",
    priority: "high",
    offset: 0,
  }), true);
  assert.equal(loader.getState().total, 51);
  assert.equal(await loader.next(), true);
  assert.equal(loader.getState().offset, 50);
  assert.equal(await loader.previous(), true);

  assert.deepEqual(requests, [
    "/api/v1/pipeline/items?queue=calls_future&limit=50&offset=0&stage=contacted&priority=high",
    "/api/v1/pipeline/items?queue=calls_future&limit=50&offset=50&stage=contacted&priority=high",
    "/api/v1/pipeline/items?queue=calls_future&limit=50&offset=0&stage=contacted&priority=high",
  ]);
  assert.deepEqual(starts.map((state) => state.offset), [0, 50, 0]);
  assert.equal(pages[1].state.total, 51);
  assert.deepEqual(pages[1].page.items.map((item) => item.lead_id), ["B"]);
});

test("queue loader preserves a selected stage when that server page is empty", async () => {
  const pages = [];
  const loader = createLatestQueueLoader({
    requestJson: async () => ({ items: [], total: 0, limit: 50, offset: 0 }),
    onStart: () => {},
    onPage: (page, state) => pages.push({ page, state }),
    onFailure: () => assert.fail("empty stage page is successful"),
  });

  assert.equal(await loader.load({ queue: "untouched", stage: "won", offset: 0 }), true);
  assert.equal(loader.getState().stage, "won");
  assert.equal(pages[0].state.stage, "won");
  assert.deepEqual(pages[0].page.items, []);
});

test("stale queue response cannot mutate rows, queue button, or stage filter", async () => {
  const first = deferred();
  const second = deferred();
  const requests = [first, second];
  const ui = { queue: null, stage: null, rows: [] };
  const loader = createLatestQueueLoader({
    requestJson: () => requests.shift().promise,
    onStart: (state) => {
      ui.queue = state.queue;
      ui.stage = state.stage;
    },
    onPage: (page) => { ui.rows = page.items.map((item) => item.lead_id); },
    onFailure: () => assert.fail("stale request must not fail current UI"),
  });

  const loadingStage = loader.load({ queue: "all", stage: "contacted", offset: 0 });
  const loadingQueue = loader.load({ queue: "calls_today", stage: "", offset: 0 });
  second.resolve({ items: [{ lead_id: "CURRENT" }], total: 1, limit: 50, offset: 0 });
  assert.equal(await loadingQueue, true);
  first.resolve({ items: [{ lead_id: "STALE" }], total: 99, limit: 50, offset: 0 });
  assert.equal(await loadingStage, false);
  assert.equal(ui.queue, "calls_today");
  assert.equal(ui.stage, "");
  assert.deepEqual(ui.rows, ["CURRENT"]);
  assert.equal(loader.getState().total, 1);
});

test("lead detail scrolls into view only at the mobile breakpoint", () => {
  const calls = [];
  const detailPanel = { scrollIntoView: (options) => calls.push(options) };

  assert.equal(revealDetailOnMobile({
    windowObject: { matchMedia: () => ({ matches: false }) },
    detailPanel,
  }), false);
  assert.deepEqual(calls, []);

  assert.equal(revealDetailOnMobile({
    windowObject: { matchMedia: () => ({ matches: true }) },
    detailPanel,
  }), true);
  assert.deepEqual(calls, [{ block: "start", behavior: "auto" }]);
});
