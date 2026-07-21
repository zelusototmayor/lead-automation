(() => {
  "use strict";

  const show = (root, state) => {
    root.querySelectorAll("[data-state]").forEach((element) => {
      element.classList.toggle("hidden", element.dataset.state !== state);
    });
  };
  const setText = (root, field, value) => {
    const element = root.querySelector(`[data-field="${field}"]`);
    if (element) element.textContent = value;
  };
  const money = (amount, currency) =>
    amount == null
      ? "—"
      : new Intl.NumberFormat("pt-PT", { style: "currency", currency }).format(amount);
  const appendText = (parent, tag, value, className = "") => {
    const element = document.createElement(tag);
    element.textContent = value;
    if (className) element.className = className;
    parent.appendChild(element);
    return element;
  };

  const renderPortfolio = (root, portfolio) => {
    const target = root.querySelector('[data-field="portfolio"]');
    target.replaceChildren();
    const counts = portfolio.value_counts || {};
    [["Propostas", portfolio.proposal_count], ["Sem valor", counts.missing || 0], ["Candidato", counts.candidate || 0], ["Confirmado", counts.confirmed || 0]].forEach(([label, value]) => {
      const card = document.createElement("div");
      card.className = "portfolio-card";
      appendText(card, "strong", String(value));
      appendText(card, "p", label, "subtle");
      target.appendChild(card);
    });
    Object.entries(portfolio.open_pipeline || {}).forEach(([currency, dimensions]) => {
      const card = document.createElement("div");
      card.className = "portfolio-card";
      appendText(card, "strong", `${currency} · pipeline aberto`);
      appendText(card, "p", `One-off ${money(dimensions.one_off, currency)} · MRR ${money(dimensions.mrr, currency)} · ARR ${money(dimensions.arr, currency)}`, "subtle");
      target.appendChild(card);
    });
  };

  const loadIndex = async (root) => {
    show(root, "loading");
    try {
      const params = new URLSearchParams();
      new FormData(root.querySelector("[data-filters]")).forEach((value, key) => {
        if (String(value).trim()) params.set(key, String(value).trim());
      });
      params.set("limit", "100");
      params.set("offset", "0");
      const [pageResponse, portfolioResponse] = await Promise.all([
        fetch(`/api/v1/proposals?${params}`, { credentials: "same-origin", headers: { Accept: "application/json" } }),
        fetch("/api/v1/proposals/portfolio", { credentials: "same-origin", headers: { Accept: "application/json" } }),
      ]);
      if (!pageResponse.ok || !portfolioResponse.ok) throw new Error("proposals unavailable");
      const [page, portfolio] = await Promise.all([pageResponse.json(), portfolioResponse.json()]);
      renderPortfolio(root, portfolio);
      const list = root.querySelector('[data-state="ready"]');
      list.replaceChildren();
      if (!Array.isArray(page.items) || page.items.length === 0) return show(root, "empty");
      page.items.forEach((proposal) => {
        const card = document.createElement("a");
        card.className = "proposal-card";
        card.href = `/propostas/${encodeURIComponent(proposal.id)}`;
        const heading = document.createElement("div");
        appendText(heading, "strong", proposal.title);
        appendText(heading, "p", proposal.account_name, "subtle");
        card.appendChild(heading);
        appendText(card, "span", proposal.status);
        appendText(card, "span", proposal.value_state);
        appendText(card, "span", proposal.age_days == null ? "Envio não verificado" : `${proposal.age_days} dias`);
        list.appendChild(card);
      });
      show(root, "ready");
    } catch (_error) {
      show(root, "error");
    }
  };

  const localDateTime = (value) => {
    if (!value) return "";
    const date = new Date(value);
    const offset = date.getTimezoneOffset() * 60000;
    return new Date(date.getTime() - offset).toISOString().slice(0, 16);
  };

  const loadDetail = async (root) => {
    try {
      const response = await fetch(`/api/v1/proposals/${encodeURIComponent(root.dataset.proposalId)}`, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("proposal unavailable");
      const proposal = await response.json();
      setText(root, "title", proposal.title);
      setText(root, "account-name", proposal.account_name);
      root.querySelector('[data-field="account-link"]').href = `/contas/${encodeURIComponent(proposal.account_id)}`;
      setText(root, "status", proposal.status);
      setText(root, "one-off", money(proposal.one_off_amount, proposal.currency));
      setText(root, "mrr", money(proposal.mrr_amount, proposal.currency));
      setText(root, "arr", money(proposal.arr_amount, proposal.currency));
      setText(root, "value-state", proposal.value_state);
      setText(root, "source-state", proposal.sent_verification_state ? `Origem do envio: ${proposal.sent_verification_state}` : "Ainda sem envio registado.");
      setText(root, "next-action", proposal.next_action || "Sem próxima ação registada.");
      const versions = root.querySelector('[data-field="versions"]');
      versions.replaceChildren();
      (proposal.versions || []).forEach((version) => {
        const card = document.createElement("div");
        appendText(card, "strong", `Versão ${version.version_number} · ${version.status}`);
        appendText(card, "p", `One-off ${money(version.one_off_amount, proposal.currency)} · MRR ${money(version.mrr_amount, proposal.currency)} · ARR ${money(version.arr_amount, proposal.currency)}`, "subtle");
        appendText(card, "p", version.source_document_evidence_id ? "Evidência associada" : "Sem evidência documental", "subtle");
        versions.appendChild(card);
      });
      if (!(proposal.versions || []).length) root.querySelector("[data-versions-empty]").classList.remove("hidden");
      const followups = root.querySelector('[data-field="followups"]');
      followups.replaceChildren();
      (proposal.followups || []).forEach((item) => appendText(followups, "p", `${item.channel} · ${new Date(item.occurred_at).toLocaleString("pt-PT")}`, "subtle"));

      const form = root.querySelector("[data-proposal-pipeline-form]");
      if (form && root.dataset.canWriteProposals === "true" && root.dataset.csrfToken) {
        form.elements.status.value = proposal.status;
        form.elements.probability.value = proposal.probability ?? "";
        form.elements.forecast_category.value = proposal.forecast_category ?? "";
        form.elements.next_action.value = proposal.next_action ?? "";
        form.elements.next_action_due_at.value = localDateTime(proposal.next_action_due_at);
        form.elements.lost_reason.value = proposal.lost_reason ?? "";
        form.addEventListener("submit", async (event) => {
          event.preventDefault();
          const commandState = root.querySelector("[data-proposal-command-state]");
          const button = form.querySelector('button[type="submit"]');
          const commandId = crypto.randomUUID();
          const data = new FormData(form);
          const textOrNull = (name) => String(data.get(name) || "").trim() || null;
          const dueAt = textOrNull("next_action_due_at");
          const body = {
            command_id: commandId,
            expected_version: proposal.version,
            status: String(data.get("status")),
            probability: textOrNull("probability"),
            forecast_category: textOrNull("forecast_category"),
            next_action: textOrNull("next_action"),
            next_action_due_at: dueAt ? new Date(dueAt).toISOString() : null,
            lost_reason: textOrNull("lost_reason"),
          };
          button.disabled = true;
          commandState.textContent = "A guardar…";
          try {
            const commandResponse = await fetch(`/api/v1/commands/proposals/${encodeURIComponent(proposal.id)}/update-pipeline`, {
              method: "POST",
              credentials: "same-origin",
              headers: {
                Accept: "application/json",
                "Content-Type": "application/json",
                "X-CSRF-Token": root.dataset.csrfToken,
                "Idempotency-Key": commandId,
              },
              body: JSON.stringify(body),
            });
            if (!commandResponse.ok) throw new Error("proposal command failed");
            window.location.reload();
          } catch (_error) {
            commandState.textContent = "Não foi possível guardar. Atualize a página e tente novamente.";
            button.disabled = false;
          }
        });
      }
      show(root, "ready");
    } catch (_error) {
      show(root, "error");
    }
  };

  document.addEventListener("DOMContentLoaded", () => {
    const index = document.getElementById("proposals-app");
    const detail = document.getElementById("proposal-app");
    if (index) {
      index.querySelector("[data-filters]").addEventListener("submit", (event) => { event.preventDefault(); loadIndex(index); });
      loadIndex(index);
    }
    if (detail) loadDetail(detail);
  });
})();
