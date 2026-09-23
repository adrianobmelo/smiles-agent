"""Testes offline do SMILES_AGENT (sem rede). Rodar: python -m pytest tests"""
import json
import sys
from datetime import date
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent  # noqa: E402
import telegram_send  # noqa: E402

HOJE = date(2026, 9, 23)

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
<item>
  <title>Smiles tem passagens para o Nordeste a partir de 8 mil milhas</title>
  <link>https://passageirodeprimeira.com/smiles-nordeste/</link>
  <pubDate>Tue, 22 Sep 2026 14:00:00 +0000</pubDate>
  <category>Smiles</category>
  <content:encoded><![CDATA[<p>São Paulo (GRU) x Recife (REC) por 8.500 milhas + taxas.</p>
  <p>Rio (GIG) x Salvador (SSA) por 9.000 milhas. A promoção é válida até 25/09/2026.</p>]]></content:encoded>
</item>
<item>
  <title>5 sugestões de voos em Classe Executiva a partir de 61 mil milhas Smiles</title>
  <link>https://passageirodeprimeira.com/executiva-smiles/</link>
  <pubDate>Wed, 23 Sep 2026 10:00:00 +0000</pubDate>
  <content:encoded><![CDATA[<p>São Paulo (GRU) x Buenos Aires (EZE) por 66.500 milhas e taxas.</p>]]></content:encoded>
</item>
<item>
  <title>LATAM tem passagens nacionais a partir de R$ 117 ou 4.737 milhas</title>
  <link>https://passageirodeprimeira.com/latam/</link>
  <pubDate>Wed, 23 Sep 2026 09:00:00 +0000</pubDate>
  <category>LATAM Pass</category>
</item>
<item>
  <title>Turkish Miles&amp;Smiles tem executiva barata</title>
  <link>https://passageirodeprimeira.com/turkish/</link>
  <pubDate>Wed, 23 Sep 2026 09:00:00 +0000</pubDate>
</item>
<item>
  <title>Smiles antiga promoção de 2025</title>
  <link>https://passageirodeprimeira.com/velha/</link>
  <pubDate>Mon, 01 Sep 2025 09:00:00 +0000</pubDate>
</item>
</channel></rss>"""


def voo(milhas):
    return {"requestedFlightSegmentList": [{"flightList": [{"fareList": [{"miles": milhas}, {"miles": milhas + 5000}]}]}]}


class FakeResp:
    def __init__(self, status=200, text="", payload=None, url=""):
        self.status_code, self.text, self._payload, self.url = status, text, payload, url
        self.content = text.encode("utf-8")
        self.headers = {"content-type": "application/json"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("sem json")
        return self._payload


class FakeSession:
    def __init__(self, smiles_up=True, telegram_up=True, promo=True, milhas=None):
        self.headers = {}
        self.smiles_up, self.telegram_up, self.promo = smiles_up, telegram_up, promo
        self.milhas = milhas or {}
        self.search_calls = 0

    def get(self, url, params=None, **kw):
        if url == agent.TELEGRAM_HOME:
            if not self.telegram_up:
                raise requests.ConnectionError("down")
            return FakeResp(404)
        if not self.smiles_up:
            raise requests.ConnectionError("down")
        if url == agent.SMILES_HOME:
            return FakeResp(200)
        if url == agent.PROMO_FEED_URL:
            return FakeResp(200 if self.promo else 403, FEED, url=url)
        if url == agent.SEARCH_URL:
            self.search_calls += 1
            rota = f"{params['originAirportCode']}-{params['destinationAirportCode']}"
            if rota not in self.milhas:
                return FakeResp(403)
            return FakeResp(200, payload=voo(self.milhas[rota]))
        raise AssertionError(url)


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    rotas = tmp_path / "rotas.yaml"
    rotas.write_text(
        "janela_dias: 30\nqueda_percentual_promocao: 20\nconsultas_por_rota: 3\n"
        "rotas:\n"
        "  - {origem: GRU, destino: REC, limite_milhas: 15000}\n"
        "  - {origem: GRU, destino: LIS, limite_milhas: 60000}\n",
        encoding="utf-8",
    )
    hist = tmp_path / "historico.json"
    hist.write_text("", encoding="utf-8")
    monkeypatch.setattr(agent, "ROTAS_PATH", rotas)
    monkeypatch.setattr(agent, "HISTORICO_PATH", hist)
    monkeypatch.setattr(agent.load_config, "__defaults__", (rotas,))
    monkeypatch.setattr(agent.load_historico, "__defaults__", (hist,))
    monkeypatch.setattr(agent.salvar_historico, "__defaults__", (hist,))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(agent, "SMILES_API_KEY", "chave-teste")
    enviados = []

    def enviar(texto, token=None, chat_id=None):
        enviados.append(texto)
        return True

    return hist, enviados, enviar


def test_parse_feed():
    posts = agent.parse_feed(FEED.encode(), HOJE, 3)
    assert [p["link"].rsplit("/", 2)[1] for p in posts] == ["smiles-nordeste", "executiva-smiles"]
    nordeste, executiva = posts
    assert nordeste["milhas"] == 8000 and nordeste["validade"] == "25/09/2026"
    assert [(r["origem"], r["destino"], r["milhas"]) for r in nordeste["rotas"]] == [
        ("GRU", "REC", 8500), ("GIG", "SSA", 9000)]
    assert executiva["milhas"] == 61000
    assert executiva["rotas"][0]["milhas"] == 66500


def test_milhas_para_int():
    assert agent.milhas_para_int("61 mil") == 61000
    assert agent.milhas_para_int("7,5 mil") == 7500
    assert agent.milhas_para_int("66.500") == 66500


def test_avaliacao_motivos():
    t = {"milhas": 7000}
    assert agent.avaliar_tarifa(t, 8000, 10000, 20) == ["abaixo limite", "queda >20%"]
    assert agent.avaliar_tarifa(t, 5000, 8000, 20) == []
    assert agent.avaliar_tarifa(t, 5000, None, 20) == []


def test_escape_html():
    item = {"tipo": "tarifa", "rota": "GRU-REC", "milhas": 9000, "data_viagem": "2026-10-01",
            "link": 'https://x.com/?a=1&b="2"', "motivo_alerta": "abaixo <limite>"}
    linha = agent.formatar_linha(item)
    assert "&amp;b=&quot;2&quot;" in linha and "abaixo &lt;limite&gt;" in linha
    assert linha.startswith("GRU → REC | 9.000 milhas | 2026-10-01 | <a href=")
    sem_rota = agent.formatar_linha({"tipo": "oficial", "titulo": "A & B", "milhas": None,
                                     "link": "https://x", "motivo_alerta": "promoção divulgada"})
    assert sem_rota == '<b>A &amp; B</b>\nmilhas n/d | sem data | <a href="https://x">link</a> | motivo: promoção divulgada'


def test_tudo_fora_do_ar(ambiente):
    hist, enviados, enviar = ambiente
    resumo = agent.run(FakeSession(smiles_up=False, telegram_up=False), HOJE, enviar)
    assert enviados == [agent.MSG_TUDO_FALHOU]
    assert resumo["historico_atualizado"] is False


def test_fontes_bloqueadas(ambiente):
    hist, enviados, enviar = ambiente
    agent.run(FakeSession(promo=False, milhas={}), HOJE, enviar)
    assert enviados == [agent.MSG_TUDO_FALHOU]


def test_fluxo_completo_e_sem_repeticao(ambiente):
    hist, enviados, enviar = ambiente
    sessao = FakeSession(milhas={"GRU-REC": 12000, "GRU-LIS": 80000})
    r1 = agent.run(sessao, HOJE, enviar)
    assert len(enviados) == 1
    msg = enviados[0]
    assert "promoção divulgada, rota monitorada, abaixo limite" in msg
    assert "GRU → REC | 8.500 milhas | 25/09/2026" in msg
    assert "61.000 milhas" in msg and "LIS" not in msg and "LATAM" not in msg
    assert r1["alertas_enviados"] == 2 + 3  # 2 posts Smiles + 3 datas GRU-REC
    assert r1["rotas_com_falha"] == []

    dados = json.loads(hist.read_text())
    assert any(h["tipo"] == "tarifa" and h["rota"] == "GRU-LIS" and not h["alerta_enviado"] for h in dados)
    assert sum(1 for h in dados if h["alerta_enviado"]) == 5

    # Mesmo cenário no dia seguinte: nada novo, nada enviado.
    agent.run(FakeSession(milhas={"GRU-REC": 12000, "GRU-LIS": 80000}), HOJE, enviar)
    assert len(enviados) == 1


def test_queda_percentual(ambiente):
    hist, enviados, enviar = ambiente
    base = [{"tipo": "tarifa", "rota": "GRU-LIS", "data_viagem": "2026-11-01",
             "data_coleta": f"2026-09-{d:02d}T08:00:00-03:00", "milhas": 100000,
             "alerta_enviado": False} for d in range(15, 23)]
    hist.write_text(json.dumps(base))
    agent.run(FakeSession(promo=False, milhas={"GRU-LIS": 75000}), HOJE, enviar)
    assert len(enviados) == 1 and "queda &gt;20%" in enviados[0]


def test_sem_chave_smiles_usa_so_feed(ambiente, monkeypatch):
    hist, enviados, enviar = ambiente
    monkeypatch.setattr(agent, "SMILES_API_KEY", "")
    sessao = FakeSession(milhas={"GRU-REC": 12000})
    r = agent.run(sessao, HOJE, enviar)
    assert sessao.search_calls == 0 and r["rotas_consultadas"] == []
    assert len(enviados) == 1 and r["alertas_enviados"] == 2


def test_sem_credenciais_nao_envia(ambiente, monkeypatch):
    hist, enviados, enviar = ambiente
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    r = agent.run(FakeSession(milhas={"GRU-REC": 12000}), HOJE, enviar)
    assert enviados == [] and r["historico_atualizado"]
    assert not any(h["alerta_enviado"] for h in json.loads(hist.read_text()))


def test_split_telegram():
    texto = "\n".join(["x" * 100] * 100)
    partes = telegram_send._split(texto)
    assert all(len(p) <= telegram_send.MAX_LEN for p in partes) and "\n".join(partes) == texto
