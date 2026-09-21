/* Commercial-call KPIs are separate from legacy useful/CallDetails. */
(function (global) {
  "use strict";
  const goalView = data => ({
    confirmed: Number.isSafeInteger(data.weekly_new_significant_goal?.confirmed) ? data.weekly_new_significant_goal.confirmed : null,
    label: "novas conversas significativas",
  });
  const createController = ({request, render}) => {
    let sequence = 0, date, period;
    const load = async (nextDate, nextPeriod) => {
      date = nextDate; period = nextPeriod;
      const current = ++sequence;
      render({state: "loading"});
      try {
        const response = await request(`/api/v1/pipeline/call-metrics?date=${encodeURIComponent(date)}&period=${encodeURIComponent(period)}`);
        if (current !== sequence) return false;
        if (!response.commercial_kpis_v1) throw new Error("KPI unavailable");
        render({state: "ready", data: response.commercial_kpis_v1});
        return true;
      } catch (_) {
        if (current === sequence) render({state: "error"});
        return false;
      }
    };
    return {load, invalidate: () => load(date, period)};
  };
  const mount = (root, leadRoot) => {
    if (!root) return null;
    const doc = root.ownerDocument;
    const el = (tag, text, cls) => {
      const node = doc.createElement(tag);
      if (text !== undefined) node.textContent = text;
      if (cls) node.className = cls;
      return node;
    };
    const request = async (path, options = {}) => {
      const response = await fetch(path, {credentials: "same-origin", ...options});
      if (!response.ok) { const error = new Error("KPI request failed"); error.status = response.status; throw error; }
      return response.json();
    };
    const button = (text, action) => {
      const node = el("button", text, "btn"); node.type = "button"; node.addEventListener("click", action); return node;
    };
    const label = (text, control) => {
      const node = el("label"); control.setAttribute("aria-label", text);
      node.append(el("span", text), control); return node;
    };
    const select = (pairs, value) => {
      const node = el("select");
      pairs.forEach(([key, text]) => { const option = el("option", text); option.value = key; node.append(option); });
      node.value = value; return node;
    };
    const controls = el("div", undefined, "kpi-controls");
    const date = el("input"); date.type = "date";
    date.value = new Intl.DateTimeFormat("en-CA", {timeZone: "Europe/Lisbon", year: "numeric", month: "2-digit", day: "2-digit"}).format(new Date());
    const period = select([["day", "Dia"], ["week", "Semana"], ["month", "Mês"]], "week");
    const stats = el("div"); stats.setAttribute("aria-live", "polite");
    const support = el("div", undefined, "kpi-info-actions");
    const sources = el("div", undefined, "kpi-sources");
    const dialog = el("dialog", undefined, "kpi-dialog");
    dialog.setAttribute("aria-label", "Classificação comercial");
    const close = button("Fechar", () => dialog.close());
    const content = el("div"); dialog.append(close, content);
    let detailSequence = 0, sourceSequence = 0, trigger = null;
    dialog.addEventListener("close", () => { ++detailSequence; content.replaceChildren(); trigger?.focus(); });
    const number = value => Number.isSafeInteger(value) && value >= 0 ? String(value) : "—";
    const timestamp = (name, value, key, absent = "Não disponível") => {
      const node = el("p", `${name}: `, "subtle");
      if (key) node.setAttribute(`data-kpi-${key}`, "");
      if (value && Number.isFinite(new Date(value).getTime())) {
        const text = new Intl.DateTimeFormat("pt-PT", {timeZone: "Europe/Lisbon", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"}).format(new Date(value));
        const time = el("time", `${text} · Lisboa`); time.dateTime = value; node.append(time);
      } else node.append(el("span", absent));
      return node;
    };
    const metric = (name, value, key) => {
      const card = el("div", undefined, "kpi-card"); const count = el("strong", number(value));
      if (key) count.setAttribute(`data-kpi-${key}`, "");
      card.append(el("span", name), count); return card;
    };
    const render = frame => {
      stats.replaceChildren();
      if (frame.state !== "ready") {
        stats.append(el("p", frame.state === "loading" ? "A atualizar · classificação pendente…" : "Não foi possível atualizar. Valores não confirmados; tente novamente."));
        return;
      }
      const data = frame.data;
      const grid = el("div", undefined, "kpi-grid");
      grid.append(metric("Conversas relevantes", data.relevance.confirmed, "confirmed"),
        metric("Tentativas", data.phone_attempts.total, "attempts"),
        metric("Novas", data.phone_attempts.new), metric("Follow-up", data.phone_attempts.follow_up));
      const goal = el("p"); goal.setAttribute("data-kpi-goal", "");
      const displayDate = value => new Intl.DateTimeFormat("pt-PT", {timeZone: "Europe/Lisbon"}).format(new Date(value));
      const newGoal = goalView(data);
      goal.className = "kpi-goal-hero";
      goal.textContent = `${number(newGoal.confirmed)}/50 ${newGoal.label} esta semana`;
      const progress = el("progress"); progress.max = 50;
      if (newGoal.confirmed !== null) progress.value = Math.min(50, Math.max(0, newGoal.confirmed));
      progress.setAttribute("aria-label", goal.textContent);
      const states = el("div", undefined, "kpi-states");
      [["pending", "Pendentes"], ["stale", "Desatualizadas"], ["conflict", "Em conflito"]].forEach(([key, text]) => {
        const node = el("span", `${text}: `); const count = el("strong", number(data.relevance[key]));
        count.setAttribute(`data-kpi-${key}`, ""); node.append(count); states.append(node);
      });
      const info = el("details", undefined, "ui-info");
      const summary = el("summary", "ⓘ");
      summary.setAttribute("aria-label", "Sobre estes indicadores");
      summary.title = "Sobre estes indicadores";
      const extra = el("div", undefined, "ui-info-content");
      const secondary = el("div", undefined, "kpi-grid");
      secondary.append(metric("Não relevantes", data.relevance.no, "no"), metric("Desconhecidas", data.relevance.unknown, "unknown"));
      extra.append(el("p", `Semana ${displayDate(data.weekly_goal.week_start)}–${displayDate(new Date(data.weekly_goal.week_end_exclusive).getTime() - 1)}`), secondary, states,
        el("p", `Relevantes na semana (inclui follow-up): ${number(data.weekly_goal.confirmed)} · Relevantes sem prova de primeira conversa: ${number(data.weekly_new_significant_goal?.unknown)} · Declarações em conflito: ${number(data.weekly_new_significant_goal?.conflict)}. Meta nova: só relevância atual + primeira conversa humana explícita; cobertura parcial.`, "subtle"),
        timestamp("Última atualização dos dados", data.generated_at, "updated"),
        timestamp("Avaliação atual mais antiga", data.assessed_at, "assessed", "Sem avaliação atual"),
        el("p", `Tipo de tentativa desconhecido: ${number(data.phone_attempts.unknown)} · Excluídas: ${number(data.exclusions.total)} · Falhas de processamento: ${number(data.coverage.processing.failed)}`, "subtle"),
        el("p", `Histórico incompleto: ${number(data.coverage.history.incomplete)} · Ordem ambígua: ${number(data.coverage.history.ambiguous_order)}. ${data.coverage.source.complete ? "Varredura concluída." : "Cobertura de processamento ainda não confirmada."}`, "subtle"));
      extra.append(support);
      info.append(summary, extra);
      stats.append(goal, progress, grid);
      if (!data.coverage.source.complete || [data.relevance.pending, data.relevance.stale, data.relevance.conflict, data.relevance.unknown, data.weekly_new_significant_goal?.unknown, data.weekly_new_significant_goal?.conflict, data.coverage.processing.failed].some(value => value > 0)) {
        stats.append(el("span", "Parcial", "kpi-partial"));
      }
      stats.append(info);
    };
    const controller = createController({request, render});
    const reload = () => { ++sourceSequence; sources.replaceChildren(); return controller.load(date.value, period.value); };
    controls.append(label("Data", date), label("Período", period), button("Atualizar KPIs", reload));
    date.addEventListener("change", reload); period.addEventListener("change", reload);
    const base = "/api/v1/pipeline/call-metrics/activities";
    const details = async (id, openedBy) => {
      const sequence = ++detailSequence;
      if (!dialog.open) { trigger = openedBy; dialog.showModal(); }
      content.replaceChildren(el("p", "A carregar fonte protegida…"));
      try {
        const value = await request(`${base}/${id}`);
        if (sequence !== detailSequence || !dialog.open) return;
        const source = value.source, assessment = value.assessment;
        const company = el("h3", source.company_display_name || "Empresa não identificada");
        company.setAttribute("data-kpi-company", "");
        const reason = el("p", assessment?.reason || "Ainda sem avaliação.");
        reason.setAttribute("data-kpi-reason", "");
        content.replaceChildren(el("h2", "Classificação comercial"), company,
          timestamp("Interação", source.occurred_at, "occurred"));
        const provenance = el("p", assessment ? `${assessment.provenance === "human" ? "Humano" : "Inferido"} · ${assessment.freshness}` : "Pendente");
        provenance.setAttribute("data-kpi-provenance", "");
        content.append(provenance, timestamp("Última avaliação", assessment?.assessed_at, "assessed", "Sem avaliação"),
          el("pre", source.summary || "Sem nota"), reason,
          el("p", `Histórico: ${source.history_coverage.state} · Revisão humana: ${value.override_revision}`, "subtle"));
        if (leadRoot.dataset.canAddNote !== "true") return;
        const form = el("form", undefined, "kpi-form");
        const relevant = select([["unknown", "Desconhecida"], ["yes", "Sim"], ["no", "Não"]], assessment?.relevant || "unknown");
        const kind = select([["unknown", "Desconhecida"], ["new", "Nova"], ["follow_up", "Follow-up"]], assessment?.phone_attempt_kind || "unknown");
        const eligibility = select([["eligible", "Elegível"], ["excluded", "Excluída"]], source.eligibility);
        const exclusion = select([["", "Sem exclusão"], ["non_phone", "Não telefónica"], ["test_scaffold", "Teste"], ["voided", "Anulada"], ["cancelled_before_occurrence", "Cancelada antes"], ["duplicate", "Duplicado"], ["superseded", "Substituída"]], source.exclusion_reason || "");
        const reasonInput = el("textarea"); reasonInput.required = true; reasonInput.maxLength = 240; reasonInput.rows = 3;
        reasonInput.value = assessment?.reason || "";
        const declares = el("input"); declares.type = "checkbox";
        const start = el("input"); start.type = "number"; start.min = "0"; start.value = "0";
        const end = el("input"); end.type = "number"; end.min = "1"; end.value = String(Array.from(source.summary || "").length);
        form.append(label("Relevância", relevant), label("Tipo de tentativa", kind), label("Elegibilidade", eligibility), label("Motivo de exclusão", exclusion),
          label("Justificação", reasonInput), label("Início da evidência (carateres)", start), label("Fim da evidência (exclusivo)", end),
          label("Declaro explicitamente que não houve tentativa telefónica anterior", declares));
        const message = el("p"); message.setAttribute("role", "status");
        const save = el("button", "Guardar correção", "btn btn-primary"); save.type = "submit";
        const remove = button("Remover correção humana", () => mutate("remove"));
        remove.disabled = !value.override_revision;
        form.append(save, remove, message); content.append(form);
        let pendingCommand = null;
        const mutate = async operation => {
          let correction = null;
          if (operation === "set") {
            if (!form.reportValidity()) return;
            let history = source.history_coverage;
            if (kind.value === "new" && history.state !== "complete_no_prior_attempt") {
              if (!declares.checked) { message.textContent = "Nova exige prova histórica ou declaração humana explícita."; return; }
              history = {state: "complete_no_prior_attempt", oldest_known_at: null, proof_activity_id: null};
            }
            const refs = source.summary ? [{activity_id: id, source_version: source.source_version, field: "summary", start: Number(start.value), end: Number(end.value)}] : [];
            correction = {relevant: relevant.value, phone_attempt_kind: kind.value, eligibility: eligibility.value,
              exclusion_reason: eligibility.value === "excluded" ? exclusion.value : null, reason: reasonInput.value, evidence_refs: refs, history_coverage: history};
          }
          const values = {expected_source_digest: source.source_digest, expected_context_digest: source.context_digest,
            expected_override_revision: value.override_revision, operation, correction};
          const signature = JSON.stringify(values);
          if (!pendingCommand || pendingCommand.signature !== signature) pendingCommand = {signature, id: crypto.randomUUID()};
          const body = {command_id: pendingCommand.id, ...values};
          save.disabled = remove.disabled = true;
          try {
            const receipt = await request(`${base}/${id}/correction`, {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": leadRoot.dataset.csrfToken || "", "Idempotency-Key": body.command_id}, body: JSON.stringify(body)});
            // Clear aggregate before any subsequent read; never display optimistic yes.
            const refreshing = controller.invalidate();
            const check = await request(`${base}/${id}`);
            if (check.override_revision !== receipt.override_revision) throw new Error("Readback changed");
            pendingCommand = null;
            await details(id, openedBy); await refreshing;
          } catch (error) {
            message.textContent = error.status === 409 ? "Fonte ou revisão alterada. Reabra a fonte antes de corrigir." : "Correção não confirmada. A sua justificação foi preservada; tente novamente.";
            controller.invalidate(); save.disabled = false; remove.disabled = !value.override_revision;
          }
        };
        form.addEventListener("submit", event => { event.preventDefault(); mutate("set"); });
      } catch (_) { if (sequence === detailSequence) content.replaceChildren(el("p", "Não foi possível abrir a fonte protegida. Autentique-se e atualize.")); }
    };
    const loadSources = async (cursor = null, sequence = ++sourceSequence) => {
      if (!cursor) sources.replaceChildren(el("p", "A carregar fontes…"));
      const query = new URLSearchParams({date: date.value, period: period.value, status: "all", limit: "50"});
      if (cursor) query.set("cursor", cursor);
      try {
        const page = await request(`${base}?${query}`);
        if (sequence !== sourceSequence) return;
        if (!cursor) sources.replaceChildren();
        page.items.forEach(item => {
          const row = el("div", undefined, "kpi-source");
          const open = button("Abrir fonte", () => details(item.source.activity_id, open));
          const summary = el("div");
          summary.append(el("strong", item.source.company_display_name || "Empresa não identificada"),
            timestamp("Interação", item.source.occurred_at),
            el("p", `${item.assessment?.freshness || "pending"} · ${item.assessment?.relevant || "unknown"} · ${item.assessment?.reason || "Ainda sem avaliação."}`));
          row.append(summary, open);
          sources.append(row);
        });
        if (page.has_more && page.next_cursor) {
          const more = button("Mais fontes", () => { more.remove(); loadSources(page.next_cursor, sequence); }); sources.append(more);
        } else if (!page.items.length && !cursor) sources.append(el("p", "Sem fontes neste período."));
      } catch (_) { if (sequence === sourceSequence) sources.replaceChildren(el("p", "Fontes indisponíveis ou alteradas. Autentique-se ou recarregue.")); }
    };
    if (root.dataset.authenticated === "true") support.append(button("Ver fontes", () => loadSources()));
    else {
      const login = el("a", "Autenticar para ver fontes", "btn");
      login.href = `${base}?date=${date.value}&period=week`;
      support.append(login);
    }
    root.append(el("h2", "Conversas comerciais"), controls, stats, sources, dialog);
    reload();
    return {invalidate: () => { ++sourceSequence; sources.replaceChildren(); if (dialog.open) dialog.close(); return controller.invalidate(); }};
  };
  const api = {createController, mount, goalView};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.CommercialKpis = api;
})(globalThis);
