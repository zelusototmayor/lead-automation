(() => {
  "use strict";
  document.addEventListener("DOMContentLoaded", () => {
    const root = document.querySelector('[data-call-day]');
    if (!root) return;
    const app = document.getElementById('leads-app');
    const status = root.querySelector('[data-call-day-status]');
    const items = root.querySelector('[data-call-day-items]');
    const button = root.querySelector('[data-prepare-call-day]');
    const day = new Intl.DateTimeFormat('en-CA', {timeZone:'Europe/Lisbon',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
    let pendingId = null;
    const request = async (path, options={}) => {
      const response = await fetch(path, {credentials:'same-origin',cache:'no-store',...options});
      if (!response.ok) throw new Error('Preparação indisponível');
      return response.json();
    };
    const render = plan => {
      if (!plan || !Array.isArray(plan.items) || plan.items.length !== plan.total) throw new Error('Plano inválido');
      items.replaceChildren();
      status.textContent = plan.version ? `${plan.total} contactos · ${plan.capacity_minutes} min · meta 10 novas atendidas` : 'Ainda não preparado';
      button.hidden = !!plan.version || app.dataset.canWriteTasks !== 'true';
      for (const entry of plan.items) {
        if (!/^[0-9a-f-]{36}$/i.test(entry.lead_id)) continue;
        const li = document.createElement('li');
        const a = document.createElement('a');
        const params = new URLSearchParams({lead:entry.lead_id,queue:entry.cohort});
        if (entry.task?.id) params.set('row', `task:${entry.task.id}`);
        a.href = `/leads?${params}`;
        a.textContent = entry.company || 'Abrir contacto';
        const label = document.createElement('span');
        label.textContent = entry.cohort === 'calls_actionable' ? ' — callback existente' : ' — prospeção; confirmar histórico';
        li.append(a,label); items.append(li);
      }
    };
    const load = async () => render(await request(`/api/v1/pipeline/call-day?date=${day}`));
    button.addEventListener('click', async () => {
      button.disabled=true;
      try {
        pendingId = pendingId || crypto.randomUUID();
        const body={command_id:pendingId,work_date:day,expected_version:0,capacity_minutes:120};
        try {
          await request('/api/v1/commands/pipeline/prepare-call-day',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':app.dataset.csrfToken,'Idempotency-Key':pendingId},body:JSON.stringify(body)});
        } catch (error) {
          // A concurrent preparer or lost response is reconciled by exact day.
          const current=await request(`/api/v1/pipeline/call-day?date=${day}`);
          if (!current.version) throw error;
        }
        await load();
      } catch (_) {status.textContent='Não foi possível confirmar. Tenta novamente; não são criadas chamadas nem convites.';}
      finally {button.disabled=false;}
    });
    load().catch(() => {status.textContent='Indisponível — não significa ausência de trabalho.';});
  });
})();
