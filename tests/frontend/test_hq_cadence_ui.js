const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const code = fs.readFileSync('dashboard/app/static/leads.js','utf8');
test('first conversation selection is read from the real call form', () => {
  assert.match(code, /first_conversation: callForm\.elements\.first_conversation/);
});
test('daily preparation has a visible real API surface, no auto-write on page load', () => {
  const html = fs.readFileSync('dashboard/app/templates/leads/index.html','utf8');
  assert.match(html, /data-call-day/);
  const daily = fs.readFileSync('dashboard/app/static/call-day.js','utf8');
  assert.match(daily, /\/api\/v1\/pipeline\/call-day/);
  assert.match(daily, /prepare-call-day/);
  assert.match(daily, /textContent/);
  assert.doesNotMatch(daily, /innerHTML/);
});
