# Contrato de paridade operacional do pipeline

Este documento é o gate funcional para substituir o CRM legado por PostgreSQL. Dados visíveis, APIs saudáveis ou novas páginas read-only não constituem paridade operacional.

## Regra de aceitação

Uma capacidade só pode passar de `missing` ou `partial` para `implemented` quando existe implementação exercitada por testes. Só passa para `owner-accepted` depois de José executar o fluxo em staging. A retirada do legado exige todas as capacidades `implemented` e `owner-accepted`, soak estável e os gates de release do plano canónico.

Estados:

- `missing`: não existe caminho PostgreSQL operacional.
- `partial`: existe fundação ou leitura, mas o fluxo diário não está completo.
- `implemented`: código e testes satisfazem o contrato, ainda sem aceitação do owner.
- `owner-accepted`: José validou o fluxo real em staging.

## Princípios obrigatórios

1. Paridade funcional antes de redesign.
2. Lista e fila antes de Kanban.
3. Operação no telefone é requisito principal.
4. Dados desconhecidos permanecem desconhecidos ou em revisão.
5. O browser usa um único caminho de escrita canónico, nunca writes diretos em Sheets.
6. Estado, atividade, tarefas, auditoria e outbox são atómicos.
7. Todas as alterações usam concorrência otimista com `expected_version`.
8. Todos os comandos são idempotentes por UUID.
9. Workspace, ator e permissões vêm exclusivamente de identidade server-side.
10. O CRM legado permanece disponível até paridade, aceitação e soak.

## Matriz atual

| Capacidade | Estado | Evidência/lacuna atual |
|---|---|---|
| Workspace diária | implemented | `/leads` apresenta filas canónicas, resumo, lista e detalhe lado a lado |
| Chamadas vencidas/hoje/futuras | implemented | filas canónicas vencidas/hoje/futuras usam o fim do dia local do workspace e paginação determinística |
| Emails vencidos/hoje/futuros | implemented | filas canónicas vencidas/hoje/futuras usam o fim do dia local do workspace e paginação determinística |
| Follow-ups de proposta vencidos/hoje | implemented | filas canónicas vencidas/hoje e tarefas no detalhe |
| Leads tocados hoje | implemented | fila `touched_today` baseada em atividades canónicas |
| Leads ainda não trabalhados | implemented | fila `untouched` baseada na ausência de atividade qualificante |
| Contadores por fase que abrem filas | implemented | resumo do pipeline devolve contagens e os botões abrem cada fila |
| Filtro por prioridade | implemented | `/leads` envia o filtro dedicado validado server-side (`low`, `medium`, `high`) antes da contagem e paginação |
| Pesquisa empresa/contacto/telefone/email/cidade | implemented | pesquisa server-side cobre empresa, contacto, telefone, email e cidade canónica, com escaping literal e sem enfraquecer filtros/paginação |
| Lista compacta desktop | implemented | fila compacta e detalhe persistente na mesma página |
| Lista compacta mobile | implemented | layout responsivo mantém fila, detalhe e comandos numa coluna |
| Detalhe sem perder a fila | implemented | detalhe abre no painel lateral sem abandonar a fila ou filtros locais |
| Click-to-call e click-to-email | implemented | detalhe protegido cria links `tel:` e `mailto:` apenas a partir dos dados da API |
| Registar resultado de chamada | implemented | comando protegido persiste outcome, nota, activity, audit e outbox atomicamente |
| Notas e histórico | implemented | notas de chamada/email e comando autónomo `add-note` entram numa timeline append-only; idempotência, versão otimista, audit e outbox não expõem o conteúdo privado |
| Concluir/reagendar tarefa | implemented | comandos e UI complete/reschedule/cancel têm versão, idempotência, activity, audit e outbox atómicos |
| Definir próxima ação e data/hora | implemented | comando e UI criam tarefa aberta tenant-safe e auditada |
| Guardar e abrir lead seguinte | implemented | `tests/frontend/test_leads_queue.js` prova captura de lead/versão/sucessor antes do POST, avanço só após sucesso, precedência de navegação/filtros mais recentes e separação entre sucesso da mutação e falhas de refresh |
| Saltar lead sem alteração | implemented | `tests/frontend/test_leads_queue.js` prova avanço local sem POST/refresh e no-op explícito no último lead, sem wrap |
| Editar empresa e contacto | partial | comando protegido existe para leads com account/contact; leads pré-conta ainda não são editáveis |
| Alterar fase | implemented | endpoint e UI protegidos usam política canónica, expected version e correção revista |
| Alterar prioridade | partial | incluída no comando de edição, mas ainda acoplada a account/contact existentes |
| Registar email manual | implemented | comando protegido regista atividade manual sem enviar email |
| Concluir follow-up de outreach | partial | tarefas genéricas podem ser concluídas; sem contrato específico de outreach/proposta |
| Atualizar estado/outcome da proposta | implemented | comando e formulário protegidos atualizam estado, probabilidade e forecast com expected_version, idempotência, activity, audit e outbox atómicos; `won` permanece fail-closed até existir prova oficial |
| Atualizar próxima ação da proposta | implemented | o mesmo comando protegido atualiza ação e prazo de forma atómica e rejeita prazo sem ação |
| Motivo de perda | implemented | o fluxo operacional exige motivo não vazio para `lost`, persiste-o e limpa estados terminais incompatíveis |
| Valor/probabilidade/forecast com evidência | partial | probabilidade e forecast têm edição manual auditada; confirmação de valor e alterações consequentes com evidência comercial continuam dependentes da política aprovada |
| Timeline imutável | implemented | detalhe lista atividades append-only e os comandos operacionais acrescentam nova evidência |
| Analytics de atividade diária | missing | sem equivalente canónico |
| Tempo por fase | partial | histórico existe parcialmente; analytics operacional ausente |
| Writes autenticados e auditados | partial | comandos de lead, tarefa e proposta estão protegidos e auditados; outreach específico ainda está incompleto |
| Proteção contra edições concorrentes | partial | comandos de lead, tarefa e proposta usam expected version; superfícies restantes ainda estão ausentes |

## Contratos mínimos

Leituras:

```text
GET /api/v1/pipeline/summary
GET /api/v1/pipeline/items
GET /api/v1/leads/{lead_id}
GET /api/v1/leads/{lead_id}/timeline
GET /api/v1/leads/{lead_id}/tasks
```

Filas obrigatórias:

```text
calls_overdue
calls_today
calls_future
emails_overdue
emails_today
emails_future
proposal_followups_overdue
proposal_followups_today
touched_today
untouched
all
```

Comandos estreitos obrigatórios:

```text
POST /api/v1/commands/leads
POST /api/v1/commands/leads/{lead_id}/edit
POST /api/v1/commands/leads/{lead_id}/transition-stage
POST /api/v1/commands/leads/{lead_id}/log-call
POST /api/v1/commands/leads/{lead_id}/log-email
POST /api/v1/commands/leads/{lead_id}/schedule-next-action
POST /api/v1/commands/tasks/{task_id}/complete
POST /api/v1/commands/tasks/{task_id}/reschedule
POST /api/v1/commands/tasks/{task_id}/cancel
POST /api/v1/commands/proposals/{proposal_id}/update-pipeline
```

Todos os comandos exigem identidade server-side, permissão exata, `Idempotency-Key`, CSRF/Origin aprovado e `expected_version`. Erros não podem revelar payload comercial, existência cross-workspace, tokens ou credenciais.

## Gate de staging

José deve conseguir, sem abrir a Sheet:

1. encontrar chamadas de hoje;
2. abrir um lead e manter a fila;
3. mudar prioridade;
4. registar chamada, resultado e nota;
5. avançar fase;
6. marcar próxima ação;
7. guardar e abrir o lead seguinte;
8. saltar sem alteração;
9. registar email manual;
10. concluir follow-up;
11. atualizar proposta e próxima ação;
12. encontrar oportunidades vencidas/paradas;
13. confirmar persistência depois de refresh/login;
14. repetir o fluxo principal no telefone.

## Bloqueio de retirada do legado

A Tarefa 19 permanece bloqueada até:

- todas as linhas estarem `implemented` e `owner-accepted`;
- backfill operacional sem omissões silenciosas;
- staging com writes, backup/restore e rollback verificados;
- soak com sessões reais, sem perda ou duplicação;
- cutover faseado concluído;
- dois releases estáveis pós-cutover;
- telemetria provar ausência de consumidores v0;
- export e aceitação final dos stakeholders.
