"""SMILES_AGENT: rotina diária de promoções Smiles.

Fluxo (ver README.md):
 1. variáveis de ambiente    2. teste de conectividade   3. configuração
 4. promoções públicas       5. tarifas em milhas        6. avaliação
 7. deduplicação             8. mensagem HTML            9. envio
10. histórico                11. log final
"""
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

from telegram_send import escape, send_message

BASE_DIR = Path(__file__).resolve().parent
ROTAS_PATH = Path(os.environ.get("SMILES_ROTAS", BASE_DIR / "rotas.yaml"))
HISTORICO_PATH = Path(os.environ.get("SMILES_HISTORICO", BASE_DIR / "historico.json"))

BRT = timezone(timedelta(hours=-3))
SMILES_HOME = "https://www.smiles.com.br"
TELEGRAM_HOME = "https://api.telegram.org"
PROMO_URL = os.environ.get("SMILES_PROMO_URL", "https://www.smiles.com.br/promocoes")
SEARCH_URL = os.environ.get(
    "SMILES_SEARCH_URL", "https://api-air-flightsearch-prd.smiles.com.br/v1/airlines/search"
)
# A busca de voos da Smiles exige o header x-api-key usado pelo próprio site.
# Sem ele a API responde 401/403 e a rota é registrada como falha.
SMILES_API_KEY = os.environ.get("SMILES_API_KEY", "")
# SMILES_DEBUG=1 escreve no log o que a Smiles devolveu, para ajustar o parser.
DEBUG = os.environ.get("SMILES_DEBUG", "").lower() in ("1", "true", "yes")
# SMILES_TESTE_TELEGRAM=1 manda uma mensagem de teste no início da execução.
TESTE_TELEGRAM = os.environ.get("SMILES_TESTE_TELEGRAM", "").lower() in ("1", "true", "yes")

JANELA_MEDIA_DIAS = 14
TIMEOUT = 20
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}

MSG_TUDO_FALHOU = "Smiles Agent: todas as fontes falharam hoje."

log = logging.getLogger("smiles_agent")


# ---------------------------------------------------------------------------
# 1. Variáveis de ambiente
# ---------------------------------------------------------------------------
def load_env():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes; execução segue sem envio")
    return token, chat_id


# ---------------------------------------------------------------------------
# 2. Conectividade
# ---------------------------------------------------------------------------
def check_url(session, url):
    try:
        resp = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        # Qualquer resposta HTTP abaixo de 500 prova que o host está acessível
        # (api.telegram.org responde 302/404 na raiz).
        return "ok" if resp.status_code < 500 else "falha"
    except requests.RequestException as exc:
        log.warning("Conectividade %s: %s", url, exc)
        return "falha"


def check_connectivity(session):
    return {
        "smiles_status": check_url(session, SMILES_HOME),
        "telegram_status": check_url(session, TELEGRAM_HOME),
    }


# ---------------------------------------------------------------------------
# 3. Configuração e histórico
# ---------------------------------------------------------------------------
def load_config(path=ROTAS_PATH):
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg.setdefault("janela_dias", 30)
    cfg.setdefault("queda_percentual_promocao", 20)
    cfg.setdefault("consultas_por_rota", 5)
    rotas = []
    for r in cfg.get("rotas") or []:
        if not r.get("ativa", True):
            continue
        rotas.append({
            "origem": str(r["origem"]).upper(),
            "destino": str(r["destino"]).upper(),
            "limite_milhas": int(r["limite_milhas"]),
        })
    cfg["rotas"] = rotas
    return cfg


def load_historico(path=HISTORICO_PATH):
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return []
    if not text:
        return []
    data = json.loads(text)
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# 4. Promoções públicas
# ---------------------------------------------------------------------------
IATA_PAIR = re.compile(r"\b([A-Z]{3})\s*(?:-|–|→|>|/|x|para)\s*([A-Z]{3})\b")
MILHAS_RE = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d{4,})\s*milhas", re.I)
PRECO_RE = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)")
VALIDADE_RE = re.compile(
    r"(?:até|válid[ao]s?\s+até|validade:?)\s*(\d{1,2}/\d{1,2}(?:/\d{2,4})?)", re.I
)


def _to_int(num):
    return int(num.replace(".", "").split(",")[0])


def parse_promo_text(texto):
    """Extrai rota, milhas/preço e validade de um trecho de texto."""
    out = {"origem": None, "destino": None, "milhas": None, "preco": None, "validade": None}
    m = IATA_PAIR.search(texto)
    if m:
        out["origem"], out["destino"] = m.group(1), m.group(2)
    m = MILHAS_RE.search(texto)
    if m:
        out["milhas"] = _to_int(m.group(1))
    m = PRECO_RE.search(texto)
    if m:
        out["preco"] = m.group(1)
    m = VALIDADE_RE.search(texto)
    if m:
        out["validade"] = m.group(1)
    return out


def parse_promocoes(html_text, base_url=PROMO_URL):
    """Lê a página de promoções e devolve os cards que mencionam milhas ou preço."""
    soup = BeautifulSoup(html_text, "html.parser")
    promos, vistos = [], set()

    # Cards costumam ser links com título e texto de oferta dentro.
    for a in soup.find_all("a", href=True):
        texto = " ".join(a.get_text(" ", strip=True).split())
        if not texto or not (MILHAS_RE.search(texto) or PRECO_RE.search(texto)):
            continue
        link = urljoin(base_url, a["href"])
        titulo_tag = a.find(["h1", "h2", "h3", "h4", "strong"])
        titulo = titulo_tag.get_text(" ", strip=True) if titulo_tag else texto[:80]
        chave = (titulo, link)
        if chave in vistos:
            continue
        vistos.add(chave)
        promo = {"titulo": titulo, "link": link}
        promo.update(parse_promo_text(texto))
        promos.append(promo)
    return promos


def coletar_promocoes(session):
    try:
        resp = session.get(PROMO_URL, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Página de promoções indisponível: %s", exc)
        return None
    promos = parse_promocoes(resp.text, resp.url or PROMO_URL)
    log.info("Promoções públicas encontradas: %d", len(promos))
    if DEBUG:
        diagnostico_pagina(resp)
    return promos


def diagnostico_pagina(resp):
    """Resume a página de promoções no log para entender a estrutura real."""
    texto = resp.text
    soup = BeautifulSoup(texto, "html.parser")
    log.info("DEBUG url final=%s status=%s tamanho=%d content-type=%s",
             resp.url, resp.status_code, len(texto), resp.headers.get("content-type"))
    log.info("DEBUG title=%r links=%d scripts=%d", soup.title.string if soup.title else None,
             len(soup.find_all("a")), len(soup.find_all("script")))
    for marcador in ("__NEXT_DATA__", "__NUXT__", "application/ld+json", "liferay", "milhas", "R$"):
        log.info("DEBUG contém %r: %d vezes", marcador, texto.count(marcador))
    for sc in soup.find_all("script", src=True)[:25]:
        log.info("DEBUG script src=%s", sc["src"])
    for m in list(re.finditer(r"https?://[^\s\"'<>]*(?:api|promo|offer|oferta)[^\s\"'<>]*", texto, re.I))[:25]:
        log.info("DEBUG url citada=%s", m.group(0)[:200])
    visivel = " ".join(soup.get_text(" ", strip=True).split())
    log.info("DEBUG texto visível (1500 chars)=%s", visivel[:1500])
    for m in list(MILHAS_RE.finditer(texto))[:10]:
        ini = max(0, m.start() - 150)
        log.info("DEBUG trecho milhas=%r", texto[ini:m.end() + 50])


# ---------------------------------------------------------------------------
# 5. Tarifas em milhas
# ---------------------------------------------------------------------------
def datas_consulta(hoje, janela_dias, n):
    """Distribui n datas entre amanhã e hoje+janela_dias."""
    n = max(1, min(n, janela_dias))
    passo = janela_dias / n
    return sorted({hoje + timedelta(days=max(1, round(passo * (i + 1)))) for i in range(n)})


def link_busca(origem, destino, dia):
    ts = int(datetime(dia.year, dia.month, dia.day, 12, tzinfo=BRT).timestamp() * 1000)
    return (
        "https://www.smiles.com.br/mfe/emissao-passagem/?adults=1&cabin=ALL&children=0"
        f"&departureDate={ts}&infants=0&isElegible=false&isFlexibleDateChecked=false"
        f"&returnDate=&searchType=g3&segments=1&tripType=2"
        f"&originAirport={origem}&destinationAirport={destino}"
    )


def menor_milhas(payload):
    """Menor quantidade de milhas entre os voos devolvidos pela API."""
    menores = []
    for seg in payload.get("requestedFlightSegmentList", []) or []:
        for voo in seg.get("flightList", []) or []:
            for fare in voo.get("fareList", []) or []:
                milhas = fare.get("miles")
                if isinstance(milhas, (int, float)) and milhas > 0:
                    menores.append(int(milhas))
    return min(menores) if menores else None


def consultar_rota(session, rota, datas):
    """Consulta a rota nas datas dadas. Retorna (tarifas, ok).

    ok é True se ao menos uma consulta respondeu, mesmo sem voos."""
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": SMILES_HOME,
        "Referer": SMILES_HOME + "/",
        "channel": "Web",
        "region": "BRASIL",
        "language": "pt-BR",
    }
    if SMILES_API_KEY:
        headers["x-api-key"] = SMILES_API_KEY
    tarifas, falhas, respostas = [], 0, 0
    for dia in datas:
        params = {
            "adults": 1, "children": 0, "infants": 0, "cabinType": "all",
            "currencyCode": "BRL", "tripType": 2, "isFlexibleDateChecked": "false",
            "forceCongener": "false", "r": "br",
            "originAirportCode": rota["origem"],
            "destinationAirportCode": rota["destino"],
            "departureDate": dia.isoformat(),
        }
        try:
            resp = session.get(SEARCH_URL, params=params, headers=headers, timeout=TIMEOUT)
            resp.raise_for_status()
            milhas = menor_milhas(resp.json())
        except (requests.RequestException, ValueError) as exc:
            falhas += 1
            log.warning("%s-%s %s: falha na consulta (%s)", rota["origem"], rota["destino"], dia, exc)
            corpo = getattr(getattr(exc, "response", None), "text", "") or ""
            if DEBUG and corpo:
                log.info("DEBUG resposta da busca=%r", corpo[:400])
            if falhas >= 2 and not respostas:
                # Bloqueio provável: não insiste nas datas restantes.
                break
            continue
        respostas += 1
        if milhas is None:
            continue
        tarifas.append({
            "origem": rota["origem"],
            "destino": rota["destino"],
            "data_viagem": dia.isoformat(),
            "milhas": milhas,
            "link": link_busca(rota["origem"], rota["destino"], dia),
        })
    return tarifas, respostas > 0


# ---------------------------------------------------------------------------
# 6. Avaliação
# ---------------------------------------------------------------------------
def media_14_dias(historico, origem, destino, hoje):
    inicio = hoje - timedelta(days=JANELA_MEDIA_DIAS)
    valores = []
    for h in historico:
        if h.get("tipo") != "tarifa" or h.get("rota") != f"{origem}-{destino}":
            continue
        try:
            coleta = date.fromisoformat(h["data_coleta"][:10])
        except (KeyError, ValueError):
            continue
        if inicio <= coleta < hoje and isinstance(h.get("milhas"), (int, float)):
            valores.append(h["milhas"])
    return mean(valores) if valores else None


def avaliar_tarifa(tarifa, limite, media, queda_pct):
    motivos = []
    if tarifa["milhas"] < limite:
        motivos.append("abaixo limite")
    if media is not None and tarifa["milhas"] < media * (1 - queda_pct / 100):
        motivos.append(f"queda >{queda_pct:g}%")
    return motivos


# ---------------------------------------------------------------------------
# 7. Deduplicação
# ---------------------------------------------------------------------------
def chave_alerta(item):
    return (item.get("tipo"), item.get("rota"), item.get("data_viagem") or item.get("validade"),
            item.get("milhas"), item.get("preco"), item.get("titulo") if item.get("tipo") == "oficial" else None)


def ja_alertado(item, historico):
    k = chave_alerta(item)
    return any(h.get("alerta_enviado") and chave_alerta(h) == k for h in historico)


# ---------------------------------------------------------------------------
# 8. Mensagem
# ---------------------------------------------------------------------------
def formatar_linha(item):
    origem, destino = (item.get("rota") or "?-?").split("-", 1)
    if item.get("milhas") is not None:
        valor = f"{item['milhas']:,}".replace(",", ".") + " milhas"
    elif item.get("preco"):
        valor = f"R$ {item['preco']}"
    else:
        valor = "valor n/d"
    quando = item.get("data_viagem") or item.get("validade") or "sem data"
    partes = [f"{escape(origem)} → {escape(destino)}", escape(valor), escape(quando)]
    if item.get("link"):
        partes.append(f'<a href="{escape(item["link"])}">link</a>')
    partes.append(f"motivo: {escape(item['motivo_alerta'])}")
    linha = " | ".join(partes)
    if item.get("tipo") == "oficial" and item.get("titulo"):
        linha = f"<b>{escape(item['titulo'])}</b>\n{linha}"
    return linha


def montar_mensagem(alertas, hoje):
    cabecalho = f"<b>Smiles Agent {hoje.strftime('%d/%m/%Y')}</b>"
    return "\n".join([cabecalho, ""] + [formatar_linha(a) for a in alertas])


# ---------------------------------------------------------------------------
# 10. Histórico
# ---------------------------------------------------------------------------
def podar_historico(historico, hoje):
    """Mantém 14 dias de observações e os alertas enviados ainda válidos."""
    limite_coleta = hoje - timedelta(days=JANELA_MEDIA_DIAS + 1)
    mantidos = []
    for h in historico:
        try:
            coleta = date.fromisoformat(str(h.get("data_coleta", ""))[:10])
        except ValueError:
            continue
        if coleta >= limite_coleta:
            mantidos.append(h)
            continue
        if h.get("alerta_enviado"):
            # Alerta antigo só é descartado depois que a viagem passou,
            # ou 90 dias depois da coleta quando não há data de viagem.
            try:
                viagem = date.fromisoformat(h.get("data_viagem") or "")
                if viagem >= hoje:
                    mantidos.append(h)
            except ValueError:
                if coleta >= hoje - timedelta(days=90):
                    mantidos.append(h)
    return mantidos


def salvar_historico(historico, path=HISTORICO_PATH):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(historico, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Orquestração
# ---------------------------------------------------------------------------
def run(session=None, hoje=None, enviar=send_message):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    agora = datetime.now(BRT)
    hoje = hoje or agora.date()
    resumo = {"conectividade": {}, "rotas_consultadas": [], "rotas_com_falha": [],
              "promocoes_encontradas": 0, "alertas_enviados": 0, "historico_atualizado": False}

    token, chat_id = load_env()
    pode_enviar = bool(token and chat_id)

    def notificar(texto):
        if not pode_enviar:
            log.error("Mensagem não enviada (credenciais ausentes)")
            return False
        return enviar(texto, token=token, chat_id=chat_id)

    if TESTE_TELEGRAM:
        ok = notificar("Smiles Agent: mensagem de teste. Se você está lendo isto, o envio funciona.")
        log.info("Teste de envio ao Telegram: %s", "ok" if ok else "falhou")

    # 2
    con = check_connectivity(session)
    resumo["conectividade"] = con
    log.info("Conectividade: smiles=%s telegram=%s", con["smiles_status"], con["telegram_status"])
    if con["smiles_status"] == "falha" and con["telegram_status"] == "falha":
        notificar(MSG_TUDO_FALHOU)
        log_final(resumo)
        return resumo

    # 3
    cfg = load_config()
    historico = load_historico()
    queda = float(cfg["queda_percentual_promocao"])

    # 4
    promos = coletar_promocoes(session)
    promos_ok = promos is not None
    promos = promos or []

    # 5
    datas = datas_consulta(hoje, int(cfg["janela_dias"]), int(cfg["consultas_por_rota"]))
    tarifas, rotas_ok = [], 0
    for rota in cfg["rotas"]:
        nome = f"{rota['origem']}-{rota['destino']}"
        encontradas, ok = consultar_rota(session, rota, datas)
        resumo["rotas_consultadas"].append(nome)
        if ok:
            rotas_ok += 1
        else:
            resumo["rotas_com_falha"].append(nome)
        for t in encontradas:
            t["limite_milhas"] = rota["limite_milhas"]
        tarifas.extend(encontradas)

    if not promos_ok and rotas_ok == 0:
        log.error("Todas as fontes de dados falharam")
        notificar(MSG_TUDO_FALHOU)
        log_final(resumo)
        return resumo

    # 6
    data_coleta = agora.isoformat(timespec="seconds")
    candidatos, observacoes = [], []
    for p in promos:
        rota = f"{p['origem']}-{p['destino']}" if p.get("origem") else None
        candidatos.append({
            "tipo": "oficial", "titulo": p.get("titulo"), "rota": rota,
            "data_viagem": None, "validade": p.get("validade"), "data_coleta": data_coleta,
            "milhas": p.get("milhas"), "preco": p.get("preco"), "link": p.get("link"),
            "motivo_alerta": "promoção oficial",
        })
    for t in tarifas:
        media = media_14_dias(historico, t["origem"], t["destino"], hoje)
        motivos = avaliar_tarifa(t, t["limite_milhas"], media, queda)
        registro = {
            "tipo": "tarifa", "rota": f"{t['origem']}-{t['destino']}",
            "data_viagem": t["data_viagem"], "data_coleta": data_coleta,
            "milhas": t["milhas"], "link": t["link"],
            "media_14d": round(media) if media is not None else None,
            "motivo_alerta": ", ".join(motivos) if motivos else None,
            "alerta_enviado": False,
        }
        observacoes.append(registro)
        if motivos:
            candidatos.append(registro)
    resumo["promocoes_encontradas"] = len(candidatos)

    # 7
    novos = [c for c in candidatos if not ja_alertado(c, historico)]
    log.info("Promoções: %d encontradas, %d novas", len(candidatos), len(novos))

    # 8 e 9
    enviado = False
    if novos:
        enviado = notificar(montar_mensagem(novos, hoje))
        if enviado:
            resumo["alertas_enviados"] = len(novos)

    # 10
    for c in novos:
        c["alerta_enviado"] = enviado
    oficiais_novos = [c for c in novos if c["tipo"] == "oficial" and enviado]
    historico = podar_historico(historico + observacoes + oficiais_novos, hoje)
    salvar_historico(historico)
    resumo["historico_atualizado"] = True

    log_final(resumo)
    return resumo


# ---------------------------------------------------------------------------
# 11. Log final
# ---------------------------------------------------------------------------
def log_final(resumo):
    log.info("RESUMO %s", json.dumps(resumo, ensure_ascii=False))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run()
    sys.exit(0)
