(() => {
  "use strict";

  const createLeadQueueBehavior = ({
    getVisibleLeadIds,
    getVisibleLeadRows,
    getSelection,
    clearSelection,
    requestLead,
    commitSelection,
    postLead,
    refreshSummary = async () => {},
    refreshQueue = async () => {},
    onLoad = () => {},
    onReadFailure = () => {},
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

    const skip = () => {
      const { leadId, rowKey } = getSelection();
      const nextLead = nextVisibleLead(leadId, rowKey);
      return nextLead ? loadLead(nextLead.leadId, nextLead.rowKey) : Promise.resolve(false);
    };

    const save = async (operation, payload, advanceAfterSave) => {
      const { leadId, rowKey, lead } = getSelection();
      if (!leadId || !lead) return false;
      const saveSequence = loadSequence;
      const nextLead = advanceAfterSave ? nextVisibleLead(leadId, rowKey) : null;
      await postLead({ operation, leadId, lead, payload });

      await Promise.all([
        refreshSummary().catch((error) => onReadFailure("summary", error)),
        refreshQueue().catch((error) => onReadFailure("queue", error)),
      ]);
      if (saveSequence !== loadSequence) return true;

      const capturedTarget = advanceAfterSave
        ? nextLead
        : { leadId, rowKey: rowKey || leadId };
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
    not_a_fit: "Sem fit",
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
  const queueMetricValues = (summary) => {
    const queues = summary?.queues || {};
    const count = (name) => Math.max(0, Number(queues[name]) || 0);
    return {
      all: count("all"),
      touchedToday: count("touched_today"),
      callsDue: count("calls_overdue") + count("calls_today"),
      emailsDue: count("emails_overdue") + count("emails_today"),
      proposalFollowupsDue: count("proposal_followups_overdue") + count("proposal_followups_today"),
    };
  };
  const leadRowKey = (lead) => (
    lead?.task?.id ? `${lead.lead_id}:${lead.task.id}` : String(lead?.lead_id || "")
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

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
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
    if (!response.ok) throw new Error("CRM request unavailable");
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
    let currentLead = null;
    let currentSummary = { queues: {} };

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

    const applyFilters = () => {
      const query = search.value.trim().toLocaleLowerCase("pt-PT");
      renderRows(
        queueItems.filter((lead) => {
          const searchable = [lead.company, lead.contact_name, lead.email, lead.phone]
            .filter(Boolean)
            .join(" ")
            .toLocaleLowerCase("pt-PT");
          return !query || searchable.includes(query);
        }),
      );
    };

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
      if (selectedLeadId !== commandLeadId || selectedRowKey !== commandRowKey) return;
      const refreshedItem = queueItems.find((item) => item.lead_id === commandLeadId) || null;
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

    const postLeadCommand = async ({ operation, leadId, lead, payload }) => {
      if (!writable || !csrfToken || !leadId || !lead) return;
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

      const callForm = root.querySelector("[data-call-log-form]");
      callForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const data = new FormData(callForm);
        try {
          await queueBehavior.save("log-call", {
            outcome_code: data.get("outcome_code"),
            summary: optionalText(callForm, "summary"),
          }, false);
          callForm.reset();
        } catch (_error) {
          window.notify("Não foi possível registar a chamada.", "err");
        }
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
        const dueAt = new Date(String(data.get("due_at") || ""));
        if (Number.isNaN(dueAt.getTime())) {
          window.notify("Data inválida.", "err");
          return;
        }
        try {
          await queueBehavior.save("schedule-next-action", {
            task_type: data.get("task_type"),
            title: String(data.get("title") || "").trim(),
            due_at: dueAt.toISOString(),
          }, false);
          nextActionForm.reset();
        } catch (_error) {
          window.notify("Não foi possível marcar a próxima ação.", "err");
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
        appendText(item, "", `${stageLabel(task.type)} · ${formatDateTime(task.due_at)} · ${stageLabel(task.status)}`);
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
        appendText(item, "task-title", activity.title);
        appendText(
          item,
          "",
          [formatDateTime(activity.occurred_at), activity.outcome_code, activity.direction]
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
      phoneLink.classList.toggle("hidden", !detail.phone);
      emailLink.classList.toggle("hidden", !detail.email);
      phoneLink.removeAttribute("href");
      emailLink.removeAttribute("href");
      if (detail.phone) phoneLink.href = `tel:${detail.phone}`;
      if (detail.email) emailLink.href = `mailto:${detail.email}`;
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
      selectedLeadId = leadId;
      selectedRowKey = rowKey;
      currentLead = null;
      applyFilters();
      root.querySelector("[data-detail-ready]").classList.add("hidden");
      root.querySelector("[data-detail-empty]").classList.remove("hidden");
    };

    const commitSelection = (_leadId, { detail, timeline, tasks, queueItem }) => {
      currentLead = detail;
      const taskItems = Array.isArray(tasks.items) ? tasks.items : [];
      const nextAction = leadNextActionView(queueItem);
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
      getSelection: () => ({ leadId: selectedLeadId, rowKey: selectedRowKey, lead: currentLead }),
      clearSelection,
      requestLead,
      commitSelection,
      postLead: postLeadCommand,
      refreshSummary: loadSummary,
      refreshQueue: loadQueue,
      onReadFailure: () => window.notify(
        "Alteração guardada, mas não foi possível atualizar todos os dados.",
        "err",
      ),
    });
    const loadLead = queueBehavior.loadLead;
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
        try {
          await loadQueue({ stage, offset: 0 });
          stageFilter.focus();
        } catch (_error) {
          show(root, "error");
        }
      },
      openQueue: (queue) => loadQueue({ queue, stage: "", offset: 0 }).catch(() => show(root, "error")),
      onFailure: (message) => {
        root.querySelector("[data-analytics-loading]")?.classList.add("hidden");
        analyticsWarning.textContent = message;
        analyticsWarning.classList.remove("hidden");
      },
    });

    root.querySelectorAll("[data-metric-targets]").forEach((button) => {
      button.addEventListener("click", () => {
        const targets = button.dataset.metricTargets.split(",");
        const queue = targets.find((target) => Number(currentSummary.queues?.[target] || 0) > 0)
          || targets[targets.length - 1];
        loadQueue({ queue, stage: "", offset: 0 }).catch(() => show(root, "error"));
      });
    });
    root.querySelectorAll("[data-pipeline-queue]").forEach((button) => {
      button.addEventListener("click", () => loadQueue({
        queue: button.dataset.pipelineQueue,
        stage: "",
        offset: 0,
      }).catch(() => show(root, "error")));
    });
    search.addEventListener("input", applyFilters);
    stageFilter.addEventListener("change", () => loadQueue({
      stage: stageFilter.value,
      offset: 0,
    }).catch(() => show(root, "error")));
    priorityFilter.addEventListener("change", () => loadQueue({
      priority: priorityFilter.value,
      offset: 0,
    }).catch(() => show(root, "error")));
    previousPageButton.addEventListener("click", () => queueLoader.previous().catch(() => show(root, "error")));
    nextPageButton.addEventListener("click", () => queueLoader.next().catch(() => show(root, "error")));
    skipButton.addEventListener("click", () => queueBehavior.skip().catch(() => show(root, "error")));
    bindCommandForms();

    analyticsBehavior.load();
    Promise.all([loadSummary(), loadQueue()]).catch(() => show(root, "error"));
  });
})();
