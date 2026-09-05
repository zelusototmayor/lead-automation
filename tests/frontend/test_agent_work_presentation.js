"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
global.document = { addEventListener() {} };
const { workView, matchesFilter } = require("../../dashboard/app/static/intelligence.js");
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
