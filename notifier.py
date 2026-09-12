"""Notificação de oportunidades aprovadas via Telegram Bot API."""

from __future__ import annotations

import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _formatar_reais(valor: float | None) -> str:
    if valor is None:
        return "N/D"
    texto = f"{valor:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {texto}"


# Caracteres especiais do Markdown legado do Telegram (parse_mode="Markdown").
# Sem escapar, um "_" ou "*" desbalanceado em texto dinâmico (título/URL de
# um anúncio real, por exemplo) faz a API inteira retornar 400 Bad Request
# ("can't parse entities") — o alerta falharia silenciosamente em produção.
_CARACTERES_MARKDOWN_LEGADO = "_*`["


def _escapar_markdown(texto) -> str:
    texto = str(texto)
    for caractere in _CARACTERES_MARKDOWN_LEGADO:
        texto = texto.replace(caractere, f"\\{caractere}")
    return texto


def _formatar_mensagem(imovel: dict) -> str:
    bairro = _escapar_markdown(imovel.get("bairro") or "N/D")
    quartos = imovel.get("quartos", "N/D")
    preco_fmt = _formatar_reais(imovel.get("preco"))
    preco_m2_fmt = _formatar_reais(imovel.get("preco_m2"))
    mediana_fmt = _formatar_reais(imovel.get("mediana_referencia_m2"))
    desconto_percentual = imovel.get("desconto_percentual")
    desconto_fmt = f"{desconto_percentual:.2f}%" if desconto_percentual is not None else "N/D"
    gatilho_aprovacao = _escapar_markdown(imovel.get("gatilho_aprovacao") or "N/D")
    # A URL não é escapada: fica dentro de "(...)" da sintaxe de link
    # [texto](url) do Markdown, cujo conteúdo é tratado como destino literal
    # (não reparsing de entidades) — escapar aqui quebraria o link.
    url = imovel.get("url") or "N/D"
    # Elevador não é mais critério eliminatório (config.EXIGIR_ELEVADOR):
    # reflete o que foi de fato comprovado no anúncio, sem presumir.
    elevador_status = "Confirmada" if imovel.get("elevador") else "Não confirmada no anúncio"

    return (
        "*Oportunidade de Flipping Identificada*\n\n"
        f"*Bairro:* {bairro}\n"
        f"*Preço:* {preco_fmt}\n"
        f"*Quartos:* {quartos} (mínimo 3)\n"
        f"*Elevador:* {elevador_status}\n"
        f"*Preço/m²:* {preco_m2_fmt}\n"
        f"*Desconto vs. Mediana:* {desconto_fmt} (mediana do bairro: {mediana_fmt}/m²)\n"
        f"*Gatilho de disparo:* {gatilho_aprovacao}\n"
        f"*Anúncio:* [Ver anúncio]({url})"
    )


def enviar_alerta_telegram(imovel: dict) -> bool:
    """Envia, via requisição HTTP à API do Telegram (sendMessage), um
    alerta em Markdown sobre o imóvel aprovado. Usa TELEGRAM_BOT_TOKEN e
    TELEGRAM_CHAT_ID (variáveis de ambiente). Retorna True se o Telegram
    confirmar o envio (campo "ok" da resposta), False em qualquer falha
    (inclusive credenciais ausentes)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error(
            "credenciais_ausentes motivo=TELEGRAM_BOT_TOKEN_ou_TELEGRAM_CHAT_ID_nao_configurados"
        )
        return False

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": _formatar_mensagem(imovel),
        "parse_mode": "Markdown",
    }
    url = TELEGRAM_API_URL.format(token=TELEGRAM_BOT_TOKEN)

    try:
        resposta = requests.post(url, json=payload, timeout=15)
        resposta.raise_for_status()
        corpo = resposta.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error(
            "falha_envio_telegram id_origem=%s erro=%s", imovel.get("id_origem"), exc
        )
        return False

    if not corpo.get("ok"):
        logger.error(
            "falha_envio_telegram id_origem=%s resposta=%s",
            imovel.get("id_origem"), corpo,
        )
        return False

    logger.info(
        "alerta_enviado id_origem=%s chat_id=%s", imovel.get("id_origem"), TELEGRAM_CHAT_ID
    )
    return True
