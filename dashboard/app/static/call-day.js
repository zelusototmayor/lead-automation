(() => {
  "use strict";
  document.addEventListener("DOMContentLoaded", () => {
    const root = document.querySelector('[data-call-day]');
    if (!root) return;
    const app = document.getElementById('leads-app');
    const status = root.querySelector('[data-call-day-status]');

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
      status.textContent = plan.version ? `${plan.total} contactos · callbacks primeiro` : 'Ainda não preparado';
      button.hidden = !!plan.version || app.dataset.canWriteTasks !== 'true';
    };
    const load = () => app.dispatchEvent(new Event('call-plan-refresh'));
    app.addEventListener('call-plan-loaded', event => render(event.detail));
    root.querySelector('[data-refresh-call-day]').addEventListener('click', load);
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

  });
})();
