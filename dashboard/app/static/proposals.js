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
      (proposal.versions || []).forEach((version) => {
        const card = document.createElement("div");
        appendText(card, "strong", `Versão ${version.version_number} · ${version.status}`);
        appendText(card, "p", `One-off ${money(version.one_off_amount, proposal.currency)} · MRR ${money(version.mrr_amount, proposal.currency)} · ARR ${money(version.arr_amount, proposal.currency)}`, "subtle");
        appendText(card, "p", version.source_document_evidence_id ? "Evidência associada" : "Sem evidência documental", "subtle");
        versions.appendChild(card);
      });
      if (!(proposal.versions || []).length) root.querySelector("[data-versions-empty]").classList.remove("hidden");
      const followups = root.querySelector('[data-field="followups"]');
      (proposal.followups || []).forEach((item) => appendText(followups, "p", `${item.channel} · ${new Date(item.occurred_at).toLocaleString("pt-PT")}`, "subtle"));
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
