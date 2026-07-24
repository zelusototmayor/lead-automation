(() => {
  "use strict";

  const show = (root, state) => {
    root.querySelectorAll("[data-state]").forEach((element) => {
      element.classList.toggle("hidden", element.dataset.state !== state);
    });
  };

  const text = (root, field, value) => {
    const element = root.querySelector(`[data-field="${field}"]`);
    if (element) element.textContent = value;
  };

  const loadIndex = async (root) => {
    try {
      const response = await fetch("/api/v1/accounts?limit=100&offset=0", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("accounts unavailable");
      const page = await response.json();
      if (!Array.isArray(page.items) || page.items.length === 0) {
        show(root, "empty");
        return;
      }
      const grid = root.querySelector('[data-state="ready"]');
      page.items.forEach((account) => {
        const card = document.createElement("a");
        card.className = "account-card";
        card.href = `/contas/${encodeURIComponent(account.id)}`;
        const title = document.createElement("strong");
        title.textContent = account.display_name;
        const stage = document.createElement("p");
        stage.className = "subtle";
        stage.textContent = account.lifecycle_stage;
        const metrics = document.createElement("div");
        metrics.className = "metrics";
        [
          `${account.email_count} emails`,
          `${account.meeting_count} reuniões`,
          `${account.proposal_count} propostas`,
        ].forEach((label) => {
          const item = document.createElement("span");
          item.textContent = label;
          metrics.appendChild(item);
        });
        card.append(title, stage, metrics);
        grid.appendChild(card);
      });
      show(root, "ready");
    } catch (_error) {
      show(root, "error");
    }
  };

  const loadDetail = async (root) => {
    try {
      const accountId = root.dataset.accountId;
      const response = await fetch(`/api/v1/accounts/${encodeURIComponent(accountId)}`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("account unavailable");
      const account = await response.json();
      text(root, "display-name", account.display_name);
      text(root, "lifecycle", account.lifecycle_stage);
      text(root, "emails", String(account.email_count));
      text(root, "meetings", String(account.meeting_count));
      text(root, "proposals", String(account.proposal_count));
      text(
        root,
        "probability",
        account.probability == null ? "—" : `${Math.round(account.probability * 100)}%`,
      );
      text(root, "next-action", account.next_action || "Sem próxima ação registada.");
      const evidence = root.querySelector('[data-field="evidence"]');
      if (Array.isArray(account.evidence_refs) && account.evidence_refs.length) {
        account.evidence_refs.forEach((reference) => {
          const item = document.createElement("p");
          item.className = "subtle";
          const occurred = new Date(reference.occurred_at).toLocaleString("pt-PT");
          item.textContent = `${reference.type} · ${occurred}`;
          evidence.appendChild(item);
        });
      } else {
        root.querySelector("[data-evidence-empty]").classList.remove("hidden");
      }
      show(root, "ready");
    } catch (_error) {
      show(root, "error");
    }
  };

  document.addEventListener("DOMContentLoaded", () => {
    const index = document.getElementById("accounts-app");
    const detail = document.getElementById("account-app");
    if (index) loadIndex(index);
    if (detail) loadDetail(detail);
  });
})();
