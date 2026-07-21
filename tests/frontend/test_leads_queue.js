"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

// Loading the browser bundle in Node only registers its DOMContentLoaded callback.
global.document = { addEventListener() {} };
const { createLeadQueueBehavior } = require("../../dashboard/app/static/leads.js");

const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
};

test("save and next captures the visible successor and saved lead before the POST", async () => {
  let selection = { leadId: "A", lead: { version: 7 } };
  let visibleIds = ["A", "B", "C"];
  const post = deferred();
  const posts = [];
  const loads = [];
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => visibleIds,
    getSelection: () => selection,
    clearSelection: () => {},
    requestLead: async (leadId) => ({ detail: { lead_id: leadId } }),
    commitSelection: () => {},
    postLead: (command) => {
      posts.push(command);
      return post.promise;
    },
    refresh: async () => {
      visibleIds = ["C", "A"];
      selection = { leadId: "C", lead: { version: 99 } };
    },
    onLoad: (leadId) => loads.push(leadId),
  });

  const saving = behavior.save("edit", { company_name: "A editada" }, true);
  assert.deepEqual(posts, [{
    operation: "edit",
    leadId: "A",
    lead: { version: 7 },
    payload: { company_name: "A editada" },
  }]);

  post.resolve();
  await saving;
  assert.deepEqual(loads, ["B"]);
});

test("newer operator navigation wins over pending save auto-advance", async () => {
  let selection = { leadId: "A", lead: { version: 7 } };
  const post = deferred();
  const loads = [];
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => ["A", "B", "C"],
    getSelection: () => selection,
    clearSelection: (leadId) => { selection = { leadId, lead: null }; },
    requestLead: async (leadId) => ({ detail: { lead_id: leadId } }),
    commitSelection: (leadId) => {
      selection = { leadId, lead: { version: 99 } };
      loads.push(leadId);
    },
    postLead: () => post.promise,
    refresh: async () => {},
  });

  const saving = behavior.save("edit", {}, true);
  await behavior.loadLead("C");
  post.resolve();
  await saving;

  assert.equal(selection.leadId, "C");
  assert.deepEqual(loads, ["C"]);
});

test("a queue or filter change cancels pending save auto-navigation", async () => {
  const post = deferred();
  const loads = [];
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => ["A", "B"],
    getSelection: () => ({ leadId: "A", lead: { version: 7 } }),
    clearSelection: () => {},
    requestLead: async (leadId) => ({ detail: { lead_id: leadId } }),
    commitSelection: (leadId) => loads.push(leadId),
    postLead: () => post.promise,
    refresh: async () => {},
  });

  const saving = behavior.save("edit", {}, true);
  behavior.invalidateNavigation();
  post.resolve();
  await saving;

  assert.deepEqual(loads, []);
});

test("a failed save does not refresh or advance", async () => {
  const loads = [];
  let refreshes = 0;
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => ["A", "B"],
    getSelection: () => ({ leadId: "A", lead: { version: 1 } }),
    clearSelection: () => {},
    requestLead: async () => ({}),
    commitSelection: () => {},
    postLead: async () => { throw new Error("POST failed"); },
    refresh: async () => { refreshes += 1; },
    onLoad: (leadId) => loads.push(leadId),
  });

  await assert.rejects(behavior.save("edit", {}, true), /POST failed/);
  assert.equal(refreshes, 0);
  assert.deepEqual(loads, []);
});

test("successful mutation is not reported as failed when queue refresh fails", async () => {
  const loads = [];
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => ["A", "B"],
    getSelection: () => ({ leadId: "A", lead: { version: 1 } }),
    clearSelection: () => {},
    requestLead: async (leadId) => {
      loads.push(leadId);
      return { detail: { lead_id: leadId } };
    },
    commitSelection: () => {},
    postLead: async () => {},
    refresh: async () => { throw new Error("summary refresh failed"); },
  });

  await assert.doesNotReject(behavior.save("edit", {}, true));
  assert.deepEqual(loads, ["B"]);
});

test("successful mutation is not reported as failed when target reload fails", async () => {
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => ["A", "B"],
    getSelection: () => ({ leadId: "A", lead: { version: 1 } }),
    clearSelection: () => {},
    requestLead: async () => { throw new Error("detail refresh failed"); },
    commitSelection: () => {},
    postLead: async () => {},
    refresh: async () => {},
  });

  await assert.doesNotReject(behavior.save("edit", {}, true));
});

test("skip loads the next visible lead without writes or refresh", async () => {
  let posts = 0;
  let refreshes = 0;
  const loads = [];
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => ["A", "B", "C"],
    getSelection: () => ({ leadId: "B", lead: { version: 1 } }),
    clearSelection: () => {},
    requestLead: async (leadId) => ({ detail: { lead_id: leadId } }),
    commitSelection: () => {},
    postLead: async () => { posts += 1; },
    refresh: async () => { refreshes += 1; },
    onLoad: (leadId) => loads.push(leadId),
  });

  assert.equal(await behavior.skip(), true);
  assert.deepEqual(loads, ["C"]);
  assert.equal(posts, 0);
  assert.equal(refreshes, 0);
});

test("skip on the last visible lead is an explicit no-op without wrap", async () => {
  const loads = [];
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => ["A", "B"],
    getSelection: () => ({ leadId: "B", lead: { version: 1 } }),
    clearSelection: () => {},
    requestLead: async () => ({}),
    commitSelection: () => {},
    postLead: async () => { throw new Error("must not write"); },
    refresh: async () => { throw new Error("must not refresh"); },
    onLoad: (leadId) => loads.push(leadId),
  });

  assert.equal(await behavior.skip(), false);
  assert.deepEqual(loads, []);
});

test("save and next on the last visible lead saves successfully without wrap", async () => {
  let posts = 0;
  let refreshes = 0;
  const loads = [];
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => ["A", "B"],
    getSelection: () => ({ leadId: "B", lead: { version: 3 } }),
    clearSelection: () => {},
    requestLead: async () => ({}),
    commitSelection: () => {},
    postLead: async () => { posts += 1; },
    refresh: async () => { refreshes += 1; },
    onLoad: (leadId) => loads.push(leadId),
  });

  assert.equal(await behavior.save("edit", {}, true), true);
  assert.equal(posts, 1);
  assert.equal(refreshes, 1);
  assert.deepEqual(loads, []);
});

test("an older A response cannot overwrite a newer B selection", async () => {
  const requests = { A: deferred(), B: deferred() };
  const commits = [];
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => ["A", "B"],
    getSelection: () => ({ leadId: null, lead: null }),
    clearSelection: () => {},
    requestLead: (leadId) => requests[leadId].promise,
    commitSelection: (leadId, result) => commits.push([leadId, result.detail.company]),
    postLead: async () => {},
    refresh: async () => {},
  });

  const loadingA = behavior.loadLead("A");
  const loadingB = behavior.loadLead("B");
  requests.B.resolve({ detail: { company: "Empresa B" } });
  assert.equal(await loadingB, true);
  requests.A.resolve({ detail: { company: "Empresa A" } });
  assert.equal(await loadingA, false);
  assert.deepEqual(commits, [["B", "Empresa B"]]);
});

test("a stale A failure is ignored after B has become the active selection", async () => {
  const requests = { A: deferred(), B: deferred() };
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => ["A", "B"],
    getSelection: () => ({ leadId: null, lead: null }),
    clearSelection: () => {},
    requestLead: (leadId) => requests[leadId].promise,
    commitSelection: () => {},
    postLead: async () => {},
    refresh: async () => {},
  });

  const loadingA = behavior.loadLead("A");
  const loadingB = behavior.loadLead("B");
  requests.B.resolve({ detail: { company: "Empresa B" } });
  await loadingB;
  requests.A.reject(new Error("stale A tasks failed"));

  assert.equal(await loadingA, false);
});

test("a partial B load failure clears A and never associates A form data with B", async () => {
  const clears = [];
  const commits = [];
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => ["A", "B"],
    getSelection: () => ({ leadId: null, lead: null }),
    clearSelection: (leadId) => clears.push(leadId),
    requestLead: async (leadId) => {
      if (leadId === "B") throw new Error("B tasks failed after detail loaded");
      return { detail: { company: "Empresa A" } };
    },
    commitSelection: (leadId, result) => commits.push([leadId, result.detail.company]),
    postLead: async () => {},
    refresh: async () => {},
  });

  await behavior.loadLead("A");
  await assert.rejects(behavior.loadLead("B"), /tasks failed/);
  assert.deepEqual(clears, ["A", "B"]);
  assert.deepEqual(commits, [["A", "Empresa A"]]);
});
