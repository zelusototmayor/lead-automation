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
