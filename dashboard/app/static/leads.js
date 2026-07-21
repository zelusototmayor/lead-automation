(() => {
  "use strict";

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
      const selectedStage = stageFilter.value;
      renderRows(
        queueItems.filter((lead) => {
          const searchable = [lead.company, lead.contact_name, lead.email, lead.phone]
            .filter(Boolean)
            .join(" ")
            .toLocaleLowerCase("pt-PT");
          return (!query || searchable.includes(query)) && (!selectedStage || lead.stage === selectedStage);
        }),
      );
    };

    const refreshStageOptions = () => {
      const previous = stageFilter.value;
      stageFilter.querySelectorAll("option:not(:first-child)").forEach((option) => option.remove());
      [...new Set(queueItems.map((lead) => lead.stage).filter(Boolean))].sort().forEach((stage) => {
        const option = document.createElement("option");
        option.value = stage;
        option.textContent = stageLabel(stage);
        stageFilter.appendChild(option);
      });
      stageFilter.value = [...stageFilter.options].some((option) => option.value === previous) ? previous : "";
    };

    const loadSummary = async () => renderSummary(await fetchJson("/api/v1/pipeline/summary"));

    const loadQueue = async (queue = activeQueue) => {
      activeQueue = queue;
      selectQueueButton();
      show(root, "loading");
      const page = await fetchJson(`/api/v1/pipeline/items?queue=${encodeURIComponent(queue)}&limit=100&offset=0`);
      queueItems = Array.isArray(page.items) ? page.items : [];
      root.querySelector("[data-lead-total]").textContent = String(page.total ?? queueItems.length);
      refreshStageOptions();
      applyFilters();
    };

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

    const leadCommandPath = (operation) => ({
      edit: `/api/v1/commands/leads/${selectedLeadId}/edit`,
      "transition-stage": `/api/v1/commands/leads/${selectedLeadId}/transition-stage`,
      "log-call": `/api/v1/commands/leads/${selectedLeadId}/log-call`,
      "log-email": `/api/v1/commands/leads/${selectedLeadId}/log-email`,
      "schedule-next-action": `/api/v1/commands/leads/${selectedLeadId}/schedule-next-action`,
    })[operation];

    const postLeadCommand = async (operation, payload) => {
      if (!writable || !csrfToken || !selectedLeadId || !currentLead) return;
      const path = leadCommandPath(operation);
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
          expected_version: currentLead.version,
          ...payload,
        }),
      });
      await Promise.all([loadSummary(), loadQueue(), loadLead(selectedLeadId)]);
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
        try {
          await postLeadCommand("edit", {
            priority: data.get("priority"),
            company_name: String(data.get("company_name") || "").trim(),
            contact_name: String(data.get("contact_name") || "").trim(),
            contact_email: String(data.get("contact_email") || "").trim(),
            contact_phone: String(data.get("contact_phone") || "").trim(),
          });
        } catch (_error) {
          window.notify("Não foi possível guardar os dados.", "err");
        }
      });

      const stageForm = root.querySelector("[data-stage-transition-form]");
      stageForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const data = new FormData(stageForm);
        try {
          await postLeadCommand("transition-stage", {
            target_stage: data.get("target_stage"),
            reviewed_correction: data.get("reviewed_correction") === "on",
          });
        } catch (_error) {
          window.notify("Não foi possível alterar a fase.", "err");
        }
      });

      const callForm = root.querySelector("[data-call-log-form]");
      callForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const data = new FormData(callForm);
        try {
          await postLeadCommand("log-call", {
            outcome_code: data.get("outcome_code"),
            summary: optionalText(callForm, "summary"),
          });
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
          await postLeadCommand("log-email", {
            direction: data.get("direction"),
            summary: optionalText(emailForm, "summary"),
          });
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
          await postLeadCommand("schedule-next-action", {
            task_type: data.get("task_type"),
            title: String(data.get("title") || "").trim(),
            due_at: dueAt.toISOString(),
          });
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

    const loadLead = async (leadId) => {
      if (!leadId) return;
      selectedLeadId = leadId;
      applyFilters();
      const [detail, timeline, tasks] = await Promise.all([
        fetchJson(`/api/v1/leads/${leadId}`),
        fetchJson(`/api/v1/leads/${leadId}/timeline?limit=50&offset=0`),
        fetchJson(`/api/v1/leads/${leadId}/tasks?limit=50&offset=0`),
      ]);
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

    root.querySelectorAll("[data-pipeline-queue]").forEach((button) => {
      button.addEventListener("click", () => loadQueue(button.dataset.pipelineQueue).catch(() => show(root, "error")));
    });
    search.addEventListener("input", applyFilters);
    stageFilter.addEventListener("change", applyFilters);
    bindCommandForms();

    Promise.all([loadSummary(), loadQueue()]).catch(() => show(root, "error"));
  });
})();
