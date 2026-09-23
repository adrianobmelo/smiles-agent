"""Envio de mensagens para o Telegram (parse_mode=HTML).

Uso como script:
    python telegram_send.py "texto da mensagem"
Uso como módulo:
    from telegram_send import send_message
"""
import html
import logging
import os
import sys

import requests

log = logging.getLogger("smiles_agent.telegram")

TELEGRAM_API = "https://api.telegram.org"
MAX_LEN = 4096  # limite do Telegram por mensagem


def escape(text):
    """Escapa &, <, > e aspas duplas para parse_mode=HTML."""
    return html.escape(str(text), quote=True)


def _split(text):
    """Quebra o texto em blocos de até MAX_LEN, sempre em fim de linha."""
    chunks, current = [], ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > MAX_LEN and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def send_message(text, token=None, chat_id=None, timeout=20):
    """Envia `text` ao chat. Retorna True se todos os blocos foram aceitos."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID ausente; envio cancelado")
        return False

    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    ok = True
    for chunk in _split(text):
        try:
            resp = requests.post(
                url,
                data={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
                timeout=timeout,
            )
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code != 200 or not body.get("ok", False):
                log.error("Telegram recusou a mensagem: %s %s", resp.status_code, body.get("description", resp.text[:200]))
                ok = False
        except requests.RequestException as exc:
            log.error("Falha ao enviar para o Telegram: %s", exc)
            ok = False
    return ok


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print('uso: python telegram_send.py "mensagem"')
        sys.exit(2)
    sys.exit(0 if send_message(" ".join(sys.argv[1:])) else 1)
