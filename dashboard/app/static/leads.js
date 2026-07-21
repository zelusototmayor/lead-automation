(() => {
  "use strict";

  const createLeadQueueBehavior = ({
    getVisibleLeadIds,
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

    const nextVisibleLeadId = (leadId) => {
      const visibleLeadIds = getVisibleLeadIds();
      const currentIndex = visibleLeadIds.indexOf(leadId);
      return currentIndex >= 0 ? visibleLeadIds[currentIndex + 1] || null : null;
    };

    const loadLead = async (leadId) => {
      if (!leadId) return false;
      const requestSequence = ++loadSequence;
      clearSelection(leadId);
      onLoad(leadId);
      let result;
      try {
        result = await requestLead(leadId);
      } catch (error) {
        if (requestSequence !== loadSequence) return false;
        throw error;
      }
      if (requestSequence !== loadSequence) return false;
      commitSelection(leadId, result);
      return true;
    };

    const skip = () => {
      const { leadId } = getSelection();
      const nextLeadId = nextVisibleLeadId(leadId);
      return nextLeadId ? loadLead(nextLeadId) : Promise.resolve(false);
    };

    const save = async (operation, payload, advanceAfterSave) => {
      const { leadId, lead } = getSelection();
      if (!leadId || !lead) return false;
      const saveSequence = loadSequence;
      const nextLeadId = advanceAfterSave ? nextVisibleLeadId(leadId) : null;
      await postLead({ operation, leadId, lead, payload });

      const reads = [];
      if (saveSequence === loadSequence) {
        const targetLeadId = advanceAfterSave ? nextLeadId : leadId;
        if (targetLeadId) {
          reads.push(loadLead(targetLeadId).catch((error) => onReadFailure("detail", error)));
        } else {
          clearSelection(leadId);
        }
      }
      reads.push(
        refreshSummary().catch((error) => onReadFailure("summary", error)),
        refreshQueue().catch((error) => onReadFailure("queue", error)),
      );
      await Promise.all(reads);
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
        analyticsElement(documentObject, "span", "", `Contactados ${Number(day.distinct_touched_leads || 0)}`),
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
    root.appendChild(grid);

    root.appendChild(analyticsElement(documentObject, "p", "analytics-unavailable", "Tempo em fase indisponível — ainda não existem transições tipadas suficientes."));
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

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      createLatestQueueLoader,
      createLeadQueueBehavior,
      createLeadAnalyticsBehavior,
      renderLeadAnalytics,
    };
  }

  const stageLabel = (value) => String(value || "sem estado").replaceAll("_", " ");
  const formatDateTime = (value) => {
    if (!value) return "Sem data";
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? "Sem data"
      : date.toLocaleString("pt-PT", { dateStyle: "short", timeStyle: "short" });
  };

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
    let currentLead = null;

    const renderSummary = (summary) => {
      root.querySelectorAll("[data-queue-count]").forEach((element) => {
        element.textContent = String(summary.queues?.[element.dataset.queueCount] ?? 0);
      });
    };

    const selectQueueButton = () => {
      root.querySelectorAll("[data-pipeline-queue]").forEach((button) => {
        button.setAttribute("aria-pressed", String(button.dataset.pipelineQueue === activeQueue));
      });
    };

    const renderRows = (rows) => {
      list.querySelectorAll(".lead-row:not(.lead-head)").forEach((row) => row.remove());
      rows.forEach((lead) => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "lead-row";
        row.dataset.leadId = lead.lead_id;
        row.setAttribute("aria-current", String(lead.lead_id === selectedLeadId));

        const identity = document.createElement("div");
        appendText(identity, "lead-company", lead.company);
        appendText(
          identity,
          "lead-contact",
          [lead.contact_name, lead.email, lead.phone].filter(Boolean).join(" · ") || "Sem contacto",
        );

        const stage = document.createElement("span");
        stage.className = "lead-stage";
        stage.textContent = stageLabel(lead.stage);

        const action = document.createElement("div");
        action.className = "lead-action";
        action.textContent = lead.task
          ? `${lead.task.title} · ${formatDateTime(lead.task.due_at)}`
          : "Sem próxima ação";

        row.append(identity, stage, action);
        row.addEventListener("click", () => loadLead(lead.lead_id));
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
      await Promise.all([loadSummary(), loadQueue(), loadLead(selectedLeadId)]);
    };

    const leadCommandPath = (leadId, operation) => ({
      edit: `/api/v1/commands/leads/${leadId}/edit`,
      "transition-stage": `/api/v1/commands/leads/${leadId}/transition-stage`,
      "log-call": `/api/v1/commands/leads/${leadId}/log-call`,
      "log-email": `/api/v1/commands/leads/${leadId}/log-email`,
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

    const requestLead = async (leadId) => {
      const [detail, timeline, tasks] = await Promise.all([
        fetchJson(`/api/v1/leads/${leadId}`),
        fetchJson(`/api/v1/leads/${leadId}/timeline?limit=50&offset=0`),
        fetchJson(`/api/v1/leads/${leadId}/tasks?limit=50&offset=0`),
      ]);
      return { detail, timeline, tasks };
    };

    const clearSelection = (leadId) => {
      selectedLeadId = leadId;
      currentLead = null;
      applyFilters();
      root.querySelector("[data-detail-ready]").classList.add("hidden");
      root.querySelector("[data-detail-empty]").classList.remove("hidden");
    };

    const commitSelection = (_leadId, { detail, timeline, tasks }) => {
      currentLead = detail;
      populateCommandForms(detail);
      root.querySelector("[data-detail-company]").textContent = detail.company;
      root.querySelector("[data-detail-contact]").textContent =
        [detail.contact_name, detail.email, detail.phone, stageLabel(detail.stage)]
          .filter(Boolean)
          .join(" · ") || "Sem contacto";
      renderContactActions(detail);
      renderTasks(Array.isArray(tasks.items) ? tasks.items : []);
      renderTimeline(Array.isArray(timeline.items) ? timeline.items : []);
      root.querySelector("[data-detail-empty]").classList.add("hidden");
      root.querySelector("[data-detail-ready]").classList.remove("hidden");
    };

    const queueBehavior = createLeadQueueBehavior({
      getVisibleLeadIds: () => [...list.querySelectorAll(".lead-row[data-lead-id]")].map(
        (row) => row.dataset.leadId,
      ),
      getSelection: () => ({ leadId: selectedLeadId, lead: currentLead }),
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
