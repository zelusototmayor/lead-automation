"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
global.document = { addEventListener() {} };
const { workView, matchesFilter } = require("../../dashboard/app/static/intelligence.js");
test("note interpretation remains partial until its obligation receipts exist", () => {
  const item={kind:"call_followup",status:"completed",processing:{state:"partial",obligations:[{task_type:"call",calendar_status:"pending"}]}};
  assert.equal(workView(item).status,"Parcial");
  assert.equal(matchesFilter(item,"attention"),true);
  assert.equal(matchesFilter(item,"completed"),false);
  assert.match(workView(item).obligations[0],/agenda.*pendente/i);
  assert.equal(workView({...item,processing:{state:"processed",obligations:[]}}).status,"Processado");
});

test("queued work is shown as queued, never presented as completed", () => {
  const view=workView({kind:"calendar_callback",status:"queued",payload:{},lead_id:"abc"});
  assert.equal(view.status,"Em fila");assert.equal(view.summary,"Aguarda execução pelo agente.");assert.equal(view.href,"/leads?lead=abc");
});
test("completed result summary takes precedence over the original request", () => {
  assert.equal(workView({status:"completed",payload:{summary:"Rever callback"},result:{summary:"Callback confirmado na agenda"}}).summary,"Callback confirmado na agenda");
});
test("attention and active filters keep waiting and failed work out of completed", () => {
  for(const status of ["waiting","failed"]) {assert.equal(matchesFilter({status},"attention"),true);assert.equal(matchesFilter({status},"completed"),false);}
  assert.equal(matchesFilter({status:"queued"},"active"),true);assert.equal(matchesFilter({status:"completed"},"active"),false);
});
