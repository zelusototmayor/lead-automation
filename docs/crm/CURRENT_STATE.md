# Estado atual do CRM

Este documento fixa o baseline seguro anterior às mudanças de evolução do CRM. Ele descreve o estado da branch de implementação, não o ambiente de produção.

## Baseline Git registrado

- Branch: `feat/crm-accounts-proposals-v1`
- Commit-base/`HEAD` verificado antes desta documentação: `106485b606a910b3062f47f249e9444bc7a686fa`
- Comparação: `origin/main...HEAD`
- Delta verificado: 5 arquivos, 488 inserções e 3 remoções.

```text
dashboard/app/main.py                  |  35 +++++-
dashboard/app/templates/logistics.html |  73 ++++++++++++-
dashboard/config/deploy.yml            |   1 -
src/crm/pt_logistics_sheet.py          | 189 ++++++++++++++++++++++++++++++++
tests/test_crm_evolution.py            | 193 +++++++++++++++++++++++++++++++++
5 files changed, 488 insertions(+), 3 deletions(-)
```

## Python e suíte baseline

Esta worktree de implementação usa o ambiente local não rastreado `.venv311`, com Python 3.11.15. Ela não contém `.venv`. O `.venv311` não é reconstruível somente a partir destes documentos, e `dashboard/requirements.txt` não inclui `pytest`; portanto, o comando e o resultado abaixo são evidência de execução local observada, não uma garantia de reprodução em um checkout limpo. Antes da criação desta worktree, o ambiente `/Users/max/clawd/automations-repo/lead-automation-crm-evolution/.venv` da worktree usada como fonte da auditoria foi verificado com Python 3.9.6; esse caminho registra a proveniência do baseline e não deve ser interpretado como um `.venv` local desta worktree.

Comando local observado:

```bash
.venv311/bin/python -m pytest tests/test_crm_evolution.py -q
```

Resultado local observado:

```text
........                                                                 [100%]
8 passed, 1 warning in 0.20s
```

O único aviso é uma depreciação de `starlette.templating.TemplateResponse`: a assinatura antiga passa `name` antes de `Request`. O baseline previamente registrado pelo controlador também foi `8 passed`, com o mesmo único aviso, em 1.38s; a diferença é apenas o tempo da execução.

## Arquitetura e contratos atuais

- O dashboard expõe APIs públicas como `/api/stats`, `/api/account-profiles`, `/api/portfolio` e `/api/recommendations`.
- A rota `/api/outreach-followups` referencia `username` sem que o nome esteja definido, regressão conhecida no estado atual.
- O modelo persistido em Sheet é plano (`flat`), sem entidades relacionais explícitas.
- Não existe uma camada relacional para separar e vincular leads, contas, contatos, oportunidades, propostas e atividades.
- Há comportamento de zero sintético para dados ausentes e de probabilidade padrão (`default`) quando a probabilidade não está disponível; esses valores não devem ser confundidos com observações reais.
- Os estados `Won` e `Meeting Booked` estão conflados em contratos/comportamentos atuais, embora representem momentos distintos do funil.
- A área de `Intelligence` está incorporada em `Proposals`, em vez de possuir separação própria.

## Limites deste inventário

Este baseline é somente leitura e documentação. Nenhuma consulta ao Google Sheets, acesso a credenciais, alteração de serviço de produção ou deploy foi realizado. O catálogo inicial de estágios está em [`STAGE_MAPPING.md`](./STAGE_MAPPING.md).

---

## Estado da implementação até à Tarefa 7

Esta secção é cumulativa e atualiza o baseline histórico acima. O trabalho decorre exclusivamente na branch `feat/crm-accounts-proposals-v1`; o `HEAD` anterior ao commit da Tarefa 7 era `fad0d066b449e0a0b1903850484ed8ae7d67f5f5`.

### Concluído e testado

- Tarefas 0–2: baseline, política de exposição e estabilização do endpoint legado.
- Tarefa 3: configuração PostgreSQL/Alembic lazy, sem conexão ou migração durante imports.
- Tarefa 4: catálogo e política canónica de fases.
- Tarefa 5: identidades de origem, ledger idempotente e checkpoints.
- Tarefa 6: contas, contactos, leads e timeline imutável de atividades.
- Tarefa 7: snapshot read-only de Sheets, backfill de contas em shadow mode, checkpoint transacional e comparação agregada com a origem legada.

O backfill da Tarefa 7:

- é dry-run por omissão e só escreve em PostgreSQL com `--apply`, URL explícita e workspace UUID;
- nunca contém métodos de escrita em Sheets;
- usa identidade estável independente do número da linha;
- revalida snapshots em cada fronteira de processamento, tornando-os estruturalmente imutáveis e aplicando um limite agregado de tamanho;
- reporta IDs duplicados, IDs ausentes, fases não mapeadas e conflitos sem imprimir os valores externos;
- trata fases terminais sem histórico suficiente como revisão;
- faz matching exato, nunca merge fuzzy por nome;
- grava eventos e o checkpoint do snapshot na mesma transação;
- faz claim atómico de eventos e identidades, rollback integral em falhas inesperadas e retry idempotente, incluindo replay concorrente;
- compara, apenas por contagens agregadas, existência, remoções na origem, fase, associação e lifecycle da conta e campos de origem.

### Evidência de execução local

Em PostgreSQL 16 descartável, sem credenciais ou dados reais:

```text
tests/migration: 40 passed
persistence + unit + security + CRM evolution: 583 passed, 4 warnings preexistentes
Alembic lifecycle: base -> 0001 -> 0002; downgrade até base e re-upgrade até 0002
Alembic check: No new upgrade operations detected
Ruff: passed
```

A coleção global do repositório continua bloqueada por dois problemas preexistentes fora do escopo CRM:

- `test_pipeline.py` não consegue importar a dependência opcional `anthropic` neste ambiente;
- `tests/test_linkedin_system.py` executa 47 checks com sucesso e depois chama `sys.exit(0)` durante collection, fazendo o pytest terminar com código 3.

Os targets CRM e de regressão relevantes acima passam integralmente.

O comando seguro documentado também foi exercitado com a fixture local:

```bash
.venv311/bin/python scripts/crm_backfill_accounts.py \
  --fixture tests/fixtures/pt_logistics_rows.json --dry-run
```

Nenhuma consulta ou alteração foi feita no Google Sheets, Gmail, Calendar, Granola, CRM live ou PostgreSQL de produção. Não houve push, merge ou deploy.

### Segurança e próximos limites

As novas superfícies ricas de Contas, Propostas e Inteligência não podem ser públicas. A Tarefa 8 deve manter as rotas novas desligadas e fail-closed por omissão, usando uma fronteira de principal autenticado injetável para testes. A escolha do fornecedor de identidade/sessão de produção continua pendente e qualquer ativação live exige decisão e aprovação explícitas.

O dashboard ainda lê a projeção legada para utilizadores; PostgreSQL permanece em shadow mode.

---

## Estado da implementação até à Tarefa 8

### Área de Contas concluída localmente

A Tarefa 8 acrescenta uma área de Contas independente de Leads, Propostas e Inteligência:

- `GET /api/v1/accounts` e `GET /api/v1/accounts/{account_id}`;
- `GET /contas` e `GET /contas/{account_id}`;
- lista paginada e perfil com contagens de contactos, atividades de email, reuniões e evidência de proposta;
- probabilidade permanece `null` enquanto não existir evidência/modelo canónico;
- próxima ação permanece `null` até existir um modelo canónico de tarefa pendente; atividades históricas não são apresentadas como ações atuais;
- referências de evidência são allowlisted, sem payloads, notas ou emails brutos, e limitadas às 50 mais recentes;
- queries têm scope obrigatório de workspace e contagem constante, sem N+1;
- IDOR cross-workspace devolve `404`.

As quatro rotas são fail-closed por omissão. O workspace deriva exclusivamente de um `CRMPrincipal` confiável injetado no servidor; URL, query, headers e cookies criados por esta tarefa não selecionam workspace. O resolver de produção permanece deny-only até existir uma decisão explícita sobre o adapter de identidade/sessão. Nenhum token é enviado para o browser.

### Evidência de execução da Tarefa 8

Em PostgreSQL 16 descartável, sem dados ou credenciais reais:

```text
tests/integration/api: 17 passed
unit + integration + security + migration + CRM evolution: 639 passed, 1 skipped
Alembic check: No new upgrade operations detected
Ruff, compileall e git diff --check: passed
```

Os quatro warnings são as depreciações preexistentes de `TemplateResponse` nas rotas legadas. As páginas novas usam a assinatura atual e foram exercitadas por `TestClient` com fixtures realistas, incluindo loading, empty, error, paginação inválida e isolamento cross-workspace.

Não houve deploy, alteração de Sheet, acesso a sistemas live ou ativação da autenticação de produção. As Tarefas 9–19 continuam pendentes.

---

## Estado da implementação até à Tarefa 9

### Portefólio relacional de Propostas concluído localmente

A Tarefa 9 separa Propostas de Leads e preserva histórico e valores desconhecidos:

- `proposals`, `proposal_versions`, `proposal_items` e `proposal_followups` são agregados relacionais distintos;
- Account é obrigatória, Lead é opcional e ambas as ligações são tenant-safe;
- versões são numeradas de forma monotónica sob row lock e nunca são sobrescritas;
- a versão selecionada tem de pertencer à mesma Proposal;
- versões substituídas permanecem no histórico como `superseded` e não duplicam o pipeline;
- follow-ups só podem referenciar Activities da mesma workspace e Account;
- valores monetários usam `Decimal`/`numeric(18,2)`, sem arredondamento silencioso ou overflow;
- valor desconhecido permanece `NULL`; zero confirmado permanece `0.00`;
- valores candidatos não entram nos totais até terem confirmação, identificador de evidência, autor e timestamp;
- o estado `confirmed` é protegido no serviço e por constraint triggers PostgreSQL contra versões sem evidência/valor elegível;
- o portefólio expõe contagens separadas de `missing`, `candidate` e `confirmed`, incluindo propostas sem versão selecionada;
- todas as mutações de valor exigem `expected_version` e conflitos devolvem erro genérico;
- totais mantêm `one_off`, `mrr` e `arr` separados e nunca somam moedas diferentes;
- grupos de opções mutuamente exclusivas só incluem a opção selecionada;
- o serviço não faz commit implícito: commit e rollback pertencem ao chamador/UoW.

A migration `0003` adiciona checks, FKs, índices e triggers PostgreSQL para estados, evidência de envio, contexto tenant, valores não negativos, versões, opções exclusivas e follow-ups. Evidence IDs continuam UUID nullable sem FK até existir o modelo canónico da Tarefa 12.

### Evidência de execução da Tarefa 9

Em PostgreSQL 16 descartável, sem dados ou credenciais reais:

```text
unit + integration + security + migration + CRM evolution: 716 passed, 1 skipped, 4 warnings preexistentes
Task 9 focused com PostgreSQL real: 112 passed
Concorrência: de dois appends com o mesmo token, um persistiu e um teve conflito; o retry persistiu a versão 2
Rollback/commit UoW: exercitados em PostgreSQL
Alembic lifecycle: base -> 0001 -> 0002 -> 0003; downgrade até base e re-upgrade até 0003
Alembic current: 0003 (head)
Alembic check: No new upgrade operations detected
Ruff e format nos ficheiros alterados, compileall e git diff --check: passed
```

Os quatro warnings continuam a ser as depreciações preexistentes de `TemplateResponse` nas rotas legadas. Não houve deploy, mutação de sistemas live, push, merge ou migração de produção. A migração de propostas legadas permanece para a Tarefa 10.
