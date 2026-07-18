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

---

## Estado da implementação até à Tarefa 10

### Backfill seguro de propostas legadas concluído localmente

A Tarefa 10 acrescenta um importador dry-run por omissão que consome snapshots locais imutáveis e associa propostas às contas/leads já importados pela identidade estável da Sheet. Valor vazio permanece `missing`; valor estritamente parseável fica `candidate`, nunca confirmado. Envios ficam `legacy_unverified`, sem evidência inventada, e `Won` permanece `won` em vez de ser convertido em `Meeting Booked`.

O apply exige URL PostgreSQL e workspace UUID explícitos, rejeita campos inválidos para revisão, reporta contas sem correspondência e usa ledger/identidade de origem para replay idempotente. O CLI redige falhas e não consulta Sheets.

Evidência local em PostgreSQL 16 descartável, sem dados ou credenciais reais:

```text
Task 10 focused: 4 passed
Migration aplicada até 0003 antes do teste de persistência
Replay idêntico: 0 novos registos, 2 no-op
```

Nenhum sistema live foi consultado ou alterado. As Tarefas 11–19 continuam pendentes.

---

## Estado da implementação até à Tarefa 11

### Área independente de Propostas concluída localmente

A Tarefa 11 acrescenta páginas e APIs protegidas para consultar o portefólio relacional:

- `GET /api/v1/proposals`, `GET /api/v1/proposals/{proposal_id}` e `GET /api/v1/proposals/portfolio`;
- `GET /propostas` e `GET /propostas/{proposal_id}`;
- paginação e filtros validados por estado, conta, owner, moeda, idade de envio verificado, próxima ação, forecast e vertical comercial;
- totais de `one_off`, `mrr` e `arr` separados por moeda, com pipeline aberto, ponderado, ganho e perdido;
- valores `missing`, `candidate`, `confirmed` e `rejected` contados separadamente; apenas valores confirmados entram nos totais;
- ponderação apenas quando a probabilidade e a respetiva origem aprovada estão presentes;
- idade apenas para envios `verified`, mantendo `legacy_unverified` explícito em vez de o apresentar como envio comprovado;
- detalhe com versões, itens e referências allowlisted de evidência/follow-up, sem payloads, notas, contactos, emails ou identidade do confirmador;
- queries de lista e detalhe com contagem constante, sem N+1, e IDOR cross-workspace tratado como `404`.

O template legado deixou de incorporar os cards de portefólio/recomendações e encaminha a área antiga para `/propostas`. Propostas não contém recomendações; Inteligência continua reservada para a Tarefa 13.

Todas as páginas e APIs novas usam exclusivamente o `CRMPrincipal` confiável do servidor para derivar a workspace e falham fechadas por omissão. Query string, headers e cookies não selecionam tenant; nenhum token é enviado ao browser e o resolver de produção continua deny-only.

### Evidência de execução da Tarefa 11

Em PostgreSQL 16 descartável local, migrado até `0003`, sem dados ou credenciais reais:

```text
Task 11 focused: 26 passed
CRM/API/security/unit/migration/persistence regression: 746 passed, 1 skipped
Ruff e format nos ficheiros alterados, compileall e git diff --check: passed
```

O skip é condicional e preexistente. Os quatro warnings continuam a ser as depreciações preexistentes de `TemplateResponse` nas rotas legadas. Uma verificação Ruff global também continua a encontrar dívida preexistente fora dos ficheiros alterados; nenhum desses ficheiros fora de escopo foi reformatado ou corrigido.

Não houve deploy, push, envio de email, acesso ou mutação de sistemas live, nem criação de sentinel. As Tarefas 12–19 continuam pendentes.

---

## Estado da implementação até à Tarefa 12

### Evidência e discovery/review de propostas concluídos localmente

A Tarefa 12 acrescenta evidência canónica append-only e discovery determinístico de propostas:

- `evidence` guarda proveniência tenant-safe, hash de conteúdo, metadados minimizados, sensibilidade e retenção, sem payload bruto;
- `review_candidates` representa ações abertas para proposta prometida ainda não enviada e valores ambíguos, com dedupe por workspace;
- os UUIDs de evidência não canónica anteriores a `0004` deixam de sustentar falsos estados `verified`/`confirmed`; observações monetárias permanecem candidatas e envios anteriores permanecem `legacy_unverified`;
- email outbound classificado explicitamente como promessa cria ação de review, não proposta enviada;
- anexo enviado cria proposta/versão candidata com evidência de mensagem e documento;
- valor ambíguo permanece `NULL` e abre review;
- novo anexo no mesmo thread cria uma nova versão, mantendo a anterior;
- follow-up não cria proposta nem versão;
- replay do mesmo artefacto não duplica evidência, proposta, versão ou review aberto;
- decisões usam apenas factos/classificações determinísticas fornecidos pelo connector; nenhum LLM decide envio, valor ou associação;
- serviços não fazem commit: transações continuam pertencendo ao chamador.

A migration `0004` acrescenta FKs de proveniência, isolamento por workspace/account, unicidade parcial por thread e review aberto, trigger append-only de evidência e validação tenant-safe da evidência documental.

### Evidência de execução da Tarefa 12

Em PostgreSQL 16 descartável local, sem dados ou credenciais reais:

```text
RED observado: collection falhou por ausência do modelo Evidence
Task 12 unit focused: 4 passed
Task 12 persistence focused: 3 passed
Focused proposal/evidence regression: 43 passed
CRM/API/security/unit/migration/persistence regression: 753 passed, 1 skipped
Alembic lifecycle: base -> 0001 -> 0002 -> 0003 -> 0004; downgrade até base;
re-upgrade até 0004; downgrade 0004 -> 0003; re-upgrade até 0004
Alembic check: No new upgrade operations detected
Ruff, format, compileall e git diff --check: passed
```

O skip e os quatro warnings de `TemplateResponse` são preexistentes. Não houve deploy, push, envio de mensagens, acesso ou mutação de sistemas live, nem criação de sentinel. As Tarefas 13–19 continuam pendentes.

---

## Estado da implementação até à Tarefa 13

### Workspace separado de Inteligência concluído localmente

A Tarefa 13 acrescenta recomendações determinísticas, persistidas e separadas de Propostas:

- `GET /api/v1/intelligence/recommendations`, detalhe por ID e `GET /inteligencia` são protegidos pelo principal confiável do servidor e isolados por workspace;
- cada recomendação contém `rule_code`, prioridade, referências allowlisted de evidência, estado e chave determinística, com uma única recomendação aberta por workspace/chave;
- a migration `0005` impõe códigos, prioridades, estados, evidência não vazia, resolução coerente, FKs tenant-safe e unicidade parcial de recomendações abertas;
- o serviço materializa regras sem LLM e sem commit implícito para reunião realizada sem notas, proposta prometida não enviada, proposta sem próxima ação, proposta parada há 14 dias, inbound aguardando resposta, reunião sem evento de Calendar, fontes de valor/estado contraditórias e candidatos de revisão de matching/valor quando representáveis pelos factos canónicos existentes;
- recomendações deixam de ser carregadas ou apresentadas dentro da área legada de Propostas; a navegação encaminha para o workspace independente de Inteligência;
- API e UI expõem apenas nomes de conta e referências opacas allowlisted, nunca payloads, notas, endereços ou conteúdo bruto; IDOR cross-workspace devolve `404` e falhas da UI são genéricas.

### Evidência de execução da Tarefa 13

Em PostgreSQL 16 descartável local, sem dados ou credenciais reais:

```text
RED observado: collection falhou por ausência de src.crm.services.intelligence_service
Task 13 focused: 70 passed
CRM/API/security/unit/migration/persistence regression: 764 passed, 1 skipped
Alembic lifecycle: base -> 0001 -> 0002 -> 0003 -> 0004 -> 0005; downgrade até 0001;
re-upgrade até 0005; downgrade 0005 -> 0004; re-upgrade até 0005
Alembic check: No new upgrade operations detected
Ruff, format dos ficheiros de implementação da Tarefa 13, compileall e git diff --check: passed
```

O skip e os quatro warnings de `TemplateResponse` são preexistentes. Não houve deploy, push, envio de mensagens, acesso ou mutação de sistemas live, nem criação de sentinel. As Tarefas 14–19 continuam pendentes.

---

## Estado da implementação até à Tarefa 14

### API idempotente e scoped de eventos de Agent concluída localmente

A Tarefa 14 acrescenta exclusivamente `POST /api/v1/agent-events` como fronteira de ingestão server-to-server:

- reutiliza exatamente o envelope estrito `schema_version: 1`, incluindo normalização de instantes equivalentes para UTC antes do hash;
- responde `202` para um evento novo, `200` para replay idêntico com o mesmo `event_id` e estado e `duplicate: true`, e `409` quando a mesma chave representa payload normalizado diferente;
- rejeita schema/chave inválidos com `422`, autenticação ausente, inválida, expirada ou fora da janela temporal com `401`, e permission/source scope incompatível com `403`;
- usa um bearer de curta duração configurado apenas no servidor, ligado a uma workspace, permissão `agent-events:write` e source scopes explícitos;
- exige timestamp fresco e `Idempotency-Key`, compara credenciais em tempo constante e não inclui segredos, payloads ou valores externos em logs ou erros;
- limita o corpo antes de parsing/validação e devolve erros genéricos em todas as entradas de validação;
- persiste apenas uma linha `received` no ledger/fila `ingest_events`; não cria entidades de domínio, não agenda outbound e não executa jobs;
- mantém a transação no chamador HTTP; o repositório continua sem commit implícito e falhas fazem rollback.

### Evidência de execução da Tarefa 14

Em PostgreSQL 16 descartável local, migrado até `0005` (`head`), sem dados ou credenciais reais:

```text
RED observado: collection falhou por ausência de get_agent_settings
Task 14 focused: 14 passed
CRM/API/security/unit/migration/persistence regression: 778 passed, 1 skipped
Ruff e format dos ficheiros alterados: passed
```

O skip e os quatro warnings de `TemplateResponse` são preexistentes. Não houve deploy, push, outbound, acesso ou mutação de sistemas live, nem criação de sentinel. As Tarefas 15–19 continuam pendentes.

---

## Estado da implementação até à Tarefa 15

### Human commands, outbox transacional e auditoria concluídos localmente

A Tarefa 15 acrescenta uma fronteira de comandos humanos que mantém a mutação de domínio, a mensagem de outbox e o registo de auditoria na mesma transação controlada pelo chamador:

- transições de fase exigem principal confiável da mesma workspace, permissão explícita e `expected_version`;
- uma transição humana para fase que exige conta é rejeitada atomicamente quando o lead não tem conta associada, em vez de quebrar a invariante central;
- conflitos de versão, replay divergente e fases inválidas devolvem erros genéricos;
- replay idêntico não repete a mutação nem cria nova outbox/auditoria, incluindo concorrência;
- IDs determinísticos incluem a workspace, evitando colisões quando duas workspaces usam o mesmo `command_id`;
- rollback persiste zero alterações; commit persiste domínio, outbox pendente e auditoria em conjunto;
- payloads da outbox são JSON estrito e limitados antes de chegar ao PostgreSQL;
- auditoria é append-only tanto no ORM como por trigger PostgreSQL;
- o helper de outbox nunca publica, envia ou faz commit. Workers externos só podem atuar depois da transação canónica estar persistida.

A migration `0006` cria `outbox_events` e `audit_events`, com isolamento por workspace, estados e payloads limitados, índices operacionais e rollback aditivo.

### Evidência de execução da Tarefa 15

Em PostgreSQL 16 descartável local, sem dados ou credenciais reais:

```text
Task 15 command/outbox focused: 13 passed
Persistence lifecycle + Task 15: 45 passed
CRM/API/security/unit/migration/persistence regression: 791 passed, 1 skipped
Alembic lifecycle explícito: 0006 -> 0005 removeu outbox/audit; 0005 -> 0006 restaurou head
Alembic current: 0006 (head)
Alembic check: No new upgrade operations detected
Ruff, format, compileall e git diff --check: passed
```

O skip e os quatro warnings de `TemplateResponse` são preexistentes. Não houve deploy, push, outbound, acesso ou mutação de sistemas live, nem criação de sentinel. As Tarefas 16–19 continuam pendentes.

---

## Estado da implementação até à Tarefa 16

### Conectores checkpointed e materialização source-first concluídos localmente

A Tarefa 16 acrescenta conectores read-only, desativados por omissão e limitados por allowlist de scope, reconciliação transacional e um processor canónico:

- Gmail recupera de cursor expirado por resync seguro; Calendar preserva revisão/reagendamento; notas de reunião fazem um retry transitório; Sheets continua estritamente read-only;
- cada página persiste eventos e avança o checkpoint na mesma transação, serializada por connector/workspace/scope/stream;
- eventos repetidos e fora de ordem são deduplicados sem recuar o high watermark;
- o processor aplica evento, identidade, conta, contacto, lead, atividade, proposta/versão/evidência e estado do ledger numa única transação física;
- crash antes do commit deixa zero agregados parciais e o evento retryable;
- propostas Gmail e reuniões Calendar/Granola comerciais criam entidades sem qualquer linha de Sheet;
- eventos pessoais/fornecedores explicitamente excluídos ficam `ignored`; matching exato ambíguo fica `review`, sem merge por nome;
- sector e vertical comercial atravessam os três conectores comerciais para os agregados canónicos;
- `scripts/crm_reconcile.py` e `scripts/crm_worker.py` são dry-run por omissão, falham fechados sem PostgreSQL configurado e nunca enviam mensagens nem publicam outbox.

### Evidência de execução da Tarefa 16

Em PostgreSQL 16 descartável local, sem dados ou credenciais reais:

```text
Task 16 focused: 19 passed
CRM/API/security/unit/migration/persistence regression: 811 passed, 1 skipped, 4 warnings preexistentes
Crash transacional: domínio ficou vazio, event permaneceu received/attempt_count=0 e retry aplicou com sucesso
CLI reconcile apply #1: 1 inserted; apply #2: 0 inserted e 1 duplicate
CLI worker apply #1: 1 processed; apply #2: 0 eligible e 0 processed
```

Nenhum connector real, Sheet, Gmail, Calendar, Granola, outbox publisher ou sistema live foi ativado. Não houve outbound, deploy, push, merge, migração de produção ou criação de sentinel. As Tarefas 17–19 continuam pendentes.

---

## Estado da implementação até à Tarefa 17

### Observabilidade, restore de backup e runbooks concluídos localmente

A Tarefa 17 acrescenta uma superfície operacional fail-closed e documentação de recuperação:

- `GET /operacoes` e `GET /api/v1/operations/metrics` exigem um principal confiável com papel admin e usam exclusivamente a workspace desse principal;
- as métricas agregadas cobrem reachability da base, lag de eventos/outbox, idade de checkpoint, dead letters, reviews de reconciliação, propostas sem valor e violações da invariante de conta;
- páginas e API operacionais usam `Cache-Control: no-store` e não expõem payloads, scopes, identidades externas ou dados de outra workspace;
- `scripts/crm_verify_backup.py` rejeita destinos remotos/não descartáveis, aceita apenas dumps custom-format e restaura para uma base aleatória num PostgreSQL 16 local;
- o verifier confirma revisão Alembic, tabelas obrigatórias, órfãos e invariantes, removendo depois apenas a base que criou;
- `RUNBOOK.md`, `MIGRATION.md`, `ROLLBACK.md` e `SECURITY.md` documentam pausa de workers/outbound, shadow migration, restore obrigatório, flags, rollback de reads/writes e blockers de exposição;
- o limite declarado do container subiu de 256 MB/0.25 CPU para 512 MB/0.5 CPU; continua a exigir benchmark/soak antes de produção.

### Evidência de execução da Tarefa 17

Em PostgreSQL 16 descartável local, migrado até `0006`, sem dados ou credenciais reais:

```text
Task 17 focused: 10 passed
CRM/API/security/unit/migration/persistence regression: 821 passed, 1 skipped, 4 warnings preexistentes
Backup custom-format restaurado: schema=0006, 11 tabelas obrigatórias, 0 workspaces, 0 violações
Base aleatória de restore e arquivo temporário removidos
Alembic check: No new upgrade operations detected
Ruff, format e git diff --check: passed
```

Esta evidência valida somente o caminho local. Não prova existência/restaurabilidade de backup de produção. Não houve deploy, acesso a dados reais, ativação de identidade, push, merge ou criação de sentinel. As Tarefas 18–19 continuam pendentes.

---

## Estado da implementação até à Tarefa 18

### Cutover guardado concluído localmente

A Tarefa 18 acrescenta controlos de ativação estritos, import-safe e fail-closed:

- as seis flags de cutover têm defaults que preservam reads legados e deixam PostgreSQL, agent ingress, projeção e conectores consequentes desligados;
- booleanos e enums aceitam apenas os literais documentados, sem coerção de casing, espaços ou valores truthy;
- combinações que ativem funcionalidades de base sem `CRM_DB_ENABLED=true`, ou projeção Sheets sem writer PostgreSQL, impedem o startup;
- Contas e Propostas só abrem uma sessão PostgreSQL para tráfego quando o respetivo read model é exatamente `postgres`; `shadow` permanece comparação offline e não altera respostas;
- Inteligência e Operações também rejeitam pedidos antes de abrir PostgreSQL quando a base canónica está desligada;
- agent events desativados ficam ocultos por `404` antes de autenticação, parsing ou acesso à base;
- `CRM_COMMAND_WRITER=postgres` bloqueia todas as mutações legadas antes de chamar o adapter Sheets, impedindo dual-write acidental;
- `.env.example`, Kamal e `MIGRATION.md` documentam o baseline seguro e a progressão de uma dimensão de cada vez.

Durante a regressão final foi reproduzida uma corrida preexistente do backfill: uma transação antiga podia atualizar `last_seen_at` com `now()` anterior ao `first_seen_at` persistido por uma concorrente. O commit `25db7a5` usa o wall clock PostgreSQL e preserva explicitamente o lower bound. O teste concorrente passou dez execuções consecutivas e o módulo de backfill passou integralmente.

### Evidência de execução da Tarefa 18

Em PostgreSQL 16 descartável local, sem dados ou credenciais reais:

```text
Task 18 focused: 25 passed
Cutover/API/security focused: 174 passed, 3 warnings preexistentes
CRM unit + integration + security + migration + regressão: 847 passed, 4 warnings preexistentes
Source-identity concurrency regression: 10 execuções consecutivas passaram
Account backfill module: 12 passed
Alembic lifecycle: 0006 -> base -> 0006
Alembic current: 0006 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0006, 11 tabelas obrigatórias, 0 workspaces, 0 violações
Ruff, compileall e git diff --check: passed nos ficheiros da Tarefa 18; main.py mantém formatação legada fora das linhas alteradas
```

A suite global não-CRM continua a ter os blockers históricos documentados anteriormente; a suite completa relevante para o CRM passou. O ambiente live observado continua na imagem/commit `7622a2b`, sem PostgreSQL. Não existe staging observável nem adapter de identidade de produção neste branch. Não foram executados backfill real, validação humana de amostra, soak, deploy ou cutover.

A Tarefa 19 permanece bloqueada pelo próprio gate do plano: exige dois releases sem rollback, telemetria que prove ausência de consumidores v0, export Sheet disponível e aprovação de stakeholders. Remover o legado antes dessa evidência violaria a estratégia aditiva e o rollback. O sentinel não pode ser criado enquanto esses gates, staging e verificação de produção não existirem.

---

## Handoff verificável após a Tarefa 18

A implementação da Tarefa 18 foi commitada em `06ed08b70b0c5f0f97a483cecc7af41362a74562` (`feat: add guarded CRM read and write cutover`) e publicada no branch remoto `origin/feat/crm-accounts-proposals-v1`. O pull request draft é `https://github.com/zelusototmayor/lead-automation/pull/1`; não tem checks CI configurados e permanece draft para impedir merge/cutover antes dos gates.

A verificação pós-commit repetiu 39 testes focados com PostgreSQL 16 descartável. A suite CRM completa observada nesta retoma passou com 847 testes e 4 warnings preexistentes. O lifecycle Alembic `0006 -> base -> 0006`, `alembic check`, Ruff, compileall, diff check, secret scan e restore de backup custom-format passaram. O PostgreSQL descartável criado nesta retoma foi removido e a porta local ficou livre.

A coleção global do repositório continua a terminar com exit code 3 porque `tests/test_linkedin_system.py` executa 47 checks com sucesso e chama `sys.exit(0)` durante collection. Este ficheiro é exterior ao âmbito CRM e não foi alterado.

Não existe evidência disponível de staging, adapter de identidade de produção, PostgreSQL/backup live, amostra real validada pelo owner, soak ou dois releases estáveis. Por isso não houve merge, deploy, migração/backfill live, ativação de conectores, cutover nem criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma verificada em 2026-07-17

A retoma partiu do `HEAD` limpo `2e222e8bc6c0531384ce68c32eb4a3068b61f100`, já sincronizado com `origin/feat/crm-accounts-proposals-v1`. O delta de produção `7622a2b` para o alias de outreach já está presente no branch por patch equivalente (`61113a0`), portanto não há hotfix live conhecido por incorporar.

### Gates locais repetidos

Num PostgreSQL 16 descartável novo, removido no fim da execução:

```text
CRM unit + integration + security + migration + regressão: 846 passed, 1 skipped, 4 warnings preexistentes
Alembic lifecycle: base -> 0006 -> base -> 0006
Alembic current: 0006 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0006, 11 tabelas obrigatórias, 0 workspaces, 0 violações
Ruff no delta origin/main...HEAD, compileall e git diff --check: passed
Gitleaks: 28 commits e cerca de 1,02 MB analisados, sem leaks
```

O Ruff global continua a encontrar uma variável não usada preexistente em `src/crm/local_services_sheet.py`, fora do delta deste branch. A coleção global continua a terminar com exit code 3 porque `tests/test_linkedin_system.py` executa 47 checks com sucesso e chama `sys.exit(0)` durante collection. Nenhum destes ficheiros foi alterado.

A imagem de dashboard foi construída localmente a partir deste `HEAD`. O smoke test da imagem confirmou `/up=200`, rotas ricas em modo deny-only com `403` e agent ingress desativado com `404` usando as flags seguras.

### Descoberta operacional live, sem mutações

O dashboard live continua saudável na imagem/commit `7622a2b2b8d5e0790858208b2c3a1f119edb7328`. O host tem um PostgreSQL 17 nativo pertencente à infraestrutura existente, mas não apresenta qualquer base com nome CRM/leads, backup CRM ou staging observável; este PostgreSQL não foi usado nem alterado. O container live também não tem as flags/segredos do novo CRM, e as rotas novas devolvem `404` porque essa imagem é anterior ao revamp. O PR `#1` permanece draft, mergeable, sem reviews e sem checks CI configurados.

Consequentemente, os gates externos continuam por satisfazer: identidade/sessão e RBAC de produção, política de retenção e scopes, PostgreSQL com backup automático e restore do arquivo real, staging, migrations/backfill/reconcile idempotentes sobre cópia real, amostra validada pelo owner comercial, browser/security smoke, soak, cutover verificado e dois releases estáveis antes da Tarefa 19. Fazer deploy, merge, remoção do legado ou criar o sentinel sem esta evidência violaria os gates técnicos explícitos do plano.

---

## Retoma e verificação final local em 2026-07-17T13:15:18Z

A retoma preservou as quatro alterações locais encontradas sobre `3d56b6d` e fechou-as em três commits atómicos:

- `0964f35` atualiza a chamada legada de `TemplateResponse` para a assinatura atual;
- `73a115a` torna os 47 checks LinkedIn compatíveis com pytest e mantém o modo standalone; `pytest.ini` limita a descoberta automática a `tests/`, excluindo os scripts live/outbound indevidamente nomeados `test_*.py` na raiz;
- `a27ff25` corrige o exemplo do restore verifier para usar uma base local de teste existente.

O documento de decisão de segurança foi reconciliado com o código e com o gate atual: apenas a superfície legada read-only mantém a exposição histórica; as novas rotas PostgreSQL de Contas, Propostas, Inteligência e Operações permanecem deny-only até existir um principal server-side com papel e workspace verificados.

### Evidência local repetida

Num PostgreSQL 16 descartável, sem dados ou credenciais reais:

```text
Suite automatizada segura completa: 848 passed em 97.91s
Suite completa com DeprecationWarning tratado como erro: 848 passed em 101.65s
Security + APIs ricas focadas: 123 passed
LinkedIn standalone: 47/47 checks, exit 0
Alembic lifecycle: base -> 0006 -> base -> 0006
Alembic current: 0006 (head)
Alembic check: No new upgrade operations detected, exit 0
Backup custom-format restaurado: schema=0006, 11 tabelas, 0 workspaces, 0 violações
Ruff no delta Python origin/main...HEAD: passed
compileall e git diff --check: passed
Gitleaks no intervalo `106485b..HEAD`: 0 leaks
```

A imagem Docker foi reconstruída a partir do candidato local. O smoke com flags seguras confirmou `/up=200`, `/contas=403`, `/propostas=403`, `/inteligencia=403` e `POST /api/v1/agent-events=404`. O dry-run da fixture de contas devolveu 4 imports potenciais, 3 contas criadas/associadas, 1 duplicado e 1 fase não mapeada, sem writes.

### Estado externo e blockers reais

O probe live read-only repetido confirmou `/up=200`; `/contas`, `/propostas`, `/inteligencia`, `/api/v1/accounts` e `/api/v1/proposals` continuam `404`, coerentes com a imagem de produção pré-revamp. O PR `#1` continua draft, mergeable e sem CI/reviews configurados. Não existe environment de staging configurado no GitHub nem staging observável no repositório/infraestrutura inspecionada.

Continuam ausentes os pré-requisitos que não podem ser fabricados por testes locais: adapter de identidade e mapping de utilizadores/papéis/workspace; política de retenção, scopes e prova de `Won`; PostgreSQL CRM staging/produção com backup automático e restore do arquivo real; cópia real para backfill/reconcile idempotente; validação da amostra pelo owner comercial; smoke browser/security em staging; soak; cutover verificado; e, para a Tarefa 19, dois releases estáveis, telemetria v0, export Sheet e aceitação de stakeholders.

Por isso não houve merge, deploy, migração/backfill live, ativação de workers/connectors/outbox, cutover ou remoção do legado. Os containers PostgreSQL descartáveis serão removidos após a verificação final. O sentinel `.hermes/crm-revamp-complete.json` continua corretamente ausente.

---

## Retoma autónoma em 2026-07-17T13:40:48Z

A retoma começou no `HEAD` limpo `8b63dd5e02e1e7cbc5f8d4f670385de76d566625`, seis commits à frente de `origin/feat/crm-accounts-proposals-v1`. O plano canónico, o histórico de commits e este documento foram reconciliados antes de qualquer execução. Não foi encontrada alteração staged, unstaged ou untracked a preservar.

### Verificação local repetida

Foi criado um PostgreSQL 16 descartável em `127.0.0.1:55432`, sem dados ou credenciais reais. Uma primeira execução da suite contra a base vazia confirmou a pré-condição operacional: os testes de API que usam fixtures relacionais não executam migrations automaticamente e falharam por ausência de `workspaces`. Depois de `alembic upgrade head`, a execução canónica passou:

```text
Suite automatizada segura completa, com DeprecationWarning como erro: 848 passed em 95.69s
Alembic lifecycle: 0006 -> base -> 0006
Alembic current: 0006 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0006, 11 tabelas, 0 workspaces, 0 violações
Ruff no delta Python origin/main...HEAD: passed
compileall e git diff --check: passed
Gitleaks no intervalo 106485b..HEAD: 31 commits, cerca de 997 KB, 0 leaks
```

A imagem Docker do candidato foi reconstruída localmente. O smoke com os defaults seguros confirmou `/up=200`, `/contas=403`, `/propostas=403`, `/inteligencia=403` e `POST /api/v1/agent-events=404`.

### Gates externos revalidados

O probe live read-only confirmou `/up=200` e `404` para as novas páginas e APIs, coerente com a imagem de produção pré-revamp. O PR `#1` continua draft, mergeable e sem CI ou reviews configurados. A API GitHub não devolveu environments configurados e os nomes de staging inspecionados não têm registo DNS.

O host de produção tem apenas cerca de 1 GiB de memória disponível, 3,2 GiB de disco livre e filesystem a 87%, além de vários workloads existentes. Não existe base CRM dedicada observável. Provisionar nesse host, por omissão, PostgreSQL CRM, staging e backups violaria os gates de capacidade, isolamento e restore do plano; a infraestrutura existente não foi reutilizada nem alterada.

Continuam indisponíveis os artefactos externos obrigatórios: adapter de identidade server-side e mapping de utilizadores/papéis/workspace; retenção, scopes e prova oficial de `Won`; staging isolado; PostgreSQL CRM com backup automático; cópia real para dois backfills/reconciliations idempotentes; validação humana da amostra; browser/security smoke e soak em staging; cutover verificado; e os dois releases estáveis exigidos antes da Tarefa 19. O deploy e o sentinel continuam bloqueados pelos gates técnicos, não por uma pausa de aprovação.

### Fecho desta retoma em 2026-07-17T14:15:04Z

A verificação repetida confirmou 848 testes com `DeprecationWarning` tratado como erro, lifecycle Alembic `0006 -> base -> 0006`, `alembic check`, restore de backup custom-format, Ruff, compileall, diff check, Gitleaks, build/smoke da imagem e dry-run do backfill. Nenhum worker CRM, reconciler ou job de outreach estava ativo durante a migração/testes. O PostgreSQL e a imagem descartáveis foram removidos e a porta 55432 ficou livre.

Os seis commits locais encontrados e o commit de evidência `911a1abf3486007956321900b90b052d4ba76889` foram publicados em `origin/feat/crm-accounts-proposals-v1`; o PR permanece draft e sem checks porque staging e os gates externos continuam ausentes. O branch não foi merged nem deployed. A Tarefa 19 e o sentinel permanecem corretamente bloqueados.

---

## Validação shadow com a Sheet real em 2026-07-17T15:46:23Z

Foi criada uma snapshot local temporária, mode `0600`, através do scope Google Sheets read-only. A credencial foi copiada para um ficheiro temporário, nunca impressa, e removida imediatamente após a captura. A snapshot também foi removida depois da validação.

O schema real confirmou 1.247 linhas, `ID` vazio em todas as linhas, headers `Stage` e `Contact`, datas de proposta no formato `YYYY/MM/DD`, 13 identidades duplicadas, 5 linhas sem identidade fallback completa e 1.216 linhas elegíveis para snapshot. O adaptador agora exige grupos fallback explícitos e source-scoped; não usa row number nem company-only identity.

Num PostgreSQL 16 descartável separado, migrado até `0006`, o mesmo input foi aplicado duas vezes:

```text
Accounts apply #1: 68 imports, 46 accounts criadas/associadas, 27 conflitos
Accounts apply #2: 0 imports, 68 replay no-op, 0 duplicados novos
Proposals apply #1: 44 imports, 6 unmatched accounts, 50 missing value/evidence
Proposals apply #2: 0 imports, 44 replay no-op, 0 duplicados novos
Compare: parity=false, 3 leads/accounts em falta, 0 stage/account/source-field mismatches
Invariantes: 0 leads rank>=40 sem account, 0 zeros sintéticos missing, 0 Won conflado com Meeting Booked, 0 eventos failed/dead-letter
Backup custom-format do shadow restaurado e verificado: schema 0006, 11 tabelas, 1 workspace, 0 violações
```

Os resultados provam idempotência do input aplicável e preservação das invariantes, mas não satisfazem o gate de dados: 3 conflitos de identidade bloqueiam paridade, 6 propostas continuam sem account correspondente, 19 terminais exigem histórico/revisão e 1.126 linhas têm fase vazia ou não mapeada. Estes casos não foram auto-fundidos nem convertidos em aliases. A base shadow, backup e snapshot descartáveis foram removidos. Não houve write na Sheet, envio outbound, merge, deploy, migração live, cutover ou criação do sentinel.

---

## Fecho do candidato local em 2026-07-17T17:11:37Z

As regressões descobertas pela compatibilidade com a Sheet real foram fechadas sem relaxar o matching: identidades fallback normalizam email IDN, telefone e texto NFKC, `Company` isolado é rejeitado, todos os IDs duplicados e linhas sem identidade entram em conflito, datas de proposta inválidas não promovem a fase e colunas `Status`/`Stage` contraditórias entram em review. Aliases canónicos equivalentes nas duas colunas são aceites. A configuração Alembic também deixou de emitir o aviso de `prepend_sys_path`.

Evidência repetida no PostgreSQL 16 descartável local:

```text
Compatibilidade real Sheet + CLI + bootstrap: 82 passed, 1 skipped
Suite migration: 62 passed
Suite automatizada segura completa com DeprecationWarning como erro: 866 passed, 1 skipped em 99.83s
Alembic lifecycle: 0006 -> base -> 0006
Alembic current: 0006 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0006, 11 tabelas, 0 workspaces, 0 violações
Ruff, format check e git diff --check: passed nos ficheiros alterados
```

O gate da Tarefa 19 continua materialmente fechado. A telemetria agregada das últimas 48 horas no proxy de produção mostrou consumidores ativos dos endpoints v0: `/api/stats` 13 pedidos, `/api/portfolio` 4, `/api/recommendations` 4, `/api/outreach-followups` 14, `/api/email-followups` 11 e `/api/proposal-followups` 12. Remover esses contratos quebraria consumidores observados.

O host live continua sem base CRM dedicada e com apenas 3,2 GiB livres no filesystem, já a 87%, além de múltiplos workloads. Não existe staging isolado, adapter de identidade, backup automático CRM, amostra humana aprovada, soak ou dois releases estáveis. Estes gates técnicos impedem merge, deploy, cutover, remoção do legado e criação do sentinel; não são substituídos pela autorização YOLO nem pelos testes locais.

---

## Handoff pós-commit em 2026-07-17T17:21:47Z

O candidato de compatibilidade com a Sheet real foi congelado com digest staged `59377815773f74bfea46417f83a27a2cbc22f4214d3ead1fffd728950fcc5736`, commitado como `132959993d10f77077344bfe288d05392e9f1d07` (`fix: harden real Sheet migration compatibility`) e publicado em `origin/feat/crm-accounts-proposals-v1`. A worktree ficou limpa e o PR `#1` continuou draft, sem checks CI configurados.

A verificação final desse candidato registou 866 testes passados e 1 skip, lifecycle Alembic `0006 -> base -> 0006`, `alembic check`, restore de backup custom-format, Ruff, format check, compileall, diff check, Gitleaks staged e build/smoke da imagem com defaults fail-closed. O PostgreSQL descartável foi removido e a porta local usada na verificação ficou livre.

Os gates externos foram revalidados sem mutações: as novas rotas live continuam `404`; não existem environments GitHub nem DNS de staging; o container live não tem configuração CRM/identidade; não existe base CRM dedicada no host; e a telemetria das últimas 48 horas continua a mostrar consumidores v0 ativos. Sem staging isolado, identidade server-side, PostgreSQL CRM com backup real restaurado, resolução dos conflitos de dados, validação humana da amostra, soak e dois releases estáveis, a sequência de cutover do plano não pode começar com segurança. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, remoção do legado ou criação do sentinel.

---

## Retoma autónoma em 2026-07-17T18:13:04Z

A retoma começou no `HEAD` limpo e sincronizado `77d14166c75ff2fd425c0f5f00a2a9f48323a606`. O plano canónico, o histórico, o estado da worktree, os processos locais e os gates externos foram reinspecionados antes de qualquer ação. Não existiam alterações staged, unstaged ou untracked a preservar, nem workers CRM/outbox ativos.

Num PostgreSQL 16 descartável novo, sem dados reais ou credenciais live, a suite segura completa passou com `866 passed, 1 skipped` e `DeprecationWarning` tratado como erro. O lifecycle Alembic `base -> 0006 -> base -> 0006`, `alembic current`, `alembic check` e o restore de um dump custom-format passaram; o restore confirmou schema `0006`, 11 tabelas, zero workspaces e zero violações. Ruff check, compileall, `git diff --check` e Gitleaks (`106485b..HEAD`, 36 commits, cerca de 1,04 MB, zero leaks) passaram. O format check global do delta continua a identificar 11 ficheiros legados/preexistentes que não foram reformatados para evitar scope creep; esta dívida não é uma regressão desta retoma.

A imagem Docker foi reconstruída a partir do `HEAD`. O smoke real do container com os defaults seguros confirmou `/up=200`, `/contas=403`, `/propostas=403`, `/inteligencia=403`, `/operacoes=403` e `POST /api/v1/agent-events=404`. Os containers PostgreSQL/app descartáveis desta verificação e dois containers de verificação antigos sem volumes persistentes foram removidos; as portas `55432`, `55434`, `58000` e `58001` ficaram livres.

A descoberta live permaneceu read-only. Produção continua na imagem `7622a2b2b8d5e0790858208b2c3a1f119edb7328`, com `/up=200` e as novas páginas/APIs em `404`. A telemetria agregada das últimas 48 horas confirmou consumidores v0 ativos (`/api/stats`: 65, `/api/portfolio`: 29, `/api/recommendations`: 29). O PR `#1` permanece draft, mergeable, sem reviews ou checks; não existem GitHub environments nem DNS de staging. O host continua sem base CRM dedicada, com o filesystem a 87% e apenas 3,2 GiB livres, tornando inseguro improvisar PostgreSQL/staging/backup no mesmo host.

A primeira tarefa genuinamente incompleta continua a ser a Tarefa 19, mas o seu gate está materialmente fechado: há consumidores v0 ativos e não existem dois releases estáveis, telemetria de ausência de consumidores, export aprovado nem aceitação de stakeholders. Os gates anteriores de cutover também continuam por satisfazer: identidade/RBAC/workspace mapping server-side, políticas de retenção/scopes/prova de `Won`, staging isolado, PostgreSQL CRM com backup automático e restore real, resolução dos conflitos da shadow migration, validação humana da amostra, smoke browser/security e soak. Por isso não houve merge, deploy, migração/backfill live, cutover, remoção do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma e compatibilidade de identidade em 2026-07-17T18:54:41Z

A retoma começou no `HEAD` limpo e sincronizado `f315372e7f311ce918e5f5ada703ca4a705511e2`. A suite segura completa foi repetida num PostgreSQL 16 descartável: `866 passed, 1 skipped`; o lifecycle `0006 -> base -> 0006`, `alembic current`, `alembic check`, restore custom-format, Ruff, compileall, diff check, Gitleaks e build/smoke da imagem passaram. O smoke confirmou `/up=200`, as páginas ricas deny-only em `403` e agent ingress desativado em `404`.

Uma nova captura read-only da Sheet real revelou um caso de compatibilidade não coberto: valores de email/telefone não canónicos faziam a captura inteira falhar em vez de isolar apenas as linhas sem identidade segura. Foi observado RED num teste focado (`1 failed`), corrigido para contabilizar o payload antes da validação e encaminhar apenas a linha malformada para review, sem usar o fallback mais fraco nem expor o valor. A regressão focada passou (`43 passed`) e a suite completa posterior passou com `867 passed, 1 skipped`; o lifecycle Alembic e `alembic check` voltaram a passar. A formatação posterior do teste foi seguida por nova execução focada verde.

A shadow migration atual, num PostgreSQL 16 descartável e com snapshot temporária mode `0600`, produziu somente agregados seguros:

```text
Snapshot: 1.247 input rows, 1.202 aplicáveis, 12 identidades duplicadas, 21 linhas sem identidade segura
Accounts apply #1: 65 imports, 46 accounts criadas/associadas, 52 conflitos
Accounts apply #2: 0 imports, 65 replay no-op
Proposals apply #1: 44 imports, 4 unmatched accounts, 48 missing value/evidence
Proposals apply #2: 0 imports, 44 replay no-op
Compare: parity=false, 1 lead/account em falta, 0 stage/account/source-field mismatches
Invariantes: 0 leads rank>=40 sem account, 0 zeros sintéticos missing, 0 eventos failed/dead-letter
```

A credencial temporária, snapshot e base shadow foram removidas. Não houve write na Sheet nem outbound. A telemetria JSON do proxy nas últimas 48 horas continua a provar consumidores v0 ativos: `/api/stats` 24, `/api/portfolio` 8, `/api/recommendations` 8, `/api/outreach-followups` 30, `/api/email-followups` 25 e `/api/proposal-followups` 26. Produção continua saudável no commit `7622a2b2b8d5e0790858208b2c3a1f119edb7328`, sem as rotas novas.

Os gates externos permanecem fechados e não podem ser substituídos por automação local: adapter de identidade e mapping server-side, políticas de retenção/scopes/prova de `Won`, staging isolado, PostgreSQL CRM com backup automático e restore do ficheiro real, resolução/revisão humana dos conflitos e da amostra, soak/cutover e dois releases estáveis sem consumidores v0 antes da Tarefa 19. O host observado tem 3,2 GiB livres e filesystem a 87%, sem base CRM ou backup automático; improvisar staging/produção nesse host violaria os gates de capacidade e isolamento. O sentinel continua corretamente ausente.

---

## Fecho verificável da retoma em 2026-07-17T19:14:06Z

A alteração preservada acima foi congelada com digest staged `2af1c6b0da3ae22715de6fa868cc4a1c1257501fb374387340b69db2831678b`, commitada como `e3583fa4d1b9367995991939b46dbafdee7d7b7c` (`fix: isolate malformed Sheet identities`) e publicada em `origin/feat/crm-accounts-proposals-v1`. A regressão pós-commit passou com `33 passed`; Ruff e `git diff --check` passaram.

Num PostgreSQL 16 descartável novo, explicitamente marcado para testes, a suite segura completa passou com `867 passed, 1 skipped` e `DeprecationWarning` tratado como erro. O lifecycle Alembic `0006 -> base -> 0006`, `alembic current`, `alembic check`, a suite de migration (`63 passed`) e o restore de um dump custom-format passaram. O restore confirmou schema `0006`, 11 tabelas obrigatórias, zero workspaces e zero violações. Ruff no delta, compileall, scan estático de linhas adicionadas e Gitleaks no histórico e staged diff passaram sem findings.

A imagem local do candidato foi reconstruída com digest `sha256:dd81c39308ec6809996c31a05fa183683f5a6bfeadd07ff8ef780b072a6da013`. O smoke real confirmou `/up=200`, `/contas=403`, `/propostas=403`, `/inteligencia=403`, `/operacoes=403` e `POST /api/v1/agent-events=404`, sem erros no log do arranque.

Os gates externos foram novamente consultados sem mutações. O PR `#1` permanece draft, mergeable e sem checks/reviews; a API GitHub continua sem environments e os nomes de staging continuam sem DNS. Produção responde `/up=200` e `404` nas novas páginas/APIs, coerente com a imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`. O host continua com filesystem a 87%, 3,2 GiB livres, sem base ou backup CRM. A telemetria agregada das últimas 48 horas continua a provar consumidores v0 ativos: `/api/stats` 24, `/api/portfolio` 8, `/api/recommendations` 8, `/api/outreach-followups` 30, `/api/email-followups` 25 e `/api/proposal-followups` 26.

Consequentemente, merge, staging, produção, cutover e Tarefa 19 continuam tecnicamente bloqueados pelos gates explícitos do plano: não há identidade/RBAC/workspace mapping de produção, políticas aprovadas de retenção/scopes/prova de `Won`, staging isolado, PostgreSQL CRM com backup automático e restore real, resolução e validação humana da amostra shadow, soak, nem dois releases estáveis sem consumidores v0. Não foi criado `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma e adapter de identidade em 2026-07-17T20:28:28Z

A retoma preservou integralmente o candidato staged encontrado sobre `a2d910b` e fechou o blocker técnico do adapter de identidade das rotas ricas no commit `4280bc8531832bc88c0c4e904bff0d6c0e6ce450` (`security: add protected CRM principal adapter`), publicado em `origin/feat/crm-accounts-proposals-v1`.

O adapter HTTP Basic está limitado às páginas e APIs ricas já protegidas por `require_crm_principal`. Username, password, workspace UUID e papel admin vêm exclusivamente da configuração server-side; configuração ausente, incompleta, não ASCII ou malformada falha com `403` sem challenge, e credenciais browser ausentes/malformadas/incorretas recebem `401` genérico. O workspace e papel não podem ser substituídos por query, headers ou cookies. O health check, superfície legada pública read-only, writes humanos e bearer de Agent conservam contratos separados. Os placeholders Kamal continuam deliberadamente incompletos, portanto o deploy atual permanece fail-closed.

### Evidência local repetida

Num PostgreSQL 16 descartável novo, sem dados reais ou credenciais live:

```text
Suite segura completa com DeprecationWarning como erro: 904 passed em 101.03s, exit 0
Security/bootstrap pós-commit: 92 passed, 1 skipped
Persistence + migration: 291 passed
Alembic lifecycle: 0006 -> base -> 0006
Alembic current: 0006 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0006, 11 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff check e Gitleaks: passed
Imagem candidata: build local concluído
Smoke com defaults: /up=200; rotas ricas=403; agent ingress=404
Smoke autenticado com DB/read models PostgreSQL: páginas e APIs de Contas, Propostas, Inteligência e Operações=200; request sem credenciais=401; agent ingress desativado=404
```

O PostgreSQL descartável e a imagem local foram usados apenas para testes e removidos no fim; as portas `55432`, `58000` e `58001` ficaram livres. O ficheiro local temporário de secrets Kamal foi igualmente removido. Nenhum worker CRM, reconciler, outbox publisher ou job outbound estava ativo. Nenhum email ou write em Sheet foi executado.

### Gates externos revalidados

Produção continua saudável na imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up=200` e as novas páginas/APIs continuam `404`. O PR `#1` permanece draft e mergeable, sem checks/reviews; a API GitHub não apresenta environments e os nomes de staging verificados não têm DNS. O token GitHub disponível também não possui scope `workflow`, pelo que um workflow de candidato validado localmente não pôde ser publicado; o respetivo patch foi preservado fora do repositório em `/Users/max/.hermes/plans/crm-candidate-ci.patch`.

O host live tem 24 GiB de filesystem, 20 GiB usados, 3,2 GiB livres, 3,8 GiB RAM total, cerca de 1,5 GiB disponível e 1,2 GiB de swap em uso. Não existe base CRM dedicada nem staging isolado. Criar ambos no mesmo host sem margem de restore e sem limpeza aprovada de workloads não relacionados violaria os gates de capacidade e isolamento. Não foi executado prune nem alterado qualquer workload externo.

A telemetria agregada das últimas 48 horas continua a provar consumidores v0 ativos: `/api/stats` 24, `/api/portfolio` 8, `/api/recommendations` 8, `/api/outreach-followups` 30, `/api/email-followups` 25 e `/api/proposal-followups` 26. Continuam também sem evidência os gates que não podem ser fabricados localmente: políticas/owner decisions de retenção, scopes e prova oficial de `Won`; staging isolado; PostgreSQL CRM com backup automático e restore do ficheiro real; resolução e validação humana dos conflitos/amostra shadow; browser/security smoke externo; soak/cutover; e dois releases estáveis sem consumidores v0 antes da Tarefa 19.

Por isso não houve merge, deploy, migração/backfill live, ativação de conectores/workers/outbox, cutover, remoção do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma em 2026-07-17T21:45:00Z

A retoma começou no `HEAD` limpo e sincronizado `171dcb81fde15864f0934abb11a4c57008a3310e`. O plano canónico, `CURRENT_STATE.md`, branch, histórico, testes e processos foram inspecionados antes de qualquer alteração. Não existia trabalho staged, unstaged ou untracked, nem worker CRM, reconciler, outbox publisher ou job outbound ativo.

### Evidência local repetida

Num PostgreSQL 16 descartável novo, explicitamente marcado para testes e sem dados ou credenciais live:

```text
Suite segura completa com PostgreSQL e DeprecationWarning como erro: 904 passed em 102.17s, exit 0
Alembic lifecycle: 0006 -> base -> 0006
Alembic current: 0006 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0006, 11 tabelas, 0 workspaces, 0 violações
Backfill da fixture #1: 4 imports, 3 contas criadas/associadas, 1 conflito, 1 duplicado
Backfill idêntico #2: 0 imports, 4 replay no-op, 0 novos registos
Ruff lint no delta, compileall, git diff --check e Gitleaks: passed; 0 leaks em 41 commits
```

A imagem candidata foi construída localmente com digest `sha256:a3d09003e2e064b25a224260a33c8bfb333634a3125eb849f62f32e6ed50f737`. O smoke com defaults seguros confirmou `/up=200`, páginas ricas em `403` e agent ingress desativado em `404`. O smoke com PostgreSQL e principal Basic de teste confirmou `401` sem credenciais e `200` autenticado nas páginas/APIs de Contas, Propostas, Inteligência e Operações; agent ingress permaneceu `404`.

### Gates externos e decisão de cutover

A descoberta externa foi read-only. Produção continua saudável na imagem `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up=200` e as novas páginas/APIs devolvem `404`. O PR `#1` permanece draft, mergeable, sem checks ou reviews; não existem GitHub environments nem DNS para os nomes de staging inspecionados.

O host live continua sem base CRM dedicada, com filesystem a 87%, 3,2 GiB livres, 3,8 GiB de RAM e 1,5 GiB de swap em uso. Improvisar staging, PostgreSQL CRM e backups no mesmo host violaria os gates de capacidade, isolamento e restore. A telemetria agregada das últimas 48 horas prova consumidores v0 ativos: `/api/stats` 24, `/api/portfolio` 8, `/api/recommendations` 8, `/api/outreach-followups` 30, `/api/email-followups` 25 e `/api/proposal-followups` 26.

A primeira tarefa genuinamente incompleta continua a ser a Tarefa 19, cujo próprio gate exige dois releases sem rollback, ausência comprovada de consumidores v0, export Sheet e aceitação de stakeholders. Permanecem também fechados os gates de cutover anteriores: mapping real de principal/papel/workspace, políticas de retenção/scopes/prova de `Won`, staging isolado, PostgreSQL CRM com backup automático e restore do arquivo real, resolução e validação humana da amostra shadow, smoke browser/security externo e soak. A autorização YOLO remove pausas de aprovação, mas não cria estes artefactos nem permite falsear gates. Não houve merge, deploy, migração/backfill live, cutover, remoção do legado ou criação do sentinel.

---

## Retoma autónoma em 2026-07-18T03:41:51Z

A retoma começou no `HEAD` limpo e sincronizado `72c4025299c7ef367a574d6d8e57d41036ea2ed8`. O plano canónico, este documento, o histórico, os testes, o PR, os processos locais e o ambiente live foram reinspecionados antes de qualquer alteração. Não existia trabalho staged, unstaged ou untracked, nem worker CRM, reconciler, outbox publisher ou job outbound ativo.

### Evidência local repetida

Num PostgreSQL 16 descartável novo, explicitamente marcado para testes e sem dados ou credenciais live:

```text
Suite segura completa com PostgreSQL e DeprecationWarning como erro: 903 passed, 1 skipped em 101.34s, exit 0
Alembic lifecycle: 0006 -> base -> 0006
Alembic current: 0006 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0006, 11 tabelas, 0 workspaces, 0 violações
Ruff lint no delta, compileall, git diff --check e Gitleaks: passed; 0 leaks em 42 commits
```

A imagem candidata foi reconstruída localmente com digest `sha256:14554035cd45562c72eabd36dfd444250c2edf158e199bbdf873b4833942beb3`. O smoke com defaults seguros confirmou `/up=200`, páginas ricas em `403` e agent ingress desativado em `404`. O smoke com PostgreSQL e principal Basic de teste confirmou `401` sem credenciais e `200` autenticado nas páginas/APIs de Contas, Propostas, Inteligência e Operações. Os containers, a base e a imagem descartáveis foram removidos; as portas `55436`, `58002` e `58003` ficaram livres.

### Gates externos revalidados

Produção continua saudável na imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up=200` e as novas páginas/APIs continuam `404`. O PR `#1` permanece draft e mergeable, sem checks ou reviews, e a API GitHub continua sem environments configurados.

O host live continua sem base CRM dedicada, com filesystem a 87%, apenas 3,2 GiB livres, 3,8 GiB de RAM e 1,3 GiB de swap em uso. A telemetria agregada das últimas 48 horas prova consumidores v0 ativos: `/api/stats` 66, `/api/portfolio` 29, `/api/recommendations` 29, `/api/outreach-followups` 79, `/api/email-followups` 67 e `/api/proposal-followups` 69.

A primeira tarefa genuinamente incompleta permanece a Tarefa 19, mas remover os contratos legados agora quebraria consumidores observados e violaria o gate de dois releases estáveis. Também continuam ausentes staging isolado, PostgreSQL CRM com backup automático e restore real, mapping live de principal/papel/workspace, decisões de retenção/scopes/prova de `Won`, resolução e validação humana da amostra shadow, smoke browser/security externo e soak. Não houve merge, deploy, migração/backfill live, cutover, remoção do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma e fecho das lacunas canónicas em 2026-07-18T08:45:24Z

A retoma começou em `f19b6c8036a7faaff0cab56ea46967ebd7ca37a0` com trabalho staged, unstaged e untracked já existente. Todo esse trabalho foi preservado, auditado e concluído em dois commits atómicos:

- `77eee6e` adiciona a migration `0007`, modelos canónicos de email, reuniões, tarefas e reconciliation runs, constraints tenant-safe e verificação de backup atualizada;
- `f605ef6` liga os factos canónicos às APIs/observabilidade, isola dependências por runtime mode, torna retries de ingestão recuperáveis e acrescenta regressões de auth-before-I/O, shadow comparison, source-first processing e worker safety.

A primeira execução comportamental válida da suite encontrou sete falhas reais na projeção de Contas: o schema exigia contagens por estado de reunião, mas a query não as selecionava nem serializava. A correção mínima foi aplicada depois do RED e o target focado passou com `13 passed`.

### Evidência local deste candidato

Num PostgreSQL 16 descartável em loopback, explicitamente marcado para testes e sem dados ou credenciais live:

```text
Suite segura completa, com DeprecationWarning como erro: 947 passed, 1 skipped em 107.54s
Targets alterados e regressões canónicas: 136 passed
Alembic lifecycle: 0007 -> 0006 -> 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff, format check, compileall, git diff --check e Gitleaks: passed
Imagem local construída: manifest list sha256:1b31f258485f466adaef660f10f95266f476f685a241fb3cb78aaee8572f067f
Smoke com defaults seguros: /up=200; rotas ricas=403; agent ingress=404; 0 erros no log
```

A descoberta externa permaneceu read-only: o PR `#1` está draft, mergeable e sem checks/reviews; a API GitHub não apresenta environments; produção responde `/up=200` e `404` nas novas rotas, coerente com o build pré-revamp. Não existe staging configurado no repositório/GitHub.

Os gates externos continuam materialmente fechados: PostgreSQL CRM com backup automático e restore do arquivo real, staging isolado, mapping live de principal/papel/workspace, decisões de retenção/scopes/prova oficial de `Won`, resolução e validação humana da amostra shadow, browser/security smoke externo, soak/cutover e dois releases estáveis com ausência comprovada de consumidores v0 antes da Tarefa 19. Não houve merge, deploy, migração/backfill live, ativação de workers/conectores/outbox, remoção do legado ou criação do sentinel.

---

## Retoma autónoma em 2026-07-18T09:49:15Z

A retoma começou no `HEAD` sincronizado `9d071db317a6b7d4ac24b1a74ad0a2b057db1b8f` e preservou a alteração unstaged em `docs/crm/MIGRATION.md` e o novo `docs/crm/DECISIONS.md`. O plano canónico, commits, testes, migrations, processos e estado externo foram reinspecionados antes de editar. Não existiam workers CRM, reconciler, outbox publisher ou jobs outbound ativos.

### Evidência local repetida

Num PostgreSQL 16 descartável em loopback, explicitamente marcado para testes e sem dados ou credenciais live:

```text
Suite segura completa com DeprecationWarning como erro: 947 passed, 1 skipped, exit 0
Alembic lifecycle: 0007 -> 0006 -> 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected, exit 0
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Fixture de contas apply #1: 4 imports, 3 contas criadas/associadas, 1 conflito
Fixture de contas apply #2: 0 imports, 4 replay no-op, 0 novos registos
Ruff no delta Python, compileall, git diff --check e Gitleaks: passed; 0 leaks em 46 commits
Imagem local construída: manifest list sha256:b76359c422825ea122c92760c0e1bda41ca8c56d9b8e958749914493ef657d46
Smoke HTTP com defaults: /up=200; rotas ricas=403; agent ingress=404; 0 erros no log
Smoke HTTP autenticado com PostgreSQL: 401 sem credenciais; páginas/APIs ricas=200; agent ingress=404
```

A navegação browser local carregou a página protegida de Contas, mas a automação utilizada não propagou a credencial Basic ao `fetch` da API; por isso esta execução não é registada como browser smoke aprovado. O gate exige repetição num staging real com o adapter e o proxy finais.

### Gates externos revalidados

Produção continua saudável na imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up=200` e as novas páginas/APIs permanecem em `404`. O PR `#1` está draft, mergeable, sem reviews ou checks; a API GitHub não apresenta environments e os três nomes de staging inspecionados não têm DNS.

O host live continua sem base ou backup CRM identificável, com filesystem a 87%, 3,25 GB livres, 3,9 GB de RAM total, cerca de 1,1 GB disponível e 1,19 GB de swap em uso. A telemetria agregada do proxy nas últimas 48 horas ainda contém pedidos aos seis contratos v0 acompanhados (`/api/stats`, `/api/portfolio`, `/api/recommendations`, `/api/outreach-followups`, `/api/email-followups` e `/api/proposal-followups`: 1 cada). Não existe evidência de dois releases sem consumidores.

A primeira tarefa genuinamente incompleta permanece a Tarefa 19, mas o seu gate proíbe retirar contratos com consumidores observados e exige dois releases estáveis, export disponível e aceitação de stakeholders. Os gates anteriores também permanecem fechados: não há staging isolado, PostgreSQL CRM com backup automático e restore real, mapping live de principal/papel/workspace, decisão oficial de `Won`, políticas de retenção/scopes, resolução e validação humana da amostra shadow, browser/security smoke externo, soak ou cutover. Improvisar staging e PostgreSQL no host live sem capacidade/isolamento violaria os gates técnicos. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, remoção do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Fecho local da política de exposição em 2026-07-18T11:09:36Z

O candidato preservado nesta retoma fecha a lacuna entre a política privada por omissão do plano e a superfície legada: apenas `GET /up` permanece público e mínimo. Dashboard, redirects, `/ready`, APIs GET legadas, assets same-origin em `/static/*`, Contas, Propostas, Inteligência e Operações exigem o mesmo principal browser configurado no servidor antes de qualquer acesso a Sheet, ficheiro da aplicação ou PostgreSQL. OpenAPI e as páginas de documentação do framework estão desativados. Writes humanos e Agent ingress conservam fronteiras de autorização separadas.

Uma auditoria da tabela real de rotas encontrou `/static/*` ainda público no primeiro candidato. O teste focado falhou com `200` em vez de `401`, a fronteira foi movida para uma rota dependente de `require_crm_principal`, e uma regressão de fixtures que não propagavam o principal aos assets foi observada e corrigida antes da verificação final.

Num PostgreSQL 16 descartável novo, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 950 passed, 1 skipped em 109.56s, exit 0
Security/runtime/legacy focused: 109 passed, exit 0
Alembic lifecycle: 0007 -> 0006 -> 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected, exit 0
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff nos ficheiros Python alterados, compileall e git diff --check: passed
Scan estático de linhas adicionadas: um único match classificado como password fake de fixture; zero findings em código de produção
```

O container de verificação foi removido e a porta `55450` ficou livre. Os outros containers PostgreSQL preexistentes foram tratados como trabalho desconhecido e não foram alterados. Não existiam processos locais de worker CRM, reconciler ou outbox publisher. Esta verificação local não satisfaz staging, backup real, validação humana, soak/cutover nem o gate de retirada do legado; o sentinel continua proibido enquanto esses gates permanecerem abertos.

---

## Commit e revalidação externa em 2026-07-18T13:21:38Z

O candidato congelado no digest staged `699a6bd07179d434d8c887f3abf7f5eeec29219c86694bfd46903330815cf469` foi verificado localmente e commitado atomicamente como `aa91f02eb6f560872e9d441152b51deff80bb46e` (`security: protect CRM browser reads`). Tentativas adicionais de revisão independente foram despachadas e também repetidas através de Codex CLI, Claude Code e Gemini CLI; os revisores delegados não devolveram resultado dentro da execução e os três CLIs externos estavam indisponíveis por autenticação/configuração. Nenhum desses bloqueios alterou o candidato.

A revalidação externa read-only confirmou que o PR `#1` ainda aponta para o commit remoto anterior, está draft e sem checks/reviews; não existem GitHub environments nem DNS para os três nomes de staging inspecionados. Produção continua no build pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`, com `/up=200`, dashboard legado em `/=200` e novas páginas/APIs em `404`. O host live mantém 87% de disco usado, 3,1 GiB livres, PostgreSQL 17 sem base CRM/leads e swap sob pressão, portanto não oferece o isolamento, PostgreSQL 16, capacidade de restore e rollback exigidos pelo plano.

A telemetria exata do proxy nas últimas 48 horas encontrou um pedido `2xx` em cada um dos seis contratos v0 acompanhados. Consequentemente, a Tarefa 19 permanece bloqueada pelos gates de consumidores ativos, dois releases estáveis, export e aceitação. Continuam também em falta staging, mapping live de identidade/workspace/papel, backup automático e restore do arquivo real, resolução e validação humana dos conflitos shadow, decisões oficiais de `Won`/retenção/scopes, smoke externo, soak e cutover. Não houve merge, deploy, migração/backfill live, ativação outbound, retirada do legado ou criação do sentinel.

---

## Retoma autónoma em 2026-07-18T13:41:21Z

A retoma começou no `HEAD` limpo e sincronizado `5be7dec8426d06fcb0c66afae517d9130daa8125`. O plano canónico, este documento, o histórico, a suite, as migrations, o PR, o host live e os processos foram reinspecionados antes de agir. Não existia trabalho staged, unstaged ou untracked. Não existiam processos locais de worker CRM, reconciler ou outbox publisher. Os containers descartáveis preexistentes foram tratados como trabalho desconhecido e não foram alterados.

### Gates locais repetidos

Num PostgreSQL 16 descartável novo em `127.0.0.1:55453`, explicitamente marcado para testes e sem dados ou credenciais live:

```text
Suite segura completa com DeprecationWarning como erro: 950 passed, 1 skipped em 109.68s, exit 0
Alembic lifecycle: 0007 -> 0006 -> 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected, exit 0
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, git diff --check e Gitleaks: passed; 0 leaks em 48 commits
```

A imagem candidata foi reconstruída localmente com manifest list `sha256:9f021cd979906d7ad6e8d380718edd60f0f28383474f812ce4ae0c361af56999`. O smoke com os defaults seguros confirmou `/up=200`, dashboard e páginas ricas em `403`, e Agent ingress desativado em `404`. O container PostgreSQL, o container de smoke, a imagem local e o backup temporário criados nesta retoma foram removidos; as portas `55453` e `58004` ficaram livres.

### Gates externos e limite seguro

O PR `#1` continua draft, mergeable, sem reviews, checks ou environments GitHub. Não existe DNS para os três nomes de staging inspecionados. Produção continua saudável no build pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up=200`, dashboard legado em `/=200` e novas páginas/APIs em `404`.

O host live continua sem base CRM identificável e sem configuração CRM/`DATABASE_URL` no container atual. O filesystem está a 87%, com 3,1 GiB livres; a máquina tem 3,8 GiB de RAM, cerca de 1,6 GiB disponível e 1,5 GiB de swap em uso. Não existem credenciais ou CLIs locais para provisionar um staging isolado noutro fornecedor. Improvisar PostgreSQL, staging e backup no host live partilhado violaria os gates de capacidade, isolamento, restore e rollback do plano.

A telemetria agregada do `kamal-proxy` nas últimas 48 horas voltou a encontrar um pedido `2xx` em cada contrato v0 acompanhado: `/api/stats`, `/api/portfolio`, `/api/recommendations`, `/api/outreach-followups`, `/api/email-followups` e `/api/proposal-followups`. A Tarefa 19 não pode remover esses contratos sem quebrar consumidores observados e sem a evidência de dois releases estáveis, export e aceitação exigida pelo plano.

Continuam ausentes artefactos que a execução local não pode fabricar: staging isolado, mapping live de principal/papel/workspace, decisão oficial e evidência para `Won`, política de retenção e scopes, PostgreSQL CRM com backup automático e restore do arquivo real, resolução/aceitação dos conflitos shadow, validação da amostra pelo owner comercial, smoke browser/security no ambiente final, soak, cutover e dois releases estáveis sem consumidores v0. A autorização autónoma remove pausas de aprovação, mas não satisfaz estes gates técnicos, de dados e de release. Não houve merge, deploy, migração/backfill live, ativação de workers/conectores/outbox, retirada do legado ou criação de `.hermes/crm-revamp-complete.json`.
