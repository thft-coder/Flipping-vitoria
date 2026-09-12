"""Buscador de oportunidades via Google Custom Search API ("google dorks")
para imóveis em Vitória-ES.

FUNDAMENTAÇÃO E LIMITAÇÕES:
Este módulo usa a Google Custom Search JSON API oficial (endpoint
`https://www.googleapis.com/customsearch/v1`), e não faz scraping do HTML
de resultados do google.com/search — essa segunda abordagem violaria os
Termos de Serviço do Google e é ativamente bloqueada por captcha/anti-bot,
sendo inviável de forma confiável.

Os nomes de parâmetros usados aqui (`key`, `cx`, `q`, `num`, `dateRestrict`)
e os campos da resposta (`items[].title`, `.link`, `.snippet`) são os da
API pública e documentada do Google Custom Search, estável desde seu
lançamento. Não foi possível, no entanto, consultar a documentação oficial
neste ambiente para confirmar em tempo real (egress bloqueado para
developers.google.com), então recomenda-se validar uma chamada real antes
do uso em produção.

Requer as credenciais GOOGLE_API_KEY e GOOGLE_CSE_ID no .env, referentes a
uma chave de API do Google Cloud e a um Mecanismo de Pesquisa Programável
(Custom Search Engine) configurado para buscar em toda a web.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from config import BENCHMARKS_M2, JANELA_MAX_HORAS
from scrapers.portais import validar_presenca_elevador

load_dotenv()

logger = logging.getLogger(__name__)

ENDPOINT = "https://www.googleapis.com/customsearch/v1"

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID")

# Termos mandatórios para quantidade de quartos (Regra: exatamente esta
# cláusula OR deve constar na busca).
TERMOS_QUARTOS = ["3 quartos", "3 dorms", "3 qts"]

# Termos de oportunidade específicos para a busca via dorks. É um subconjunto
# curado de config.TERMOS_OPORTUNIDADE (que também inclui "partilha" e
# "motivo de mudança"), conforme especificado para esta consulta.
TERMOS_OPORTUNIDADE_DORK = ["reforma", "original", "inventário", "urgente"]

BAIRROS_PRIORITARIOS = list(BENCHMARKS_M2.keys())


def _clausula_or(termos: list[str]) -> str:
    """Monta uma cláusula "(\"a\" OR \"b\" OR ...)" no formato aceito pela
    sintaxe de busca do Google."""
    return "(" + " OR ".join(f'"{termo}"' for termo in termos) + ")"


def montar_query() -> str:
    """Monta a query de busca ("dork") combinando, em cláusulas AND:
    quartos (OR), elevador, segmentação geográfica (Vitória + bairros
    prioritários, OR) e termos de oportunidade (OR)."""
    clausula_quartos = _clausula_or(TERMOS_QUARTOS)
    clausula_bairros = _clausula_or(BAIRROS_PRIORITARIOS)
    clausula_oportunidade = _clausula_or(TERMOS_OPORTUNIDADE_DORK)

    return (
        f"{clausula_quartos} elevador Vitória {clausula_bairros} "
        f"{clausula_oportunidade}"
    )


def _dias_da_janela() -> int:
    """Converte config.JANELA_MAX_HORAS em dias inteiros para o parâmetro
    dateRestrict da Custom Search API, cuja granularidade é diária
    (formato "dN" = indexado nos últimos N dias)."""
    return max(1, math.ceil(JANELA_MAX_HORAS / 24))


class GoogleDorksScraper:
    """Busca oportunidades via Google Custom Search API usando uma query
    "dork" com os critérios obrigatórios de quartos, elevador, segmentação
    geográfica e termos de oportunidade, restrita a indexações recentes."""

    portal = "google_dorks"

    def __init__(self, query: str | None = None):
        self.query = query or montar_query()

    def extrair_recentes(self) -> list[dict]:
        if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
            logger.error(
                "credenciais_ausentes portal=%s "
                "motivo=GOOGLE_API_KEY_ou_GOOGLE_CSE_ID_nao_configurados",
                self.portal,
            )
            return []

        params = {
            "key": GOOGLE_API_KEY,
            "cx": GOOGLE_CSE_ID,
            "q": self.query,
            "dateRestrict": f"d{_dias_da_janela()}",
            "num": 10,
        }

        try:
            resposta = requests.get(ENDPOINT, params=params, timeout=15)
            resposta.raise_for_status()
            payload = resposta.json()
        except (requests.RequestException, ValueError) as exc:
            logger.error("falha_requisicao portal=%s erro=%s", self.portal, exc)
            return []

        resultados = payload.get("items", []) or []
        return [
            item
            for resultado in resultados
            if (item := self._normalizar_resultado(resultado)) is not None
        ]

    def _normalizar_resultado(self, resultado: dict) -> dict | None:
        titulo = resultado.get("title", "")
        url = resultado.get("link", "")
        snippet = resultado.get("snippet", "")

        if not url:
            return None

        id_origem = f"google-{hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]}"

        anuncio = {
            "titulo": titulo,
            "descricao": snippet,
            "atributos_estruturados": [],
        }

        if not validar_presenca_elevador(anuncio):
            logger.info(
                "descartado motivo=elevador_nao_comprovado portal=%s id_origem=%s url=%s",
                self.portal, id_origem, url,
            )
            return None

        return {
            "id_origem": id_origem,
            "portal": self.portal,
            "titulo": titulo,
            "url": url,
            "snippet": snippet,
            "elevador": True,
            "data_coleta": datetime.now(timezone.utc).isoformat(),
        }
