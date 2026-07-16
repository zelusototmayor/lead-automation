function intelligenceWorkspace() {
  return {
    items: [], loading: true, error: '',
    labels: {
      held_meeting_without_notes: 'Reunião realizada sem notas',
      promised_proposal_not_sent: 'Proposta prometida ainda não enviada',
      proposal_missing_next_action: 'Proposta sem próxima ação',
      proposal_stale: 'Proposta sem evolução recente',
      inbound_awaiting_response: 'Mensagem recebida aguarda resposta',
      meeting_without_calendar_event: 'Reunião sem evento de calendário',
      contradictory_value_status_sources: 'Fontes de valor ou estado contraditórias',
      matching_review_candidate: 'Associação requer revisão',
      value_review_candidate: 'Valor requer revisão'
    },
    async load() {
      this.loading = true; this.error = '';
      try {
        const response = await fetch('/api/v1/intelligence/recommendations');
        if (!response.ok) throw new Error('request failed');
        this.items = (await response.json()).items;
      } catch (_) { this.error = 'Não foi possível carregar Inteligência.'; }
      finally { this.loading = false; }
    }
  };
}
