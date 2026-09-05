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

  const labels = { new:"Nova", contacted:"Contactada", qualified:"Qualificada", meeting_booked:"Reunião marcada", meeting_held:"Reunião realizada", proposal_requested:"Proposta pedida", proposal_sent:"Proposta enviada", negotiation:"Em negociação", won:"Cliente", lost:"Perdida", not_a_fit:"Sem enquadramento", prospect:"Potencial cliente", customer:"Cliente", active:"Ativa" };
  const label = value => labels[value] || String(value || "Por classificar").replaceAll("_"," ");

  const loadIndex = async (root) => {
    let accounts = [], total = 0, loading = false, generation = 0, searchTimer;
    const search = root.querySelector("[data-account-search]");
    const more = root.querySelector("[data-accounts-more]");
    const render = () => {
      const query = search.value.trim().toLocaleLowerCase("pt-PT");
      const visible = accounts; // The server searches company, city and every contact, not this page alone.
      const grid = root.querySelector('[data-state="ready"]'); grid.replaceChildren();
      visible.forEach(account => {
        const card = document.createElement("a"); card.className = "account-card"; card.href = `/contas/${encodeURIComponent(account.id)}`;
        const title = document.createElement("strong"); title.textContent = account.display_name;
        const stage = document.createElement("p"); stage.className = "subtle"; stage.textContent = [label(account.lifecycle_stage), account.sector].filter(Boolean).join(" · ");
        const metrics = document.createElement("div"); metrics.className = "metrics";
        [`${account.contact_count} contactos`, `${account.email_count} emails`, `${account.meeting_count} reuniões`, `${account.proposal_count} propostas`].forEach(value => { const span = document.createElement("span"); span.textContent=value; metrics.appendChild(span); });
        const next = document.createElement("p"); next.className="account-next-action"; next.textContent=account.next_action || "Próximo passo por definir";
        card.append(title,stage,metrics,next); grid.appendChild(card);
      });
      root.querySelector("[data-account-count]").textContent = query ? `${accounts.length} de ${total} empresas encontradas` : `${accounts.length} de ${total} empresas`;
      const empty = root.querySelector('[data-state="empty"]');
      empty.textContent = query ? "Não há empresas ou contactos que correspondam à pesquisa." : "Ainda não há empresas para mostrar. Adiciona um contacto em Hoje & chamadas.";
      show(root, visible.length ? "ready" : "empty");
      more.classList.toggle("hidden",accounts.length >= total); more.disabled=loading;
    };
    const loadPage = async (reset = false) => {
      if (loading && !reset) return;
      const requestGeneration=++generation; loading=true; more.disabled=true;
      const offset=reset ? 0 : accounts.length;
      const params=new URLSearchParams({limit:"100",offset:String(offset)});
      const query=search.value.trim().slice(0,200); if(query)params.set("search",query);
      if(reset)show(root,"loading");
      try {
        const response = await fetch(`/api/v1/accounts?${params}`, { credentials:"same-origin",headers:{Accept:"application/json"} });
        if (!response.ok) throw Error("accounts unavailable");
        const page = await response.json();
        if(requestGeneration !== generation)return;
        total = Number(page.total || 0);
        accounts=reset ? [...(page.items || [])] : [...accounts,...(page.items || [])]; loading=false; render();
      } catch (_) { if(requestGeneration !== generation)return; loading=false; more.disabled=false; if (reset || !accounts.length) show(root,"error"); else window.notify("Não foi possível carregar mais empresas.","err"); }
    };
    search.addEventListener("input",()=>{generation++;window.clearTimeout(searchTimer);searchTimer=window.setTimeout(()=>loadPage(true),250);});
    more.addEventListener("click",()=>loadPage());
    await loadPage(true);
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
      text(root, "lifecycle", label(account.lifecycle_stage));
      root.querySelector('[data-field="account-proposals-link"]').href = `/propostas?account_id=${encodeURIComponent(account.id)}`;
      root.querySelector('[data-field="account-calls-link"]').href = `/leads?search=${encodeURIComponent(account.display_name)}`;
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
          item.textContent = `${({email:"Email",call:"Chamada",meeting:"Reunião",note:"Nota",stage_transition:"Mudança de fase"})[reference.type] || label(reference.type)} · ${occurred}`;
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
