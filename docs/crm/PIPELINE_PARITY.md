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
| Workspace diária | missing | `/leads` é uma lista de validação, não uma fila diária |
| Chamadas vencidas/hoje/futuras | missing | sem read model por tarefas e timezone comercial |
| Emails vencidos/hoje/futuros | missing | sem filas operacionais equivalentes |
| Follow-ups de proposta vencidos/hoje | partial | histórico relacional existe, sem fila diária completa |
| Leads tocados hoje | missing | sem projeção operacional baseada em atividades |
| Leads ainda não trabalhados | partial | lista existe; sem semântica canónica de atividade qualificante |
| Contadores por fase que abrem filas | missing | sem resumo transacional do pipeline |
| Filtro por prioridade | missing | lista temporária não oferece o fluxo completo |
| Pesquisa empresa/contacto/telefone/email/cidade | partial | pesquisa temporária cobre apenas parte dos campos |
| Lista compacta desktop | partial | lista temporária sem workspace diária/detalhe persistente |
| Lista compacta mobile | partial | renderização existe; contrato operacional mobile não está provado |
| Detalhe sem perder a fila | missing | navegação não preserva fila, filtros e posição |
| Click-to-call e click-to-email | missing | não existem no workspace PostgreSQL operacional |
| Registar resultado de chamada | missing | sem comando HTTP PostgreSQL |
| Notas e histórico | partial | modelos existem; fluxo de captura e timeline operacional incompleto |
| Concluir/reagendar tarefa | missing | sem comandos HTTP e UI |
| Definir próxima ação e data/hora | missing | sem comando operacional por lead |
| Guardar e abrir lead seguinte | missing | sem fluxo atómico/estado de fila |
| Saltar lead sem alteração | missing | sem controlo de fila |
| Editar empresa e contacto | missing | sem comandos estreitos PostgreSQL |
| Alterar fase | partial | `HumanCommandService` existe; sem endpoint/browser seguro |
| Alterar prioridade | missing | sem comando PostgreSQL |
| Registar email manual | missing | sem comando PostgreSQL |
| Concluir follow-up de outreach | missing | sem comando PostgreSQL |
| Atualizar estado/outcome da proposta | missing | leitura existe; sem comando operacional |
| Atualizar próxima ação da proposta | missing | leitura existe; sem comando operacional |
| Motivo de perda | partial | modelo parcial, sem fluxo completo |
| Valor/probabilidade/forecast com evidência | partial | domínio existe; edição operacional protegida não existe |
| Timeline imutável | partial | persistência existe; UI operacional incompleta |
| Analytics de atividade diária | missing | sem equivalente canónico |
| Tempo por fase | partial | histórico existe parcialmente; analytics operacional ausente |
| Writes autenticados e auditados | partial | fundações de comando/audit/outbox existem; cobertura funcional insuficiente |
| Proteção contra edições concorrentes | partial | domínio usa versões; endpoints operacionais ainda ausentes |

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
emails_overdue
emails_today
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
