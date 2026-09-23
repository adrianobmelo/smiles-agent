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
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

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
PROMO_FEED_URL = os.environ.get("SMILES_PROMO_FEED", "https://passageirodeprimeira.com/feed/")
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
    cfg.setdefault("promocoes_dias", 3)
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
# 4. Promoções públicas (feed do Passageiro de Primeira)
# ---------------------------------------------------------------------------
# A página de promoções da Smiles só carrega com JavaScript e a Akamai bloqueia
# os servidores do GitHub, então as promoções vêm do feed RSS de um blog que
# divulga ofertas. Só entram posts que citam a Smiles no título ou na categoria.
IATA_PAIR = re.compile(r"\b([A-Z]{3})\s*(?:-|–|→|>|/|x|para)\s*([A-Z]{3})\b")
# "São Paulo (GRU) x Buenos Aires (EZE)"
IATA_PAR_TEXTO = re.compile(r"\(([A-Z]{3})\)\s*(?:x|para|-|–|→|>|/|a)\s*[^()]{0,40}?\(([A-Z]{3})\)")
MILHAS_RE = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d{4,}|\d{1,3}(?:,\d+)?\s*mil)\s*milhas", re.I)
VALIDADE_RE = re.compile(
    r"(?:válid[ao]s?\s+até|vai\s+até|até)\s+(?:o\s+dia\s+|as\s+\d{1,2}h\s+do\s+dia\s+)?"
    r"(\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{1,2}\s+de\s+[a-zç]+|hoje|amanhã)",
    re.I,
)
SMILES_RE = re.compile(r"smiles", re.I)
TURKISH_RE = re.compile(r"miles\s*&\s*smiles", re.I)
NS_CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"


def milhas_para_int(texto):
    texto = texto.strip().lower()
    if texto.endswith("mil"):
        return int(round(float(texto[:-3].strip().replace(",", ".")) * 1000))
    return int(texto.replace(".", ""))


def extrair_rotas(texto):
    """Pares de aeroportos citados no texto, cada um com as milhas logo depois."""
    rotas, vistas = [], set()
    for regex in (IATA_PAR_TEXTO, IATA_PAIR):
        for m in regex.finditer(texto):
            par = (m.group(1), m.group(2))
            if par in vistas or par[0] == par[1]:
                continue
            mm = MILHAS_RE.search(texto, m.end(), m.end() + 150)
            vistas.add(par)
            rotas.append({"origem": par[0], "destino": par[1],
                          "milhas": milhas_para_int(mm.group(1)) if mm else None})
    return rotas


def eh_smiles(titulo, categorias):
    alvo = " ".join([titulo] + categorias)
    return bool(SMILES_RE.search(TURKISH_RE.sub("", alvo)))


def parse_feed(xml_text, hoje, dias):
    """Lê o RSS e devolve os posts recentes sobre Smiles."""
    root = ET.fromstring(xml_text)
    limite = hoje - timedelta(days=dias)
    posts = []
    for item in root.iter("item"):
        titulo = (item.findtext("title") or "").strip()
        categorias = [c.text or "" for c in item.findall("category")]
        if not eh_smiles(titulo, categorias):
            continue
        try:
            publicado = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(BRT).date()
        except (TypeError, ValueError):
            publicado = None
        if publicado and publicado < limite:
            continue
        html_corpo = item.findtext(NS_CONTENT) or item.findtext("description") or ""
        corpo = " ".join(BeautifulSoup(html_corpo, "html.parser").get_text(" ").split())
        m_titulo = MILHAS_RE.search(titulo)
        rotas = extrair_rotas(corpo)
        milhas_rotas = [r["milhas"] for r in rotas if r["milhas"]]
        m_validade = VALIDADE_RE.search(corpo)
        validade = m_validade.group(1) if m_validade else None
        # "hoje"/"amanhã" são relativos à data do post, não à data do alerta.
        if validade and publicado and validade.lower() in ("hoje", "amanhã"):
            dia = publicado + timedelta(days=1 if validade.lower() == "amanhã" else 0)
            validade = dia.strftime("%d/%m/%Y")
        posts.append({
            "titulo": titulo,
            "link": (item.findtext("link") or "").strip(),
            "publicado": publicado.isoformat() if publicado else None,
            "milhas": milhas_para_int(m_titulo.group(1)) if m_titulo else (min(milhas_rotas) if milhas_rotas else None),
            "validade": validade,
            "rotas": rotas,
        })
    return posts


def coletar_promocoes(session, hoje, dias):
    try:
        resp = session.get(PROMO_FEED_URL, timeout=TIMEOUT)
        resp.raise_for_status()
        posts = parse_feed(resp.content, hoje, dias)
    except (requests.RequestException, ET.ParseError) as exc:
        log.warning("Feed de promoções indisponível: %s", exc)
        return None
    log.info("Promoções Smiles no feed (últimos %d dias): %d", dias, len(posts))
    if DEBUG:
        for p in posts:
            log.info("DEBUG post=%s", json.dumps(p, ensure_ascii=False))
    return posts


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
    if item.get("tipo") == "oficial":
        return ("oficial", item.get("link") or item.get("titulo"))
    return (item.get("tipo"), item.get("rota"), item.get("data_viagem"), item.get("milhas"))


def ja_alertado(item, historico):
    k = chave_alerta(item)
    return any(h.get("alerta_enviado") and chave_alerta(h) == k for h in historico)


# ---------------------------------------------------------------------------
# 8. Mensagem
# ---------------------------------------------------------------------------
def _milhas(valor):
    return f"{valor:,}".replace(",", ".") + " milhas"


def _data_br(texto):
    """2026-10-03 vira 03/10/2026; outros formatos passam como vieram."""
    try:
        return date.fromisoformat(texto).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return texto


def eh_passagem(item):
    """Post com milhas por trecho ou rota é oferta de voo; o resto é bônus/compra."""
    return item.get("tipo") != "oficial" or bool(item.get("milhas") or item.get("rota"))


def formatar_item(n, item):
    """Bloco de um alerta: título com link, depois rota/milhas e validade/motivo."""
    if item.get("tipo") == "oficial":
        titulo = f"<b>{escape(item.get('titulo') or 'Promoção')}</b>"
    else:
        titulo = "<b>Tarifa encontrada na Smiles</b>"
    if item.get("link"):
        titulo = f'<a href="{escape(item["link"])}">{titulo}</a>'
    linhas = [f"{n}. {titulo}"]

    detalhe = []
    if item.get("rota"):
        origem, destino = item["rota"].split("-", 1)
        detalhe.append(f"{escape(origem)} → {escape(destino)}")
    if item.get("milhas") is not None:
        detalhe.append(escape(_milhas(item["milhas"])))
    if detalhe:
        linhas.append(" · ".join(detalhe))

    rodape = []
    if item.get("data_viagem"):
        rodape.append(f"ida {escape(_data_br(item['data_viagem']))}")
    if item.get("validade"):
        rodape.append(f"válida até {escape(_data_br(item['validade']))}")
    # "promoção divulgada" vale para todo post do feed, então só aparece o que acrescenta.
    motivos = [m for m in (item.get("motivo_alerta") or "").split(", ")
               if m and m != "promoção divulgada"]
    if motivos:
        rodape.append(f"<i>{escape(', '.join(motivos))}</i>")
    if rodape:
        linhas.append(" · ".join(rodape))
    return "\n".join(linhas)


def montar_mensagem(alertas, hoje):
    passagens = [a for a in alertas if eh_passagem(a)]
    outros = [a for a in alertas if not eh_passagem(a)]
    total = len(alertas)
    blocos = [f"<b>Smiles Agent · {hoje.strftime('%d/%m/%Y')}</b>\n"
              f"{total} {'novidade' if total == 1 else 'novidades'}"]
    n = 0
    for nome, itens in (("PASSAGENS", passagens), ("COMPRA E TRANSFERÊNCIA DE MILHAS", outros)):
        for i, item in enumerate(itens):
            n += 1
            bloco = formatar_item(n, item)
            blocos.append(f"<b>{nome}</b>\n{bloco}" if i == 0 else bloco)
    blocos.append("<i>Fonte: Passageiro de Primeira</i>")
    return "\n\n".join(blocos)


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
        # Reenvia os últimos alertas do histórico no layout atual, como prévia.
        amostra = [h for h in load_historico() if h.get("alerta_enviado")][-6:]
        if amostra:
            texto = "<i>Prévia de teste com alertas já enviados</i>\n\n" + montar_mensagem(amostra, hoje)
        else:
            texto = "Smiles Agent: mensagem de teste. Se você está lendo isto, o envio funciona."
        ok = notificar(texto)
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
    promos = coletar_promocoes(session, hoje, int(cfg["promocoes_dias"]))
    promos_ok = promos is not None
    promos = promos or []

    # 5
    datas = datas_consulta(hoje, int(cfg["janela_dias"]), int(cfg["consultas_por_rota"]))
    tarifas, rotas_ok = [], 0
    if not SMILES_API_KEY:
        log.info("Busca de tarifas desativada: SMILES_API_KEY não configurada")
    for rota in cfg["rotas"] if SMILES_API_KEY else []:
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
    limites = {f"{r['origem']}-{r['destino']}": r["limite_milhas"] for r in cfg["rotas"]}
    for p in promos:
        rota, milhas, motivos = None, p.get("milhas"), ["promoção divulgada"]
        # Se o post cita uma rota monitorada, a linha mostra essa rota.
        for r in p["rotas"]:
            nome = f"{r['origem']}-{r['destino']}"
            if nome in limites:
                rota, milhas = nome, r["milhas"] or milhas
                motivos.append("rota monitorada")
                if r["milhas"] and r["milhas"] < limites[nome]:
                    motivos.append("abaixo limite")
                break
        candidatos.append({
            "tipo": "oficial", "titulo": p["titulo"], "rota": rota,
            "data_viagem": None, "validade": p.get("validade"), "data_coleta": data_coleta,
            "milhas": milhas, "link": p["link"], "publicado": p.get("publicado"),
            "motivo_alerta": ", ".join(motivos),
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
