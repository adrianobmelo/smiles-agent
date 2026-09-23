# SMILES_AGENT

Rotina diária que testa a conexão com Smiles e Telegram, coleta promoções da
Smiles divulgadas no feed do Passageiro de Primeira, compara com
`historico.json` e manda as promoções novas no Telegram.

## De onde vêm os dados

A página de promoções da Smiles só carrega com JavaScript, e a API de busca
de voos responde 406 para os servidores do GitHub (bloqueio da Akamai). Por
isso as promoções vêm do RSS https://passageirodeprimeira.com/feed/, filtrado
pelos posts que citam Smiles no título ou na categoria e publicados nos
últimos `promocoes_dias` dias. Do texto de cada post saem os pares de
aeroportos (ex.: "São Paulo (GRU) x Recife (REC) por 8.500 milhas"), as
milhas e a validade.

A busca direta de tarifas na Smiles continua no código, mas só roda se o
secret `SMILES_API_KEY` existir. O Seats.aero (plano Pro, API paga) é a
alternativa estudada para voltar a ter tarifas rota a rota.

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
- `SMILES_API_KEY` (opcional): liga a busca direta de tarifas na Smiles.
  Hoje ela é bloqueada a partir do GitHub mesmo com a chave.

## Regras de promoção

Todo post novo sobre Smiles vira alerta com motivo "promoção divulgada". Se o
post cita uma rota de `rotas.yaml`, o motivo ganha "rota monitorada" e, se as
milhas estiverem abaixo de `limite_milhas`, "abaixo limite".

Quando a busca de tarifas está ligada, uma tarifa vira alerta quando está
abaixo de `limite_milhas` ou abaixo de `média × (1 − queda_percentual_promocao/100)`,
onde a média é das tarifas da mesma rota coletadas nos 14 dias anteriores.

Um post não é alertado duas vezes (a chave é o link). Uma tarifa não se
repete se já existe no histórico um alerta enviado com a mesma rota, data de
viagem e milhas. Se o envio ao Telegram falhar, o item fica com `alerta_enviado=false`
e volta a ser candidato no dia seguinte.

Com a busca ligada, para manter poucas requisições, cada rota é consultada em
`consultas_por_rota` datas espalhadas pela janela, e a consulta da rota é
interrompida depois de duas falhas quando nenhuma data respondeu.

## Rodar localmente

```
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python agent.py
python -m pytest tests   # precisa de pytest
```

## Execução manual

Em Actions → Smiles Agent → Run workflow há duas opções: `teste_telegram`
reenvia os últimos alertas do histórico como prévia do layout e `debug` escreve no log os posts lidos do feed e
o corpo das respostas de erro da busca.

## Limitações conhecidas

O agente só vê as promoções que o Passageiro de Primeira decidiu publicar. É
uma curadoria, não uma varredura de todas as tarifas da Smiles. A extração de
rotas depende do padrão "Cidade (AAA) x Cidade (BBB) por N milhas" usado nos
posts; se o blog mudar a redação, ajuste `extrair_rotas` em `agent.py`.
