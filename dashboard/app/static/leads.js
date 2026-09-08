(() => {
  "use strict";

  const createLeadQueueBehavior = ({
    getVisibleLeadIds,
    getVisibleLeadRows,
    getViewIntentGeneration = () => 0,
    getSelection,
    clearSelection,
    requestLead,
    commitSelection,
    postLead,
    refreshSummary = async () => {},
    refreshQueue = async () => {},
    refreshCallMetrics = async () => {},
    onLoad = () => {},
    onReadFailure = () => {},
    nextPageRow = async () => null,
  }) => {
    let loadSequence = 0;

    const visibleRows = () => (
      getVisibleLeadRows
        ? getVisibleLeadRows()
        : getVisibleLeadIds().map((leadId) => ({ leadId, rowKey: leadId }))
    );

    const nextVisibleLead = (leadId, rowKey = leadId) => {
      const rows = visibleRows();
      const currentIndex = rows.findIndex((row) => row.rowKey === rowKey);
      return currentIndex >= 0 ? rows[currentIndex + 1] || null : null;
    };

    const loadLead = async (leadId, rowKey = leadId) => {
      if (!leadId) return false;
      const requestSequence = ++loadSequence;
      clearSelection(leadId, rowKey);
      onLoad(leadId, rowKey);
      let result;
      try {
        result = await requestLead(leadId, rowKey);
      } catch (error) {
        if (requestSequence !== loadSequence) return false;
        throw error;
      }
      if (requestSequence !== loadSequence) return false;
      commitSelection(leadId, result, rowKey);
      return true;
    };

    const skip = async () => {
      const sequence = loadSequence, intent = getViewIntentGeneration();
      const { leadId, rowKey } = getSelection();
      const nextLead = nextVisibleLead(leadId, rowKey) || await nextPageRow();
      if (sequence !== loadSequence || intent !== getViewIntentGeneration()) return false;
      return nextLead ? loadLead(nextLead.leadId, nextLead.rowKey) : false;
    };

    const save = async (operation, payload, advanceAfterSave) => {
      const { leadId, rowKey, lead } = getSelection();
      if (!leadId || !lead) return false;
      const saveSequence = loadSequence;
      const saveViewIntentGeneration = getViewIntentGeneration();
      const nextLead = advanceAfterSave ? nextVisibleLead(leadId, rowKey) : null;
      await postLead({ operation, leadId, lead, payload });

      await Promise.all([
        refreshSummary().catch((error) => onReadFailure("summary", error)),
        refreshQueue().catch((error) => onReadFailure("queue", error)),
        refreshCallMetrics().catch((error) => onReadFailure("call-metrics", error)),
      ]);
      if (
        saveSequence !== loadSequence
        || saveViewIntentGeneration !== getViewIntentGeneration()
      ) return true;

      const capturedTarget = advanceAfterSave
        ? (nextLead || await nextPageRow())
        : { leadId, rowKey: rowKey || leadId };
      if (saveSequence !== loadSequence || saveViewIntentGeneration !== getViewIntentGeneration()) return true;
      const refreshedRows = visibleRows();
      const targetLead = capturedTarget
        ? refreshedRows.find((row) => row.rowKey === capturedTarget.rowKey)
          || refreshedRows.find((row) => row.leadId === capturedTarget.leadId)
          || capturedTarget
        : null;
      if (targetLead) {
        await loadLead(targetLead.leadId, targetLead.rowKey)
          .catch((error) => onReadFailure("detail", error));
      } else {
        clearSelection(leadId, rowKey);
      }
      return true;
    };

    return { loadLead, save, skip };
  };

  const createLatestQueueLoader = ({
    requestJson,
    onStart = () => {},
    onPage = () => {},
    onFailure = () => {},
    limit = 50,
  }) => {
    let requestSequence = 0;
    let state = {
      queue: "all",
      stage: "",
      priority: "",
      search: "",
      limit,
      offset: 0,
      total: 0,
    };
    const snapshot = () => ({ ...state });

    const load = async (changes = {}) => {
      state = { ...state, ...changes, limit };
      const requestState = snapshot();
      const sequence = ++requestSequence;
      onStart(requestState);
      const searchParams = new URLSearchParams({
        queue: requestState.queue,
        limit: String(requestState.limit),
        offset: String(requestState.offset),
      });
      if (requestState.stage) searchParams.set("stage", requestState.stage);
      if (requestState.priority) searchParams.set("priority", requestState.priority);
      if (requestState.search) searchParams.set("search", requestState.search);

      let page;
      try {
        page = await requestJson(`/api/v1/pipeline/items?${searchParams.toString()}`);
      } catch (error) {
        if (sequence !== requestSequence) return false;
        onFailure(error, requestState);
        throw error;
      }
      if (sequence !== requestSequence) return false;

      state = {
        ...requestState,
        total: Number(page.total ?? 0),
        limit: Number(page.limit ?? requestState.limit),
        offset: Number(page.offset ?? requestState.offset),
      };
      onPage(page, snapshot());
      return true;
    };

    const next = () => (
      state.offset + state.limit < state.total
        ? load({ offset: state.offset + state.limit })
        : Promise.resolve(false)
    );
    const previous = () => (
      state.offset > 0
        ? load({ offset: Math.max(0, state.offset - state.limit) })
        : Promise.resolve(false)
    );

    return { getState: snapshot, load, next, previous };
  };

  const analyticsElement = (documentObject, tagName, className, text) => {
    const element = documentObject.createElement(tagName);
    element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  };

  const appendBreakdown = (documentObject, parent, values, className = "analytics-breakdown") => {
    const list = analyticsElement(documentObject, "div", className);
    Object.entries(values || {}).forEach(([label, count]) => {
      const item = analyticsElement(documentObject, "span", "analytics-chip");
      item.append(
        analyticsElement(documentObject, "span", "analytics-chip-label", stageLabel(label)),
        analyticsElement(documentObject, "strong", "", count),
      );
      list.appendChild(item);
    });
    parent.appendChild(list);
    return list;
  };

  const renderLeadAnalytics = ({ document: documentObject, root, analytics, filterByStage, openQueue }) => {
    root.replaceChildren();
    const days = Number(analytics.period?.days || 30);
    const heading = analyticsElement(documentObject, "div", "analytics-heading");
    heading.append(
      analyticsElement(documentObject, "strong", "", `Últimos ${days} dias`),
      analyticsElement(documentObject, "span", "analytics-caption", "Agregados operacionais; sem dados pessoais"),
    );
    root.appendChild(heading);

    const grid = analyticsElement(documentObject, "div", "analytics-grid");
    const daily = analyticsElement(documentObject, "article", "analytics-card analytics-card-wide");
    daily.appendChild(analyticsElement(documentObject, "h3", "", "Atividade diária"));
    const daysList = analyticsElement(documentObject, "div", "analytics-days");
    (analytics.daily || []).forEach((day) => {
      const activities = Object.values(day.activity_types || {}).reduce((total, count) => total + Number(count), 0);
      const dayElement = analyticsElement(documentObject, "div", "analytics-day");
      dayElement.append(
        analyticsElement(documentObject, "time", "", day.date),
        analyticsElement(documentObject, "strong", "", `Atividades ${activities}`),
        analyticsElement(documentObject, "span", "", `Trabalhados ${Number(day.distinct_touched_leads || 0)}`),
        analyticsElement(documentObject, "span", "analytics-caption", "Resultados"),
      );
      appendBreakdown(documentObject, dayElement, day.activity_types, "analytics-breakdown analytics-breakdown-compact");
      appendBreakdown(documentObject, dayElement, day.outcomes, "analytics-breakdown analytics-breakdown-compact");
      daysList.appendChild(dayElement);
    });
    daily.appendChild(daysList);
    grid.appendChild(daily);

    const stages = analyticsElement(documentObject, "article", "analytics-card");
    stages.appendChild(analyticsElement(documentObject, "h3", "", `Leads por fase ${Number(analytics.stages?.total || 0)}`));
    const stageActions = analyticsElement(documentObject, "div", "analytics-actions");
    Object.entries(analytics.stages?.by_status || {}).forEach(([stage, count]) => {
      const button = analyticsElement(documentObject, "button", "analytics-metric-btn", `${stageLabel(stage)} ${count}`);
      button.setAttribute("type", "button");
      button.dataset.analyticsStage = stage;
      button.addEventListener("click", () => filterByStage(stage));
      stageActions.appendChild(button);
    });
    stages.appendChild(stageActions);
    grid.appendChild(stages);

    const proposals = analyticsElement(documentObject, "a", "analytics-card analytics-card-link");
    proposals.setAttribute("href", "/propostas");
    proposals.appendChild(analyticsElement(documentObject, "h3", "", `Propostas ${Number(analytics.proposals?.total || 0)}`));
    appendBreakdown(documentObject, proposals, analytics.proposals?.by_status);
    grid.appendChild(proposals);

    const tasks = analyticsElement(documentObject, "article", "analytics-card");
    const openTasks = Number(analytics.tasks?.by_status?.open || 0);
    tasks.append(
      analyticsElement(documentObject, "h3", "", `Tarefas ${Number(analytics.tasks?.total || 0)}`),
      analyticsElement(documentObject, "strong", "analytics-primary", `Em aberto ${openTasks}`),
    );
    appendBreakdown(documentObject, tasks, analytics.tasks?.open_by_type);
    grid.appendChild(tasks);

    const queues = analyticsElement(documentObject, "article", "analytics-card analytics-card-wide");
    queues.appendChild(analyticsElement(documentObject, "h3", "", "Filas com prazo"));
    const queueActions = analyticsElement(documentObject, "div", "analytics-actions");
    Object.entries(analytics.queues?.counts || {}).forEach(([queue, count]) => {
      const button = analyticsElement(documentObject, "button", "analytics-metric-btn", `${stageLabel(queue)} ${count}`);
      button.setAttribute("type", "button");
      button.dataset.analyticsQueue = queue;
      button.addEventListener("click", () => openQueue(queue));
      queueActions.appendChild(button);
    });
    queues.appendChild(queueActions);
    grid.appendChild(queues);

    const timeInStage = analytics.time_in_stage || {};
    const coverage = timeInStage.coverage || {};
    const structuredTransitions = Math.max(0, Number(coverage.structured_transitions) || 0);
    const usableIntervals = Math.max(0, Number(coverage.usable_intervals) || 0);
    const legacyTransitions = Math.max(0, Number(coverage.legacy_transitions) || 0);
    const dwellRows = Array.isArray(timeInStage.stages) ? timeInStage.stages : [];
    if (timeInStage.status === "available" && dwellRows.length > 0) {
      const dwell = analyticsElement(documentObject, "article", "analytics-card analytics-card-wide");
      dwell.append(
        analyticsElement(documentObject, "h3", "", "Tempo em fase"),
        analyticsElement(
          documentObject,
          "p",
          "analytics-caption",
          `Cobertura ${usableIntervals} de ${structuredTransitions} transições estruturadas · ${legacyTransitions} transições legadas`,
        ),
      );
      dwellRows.forEach((row) => {
        const completed = Math.max(0, Number(row.completed_intervals) || 0);
        const average = Math.max(0, Number(row.average_hours) || 0);
        const item = analyticsElement(documentObject, "div", "analytics-stage-dwell");
        item.append(
          analyticsElement(documentObject, "strong", "", stageLabel(row.stage)),
          analyticsElement(
            documentObject,
            "span",
            "",
            `${average.toLocaleString("pt-PT", { maximumFractionDigits: 2 })} h em média · ${completed} ${completed === 1 ? "intervalo concluído" : "intervalos concluídos"}`,
          ),
        );
        dwell.appendChild(item);
      });
      grid.appendChild(dwell);
    } else {
      const unavailable = analyticsElement(
        documentObject,
        "p",
        "analytics-unavailable",
        `Tempo em fase indisponível — ${usableIntervals} intervalos utilizáveis em ${structuredTransitions} transições estruturadas; ${legacyTransitions} transições legadas não foram inferidas.`,
      );
      grid.appendChild(unavailable);
    }
    root.appendChild(grid);
  };

  const createLeadAnalyticsBehavior = ({
    fetchJson: requestJson,
    renderAnalytics,
    filterByStage,
    openQueue,
    onFailure,
  }) => ({
    load: async () => {
      try {
        const analytics = await requestJson("/api/v1/pipeline/analytics?days=30");
        renderAnalytics(analytics, { filterByStage, openQueue });
        return true;
      } catch (_error) {
        onFailure("Não foi possível sincronizar os indicadores.");
        return false;
      }
    },
  });

  const revealDetailOnMobile = ({ windowObject, detailPanel }) => {
    if (!windowObject.matchMedia("(max-width: 820px)").matches) return false;
    detailPanel.scrollIntoView({ block: "start", behavior: "auto" });
    return true;
  };

  const STAGE_LABELS = Object.freeze({
    new: "Novo",
    contacted: "Contactado",
    qualified: "Qualificado",
    meeting_booked: "Reunião marcada",
    meeting_held: "Reunião feita",
    proposal_requested: "Proposta pedida",
    proposal_sent: "Proposta enviada",
    negotiation: "Negociação",
    won: "Ganho",
    lost: "Perdido",
    not_a_fit: "Sem enquadramento",
    call: "Chamada", email: "Email", follow_up: "Acompanhamento", proposal_followup: "Acompanhar proposta",
    open: "Por fazer", completed: "Concluída", cancelled: "Cancelada", connected: "Atendeu", no_answer: "Não atendeu",
    voicemail: "Caixa de mensagens", wrong_number: "Número errado", not_interested: "Sem interesse", outbound: "Enviado", inbound: "Recebido",
    phone_new: "Nova prospeção certificada", phone_unknown: "Histórico telefónico desconhecido", calls_actionable: "Chamadas acionáveis",
    human_counterparty: "Humano / contraparte", ivr: "IVR", reception: "Receção", decision_maker: "Decisor", other: "Outro", unknown: "Desconhecido",
  });
  const PRIORITY_LABELS = Object.freeze({ high: "Alta", medium: "Média", low: "Baixa" });
  const stageLabel = (value) => STAGE_LABELS[value] || String(value || "Sem estado").replaceAll("_", " ");
  const priorityLabel = (value) => PRIORITY_LABELS[value] || "Sem prioridade";
  const formatDateTime = (value) => {
    if (!value) return "Sem data";
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? "Sem data"
      : date.toLocaleString("pt-PT", { dateStyle: "short", timeStyle: "short" });
  };
  const formatDateInTimezone = (date, timeZone = "Europe/Lisbon") => {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(date instanceof Date ? date : new Date(date));
    const value = Object.fromEntries(parts.filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
    return `${value.year}-${value.month}-${value.day}`;
  };
  const toAbsoluteISOString = (value, message) => {
    const date = new Date(value || "");
    if (Number.isNaN(date.getTime())) throw new Error(message);
    return date.toISOString();
  };
  const triState = (value) => {
    if (value === true || value === "yes" || value === "true") return true;
    if (value === false || value === "no" || value === "false") return false;
    return null;
  };
  const callIntent = (purpose, dueIso, evidenceRefs = [], gateReason = null) => ({
    schema_version: 1,
    purpose,
    agreed_with_client: purpose === "agreed_callback",
    calendar_policy: "none",
    obligation_key: `${purpose === "agreed_callback" ? "agreed-callback" : "internal-preparation"}:${dueIso}`,
    evidence_refs: Array.isArray(evidenceRefs) ? evidenceRefs : [],
    gate_reason: gateReason || null,
  });
  const buildCallDetails = (values) => {
    const allowedAnswerKinds = ["unknown", "human_counterparty", "no_answer", "ivr", "voicemail", "wrong_number"];
    const outcomeDefaults = { no_answer: "no_answer", voicemail: "voicemail", wrong_number: "wrong_number" };
    const answerKind = values.answer_kind || outcomeDefaults[values.outcome_code] || "unknown";
    if (!allowedAnswerKinds.includes(answerKind)) throw new Error("Escolhe o tipo de atendimento.");
    const contradictory = {
      no_answer: ["human_counterparty", "ivr", "voicemail", "wrong_number"],
      voicemail: ["human_counterparty", "no_answer", "ivr", "wrong_number"],
      wrong_number: ["human_counterparty", "no_answer", "ivr", "voicemail"],
    };
    if ((contradictory[values.outcome_code] || []).includes(answerKind)) {
      throw new Error("O resultado da chamada contradiz o tipo de atendimento escolhido.");
    }
    const useful = triState(values.useful);
    const decisionMaker = triState(values.decision_maker);
    const role = values.interlocutor_role || "unknown";
    if (!["unknown", "reception", "decision_maker", "other"].includes(role)) throw new Error("Escolhe o papel do interlocutor.");
    if (useful === true && answerKind !== "human_counterparty") throw new Error("Conversa útil requer atendimento humano.");
    if (decisionMaker === true && answerKind !== "human_counterparty") throw new Error("Decisor requer atendimento humano.");
    if (decisionMaker === true && role === "reception") throw new Error("A receção não pode ser marcada como decisor.");
    if (decisionMaker === true && role !== "decision_maker") throw new Error("Decisor requer papel de decisor.");
    const repeatReason = String(values.repeat_reason || "").trim();
    return {
      schema_version: 1,
      attempted: true,
      answer_kind: answerKind,
      useful,
      decision_maker: decisionMaker,
      interlocutor_role: role,
      repeat_reason: repeatReason || null,
      ...(Object.hasOwn(values, "first_conversation") ? { first_conversation: triState(values.first_conversation) } : {}),
    };
  };
  const buildNextActionPayload = (values) => {
    const dueIso = toAbsoluteISOString(values.due_at, "Escolhe a data e hora da próxima ação.");
    const payload = {
      task_type: values.task_type,
      title: String(values.title || "").trim(),
      due_at: dueIso,
    };
    if (payload.task_type === "call") payload.call_intent = callIntent("internal_preparation", dueIso);
    return payload;
  };
  const metricNumber = (value) => (Number.isInteger(value) && value >= 0 ? value : null);
  const displayMetric = (value) => (metricNumber(value) === null ? "—" : String(value));
  const normaliseCallMetrics = (metrics, fallbackDate) => {
    const counts = metrics?.counts || {};
    const coverage = metrics?.coverage || {};
    return {
      schema_version: 1,
      date: metrics?.date || fallbackDate,
      timezone: metrics?.timezone || "Europe/Lisbon",
      generated_at: metrics?.generated_at || null,
      source_status: ["available", "partial", "unavailable"].includes(metrics?.source_status) ? metrics.source_status : "partial",
      counts: Object.fromEntries(["attempts", "answered", "first_answered_confirmed", "first_answered_recorded", "first_answered_certified", "answered_novelty_unknown", "useful", "decision_maker", "followups_due", "followups_executed", "followups_pending"].map((key) => [key, metricNumber(counts[key])])),
      target_first_answered: metricNumber(metrics?.target_first_answered),
      deficit: metricNumber(metrics?.deficit),
      confirmed_deficit: metricNumber(metrics?.confirmed_deficit),
      coverage: Object.fromEntries(["answer_unknown", "useful_unknown", "decision_maker_unknown", "history_unknown_leads"].map((key) => [key, metricNumber(coverage[key])])),
      blockers: Array.isArray(metrics?.blockers) ? metrics.blockers.map((item) => String(item)) : [],
    };
  };
  const renderCallMetrics = ({ document: documentObject, root, metrics }) => {
    root.replaceChildren();
    const data = normaliseCallMetrics(metrics, metrics?.date || formatDateInTimezone(new Date()));
    const wrapper = analyticsElement(documentObject, "section", `call-metrics call-metrics-${data.source_status}`);
    const heading = analyticsElement(documentObject, "div", "analytics-heading");
    heading.append(
      analyticsElement(documentObject, "strong", "", `Meta chamadas ${data.date}`),
      analyticsElement(documentObject, "span", "analytics-caption", `Fonte ${data.source_status}`),
    );
    wrapper.appendChild(heading);
    const cards = analyticsElement(documentObject, "div", "analytics-breakdown");
    [
      ["Tentativas", data.counts.attempts],
      ["Atendidas", data.counts.answered],
      ["1ª confirmadas", data.counts.first_answered_confirmed],
      ["Úteis", data.counts.useful],
      ["Decisores", data.counts.decision_maker],
      ["Faltam para 10", data.confirmed_deficit],
    ].forEach(([label, value]) => {
      const chip = analyticsElement(documentObject, "span", "analytics-chip");
      chip.append(
        analyticsElement(documentObject, "span", "analytics-chip-label", label),
        analyticsElement(documentObject, "strong", "", displayMetric(value)),
      );
      cards.appendChild(chip);
    });
    wrapper.appendChild(cards);
    const coverage = analyticsElement(documentObject, "p", "analytics-caption", `Unknowns: atendimento ${displayMetric(data.coverage.answer_unknown)}, útil ${displayMetric(data.coverage.useful_unknown)}, decisor ${displayMetric(data.coverage.decision_maker_unknown)}, histórico ${displayMetric(data.coverage.history_unknown_leads)}`);
    wrapper.appendChild(coverage);
    if (data.blockers.length) wrapper.appendChild(analyticsElement(documentObject, "p", "analytics-warning", data.blockers.join(" · ")));
    root.appendChild(wrapper);
  };
  const createCallMetricsBehavior = ({
    requestJson,
    renderMetrics,
    onFailure = () => {},
    now = () => new Date(),
    timeZone = "Europe/Lisbon",
  }) => ({
    load: async () => {
      const date = formatDateInTimezone(now(), timeZone);
      try {
        renderMetrics(normaliseCallMetrics(await requestJson(`/api/v1/pipeline/call-metrics?date=${date}`), date));
        return true;
      } catch (error) {
        if (error?.status === 404 || error?.status === 503) {
          renderMetrics(normaliseCallMetrics({
            date,
            timezone: timeZone,
            source_status: "unavailable",
            counts: {},
            coverage: {},
            blockers: ["Métricas de chamadas indisponíveis durante rollout."],
          }, date));
          return false;
        }
        onFailure("Não foi possível sincronizar as métricas de chamadas.");
        return false;
      }
    },
  });
  const queueMetricValues = (summary) => {
    const queues = summary?.queues || {};
    const count = (name) => Math.max(0, Number(queues[name]) || 0);
    const values = {
      all: count("all"),
      touchedToday: count("touched_today"),
      callsDue: queues.calls_actionable === undefined ? count("calls_overdue") + count("calls_today") : count("calls_actionable"),
      emailsDue: count("emails_overdue") + count("emails_today"),
      proposalFollowupsDue: count("proposal_followups_overdue") + count("proposal_followups_today"),
    };
    if (queues.phone_new !== undefined) values.phoneNew = count("phone_new");
    if (queues.phone_unknown !== undefined) values.phoneUnknown = count("phone_unknown");
    if (queues.calls_actionable !== undefined) values.callsActionable = count("calls_actionable");
    return values;
  };
  const leadRowKey = (lead) => (
    lead?.task?.id ? `${lead.lead_id}:${lead.task.id}` : String(lead?.lead_id || "")
  );
  const resolveRefreshedLeadRow = (rows, leadId, rowKey) => (
    rows.find((item) => leadRowKey(item) === rowKey)
    || rows.find((item) => item.lead_id === leadId)
    || null
  );
  const leadRowView = (lead) => ({
    company: lead.company || "Sem empresa",
    contact: lead.contact_name || "—",
    phone: lead.phone || "—",
    email: lead.email || "—",
    stage: stageLabel(lead.stage),
    priority: priorityLabel(lead.priority),
    actionTitle: lead.task?.title || "Sem próxima ação",
    due: lead.task?.due_at ? formatDateTime(lead.task.due_at) : "—",
  });
  const leadNextActionView = (queueItem) => ({
    title: queueItem?.task?.title || "Sem próxima ação",
    due: queueItem?.task?.due_at ? formatDateTime(queueItem.task.due_at) : "—",
  });
  const callDetailsSummary = (details = null) => {
    if (!details || typeof details !== "object") return "Atendimento desconhecido · útil desconhecido · decisor desconhecido";
    const answered = details.answer_kind === "human_counterparty" ? "atendida humana" : details.answer_kind ? stageLabel(details.answer_kind) : "atendimento desconhecido";
    const useful = details.useful === true ? "útil sim" : details.useful === false ? "útil não" : "útil desconhecido";
    const decisionMaker = details.decision_maker === true ? "decisor sim" : details.decision_maker === false ? "decisor não" : "decisor desconhecido";
    const role = `papel ${stageLabel(details.interlocutor_role || "unknown")}`;
    return [answered, useful, decisionMaker, role, details.repeat_reason ? `repetição: ${details.repeat_reason}` : null].filter(Boolean).join(" · ");
  };

  const createCallDraftStore = (storage, now = () => Date.now()) => {
    const prefix = "zelus.crm.call-draft.v1:";
    const read = (leadId) => {
      try {
        const draft = JSON.parse(storage?.getItem(prefix + leadId) || "null");
        if (!draft || typeof draft !== "object" || now() - draft.updatedAt > 7 * 86400000) {
          storage?.removeItem(prefix + leadId);
          return null;
        }
        return draft;
      } catch (_) { return null; }
    };
    return {
      read,
      write: (leadId, values) => {
        if (!leadId) return false;
        try {
          storage?.setItem(prefix + leadId, JSON.stringify({ ...read(leadId), ...values, updatedAt: now() }));
          return !!storage;
        } catch (_) { return false; }
      },
      remove: (leadId) => { try { storage?.removeItem(prefix + leadId); } catch (_) {} },
    };
  };

  const buildCallPayload = (values, selectedTask = null) => {
    const outcomes = ["connected", "no_answer", "voicemail", "wrong_number", "not_interested", "follow_up"];
    if (!outcomes.includes(values.outcome_code)) throw new Error("Escolhe o resultado da chamada.");
    const payload = {
      outcome_code: values.outcome_code,
      summary: String(values.summary || "").trim() || null,
    };
    if (values.occurred_at) payload.occurred_at = toAbsoluteISOString(values.occurred_at, "Confirma a data e hora real da chamada.");
    if (values.answer_kind || values.useful || values.decision_maker || values.interlocutor_role || values.repeat_reason) {
      payload.call_details = buildCallDetails(values);
    }
    if (values.callback_enabled) {
      const dueIso = toAbsoluteISOString(values.callback_due_at, "Escolhe a data e hora para voltar a ligar.");
      if ((Object.hasOwn(values, "callback_agreed") || values.answer_kind || values.occurred_at) && !values.callback_agreed) throw new Error("Só marca callback quando tiver sido combinado com o cliente.");
      payload.next_action = {
        task_type: "call",
        title: String(values.callback_title || "").trim() || "Retomar a conversa",
        due_at: dueIso,
      };
      if (values.callback_agreed) payload.next_action.call_intent = callIntent("agreed_callback", dueIso);
    }
    if (selectedTask?.queue?.startsWith("calls_") && selectedTask.task?.type === "call") {
      payload.completed_task = { id: selectedTask.task.id, expected_version: selectedTask.task.version };
    }
    return payload;
  };

  // Keep the exact command across a lost response. Retrying never logs a second call.
  const createCallCommandBehavior = ({ createId, store, send }) => ({
    submit: async (leadId, expectedVersion, payload) => {
      const { completed_task, ...userIntent } = payload;
      const fingerprint = JSON.stringify(userIntent);
      let pending = store.read(leadId)?.pending;
      if (!pending || pending.fingerprint !== fingerprint) {
        pending = { fingerprint, commandId: createId(), expectedVersion, payload };
        store.write(leadId, { pending });
      }
      try {
        const result = await send(leadId, {
          command_id: pending.commandId, expected_version: pending.expectedVersion, ...(pending.payload || payload),
        });
        store.remove(leadId);
        return result;
      } catch (error) {
        if (error.status >= 400 && error.status < 500) store.write(leadId, { pending: null });
        throw error;
      }
    },
  });

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      createCallDraftStore,
      createCallCommandBehavior,
      buildCallPayload,
      buildCallDetails,
      buildNextActionPayload,
      createCallMetricsBehavior,
      renderCallMetrics,
      formatDateInTimezone,
      createLatestQueueLoader,
      createLeadQueueBehavior,
      createLeadAnalyticsBehavior,
      renderLeadAnalytics,
      revealDetailOnMobile,
      stageLabel,
      priorityLabel,
      queueMetricValues,
      leadRowKey,
      leadRowView,
      leadNextActionView,
      resolveRefreshedLeadRow,
    };
  }

  const show = (root, state) => {
    root.querySelectorAll("[data-state]").forEach((element) => {
      element.classList.toggle("hidden", element.dataset.state !== state);
    });
  };

  const fetchJson = async (url, options = {}) => {
    const response = await fetch(url, {
      credentials: "same-origin",
      ...options,
      headers: { Accept: "application/json", ...(options.headers || {}) },
    });
    if (!response.ok) { const error = new Error("CRM request unavailable"); error.status = response.status; throw error; }
    return response.json();
  };

  const appendText = (parent, className, text) => {
    const element = document.createElement("div");
    element.className = className;
    element.textContent = text;
    parent.appendChild(element);
    return element;
  };

  document.addEventListener("DOMContentLoaded", () => {
    const root = document.getElementById("leads-app");
    if (!root) return;

    const list = root.querySelector("[data-leads-list]");
    const search = root.querySelector("[data-lead-search]");
    const stageFilter = root.querySelector("[data-stage-filter]");
    const priorityFilter = root.querySelector("[data-priority-filter]");
    const previousPageButton = root.querySelector("[data-page-previous]");
    const nextPageButton = root.querySelector("[data-page-next]");
    const pageRange = root.querySelector("[data-page-range]");
    const skipButton = root.querySelector("[data-skip-lead]");
    const writable = root.dataset.writable === "true";
    const canWriteTasks = root.dataset.canWriteTasks === "true";
    const csrfToken = root.dataset.csrfToken || "";
    let activeQueue = "all";
    let queueItems = [];
    let selectedLeadId = null;
    let selectedRowKey = null;
    let viewIntentGeneration = 0;
    let currentLead = null;
    let currentSummary = { queues: {} };
    let draftStorage = null;
    try { draftStorage = window.localStorage; } catch (_) {}
    const callDrafts = createCallDraftStore(draftStorage);
    const callForm = root.querySelector("[data-call-log-form]");
    let savingCall = false;
    const localDateTimeValue = (date = new Date()) => {
      const offsetMs = date.getTimezoneOffset() * 60000;
      return new Date(date.getTime() - offsetMs).toISOString().slice(0, 16);
    };
    const readCallForm = () => callForm ? {
      outcome_code: callForm.elements.outcome_code.value,
      summary: callForm.elements.summary.value,
      occurred_at: callForm.elements.occurred_at?.value || "",
      answer_kind: callForm.elements.answer_kind?.value || "unknown",
      first_conversation: callForm.elements.first_conversation?.value || "unknown",
      useful: callForm.elements.useful?.value || "unknown",
      decision_maker: callForm.elements.decision_maker?.value || "unknown",
      interlocutor_role: callForm.elements.interlocutor_role?.value || "unknown",
      repeat_reason: callForm.elements.repeat_reason?.value || "",
      callback_enabled: !!callForm.elements.callback_enabled?.checked,
      callback_agreed: !!callForm.elements.callback_agreed?.checked,
      callback_due_at: callForm.elements.callback_due_at?.value || "",
      callback_title: callForm.elements.callback_title?.value || "",
    } : null;
    const syncCallback = () => {
      if (!callForm?.elements.callback_enabled) return;
      const enabled = callForm.elements.callback_enabled.checked;
      callForm.querySelector("[data-callback-fields]").classList.toggle("hidden", !enabled);
      callForm.elements.callback_due_at.required = enabled;
    };
    const persistCallDraft = () => {
      if (!selectedLeadId || !currentLead || !callForm) return;
      const values = readCallForm();
      if (!values.outcome_code && !values.summary && !values.callback_enabled && !values.callback_due_at && !values.callback_title && !values.repeat_reason) {
        if (callDrafts.read(selectedLeadId)?.pending) callDrafts.write(selectedLeadId, values);
        else callDrafts.remove(selectedLeadId);
        root.querySelector("[data-call-draft-status]").textContent = "";
        return;
      }
      const saved = callDrafts.write(selectedLeadId, values);
      root.querySelector("[data-call-draft-status]").textContent = saved ? "Rascunho guardado" : "Rascunho nesta página";
    };
    const restoreCallDraft = (leadId) => {
      if (!callForm) return;
      callForm.reset();
      const draft = callDrafts.read(leadId);
      if (callForm.elements.occurred_at) callForm.elements.occurred_at.value = localDateTimeValue();
      if (callForm.elements.answer_kind) callForm.elements.answer_kind.value = "unknown";
      if (callForm.elements.useful) callForm.elements.useful.value = "unknown";
      if (callForm.elements.decision_maker) callForm.elements.decision_maker.value = "unknown";
      if (callForm.elements.interlocutor_role) callForm.elements.interlocutor_role.value = "unknown";
      if (draft) {
        callForm.elements.outcome_code.value = draft.outcome_code || "";
        callForm.elements.summary.value = draft.summary || "";
        if (callForm.elements.occurred_at) callForm.elements.occurred_at.value = draft.occurred_at || callForm.elements.occurred_at.value;
        if (callForm.elements.answer_kind) callForm.elements.answer_kind.value = draft.answer_kind || "unknown";
        if (callForm.elements.useful) callForm.elements.useful.value = draft.useful || "unknown";
        if (callForm.elements.decision_maker) callForm.elements.decision_maker.value = draft.decision_maker || "unknown";
        if (callForm.elements.interlocutor_role) callForm.elements.interlocutor_role.value = draft.interlocutor_role || "unknown";
        if (callForm.elements.repeat_reason) callForm.elements.repeat_reason.value = draft.repeat_reason || "";
        if (callForm.elements.callback_enabled) {
          callForm.elements.callback_enabled.checked = !!draft.callback_enabled;
          if (callForm.elements.callback_agreed) callForm.elements.callback_agreed.checked = !!draft.callback_agreed;
          callForm.elements.callback_due_at.value = draft.callback_due_at || "";
          callForm.elements.callback_title.value = draft.callback_title || "";
        }
      }
      syncCallback();
      root.querySelector("[data-call-draft-status]").textContent = draft ? "Rascunho recuperado" : "";
      root.querySelector("[data-call-save-state]").textContent = "Guarda o resultado para continuar.";
    };
    const setContactLocation = (leadId, rowKey) => {
      const url = new URL(window.location.href);
      if (leadId) { url.searchParams.set("lead", leadId); url.searchParams.set("row", rowKey || leadId); }
      else { url.searchParams.delete("lead"); url.searchParams.delete("row"); }
      url.searchParams.set("queue", activeQueue);
      const state = queueLoader.getState();
      for (const key of ["search", "stage", "priority"]) {
        if (state[key]) url.searchParams.set(key, state[key]); else url.searchParams.delete(key);
      }
      if (state.offset) url.searchParams.set("offset", String(state.offset)); else url.searchParams.delete("offset");
      window.history.replaceState({}, "", url);
    };
    const markViewIntent = () => { viewIntentGeneration += 1; };

    const renderSummary = (summary) => {
      currentSummary = summary;
      root.querySelectorAll("[data-queue-count]").forEach((element) => {
        element.textContent = String(summary.queues?.[element.dataset.queueCount] ?? 0);
      });
      const metrics = queueMetricValues(summary);
      root.querySelectorAll("[data-metric-value]").forEach((element) => {
        element.textContent = String(metrics[element.dataset.metricValue] ?? 0);
      });
      root.querySelectorAll("[data-metric-targets]").forEach((button) => {
        const targets = button.dataset.metricTargets.split(",");
        button.setAttribute("aria-pressed", String(targets.includes(activeQueue)));
      });
    };

    const selectQueueButton = () => {
      root.querySelectorAll("[data-pipeline-queue]").forEach((button) => {
        button.setAttribute("aria-pressed", String(button.dataset.pipelineQueue === activeQueue));
      });
      root.querySelectorAll("[data-metric-targets]").forEach((button) => {
        const targets = button.dataset.metricTargets.split(",");
        button.setAttribute("aria-pressed", String(targets.includes(activeQueue)));
      });
    };

    const renderRows = (rows) => {
      list.querySelectorAll(".lead-row").forEach((row) => row.remove());
      rows.forEach((lead) => {
        const view = leadRowView(lead);
        const rowKey = leadRowKey(lead);
        const row = document.createElement("tr");
        row.className = "lead-row";
        row.dataset.leadId = lead.lead_id;
        row.dataset.rowKey = rowKey;
        row.setAttribute("aria-current", String(rowKey === selectedRowKey));

        const appendCell = (column, className, text) => {
          const cell = document.createElement("td");
          cell.dataset.column = column;
          cell.dataset.label = column;
          cell.className = className;
          cell.textContent = text;
          row.appendChild(cell);
          return cell;
        };
        const company = appendCell("company", "lead-company", "");
        const openButton = document.createElement("button");
        openButton.type = "button";
        openButton.className = "lead-open-button";
        openButton.textContent = view.company;
        openButton.setAttribute("aria-label", `Abrir lead de ${view.company}`);
        company.appendChild(openButton);
        appendCell("contact", "lead-contact", view.contact);
        const action = appendCell("due", "lead-action", view.actionTitle);
        appendText(action, "lead-due", view.due);
        appendCell("phone", "lead-phone", view.phone);
        appendCell("email", "lead-email", view.email);
        const stage = appendCell("stage", "lead-stage", view.stage);
        stage.dataset.stage = lead.stage || "";
        appendCell("priority", "lead-priority", view.priority);

        const open = () => loadLead(lead.lead_id, rowKey);
        row.addEventListener("click", open);
        list.appendChild(row);
      });
      show(root, rows.length ? "ready" : "empty");
    };

    // Search, stage and priority are applied to the full CRM by the server.
    // Re-filtering only this page would hide valid city/contact matches.
    const applyFilters = () => renderRows(queueItems);

    const loadSummary = async () => renderSummary(await fetchJson("/api/v1/pipeline/summary"));

    const renderPagination = ({ total, limit, offset }) => {
      const first = total > 0 ? offset + 1 : 0;
      const last = Math.min(offset + queueItems.length, total);
      pageRange.textContent = `${first}–${last} de ${total}`;
      previousPageButton.disabled = offset <= 0;
      nextPageButton.disabled = offset + limit >= total;
    };

    const queueLoader = createLatestQueueLoader({
      requestJson: fetchJson,
      onStart: (state) => {
        activeQueue = state.queue;
        stageFilter.value = state.stage;
        priorityFilter.value = state.priority;
        search.value = state.search;
        selectQueueButton();
        previousPageButton.disabled = true;
        nextPageButton.disabled = true;
        show(root, "loading");
      },
      onPage: (page, state) => {
        queueItems = Array.isArray(page.items) ? page.items : [];
        root.querySelector("[data-lead-total]").textContent = String(state.total);
        applyFilters();
        renderPagination(state);
      },
    });
    const loadQueue = (changes = {}) => queueLoader.load(changes);

    const taskCommand = async (task, action) => {
      if (!canWriteTasks || !csrfToken || task.status !== "open") return;
      const commandLeadId = selectedLeadId;
      const commandRowKey = selectedRowKey;
      const commandViewIntentGeneration = viewIntentGeneration;
      const commandId = crypto.randomUUID();
      const body = { command_id: commandId, expected_version: task.version };
      if (action === "reschedule") {
        const proposed = window.prompt(
          "Nova data e hora (AAAA-MM-DD HH:MM)",
          new Date(task.due_at).toISOString().slice(0, 16).replace("T", " "),
        );
        if (!proposed) return;
        const dueAt = new Date(proposed.replace(" ", "T"));
        if (Number.isNaN(dueAt.getTime())) {
          window.alert("Data inválida.");
          return;
        }
        body.due_at = dueAt.toISOString();
      }
      await fetchJson(`/api/v1/commands/tasks/${task.id}/${action}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
          "Idempotency-Key": commandId,
        },
        body: JSON.stringify(body),
      });
      await Promise.all([loadSummary(), loadQueue()]);
      if (
        selectedLeadId !== commandLeadId
        || selectedRowKey !== commandRowKey
        || viewIntentGeneration !== commandViewIntentGeneration
      ) return;
      const refreshedItem = resolveRefreshedLeadRow(
        queueItems,
        commandLeadId,
        commandRowKey,
      );
      if (refreshedItem) {
        await loadLead(commandLeadId, leadRowKey(refreshedItem));
      } else {
        clearSelection(commandLeadId, null);
      }
    };

    const leadCommandPath = (leadId, operation) => ({
      edit: `/api/v1/commands/leads/${leadId}/edit`,
      "transition-stage": `/api/v1/commands/leads/${leadId}/transition-stage`,
      "log-call": `/api/v1/commands/leads/${leadId}/log-call`,
      "log-email": `/api/v1/commands/leads/${leadId}/log-email`,
      "add-note": `/api/v1/commands/leads/${leadId}/add-note`,
      "schedule-next-action": `/api/v1/commands/leads/${leadId}/schedule-next-action`,
    })[operation];

    const callCommands = createCallCommandBehavior({
      createId: () => crypto.randomUUID(), store: callDrafts,
      send: (leadId, body) => fetchJson(leadCommandPath(leadId, "log-call"), {
        method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken, "Idempotency-Key": body.command_id }, body: JSON.stringify(body),
      }),
    });
    const postLeadCommand = async ({ operation, leadId, lead, payload }) => {
      if (!writable || !csrfToken || !leadId || !lead) return;
      if (operation === "log-call") {
        await callCommands.submit(leadId, lead.version, payload);
        if (selectedLeadId === leadId && callForm) { callForm.reset(); syncCallback(); }
        window.notify(payload.next_action ? "Chamada e callback guardados." : "Chamada guardada.");
        return;
      }
      const path = leadCommandPath(leadId, operation);
      if (!path) throw new Error("Unsupported command");
      const commandId = crypto.randomUUID();
      await fetchJson(path, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
          "Idempotency-Key": commandId,
        },
        body: JSON.stringify({
          command_id: commandId,
          expected_version: lead.version,
          ...payload,
        }),
      });
      window.notify("Alteração guardada.");
    };

    const optionalText = (form, name) => {
      const value = String(new FormData(form).get(name) || "").trim();
      return value || null;
    };

    const bindCommandForms = () => {
      const editForm = root.querySelector("[data-lead-edit-form]");
      editForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const data = new FormData(editForm);
        const advanceAfterSave = event.submitter?.dataset.advanceAfterSave === "true";
        try {
          await queueBehavior.save("edit", {
            priority: data.get("priority"),
            company_name: String(data.get("company_name") || "").trim(),
            contact_name: String(data.get("contact_name") || "").trim(),
            contact_email: String(data.get("contact_email") || "").trim(),
            contact_phone: String(data.get("contact_phone") || "").trim(),
          }, advanceAfterSave);
        } catch (_error) {
          window.notify("Não foi possível guardar os dados.", "err");
        }
      });

      const stageForm = root.querySelector("[data-stage-transition-form]");
      stageForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const data = new FormData(stageForm);
        try {
          await queueBehavior.save("transition-stage", {
            target_stage: data.get("target_stage"),
            reviewed_correction: data.get("reviewed_correction") === "on",
          }, false);
        } catch (_error) {
          window.notify("Não foi possível alterar a fase.", "err");
        }
      });

      callForm?.addEventListener("input", () => { syncCallback(); persistCallDraft(); });
      callForm?.addEventListener("change", () => { syncCallback(); persistCallDraft(); });
      callForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (savingCall || !currentLead || currentLead.suppressed) return;
        let payload;
        try { payload = buildCallPayload(readCallForm(), { queue: activeQueue, task: queueItems.find(item => leadRowKey(item) === selectedRowKey)?.task }); }
        catch (error) { window.notify(error.message, "err"); return; }
        persistCallDraft();
        savingCall = true;
        const button = callForm.querySelector("[data-call-save]");
        const saveState = root.querySelector("[data-call-save-state]");
        button.disabled = true;
        saveState.textContent = "A guardar chamada…";
        try {
          await queueBehavior.save("log-call", payload, true);
          if (!currentLead) {
            root.classList.remove("contact-open");
            document.body.classList.remove("call-focus");
            setContactLocation(null);
            window.notify("Chamada guardada. Chegaste ao fim desta fila.");
          }
        } catch (error) {
          const message = error.status === 409
            ? "Este contacto foi atualizado. O teu rascunho está guardado; abre-o de novo antes de guardar."
            : "Não foi possível confirmar. O rascunho está guardado; tenta novamente.";
          saveState.textContent = message;
          window.notify(message, "err");
        } finally { savingCall = false; button.disabled = false; }
      });

      const emailForm = root.querySelector("[data-email-log-form]");
      emailForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const data = new FormData(emailForm);
        try {
          await queueBehavior.save("log-email", {
            direction: data.get("direction"),
            summary: optionalText(emailForm, "summary"),
          }, false);
          emailForm.reset();
        } catch (_error) {
          window.notify("Não foi possível registar o email.", "err");
        }
      });

      const noteForm = root.querySelector("[data-note-form]");
      noteForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        try {
          await queueBehavior.save("add-note", {
            summary: String(new FormData(noteForm).get("summary") || "").trim(),
          }, false);
          noteForm.reset();
        } catch (_error) {
          window.notify("Não foi possível guardar a nota.", "err");
        }
      });

      const nextActionForm = root.querySelector("[data-next-action-form]");
      nextActionForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const data = new FormData(nextActionForm);
        try {
          await queueBehavior.save("schedule-next-action", buildNextActionPayload({
            task_type: data.get("task_type"),
            title: String(data.get("title") || "").trim(),
            due_at: String(data.get("due_at") || ""),
          }), false);
          nextActionForm.reset();
        } catch (error) {
          window.notify(error.message || "Não foi possível marcar a próxima ação.", "err");
        }
      });
    };

    const populateCommandForms = (detail) => {
      const form = root.querySelector("[data-lead-edit-form]");
      if (form) {
        form.elements.priority.value = detail.priority || "medium";
        form.elements.company_name.value = detail.company || "";
        form.elements.contact_name.value = detail.contact_name || "";
        form.elements.contact_email.value = detail.email || "";
        form.elements.contact_phone.value = detail.phone || "";
      }
      const stageForm = root.querySelector("[data-stage-transition-form]");
      if (stageForm && [...stageForm.elements.target_stage.options].some((option) => option.value === detail.stage)) {
        stageForm.elements.target_stage.value = detail.stage;
        stageForm.elements.reviewed_correction.checked = false;
      }
    };

    const renderTasks = (tasks) => {
      const container = root.querySelector("[data-lead-tasks]");
      container.replaceChildren();
      if (!tasks.length) {
        appendText(container, "task-item", "Sem tarefas.");
        return;
      }
      tasks.forEach((task) => {
        const item = document.createElement("div");
        item.className = "task-item";
        appendText(item, "task-title", task.title);
        appendText(item, "", [stageLabel(task.type), formatDateTime(task.due_at), stageLabel(task.status), task.call_intent?.purpose === "agreed_callback" ? "callback combinado" : task.call_intent?.purpose === "internal_preparation" ? "preparação interna" : null].filter(Boolean).join(" · "));
        if (canWriteTasks && task.status === "open") {
          const actions = root.querySelector("[data-task-actions-template]").content.cloneNode(true);
          actions.querySelector("[data-task-complete]").addEventListener("click", () => taskCommand(task, "complete"));
          actions.querySelector("[data-task-reschedule]").addEventListener("click", () => taskCommand(task, "reschedule"));
          actions.querySelector("[data-task-cancel]").addEventListener("click", () => taskCommand(task, "cancel"));
          item.appendChild(actions);
        }
        container.appendChild(item);
      });
    };

    const renderTimeline = (timeline) => {
      const container = root.querySelector("[data-lead-timeline]");
      container.replaceChildren();
      if (!timeline.length) {
        appendText(container, "timeline-item", "Sem histórico.");
        return;
      }
      timeline.forEach((activity) => {
        const item = document.createElement("div");
        item.className = "timeline-item";
        appendText(item, "task-title", ({ "Call logged": "Chamada registada", "Email logged": "Email registado", "Note added": "Nota adicionada", "Stage changed": "Fase atualizada", "Next action scheduled": "Próximo passo marcado" })[activity.title] || activity.title);
        appendText(
          item,
          "",
          [activity.actor_type === "migration" ? "Nota importada · data original desconhecida" : formatDateTime(activity.occurred_at), activity.outcome_code ? stageLabel(activity.outcome_code) : null, activity.direction ? stageLabel(activity.direction) : null, activity.call_details ? callDetailsSummary(activity.call_details) : activity.outcome_code ? callDetailsSummary(null) : null]
            .filter(Boolean)
            .join(" · "),
        );
        if (activity.summary) appendText(item, "", activity.summary);
        container.appendChild(item);
      });
    };

    const renderContactActions = (detail) => {
      const phoneLink = root.querySelector("[data-detail-phone-link]");
      const emailLink = root.querySelector("[data-detail-email-link]");
      phoneLink.classList.toggle("hidden", !detail.phone || detail.suppressed);
      emailLink.classList.toggle("hidden", !detail.email || detail.suppressed);
      phoneLink.removeAttribute("href");
      emailLink.removeAttribute("href");
      if (detail.phone && !detail.suppressed) phoneLink.href = `tel:${detail.phone.replace(/[^+\d*#;,]/g, "")}`;
      root.querySelector("[data-call-phone-number]").textContent = detail.phone || "";
      if (detail.email && !detail.suppressed) emailLink.href = `mailto:${detail.email}`;
    };

    const requestLead = async (leadId, rowKey) => {
      const queueItem = queueItems.find((item) => leadRowKey(item) === rowKey) || null;
      const [detail, timeline, tasks] = await Promise.all([
        fetchJson(`/api/v1/leads/${leadId}`),
        fetchJson(`/api/v1/leads/${leadId}/timeline?limit=50&offset=0`),
        fetchJson(`/api/v1/leads/${leadId}/tasks?limit=50&offset=0`),
      ]);
      return { detail, timeline, tasks, queueItem };
    };

    const clearSelection = (leadId, rowKey = leadId) => {
      persistCallDraft();
      selectedLeadId = leadId;
      selectedRowKey = rowKey;
      currentLead = null;
      applyFilters();
      root.querySelector("[data-detail-ready]").classList.add("hidden");
      root.querySelector("[data-detail-empty]").classList.remove("hidden");
    };

    const commitSelection = (_leadId, { detail, timeline, tasks, queueItem }) => {
      currentLead = detail;
      root.querySelector(".detail-head .eyebrow").textContent = detail.suppressed ? "Histórico do contacto" : "Em conversa";
      root.querySelector("[data-contact-protected]").classList.toggle("hidden", !detail.suppressed);
      callForm?.classList.toggle("hidden", !!detail.suppressed);
      root.querySelector("[data-next-action-form]")?.closest("details")?.classList.toggle("hidden", !!detail.suppressed);
      restoreCallDraft(selectedLeadId);
      root.classList.add("contact-open");
      document.body.classList.add("call-focus");
      setContactLocation(selectedLeadId, selectedRowKey);
      const taskItems = Array.isArray(tasks.items) ? tasks.items : [];
      const selectedAction = queueItem?.task || taskItems.find(task => task.status === "open");
      const nextAction = leadNextActionView({ task: selectedAction });
      root.querySelector("[data-detail-summary]").dataset.empty = String(!selectedAction);
      const recent = (timeline.items || []).find(item => item.summary);
      const context = root.querySelector("[data-call-context]");
      context.classList.toggle("hidden", !recent);
      if (recent) {
        context.querySelector("p").textContent = recent.summary;
        context.querySelector(".eyebrow").textContent = recent.actor_type === "migration" ? "Contexto importado · data original por confirmar" : "Última nota";
      }
      populateCommandForms(detail);
      root.querySelector("[data-detail-company]").textContent = detail.company;
      root.querySelector("[data-detail-contact]").textContent =
        [detail.contact_name, detail.email, detail.phone]
          .filter(Boolean)
          .join(" · ") || "Sem contacto";
      const detailStage = root.querySelector("[data-detail-stage]");
      detailStage.textContent = stageLabel(detail.stage);
      detailStage.dataset.stage = detail.stage || "";
      root.querySelector("[data-detail-priority]").textContent = priorityLabel(detail.priority);
      root.querySelector("[data-detail-next-action]").textContent = nextAction.title;
      root.querySelector("[data-detail-next-due]").textContent = nextAction.due;
      renderContactActions(detail);
      renderTasks(taskItems);
      renderTimeline(Array.isArray(timeline.items) ? timeline.items : []);
      root.querySelector("[data-detail-empty]").classList.add("hidden");
      root.querySelector("[data-detail-ready]").classList.remove("hidden");
      revealDetailOnMobile({
        windowObject: window,
        detailPanel: root.querySelector("[data-lead-detail-panel]"),
      });
    };

    const queueBehavior = createLeadQueueBehavior({
      getVisibleLeadIds: () => [...list.querySelectorAll(".lead-row[data-lead-id]")].map(
        (row) => row.dataset.leadId,
      ),
      getVisibleLeadRows: () => [...list.querySelectorAll(".lead-row[data-lead-id]")].map(
        (row) => ({ leadId: row.dataset.leadId, rowKey: row.dataset.rowKey }),
      ),
      getViewIntentGeneration: () => viewIntentGeneration,
      getSelection: () => ({ leadId: selectedLeadId, rowKey: selectedRowKey, lead: currentLead }),
      clearSelection,
      requestLead,
      commitSelection,
      postLead: postLeadCommand,
      refreshSummary: loadSummary,
      refreshQueue: loadQueue,
      refreshCallMetrics: () => callMetricsBehavior.load(),
      nextPageRow: async () => {
        if (!await queueLoader.next()) return null;
        const row = list.querySelector(".lead-row[data-lead-id]");
        return row ? { leadId: row.dataset.leadId, rowKey: row.dataset.rowKey } : null;
      },
      onReadFailure: () => window.notify(
        "Alteração guardada, mas não foi possível atualizar todos os dados.",
        "err",
      ),
    });
    const loadLead = queueBehavior.loadLead;
    const callMetricsContent = root.querySelector("[data-call-metrics]");
    const callMetricsBehavior = createCallMetricsBehavior({
      requestJson: fetchJson,
      renderMetrics: (metrics) => renderCallMetrics({ document, root: callMetricsContent, metrics }),
      onFailure: (message) => window.notify(message, "err"),
    });
    const analyticsContent = root.querySelector("[data-analytics-content]");
    const analyticsWarning = root.querySelector("[data-analytics-warning]");
    const analyticsBehavior = createLeadAnalyticsBehavior({
      fetchJson,
      renderAnalytics: (analytics, actions) => renderLeadAnalytics({
        document,
        root: analyticsContent,
        analytics,
        ...actions,
      }),
      filterByStage: async (stage) => {
        markViewIntent();
        try {
          await loadQueue({ stage, offset: 0 });
          stageFilter.focus();
        } catch (_error) {
          show(root, "error");
        }
      },
      openQueue: (queue) => {
        markViewIntent();
        return loadQueue({ queue, stage: "", offset: 0 }).catch(() => show(root, "error"));
      },
      onFailure: (message) => {
        root.querySelector("[data-analytics-loading]")?.classList.add("hidden");
        analyticsWarning.textContent = message;
        analyticsWarning.classList.remove("hidden");
      },
    });

    root.querySelectorAll("[data-metric-targets]").forEach((button) => {
      button.addEventListener("click", () => {
        markViewIntent();
        const targets = button.dataset.metricTargets.split(",");
        const queue = targets.find((target) => Number(currentSummary.queues?.[target] || 0) > 0)
          || targets[targets.length - 1];
        loadQueue({ queue, stage: "", offset: 0 }).catch(() => show(root, "error"));
      });
    });
    root.querySelectorAll("[data-pipeline-queue]").forEach((button) => {
      button.addEventListener("click", () => {
        markViewIntent();
        loadQueue({
          queue: button.dataset.pipelineQueue,
          stage: "",
          offset: 0,
        }).catch(() => show(root, "error"));
      });
    });
    let searchTimer;
    search.addEventListener("input", () => {
      markViewIntent();
      window.clearTimeout(searchTimer);
      const value = search.value.trim().slice(0, 200);
      searchTimer = window.setTimeout(() => loadQueue({ search: value, offset: 0 }).catch(() => show(root, "error")), 250);
    });
    stageFilter.addEventListener("change", () => {
      markViewIntent();
      loadQueue({
        stage: stageFilter.value,
        offset: 0,
      }).catch(() => show(root, "error"));
    });
    priorityFilter.addEventListener("change", () => {
      markViewIntent();
      loadQueue({
        priority: priorityFilter.value,
        offset: 0,
      }).catch(() => show(root, "error"));
    });
    previousPageButton.addEventListener("click", () => {
      markViewIntent();
      queueLoader.previous().catch(() => show(root, "error"));
    });
    nextPageButton.addEventListener("click", () => {
      markViewIntent();
      queueLoader.next().catch(() => show(root, "error"));
    });
    skipButton.addEventListener("click", async () => {
      persistCallDraft();
      try { if (!await queueBehavior.skip()) window.notify("Chegaste ao fim desta fila. O rascunho fica guardado."); }
      catch (_) { window.notify("Não foi possível abrir o próximo contacto.", "err"); }
    });
    root.querySelectorAll("[data-start-calls]").forEach(button => button.addEventListener("click", () => {
      const first = list.querySelector(".lead-row[data-lead-id]");
      if (first) loadLead(first.dataset.leadId, first.dataset.rowKey).catch(() => window.notify("Não foi possível abrir o contacto.", "err"));
      else window.notify("Não há contactos nesta fila.");
    }));
    root.querySelector("[data-exit-focus]").addEventListener("click", () => { root.classList.remove("contact-open"); document.body.classList.remove("call-focus"); });
    root.querySelector("[data-back-to-queue]").addEventListener("click", () => {
      persistCallDraft(); root.classList.remove("contact-open");
            document.body.classList.remove("call-focus"); setContactLocation(null);
      root.querySelector(".queue-heading").scrollIntoView({ block: "start" });
    });
    root.querySelector("[data-detail-phone-link]").addEventListener("click", persistCallDraft);
    window.addEventListener("pagehide", persistCallDraft);
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") persistCallDraft(); });
    root.querySelector("[data-today-label]").textContent = new Date().toLocaleDateString("pt-PT", { weekday:"long", day:"numeric", month:"long" });
    bindCommandForms();
    const newContactDialog = root.querySelector("[data-new-contact-dialog]");
    const newContactForm = root.querySelector("[data-new-contact-form]");
    if (newContactDialog && newContactForm) {
      const readNewContact = () => Object.fromEntries([...new FormData(newContactForm)].map(([key,value]) => [key,String(value).trim() || null]));
      root.querySelector("[data-new-contact]").addEventListener("click", () => {
        const draft = callDrafts.read("__new-contact__");
        if (draft?.values) Object.entries(draft.values).forEach(([key,value]) => { if (newContactForm.elements[key]) newContactForm.elements[key].value = value || ""; });
        newContactDialog.showModal();
      });
      root.querySelector("[data-close-contact]").addEventListener("click", () => newContactDialog.close());
      newContactForm.addEventListener("input", () => callDrafts.write("__new-contact__", { values: readNewContact() }));
      newContactForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = newContactForm.querySelector('[type="submit"]');
        if (button.disabled) return;
        const values = readNewContact();
        const fingerprint = JSON.stringify(values);
        let pending = callDrafts.read("__new-contact__")?.pending;
        if (!pending || pending.fingerprint !== fingerprint) pending = { fingerprint, commandId: crypto.randomUUID() };
        callDrafts.write("__new-contact__", { values, pending });
        button.disabled = true;
        const state = root.querySelector("[data-new-contact-state]"); state.textContent = "A criar contacto…";
        try {
          const result = await fetchJson("/api/v1/commands/leads", { method:"POST", headers:{ "Content-Type":"application/json", "X-CSRF-Token":csrfToken, "Idempotency-Key":pending.commandId }, body:JSON.stringify({command_id:pending.commandId,...values}) });
          callDrafts.remove("__new-contact__"); newContactForm.reset(); newContactDialog.close();
          search.value = ""; markViewIntent();
          window.notify("Contacto criado.");
          await loadQueue({queue:"all",stage:"",priority:"",search:"",offset:0}).catch(() => {});
          await loadLead(result.lead_id).catch(() => window.notify("Contacto criado. Não foi possível abrir o detalhe; atualiza a fila.","err"));
        } catch(error) {
          if (error.status >= 400 && error.status < 500) callDrafts.write("__new-contact__", { pending:null });
          state.textContent = "Não foi possível confirmar. Os dados ficam guardados; tenta novamente.";
        } finally { button.disabled = false; }
      });
    }

    analyticsBehavior.load();
    callMetricsBehavior.load();
    const initialParams = new URLSearchParams(window.location.search);
    const knownQueues = [...root.querySelectorAll("[data-pipeline-queue]")].map(button => button.dataset.pipelineQueue);
    const defaultCallQueue = knownQueues.includes("calls_actionable") ? "calls_actionable" : "calls_overdue";
    const initialQueue = knownQueues.includes(initialParams.get("queue")) ? initialParams.get("queue") : (initialParams.has("search") || initialParams.has("lead") ? "all" : defaultCallQueue);
    const initialStage = [...stageFilter.options].some(option => option.value === initialParams.get("stage")) ? initialParams.get("stage") : "";
    const initialPriority = ["low","medium","high"].includes(initialParams.get("priority")) ? initialParams.get("priority") : "";
    const initialOffset = Math.max(0, Math.min(1000000, parseInt(initialParams.get("offset"), 10) || 0));
    Promise.all([loadSummary(), loadQueue({ queue: initialQueue, search: (initialParams.get("search") || "").trim().slice(0,200), stage: initialStage, priority: initialPriority, offset: initialOffset })]).then(() => {
      const leadId = initialParams.get("lead");
      if (leadId) return loadLead(leadId, initialParams.get("row") || leadId);
    }).catch(() => show(root, "error"));
  });
})();
