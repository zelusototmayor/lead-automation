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
