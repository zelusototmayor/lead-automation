"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

global.document = { addEventListener() {} };
const {
  stageLabel,
  priorityLabel,
  queueMetricValues,
  leadRowView,
  leadNextActionView,
} = require("../../dashboard/app/static/leads.js");

test("stage and priority values are presented as Portuguese operational labels", () => {
  assert.equal(stageLabel("new"), "Novo");
  assert.equal(stageLabel("meeting_booked"), "Reunião marcada");
  assert.equal(stageLabel("meeting_held"), "Reunião feita");
  assert.equal(stageLabel("proposal_sent"), "Proposta enviada");
  assert.equal(stageLabel("not_a_fit"), "Sem fit");
  assert.equal(priorityLabel("high"), "Alta");
  assert.equal(priorityLabel("medium"), "Média");
  assert.equal(priorityLabel("low"), "Baixa");
  assert.equal(priorityLabel(null), "Sem prioridade");
});

test("metric cards derive useful work totals only from canonical queue counts", () => {
  const metrics = queueMetricValues({
    queues: {
      all: 65,
      touched_today: 4,
      calls_overdue: 2,
      calls_today: 3,
      emails_overdue: 5,
      emails_today: 7,
      proposal_followups_overdue: 11,
      proposal_followups_today: 13,
    },
  });

  assert.deepEqual(metrics, {
    all: 65,
    touchedToday: 4,
    callsDue: 5,
    emailsDue: 12,
    proposalFollowupsDue: 24,
  });
});

test("lead row view exposes dense existing fields without inventing missing data", () => {
  const view = leadRowView({
    company: "Example Logistics",
    contact_name: "Ana",
    email: "ana@example.test",
    phone: "+351****1234",
    stage: "proposal_sent",
    priority: "high",
    task: {
      type: "call",
      title: "Confirmar decisão",
      due_at: "2026-07-21T14:30:00Z",
    },
  });

  assert.equal(view.company, "Example Logistics");
  assert.equal(view.contact, "Ana");
  assert.equal(view.phone, "+351****1234");
  assert.equal(view.email, "ana@example.test");
  assert.equal(view.stage, "Proposta enviada");
  assert.equal(view.priority, "Alta");
  assert.equal(view.actionTitle, "Confirmar decisão");
  assert.match(view.due, /21\/07\/26/);

  const missing = leadRowView({ company: "Sem dados", stage: "new", task: null });
  assert.equal(missing.contact, "—");
  assert.equal(missing.phone, "—");
  assert.equal(missing.email, "—");
  assert.equal(missing.actionTitle, "Sem próxima ação");
  assert.equal(missing.due, "—");
});

test("detail next action uses the canonical task embedded in the selected queue item", () => {
  const next = leadNextActionView({
    task: { title: "Ligar depois da reunião", due_at: "2026-07-22T09:00:00Z" },
  });
  assert.equal(next.title, "Ligar depois da reunião");
  assert.match(next.due, /22\/07\/26/);

  assert.deepEqual(leadNextActionView({ task: null }), {
    title: "Sem próxima ação",
    due: "—",
  });
});
