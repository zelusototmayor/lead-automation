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

---

## Retoma autónoma em 2026-07-18T14:19:18Z

A retoma começou no `HEAD` limpo e sincronizado `cd1d49d2fc11f5c4fad4a4f994ae18b2571fe2dd`. O plano canónico, este documento, commits, PR, processos locais, containers, configuração de deploy e host live foram reinspecionados antes de qualquer alteração. Não existiam workers CRM, reconciler, outbox publisher ou jobs outbound ativos. Containers PostgreSQL preexistentes foram tratados como trabalho desconhecido e não foram alterados.

### Gates locais e shadow real

Num PostgreSQL 16 descartável novo em `127.0.0.1:55454`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 950 passed, 1 skipped em 105.43s, exit 0
Alembic lifecycle: 0007 -> 0006 -> 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup vazio restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Backup do shadow restaurado: schema=0007, 15 tabelas, 1 workspace, 0 violações
Ruff lint no delta, compileall, git diff --check e Gitleaks: passed; 0 leaks em 49 commits
Imagem local construída: manifest list sha256:cd6db0daed9fe0c29d833ce5c3d60fc47ac24c97b84bb3d0c44bc907fd899db0
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress=404
Smoke autenticado com PostgreSQL: 401 sem credenciais; páginas e APIs ricas=200; Agent ingress=404
```

Uma snapshot temporária `0600` da Sheet real, capturada via scope read-only e removida no fim, confirmou o estado atual:

```text
Snapshot: 1.247 input rows, 1.202 aplicáveis, 12 identidades duplicadas, 21 linhas sem identidade segura
Accounts apply #1: 65 imports, 46 accounts criadas/associadas, 52 conflitos
Accounts apply #2: 0 imports, 65 replay no-op
Proposals apply #1: 44 imports, 4 unmatched accounts, 48 missing value/evidence
Proposals apply #2: 0 imports, 44 replay no-op
Compare: parity=false, 1 lead/account em falta, 0 stage/account/source-field mismatches
Invariantes: 0 leads rank>=40 sem account, 0 zeros sintéticos missing, 0 eventos failed/dead-letter
```

A primeira tentativa de apply com um UUID que ainda não existia na tabela `workspaces` falhou fechada por FK e não persistiu registos. `MIGRATION.md` passou a documentar explicitamente esta pré-condição antes do backfill; o apply válido foi repetido apenas depois de criar a workspace descartável.

### Gates externos e decisão de não cutover

O PR `#1` permanece draft, mergeable, sem reviews/checks e sem GitHub environments. A tentativa de obter scope OAuth `workflow` exigiu login interativo indisponível, pelo que o workflow preservado fora do repositório não foi aplicado nem foi deixado trabalho local não publicável.

Produção continua saudável na imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`. O host tem filesystem a 87%, 3,1 GiB livres, 3,8 GiB de RAM, swap sob pressão e nenhuma base CRM identificável. Não há credenciais ou CLI para provisionar staging isolado noutro fornecedor. Improvisar staging, PostgreSQL CRM e backup no host partilhado violaria os gates de capacidade, isolamento, restore e rollback.

A telemetria estruturada do `kamal-proxy` nas últimas 48 horas contém exatamente um pedido `2xx` para cada contrato v0 acompanhado: `/api/stats`, `/api/portfolio`, `/api/recommendations`, `/api/outreach-followups`, `/api/email-followups` e `/api/proposal-followups`. A Tarefa 19 não pode retirar esses contratos sem quebrar consumidores observados e sem dois releases estáveis, export e aceitação.

Continuam materialmente ausentes staging isolado, mapping live de principal/papel/workspace, decisão oficial de `Won`, política de retenção/scopes, PostgreSQL CRM com backup automático e restore do arquivo real, resolução/aceitação dos conflitos shadow, validação da amostra pelo owner, browser/security smoke no ambiente final, soak e cutover. Estes gates explícitos impedem merge, deploy, migração live, ativação de workers/conectores/outbox, retirada do legado e criação do sentinel; não são substituídos pela autorização autónoma.

---

## Retoma autónoma em 2026-07-18T15:18:23Z

A retoma começou no `HEAD` limpo e sincronizado `4072815acea77a047522b8cf75bd53f36bb61dd1`. Foram reinspecionados o plano canónico, `CURRENT_STATE.md`, commits, PR, processos, containers, produção e configuração antes de qualquer mutação. Não existiam workers CRM, reconciler, outbox publisher ou jobs outbound do projeto ativos. Containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

### Candidato local e PostgreSQL descartável

Num PostgreSQL 16 novo em `127.0.0.1:55455`, explicitamente marcado para testes:

```text
Suite segura completa com DeprecationWarning como erro: 950 passed, 1 skipped em 105.46s, exit 0
Alembic lifecycle: 0007 -> 0006 -> 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected, exit 0
Backup vazio restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, git diff --check e Gitleaks: passed; 0 leaks em 50 commits
Imagem local construída: manifest list sha256:6bf3e1db478121b250ac506e73eb5f366cae9e41e598b0082db2177d854025d2
Smoke com defaults: /up=200; dashboard e rotas ricas=403; POST Agent ingress=404
Browser smoke autenticado local: 401 sem credenciais; páginas e APIs ricas=200; 0 erros de consola
```

Uma captura temporária `0600` da Sheet real foi repetida através do scope Google read-only. Credencial, snapshot e backups temporários foram removidos sem imprimir conteúdo. O apply foi feito apenas no PostgreSQL descartável e repetido com input idêntico:

```text
Snapshot: 1.247 input rows, 1.202 aplicáveis, 12 identidades duplicadas, 21 linhas sem identidade segura
Accounts apply #1: 65 imports, 46 accounts criadas/associadas, 52 conflitos
Accounts apply #2: 0 imports, 65 replay no-op
Proposals apply #1: 44 imports, 4 unmatched accounts, 48 missing value/evidence
Proposals apply #2: 0 imports, 44 replay no-op
Compare: parity=false, 1 lead/account em falta, 0 stage/account/source-field mismatches
Invariantes: 0 leads rank>=40 sem account, 0 zeros sintéticos missing, 0 eventos failed/dead-letter
Backup shadow restaurado: schema=0007, 15 tabelas, 1 workspace, 0 violações
```

A idempotência dos registos aplicáveis e as invariantes permanecem verdes, mas os conflitos e a falta de paridade continuam a bloquear o gate de dados. Não foram feitos merges automáticos por nome nem writes na Sheet.

### Revalidação externa

O PR `#1` continua draft, mergeable e sem reviews, checks ou environments GitHub. Não existe DNS para os três nomes de staging inspecionados. Produção continua no build pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up=200`, dashboard legado em `/=200` e novas páginas/APIs em `404`.

O host live não tem `DATABASE_URL`, flags CRM nem mapping de principal no container atual; não existe base ou backup CRM identificável. O filesystem está a 87%, com 3,1 GiB livres; a máquina tem 3,8 GiB de RAM e 1,4 GiB de swap em uso. Não existem CLIs ou credenciais locais para provisionar staging isolado noutro fornecedor. A telemetria JSON do proxy nas últimas 48 horas voltou a mostrar um pedido `2xx` em cada um dos seis contratos v0 acompanhados.

Continuam sem evidência os gates que não podem ser fabricados localmente: staging isolado, mapping live de principal/papel/workspace, decisão oficial de `Won`, política de retenção/scopes, PostgreSQL CRM com backup automático e restore do arquivo real, resolução/aceitação dos conflitos shadow, validação da amostra pelo owner, browser/security smoke no ambiente final, soak/cutover e dois releases estáveis sem consumidores v0. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, retirada do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma em 2026-07-18T15:47:43Z

A retoma começou no `HEAD` limpo e sincronizado `dc816c2d0bdb0f4f5d1dd28fefe29070ca497dac`. O plano canónico, `CURRENT_STATE.md`, commits, decisões, migrations, processos, PR, staging e produção foram reinspecionados antes de qualquer mutação. Não existiam workers CRM, reconciler, outbox publisher ou jobs outbound ativos. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

### Gates locais repetidos

Num PostgreSQL 16 descartável novo em `127.0.0.1:55456`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 950 passed, 1 skipped em 105.61s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected, exit 0
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, git diff --check e Gitleaks: passed; 0 leaks em 51 commits
Imagem local construída: manifest list sha256:f762389fc15abd8fc5a74ce6fb40df19fb74f23dfa7aef209f3441333074b01e
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress=404; 0 erros no log
Smoke HTTP autenticado: 401 sem credenciais; páginas e APIs ricas=200; Agent ingress=404
Browser smoke Playwright com HTTP credentials: Contas, Propostas, Inteligência e Operações=200; estados de erro ausentes; 0 erros de consola
```

Os containers, base, backup temporário e imagem criados nesta retoma foram removidos; as portas `55456`, `58005` e `58006` ficaram livres. O único skip continua a ser o teste que exige literalmente a porta documentada `55432`, ocupada por um container preexistente que não foi alterado.

Foi também criado um export read-only do tab `PT Logistics` fora do repositório, com permissões `0600`: 1.248 linhas, 43 colunas máximas, 502.197 bytes e SHA-256 `f3a92324fc8aa3a9e187e67f2eb8cc0ac1fb5e2dc2bf5d8b12278a89ea74f9e1`. O export está em `/Users/max/.hermes/profiles/marketing-max/backups/crm/pt-logistics-export-20260718T154500Z.json`; nenhum valor foi impresso. A tentativa de obter também o workbook XLSX nativo falhou fechada com `403 insufficientPermissions`: o OAuth disponível autorizou a leitura via Sheets API, mas não a operação Drive export. Nenhum ficheiro parcial foi preservado.

### Gates externos revalidados

O PR `#1` continua draft, mergeable e sem reviews, checks ou environments GitHub. Os três nomes de staging inspecionados continuam sem DNS. Produção permanece no build pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`, com `/up=200` e as novas páginas/APIs em `404`. O host mantém filesystem a 87%, 3,2 GiB livres, 3,8 GiB de RAM e nenhum PostgreSQL/backup CRM identificável.

A telemetria estruturada disponível do `kamal-proxy`, entre `2026-07-18T01:56:31Z` e `2026-07-18T15:38:24Z`, contém um pedido `200` para cada contrato v0 acompanhado: `/api/stats`, `/api/portfolio`, `/api/recommendations`, `/api/outreach-followups`, `/api/email-followups` e `/api/proposal-followups`. Remover estes contratos quebraria consumidores observados e violaria o gate da Tarefa 19.

Continuam materialmente fechados os gates de staging/cutover: mapping live de principal/papel/workspace, decisão oficial de `Won`, política de retenção/scopes, PostgreSQL CRM com backup automático e restore do arquivo real, resolução/aceitação dos conflitos shadow, validação da amostra pelo owner, browser/security smoke no ambiente final, soak e cutover. A Tarefa 19 exige ainda dois releases estáveis sem rollback, ausência de consumidores v0 e aceitação de stakeholders. Por isso não houve merge, deploy, migração/backfill live, ativação de workers/conectores/outbox, retirada do legado ou criação do sentinel.

---

## Retoma autónoma em 2026-07-18T16:14:11Z

A retoma preservou a alteração staged de `ROLLBACK.md` e as duas camadas staged/unstaged de `CURRENT_STATE.md`, verificou-as e publicou-as no commit atómico `f237a8ab4429178502938b4b68087c1559da277c` (`docs: record CRM export and rollback evidence`). A branch ficou sincronizada com `origin/feat/crm-accounts-proposals-v1` e o sentinel continuou ausente.

### Verificação local no candidato publicado

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55457`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 950 passed, 1 skipped em 110.03s, exit 0
Alembic lifecycle: base -> 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, git diff --check e Gitleaks: passed; 0 leaks em 52 commits
Imagem local: sha256:afa5cb07413f2f9a63789dfb1c09857aef4a85ca58afa9f99126fc9b328818c3
Smoke com PostgreSQL e principal de teste: /up=200; páginas e APIs ricas autenticadas=200; pedidos sem credenciais rejeitados; Agent ingress desativado=404
```

Os containers e a imagem criados por esta retoma foram removidos; as portas `55457` e `58007` ficaram livres. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido. Não existiam workers CRM, reconciler, outbox publisher ou jobs outbound ativos.

### Revalidação dos gates externos

Produção continua no build pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up=200`, dashboard legado em `/=200` e novas páginas/APIs em `404`. O PR `#1` permanece draft, mergeable e sem reviews, checks ou environments GitHub. Os três nomes de staging inspecionados continuam sem DNS.

O host live mantém filesystem a 87%, 3,2 GiB livres, 3,8 GiB de RAM, 1,4 GiB de swap em uso e nenhum PostgreSQL/backup CRM identificável. Não existem credenciais ou configuração local para provisionar um serviço PostgreSQL/staging isolado noutro fornecedor; usar o host partilhado sem margem de restore e isolamento violaria os gates de capacidade e rollback.

A telemetria estruturada do proxy, entre `2026-07-17T23:05:32Z` e `2026-07-18T16:11:52Z`, contém um pedido `200` em cada contrato v0 acompanhado. Além dos consumidores observados, continuam ausentes os artefactos que a execução não pode fabricar: decisão oficial e evidência de `Won`, política de retenção/scopes, resolução ou aceitação dos conflitos shadow, validação da amostra pelo owner comercial, staging final, backup automático e restore real, soak/cutover e dois releases estáveis com aceitação dos stakeholders. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, retirada do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma em 2026-07-18T17:18:32Z

A retoma começou no `HEAD` sincronizado `d91754b9e8dd967c40e3f7beb4fbb38fef020870` e preservou duas alterações staged já existentes. O candidato implementa o controlo mínimo de rate limiting exigido para Agent ingress: 60 pedidos autenticados por minuto, por principal/workspace e endpoint, antes de ler ou validar o payload, com sincronização entre threads, limpeza de buckets expirados, resposta genérica `429` e `Retry-After: 60`. O trabalho foi verificado e publicado no commit `e0da7bbf4eb1c5739c4addd1d7f1944bd8001e12` (`security: rate limit CRM agent ingestion`).

### Evidência local

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55458`, explicitamente marcado para testes e removido no fim:

```text
Agent event API focused: 15 passed
Suite segura completa com DeprecationWarning como erro: 951 passed, 1 skipped em 109.06s, exit 0
Rate limit concorrente: 60 pedidos aceites e 40 rejeitados em 100 chamadas paralelas
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, format check, compileall, git diff --check e Gitleaks: passed; 0 leaks em 53 commits e no candidato staged
Imagem local: sha256:3e93a5aa59d3c7ec124db42161e03702be86981f59a6272f14aff30da35214f1
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress desativado=404; 0 erros no log
```

O PostgreSQL, backup, container de smoke e imagem criados nesta retoma foram removidos. Não existiam processos locais de worker CRM, reconciler, outbox publisher ou jobs outbound. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

### Gates externos revalidados

O PR `#1` permanece draft, mergeable e sem reviews, checks ou environments GitHub. Os três nomes de staging inspecionados continuam sem DNS. Produção permanece saudável no build pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up=200`, dashboard legado em `/=200` e novas páginas/APIs em `404`. O host mantém filesystem a 87%, 3,2 GiB livres, 3,8 GiB de RAM, 1,3 GiB de swap em uso e nenhum PostgreSQL/backup CRM identificável.

A telemetria estruturada do proxy nas últimas 48 horas contém consumidores `2xx` ativos: `/api/stats` 2 pedidos, `/api/portfolio` 1, `/api/recommendations` 1, `/api/outreach-followups` 3, `/api/email-followups` 3 e `/api/proposal-followups` 3, entre `2026-07-18T09:09:06Z` e `2026-07-18T16:48:46Z`.

Continuam materialmente fechados os gates de staging/cutover e de dados: mapping live de principal/papel/workspace, decisão oficial de `Won`, política de retenção/scopes, PostgreSQL CRM com backup automático e restore do arquivo real, resolução ou aceitação dos conflitos shadow, validação da amostra pelo owner, browser/security smoke no ambiente final, soak e cutover. A Tarefa 19 exige ainda dois releases estáveis sem rollback, ausência de consumidores v0 e aceitação de stakeholders. Improvisar staging e PostgreSQL no host live partilhado violaria os gates de capacidade, isolamento, restore e rollback. Por isso não houve merge, deploy, migração/backfill live, ativação de workers/conectores/outbox, retirada do legado ou criação do sentinel.

---

## Retoma autónoma em 2026-07-18T17:41:43Z

A retoma começou no `HEAD` limpo e sincronizado `f6b9c1e0176dd4c11c96aed9107fdf205f4403bf`. O plano canónico, este documento, commits, suite, migrations, processos, PR, staging e produção foram reinspecionados antes de qualquer mutação. Não existiam workers CRM, reconciler, outbox publisher ou jobs outbound ativos. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

### Gates locais repetidos

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55459`, explicitamente marcado para testes e sem dados ou credenciais live:

```text
Suite segura completa com DeprecationWarning como erro: 951 passed, 1 skipped em 109.24s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Fixture de contas apply #1: 4 imports, 3 contas criadas/associadas, 1 conflito
Fixture idêntica apply #2: 0 imports, 4 replay no-op, 0 novos registos
Ruff no delta Python, compileall, git diff --check e Gitleaks: passed; 0 leaks em 55 commits
Imagem local: sha256:2d10579030a44aeccba8b8c7deee61ed6786f49240af8fd17b9fbdd37bfaf82c
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress=404
Smoke autenticado com PostgreSQL: pedidos sem credenciais=401; páginas e APIs ricas=200
```

O export read-only preservado fora do repositório foi verificado sem ler ou imprimir conteúdo: modo `0600`, 502.197 bytes e SHA-256 `f3a92324fc8aa3a9e187e67f2eb8cc0ac1fb5e2dc2bf5d8b12278a89ea74f9e1`. O PostgreSQL, containers de smoke e imagem criados nesta retoma foram removidos; as portas `55459`, `58008` e `58009` ficaram livres.

### Gates externos revalidados

O PR `#1` continua draft, mergeable e sem reviews, checks ou environments GitHub. Os três nomes de staging inspecionados continuam sem DNS. Produção permanece saudável no build pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up=200`, dashboard legado em `/=200` e novas páginas/APIs em `404`.

O host live mantém filesystem a 87%, 3,1 GiB livres, 3,8 GiB de RAM, 1,3 GiB de swap em uso e nenhuma base CRM identificável. O PostgreSQL instalado não tem timer de backup CRM observado. A telemetria estruturada do proxy nas últimas 48 horas continua a provar consumidores `2xx` ativos: `/api/stats` 2, `/api/portfolio` 1, `/api/recommendations` 1, `/api/outreach-followups` 3, `/api/email-followups` 3 e `/api/proposal-followups` 3.

Continuam ausentes os artefactos que a execução não pode fabricar sem violar os gates: staging isolado; mapping live de principal/papel/workspace; decisão oficial de `Won`; política de retenção/scopes; PostgreSQL CRM com backup automático e restore do arquivo real; resolução ou aceitação dos conflitos shadow; validação da amostra pelo owner; smoke browser/security no ambiente final; soak/cutover; e dois releases estáveis sem consumidores v0. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, retirada do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma e auth-before-database em 2026-07-18T19:22:38Z

A retoma começou em `99ee9a5637253548cecd6c334eb9e88e4d3950fa` e preservou integralmente duas alterações staged já existentes. O candidato corrigia uma ordem insegura de dependencies no Agent ingress: quando a rota estava ativa, FastAPI podia construir a engine/sessão antes de autenticar o pedido. O RED foi reproduzido anteriormente num worktree detached contra o código de `HEAD`; o teste falhou porque o pedido não autenticado alcançou a dependency da base. O candidato move autenticação, timestamp, scope de escrita e rate limit para uma dependency partilhada que precede a sessão. A cache de dependencies do FastAPI garante uma única autenticação/consumo do bucket por pedido; a regressão de rate limit existente continuou a provar 60 pedidos aceites e o 61.º rejeitado.

O candidato exato foi congelado com digest staged `3cc2021070b4396c17db6a41031d130928db4b8791d65ed3d887c2cf5a58cdbf`. Duas revisões independentes em processos Hermes separados devolveram `PASS` e `APPROVED`, sem findings críticos, importantes, de segurança ou lógica. Foi commitado atomicamente como `b23257e93aabef4cbd6834a04a50342c18cce652` (`security: authenticate agent ingress before database access`).

### Evidência local repetida

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55460`, explicitamente marcado para testes e sem dados ou credenciais live:

```text
Agent API + shadow/runtime focused: 42 passed
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 108.03s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta, format check, compileall, git diff checks e Gitleaks: passed; 0 leaks nos 56 commits existentes antes do commit de código
Imagem local: manifest list sha256:80fef1eecff45255a5e5feb109cfd27eba23e97b23ea65d15544d86a9bf28359
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress desativado=404; 0 erros no log
Smoke autenticado com PostgreSQL: pedidos sem credenciais=401; páginas e APIs ricas=200
Auth-before-database com destino PostgreSQL deliberadamente inalcançável: sem auth=401; scope insuficiente=403; 0 tentativas de conexão registadas
```

O export read-only preservado fora do repositório foi novamente verificado sem imprimir conteúdo: modo `0600`, 502.197 bytes e SHA-256 `f3a92324fc8aa3a9e187e67f2eb8cc0ac1fb5e2dc2bf5d8b12278a89ea74f9e1`. Não existiam workers CRM, reconciler, outbox publisher ou jobs outbound ativos durante testes/migrations.

### Gates externos revalidados

O PR `#1` continua draft, mergeable, sem reviews/checks e sem GitHub environments. Os três nomes de staging inspecionados continuam sem DNS. Produção permanece saudável no build pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up=200` e as novas páginas/APIs permanecem em `404`.

O host live mantém filesystem a 87%, 3,1 GiB livres, 3,8 GiB de RAM, 1,3 GiB de swap em uso, nenhuma base CRM identificável e nenhum timer de backup CRM observado. A telemetria estruturada do proxy nas últimas 48 horas continua a provar consumidores `2xx` ativos: `/api/stats` 2, `/api/portfolio` 1, `/api/recommendations` 1, `/api/outreach-followups` 3, `/api/email-followups` 3 e `/api/proposal-followups` 3, entre `2026-07-18T09:09:06Z` e `2026-07-18T16:48:46Z`.

A primeira tarefa formalmente incompleta continua a ser a Tarefa 19, mas retirar o legado agora quebraria consumidores observados e violaria os gates de dois releases estáveis, export e aceitação. Permanecem também fechados staging isolado, mapping live de principal/papel/workspace, decisão oficial de `Won`, política de retenção/scopes, PostgreSQL CRM com backup automático e restore real, resolução/aceitação dos conflitos shadow, validação da amostra pelo owner, browser/security smoke no ambiente final, soak e cutover. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, retirada do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma em 2026-07-18T19:41:35Z

A retoma começou no `HEAD` limpo e sincronizado `e8c6b2494967460bc6bdc7bf433f4a7950303e32`. O plano canónico, este documento, commits, suite, migrations, processos, PR, staging, produção e pré-requisitos de cloud foram reinspecionados antes de qualquer mutação. Não existiam workers CRM, reconciler, outbox publisher ou jobs outbound ativos. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

### Gates locais repetidos

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55461`, explicitamente marcado para testes, sem dados ou credenciais live e removido automaticamente no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 108.77s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, git diff checks e Gitleaks: passed; 0 leaks em 58 commits
Imagem local: sha256:632121f6a07f203cf40cde8de3f2862bae568790e89a65f5aca44ff88c9f70a3
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress=404; 0 erros no log
```

A primeira invocação do restore verifier usou nomes de argumentos incorretos, falhou antes do restore e executou o cleanup registado. A invocação documentada foi depois repetida num PostgreSQL exclusivo novo e passou. O PostgreSQL, dump, base de restore, container de smoke e imagem temporários foram removidos; as portas `55461` e `58010` ficaram livres.

O export read-only preservado fora do repositório foi verificado sem imprimir conteúdo: modo `0600`, 502.197 bytes e SHA-256 `f3a92324fc8aa3a9e187e67f2eb8cc0ac1fb5e2dc2bf5d8b12278a89ea74f9e1`.

### Gates externos revalidados

O PR `#1` continua draft, mergeable e sem reviews/checks; a API GitHub continua sem environments. Os três nomes de staging inspecionados continuam sem DNS. Não existem CLIs nem credenciais de cloud disponíveis localmente para provisionar um staging isolado noutro fornecedor.

Produção continua saudável na imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up=200`, dashboard legado em `/=200` e novas páginas/APIs em `404`. O host mantém filesystem a 87%, 3,2 GiB livres, 3,8 GiB de RAM, 1,2 GiB de swap em uso, nenhuma base CRM identificável e nenhum timer de backup CRM observado. A consulta estruturada atual às últimas 48 horas não encontrou registos dos seis endpoints v0 acompanhados, mas uma única janela vazia não satisfaz o gate de ausência de consumidores, porque não existem dois releases pós-cutover estáveis e as janelas imediatamente anteriores registaram consumidores `2xx` ativos.

A primeira tarefa formalmente incompleta permanece a Tarefa 19. Permanecem também fechados os gates anteriores de staging e cutover: mapping live de principal/papel/workspace, decisão oficial de `Won`, política de retenção/scopes, PostgreSQL CRM com backup automático e restore real, resolução/aceitação dos conflitos shadow, validação da amostra pelo owner, browser/security smoke no ambiente final e soak. A autorização autónoma não substitui esta evidência técnica, de dados e de release. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, retirada do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Ensaio de staging isolado em 2026-07-18T20:38:09Z

Foi provisionado um GitHub Codespace isolado e efémero, `crm-revamp-staging-20260718-696rwjr5w75jc4pv`, com 4 cores, 16 GiB de RAM e 32 GiB de storage, no branch limpo `feat/crm-accounts-proposals-v1` e no SHA exato `e151925e965ffa7ecf041605c3e239dc3837c437`. Não foi usado o host de produção para PostgreSQL ou staging. O ambiente não recebeu publishers de outbox, workers outbound, conectores live ou jobs comerciais.

O candidato foi construído com `python:3.11-slim`; PostgreSQL 16 foi executado numa network e volume exclusivos. Migrations aditivas aplicaram de `base` até `0007`. Uma base separada e descartável foi usada para a suite e para o lifecycle, sem tocar os dados shadow:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 122.41s
Alembic lifecycle descartável: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
compileall e git diff --check: passed
```

O export read-only preservado fora do repositório foi transferido para o ambiente com mode `0600` e checksum verificado, sem imprimir valores. A snapshot canónica e os dois applies idênticos produziram apenas métricas agregadas:

```text
Snapshot: 1.247 input rows, 1.202 aplicáveis, 12 identidades duplicadas, 21 linhas sem identidade segura
Accounts apply #1: 65 imports, 46 contas criadas/associadas, 52 conflitos
Accounts apply #2: 0 imports, 65 replay no-op
Proposals apply #1: 44 imports, 4 unmatched accounts, 48 missing value/evidence
Proposals apply #2: 0 imports, 44 replay no-op
Compare: parity=false, 1 lead/account em falta, 0 stage/account/source-field mismatches
Invariantes: 0 leads rank>=40 sem account, 0 zeros sintéticos missing, 0 eventos failed/dead-letter
```

Um dump custom-format do shadow, com 196.034 bytes e SHA-256 `0dd6744d6a5a64eaebb603970ada1069569a195f8dd0e3cac406c36b38e2fb84`, foi restaurado num PostgreSQL 16 descartável e validado: schema `0007`, 15 tabelas, 1 workspace e 0 violações. Um sidecar isolado ficou configurado para dumps automáticos de staging a cada 30 minutos enquanto o Codespace estiver ativo; isto não substitui backup automático de produção.

O smoke HTTP confirmou `/up=200`, `401` sem credenciais nas rotas ricas, `200` autenticado para Contas, Propostas, Inteligência e Operações e `404` para Agent ingress desativado. Um browser smoke Playwright através de tunnel autenticado carregou as quatro áreas com `200`, zero erros de consola e zero responses falhadas. Um soak técnico adicional fez 16 ciclos em quatro minutos sobre health, Contas e Propostas: zero falhas, zero restarts e zero erros de aplicação.

Este ensaio fecha a ausência de um ambiente externo isolado para validação técnica, mas não abre o cutover. A paridade real continua falsa por conflitos que não podem ser auto-fundidos; falta validação/aceitação da amostra pelo owner, decisões oficiais de `Won` e retenção/scopes, mapping final de principal/papel/workspace, TLS/proxy do ambiente final, PostgreSQL e backup automático de produção, soak de produção e dois releases estáveis.

A revalidação read-only em `2026-07-18T21:37:57Z` confirmou que o Codespace efémero continuava disponível no SHA exato e com a worktree limpa, mas que todos os containers, bases, backups e processos do ensaio já tinham sido removidos. Produção permanecia no container saudável `7622a2b2b8d5e0790858208b2c3a1f119edb7328`, sem base CRM nem timer de backup CRM. A telemetria JSON estruturada disponível entre `2026-07-18T19:16:31Z` e `2026-07-18T21:13:55Z` continha pedidos `2xx` ativos em `/api/stats` (1), `/api/portfolio` (1) e `/api/recommendations` (1). A ausência de tráfego nos outros três endpoints durante esta janela não satisfaz ausência de consumidores nem substitui os dois releases pós-cutover. O gate da Tarefa 19 continua fechado.

Não houve merge, deploy de produção, migração live, cutover, remoção do legado ou criação do sentinel.

---

## Retoma autónoma e revalidação final em 2026-07-18T21:47:56Z

A retoma preservou o candidato staged encontrado em `e151925e965ffa7ecf041605c3e239dc3837c437`, corrigiu apenas a descrição factual do estado do Codespace, congelou o diff no digest `488f04ac051a59e1e9bfa1a26e518753f3e02896386ee03204da078867e5d9b5` e publicou-o no commit `e7463d76d83457894a32eb7582fcc6c5cc35306c` (`docs: record isolated CRM staging rehearsal`).

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55462`, sem dados ou credenciais live:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 110.12s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff checks, scan estático de linhas adicionadas e Gitleaks: passed
Imagem local: sha256:c19a9a9a51804195b46a1fa0ec527287677b7b6ccb4842b08a796d3bf3bac6c3
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress=404; 0 erros no log
Smoke autenticado com PostgreSQL: rotas ricas sem credenciais=401; páginas e APIs de Contas, Propostas, Inteligência e Operações=200
```

O PostgreSQL, containers de smoke, dump e imagem criados nesta retoma foram removidos; as portas `55462`, `58011` e `58012` ficaram livres. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido. O Codespace de staging foi parado, sem ser apagado, depois de confirmar worktree limpa e ausência de containers, bases, backups ou processos do ensaio.

A descoberta live permaneceu read-only. Produção continuava saudável na imagem `7622a2b2b8d5e0790858208b2c3a1f119edb7328`, com `/up=200`, dashboard legado em `/=200` e novas páginas/APIs em `404`. O host mantinha filesystem a 87%, 3,2 GiB livres, 3,8 GiB de RAM, 1,2 GiB de swap em uso, nenhuma base CRM identificável e nenhum timer de backup CRM. Nenhum worker CRM, reconciler, outbox publisher ou job outbound estava ativo.

A telemetria estruturada das últimas 48 horas continha 2.248 registos parseáveis e pedidos `2xx` ativos em `/api/stats` (1), `/api/portfolio` (1) e `/api/recommendations` (1), todos entre `2026-07-18T20:35:23Z` e `2026-07-18T21:12:54Z`. Portanto, retirar v0 agora quebraria consumidores observados e violaria o gate da Tarefa 19.

O PR `#1` continua draft, sem checks ou reviews configurados. O cutover permanece materialmente bloqueado por paridade real falsa, conflitos de identidade/account não resolvidos ou aceites, ausência de validação da amostra pelo owner comercial, decisão oficial de `Won`, política de retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção com backup automático e restore do arquivo real, smoke no proxy/TLS final e soak de produção. A Tarefa 19 exige ainda dois releases estáveis sem rollback, ausência de consumidores v0 e aceitação dos stakeholders.

Estes são gates explícitos de dados, operação e release, não pausas de aprovação que a autorização autónoma possa remover. Por isso não houve merge, deploy de produção, migração/backfill live, ativação de workers/conectores/outbox, cutover, retirada do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma em 2026-07-18T22:42:37Z

A retoma começou no `HEAD` limpo e sincronizado `b121def625e91644dd8d308d906677cde0cee308`. O plano canónico, este documento, commits, suite, migrations, processos, containers, PR, Codespace de staging e produção foram reinspecionados antes de qualquer mutação. Não existiam workers CRM, reconciler, outbox publisher ou jobs outbound ativos. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55463`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 110.52s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff checks e Gitleaks: passed; 0 leaks em 61 commits
```

O container e dump criados nesta retoma foram removidos e a porta `55463` ficou livre. O sentinel continuava ausente. O PR `#1` permanecia draft, mergeable, no SHA exato da branch, sem reviews, checks ou environments GitHub. O Codespace isolado permanecia parado, com branch sincronizada e worktree limpa. Os três nomes de staging inspecionados continuavam sem DNS.

A descoberta de produção permaneceu read-only. `/up` devolveu `200`, o dashboard legado `200` e as novas páginas/APIs `404`. O container saudável continuava na imagem `7622a2b2b8d5e0790858208b2c3a1f119edb7328`. O host mantinha filesystem a 87%, 3,2 GiB livres, 3,8 GiB de RAM, 1,3 GiB de swap em uso, nenhuma base CRM identificável e nenhum backup/timer CRM observável. A telemetria estruturada das últimas 48 horas confirmou consumidores `2xx` ativos em `/api/stats`, `/api/portfolio` e `/api/recommendations`, um pedido em cada rota.

A primeira tarefa formalmente incompleta continua a ser a Tarefa 19. Retirar v0 quebraria consumidores observados e violaria os gates de dois releases estáveis, export e aceitação. O cutover continua bloqueado por paridade real falsa, conflitos de identidade/account não resolvidos ou aceites, ausência de validação da amostra pelo owner comercial, decisão oficial de `Won`, política de retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção com backup automático e restore do arquivo real, smoke no proxy/TLS final e soak de produção. Não houve merge, deploy de produção, migração/backfill live, ativação de workers/conectores/outbox, cutover, retirada do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma em 2026-07-18T23:11:50Z

A retoma começou no `HEAD` limpo e sincronizado `d6265a063713fa2e80a2ccf9c23779e816fb202d`. O plano canónico, este documento, branch, commits, suite, migrations, PR, Codespace, DNS, processos, containers e produção foram reinspecionados antes de qualquer mutação. Não existia trabalho staged, unstaged ou untracked. Não existiam workers CRM, reconciler, outbox publisher ou jobs outbound ativos. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55464`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 109.07s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff check e Gitleaks: passed; 0 leaks em 62 commits
```

O container e dump criados nesta retoma foram removidos e a porta `55464` ficou livre. O PR `#1` permanece draft, mergeable, no SHA exato da branch, sem reviews, checks ou environments GitHub. O Codespace isolado permanece parado, sincronizado e limpo. Desde o SHA do ensaio de staging `e151925`, apenas `CURRENT_STATE.md` e `MIGRATION.md` mudaram; não existe delta de código. Os três nomes de staging inspecionados continuam sem DNS. Não existem CLIs, variáveis de credencial ou configuração local observáveis para provisionar um PostgreSQL/staging isolado noutro fornecedor.

A descoberta de produção permaneceu read-only. `/up` e o dashboard legado devolveram `200`; as novas páginas/APIs devolveram `404`. O container saudável continua na imagem `7622a2b2b8d5e0790858208b2c3a1f119edb7328`. O host tem filesystem a 87%, 3,2 GiB livres, 3,8 GiB de RAM, 1,2 GiB de swap em uso, zero bases com nome CRM/leads, zero containers CRM e zero timers de backup CRM. Em 2.879 registos JSON parseáveis das últimas 48 horas, a telemetria confirmou um pedido `2xx` em cada uma de `/api/stats`, `/api/portfolio` e `/api/recommendations`.

O código, migrations, rollback descartável e ensaio técnico de staging permanecem verdes, mas os gates de dados, produção e retirada do legado continuam materialmente fechados. A paridade real continua falsa; conflitos de identidade/account não foram resolvidos nem aceites; não existe validação da amostra pelo owner comercial, decisão oficial de `Won`, política de retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção com backup automático e restore do arquivo real, smoke no proxy/TLS final ou soak de produção. A Tarefa 19 continua bloqueada por consumidores v0 observados, ausência de dois releases pós-cutover e falta de aceitação dos stakeholders. Improvisar PostgreSQL e backup no host partilhado sob pressão de disco/swap violaria os gates de capacidade, isolamento, restore e rollback. Por isso não houve merge, deploy, migração/backfill live, ativação de workers/conectores/outbox, cutover, retirada do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma em 2026-07-18T23:40:34Z

A retoma partiu do `HEAD` limpo e sincronizado `3e9c7e9a95e56e69d91a020c6fef07070913a8b4`. O plano canónico, este documento, branch, commits, suite, migrations, processos, containers, PR, Codespace e produção foram reinspecionados. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55465`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 106.95s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Ruff no delta Python, compileall, diff check e Gitleaks: passed; 0 leaks em 63 commits
```

O container foi removido e a porta `55465` ficou livre. Produção respondeu `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência e as APIs v1 continuaram `404`, coerentes com a imagem pré-revamp. O PR `#1` permaneceu draft, mergeable, no SHA exato da branch, sem reviews ou checks. O Codespace isolado permaneceu parado, sincronizado e limpo. Não houve alteração de código desde o ensaio técnico de staging.

A primeira tarefa formalmente incompleta continua a ser a Tarefa 19, mas os gates anteriores de cutover também permanecem fechados. A paridade real é falsa; conflitos de identidade/account continuam sem resolução ou aceitação; faltam validação da amostra pelo owner, decisão oficial de `Won`, política de retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção com backup automático e restore real, smoke no proxy/TLS final e soak de produção. A retirada do legado exige ainda dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. Estes artefactos não podem ser fabricados por testes locais nem substituídos pela autorização autónoma. Não houve merge, deploy, migração live, cutover, retirada do legado ou criação do sentinel.

---

## Retoma autónoma em 2026-07-19T01:16:36+01:00

A retoma começou no `HEAD` limpo e sincronizado `2753c708579e156d342959f48de97bf917311a50`. O plano canónico, este documento, commits, suite, migrations, processos, containers, PR, staging e produção foram reinspecionados antes de qualquer mutação. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em loopback, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 108.57s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff lint no delta Python, compileall, git diff --check e Gitleaks: passed; 0 leaks em 64 commits
Imagem local: sha256:55d2f5ca918d19e3d1cf4d178ee0616033bdcb9667473f41aaded06a0417059e
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress=404; 0 erros no log
```

O format check global do delta continua a identificar seis ficheiros legados/preexistentes que seriam reformatados; não existe delta de código desde o ensaio de staging `e151925`, apenas alterações de documentação em `CURRENT_STATE.md` e `MIGRATION.md`. Esses ficheiros não foram reformatados para evitar scope creep.

Uma captura read-only atual da Sheet real, guardada apenas num ficheiro temporário `0600` e removida no fim, foi aplicada duas vezes num segundo PostgreSQL 16 descartável:

```text
Snapshot: 1.247 input rows, 1.202 aplicáveis, 12 identidades duplicadas, 21 linhas sem identidade segura
Accounts apply #1: 65 imports, 46 contas criadas/associadas, 52 conflitos
Accounts apply #2: 0 imports, 65 replay no-op
Review: 12 duplicate_stable_id, 21 missing_stable_id, 18 history_required, 1 identity_conflict
Proposals apply #1: 44 imports, 4 unmatched accounts, 48 missing value/evidence
Proposals apply #2: 0 imports, 44 replay no-op
Compare: parity=false, 1 lead/account em falta, 0 stage/account/source-field mismatches
```

A comparação terminou deliberadamente com exit `1` porque a paridade permanece falsa; isto é um gate de dados falhado, não uma falha do comando. Nenhum conflito foi auto-fundido, nenhum valor foi impresso, não houve write na Sheet e a credencial temporária, snapshot, bases e containers criados nesta retoma foram removidos.

A descoberta externa permaneceu read-only. O PR `#1` continua draft, mergeable, no SHA exato da branch e sem checks. O Codespace de staging técnico está parado; não existe DNS de staging. O browser local não tinha sessão DigitalOcean e não existem `doctl`, `flyctl`, `railway` ou credenciais cloud observáveis para provisionar um PostgreSQL gerido isolado. O host live continua no build pré-revamp, com PostgreSQL 17 partilhado mas sem base/backup CRM, filesystem a 87%, cerca de 3,2 GB livres e pressão de swap. Usar esse serviço partilhado ou improvisar backups no único host violaria os gates de PostgreSQL 16, isolamento, capacidade, restore e rollback.

A telemetria estruturada do `kamal-proxy` nas últimas 48 horas continha 3.504 registos JSON parseáveis e consumidores `2xx` ativos entre `2026-07-18T20:35:23Z` e `2026-07-19T00:05:47Z`: `/api/stats` 3, `/api/portfolio` 1, `/api/recommendations` 1, `/api/outreach-followups` 4, `/api/email-followups` 4 e `/api/proposal-followups` 4. Retirar ou proteger estes contratos sem migrar os consumidores quebraria tráfego observado.

Os blockers materiais mantêm-se: paridade real falsa; conflitos sem resolução/aceitação; validação da amostra pelo owner; decisão oficial de `Won`; política de retenção/scopes; mapping final de principal/papel/workspace; PostgreSQL de produção isolado com backup automático e restore do arquivo real; smoke no proxy/TLS final; soak e cutover. A Tarefa 19 exige adicionalmente dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. A autorização autónoma não substitui esta evidência técnica, humana e temporal. Por isso não houve merge, deploy, migração live, ativação de workers/conectores/outbox, cutover, retirada do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma em 2026-07-19T00:41:12Z

A retoma começou no `HEAD` limpo e sincronizado `da17b8ec7f89ecd963cba26e8e0fea915c534b1f`. O plano canónico, este documento, commits, suite, migrations, processos, containers, PR, staging e produção foram reinspecionados antes de qualquer mutação. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55466`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 106.83s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, git diff check e Gitleaks: passed; 0 leaks em 65 commits
```

A primeira invocação agregada dos checks estáticos passou incorretamente a lista de ficheiros newline-delimited ao Ruff como um único caminho e terminou com `E902 File name too long`; o mesmo check foi repetido com `git -z | xargs -0` e passou. Isto foi um erro do comando de verificação, não um finding de código. O container, dump e base de restore criados nesta retoma foram removidos e a porta `55466` ficou livre.

A descoberta externa permaneceu read-only. Produção responde `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência e APIs v1 continuam `404`, coerentes com a imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`. O host mantém filesystem a 87%, 3,2 GiB livres, 3,8 GiB de RAM, 1,2 GiB de swap em uso, zero bases com nome CRM/leads e zero timers de backup CRM observáveis. O PR `#1` permanece draft, mergeable, no SHA exato da branch, sem reviews, checks ou environments GitHub; o Codespace técnico permanece parado.

Em 3.913 registos JSON parseáveis do `kamal-proxy` nas últimas 48 horas, foram observados pedidos `2xx` ativos entre `2026-07-18T19:16:31Z` e `2026-07-19T00:40:30Z`: `/api/stats` 3, `/api/portfolio` 1, `/api/recommendations` 1, `/api/outreach-followups` 4, `/api/email-followups` 4 e `/api/proposal-followups` 4. Retirar ou proteger esses contratos sem migrar os consumidores quebraria tráfego observado.

A primeira tarefa formalmente incompleta continua a ser a Tarefa 19, mas o seu gate permanece fechado por consumidores v0 ativos, ausência de dois releases pós-cutover e falta de aceitação dos stakeholders. Os gates anteriores de cutover também permanecem fechados por paridade real falsa, conflitos sem resolução/aceitação, ausência de validação da amostra pelo owner, decisão oficial de `Won`, política de retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção isolado com backup automático e restore real, smoke no proxy/TLS final e soak de produção. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, cutover, retirada do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma em 2026-07-19T01:18:22Z

A retoma começou no `HEAD` limpo e sincronizado `1b60e1e9a34e69af1cce19b57a79581f0295a942`. O plano canónico, este documento, branch, commits, suite, migrations, processos, containers, PR, Codespace, Sheet real e produção foram reinspecionados antes de qualquer mutação. Não existia trabalho staged, unstaged ou untracked. Não existiam workers CRM, reconciler, outbox publisher ou jobs outbound ativos. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55467`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 109.84s, return code explícito 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff checks e Gitleaks: passed; 0 leaks em 66 commits
```

Uma captura read-only atual da Sheet real foi guardada apenas em ficheiros temporários `0600`, aplicada duas vezes num segundo PostgreSQL 16 descartável em `127.0.0.1:55468` e removida com a credencial temporária no fim:

```text
Snapshot: 1.247 input rows, 1.202 aplicáveis, 12 identidades duplicadas, 21 linhas sem identidade segura
Accounts apply #1: 65 imports, 46 contas criadas/associadas, 52 conflitos
Accounts apply #2: 0 imports, 65 replay no-op
Review: 12 duplicate_stable_id, 21 missing_stable_id, 18 history_required, 1 identity_conflict
Proposals apply #1: 44 imports, 4 unmatched accounts, 48 missing value/evidence
Proposals apply #2: 0 imports, 44 replay no-op
Compare: parity=false, 1 lead/account em falta, 0 stage/account/source-field mismatches
Invariantes materiais: 0 leads rank>=40 sem account, 0 valores missing com montante sintético, 0 eventos failed/dead-letter
```

A comparação terminou deliberadamente com exit `1` porque a paridade permanece falsa. Nenhum conflito foi auto-fundido, nenhum valor comercial foi impresso, não houve write na Sheet e os dois containers, dumps, snapshots e credenciais temporários criados nesta retoma foram removidos. As portas `55467` e `55468` ficaram livres.

A descoberta externa permaneceu read-only. O PR `#1` continua draft, mergeable e sem checks/reviews/environments. O Codespace técnico está parado, limpo e sincronizado; desde o ensaio de staging não existe delta de código. Os três nomes de staging inspecionados continuam sem DNS. Produção responde `/up=200`, dashboard legado `200` e novas páginas/APIs `404`, coerente com a imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`. O host mantém filesystem a 87%, 3,2 GiB livres, 3,8 GiB de RAM, 1,2 GiB de swap em uso, zero bases CRM/leads e zero timers de backup CRM observáveis.

Em 4.488 registos JSON parseáveis do `kamal-proxy` nas últimas 48 horas, foram observados pedidos `2xx` ativos: `/api/stats` 3, `/api/portfolio` 1, `/api/recommendations` 1, `/api/outreach-followups` 4, `/api/email-followups` 4 e `/api/proposal-followups` 4. Retirar ou proteger estes contratos sem migrar os consumidores quebraria tráfego observado.

A primeira tarefa formalmente incompleta continua a ser a Tarefa 19 e os gates anteriores de cutover permanecem materialmente fechados. Persistem paridade real falsa, conflitos sem resolução/aceitação, falta de validação da amostra pelo owner, decisão oficial de `Won`, política de retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção isolado com backup automático e restore real, smoke no proxy/TLS final e soak de produção. A retirada do legado exige ainda dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. Por isso não houve merge, deploy, migração live, ativação de workers/conectores/outbox, cutover, retirada do legado ou criação do sentinel.

---

## Retoma autónoma em 2026-07-19T03:13:33Z

A retoma começou no `HEAD` limpo e sincronizado `a08a91bc60a1b407ec833f92634b9f12d8caffc6`. O plano canónico, `CURRENT_STATE.md`, commits, suite, migrations, PR, Codespace, DNS, processos, containers e produção foram reinspecionados antes de qualquer mutação. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55469`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 111.00s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, git diff check e Gitleaks: passed; 0 leaks em 67 commits
```

A primeira invocação de Ruff colocou `-z` depois do pathspec e passou a lista newline-delimited como um único caminho, terminando com `E902 File name too long`. O mesmo check foi repetido com `git diff --name-only -z ... | xargs -0` e passou. Isto foi um erro do comando de verificação, não um finding de código. O container e dump criados nesta retoma foram removidos e a porta `55469` ficou livre.

A descoberta externa permaneceu read-only. O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews, checks ou environments GitHub. O Codespace técnico está parado, limpo e sincronizado; os três nomes de staging inspecionados continuam sem DNS. Produção responde `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência e APIs v1 continuam `404`, coerentes com a imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`.

O host live mantém o filesystem a 87%, 3,2 GiB livres, 3,8 GiB de RAM, 1,3 GiB de swap em uso, zero bases com nome CRM/leads e zero timers de backup CRM observáveis. A telemetria estruturada das últimas 48 horas contém pedidos ativos nos seis contratos v0 acompanhados: `/api/stats` 3, `/api/portfolio` 1, `/api/recommendations` 1, `/api/outreach-followups` 4, `/api/email-followups` 4 e `/api/proposal-followups` 4.

A implementação até à Tarefa 18 continua verde, mas a conclusão global não é tecnicamente possível no estado real atual. A paridade real continua falsa; conflitos de identidade/account não foram resolvidos nem aceites; faltam validação da amostra pelo owner, decisão oficial de `Won`, política de retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção isolado com backup automático e restore do arquivo real, smoke no proxy/TLS final e soak de produção. A Tarefa 19 exige adicionalmente dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. Fazer merge, deploy, migração live, cutover, retirar o legado ou criar `.hermes/crm-revamp-complete.json` agora violaria gates explícitos do plano; nenhuma dessas ações foi executada.

---

## Retoma autónoma em 2026-07-19T03:46:50Z

A retoma partiu do `HEAD` limpo e sincronizado `485b1318e6a760d084f5ac9a8bd605ae20d6d9fb`. O plano canónico, `CURRENT_STATE.md`, commits, suite, migrations, processos, containers, PR, Codespace, DNS e produção foram reinspecionados antes de qualquer mutação. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55470`, explicitamente marcado para testes:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 110.22s, exit 0
Alembic lifecycle serializado: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected, exit 0
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, git diff check e Gitleaks: passed; 0 leaks em 68 commits
Imagem local: sha256:2ee4e142e7c278bfe27ae2ed03ddcf02ce6baf60232a61044b50138f2050a42f
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress=404; 0 erros no log
```

O PostgreSQL, dump, container de smoke e imagem criados nesta retoma foram removidos; as portas `55470` e `58013` ficaram livres. O único skip exige literalmente o URL descartável documentado na porta `55432`, ocupada por trabalho preexistente que não foi alterado.

A descoberta externa permaneceu read-only. O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews, checks ou environments GitHub. O Codespace técnico está parado, limpo e sincronizado; os três nomes de staging inspecionados continuam sem DNS. Produção responde `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência, Operações e APIs v1 continuam `404`, coerentes com a imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`.

O host live mantém filesystem a 87%, 3,2 GiB livres, 3,8 GiB de RAM, 1,3 GiB de swap em uso, zero bases CRM/leads, zero containers CRM e zero timers de backup CRM observáveis. Em 6.491 registos JSON parseáveis das últimas 48 horas, foram observados pedidos `2xx` ativos nos seis contratos v0 acompanhados: `/api/stats` 3, `/api/portfolio` 1, `/api/recommendations` 1, `/api/outreach-followups` 4, `/api/email-followups` 4 e `/api/proposal-followups` 4.

A primeira tarefa formalmente incompleta continua a ser a Tarefa 19, mas o seu gate permanece fechado por consumidores v0 ativos, ausência de dois releases pós-cutover e falta de aceitação dos stakeholders. Os gates anteriores de cutover também permanecem fechados por paridade real falsa, conflitos sem resolução/aceitação, ausência de validação da amostra pelo owner, decisão oficial de `Won`, política de retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção isolado com backup automático e restore real, smoke no proxy/TLS final e soak de produção. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, cutover, retirada do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma em 2026-07-19T04:38:45Z

A retoma começou no `HEAD` limpo e sincronizado `b8f8aa049f2790d5914cf9c2e43a84acc733674f`. Foram reinspecionados o plano canónico, este documento, branch, commits, PR, staging, processos, containers e produção. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound. Containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55471`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 110.11s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, git diff check e Gitleaks: passed; 0 leaks em 69 commits
Cleanup: container removido e porta 55471 livre
```

O PR `#1` continua draft, mergeable, no SHA exato da branch e sem reviews/checks/environments. O Codespace técnico está parado, limpo e sincronizado; os três nomes de staging inspecionados continuam sem DNS. Produção responde `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência, Operações e APIs v1 continuam `404`, coerentes com a imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`.

O host live mantém filesystem a 87%, 3,2 GiB livres, 3,8 GiB de RAM, 1,3 GiB de swap em uso, nenhuma base CRM/leads e nenhum timer de backup CRM observável. Em 6.898 registos JSON parseáveis do `kamal-proxy` nas últimas 48 horas foram observados pedidos `2xx` ativos: `/api/stats` 4, `/api/portfolio` 2, `/api/recommendations` 2, `/api/outreach-followups` 5, `/api/email-followups` 5 e `/api/proposal-followups` 5.

A implementação até à Tarefa 18 permanece verde. A conclusão global continua materialmente bloqueada por paridade real falsa, conflitos sem resolução/aceitação, validação humana da amostra, decisões oficiais de `Won` e retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção isolado com backup automático e restore real, smoke no proxy/TLS final, soak e cutover. A Tarefa 19 exige ainda dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. Estes gates técnicos, de dados, humanos e temporais não podem ser fabricados nem dispensados pela autorização autónoma. Não houve merge, deploy, migração live, cutover, retirada do legado ou criação do sentinel.

---

## Retoma autónoma em 2026-07-19T05:12:38Z

A retoma começou no `HEAD` limpo e sincronizado `d1358000d1e4696995ff83b79d9973fd63deabd2`. O plano canónico, este documento, commits, suite, migrations, processos, containers, PR, staging, DNS e produção foram reinspecionados antes de qualquer mutação. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55472`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 108.02s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, git diff check e Gitleaks: passed; 0 leaks em 70 commits
Imagem local: manifest list sha256:ae6bc273bae639410340f6d9589b9260b8f73f5caf116cd241e48bacf744b921
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress=404; 0 erros no log
Cleanup: containers e imagem desta retoma removidos; portas 55472 e 58014 livres
```

O PR `#1` continua draft, mergeable, no SHA exato da branch e sem reviews/checks/environments. Não existe environment GitHub nem DNS nos três nomes de staging inspecionados. Produção responde `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência e APIs v1 continuam `404`, coerentes com a imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`.

O host live mantém o filesystem a 87%, 3,2 GiB livres, 3,8 GiB de RAM, 1,3 GiB de swap em uso, nenhuma base CRM/leads, nenhum container CRM e nenhum timer de backup CRM observável. Não existem CLIs, credenciais cloud ou configuração local observáveis para provisionar um PostgreSQL/staging isolado noutro fornecedor. Em 7.521 registos JSON parseáveis do `kamal-proxy` nas últimas 48 horas foram observados pedidos `2xx` ativos: `/api/stats` 4, `/api/portfolio` 2, `/api/recommendations` 2, `/api/outreach-followups` 5, `/api/email-followups` 5 e `/api/proposal-followups` 5.

A implementação até à Tarefa 18 permanece verde, mas a Tarefa 19 e os gates anteriores de cutover continuam materialmente fechados: paridade real falsa, conflitos sem resolução/aceitação, validação da amostra pelo owner, decisões oficiais de `Won` e retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção isolado com backup automático e restore real, smoke no proxy/TLS final, soak e cutover. A retirada do legado exige ainda dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. Fazer merge, deploy, migração live, cutover, retirada do legado ou criar `.hermes/crm-revamp-complete.json` agora violaria gates explícitos do plano; nenhuma dessas ações foi executada.

---

## Retoma autónoma em 2026-07-19T06:11:15Z

A retoma começou no `HEAD` limpo e sincronizado `df7e80afd15ec5d6b45ebd5a7d3d1ce2dbb7f74b`. O plano canónico, este documento, branch, commits, suite, migrations, processos, containers, PR e superfícies live foram reinspecionados antes de qualquer mutação. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55473`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 109.91s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff checks e Gitleaks: passed; 0 leaks em 71 commits
Cleanup: container e dump removidos; porta 55473 livre
```

O fetch confirmou que a branch e `origin/feat/crm-accounts-proposals-v1` apontavam para o mesmo SHA. O PR `#1` permanecia draft, mergeable, no SHA exato da branch e sem reviews, checks ou environments GitHub. A consulta direta a produção confirmou `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência e APIs v1 continuavam `404`, coerentes com a imagem pré-revamp. Nenhum deploy, migração live ou cutover foi executado.

A primeira tarefa formalmente incompleta continua a ser a Tarefa 19. Os gates que impedem o cutover permanecem reais: paridade real falsa e conflitos não resolvidos/aceites; validação da amostra pelo owner; decisões oficiais de `Won` e retenção/scopes; mapping final de principal/papel/workspace; PostgreSQL de produção isolado com backup automático e restore do arquivo real; smoke no proxy/TLS final; soak e cutover. A retirada do legado exige ainda dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. O sentinel continua corretamente ausente.

---

## Retoma autónoma em 2026-07-19T06:41:12Z

A retoma começou no `HEAD` limpo e sincronizado `115d4416dbbc071cb86c83d68dc4a2e6b0721872`. O plano canónico, este documento, branch, commits, suite, migrations, processos, containers, PR, Codespace, DNS, produção e telemetria foram reinspecionados antes de qualquer mutação. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55474`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 108.51s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff checks e Gitleaks: passed; 0 leaks em 72 commits
Cleanup: container e dump removidos; porta 55474 livre
```

O PR `#1` continua draft, mergeable, no SHA exato da branch e sem reviews/checks/environments. O Codespace técnico continua parado e não existe DNS nos três nomes de staging inspecionados. Produção responde `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência, Operações e APIs v1 continuam `404`, coerentes com a imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`. O tag imutável dessa imagem de rollback continua disponível no registry; o tag do candidato atual não existe ou não está acessível.

O host live mantém filesystem a 87%, 3,2 GiB livres, 3,8 GiB de RAM, 1,2 GiB de swap em uso, PostgreSQL 17.7 sem base CRM/leads e nenhum timer de backup CRM observável. Em 8.231 registos JSON parseáveis do `kamal-proxy` nas últimas 48 horas foram observados pedidos `2xx` ativos nos seis contratos v0 acompanhados: `/api/stats` 4, `/api/portfolio` 2, `/api/recommendations` 2, `/api/outreach-followups` 5, `/api/email-followups` 5 e `/api/proposal-followups` 5. O export read-only preservado continua disponível com mode `0600`, 502.197 bytes e SHA-256 `f3a92324fc8aa3a9e187e67f2eb8cc0ac1fb5e2dc2bf5d8b12278a89ea74f9e1`.

A implementação até à Tarefa 18 permanece verde, mas os gates de cutover e da Tarefa 19 continuam materialmente fechados: paridade real falsa; conflitos sem resolução/aceitação; validação da amostra pelo owner; decisões oficiais de `Won` e retenção/scopes; mapping final de principal/papel/workspace; PostgreSQL de produção isolado com backup automático e restore do arquivo real; smoke no proxy/TLS final; soak e cutover. A retirada do legado exige ainda dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. Fazer merge, deploy, migração live, cutover, retirar o legado ou criar `.hermes/crm-revamp-complete.json` agora violaria gates explícitos do plano; nenhuma dessas ações foi executada.

---

## Retoma autónoma em 2026-07-19T08:13:10Z

A retoma começou no `HEAD` limpo e sincronizado `acf0b30cc9451ad0a9070d530fcd6c824c7d52a8`. O plano canónico, este documento, commits, suite, migrations, processos, containers, PR, Codespace e superfícies públicas de produção foram reinspecionados antes de qualquer mutação. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55475`, explicitamente marcado para testes:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 107.92s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff check e Gitleaks: passed; 0 leaks em 73 commits
Cleanup: container e dump removidos; porta 55475 livre
```

O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews, checks ou environments GitHub. O Codespace técnico continua parado, sincronizado e limpo. A verificação pública confirmou `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência, Operações e APIs v1 continuam `404`, coerentes com produção pré-revamp.

A primeira tarefa formalmente incompleta continua a ser a Tarefa 19, e os gates anteriores de cutover continuam fechados pela evidência mais recente: paridade real falsa, conflitos sem resolução/aceitação, falta de validação da amostra pelo owner, decisões oficiais de `Won` e retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção isolado com backup automático e restore real, smoke no proxy/TLS final, soak e cutover. A retirada do legado exige ainda dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. O sentinel continua corretamente ausente.

---

## Retoma autónoma em 2026-07-19T08:43:37Z

A retoma começou no `HEAD` limpo e sincronizado `473fe9e97b4f22c08d0232f2f4ea58ca2d081d98`. Foram reinspecionados o plano canónico, este documento, commits, suite, migrations, processos, containers, PR, Codespace, deploy e produção. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55476`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 108.44s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, git diff check e Gitleaks: passed; 0 leaks em 74 commits
Cleanup: container e dump removidos; porta 55476 livre
```

O PR `#1` continua draft, mergeable e sem reviews, checks ou environments GitHub. O Codespace técnico permanece parado, sincronizado e limpo. Produção respondeu `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência, Operações e APIs v1 continuaram `404`, coerentes com o build pré-revamp.

O host live mantém filesystem a 87%, 3,2 GiB livres, 3,8 GiB de RAM, 1,2 GiB de swap em uso, zero bases com nome CRM/leads e zero timers de backup CRM. Em 9.883 registos JSON parseáveis das últimas 48 horas foram observados pedidos `2xx` ativos nos seis contratos v0: `/api/stats` 5, `/api/portfolio` 3, `/api/recommendations` 3, `/api/outreach-followups` 6, `/api/email-followups` 6 e `/api/proposal-followups` 6, entre `2026-07-18T20:35:23Z` e `2026-07-19T08:11:54Z`.

A implementação até à Tarefa 18 permanece verde. A conclusão global continua impedida pelos gates explícitos do plano: paridade real falsa; conflitos sem resolução ou aceitação; validação da amostra pelo owner; decisões oficiais de `Won` e retenção/scopes; mapping final de principal/papel/workspace; PostgreSQL de produção isolado com backup automático e restore do arquivo real; smoke no proxy/TLS final; soak e cutover. A Tarefa 19 exige ainda dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. Fazer merge, deploy, migração live, cutover, retirar o legado ou criar `.hermes/crm-revamp-complete.json` neste estado violaria os gates técnicos, de dados, humanos e temporais do plano; nenhuma dessas ações foi executada.

---

## Retoma autónoma em 2026-07-19T09:11:41Z

A retoma partiu do `HEAD` limpo e sincronizado `1738f328c78b3efcf76c2e6d168b4b1c28c2336c`, na branch esperada. O plano canónico, este documento, commits, staged/unstaged work, processos CRM, containers, PR, Codespace e superfícies públicas foram reinspecionados antes de qualquer mutação. Não existia trabalho local por preservar, nem worker CRM, reconciler, outbox publisher ou job outbound ativo. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55477`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 108.76s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, git diff check e Gitleaks: passed; 0 leaks em 75 commits
Cleanup: container e dump removidos; porta 55477 livre
```

O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews, checks ou environments GitHub. O Codespace técnico continua parado, sincronizado e limpo; os três nomes de staging inspecionados continuam sem DNS. Produção respondeu `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência, Operações e APIs v1 continuam `404`, coerentes com o build pré-revamp.

A primeira tarefa formalmente incompleta continua a ser a Tarefa 19. Os gates anteriores de dados e cutover também permanecem fechados pela evidência mais recente: paridade real falsa, conflitos sem resolução/aceitação, falta de validação da amostra pelo owner, decisões oficiais de `Won` e retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção isolado com backup automático e restore real, smoke no proxy/TLS final, soak e cutover. A retirada do legado exige adicionalmente dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, cutover, retirada do legado ou criação do sentinel.

---

## Revalidação autónoma em 2026-07-19T09:43:09Z

A alteração staged desta página foi preservada. A branch e o remote continuavam no SHA `1738f328c78b3efcf76c2e6d168b4b1c28c2336c`; não existiam alterações unstaged nem processos de worker CRM, reconciler, outbox publisher ou jobs outbound. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55478`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 108.29s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff checks e Gitleaks: passed; 0 leaks em 75 commits e no staged diff
Cleanup: container e dump removidos; porta 55478 livre
```

O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews, checks ou environments GitHub. O Codespace técnico continua parado, sincronizado e limpo; os três nomes de staging inspecionados continuam sem DNS. Produção respondeu `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência, Operações e APIs v1 continuam `404`, coerentes com o build pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`.

O host live mantém filesystem a 87%, 3,2 GiB livres, 3,8 GiB de RAM, 1,1 GiB de swap em uso, zero bases com nome CRM/leads e zero timers de backup CRM. Em 10.023 registos JSON parseáveis das últimas 48 horas foram observados pedidos `2xx` ativos nos seis contratos v0: `/api/stats` 5, `/api/portfolio` 3, `/api/recommendations` 3, `/api/outreach-followups` 6, `/api/email-followups` 6 e `/api/proposal-followups` 6, entre `2026-07-18T20:35:23Z` e `2026-07-19T08:11:54Z`.

Os gates continuam materialmente fechados. Não é seguro provisionar PostgreSQL CRM, backups e cutover no host partilhado sob pressão de disco/swap; a paridade real e a validação humana continuam em falta; e retirar v0 quebraria consumidores observados antes dos dois releases pós-cutover exigidos. O sentinel permanece proibido neste estado.

---

## Retoma autónoma em 2026-07-19T10:14:50Z

A retoma começou no `HEAD` limpo e sincronizado `26522b3e52aa1bba755b6c57fcc5e1e58a55108b`, na branch esperada. O plano canónico, este documento, commits, suite, migrations, processos, containers, PR, Codespace, DNS, produção e telemetria foram reinspecionados antes de qualquer mutação. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55479`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 109.89s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Cleanup: container e dump removidos; porta 55479 livre
```

O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews, checks, deployments ou environments GitHub. O Codespace técnico continua parado, sincronizado e limpo; os três nomes de staging inspecionados continuam sem DNS. Desde o ensaio isolado de staging `e151925`, apenas `CURRENT_STATE.md` e `MIGRATION.md` mudaram. Produção respondeu `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência, Operações e APIs v1 continuam `404`, coerentes com o build pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`. A imagem live continua presente localmente no host pelo digest `sha256:38a76feee6fff1b68eaa832e4187edc190c4749a2b4eebac0de1ca4cbe64b817`; não existe imagem imutável acessível do candidato atual.

O host live continua inadequado para improvisar staging e PostgreSQL CRM: filesystem a 87% com 3,2 GiB livres, 3,8 GiB de RAM, 1,2 GiB disponíveis, 1,1 GiB de swap em uso, PostgreSQL 17.7 partilhado sem base CRM/leads e zero timers de backup CRM. Em 10.210 registos JSON parseáveis das últimas 48 horas foram observados pedidos GET `200` ativos nos seis contratos v0: `/api/stats` 5, `/api/portfolio` 3, `/api/recommendations` 3, `/api/outreach-followups` 6, `/api/email-followups` 6 e `/api/proposal-followups` 6, entre `2026-07-18T20:35:23Z` e `2026-07-19T08:11:54Z`.

A primeira tarefa formalmente incompleta continua a ser a Tarefa 19, e os gates anteriores de dados e cutover permanecem fechados: paridade real falsa, conflitos sem resolução/aceitação, falta de validação da amostra pelo owner, decisões oficiais de `Won` e retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção isolado com backup automático e restore do arquivo real, smoke no proxy/TLS final, soak e cutover. A retirada do legado exige adicionalmente dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. Fazer merge, deploy, migração live, cutover, retirar o legado ou criar `.hermes/crm-revamp-complete.json` neste estado violaria os gates explícitos do plano; nenhuma dessas ações foi executada.

---

## Retoma autónoma em 2026-07-19T10:46:17Z

A retoma começou no `HEAD` limpo e sincronizado `e140260feb6d6829cc5338ee657937f07c7862cc`, na branch esperada. O plano canónico, este documento, commits, suite, migrations, processos, containers, PR, staging e produção foram reinspecionados antes de qualquer alteração. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55480`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 108.58s, exit explícito 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, git diff check e Gitleaks: passed; 0 leaks em 77 commits
Imagem local: manifest list sha256:d9ceefe8af347640241194d9085212ff8b7570bf36b5b76c9092e30fa1d3c286
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress=404; 0 erros no log
Cleanup: PostgreSQL, container de smoke e imagem removidos; portas 55480 e 58015 livres
```

O export read-only preservado fora do repositório continua disponível com mode `0600`, 502.197 bytes e SHA-256 `f3a92324fc8aa3a9e187e67f2eb8cc0ac1fb5e2dc2bf5d8b12278a89ea74f9e1`. Desde o ensaio isolado de staging `e151925`, não existe delta de código, templates, JavaScript, configuração ou Dockerfile; apenas `CURRENT_STATE.md` e `MIGRATION.md` mudaram. O Codespace efémero continua listado em estado `Shutdown`, na branch esperada, limpo e sem commits por publicar; não é um staging persistente em execução nem um ambiente final de produção a promover.

A descoberta de produção permaneceu read-only. `/up` e o dashboard legado devolveram `200`; Contas, Propostas, Inteligência, Operações e APIs v1 devolveram `404`. O container live continua no commit `7622a2b2b8d5e0790858208b2c3a1f119edb7328`. O host mantém filesystem a 87% com 3,2 GiB livres, 3,8 GiB de RAM, cerca de 1,0 GiB disponível, 1,1 GiB de swap em uso, zero bases CRM/leads, zero containers CRM e zero timers de backup CRM observáveis.

Em 9.862 access records JSON normalizados das últimas 48 horas foram observados pedidos GET `2xx` ativos: `/api/stats` 7, `/api/portfolio` 5, `/api/recommendations` 5, `/api/outreach-followups` 6, `/api/email-followups` 6 e `/api/proposal-followups` 6. Os pedidos mais recentes de portfolio/recommendations/stats ocorreram em 2026-07-19T10:38Z. Retirar ou proteger v0 agora quebraria consumidores observados.

A implementação até à Tarefa 18 permanece verde, mas os gates de dados, produção e release continuam materialmente fechados. Paridade e conflitos reais continuam sem resolução ou aceitação; faltam validação da amostra pelo owner, decisões oficiais de `Won` e retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção isolado com backup automático e restore do arquivo real, smoke no proxy/TLS final, soak e cutover. A Tarefa 19 exige ainda dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. A autorização autónoma não permite fabricar esta evidência nem dispensar os gates técnicos e temporais. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, cutover, retirada do legado ou criação do sentinel.

---

## Retoma autónoma em 2026-07-19T12:15:10Z

A retoma começou no `HEAD` limpo e sincronizado `a7b85e440687eaba18476331e5f2ad2a6723eed7`, na branch esperada. O plano canónico, `CURRENT_STATE.md`, commits, testes, migrations, processos, containers, PR, Codespace, produção e gates de cutover foram reinspecionados antes de qualquer alteração. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55482`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 109.61s, exit explícito 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff checks e Gitleaks: passed; 0 leaks em 78 commits
Imagem local: manifest list sha256:0cd003e8a3cbaed5f4a2ba7f79ffdec058603943d2fd2ea49fcdbc1d7b85f9dc
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress=404; 0 erros no log
Cleanup: PostgreSQL, dump, container de smoke e imagem removidos; portas 55482 e 58016 livres
```

O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews ou checks. O Codespace técnico continua em `Shutdown`, sincronizado e limpo. A verificação pública atual confirmou `/up=200`, dashboard legado `200` e novas páginas/APIs `404`, coerentes com produção pré-revamp. O host mantém filesystem a 87%, cerca de 3,2 GiB livres, cerca de 1,0 GiB de memória disponível, zero bases CRM/leads, zero containers CRM e zero timers de backup CRM observáveis.

A primeira tarefa formalmente incompleta continua a ser a Tarefa 19. Os gates anteriores de dados e cutover permanecem fechados pela evidência real mais recente: paridade falsa e conflitos sem resolução/aceitação; falta de validação da amostra pelo owner; decisões oficiais de `Won` e retenção/scopes; mapping final de principal/papel/workspace; PostgreSQL de produção isolado com backup automático e restore real; smoke no proxy/TLS final; soak e cutover. A retirada do legado exige adicionalmente dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders; a telemetria registada imediatamente antes desta retoma ainda tinha consumidores v0 ativos. Fazer merge, deploy, migração live, cutover, retirar o legado ou criar `.hermes/crm-revamp-complete.json` neste estado violaria gates explícitos do plano; nenhuma dessas ações foi executada.

---

## Retoma autónoma em 2026-07-19T13:13:30Z

A retoma começou no `HEAD` limpo e sincronizado `00048312fc393b644822e1a9b8da5e5eedbbaef5`, na branch esperada. O plano canónico, este documento, commits, suite, migrations, processos, containers, PR, staging, produção e telemetria foram reinspecionados. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55483`, explicitamente marcado para testes:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 110.44s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff checks e Gitleaks: passed; 0 leaks em 79 commits
```

A verificação pública confirmou `/up=200`, dashboard legado `200` e novas páginas/APIs `404`. O host live continua no commit pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`, com filesystem a 87%, 3,2 GiB livres, cerca de 1,0 GiB de memória disponível, zero bases CRM/leads e zero timers de backup CRM observáveis. O PR `#1` permanece draft, mergeable, sem reviews, checks, deployments ou environments GitHub.

A telemetria estruturada das últimas 48 horas contém consumidores `2xx` ativos nos seis contratos v0: `/api/stats` 9, `/api/portfolio` 7, `/api/recommendations` 7, `/api/outreach-followups` 6, `/api/email-followups` 6 e `/api/proposal-followups` 6. Os pedidos mais recentes a stats/portfolio/recommendations ocorreram em 2026-07-19T11:39Z. Retirar ou proteger estes contratos agora quebraria tráfego observado.

A implementação até à Tarefa 18 permanece verde. A conclusão global continua bloqueada pelos gates explícitos de dados, produção e release: paridade falsa e conflitos sem resolução/aceitação; validação da amostra pelo owner; decisões oficiais de `Won` e retenção/scopes; mapping final de principal/papel/workspace; PostgreSQL de produção isolado com backup automático e restore do arquivo real; smoke no proxy/TLS final; soak e cutover. A Tarefa 19 exige ainda dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, cutover, retirada do legado ou criação do sentinel.

---

## Retoma autónoma em 2026-07-19T15:15:25Z

A retoma começou no `HEAD` limpo e sincronizado `30cf368ab3a67799e1ad9505830839508d5e2fbc`, na branch esperada. O plano canónico, este documento, commits, suite, migrations, processos, containers, PR, staging, produção e telemetria foram reinspecionados antes de qualquer alteração. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55484`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 110.39s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
compileall, git diff check e Gitleaks: passed; 0 leaks em 80 commits
Cleanup: container e dump removidos; porta 55484 livre
```

A descoberta externa permaneceu read-only. Produção responde `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência, Operações e APIs v1 continuam `404`, coerentes com a imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`. O host mantém filesystem a 87%, 3.242.680 KiB livres, 3.915 MiB de RAM total, 1.275 MiB disponíveis, 1.115 MiB de swap em uso, zero bases CRM/leads e zero timers de backup CRM. O PR `#1` permanece draft, mergeable, sem reviews, checks, deployments ou environments GitHub; não existe DNS para os três nomes de staging inspecionados.

Em 11.854 registos JSON parseáveis das últimas 48 horas foram observados pedidos `2xx` ativos nos seis contratos v0: `/api/stats` 9, `/api/portfolio` 7, `/api/recommendations` 7, `/api/outreach-followups` 6, `/api/email-followups` 6 e `/api/proposal-followups` 6. Os pedidos mais recentes ocorreram entre `2026-07-19T08:11:54Z` e `2026-07-19T11:39:22Z`. Retirar ou proteger estes contratos agora quebraria tráfego observado.

A primeira tarefa formalmente incompleta continua a ser a Tarefa 19. Os gates anteriores de dados e cutover permanecem fechados: paridade real falsa e conflitos sem resolução/aceitação; falta de validação da amostra pelo owner; decisões oficiais de `Won` e retenção/scopes; mapping final de principal/papel/workspace; PostgreSQL de produção isolado com backup automático e restore do arquivo real; smoke no proxy/TLS final; soak e cutover. A retirada do legado exige adicionalmente dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. Fazer merge, deploy, migração live, cutover, retirar o legado ou criar `.hermes/crm-revamp-complete.json` neste estado violaria gates explícitos do plano; nenhuma dessas ações foi executada.

---

## Retoma autónoma em 2026-07-19T16:15:07Z

A retoma começou no `HEAD` limpo e sincronizado `8093430255ad8f3fb23fcd02cafbd24362f87fae`, na branch esperada. O plano canónico, este documento, commits, testes, migrations, processos, containers, PR, staging, produção, Sheet real e telemetria foram reinspecionados antes de qualquer alteração. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Recursos preexistentes desconhecidos foram preservados.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55485`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 110.22s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff checks e Gitleaks: passed; 0 leaks em 81 commits
Imagem local: sha256:5bebb25d36fb6b98ff5b0348dee8ee046afcd58dfd25921cec5fe2e259ab816c
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress=404; 0 erros no log
Cleanup: PostgreSQL, dump, container de smoke e imagem removidos; portas 55485, 55486 e 58017 livres
```

Uma captura read-only atual da Sheet real foi guardada apenas em ficheiros temporários `0600`, aplicada duas vezes num segundo PostgreSQL 16 descartável e removida com a credencial temporária no fim:

```text
Snapshot: 1.247 input rows, 1.202 aplicáveis, 12 identidades duplicadas, 21 linhas sem identidade segura
Accounts apply #1: 65 imports, 46 contas criadas/associadas, 52 conflitos
Accounts apply #2: 0 imports, 65 replay no-op
Review: 12 duplicate_stable_id, 21 missing_stable_id, 18 history_required, 1 identity_conflict
Proposals apply #1: 44 imports, 4 unmatched accounts, 48 missing value/evidence
Proposals apply #2: 0 imports, 44 replay no-op
Compare: parity=false, 1 lead/account em falta, 0 stage/account/source-field mismatches
```

A comparação terminou deliberadamente com exit `1` porque a paridade permanece falsa. Nenhum conflito foi auto-fundido, nenhum valor comercial foi impresso, não houve write na Sheet e todos os recursos temporários desta validação foram removidos.

A descoberta externa permaneceu read-only. O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews, checks, deployments ou environments GitHub. O Codespace técnico continua em `Shutdown`; não existe DNS para os três nomes de staging inspecionados. Produção responde `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência, Operações e APIs v1 continuam `404`, coerentes com a imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`.

O host live mantém filesystem a 87%, 3.229.744 KiB livres, 3.915 MiB de RAM total, 1.330 MiB disponíveis, 1.105 MiB de swap em uso, zero bases CRM/leads e zero timers de backup CRM observáveis. Em 12.699 registos JSON parseáveis das últimas 48 horas foram observados pedidos GET `2xx` ativos nos seis contratos v0: `/api/stats` 9, `/api/portfolio` 7, `/api/recommendations` 7, `/api/outreach-followups` 6, `/api/email-followups` 6 e `/api/proposal-followups` 6. Os pedidos mais recentes ocorreram entre `2026-07-19T08:11:54Z` e `2026-07-19T11:39:22Z`.

A implementação até à Tarefa 18 permanece verde, mas os gates de dados, produção e release continuam materialmente fechados. Persistem paridade falsa, conflitos sem resolução ou aceitação, falta de validação da amostra pelo owner, decisões oficiais de `Won` e retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção isolado com backup automático e restore do arquivo real, smoke no proxy/TLS final, soak e cutover. A Tarefa 19 exige ainda dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. Fazer merge, deploy, migração live, cutover, retirar o legado ou criar `.hermes/crm-revamp-complete.json` neste estado violaria os gates explícitos do plano; nenhuma dessas ações foi executada.

---

## Retoma autónoma em 2026-07-19T17:14:25Z

A retoma começou no `HEAD` limpo e sincronizado `8530cf0320cd48f3c96e9bd431d0b31102e10fd9`, na branch esperada. O plano canónico, este documento, commits, suite, migrations, processos, containers, PR, staging, produção e telemetria foram reinspecionados antes de qualquer alteração. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Recursos preexistentes desconhecidos foram preservados.

Dois PostgreSQL 16 descartáveis exclusivos foram usados em loopback e removidos no fim. A primeira execução verificou migrations, rollback e restore; a segunda repetiu a suite com o exit code capturado explicitamente:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 109.67s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff checks e Gitleaks: passed; 0 leaks em 82 commits
Cleanup: containers e dump removidos; portas 55487 e 55488 livres
```

O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews ou checks. O Codespace técnico continua em `Shutdown`, sincronizado e limpo. Produção responde `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência, Operações e APIs v1 continuam `404`, coerentes com a imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`.

O host live mantém filesystem a 87%, 3.251.644 KiB livres, 3.915 MiB de RAM total, 999 MiB disponíveis, 1.057 MiB de swap em uso, PostgreSQL 17.7 sem base CRM/leads e zero timers de backup CRM observáveis. Nos 745 registos JSON parseáveis disponíveis das últimas 48 horas foram observados pedidos `2xx` ativos em `/api/stats` (1), `/api/outreach-followups` (2), `/api/email-followups` (2) e `/api/proposal-followups` (2), com os últimos pedidos em 2026-07-19T16:48Z. A janela atual não contém portfolio/recommendations, mas janelas anteriores continham consumidores e não existem dois releases pós-cutover.

A implementação até à Tarefa 18 continua verde. A primeira tarefa formalmente incompleta é a Tarefa 19, mas os gates anteriores de dados e cutover permanecem fechados: paridade real falsa e conflitos sem resolução ou aceitação; validação da amostra pelo owner; decisões oficiais de `Won` e retenção/scopes; mapping final de principal/papel/workspace; PostgreSQL de produção isolado com backup automático e restore do arquivo real; smoke no proxy/TLS final; soak e cutover. A retirada do legado exige ainda dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. A autorização autónoma não cria evidência humana ou temporal nem permite violar estes gates técnicos. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, cutover, retirada do legado ou criação do sentinel.

---

## Retoma autónoma em 2026-07-19T17:45:58Z

A retoma começou no `HEAD` limpo e sincronizado `ee4de273eb3925ea82c8f82837ddd8d7331e1a24`, na branch esperada. O plano canónico, este documento, commits, testes, migrations, processos, containers, PR, Codespace, produção e gates externos foram reinspecionados antes de qualquer alteração. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Recursos preexistentes desconhecidos foram preservados.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55489`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 110.70s, exit explícito 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff check e Gitleaks: passed; 0 leaks em 83 commits
Cleanup: container e dump removidos; porta 55489 livre
```

A descoberta externa permaneceu read-only. O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews, checks ou environments GitHub. O Codespace técnico continua em `Shutdown`, sincronizado e limpo. Produção responde `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência, Operações e APIs v1 continuam `404`, coerentes com o build pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`.

O host live mantém filesystem a 87%, 3.251.204 KiB livres, 3.915 MiB de RAM total, 1.016 MiB disponíveis, 1.056 MiB de swap em uso, PostgreSQL 17 sem base CRM/leads, zero containers CRM e zero timers de backup CRM observáveis. A janela atual do `kamal-proxy` tinha apenas 82 registos JSON parseáveis e não continha os seis endpoints v0 acompanhados; esta janela curta não prova ausência de consumidores, porque a verificação imediatamente anterior observou tráfego `2xx` ativo e ainda não existe qualquer release pós-cutover.

A implementação até à Tarefa 18 permanece verde, mas a conclusão global e a Tarefa 19 continuam bloqueadas pelos gates explícitos do plano: paridade real falsa e conflitos sem resolução ou aceitação; validação da amostra pelo owner; decisões oficiais de `Won` e retenção/scopes; mapping final de principal/papel/workspace; PostgreSQL de produção isolado com backup automático e restore real; smoke no proxy/TLS final; soak e cutover; dois releases pós-cutover; ausência comprovada de consumidores v0; e aceitação dos stakeholders. Fazer merge, deploy, migração live, cutover, retirar o legado ou criar `.hermes/crm-revamp-complete.json` neste estado violaria esses gates; nenhuma dessas ações foi executada.

---

## Retoma autónoma em 2026-07-19T19:16:25Z

A retoma começou no `HEAD` limpo e sincronizado `be93393a33b0c0192f7cab90eb4fca518564bb12`, na branch esperada. O plano canónico, este documento, commits, testes, migrations, processos, containers, PR, staging, produção e telemetria foram reinspecionados antes de qualquer alteração. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Recursos preexistentes desconhecidos foram preservados.

Num PostgreSQL 16 descartável exclusivo em loopback, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 111.41s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall e diff checks: passed
Cleanup: containers, dump e bases de restore removidos; portas 55490 e 55491 livres
```

Uma primeira invocação da suite omitiu o marker obrigatório `CRM_DISPOSABLE_TEST_DATABASE=1` e falhou fechada antes de constituir evidência comportamental válida. A invocação corrigida acima passou integralmente; não houve alteração de código para obter o resultado.

A descoberta externa permaneceu read-only. O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews, checks ou environments GitHub. O Codespace técnico continua em `Shutdown`, sincronizado e limpo, e não existe DNS nos três nomes de staging inspecionados. Produção responde `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência, Operações e APIs v1 continuam `404`, coerentes com a imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`.

O host live mantém filesystem a 87%, 3.249.912 KiB livres, 3.915 MiB de RAM total, 1.276 MiB disponíveis, 1.052 MiB de swap em uso, zero bases CRM/leads, zero containers CRM e zero timers de backup CRM observáveis. Em 1.838 registos JSON parseáveis das últimas 48 horas foram observados pedidos `2xx` ativos em `/api/stats` (1), `/api/outreach-followups` (2), `/api/email-followups` (2) e `/api/proposal-followups` (2), com os últimos pedidos em 2026-07-19T16:48Z. A janela atual não contém portfolio/recommendations, mas janelas anteriores continham consumidores e não existe qualquer release pós-cutover.

O export read-only preservado continua disponível fora do repositório com mode `0600`, 502.197 bytes e SHA-256 `f3a92324fc8aa3a9e187e67f2eb8cc0ac1fb5e2dc2bf5d8b12278a89ea74f9e1`. A implementação até à Tarefa 18 permanece verde, mas a Tarefa 19 e a conclusão global continuam bloqueadas pelos gates de dados, produção, humanos e temporais do plano. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, cutover, retirada do legado ou criação do sentinel.

---

## Retoma autónoma em 2026-07-19T19:45:24Z

A retoma começou no `HEAD` limpo e sincronizado `e88f922cf49db54fcc9b76507146442a56292102`, na branch esperada. O plano canónico, este documento, commits, suite, migrations, processos, containers, PR, Codespace, produção e telemetria foram reinspecionados antes de qualquer alteração. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55492`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 952 passed, 1 skipped em 109.70s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected, exit 0
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff checks e Gitleaks: passed; 0 leaks em 85 commits
Cleanup: container, dump e base de restore removidos; porta 55492 livre
```

Uma primeira agregação do Ruff perdeu separadores NUL através de command substitution e terminou com `E902 File name too long`; a invocação corrigida com pipe NUL direto para `xargs -0` passou. O lifecycle Alembic agregado também foi repetido isoladamente para capturar `alembic check` com exit explícito `0`. Estes foram erros dos comandos de verificação, não findings de código.

A descoberta externa permaneceu read-only. O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews, checks, deployments ou environments GitHub. O Codespace técnico continua em `Shutdown`, sincronizado e limpo, e não existe DNS nos três nomes de staging inspecionados. Produção responde `/up=200` e dashboard legado `200`; Contas, Propostas, Inteligência e APIs v1 continuam `404`, coerentes com a imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`.

O host live continua sem base/container/timer de backup CRM observável e inadequado para improvisar PostgreSQL canónico: filesystem a 87%, 3.249.508 KiB livres, 3.915 MiB de RAM total, 270 MiB livres e 1.052 MiB de swap em uso no probe atual. Em 1.947 registos JSON parseáveis do `kamal-proxy` nas últimas 48 horas foram observados pedidos `2xx` ativos em `/api/stats` (1), `/api/outreach-followups` (2), `/api/email-followups` (2) e `/api/proposal-followups` (2), com os últimos pedidos em 2026-07-19T16:48Z. A janela atual não contém portfolio/recommendations, mas janelas anteriores continham consumidores e não existe qualquer release pós-cutover.

A primeira tarefa formalmente incompleta permanece a Tarefa 19. Os gates anteriores de dados e cutover continuam fechados: paridade real falsa e conflitos sem resolução ou aceitação; validação da amostra pelo owner; decisões oficiais de `Won` e retenção/scopes; mapping final de principal/papel/workspace; PostgreSQL de produção isolado com backup automático e restore real; smoke no proxy/TLS final; soak e cutover. A retirada do legado exige ainda dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. Fazer merge, deploy, migração live, cutover, retirar o legado ou criar `.hermes/crm-revamp-complete.json` neste estado violaria os gates explícitos do plano; nenhuma dessas ações foi executada.

---

## Retoma autónoma em 2026-07-19T20:25:08Z

A retoma começou no `HEAD` limpo e sincronizado `9ec148304ee1c1d670f690e96ee5e69fa41bb233`, na branch esperada. O plano canónico, `CURRENT_STATE.md`, commits, staged/unstaged work, testes, migrations, processos, containers, PR, Codespace, produção, telemetria e rollback foram reinspecionados antes de qualquer alteração. Não existia trabalho local por preservar nem processos de worker CRM, reconciler ou outbox publisher ativos. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Três PostgreSQL 16 descartáveis exclusivos foram criados em loopback, explicitamente marcados para testes e removidos no fim. A primeira tentativa de suite incluiu o diretório inexistente `tests/contract` e terminou antes da coleção; a segunda começou sobre uma base vazia e confirmou a pré-condição operacional de migrations. A invocação corrigida aplicou `upgrade head` antes dos testes e passou.

A auditoria do harness encontrou módulos de integração antigos que aceitavam qualquer URL `postgresql+psycopg` quando executados isoladamente. Foi observado RED num subprocesso de coleção com um destino remoto/production-shaped (`returncode=0`). O novo `tests/conftest.py` reutiliza o guard existente antes da coleção de toda a suite; o mesmo teste passou depois da correção e o harness agora exige simultaneamente driver exato, loopback, nome de base contendo `test` e `CRM_DISPOSABLE_TEST_DATABASE=1`, sem revelar a URL rejeitada. A suite completa foi repetida após esta alteração:

```text
Suite segura completa com DeprecationWarning como erro: 953 passed, 1 skipped em 110.42s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff check e Gitleaks: passed; 0 leaks em 86 commits
Imagem local: sha256:f3eb4c0b86d2e870e8b3825b5a410d8b9e4e46a983aebd3d6bf69f82430304d6
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress=404; 0 erros no log
Cleanup: PostgreSQL, dumps, base de restore, container de smoke e imagem removidos; portas 55493, 55494, 55495 e 58018 livres
```

O export read-only preservado fora do repositório foi verificado sem ler ou imprimir conteúdo: mode `0600`, 502.197 bytes e SHA-256 `f3a92324fc8aa3a9e187e67f2eb8cc0ac1fb5e2dc2bf5d8b12278a89ea74f9e1`.

A descoberta externa permaneceu read-only. O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews, checks, deployments ou environments GitHub. O único Codespace técnico está em `Shutdown`, sincronizado e limpo. Desde o ensaio isolado de staging `e151925`, a aplicação, migrations, configuração, templates e imagem não mudaram; o delta posterior limita-se à documentação e ao guard do harness de testes.

Produção respondeu `/up=200`, dashboard legado `200`, seis APIs v0 acompanhadas `200` e todas as novas páginas/APIs `404`, coerente com o build pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`. Essa imagem continua disponível localmente no host como rollback. O host live mantém filesystem a 87%, 3.248.864 KiB livres, 3.915 MiB de RAM total, 1.169 MiB disponíveis, 1.050 MiB de swap em uso, zero bases CRM/leads e zero timers de backup CRM. Em 2.575 registos JSON parseáveis das últimas 48 horas foram observados pedidos `2xx` ativos em `/api/stats` (1), `/api/outreach-followups` (2), `/api/email-followups` (2) e `/api/proposal-followups` (2), com os últimos pedidos em 2026-07-19T16:48Z. Não existiam processos canónicos de worker/reconciler/outbox.

A implementação até à Tarefa 18 permanece verde, mas a Tarefa 19 e a conclusão global continuam bloqueadas pelos gates explícitos do plano: paridade real falsa e conflitos sem resolução ou aceitação; validação da amostra pelo owner; decisões oficiais de `Won` e retenção/scopes; mapping final de principal/papel/workspace; PostgreSQL de produção isolado com backup automático e restore do arquivo real; staging persistente no proxy/TLS final; soak e cutover; dois releases pós-cutover; ausência comprovada de consumidores v0; e aceitação dos stakeholders. A autorização autónoma não cria evidência humana ou temporal e não torna seguro improvisar PostgreSQL no host partilhado sob pressão de capacidade. Não houve merge, deploy, migração/backfill live, ativação de workers/conectores/outbox, cutover, retirada do legado ou criação do sentinel.

---

## Fecho verificável da retoma em 2026-07-20T08:49:14Z

O hardening preservado do harness foi congelado no digest staged `83931b65c9abb0e7eda3d77064f74a2b4078b7a9d991573b1c9d67841868926f`, commitado atomicamente como `1f1210deb00dfe89a5198c1c909c92d42aff33e5` (`test: guard CRM suites against unsafe databases`) e publicado em `origin/feat/crm-accounts-proposals-v1`. O teste focado passou com `2 passed`; a suite segura completa contra PostgreSQL 16 descartável passou com `953 passed, 1 skipped` e `DeprecationWarning` tratado como erro.

A verificação repetiu o lifecycle Alembic `0007 -> base -> 0007`, `alembic current`, `alembic check` e o restore de um dump custom-format, que confirmou schema `0007`, 15 tabelas, zero workspaces e zero violações. Ruff, compileall, diff checks, scan estático de linhas adicionadas e Gitleaks passaram sem findings. A imagem candidata foi reconstruída localmente como manifest list `sha256:9605aa7f2338c87a6f0002dd990414f1bff4533e96480efa5e669b736bf6e2d1`; o smoke confirmou `/up=200`, dashboard e rotas ricas deny-only em `403`, Agent ingress desativado em `404` e zero erros de arranque. O PostgreSQL, dump, base de restore, container de smoke e imagem criados nesta retoma foram removidos; as portas exclusivas `55496` e `58019` ficaram livres. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

O export read-only externo permanece mode `0600`, com 502.197 bytes e checksum SHA-256 `f3a92324fc8aa3a9e187e67f2eb8cc0ac1fb5e2dc2bf5d8b12278a89ea74f9e1`. O PR `#1` aponta para o commit publicado, continua draft, sem checks ou environments; o Codespace técnico continua em `Shutdown`, sincronizado e limpo.

A revalidação live permaneceu read-only. Produção continua no container saudável `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up` e o dashboard legado devolvem `200`, as novas páginas/APIs devolvem `404` e as seis APIs v0 acompanhadas devolvem `200`. O host mantém filesystem a 87%, 3.237.508 KiB livres, 3.915 MiB de RAM total, 1.138 MiB disponíveis, 981 MiB de swap em uso, zero bases CRM/leads e zero timers de backup CRM. Em 9.577 registos JSON parseáveis das últimas 48 horas existia tráfego `2xx` v0 não-curl, incluindo browser, em `/api/stats` (8), `/api/portfolio` (5), `/api/recommendations` (5), `/api/outreach-followups` (5), `/api/email-followups` (4) e `/api/proposal-followups` (4); os últimos pedidos não-curl ocorreram em 2026-07-20T08:40Z. Nenhum worker CRM, reconciler ou outbox publisher estava ativo.

Os gates de conclusão continuam materialmente fechados pela realidade externa e temporal, não por uma pausa de aprovação: paridade real falsa e conflitos sem resolução/aceitação; validação da amostra pelo owner; política oficial de `Won` e retenção/scopes; mapping final de principal/papel/workspace; PostgreSQL de produção isolado com backup automático e restore do arquivo real; staging final no proxy/TLS; soak e cutover; dois releases pós-cutover; ausência de consumidores v0; e aceitação dos stakeholders. Retirar v0 agora quebraria tráfego observado; improvisar PostgreSQL no host partilhado violaria capacidade, isolamento, restore e rollback. Não houve merge, deploy, migração/backfill live, ativação de workers/conectores/outbox, cutover, retirada do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma em 2026-07-20T09:47:34Z

A retoma partiu do `HEAD` limpo e sincronizado `5b70623b8b0e4919fcec128245763a5dc06dc9cc`, na branch esperada. O plano canónico, este documento, commits, suite, migrations, processos, containers, PR, Codespace, produção, capacidade, telemetria e opções de cloud foram reinspecionados antes de qualquer alteração. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Recursos preexistentes desconhecidos foram preservados.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55500`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 953 passed, 1 skipped em 111.19s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff checks e Gitleaks: passed; 0 leaks em 88 commits
Imagem local: manifest list sha256:5338805f7acc2f58344fcbed95b0e4138f7ea2ce004eec25821858efbd4a1f6c
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress=404; 0 erros no log
Cleanup: PostgreSQL, dump, base de restore, container de smoke e imagem removidos; portas 55500 e 58020 livres
```

O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews, checks, deployments ou environments GitHub. O único Codespace técnico continua em `Shutdown`, sincronizado e limpo. Não existem DNS, sessão DigitalOcean, CLIs ou credenciais cloud observáveis para provisionar um staging final ou PostgreSQL gerido isolado. Desde o ensaio técnico de staging, o único delta não documental é o guard fail-closed do harness de PostgreSQL.

A descoberta live permaneceu read-only. Produção continua na imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up` e o dashboard legado devolvem `200`, as novas páginas/APIs devolvem `404` e as APIs v0 acompanhadas devolvem `200`. O host mantém filesystem a 87%, 3.235.864 KiB livres, 3.915 MiB de RAM total, 997.308 KiB disponíveis, zero bases CRM/leads e zero timers de backup CRM observáveis. Em 10.182 registos JSON parseáveis das últimas 48 horas foram observados pedidos `2xx` não-curl ativos: `/api/stats` 14, `/api/portfolio` 5, `/api/recommendations` 5, `/api/outreach-followups` 14, `/api/email-followups` 10 e `/api/proposal-followups` 12; os pedidos mais recentes ocorreram durante esta retoma.

A primeira tarefa formalmente incompleta permanece a Tarefa 19, mas remover v0 agora quebraria consumidores observados e violaria os gates de ausência de consumidores, dois releases pós-cutover e aceitação. Os gates anteriores de dados e cutover continuam fechados por paridade real falsa, conflitos sem resolução/aceitação, falta de validação da amostra pelo owner, decisões oficiais de `Won` e retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção isolado com backup automático e restore do arquivo real, staging final no proxy/TLS, soak e cutover. A autorização autónoma não permite fabricar evidência humana/temporal nem usar o host partilhado sem capacidade e isolamento. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, cutover, retirada do legado ou criação do sentinel.

---

## Retoma autónoma em 2026-07-20T10:18:12Z

A retoma começou no `HEAD` limpo e sincronizado `f13f61a9d743aa10a45369c7a34da93603e41a51`, na branch esperada. O plano canónico, `CURRENT_STATE.md`, commits, staged/unstaged work, suite, migrations, processos, containers, PR, Codespace, produção, capacidade e telemetria foram reinspecionados antes de qualquer mutação. Não existia trabalho local por preservar, nem processos CRM de worker, reconciler ou outbox publisher ativos. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55501`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 953 passed, 1 skipped em 113.07s, exit explícito 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected, exit 0
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff checks e Gitleaks: passed; 0 leaks em 89 commits
Cleanup: container, dump e base de restore removidos; porta 55501 livre
```

O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews, checks, deployments ou environments GitHub. O único Codespace técnico continua em `Shutdown`, sincronizado e limpo. Não existem CLIs, variáveis de credencial cloud ou configuração local observáveis para provisionar um staging final ou PostgreSQL gerido isolado. Desde o ensaio técnico de staging, a aplicação e as migrations não mudaram; o delta posterior limita-se ao guard fail-closed do harness e documentação.

A descoberta live permaneceu read-only. Produção continua na imagem pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up` e o dashboard legado devolvem `200`, as novas páginas/APIs devolvem `404` e as seis APIs v0 acompanhadas devolvem `200`. O host mantém filesystem a 87%, 3.235.244 KiB livres, 3.915 MiB de RAM total, 984 MiB disponíveis, cerca de 980 MiB de swap em uso, zero bases CRM/leads e zero timers de backup CRM observáveis.

Em 10.688 registos JSON parseáveis das últimas 48 horas foram observados pedidos `2xx` não-curl ativos: `/api/stats` 18, `/api/portfolio` 5, `/api/recommendations` 5, `/api/outreach-followups` 18, `/api/email-followups` 14 e `/api/proposal-followups` 16. Os pedidos mais recentes ocorreram durante esta retoma. Retirar ou proteger v0 agora quebraria tráfego observado.

A implementação até à Tarefa 18 permanece verde. A primeira tarefa formalmente incompleta é a Tarefa 19, mas os gates anteriores de dados e cutover continuam fechados por paridade real falsa, conflitos sem resolução/aceitação, falta de validação da amostra pelo owner, decisões oficiais de `Won` e retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção isolado com backup automático e restore do arquivo real, staging final no proxy/TLS, soak e cutover. A retirada do legado exige ainda dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. Fazer merge, deploy, migração live, cutover, retirar o legado ou criar `.hermes/crm-revamp-complete.json` neste estado violaria gates explícitos do plano; nenhuma dessas ações foi executada.

---

## Retoma autónoma em 2026-07-20T11:50:22Z

A retoma começou no `HEAD` limpo e sincronizado `9335ba5d9f35560047cb80d7984dc3fa2ddf168f`, na branch esperada. O plano canónico, este documento, commits, suite, migrations, processos, containers, PR, Codespace, produção, capacidade e telemetria foram reinspecionados antes de qualquer mutação. Não existia trabalho staged, unstaged ou untracked, nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Recursos preexistentes desconhecidos foram preservados.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55502`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 953 passed, 1 skipped em 113.08s, exit explícito 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected, exit 0
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Fixture de contas apply #1: 4 imports, 3 contas criadas/associadas, 1 conflito
Fixture idêntica apply #2: 0 imports, 4 replay no-op, 0 novos registos
Ruff no delta Python, compileall, diff checks e Gitleaks: passed; 0 leaks em 90 commits
Imagem local: manifest list sha256:ba0e22cb2ab51d5950b1cce9c21d1c7727e3a8a0f0e72eb49c035761883faca3
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress=404; 0 erros no log
Smoke autenticado com PostgreSQL: pedidos sem credenciais=401; páginas e APIs ricas=200
Cleanup: PostgreSQL, dump, bases de restore, containers de smoke e imagem removidos; portas 55502, 58021 e 58022 livres
```

O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews, checks, deployments ou environments GitHub. O único Codespace técnico continua em `Shutdown`, sincronizado e limpo. Produção permanece no build pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up` e o dashboard legado devolvem `200`, as novas páginas/APIs devolvem `404` e as APIs v0 acompanhadas devolvem `200`.

O host live mantém filesystem a 87%, cerca de 3,1 GiB livres, 3,8 GiB de RAM, 966 MiB disponíveis, 972 MiB de swap em uso, zero bases CRM/leads e zero timers de backup CRM observáveis. A telemetria normalizada das últimas 48 horas confirmou tráfego GET `2xx` não-curl ativo nos seis contratos v0: `/api/stats` 21, `/api/portfolio` 6, `/api/recommendations` 6, `/api/outreach-followups` 19, `/api/email-followups` 15 e `/api/proposal-followups` 17; os pedidos mais recentes ocorreram em 2026-07-20T11:13Z.

A implementação até à Tarefa 18 continua verde, mas os gates de conclusão permanecem materialmente fechados: paridade real falsa e conflitos sem resolução/aceitação; validação da amostra pelo owner; política oficial de `Won` e retenção/scopes; mapping final de principal/papel/workspace; PostgreSQL de produção isolado com backup automático e restore do arquivo real; staging final no proxy/TLS; soak e cutover; dois releases pós-cutover; ausência comprovada de consumidores v0; e aceitação dos stakeholders. Retirar v0 agora quebraria tráfego observado e improvisar PostgreSQL no host partilhado violaria capacidade, isolamento, restore e rollback. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, cutover, retirada do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma em 2026-07-20T13:18:58Z

A retoma preservou a alteração unstaged em `docs/crm/MIGRATION.md` e o commit local `9e6a95ca79e8f8e53f095b4623da0da878a8cf12` (`feat: mark legacy CRM APIs deprecated`), que ainda não estava no remote. O commit não retira contratos: acrescenta `Deprecation: true` apenas a `/api/*` v0 e telemetria com route template e status, excluindo query strings, paths dinâmicos não reconhecidos, headers e payloads. Não inventa uma data `Sunset` nem altera auth, status ou body. Um RED adicional mostrou que a raiz exata `/api/v1` era indevidamente classificada como legada; o commit atómico `e5dcfa4` corrige a fronteira e mantém a raiz e os descendentes v1 fora da depreciação.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55504`, explicitamente marcado para testes:

```text
Teste focado de depreciação v0: 3 passed
Suite segura completa com DeprecationWarning como erro: 956 passed, 1 skipped em 113.25s, exit explícito 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff checks e Gitleaks: passed; 0 leaks em 92 commits
Imagem local: manifest list sha256:fca9053296bea734ab6d595e796bdb7d1ecd55b7dd02782ee1bef3a1881c7099
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress=404; 0 erros no log
```

A descoberta externa permaneceu read-only. Produção continua no build pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up` e o dashboard legado devolvem `200`, enquanto as novas páginas/APIs devolvem `404`. O host mantém filesystem a 87%, cerca de 3,1 GiB livres, cerca de 1,7 GiB de RAM disponível, zero bases CRM/leads, zero containers CRM e zero timers de backup CRM. O PR `#1` permanece draft, mergeable, sem reviews, checks ou environments; o Codespace técnico continua em `Shutdown`.

Em 12.602 registos JSON parseáveis das últimas 48 horas foram observados pedidos GET `2xx` não-curl ativos nos seis contratos v0: `/api/stats` 27, `/api/portfolio` 7, `/api/recommendations` 7, `/api/outreach-followups` 21, `/api/email-followups` 17 e `/api/proposal-followups` 19. Os pedidos mais recentes ocorreram em 2026-07-20T12:59:25Z. A depreciação não destrutiva é, portanto, o único avanço seguro da Tarefa 19 neste momento.

Os gates de conclusão continuam fechados pela realidade externa e temporal: paridade real falsa e conflitos sem resolução/aceitação; validação da amostra pelo owner; políticas oficiais de `Won` e retenção/scopes; mapping final de principal/papel/workspace; PostgreSQL de produção isolado com backup automático e restore real; staging final no proxy/TLS; soak/cutover; dois releases pós-cutover; ausência comprovada de consumidores v0; e aceitação dos stakeholders. Retirar v0 quebraria tráfego observado. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, cutover, retirada do legado nem criação do sentinel.

---

## Retoma autónoma em 2026-07-20T14:15:56Z

A retoma começou no `HEAD` sincronizado `33beead879b6f422cf6bae141fe0a7cf11864896`, na branch esperada, e preservou a criação staged de `tests/__init__.py`. O ficheiro torna o package de testes explícito para impedir que um package externo com o mesmo nome esconda os helpers CRM. A alteração passou Ruff, compileall, diff check e a suite completa antes de ser commitada atomicamente como `b13a157291f6c487987fd72c0711d3e12f3f2461` (`test: make repository tests an explicit package`) e publicada em `origin/feat/crm-accounts-proposals-v1`.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55505`, explicitamente marcado para testes:

```text
Suite segura completa com DeprecationWarning como erro: 956 passed, 1 skipped em 112.43s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python e no ficheiro preservado, compileall e diff checks: passed
```

O PR `#1` aponta para `b13a157`, continua draft, mergeable e sem reviews, checks, deployments ou environments GitHub. O único Codespace técnico continua em `Shutdown`, sincronizado e limpo. A produção permanece no build pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up` e o dashboard legado devolvem `200`, as novas páginas/APIs devolvem `404` e as seis APIs v0 acompanhadas devolvem `200`.

O host live mantém filesystem a 87%, 3.231.012 KiB livres, 3.915 MiB de RAM total, 1.608 MiB disponíveis e 1.452 MiB de swap em uso. Não existem bases CRM/leads nem timers de backup CRM observáveis. A telemetria estruturada das últimas 48 horas confirmou tráfego GET `2xx` não-curl ativo nos seis contratos v0: `/api/stats` 31, `/api/portfolio` 8, `/api/recommendations` 8, `/api/outreach-followups` 23, `/api/email-followups` 19 e `/api/proposal-followups` 21; o pedido mais recente ocorreu em 2026-07-20T14:04:21Z.

A implementação até à Tarefa 18 permanece verde e a depreciação não destrutiva é o único avanço seguro da Tarefa 19. Os gates de conclusão continuam materialmente fechados: paridade real falsa e conflitos sem resolução/aceitação; validação da amostra pelo owner; políticas oficiais de `Won` e retenção/scopes; mapping final de principal/papel/workspace; PostgreSQL de produção isolado com backup automático e restore do arquivo real; staging final no proxy/TLS; soak/cutover; dois releases pós-cutover; ausência comprovada de consumidores v0; e aceitação dos stakeholders. Retirar v0 quebraria tráfego observado e improvisar PostgreSQL no host partilhado violaria capacidade, isolamento, restore e rollback. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, cutover, retirada do legado nem criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma em 2026-07-20T14:47:34Z

A retoma começou no `HEAD` limpo e sincronizado `8eefd7a191f58d1df86c3bbed0c068e1c1a663b8`, na branch esperada. O plano canónico, este documento, commits, testes, migrations, processos, containers, PR, Codespace, produção, capacidade e telemetria foram reinspecionados antes de qualquer alteração. Não existia trabalho staged, unstaged ou untracked nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55506`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 956 passed, 1 skipped em 112.05s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff checks e Gitleaks: passed; 0 leaks em 96 commits
Cleanup: container, dump e base de restore removidos; porta 55506 livre
```

O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews, checks, deployments ou environments GitHub. O único Codespace técnico foi atualizado por fast-forward para o SHA exato `8eefd7a191f58d1df86c3bbed0c068e1c1a663b8`, mantendo a worktree limpa. A imagem foi construída no ambiente isolado com manifest list `sha256:108a9d959e1d535cd920ac15bd4ef56e7236b9942ad85197617bc740b95e422b`; o smoke confirmou `/up=200`, dashboard e rotas ricas fail-closed em `403`, Agent ingress desativado em `404` e zero erros de aplicação no log. O container e a imagem foram removidos, a limpeza foi verificada e o Codespace voltou a `Shutdown` sem alterações locais. Este ensaio externo do candidato exato não substitui staging persistente no proxy/TLS final nem os gates de dados e cutover.

A produção permanece no build pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/up` e o dashboard legado devolvem `200`, as novas páginas/APIs devolvem `404` e as seis APIs v0 acompanhadas devolvem `200`.

O host live mantém filesystem a 87%, 3.230.712 KiB livres, 3.915 MiB de RAM total, 1.636 MiB disponíveis, PostgreSQL 17.7 sem base CRM/leads, zero containers CRM e zero timers de backup CRM observáveis. Em 12.788 registos JSON parseáveis das últimas 48 horas foram observados pedidos GET `2xx` não-curl ativos nos seis contratos v0: `/api/stats` 30, `/api/portfolio` 7, `/api/recommendations` 7, `/api/outreach-followups` 23, `/api/email-followups` 19 e `/api/proposal-followups` 21. Os pedidos mais recentes ocorreram entre `2026-07-20T12:12:54Z` e `2026-07-20T14:04:21Z`.

A primeira tarefa formalmente incompleta permanece a Tarefa 19. O gate da própria tarefa proíbe retirar contratos com consumidores observados e exige dois releases pós-cutover, export e aceitação. Os gates anteriores de dados e cutover também continuam fechados por paridade real falsa, conflitos sem resolução/aceitação, falta de validação da amostra pelo owner, políticas oficiais de `Won` e retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção isolado com backup automático e restore do arquivo real, staging final no proxy/TLS, soak e cutover. Fazer merge, deploy, migração live, cutover, retirar o legado ou criar `.hermes/crm-revamp-complete.json` neste estado violaria gates explícitos do plano; nenhuma dessas ações foi executada.

---

## Retoma autónoma em 2026-07-20T15:21:00Z

A retoma começou no `HEAD` limpo e sincronizado `178e77d4f2b244add5397af465e0eb26ba8dcdec`, na branch esperada. Foram reinspecionados o plano canónico, este documento, commits, migrations, PR, Codespace, processos e containers antes de qualquer ação. Não existia trabalho staged, unstaged ou untracked nem processos CRM de worker, reconciler ou outbox publisher. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

A suite sem `DATABASE_URL` passou com `738 passed, 219 skipped`. A primeira invocação com PostgreSQL falhou por erro de preparação do operador: a base descartável estava vazia e as fixtures de API encontraram `relation "workspaces" does not exist`. A base foi removida e o comando correto foi repetido numa instância PostgreSQL 16 descartável exclusiva em `127.0.0.1:55521`, explicitamente marcada para testes, depois de `alembic upgrade head`:

```text
Suite segura completa com DeprecationWarning como erro: 956 passed, 1 skipped em 112.69s, exit 0
Alembic lifecycle numa segunda base exclusiva: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff check no delta Python da branch, compileall, diff check e Gitleaks: passed; 0 leaks em 143 commits
Imagem local exata: sha256:7e18005c1baefeca4c468a766ea8a72783d848d87653dd725e07399f1f59da8e
Smoke com defaults: /up=200; dashboard e Contas=403; Agent ingress=404; 0 erros de aplicação no log
Cleanup: bases, dump, containers, imagem e portas 55520-55522/58030 removidos ou livres
```

O PR `#1` continua draft, mergeable e `CLEAN`, no SHA exato da branch, sem reviews nem checks. O Codespace técnico permanece em `Shutdown`, sincronizado e limpo. A verificação HTTP live confirmou que produção continua no build pré-revamp: `/` e `/up` devolvem `200`, as novas rotas `/contas`, `/propostas`, `/inteligencia` e `/api/v1/accounts` devolvem `404`, e os seis contratos v0 acompanhados devolvem `200`.

A implementação até à Tarefa 18 e a depreciação não destrutiva da Tarefa 19 permanecem verdes. A retirada do legado e o cutover continuam proibidos pelos gates materiais já registados: não existem dois releases pós-cutover nem staging final/soak; produção não tem PostgreSQL CRM isolado com backup automático e restore real; faltam mapping final de principal/papel/workspace, políticas oficiais de `Won` e retenção/scopes, paridade real, resolução/aceitação de conflitos e validação da amostra pelo owner; os contratos v0 continuam ativos. Não houve merge, deploy, migração/backfill live, ativação de workers/conectores/outbox, cutover, retirada do legado nem criação do sentinel.

---

## Retoma autónoma em 2026-07-20T16:17:22Z

A retoma começou no `HEAD` limpo e sincronizado `0fc5634fd28dd9624b9d312c573dee73b6d81c80`, na branch esperada. O plano canónico, este documento, commits, testes, migrations, processos, containers, PR, Codespace, produção, capacidade, telemetria e pré-requisitos de cloud foram reinspecionados antes de qualquer alteração. Não existia trabalho staged, unstaged ou untracked nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Recursos preexistentes desconhecidos foram preservados.

Num PostgreSQL 16 descartável exclusivo em `127.0.0.1:55530`, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 956 passed, 1 skipped em 110.00s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
Ruff no delta Python, compileall, diff checks e Gitleaks: passed; 0 leaks em 98 commits
Imagem local exata: sha256:765fb8c26302c95ee3bbd898ff58f290482d22b632529dd2aa4f13a9d464a8ec
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress=404; 0 erros de aplicação no log
Cleanup: PostgreSQL, dump, base de restore, container de smoke e imagem removidos; portas 55530 e 58031 livres
```

O export read-only preservado fora do repositório foi verificado sem ler nem imprimir conteúdo: mode `0600`, 502.197 bytes e SHA-256 `f3a92324fc8aa3a9e187e67f2eb8cc0ac1fb5e2dc2bf5d8b12278a89ea74f9e1`. O PR `#1` continua draft, mergeable e `CLEAN`, no SHA exato da branch, sem reviews, checks, deployments ou environments GitHub. O único Codespace técnico continua em `Shutdown`, sincronizado e limpo. Não existem CLIs, credenciais cloud ou configuração observáveis para provisionar um staging final ou PostgreSQL gerido isolado.

A descoberta live permaneceu read-only. Produção continua no container saudável pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/` e `/up` devolvem `200`, as novas páginas/APIs devolvem `404` e as seis APIs v0 acompanhadas devolvem `200`. O host mantém filesystem a 87%, 3.229.524 KiB livres, 3.915 MiB de RAM total, 1.628 MiB disponíveis, zero bases/containers CRM e zero timers de backup CRM observáveis. Em 13.543 registos JSON parseáveis das últimas 48 horas foram observados pedidos `2xx` não-curl ativos: `/api/stats` 30, `/api/portfolio` 7, `/api/recommendations` 7, `/api/outreach-followups` 21, `/api/email-followups` 19 e `/api/proposal-followups` 21; os pedidos mais recentes ocorreram em 2026-07-20T14:46:31Z.

A primeira tarefa formalmente incompleta permanece a Tarefa 19. Retirar v0 agora quebraria consumidores observados e violaria os gates de ausência de consumidores, dois releases pós-cutover e aceitação. Os gates anteriores de dados e cutover continuam fechados por paridade real falsa, conflitos sem resolução/aceitação, falta de validação da amostra pelo owner, políticas oficiais de `Won` e retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção isolado com backup automático e restore do arquivo real, staging final no proxy/TLS, soak e cutover. A autorização autónoma não permite fabricar evidência humana ou temporal nem usar o host partilhado sem capacidade e isolamento. Não houve merge, deploy, migração live, ativação de workers/conectores/outbox, cutover, retirada do legado nem criação de `.hermes/crm-revamp-complete.json`.

---

## Retoma autónoma em 2026-07-20T17:18:02Z

A retoma começou no `HEAD` limpo e sincronizado `36a91fae9a1542778c7ff1a557ebcbb1d7836862`, na branch esperada. O plano canónico, este documento, commits, testes, migrations, processos, containers, PR, Codespace, produção, capacidade e telemetria foram reinspecionados antes de qualquer alteração. Não existia trabalho staged, unstaged ou untracked nem processos CRM de worker, reconciler, outbox publisher ou jobs outbound ativos. Os containers PostgreSQL preexistentes foram preservados como trabalho desconhecido.

Em dois PostgreSQL 16 descartáveis exclusivos em loopback, explicitamente marcados para testes e removidos no fim:

```text
Suite segura completa com DeprecationWarning como erro: 956 passed, 1 skipped em 111.99s, exit 0
Alembic lifecycle: base -> 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Backup custom-format restaurado: schema=0007, 15 tabelas, 0 workspaces, 0 violações
compileall e git diff --check: passed
Cleanup: containers, dump e base de restore removidos; portas 55531 e 55532 livres
```

O export read-only preservado fora do repositório continua mode `0600`, com 502.197 bytes e SHA-256 `f3a92324fc8aa3a9e187e67f2eb8cc0ac1fb5e2dc2bf5d8b12278a89ea74f9e1`. O PR `#1` continua draft, mergeable, no SHA exato da branch, sem reviews, checks, deployments ou environments GitHub. O único Codespace técnico continua em `Shutdown`, sincronizado e limpo. Os três nomes de staging inspecionados continuam sem DNS.

A descoberta live permaneceu read-only. Produção continua no container saudável pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/` e `/up` devolvem `200`, as novas páginas/APIs devolvem `404` e as seis APIs v0 acompanhadas devolvem `200`. O host tem agora cerca de 12,3 GB livres no filesystem, 1,8 GB de RAM disponível e 912 MB de swap em uso, mas continua sem base CRM identificável e sem timer de backup CRM. A melhoria de capacidade não satisfaz isolamento, PostgreSQL 16, backup/restore real, staging final ou rollback. A telemetria estruturada das últimas 48 horas continua a mostrar consumo v0 não-probe em `/api/stats` (2 pedidos às `2026-07-20T17:15Z`); portanto não existe evidência de ausência de consumidores.

A implementação até à Tarefa 18 e a depreciação não destrutiva da Tarefa 19 permanecem verdes. A retirada do legado e a conclusão global continuam materialmente bloqueadas por paridade real falsa, conflitos sem resolução/aceitação, validação da amostra pelo owner, políticas oficiais de `Won` e retenção/scopes, mapping final de principal/papel/workspace, PostgreSQL de produção isolado com backup automático e restore real, staging final no proxy/TLS, soak/cutover, dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. Não houve merge, deploy, migração/backfill live, ativação de workers/conectores/outbox, cutover, retirada do legado nem criação de `.hermes/crm-revamp-complete.json`.

---

## Staging persistente iniciado em 2026-07-20T17:32:21Z

Após autorização explícita, os workloads antigos aprovados foram removidos do host e a capacidade foi revalidada em cerca de 12 GB livres, 52% de filesystem utilizado. As aplicações preservadas continuaram ativas e os seus objetos de imagem permaneceram disponíveis; uma imagem/container de rollback foi mantida para IPIIA, Leads Dashboard e Portfolio.

Foi criado staging logicamente isolado na mesma droplet, sem alterar o serviço de produção `leads-dashboard-web`: PostgreSQL 16 dedicado em `crm-postgres-staging`, rede e volume próprios, bind apenas em loopback, limites de 0,5 CPU/512 MiB e secrets mode `0600`; aplicação `crm-staging-web` com a imagem amd64 construída do código de `36a91fae9a1542778c7ff1a557ebcbb1d7836862`, também limitada a 0,5 CPU/512 MiB. O staging foi publicado com TLS em `https://chat.zelusottomayor.com`, reaproveitando o hostname do N8N retirado. Nenhuma credencial Google foi montada. Os reads de Contas e Propostas usam PostgreSQL; writer permanece `sheet`, projeção Sheets e Agent ingress permanecem desligados.

As migrations aplicaram `base -> 0007`; `alembic current` devolveu `0007 (head)` e `alembic check` não detetou operações pendentes. Foi criado exatamente um workspace de staging. O smoke público confirmou `/up=200`, rotas protegidas sem ou com credenciais erradas em `401`, Contas/Propostas/Inteligência/Operações e APIs v1 autenticadas em `200`, `Cache-Control: no-store`, Agent ingress em `404` e zero linhas de erro de aplicação.

O export read-only preservado foi transformado numa snapshot canónica mode `0600`, com os grupos fallback documentados. A snapshot observada contém 1.247 linhas de input, 1.202 linhas elegíveis, 12 identidades duplicadas e 21 linhas sem identidade. Os backfills foram aplicados exclusivamente ao staging e repetidos com input idêntico:

```text
Accounts apply #1: 65 imports, 46 accounts criadas/associadas, 52 conflitos
Accounts apply #2: 0 imports, 65 replay no-op
Proposals apply #1: 44 imports, 4 contas sem correspondência, 37 conflitos
Proposals apply #2: 0 imports, 44 replay no-op
Persistido: 46 accounts, 46 contacts, 65 leads, 44 proposals e 44 proposal_versions
Compare: parity=false, 1 account e 1 lead em falta, 39 conflitos, 0 stage/account/source-field mismatches
```

Foi instalado um timer diário de backup custom-format com retenção local de sete dias. O arquivo pós-import foi restaurado numa base aleatória e validado com PostgreSQL 16, schema `0007`, um workspace, todas as tabelas/constraints/indexes exigidas, zero violações e zero órfãos; a base de restore foi removida. Este backup local de staging não substitui backup off-host de produção.

Produção, Sheet e contratos v0 permaneceram inalterados; não foram ativados conectores, workers, outbox, Agent ingress, command writer PostgreSQL ou cutover. Os gates materiais restantes são a resolução/aceitação dos conflitos e paridade falsa, validação humana da amostra, políticas oficiais de `Won` e retenção/scopes, mapping final de produção, backup off-host, soak, cutover faseado, dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders.

---

## Verificação do staging persistente em 2026-07-20T18:19:06Z

A retoma começou no `HEAD` limpo e sincronizado `8c70a591058ba249d8e14ab791871f688d273c1e`, na branch esperada. O plano canónico, este documento, commits, staged/unstaged work, suite, migrations, processos, containers, PR e staging foram reinspecionados. Não existia trabalho local por preservar nem processos de worker CRM, reconciler, outbox publisher ou jobs outbound ativos. Os containers PostgreSQL locais preexistentes foram preservados como trabalho desconhecido.

Num PostgreSQL 16 descartável exclusivo, explicitamente marcado para testes e removido no fim:

```text
Suite segura completa com DeprecationWarning como erro: 956 passed, 1 skipped em 110.28s, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected
Ruff no delta Python, compileall, diff checks e Gitleaks: passed; 0 leaks em 101 commits
```

O staging persistente foi atualizado para uma imagem amd64 construída do SHA exato `8c70a591058ba249d8e14ab791871f688d273c1e`, com image ID remoto `sha256:13b363bf694ac9db84f2ab9f2268eab70c62cccade2b3472f5782b1b57c14e38`. A imagem anterior `36a91fa` permaneceu disponível para rollback. PostgreSQL e aplicação ficaram `healthy`, sem restarts; uma amostra de recursos observou cerca de 83 MiB para a aplicação e 66 MiB para PostgreSQL dentro dos limites de 512 MiB.

O smoke no proxy/TLS final confirmou `/up=200`, rich reads sem credenciais em `401`, Agent ingress por POST desativado em `404`, e Contas, Propostas, Inteligência, Operações e APIs v1 autenticadas em `200` com `Cache-Control: no-store`. O browser smoke Playwright carregou as quatro áreas autenticadas sem erros de consola. Um soak monitorizado de `2026-07-20T17:49:11Z` a `2026-07-20T18:19:06Z` executou 360 pedidos autenticados, manteve aplicação e PostgreSQL healthy e encontrou zero linhas de erro/exception/traceback no log.

O dump custom-format de staging foi copiado off-host para `/Users/max/.hermes/profiles/marketing-max/backups/crm/staging/crm-staging-20260720T173130Z.dump`, com mode `0600`, 195.355 bytes e SHA-256 `910bec28d47d8889b114ae6f5690ea7b68d3579416897e5e8fc74170ad8a5597`. O arquivo exato foi restaurado num segundo PostgreSQL 16 descartável e validado com schema `0007`, 15 tabelas, um workspace e zero violações. O timer diário local do host continua ativo; esta cópia manual off-host ainda não equivale a uma política automática off-host de produção.

As invariantes agregadas do staging permanecem verdes: zero leads com rank 40+ sem account, zero zeros sintéticos em propostas `missing`, zero eventos failed/dead-letter e zero outbox pendente. Foram preparados dois CSVs privados mode `0600` fora do repositório, em `/Users/max/.hermes/profiles/marketing-max/backups/crm/review/`, com 46 linhas de contas e 44 propostas para validação do owner; a engenharia não marcou essa validação humana como concluída.

A implementação e o staging técnico estão verdes, mas o plano global não está completo. A comparação real continua `parity=false` com conflitos não resolvidos/aceites; faltam a validação comercial da amostra, política oficial de `Won` e retenção/scopes, mapping final de produção, PostgreSQL de produção com backup automático off-host e restore real, cutover e soak de produção. A Tarefa 19 exige depois dois releases estáveis, telemetria sem consumidores v0 e aceitação dos stakeholders. Estes gates humanos, de produção e temporais não podem ser fabricados pela autorização autónoma. Produção e Sheet permaneceram inalteradas, e o sentinel não foi criado.

---

## Retoma autónoma e lista operacional de Leads em 2026-07-20T19:00:42Z

A retoma começou no `HEAD` limpo e sincronizado `9b1aec0a030ff8665a1e9456d3e6cda62cca2449`, na branch esperada. O plano canónico, este documento, commits, staged/unstaged work, suite, migrations, processos, containers, PR, staging, produção, backup e telemetria foram reinspecionados antes da alteração. O trabalho intermédio encontrado durante a execução foi preservado e fechado em dois commits atómicos: `0e71274c0a3cea230700a26cbc2e0eba8b0dc687` (`feat: add compact operational leads list`) e `8e71ae751746d7c2301d087f1f76ae56e721a7b6` (`fix: expose lead progress in operational list`).

O novo `GET /api/v1/leads` é protegido pela mesma dependency server-side e pelo mesmo scope de workspace das Contas, pagina a um máximo de 100 registos e devolve apenas os campos operacionais necessários. `GET /leads` apresenta pesquisa local, filtro de estado, estados loading/empty/error, empresa/contacto, fase, contagem de propostas, atualização, próxima ação e ligação para a conta. Quando o adapter legado está indisponível e os reads de Contas usam PostgreSQL, `/` encaminha para `/leads` em vez de servir um dashboard legado sem dados. A página e a API mantêm `Cache-Control: no-store`.

O RED válido foi observado enquanto o tracer bullet estava incompleto: o teste da API recebeu `404` em `/api/v1/leads`; depois da implementação, o teste da página encontrou `TemplateNotFound: leads/index.html`. O candidato completo passou:

```text
Suite segura completa no SHA final 8e71ae7, com DeprecationWarning como erro: 959 passed, 1 skipped em 114,12 s, exit 0
Regressão focada pós-commit no SHA 8e71ae7: 77 passed, exit 0
Alembic lifecycle: 0007 -> base -> 0007
Alembic current: 0007 (head)
Alembic check: No new upgrade operations detected, exit 0
Ruff nos ficheiros alterados, node --check, compileall, git diff --check e Gitleaks: passed
Restore do dump off-host de staging: schema 0007, 15 tabelas, 1 workspace, 0 violações
```

O staging persistente foi atualizado para a imagem `crm-staging:8e71ae7`, image ID `sha256:e7863ee20fe57398b8f7a537a2f46791bc50f7090e1156c3c42596bfcf007312`, e permaneceu healthy sem restarts. O smoke autenticado no proxy/TLS confirmou `200` em Leads, Contas, Propostas, Inteligência, Operações e APIs v1, todos com `no-store`; a API devolveu 65 leads, 46 contas e 44 propostas. O browser Playwright seguiu `/` até `/leads`, renderizou 65 linhas, quatro colunas e 46 ligações para contas, sem erros de consola ou requests falhados. Um soak adicional do SHA exato executou 90 pedidos a `/up`, `/leads` e `/api/v1/leads`, com 90 sucessos, zero falhas, zero restarts e health verde.

As invariantes agregadas de staging permanecem verdes: 46 contas, 46 contactos, 65 leads, 44 propostas e 44 versões; zero leads com rank 40+ sem conta; zero valores sintéticos em propostas `missing`; zero eventos failed/dead-letter; zero outbox pendente. O timer diário de backup de staging está ativo. A cópia off-host mode `0600`, com 195.355 bytes e SHA-256 `910bec28d47d8889b114ae6f5690ea7b68d3579416897e5e8fc74170ad8a5597`, foi novamente restaurada e validada num PostgreSQL 16 descartável.

Produção continua no build pré-revamp `7622a2b2b8d5e0790858208b2c3a1f119edb7328`: `/` e `/up` devolvem `200`, as novas páginas/APIs devolvem `404` e os contratos v0 continuam ativos. A telemetria normalizada das últimas 48 horas, excluindo `curl`, observou pedidos `2xx` em `/api/stats` (14), `/api/portfolio` (1), `/api/recommendations` (1), `/api/outreach-followups` (12), `/api/email-followups` (11) e `/api/proposal-followups` (12), com tráfego até `2026-07-20T18:39:09Z`.

O candidato local, o staging e o rollback técnico permanecem verdes, mas os gates de conclusão continuam materialmente fechados: `parity=false` e conflitos sem resolução/aceitação; validação da amostra pelo owner; políticas oficiais de `Won` e retenção/scopes; mapping final de principal/papel/workspace; PostgreSQL de produção com backup automático off-host e restore real; cutover e soak de produção. A retirada v0 exige ainda dois releases pós-cutover, ausência comprovada de consumidores e aceitação dos stakeholders. Não houve merge, deploy de produção, migração/backfill live, ativação de workers/conectores/outbox, cutover, retirada do legado ou criação de `.hermes/crm-revamp-complete.json`.

---

## Filas futuras e filtro de prioridade em 2026-07-21T07:29:00Z

A retoma preservou nove ficheiros staged/unstaged sobre `1245172a94f519d5a8672990a264cafe0697cfb3`, verificou o candidato exato e publicou-o atomicamente como `84796c305047b69b5e9fe2c0320a91d8e39c7da7` (`feat: complete future pipeline queues and priority filter`).

O pipeline operacional acrescenta filas `calls_future` e `emails_future`, definidas estritamente depois do fim do dia local da workspace. As filas de hoje continuam a incluir o limite do fim do dia. O resumo conta leads distintos, enquanto a listagem preserva tarefas individuais e pagina de forma determinística por prazo, task ID e lead ID. O filtro server-side de prioridade aceita apenas `low`, `medium` ou `high`, é aplicado antes da contagem/paginação e a UI envia-o sem alterar a pesquisa e o filtro de fase locais. O isolamento por workspace e a autenticação existente foram preservados.

Evidência local num PostgreSQL 16 descartável, migrado de raiz até `0009`:

```text
Suite segura completa com DeprecationWarning como erro: 1047 passed, 1 skipped em 170,36 s
Regressão focada pós-commit: 21 passed
Alembic lifecycle: 0009 -> base -> 0009
Alembic current: 0009 (head)
Alembic check: No new upgrade operations detected
Ruff, format check, node --check, compileall, diff checks e Gitleaks: passed
```

O backup custom-format de staging anterior ao deploy foi copiado off-host para `/Users/max/.hermes/profiles/marketing-max/backups/crm/staging/crm-staging-20260721T072609Z-pre-84796c3.dump`, com mode `0600`, 197.794 bytes e SHA-256 `fe957af8c2edc454acab3c690796931b874343169fa4516cf16308d88ec0565f`. O arquivo exato foi restaurado num PostgreSQL 16 descartável e validado com schema `0009`, 15 tabelas, uma workspace e zero violações.

O staging persistente foi atualizado para `crm-staging:84796c3`, com revisão exata `84796c305047b69b5e9fe2c0320a91d8e39c7da7`; a imagem anterior `crm-staging:1245172` permanece disponível para rollback. Aplicação e PostgreSQL ficaram healthy, sem restarts. O proxy/TLS confirmou `/up=200`, rotas ricas sem credenciais em `401`, páginas/APIs autenticadas em `200` com `no-store`, filas futuras disponíveis e Agent ingress desligado. O browser smoke confirmou os dois controlos de fila e um request server-side com `priority=high`, sem erros de consola ou requests falhados. O soak executou 360/360 pedidos com sucesso, zero restarts e zero linhas de erro de aplicação.

A matriz ainda contém capacidades operacionais `partial`/`missing`, e os gates globais continuam fechados por `parity=false`, conflitos reais sem resolução/aceitação, validação humana da amostra, políticas oficiais de `Won` e retenção/scopes, mapping final de produção, PostgreSQL/backup automático off-host de produção, cutover/soak de produção, dois releases pós-cutover, ausência comprovada de consumidores v0 e aceitação dos stakeholders. Não houve deploy de produção, migração live, outbound, retirada do legado ou criação do sentinel.

---

## Identidade operacional de Leads pré-conta em 2026-07-21T11:30:08Z

A retoma preservou integralmente o trabalho staged encontrado sobre `864d526627fa2d417282496cb2bce1abdb43d175` e concluiu o slice de Leads anteriores ao milestone de criação de Account:

- a migration aditiva `0011` acrescenta empresa, contacto, email, telefone e cidade nullable diretamente ao Lead, com constraints nonblank;
- o downgrade preserva dados: recusa eliminar as colunas quando qualquer identidade pré-conta está preenchida e mantém a revisão `0011` transacionalmente;
- o backfill mantém identidade operacional em Leads abaixo de rank 40 sem criar Accounts artificiais;
- lista, detalhe e pesquisa usam Account/Contact quando existem e fazem fallback para a identidade do Lead pré-conta;
- o comando de edição atualiza prioridade e identidade pré-conta sem criar Account/Contact;
- uma transição humana para `meeting_booked` ou fase posterior cria/associa Account e Contact apenas quando empresa e email fornecem identidade exata; conflitos falham atomicamente;
- o backup verifier e o runbook passam a exigir schema `0011`.

Evidência local num PostgreSQL 16 descartável exclusivo, migrado de raiz:

```text
Regressão focada de migration/backfill/pipeline/comandos/backup: 80 passed
Migration/backup/comando focado em base limpa: 21 passed
Suite segura completa com DeprecationWarning como erro: 1060 passed, 1 skipped em 142,38 s
Alembic lifecycle: 0011 -> 0010 -> 0011 -> base -> 0011
Alembic current: 0011 (head)
Alembic check: No new upgrade operations detected
Restore custom-format: schema 0011, 15 tabelas, 0 workspaces, 0 violações
Ruff, format, compileall, node --check, git diff --check e scan estático: passed
```

Esta evidência é local. O staging continua no schema/imagem anterior até o candidato ser congelado, revisto e publicado. Nenhum worker, reconciler, outbox publisher ou job outbound foi ativado; não houve write em Sheet, deploy de produção, cutover, retirada do legado nem criação do sentinel.

Uma regressão adicional isolou conflito entre email exato e nome de empresa divergente sem depender de diferenças de telefone/cidade: o primeiro run devolveu `200` em vez de `409`; a correção exige concordância do nome normalizado antes de associar a Account existente. O teste focado e o módulo completo de comandos passaram depois da correção. Foram também reparadas, sem descartar o trabalho staged herdado, uma expressão incompleta no serviço de transição e referências inconsistentes no update do backfill; `compileall` e a regressão completa acima foram executados sobre o candidato reparado.

Um segundo RED isolou um Lead com Account mas sem Contact ligado: o primeiro run aceitava a edição e voltava a guardar identidade de contacto no próprio Lead. O serviço agora exige que Account e Contact estejam ambos ligados ou ambos ausentes; estados parciais falham com conflito genérico e rollback integral. O teste focado, o módulo completo de operações e a suite completa passaram depois da correção.

Um terceiro RED mostrou que o backfill accountful limpava o telefone do Lead sem o copiar para o Contact canónico. O apply agora preenche telefone ausente no Contact e envia valores contraditórios para review em vez de os sobrescrever; o teste focado, o módulo completo de backfill e a suite completa passaram depois da correção.

Um quarto RED cobriu o estado transitório em que o Lead já tinha uma Account exata mas ainda não tinha Contact ligado. A transição para `meeting_booked` devolvia sucesso, limpava a identidade pré-conta e deixava `contact_id` nulo. O serviço agora valida a Account pelo nome normalizado, cria ou associa o Contact pelo email exato, preserva o telefone sem sobrescrever conflitos e só depois limpa os campos transitórios. O RED observado foi `1 failed`; depois da correção o teste passou e a regressão de comandos/operações/backfill passou com `42 passed`.

### Verificação do candidato reparado em 2026-07-21T12:50:44Z

Num PostgreSQL 16 descartável exclusivo em loopback, explicitamente marcado para testes e sem dados reais:

```text
Suite segura completa sobre o candidato formatado, com DeprecationWarning como erro: 1061 passed, 1 skipped
Regressão pós-format de comandos/operações/backfill/migration 0011: 42 passed
Alembic lifecycle: 0011 -> 0010 -> 0011 -> base -> 0011
Alembic current: 0011 (head)
Alembic check: No new upgrade operations detected
Restore custom-format: schema 0011, 15 tabelas, 0 workspaces, 0 violações
Ruff e git diff --check: passed nos ficheiros reparados
```

O candidato está pronto para congelamento e revisão independente antes do commit. O staging permanece no schema/imagem anterior até revisão e publicação deste slice. Nenhum worker, reconciler, outbox publisher ou job outbound foi ativado; não houve write em Sheet, deploy de produção, cutover, retirada do legado nem criação do sentinel.
## Paridade operacional local concluída em 2026-07-21

A implementação do plano `CRM-PIPELINE-OPERATIONAL-PARITY-PLAN.md` foi concluída localmente na branch `feat/crm-pipeline-parity`. O candidato funcional anterior a esta atualização documental era `64e2f1aeb3658303d18b4a0f9221a55dc23c5816`; nenhum commit desta branch foi enviado, merged ou publicado.

O novo workspace preserva Leads como unidade operacional e acrescenta Contas, Propostas e Inteligência sem substituir o fluxo diário. Estão implementados:

- filas server-side de chamadas, emails, propostas, tarefas vencidas, tarefas de hoje e trabalho futuro, com cardinalidade explícita;
- detalhe e timeline por Lead, ações humanas auditáveis, tarefas, outcomes, edição e `expected_version`;
- `Guardar e seguinte` com sucessor capturado antes do write e avanço apenas após confirmação; `Saltar` sem writes nem wrap;
- latest-request-wins para seleção, filtros e paginação, mantendo sucesso de writes quando apenas o refresh posterior falha;
- workspace desktop e mobile validado a 390×844 e 320×844, sem overflow de página e com touch targets operacionais;
- analytics acionáveis, agregados seguros e tempo em fase derivado exclusivamente de `Activity.from_stage`/`to_stage`, com cobertura explícita e ordem canónica;
- replay idempotente ligado ao ator original, CSRF/origin, isolamento por workspace, auditoria e outbox transacional;
- Propostas com proveniência de evidência por thread, estados terminais fail-closed, `won` bloqueado no comando genérico e before/after completo das mutações comerciais;
- retry manual do formulário de Propostas que reutiliza `command_id` após resposta ambígua, sem repetir writes automaticamente;
- backfill operacional dry-run por omissão, determinístico, atómico, DST fail-closed e sem matching destrutivo por nome;
- migrations fail-closed até `0011`, incluindo factos estruturados de fase e constraint PostgreSQL `ck_leads_stage_requires_account`;
- verificador de backups alinhado com `0011`, constraints obrigatórias, invariantes accountless e preservação do erro primário quando o cleanup também falha.

### Evidência do gate final

Numa base PostgreSQL 16 descartável e exclusiva em loopback, criada de raiz:

```text
Alembic upgrade: base -> 0011 (head)
Alembic check: No new upgrade operations detected
Suite Python completa exclusiva: 1185 passed, 1 skipped em 225,12 s
Frontend Node: 27 passed
Ruff e format check: 40 ficheiros Python alterados passaram
compileall, node --check e git diff --check: passed
Revisão independente dos cinco blockers finais: PASS
```

O primeiro ensaio completo concorrente com uma revisão independente foi invalidado por contaminação da mesma base de testes. Depois de remover qualquer segundo processo, os seis casos afetados passaram juntos numa base nova e a suite completa exclusiva passou integralmente. A fixture de Operações também deixou de fabricar um Lead sem Conta em `meeting_booked`, estado agora corretamente impossível sob `0011`.

A imagem operacional do dashboard foi construída a partir de `dashboard/Dockerfile`. O smoke do candidato funcional confirmou:

```text
/up: 200
/leads, /contas, /propostas, /inteligencia e API sem autenticação: 401
WWW-Authenticate: Basic
Cache-Control em conteúdo protegido: no-store
/static/proposals.js autenticado: 200, ETag presente, caching normal
container: healthy
```

### Gates ainda fechados

Esta conclusão é de implementação e verificação local, não de rollout. O staging existente não foi atualizado por esta branch. Permanecem proibidos sem aprovação e validação próprias:

- push, merge ou deploy deste candidato;
- `--apply` do backfill operacional: o dry-run conhecido mantém 187 casos para revisão;
- resolução automática dos conflitos de dados e declaração de paridade real;
- ativação de writes PostgreSQL, conectores, workers, outbox publisher, Agent ingress ou projeção Sheets;
- cutover ou retirada do CRM legado/Sheet.

A fase seguinte é preparar uma validação de staging fail-closed, com identidade real e revisão do proprietário, mantendo o legado como fallback. O cutover continua bloqueado por paridade de dados, validação humana, backup off-host/restore, soak e aprovação explícita.

---

## Retoma do candidato integrado em 2026-07-21T16:25:32Z

A retoma partiu do `HEAD` sincronizado `d30f6f6dd3589d5429f3577450d6e91861f02495` e preservou integralmente 53 ficheiros staged, incluindo correções posteriores unstaged nas migrations e respetivos testes. O candidato integrado mantém os IDs já publicados `0010` e `0011`, acrescenta `0012` para factos estruturados de transição e `0013` para a invariante de Account obrigatória, e reúne o workspace operacional, analytics, idempotência browser, backfill legado e constraints PostgreSQL sem retirar contratos v0.

Num PostgreSQL 16 descartável e exclusivo em loopback, sem dados reais ou credenciais live:

```text
Suite Python segura completa com DeprecationWarning como erro: 1200 passed, 1 skipped em 250,38 s
Frontend Node: 28 passed
Regressão transacional isolada após contaminação de uma ordem focada: 12 passed
Alembic lifecycle: base -> 0013 -> 0011 -> 0013 -> base -> 0013
Alembic current: 0013 (head)
Alembic check: No new upgrade operations detected
Restore custom-format: schema 0013, 15 tabelas, 0 workspaces, 0 violações
Ruff, format check, compileall, node --check e git diff --check: passed
Gitleaks no histórico e no candidato staged: zero leaks
Imagem local: manifest list sha256:5697aae4d4311384e226e3d472c6e4b5d332bb3094b01a181c736244f7c03a98
Smoke com defaults: /up=200; dashboard e rotas ricas=403; Agent ingress=404; zero linhas de erro no log
Smoke autenticado com PostgreSQL: Leads, Contas, Propostas, Inteligência, Operações e APIs v1=200; pedidos sem credenciais=401
Backfill operacional real em dry-run: 1.247 linhas, 38 tarefas candidatas, 269 notas candidatas, 187 conflitos, zero writes
```

A falha inicial de três testes transacionais resultou de executar módulos independentes numa ordem que deixou Activities append-only de outros módulos na mesma base. A repetição exclusiva do módulo numa base nova passou; a suite completa canónica, também exclusiva e criada de raiz, passou integralmente. Nenhum worker CRM, reconciler, outbox publisher ou job outbound estava ativo. O candidato ainda necessita congelamento final, revisão independente, commit/publicação e validação no staging persistente antes de qualquer decisão de cutover. O sentinel permanece ausente.
