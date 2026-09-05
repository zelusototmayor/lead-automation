"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
global.document = { addEventListener() {} };
const { createCallDraftStore, createCallCommandBehavior, buildCallPayload, createLeadQueueBehavior } = require("../../dashboard/app/static/leads.js");
const memoryStorage = () => { const data = new Map(); return { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key) }; };

test("returning from the phone app or reloading restores each contact's own note and callback", () => {
  const storage = memoryStorage();
  const first = createCallDraftStore(storage);
  first.write("A", { outcome_code: "connected", summary: "Ligar terça à Paula", callback_enabled: true, callback_due_at: "2026-09-08T10:00" });
  first.write("B", { outcome_code: "no_answer", summary: "" });
  const returned = createCallDraftStore(storage);
  assert.equal(returned.read("A").summary, "Ligar terça à Paula");
  assert.equal(returned.read("A").callback_enabled, true);
  assert.equal(returned.read("B").outcome_code, "no_answer");
  assert.equal(returned.read("C"), null);
});

test("only the successfully saved contact draft is removed", () => {
  const store = createCallDraftStore(memoryStorage());
  store.write("A", { summary: "A" }); store.write("B", { summary: "B" });
  store.remove("A");
  assert.equal(store.read("A"), null); assert.equal(store.read("B").summary, "B");
});

test("stale or unavailable browser storage does not stop calls", () => {
  let now = 1;
  const store = createCallDraftStore(memoryStorage(), () => now);
  store.write("A", { summary: "Old" }); now += 8 * 86400000;
  assert.equal(store.read("A"), null);
  const blocked = createCallDraftStore({ getItem() { throw Error("blocked"); }, setItem() { throw Error("blocked"); }, removeItem() { throw Error("blocked"); } });
  assert.equal(blocked.write("A", { summary: "A" }), false);
  assert.equal(blocked.read("A"), null); blocked.remove("A");
});

test("a call requires an explicit outcome and callback requires a real date", () => {
  assert.throws(() => buildCallPayload({ summary: "hello" }), /resultado/);
  assert.throws(() => buildCallPayload({ outcome_code: "connected", callback_enabled: true }), /data e hora/);
  assert.deepEqual(buildCallPayload({ outcome_code: "no_answer", summary: " " }), { outcome_code: "no_answer", summary: null });
});

test("call note and independent callback travel in one command with an absolute time", () => {
  const payload = buildCallPayload({ outcome_code: "connected", summary: " Falámos ", callback_enabled: true, callback_due_at: "2026-09-08T10:00:00+01:00", callback_title: " Ligar à Paula " });
  assert.deepEqual(payload, { outcome_code: "connected", summary: "Falámos", next_action: { task_type: "call", title: "Ligar à Paula", due_at: "2026-09-08T09:00:00.000Z" } });
});

test("lost save response and page reload retry the exact command and original lead version", async () => {
  const storage = memoryStorage(); const requests = []; let attempt = 0;
  const send = async (id, body) => { requests.push([id, body]); if (++attempt === 1) throw Error("network lost"); return { replayed: true }; };
  const store = createCallDraftStore(storage); store.write("A", { summary: "A" });
  const first = createCallCommandBehavior({ store, createId: () => "same-id", send });
  const payload = { outcome_code: "connected", summary: "A" };
  await assert.rejects(first.submit("A", 2, payload));
  const returned = createCallCommandBehavior({ store: createCallDraftStore(storage), createId: () => "must-not-be-used", send });
  await returned.submit("A", 3, payload);
  assert.deepEqual(requests[0], requests[1]); assert.equal(requests[1][1].expected_version, 2);
  assert.equal(store.read("A"), null);
});

test("conflict preserves the note and starts a fresh command after reading the new version", async () => {
  const store = createCallDraftStore(memoryStorage()); store.write("A", { summary: "Preserve" });
  let id = 0; const requests = [];
  const commands = createCallCommandBehavior({ store, createId: () => String(++id), send: async (_, body) => { requests.push(body); if (requests.length === 1) { const e = Error("conflict"); e.status = 409; throw e; } return {}; } });
  const payload = { outcome_code: "connected", summary: "Preserve" };
  await assert.rejects(commands.submit("A", 1, payload));
  assert.equal(store.read("A").summary, "Preserve");
  await commands.submit("A", 2, payload);
  assert.notEqual(requests[0].command_id, requests[1].command_id);
  assert.equal(requests[1].expected_version, 2);
});

test("save-and-next and skip continue across a server page without looping to the first contact", async () => {
  const loads = []; let selection = { leadId: "A", rowKey: "A", lead: { version: 1 } };
  const behavior = createLeadQueueBehavior({ getVisibleLeadIds: () => ["A"], getSelection: () => selection, clearSelection: () => {}, requestLead: async id => { loads.push(id); return {}; }, commitSelection: () => {}, postLead: async () => {}, nextPageRow: async () => ({ leadId: "B", rowKey: "B" }) });
  await behavior.save("log-call", { outcome_code: "no_answer" }, true);
  assert.deepEqual(loads, ["B"]);
});

test("a newer contact selection while fetching the next server page wins", async () => {
  let resolvePage; const page = new Promise(resolve => { resolvePage=resolve; });
  const loads=[]; let selection={leadId:"A",rowKey:"A",lead:{version:1}};
  const behavior=createLeadQueueBehavior({getVisibleLeadIds:()=>["A"],getSelection:()=>selection,clearSelection:id=>{selection={leadId:id,rowKey:id,lead:{version:1}};},requestLead:async id=>{loads.push(id);return {};},commitSelection:()=>{},postLead:async()=>{},nextPageRow:()=>page});
  const saving=behavior.save("log-call",{},true);
  await new Promise(resolve=>setImmediate(resolve));
  await behavior.loadLead("C");resolvePage({leadId:"B",rowKey:"B"});await saving;
  assert.deepEqual(loads,["C"]);
});

test("contact search goes to the entire server queue and remains on subsequent pages", async () => {
  const { createLatestQueueLoader } = require("../../dashboard/app/static/leads.js"); const urls=[];
  const loader=createLatestQueueLoader({requestJson:async url=>{urls.push(url);return {items:[],total:101,limit:50,offset:urls.length===1?0:50};}});
  await loader.load({search:"Paula Lisboa",offset:0});await loader.next();
  assert.equal(new URL(urls[0],"https://crm.test").searchParams.get("search"),"Paula Lisboa");
  assert.equal(new URL(urls[1],"https://crm.test").searchParams.get("search"),"Paula Lisboa");
  assert.equal(new URL(urls[1],"https://crm.test").searchParams.get("offset"),"50");
});

test("only the selected call obligation is completed, never an email or generic lead task", () => {
  const values={outcome_code:"no_answer"};
  const task={id:"call-1",version:3,type:"call"};
  assert.deepEqual(buildCallPayload(values,{queue:"calls_overdue",task}).completed_task,{id:"call-1",expected_version:3});
  assert.equal(buildCallPayload(values,{queue:"all",task}).completed_task,undefined);
  assert.equal(buildCallPayload(values,{queue:"emails_today",task:{...task,type:"email"}}).completed_task,undefined);
});

test("retry after a call task disappears preserves the original atomic completion payload", async () => {
  const store=createCallDraftStore(memoryStorage());const requests=[];
  const send=async(_,body)=>{requests.push(body);if(requests.length===1)throw Error("lost response");return {replayed:true};};
  const call=createCallCommandBehavior({store,createId:()=>"call-command",send});
  const fields={outcome_code:"connected",summary:"Called"};
  await assert.rejects(call.submit("A",1,{...fields,completed_task:{id:"task-1",expected_version:2}}));
  await call.submit("A",2,fields);
  assert.deepEqual(requests[0],requests[1]);
});
