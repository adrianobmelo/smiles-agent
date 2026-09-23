# SMILES_AGENT

Rotina diária que testa a conexão com Smiles e Telegram, coleta as promoções
públicas da Smiles, consulta tarifas em milhas das rotas de `rotas.yaml`,
compara com `historico.json` e manda as promoções novas no Telegram.

Roda pelo GitHub Actions (`.github/workflows/smiles-agent.yml`) todo dia às
11:00 UTC, que corresponde a 08:00 em Brasília. Também dá pra disparar na mão
em Actions → Smiles Agent → Run workflow. O GitHub só executa agendamentos que
estão no branch padrão do repositório, então o workflow precisa estar no
`main` para o cron valer.

## Arquivos

| Arquivo | Função |
|---|---|
| `agent.py` | rotina completa (passos 1 a 11) |
| `telegram_send.py` | envio via `sendMessage` com `parse_mode=HTML` |
| `rotas.yaml` | janela, queda percentual e rotas monitoradas |
| `historico.json` | observações dos últimos 14 dias e alertas já enviados |
| `tests/` | testes offline, sem rede |

## Secrets do repositório

Em Settings → Secrets and variables → Actions:

- `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`: sem eles o agente roda, grava o
  histórico e registra erro, mas não envia nada.
- `SMILES_API_KEY` (opcional na prática, necessário para o passo 5): a API de
  busca de voos da Smiles só responde com o header `x-api-key` que o próprio
  site usa. Sem ele cada rota é registrada como falha e só a página de
  promoções é considerada.

## Regras de promoção

Uma tarifa vira alerta quando está abaixo de `limite_milhas` ou abaixo de
`média × (1 − queda_percentual_promocao/100)`, onde a média é das tarifas da
mesma rota coletadas nos 14 dias anteriores. Cards da página de promoções
entram sempre com motivo "promoção oficial".

Um alerta não se repete se já existe no histórico um alerta enviado com a
mesma rota, data de viagem (ou validade, no caso das promoções oficiais) e
milhas. Se o envio ao Telegram falhar, o item fica com `alerta_enviado=false`
e volta a ser candidato no dia seguinte.

Para manter poucas requisições, cada rota é consultada em
`consultas_por_rota` datas espalhadas pela janela, e a consulta da rota é
interrompida depois de duas falhas quando nenhuma data respondeu.

## Rodar localmente

```
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python agent.py
python -m pytest tests   # precisa de pytest
```

## Limitações conhecidas

A Smiles usa proteção anti-bot e muda o HTML da página de promoções com
frequência. O parser procura links que mencionam milhas ou R$ e extrai rota
(par de códigos IATA), valor e validade do texto; se o layout mudar, ajuste
`parse_promocoes` em `agent.py`. O agente não foi testado contra o site real
porque o ambiente onde foi escrito não tinha acesso à internet.
