(() => {
  'use strict';
  document.addEventListener('DOMContentLoaded', () => {
    const app = document.getElementById('leads-app');
    if (!app) return;
    const panel = app.querySelector('[data-activity-analysis]');
    const period = panel.querySelector('[data-analysis-period]');
    const range = panel.querySelector('[data-analysis-range]');
    const totals = panel.querySelector('[data-analysis-totals]');
    const chart = panel.querySelector('[data-analysis-chart]');
    const keys = ['email_initial', 'email_follow_up', 'call_initial', 'call_follow_up'];
    const colors = ['#41674f', '#8b6b27', '#436ca1', '#946281'];
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'btn'; button.dataset.openAnalysis = '';
    button.textContent = 'Análise'; button.setAttribute('aria-expanded', 'false');
    app.querySelector('.header-actions').append(button);
    const node = (tag, text, cls) => {
      const el = document.createElement(tag);
      if (text !== undefined) el.textContent = text;
      if (cls) el.className = cls;
      return el;
    };
    const svgNode = (tag, attrs, text) => {
      const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
      Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, String(value)));
      if (text !== undefined) el.textContent = text;
      return el;
    };
    const render = data => {
      if (data.schema_version !== 1 || keys.some(key => !Array.isArray(data.series?.[key]?.points))) throw new Error('Contrato indisponível');
      totals.replaceChildren(); chart.replaceChildren();
      range.textContent = `${data.days} dias · ${data.start_date} a ${data.end_date}`;
      const width = Math.max(300, Math.min(960, chart.clientWidth));
      const right = width - 20;
      const svg = svgNode('svg', {viewBox:`0 0 ${width} 280`, role:'img', 'aria-label':'Atividade diária registada: quatro séries; lacunas não equivalem a zero.'});
      const max = Math.max(1, ...keys.flatMap(key => data.series[key].points.map(p => p.value ?? 0)));
      for (let tick = 0; tick <= 4; tick++) {
        const value = Math.ceil(max / 4) * tick;
        const y = 230 - (value / (Math.ceil(max / 4) * 4)) * 200;
        svg.append(svgNode('line', {x1:40,x2:right,y1:y,y2:y,stroke:'#e1e7dd'}));
        svg.append(svgNode('text', {x:30,y:y+4,'text-anchor':'end',fill:'#637168','font-size':12}, value));
      }
      const legend = node('div', undefined, 'analysis-legend');
      keys.forEach((key, index) => {
        const series = data.series[key];
        const card = node('article', undefined, 'activity-total'); card.style.setProperty('--series-color', colors[index]);
        const value = node('strong', series.total === null ? '—' : series.total); value.dataset.analysisTotal = key;
        const emailSeries = key.startsWith('email_');
        card.append(node('h3',series.label),value,node('p',series.total === null ? 'Sem dados classificáveis' : (emailSeries ? 'Emails assinalados como enviados · parcial' : 'Registos classificados · parcial')));
        totals.append(card);
        const label = node('span', series.label); label.style.setProperty('--series-color',colors[index]); legend.append(label);
        let segment = [];
        const flush = () => {
          if (segment.length) svg.append(svgNode('polyline', {points:segment.join(' '),fill:'none',stroke:colors[index],'stroke-width':2,'stroke-dasharray':index % 2 ? '5 3' : 'none'}));
          segment = [];
        };
        series.points.forEach((point, i) => {
          if (point.value === null) { flush(); return; }
          const x = 40 + i / Math.max(1,series.points.length-1) * (right - 40);
          const y = 230 - point.value / (Math.ceil(max / 4) * 4) * 200;
          segment.push(`${x},${y}`);
          const dot = svgNode('circle',{cx:x,cy:y,r:3,fill:colors[index]});
          dot.append(svgNode('title',{},`${point.date} · ${series.label}: ${point.value} registados`)); svg.append(dot);
        });
        flush();
      });
      svg.append(svgNode('text',{x:40,y:260,fill:'#637168','font-size':12},data.start_date));
      svg.append(svgNode('text',{x:right,y:260,'text-anchor':'end',fill:'#637168','font-size':12},data.end_date));
      if (keys.every(key => data.series[key].total === null)) {
        svg.append(svgNode('text',{x:width/2,y:120,'text-anchor':'middle',fill:'#637168','font-size':12},'Sem registos classificáveis no período'));
      }
      chart.append(svg,legend);
      panel.querySelector('[data-analysis-source]').textContent = 'Fonte: CRM · emails assinalados como enviados por José/agente; cobertura histórica parcial. Lacunas não significam zero atividade.';
      const c = data.coverage;
      panel.querySelector('[data-analysis-coverage]').textContent = `${data.notes.join(' ')} ${c.email_unknown} emails e ${c.call_unknown} chamadas com tipo desconhecido; quando existe vínculo Gmail, mensagens repetidas são deduplicadas. Excluídos dos totais classificados.`;
    };
    let sequence = 0;
    const load = async () => {
      const current = ++sequence;
      range.textContent = 'A consultar…'; totals.replaceChildren(); chart.replaceChildren();
      try {
        const response = await fetch(`/api/v1/pipeline/activity-analysis?days=${period.value}`, {credentials:'same-origin',cache:'no-store'});
        if (!response.ok) throw new Error('Indisponível');
        const data = await response.json();
        if (current === sequence) render(data);
      } catch (_) {
        if (current === sequence) range.textContent = 'Análise indisponível. Volta a consultar; não significa zero atividade.';
      }
    };
    button.addEventListener('click', () => {
      app.classList.add('analysis-open'); panel.hidden = false; button.setAttribute('aria-expanded','true'); load();
    });
    panel.querySelector('[data-close-analysis]').addEventListener('click', () => {
      sequence++; app.classList.remove('analysis-open'); panel.hidden = true; button.setAttribute('aria-expanded','false'); button.focus();
    });
    period.addEventListener('change', load);
  });
})();
