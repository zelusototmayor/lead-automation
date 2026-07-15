# Semântica inicial de estágios do CRM

> **Status:** semântica inicial aprovada no plano. Ainda depende de inventário somente leitura dos estágios reais e de confirmação humana antes de ser tratada como contrato definitivo. Este documento não resulta de consulta ao Google Sheets.

## Princípios

- A ordenação é **explícita e não lexicográfica**: nunca comparar nomes de estágio alfabeticamente para determinar progressão.
- Cada estágio canônico possui uma ordem numérica definida no catálogo abaixo.
- A maior ordem já alcançada (`highest_rank`) é monotônica: nunca diminui, inclusive ao entrar em um estágio terminal.
- Antes de resolver um alias, normalizar apenas caixa e espaços: remover espaços nas extremidades, condensar sequências de whitespace e comparar sem distinção entre maiúsculas e minúsculas.
- Um estágio desconhecido não recebe ordem inferida nem alias aproximado: deve ir para **revisão**.
- Estágios terminais não progridem automaticamente para outro estágio.

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

Portanto, a ordem terminal 90 nunca pode ser usada como evidência para decidir se `lost` ou `not_a_fit` exige conta. Uma progressão de um terminal para outro terminal é inválida no fluxo normal; a troca só pode ocorrer como evento explícito de correção revisada. Se o alvo da correção for `won`, validar e associar exatamente uma conta canônica, independentemente da decisão terminal original. Se o alvo for `lost` ou `not_a_fit`, usar o `previous_highest_rank` capturado ou a decisão de `requires_account` persistida na primeira entrada terminal, nunca a ordem atual 90. Na falta dessa evidência pré-terminal original, encaminhar a correção para revisão humana sem inferir. Uma correção nunca remove silenciosamente associações de conta existentes.

Uma linha terminal importada sem histórico anterior de estágios deve ir para revisão humana. Nesse caso, não inferir `requires_account` a partir da ordem terminal 90 nem presumir um `previous_highest_rank`. Uma eventual materialização futura separada desse valor histórico pode ser considerada como otimização, mas não constitui requisito do esquema atual.

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

Qualquer valor vazio, não catalogado ou que não corresponda exatamente a um estágio canônico/alias após a normalização deve ser marcado para **revisão humana**. Não usar distância textual, ordenação lexical, probabilidade padrão ou posição da linha para inferir o estágio.

## Validação pendente

Antes de consolidar este catálogo como contrato definitivo:

1. produzir um inventário somente leitura dos valores de estágio reais;
2. comparar valores e frequências com este catálogo, sem alterar a fonte;
3. encaminhar desconhecidos e possíveis aliases para revisão;
4. obter confirmação humana da semântica, em especial dos terminais com conta condicional.
