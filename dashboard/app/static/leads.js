(() => {
  "use strict";

  const stageLabel = (value) => String(value || "sem estado").replaceAll("_", " ");
  const formatDueAt = (value) => {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : ` · ${date.toLocaleDateString("pt-PT")}`;
  };

  const show = (root, state) => {
    root.querySelectorAll("[data-state]").forEach((element) => {
      element.classList.toggle("hidden", element.dataset.state !== state);
    });
  };

  const render = (root, rows) => {
    const list = root.querySelector("[data-leads-list]");
    list.querySelectorAll(".lead-row:not(.lead-head)").forEach((row) => row.remove());
    rows.forEach((lead) => {
      const row = document.createElement("article");
      row.className = "lead-row";

      const identity = document.createElement("div");
      const company = document.createElement(lead.account_id ? "a" : "div");
      company.className = "lead-company";
      company.textContent = lead.company;
      if (lead.account_id) company.href = `/contas/${encodeURIComponent(lead.account_id)}`;
      const contact = document.createElement("div");
      contact.className = "lead-contact";
      contact.textContent = [lead.contact_name, lead.email, lead.phone].filter(Boolean).join(" · ") || "Sem contacto";
      identity.append(company, contact);

      const stage = document.createElement("span");
      stage.className = "lead-stage";
      stage.textContent = stageLabel(lead.stage);

      const done = document.createElement("div");
      done.className = "lead-action";
      const proposalLabel = `${lead.proposal_count} proposta${lead.proposal_count === 1 ? "" : "s"}`;
      done.textContent = `${proposalLabel} · atualizado ${new Date(lead.updated_at).toLocaleDateString("pt-PT")}`;

      const action = document.createElement("div");
      action.className = "lead-action";
      action.textContent = lead.next_action ? `${lead.next_action}${formatDueAt(lead.next_action_due_at)}` : "Sem próxima ação";

      row.append(identity, stage, done, action);
      list.appendChild(row);
    });
    show(root, rows.length ? "ready" : "empty");
  };

  document.addEventListener("DOMContentLoaded", async () => {
    const root = document.getElementById("leads-app");
    if (!root) return;
    try {
      const response = await fetch("/api/v1/leads?limit=100&offset=0", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("leads unavailable");
      const page = await response.json();
      const leads = Array.isArray(page.items) ? page.items : [];
      root.querySelector("[data-lead-total]").textContent = String(page.total ?? leads.length);
      const search = root.querySelector("[data-lead-search]");
      const stageFilter = root.querySelector("[data-stage-filter]");
      [...new Set(leads.map((lead) => lead.stage).filter(Boolean))].sort().forEach((stage) => {
        const option = document.createElement("option");
        option.value = stage;
        option.textContent = stageLabel(stage);
        stageFilter.appendChild(option);
      });
      const applyFilters = () => {
        const query = search.value.trim().toLocaleLowerCase("pt-PT");
        const selectedStage = stageFilter.value;
        render(root, leads.filter((lead) => {
          const searchable = [lead.company, lead.contact_name, lead.email, lead.phone].filter(Boolean).join(" ").toLocaleLowerCase("pt-PT");
          return (!query || searchable.includes(query)) && (!selectedStage || lead.stage === selectedStage);
        }));
      };
      search.addEventListener("input", applyFilters);
      stageFilter.addEventListener("change", applyFilters);
      applyFilters();
    } catch (_error) {
      show(root, "error");
    }
  });
})();
