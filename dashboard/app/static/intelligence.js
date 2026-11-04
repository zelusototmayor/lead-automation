(() => {
  "use strict";
  const RULE_LABELS = {
    held_meeting_without_notes: "Falta o resultado de uma reunião", promised_proposal_not_sent: "Proposta prometida por enviar",
    proposal_missing_next_action: "Proposta sem próximo passo", proposal_stale: "Proposta sem evolução recente",
    inbound_awaiting_response: "Resposta de cliente por tratar", meeting_without_calendar_event: "Reunião por confirmar na agenda",
    contradictory_value_status_sources: "Confirmar o valor ou o estado", matching_review_candidate: "Confirmar a empresa associada", value_review_candidate: "Confirmar o valor da proposta",
  };
  const STATUS_LABELS = { queued:"Em fila", running:"Em curso", waiting:"A aguardar", completed:"Concluído", failed:"Precisa de atenção" };
  const KIND_LABELS = { calendar_callback:"Callback na agenda", call_followup:"Seguimento da chamada", calendar_review:"Rever reunião", proposal_review:"Rever proposta", proposal_sent:"Proposta enviada", email_followup:"Seguimento de email", inbound_reply:"Resposta recebida", email_review:"Rever email", email_observation:"Atualizar oportunidade", proposal_discovery:"Associar proposta" };
  const NOTE_LABELS = {pending:"Pendente",processing:"Em curso",partial:"Parcial",blocked:"Bloqueado",failed:"Precisa de atenção",processed:"Processado"};
  const effectiveStatus = item => ({pending:"queued",processing:"running",partial:"waiting",blocked:"waiting",failed:"failed",processed:"completed"})[item.processing?.state] || item.status;
  const workView = item => ({
    title: KIND_LABELS[item.kind] || "Acompanhamento comercial",
    status: NOTE_LABELS[item.processing?.state] || STATUS_LABELS[item.status] || "Estado por confirmar",
    obligations: (item.processing?.obligations || []).map(ref => ref.task_status && ref.task_status !== "open" ? "Obrigação encerrada no CRM; não é prova de envio." : ref.task_type === "email" ? "Draft preparado para revisão; não enviado." : ref.task_type === "call" ? `Callback na agenda: ${ref.calendar_status === "verified" ? "confirmado" : "pendente"}.` : "Próxima ação interna registada."),
    summary: item.result?.summary || item.payload?.summary || item.payload?.subject || (item.status === "queued" ? "Aguarda execução pelo agente." : "Abre o contacto para consultar o contexto."),
    href: item.lead_id ? `/leads?lead=${encodeURIComponent(item.lead_id)}` : null,
  });
  const matchesFilter = (item, filter) => filter === "all" || (filter === "active" && ["queued","running"].includes(effectiveStatus(item))) || (filter === "attention" && ["waiting","failed"].includes(effectiveStatus(item))) || effectiveStatus(item) === filter;
  if (typeof module !== "undefined" && module.exports) module.exports = { workView, matchesFilter };
  const append = (parent, tag, className, value) => { const el=document.createElement(tag); el.className=className; el.textContent=String(value ?? ""); parent.appendChild(el); return el; };
  const date = value => { const d=new Date(value); return Number.isNaN(d.getTime()) ? "" : d.toLocaleString("pt-PT",{dateStyle:"short",timeStyle:"short"}); };
  document.addEventListener("DOMContentLoaded", () => {
    const root=document.getElementById("agent-workspace"); if (!root) return;
    let items=[], filter="all", loading=false;
    const render = () => {
      const list=root.querySelector("[data-work-list]"); list.replaceChildren();
      const visible=items.filter(item=>matchesFilter(item,filter));
      const state=root.querySelector("[data-work-state]"); state.classList.toggle("hidden",visible.length>0);
      state.textContent=filter === "attention" ? "Sem tarefas a precisar de atenção nesta vista." : "Ainda não há trabalho neste estado.";
      visible.forEach(item=>{
        const view=workView(item); const card=append(list,"article","agent-work-card","");
        const meta=append(card,"div","agent-card-meta",""); const status=append(meta,"span","work-status",view.status); status.dataset.status=item.status;
        append(meta,"time","subtle",date(item.updated_at)); append(card,"h3","",view.title); append(card,"p","section-note",view.summary);
        view.obligations.forEach(text=>append(card,"p","agent-next",text));
        if (item.result?.next_action) {
          const next=item.result.next_action; const title=typeof next === "string" ? next : next.title || next.summary || (next.due_at ? `Próximo passo: ${date(next.due_at)}` : "Próximo passo registado");
          append(card,"p","agent-next",title);
        }
        if (view.href) { const link=append(card,"a","btn","Abrir contacto →"); link.href=view.href; }
        const refs=item.result?.evidence || [];
        if (Array.isArray(refs) && refs.length) { const details=append(card,"details","source-details",""); append(details,"summary","","Ver fontes"); refs.forEach(ref=>append(details,"p","",typeof ref === "string" ? ref : ref.title || ref.source || "Fonte associada")); }
      });
    };
    const load = async () => {
      if (loading) return; loading=true;
      const results=await Promise.allSettled([
        fetch("/api/v1/agent-work?limit=50",{credentials:"same-origin",headers:{Accept:"application/json"}}).then(async r=>{if(!r.ok)throw Error("unavailable");return r.json();}),
        fetch("/api/v1/intelligence/recommendations?limit=100",{credentials:"same-origin",headers:{Accept:"application/json"}}).then(async r=>{if(!r.ok)throw Error("unavailable");return r.json();}),
      ]);
      if(results[0].status === "fulfilled") { items=results[0].value.items || []; render(); root.querySelector("[data-work-updated]").textContent=`Atualizado às ${new Date().toLocaleTimeString("pt-PT",{hour:"2-digit",minute:"2-digit"})}`; }
      else { const state=root.querySelector("[data-work-state]"); state.textContent="Não foi possível atualizar o trabalho dos agentes.";state.classList.remove("hidden"); }
      const list=root.querySelector("[data-recommendation-list]"); const state=root.querySelector("[data-recommendation-state]");
      if(results[1].status === "fulfilled") {
        list.replaceChildren(); const recommendations=results[1].value.items || []; state.classList.toggle("hidden",recommendations.length>0); state.textContent="Sem novos pontos a acompanhar.";
        recommendations.forEach(item=>{
          const card=append(list,"article","agent-work-card",""); const meta=append(card,"div","agent-card-meta","");
          append(meta,"span","priority-pill",({high:"Prioridade alta",medium:"Prioridade normal",low:"Sem urgência",critical:"Urgente"})[item.priority] || "A acompanhar");
          append(meta,"time","subtle",date(item.observed_at)); append(card,"h3","",RULE_LABELS[item.rule_code] || "Rever oportunidade"); append(card,"p","section-note",item.account_name);
          const link=append(card,"a","btn",item.proposal_id ? "Ver proposta →" : "Ver empresa →"); link.href=item.proposal_id?`/propostas/${encodeURIComponent(item.proposal_id)}`:`/contas/${encodeURIComponent(item.account_id)}`;
          if(item.evidence?.length) { const details=append(card,"details","source-details",""); append(details,"summary","","Ver fontes"); item.evidence.forEach(ref=>append(details,"p","",ref)); }
        });
      } else { state.textContent="Não foi possível atualizar os pontos a acompanhar.";state.classList.remove("hidden"); }
      loading=false;
    };
    root.querySelectorAll("[data-work-filter]").forEach(button=>button.addEventListener("click",()=>{filter=button.dataset.workFilter;root.querySelectorAll("[data-work-filter]").forEach(b=>b.setAttribute("aria-pressed",String(b===button)));render();}));
    root.querySelector("[data-refresh-agents]").addEventListener("click",load);
    document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible")load();});
    load();
  });
})();
