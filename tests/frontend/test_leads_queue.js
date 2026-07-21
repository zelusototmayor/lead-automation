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

test("save and next never overwrites a newer selection while its POST is pending", async () => {
  let selection = { leadId: "A", lead: { version: 7 } };
  let visibleIds = ["A", "B", "C"];
  const post = deferred();
  const posts = [];
  const loads = [];
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => visibleIds,
    getSelection: () => selection,
    clearSelection: (leadId) => {
      selection = { leadId, lead: null };
    },
    requestLead: async (leadId) => ({ detail: { lead_id: leadId, version: 99 } }),
    commitSelection: (leadId, result) => {
      selection = { leadId, lead: result.detail };
    },
    postLead: (command) => {
      posts.push(command);
      return post.promise;
    },
    refreshQueue: async () => {
      visibleIds = ["C", "A"];
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

  // The operator explicitly selects C while A is still being saved.
  assert.equal(await behavior.loadLead("C"), true);
  post.resolve();

  assert.equal(await saving, true);
  assert.deepEqual(loads, ["C"]);
  assert.equal(selection.leadId, "C");
});

test("save and next uses the successor captured before the queue refresh", async () => {
  let visibleIds = ["A", "B", "C"];
  const loads = [];
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => visibleIds,
    getSelection: () => ({ leadId: "A", lead: { version: 7 } }),
    clearSelection: () => {},
    requestLead: async (leadId) => ({ detail: { lead_id: leadId } }),
    commitSelection: () => {},
    postLead: async () => {},
    refreshQueue: async () => {
      visibleIds = ["C", "A"];
    },
    onLoad: (leadId) => loads.push(leadId),
  });

  assert.equal(await behavior.save("edit", {}, true), true);
  assert.deepEqual(loads, ["B"]);
});

test("a summary read failure after commit still resolves the save and advances", async () => {
  let visibleIds = ["A", "B", "C"];
  let posts = 0;
  let queueRefreshes = 0;
  const loads = [];
  const readFailures = [];
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => visibleIds,
    getSelection: () => ({ leadId: "A", lead: { version: 7 } }),
    clearSelection: () => {},
    requestLead: async (leadId) => ({ detail: { lead_id: leadId } }),
    commitSelection: () => {},
    postLead: async () => { posts += 1; },
    refreshSummary: async () => { throw new Error("summary failed"); },
    refreshQueue: async () => {
      queueRefreshes += 1;
      visibleIds = ["C", "A"];
    },
    onLoad: (leadId) => loads.push(leadId),
    onReadFailure: (source, error) => readFailures.push([source, error.message]),
  });

  assert.equal(await behavior.save("edit", {}, true), true);
  assert.equal(posts, 1);
  assert.equal(queueRefreshes, 1);
  assert.deepEqual(loads, ["B"]);
  assert.deepEqual(readFailures, [["summary", "summary failed"]]);
});

test("a queue read failure after commit still resolves the save and advances", async () => {
  let posts = 0;
  let summaryRefreshes = 0;
  const loads = [];
  const readFailures = [];
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => ["A", "B", "C"],
    getSelection: () => ({ leadId: "A", lead: { version: 7 } }),
    clearSelection: () => {},
    requestLead: async (leadId) => ({ detail: { lead_id: leadId } }),
    commitSelection: () => {},
    postLead: async () => { posts += 1; },
    refreshSummary: async () => { summaryRefreshes += 1; },
    refreshQueue: async () => { throw new Error("queue failed"); },
    onLoad: (leadId) => loads.push(leadId),
    onReadFailure: (source, error) => readFailures.push([source, error.message]),
  });

  assert.equal(await behavior.save("edit", {}, true), true);
  assert.equal(posts, 1);
  assert.equal(summaryRefreshes, 1);
  assert.deepEqual(loads, ["B"]);
  assert.deepEqual(readFailures, [["queue", "queue failed"]]);
});

test("a target detail read failure after commit resolves and leaves no stale saved version", async () => {
  let selection = { leadId: "A", lead: { version: 7 } };
  let posts = 0;
  let summaryRefreshes = 0;
  let queueRefreshes = 0;
  const readFailures = [];
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => ["A", "B", "C"],
    getSelection: () => selection,
    clearSelection: (leadId) => { selection = { leadId, lead: null }; },
    requestLead: async () => { throw new Error("detail failed"); },
    commitSelection: (leadId, result) => { selection = { leadId, lead: result.detail }; },
    postLead: async () => { posts += 1; },
    refreshSummary: async () => { summaryRefreshes += 1; },
    refreshQueue: async () => { queueRefreshes += 1; },
    onReadFailure: (source, error) => readFailures.push([source, error.message]),
  });

  assert.equal(await behavior.save("edit", {}, true), true);
  assert.equal(posts, 1);
  assert.equal(summaryRefreshes, 1);
  assert.equal(queueRefreshes, 1);
  assert.deepEqual(selection, { leadId: "B", lead: null });
  assert.deepEqual(readFailures, [["detail", "detail failed"]]);
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
    refreshQueue: async () => { refreshes += 1; },
    onLoad: (leadId) => loads.push(leadId),
  });

  await assert.rejects(behavior.save("edit", {}, true), /POST failed/);
  assert.equal(refreshes, 0);
  assert.deepEqual(loads, []);
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
    refreshQueue: async () => { refreshes += 1; },
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
    refreshQueue: async () => { throw new Error("must not refresh"); },
    onLoad: (leadId) => loads.push(leadId),
  });

  assert.equal(await behavior.skip(), false);
  assert.deepEqual(loads, []);
});

test("save and next on the last visible lead saves successfully without wrap", async () => {
  let selection = { leadId: "B", lead: { version: 3 } };
  let posts = 0;
  let refreshes = 0;
  const loads = [];
  const behavior = createLeadQueueBehavior({
    getVisibleLeadIds: () => ["A", "B"],
    getSelection: () => selection,
    clearSelection: (leadId) => { selection = { leadId, lead: null }; },
    requestLead: async () => ({}),
    commitSelection: () => {},
    postLead: async () => { posts += 1; },
    refreshQueue: async () => { refreshes += 1; },
    onLoad: (leadId) => loads.push(leadId),
  });

  assert.equal(await behavior.save("edit", {}, true), true);
  assert.equal(posts, 1);
  assert.equal(refreshes, 1);
  assert.deepEqual(loads, []);
  assert.deepEqual(selection, { leadId: "B", lead: null });
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
    refreshQueue: async () => {},
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
    refreshQueue: async () => {},
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
    refreshQueue: async () => {},
  });

  await behavior.loadLead("A");
  await assert.rejects(behavior.loadLead("B"), /tasks failed/);
  assert.deepEqual(clears, ["A", "B"]);
  assert.deepEqual(commits, [["A", "Empresa A"]]);
});
