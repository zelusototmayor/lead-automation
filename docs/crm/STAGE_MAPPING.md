# Semântica inicial de estágios do CRM

> **Status:** o catálogo canónico e a política descritos neste documento estão implementados como contrato de domínio. O inventário read-only de 2026-07-17 está registado no fim do documento. Valores desconhecidos continuam em revisão e não foram promovidos a aliases por inferência.

## Princípios

- A ordenação é **explícita e não lexicográfica**: nunca comparar nomes de estágio alfabeticamente para determinar progressão.
- Cada estágio canônico possui uma ordem numérica definida no catálogo abaixo.
- A maior ordem já alcançada (`highest_rank`) é monotônica: nunca diminui, inclusive ao entrar em um estágio terminal.
- Toda ordem histórica fornecida deve ser um inteiro exato (booleanos não são aceitos) entre 0 e `MAX_STAGE_RANK`, a maior ordem permitida derivada do catálogo e atualmente igual a 90; esse máximo serve somente como limite de validação, e valores acima dele são inválidos e não podem contaminar o histórico monotônico.
- As ordens semanticamente contaminadas por estágios terminais formam o conjunto imutável `TERMINAL_STAGE_RANKS`, derivado separadamente das definições com `terminal=True` e atualmente igual a `{90}`; esse conjunto não é definido pelo valor de `MAX_STAGE_RANK`.
- Antes de resolver um alias, normalizar apenas caixa e espaços: remover espaços nas extremidades, condensar sequências de whitespace e comparar sem distinção entre maiúsculas e minúsculas.
- Um estágio desconhecido não recebe ordem inferida nem alias aproximado: deve ir para **revisão**.
- Qualquer saída de um estágio terminal para um alvo diferente exige uma correção revisada explícita; repetir o mesmo terminal é idempotente.

## Catálogo canônico não lexicográfico

| Estágio canônico | Ordem | Terminal | Exige conta |
|---|---:|:---:|---|
| `new` | 10 | não | não |
| `contacted` | 20 | não | não |
| `qualified` | 30 | não | não |
| `meeting_booked` | 40 | não | sim |
| `meeting_held` | 50 | não | sim |
| `proposal_requested` | 60 | não | sim |
| `proposal_sent` | 70 | não | sim |
| `negotiation` | 80 | não | sim |
| `won` | 90 | sim | sim |
| `lost` | 90 | sim | sim somente se a maior ordem anterior for `>= 40` |
| `not_a_fit` | 90 | sim | sim somente se a maior ordem anterior for `>= 40` |

`Exige conta` explicita o invariante central `requires_account`. Quando seu valor for `sim`, a transação deve terminar associada a **exatamente uma conta canônica**; os estágios marcados como `não` não impõem essa associação. O valor de ordem 90 agrupa os terminais e não estabelece precedência entre eles.

## Semântica de transição para terminais

Ao processar uma transição, aplicar esta ordem:

1. capturar `previous_highest_rank` a partir de `highest_rank` **antes** de aplicar a ordem do novo estágio;
2. para entrada em `lost` ou `not_a_fit`, calcular `requires_account` usando exclusivamente `previous_highest_rank`: o valor é `sim` se e somente se `previous_highest_rank >= 40`;
3. validar a associação a exatamente uma conta canônica quando `requires_account` for `sim`;
4. somente depois atualizar a maior ordem monotônica para `highest_rank = max(previous_highest_rank, 90)`.

Portanto, toda ordem presente em `TERMINAL_STAGE_RANKS` — atualmente 90 — nunca pode ser usada como evidência para decidir se `lost` ou `not_a_fit` exige conta. Se o catálogo ganhar no futuro uma ordem máxima maior, por exemplo 100, isso não reabilita a ordem 90 como evidência pré-terminal: 90 continua contaminada enquanto pertencer ao conjunto derivado dos estágios terminais. Qualquer saída de `won`, `lost` ou `not_a_fit` para um estágio diferente — terminal ou não terminal — é inválida no fluxo normal; a troca só pode ocorrer como evento explícito de correção revisada com `reviewed_correction=True`. A repetição do mesmo estágio terminal permanece idempotente e não exige essa autorização. Uma transição normal de um estágio não terminal para um terminal continua permitida, sujeita à política de conta do alvo. Se o alvo da correção for `won`, validar e associar exatamente uma conta canônica, independentemente da decisão terminal original. Se o alvo for `lost` ou `not_a_fit`, usar o `previous_highest_rank` capturado ou a decisão de `requires_account` persistida na primeira entrada terminal, nunca uma ordem pertencente a `TERMINAL_STAGE_RANKS`. Na falta dessa evidência pré-terminal original, encaminhar a correção para revisão humana sem inferir. Uma correção nunca remove silenciosamente associações de conta existentes.

Uma linha terminal importada sem histórico anterior de estágios deve ir para revisão humana. Nesse caso, não inferir `requires_account` a partir da ordem terminal 90 nem presumir um `previous_highest_rank`. Uma eventual materialização futura separada desse valor histórico pode ser considerada como otimização, mas não constitui requisito do esquema atual.

Quando `lost` ou `not_a_fit` recebe simultaneamente uma ordem pré-terminal original utilizável (0 a 89) e uma decisão terminal persistida, as evidências devem concordar: ordens abaixo de 40 correspondem a `False`, e ordens a partir de 40 correspondem a `True`. Divergência produz `ConflictingAccountEvidenceError` e exige tratamento explícito. A ordem 90 continua inutilizável como evidência original; sem decisão persistida ela exige revisão, mas, quando acompanhada de um booleano persistido válido, esse booleano é aceito para correção ou leitura do estado terminal atual, sem comparação semântica com 90.

## Aliases iniciais

A resolução abaixo ocorre depois da normalização de caixa e whitespace.

| Entrada/alias documentado | Estágio canônico |
|---|---|
| `Meeting Booked` | `meeting_booked` |
| `Proposal Sent` | `proposal_sent` |
| `Won` | `won` |

Exemplos equivalentes pela normalização:

- ` meeting booked `, `MEETING BOOKED` e `Meeting   Booked` resolvem para `meeting_booked`.
- ` proposal sent ` e `PROPOSAL SENT` resolvem para `proposal_sent`.
- ` won ` e `WON` resolvem para `won`.

Nomes canônicos também são aceitos após a mesma normalização de caixa e espaços. Nenhum sinônimo adicional deve ser inventado sem confirmação no inventário real.

## Tratamento de desconhecidos

Qualquer valor vazio, não catalogado ou que não corresponda exatamente a um estágio canônico/alias após a normalização deve ser marcado para **revisão humana**. Não usar distância textual, ordenação lexical, probabilidade padrão ou posição da linha para inferir o estágio. O diagnóstico de `UnknownStageError` é deliberadamente genérico (`unknown CRM stage; review required`) e nunca ecoa o valor externo completo, evitando expor PII ou segredos em logs; consumidores devem reagir ao tipo da exceção, não analisar sua mensagem.

## Contrato implementado

O enum canônico `CRMStage` está em `src/crm/domain/enums.py`. O catálogo imutável `STAGE_CATALOG`, a definição imutável `StageDefinition`, o limite público `MAX_STAGE_RANK`, o conjunto público imutável `TERMINAL_STAGE_RANKS` e as funções puras de política estão em `src/crm/domain/stage_policy.py`:

- `normalize_stage` remove whitespace das extremidades, condensa todo whitespace interno e aplica `casefold`;
- `resolve_stage` aceita somente um `CRMStage`, um nome canônico `snake_case` ou um dos três aliases documentados;
- `stage_rank` e `is_terminal_stage` consultam o catálogo explícito;
- `requires_account` valida globalmente qualquer `previous_highest_rank` fornecido e qualquer `persisted_terminal_requires_account` antes de aplicar a semântica do estágio; para estágios incondicionais, evidências válidas podem ser ignoradas, enquanto `lost`/`not_a_fit` exigem concordância entre duas evidências utilizáveis;
- `MAX_STAGE_RANK` é derivado de todas as ordens do catálogo e limita a validação; `TERMINAL_STAGE_RANKS` é um `frozenset` derivado somente das definições terminais e identifica ordens inutilizáveis como evidência pré-terminal, independentemente do máximo;
- `highest_stage_rank` valida uma ordem anterior inteira exata no intervalo inclusivo de 0 a `MAX_STAGE_RANK` e retorna o máximo monotônico;
- `validate_transition` exige que `reviewed_correction` seja exatamente booleano e rejeita qualquer saída de um terminal para um alvo diferente sem `reviewed_correction=True`; a repetição do mesmo terminal é idempotente.

Falhas de domínio são tipadas, para que consumidores não analisem mensagens de erro: `UnknownStageError` identifica entrada vazia/desconhecida com diagnóstico genérico; `AccountRequirementReviewRequired` exige revisão quando falta evidência pré-terminal original; `ConflictingAccountEvidenceError` identifica divergência entre ordem original utilizável e decisão terminal persistida; `InvalidTransitionError` identifica transição terminal inválida; `InvalidCorrectionFlagError` identifica qualquer `reviewed_correction` que não seja exatamente `True` ou `False` (inclusive `None`, pois o padrão da API já é `False` explícito); `InvalidHighestRankError` identifica histórico de ordem fora do tipo ou intervalo aceito. Sempre que `persisted_terminal_requires_account` for fornecido, seu tipo é validado para todos os estágios antes de qualquer retorno de política; valores não booleanos emitem `InvalidAccountEvidenceError`.

Em correções revisadas, `validate_transition` autoriza somente a troca de fase. Essa autorização nunca dispensa a chamada independente a `requires_account`: `won` continua exigindo conta; `lost`/`not_a_fit` continuam exigindo `previous_highest_rank` original menor que 90 ou a decisão terminal original persistida. A ordem terminal atual 90, isoladamente, produz `AccountRequirementReviewRequired` e nunca é convertida em evidência.

## Validação pendente para aliases adicionais

Antes de ampliar o conjunto documentado de aliases:

1. produzir um inventário somente leitura dos valores de estágio reais;
2. comparar valores e frequências com este catálogo, sem alterar a fonte;
3. encaminhar desconhecidos e possíveis aliases para revisão;
4. obter confirmação humana antes de modificar o contrato.

## Inventário read-only observado em 2026-07-17

Uma leitura sem writes da tab `PT Logistics` encontrou 1.247 linhas e o seguinte inventário agregado do campo real `Stage`:

| Valor observado | Contagem | Tratamento atual |
|---|---:|---|
| vazio | 993 | revisão/unmapped, salvo quando existe data de proposta legada |
| `Email Sent` | 96 | revisão/unmapped, salvo quando existe data de proposta legada |
| `No Answer` | 44 | revisão/unmapped |
| `Call Back` | 28 | revisão/unmapped |
| `Not a Fit` | 28 | terminal; sem histórico pré-terminal continua em revisão |
| `Lost` | 20 | terminal; sem histórico pré-terminal continua em revisão |
| `New` | 20 | `new` pelo nome canónico |
| `Proposal Sent` | 10 | `proposal_sent` pelo alias aprovado |
| `Contacted` | 4 | `contacted` pelo nome canónico |
| `Meeting Booked` | 2 | `meeting_booked` pelo alias aprovado |
| `Send Email` | 1 | revisão/unmapped |
| `Warm` | 1 | revisão/unmapped |

O adaptador de migração aceita explicitamente `Stage` como nome legado alternativo de `Status`, sem transformar os valores desconhecidos em aliases. Se ambos os campos existirem e resolverem para fases diferentes, a linha entra em revisão por conflito.

Uma data não vazia em `Proposal Sent` é tratada como evidência legada suficiente para criar a associação de conta em shadow mode e elevar a fase efetiva para pelo menos `proposal_sent`. A proposta continua marcada `legacy_unverified`, o valor vazio continua `NULL/missing` e o valor original de `Stage` permanece em `source_stage_raw`. Esta regra não converte `Email Sent`, `No Answer`, `Call Back`, `Send Email` ou `Warm` em aliases globais.
