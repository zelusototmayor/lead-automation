# Backfill operacional legado de Leads

## Objetivo e segurança

`crm_backfill_legacy_operations.py` materializa apenas o estado operacional que existe num **snapshot imutável da folha principal**: tarefas abertas futuras e notas ainda disponíveis nas células. O comando é `dry-run` por omissão, nunca escreve em Sheets e só aplica com `--apply`, URL PostgreSQL, workspace UUID e owner UUID explícitos.

O backfill não faz matching por nome, empresa ou posição da linha. Cada Lead é resolvido exclusivamente por `(workspace_id, google_sheets, source_scope, lead, external_id)`, usando exatamente o mesmo `source_scope` canónico do backfill de contas. O workspace tem de existir. Leads pré-conta continuam válidos: tarefas e atividades usam `lead_id`, deixam `account_id = NULL` e nunca inventam uma conta. Quando o Lead já tem conta/contacto, as ligações canónicas existentes são preservadas.

## Mapeamento do vocabulário legado

O snapshot preservado em julho de 2026 usa o vocabulário canónico abaixo. O backfill mantém compatibilidade explícita com alguns aliases de exports anteriores, mas esses aliases não substituem o contrato real.

| Cabeçalho preservado | Artefacto canónico | Regra |
| --- | --- | --- |
| `Due` + `Due Time` + `Stage` | `Task(call|email, open)` | `Send Email`/`Email Sent` cria email; outras fases não terminais criam chamada. Hora ausente usa a hora conservadora documentada. Fases terminais vão para revisão, sem trabalho aberto inventado. |
| `Proposal Next Action Due` | `Task(follow_up, open)` | Associa `proposal_id` apenas por identidade de origem exata; sem proposta inequívoca vai para revisão. |
| `Initial Email Sent` e `Outreach FU1/FU2/FU3/Reactivation Sent` | próxima `Task(email, open)` | Deriva no máximo o próximo passo de uma sequência contínua e inequívoca. Lacunas, datas inválidas ou sequência terminal vão para revisão. |
| `notes` | `Activity(note)` | Preserva a nota atual; timestamp histórico não é inferido. |
| `What Happened` + `Last Touch Type` + `Dashboard Touched` | `Activity` tipada conservadoramente | Só tipos conhecidos são materializados. A data é preservada quando válida, mas a hora permanece explicitamente indisponível. Valores livres ou contexto incompleto vão para revisão. |

Aliases antigos suportados, quando aparecem realmente no snapshot: `Next Call Date`/`Next Call Time`, `Next Email Date`, `Next Proposal Follow-Up`, `Follow-Up Due`, `Notes`, `Call Notes`, `Email Notes` e `Proposal Notes`. O backfill não exige nem fabrica os cabeçalhos sintéticos `Outreach`/`Outreach Method` no formato preservado atual.

Datas aceites: `YYYY-MM-DD`, `YYYY/MM/DD`, `DD/MM/YYYY` e `DD-MM-YYYY`. Horas aceites: 24 horas com minutos/segundos e 12 horas com `AM`/`PM`.

As atividades recebem `source_system=google_sheets`, a identidade exata do Lead e um fingerprint semântico SHA-256. Quando a fonte só fornece uma data, o título declara `time unavailable`; quando não fornece sequer data histórica, declara `timestamp unavailable`. Esses valores **não devem ser interpretados como a hora ou data real da interação**.

## Idempotência e conflitos

UUIDs de eventos, tarefas e atividades são UUIDv5 determinísticos, derivados de uma codificação JSON sem colisões por delimitador de workspace, scope, external ID, tipo e slot legado. O ledger de ingestão usa uma chave estável por slot e um fingerprint do significado importado.

- Replay idêntico não cria duplicados.
- Alteração posterior da célula já importada é revisão (`changed_legacy_task`/`changed_legacy_note`), não uma atualização silenciosa.
- Uma tarefa canónica já concluída, cancelada, reatribuída, reagendada ou editada é `conflicting_current_task` e nunca é sobrescrita.
- Atividade divergente ou identidade ausente/inválida também vai para revisão.
- IDs estáveis ausentes/duplicados, datas/horas inválidas, campos acompanhantes ausentes e outreach ambíguo/desconhecido aparecem apenas em contagens agregadas; o relatório não imprime conteúdo comercial.
- Conflitos esperados usam savepoints por artefacto; uma falha inesperada aborta a transação PostgreSQL exterior inteira.

## Limitação explícita de histórico

O relatório devolve sempre `full_history_unavailable: true`. Um snapshot da folha principal contém apenas o valor atual de cada célula. O processo **não fabrica Activity Log, interações anteriores, timestamps, direções, resultados, sequências de proposta ou versões apagadas/sobrescritas**. Recuperar histórico completo exige uma fonte histórica independente e verificável.

## Operação

Dry-run:

```bash
.venv311/bin/python scripts/crm_backfill_legacy_operations.py \
  --snapshot /path/fora-do-repo/pt-logistics.snapshot.json
```

Apply em staging, depois replay idêntico:

```bash
.venv311/bin/python scripts/crm_backfill_legacy_operations.py \
  --snapshot /path/fora-do-repo/pt-logistics.snapshot.json \
  --apply \
  --database-url 'postgresql+psycopg://…' \
  --workspace-id '<workspace-uuid>' \
  --owner-user-id '<owner-uuid>'
```

Antes de apply: confirmar backup/restore, migrations até `head`, workspace existente e que o snapshot pertence exatamente à mesma spreadsheet/tab/stable-ID usada no backfill de Leads. Depois: guardar apenas o relatório agregado, rever todos os conflitos, repetir o input idêntico e exigir `tasks_created=0`, `activities_created=0` e apenas `replay_noop` para os artefactos já importados.
