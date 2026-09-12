"""Ponto de entrada: orquestra scraping, análise e notificação de oportunidades."""

from __future__ import annotations

import logging

from database import ja_processado, salvar_imovel
from engine import avaliar_oportunidade
from notifier import enviar_alerta_telegram
from scrapers.portais import OLXScraper, ZapVivaRealScraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# GoogleDorksScraper desativado temporariamente: a Custom Search API está
# retornando 403 Forbidden (problema de configuração no Google Cloud/CSE,
# não de credencial ausente) — reativar em scrapers/google_dorks.py assim
# que resolvido, importando e adicionando GoogleDorksScraper() de volta aqui.
SCRAPERS = [OLXScraper(), ZapVivaRealScraper()]


def processar_anuncio(anuncio: dict) -> bool:
    """Processa um único anúncio: deduplicação -> motor de análise ->
    notificação -> persistência. Retorna True se o imóvel foi aprovado e
    registrado no database.py (para nunca mais gerar alerta duplicado)."""
    id_origem = anuncio.get("id_origem")

    if ja_processado(id_origem):
        logger.info("ignorado motivo=ja_processado id_origem=%s", id_origem)
        return False

    aprovado = avaliar_oportunidade(anuncio)
    if aprovado is None:
        return False

    alerta_enviado = enviar_alerta_telegram(aprovado)
    if not alerta_enviado:
        logger.error("falha_alerta id_origem=%s", id_origem)

    salvar_imovel(aprovado)
    logger.info(
        "registrado_e_alertado id_origem=%s alerta_enviado=%s",
        id_origem, alerta_enviado,
    )
    return True


def executar() -> None:
    """Roda todos os scrapers configurados e processa cada anúncio
    encontrado. A falha de um scraper (ex.: portal indisponível) não
    interrompe os demais."""
    total_avaliados = 0
    total_aprovados = 0

    for scraper in SCRAPERS:
        nome_portal = getattr(scraper, "portal", scraper.__class__.__name__)
        try:
            anuncios = scraper.extrair_recentes()
        except Exception:
            logger.exception("falha_scraper portal=%s", nome_portal)
            continue

        logger.info(
            "scraper_concluido portal=%s total_extraido=%d", nome_portal, len(anuncios)
        )

        for anuncio in anuncios:
            total_avaliados += 1
            if processar_anuncio(anuncio):
                total_aprovados += 1

    logger.info(
        "execucao_concluida total_avaliados=%d total_aprovados=%d",
        total_avaliados, total_aprovados,
    )


if __name__ == "__main__":
    executar()
