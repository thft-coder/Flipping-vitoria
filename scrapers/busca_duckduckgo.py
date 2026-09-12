"""Buscador de oportunidades via DuckDuckGo ("dorks" de busca web) para
imóveis em Vitória-ES.

FUNDAMENTAÇÃO E LIMITAÇÕES:
Substituiu o GoogleDorksScraper (Google Custom Search API), que exigia
GOOGLE_API_KEY/GOOGLE_CSE_ID configurados no Google Cloud Console e
retornou 403 Forbidden mesmo com credenciais presentes (problema de
configuração externa — API não habilitada, restrição de chave incompatível
ou cota excedida — nunca diagnosticado por falta de acesso ao Console).

Usa a biblioteca `ddgs` (sucessora do pacote `duckduckgo-search`, mesmo
mantenedor), especificando `backend="duckduckgo"` para consultar
especificamente o DuckDuckGo (a lib também agrega outros motores, mas não
são usados aqui). Verificado neste ambiente antes de implementar: a API
pública do pacote (`DDGS().text(query, region=..., backend=...)`) e o
schema de resultado (`title`/`href`/`body`) foram confirmados via
inspeção do código-fonte instalado (ddgs 9.16.0). Uma chamada real não
pôde ser testada aqui — `html.duckduckgo.com` está bloqueado pelo mesmo
proxy de egress que já bloqueia outros domínios usados neste projeto
(olx.com.br, vivareal.com.br) — mas deve funcionar no GitHub Actions,
onde essa restrição não existe.

IMPORTANTE: assim como a OLX, o `ddgs` não é uma API oficialmente
sancionada pelo DuckDuckGo para uso automatizado — é uma biblioteca
open-source que faz scraping do endpoint HTML público deles. Sujeita aos
mesmos riscos já vividos neste projeto: bloqueio por IP/rate-limit ou
mudança de estrutura sem aviso. Vantagem sobre a Custom Search API: não
depende de credencial nem configuração externa.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from ddgs import DDGS
from ddgs.exceptions import DDGSException

from config import BENCHMARKS_M2
from scrapers.portais import validar_presenca_elevador

logger = logging.getLogger(__name__)

# Termos mandatórios para quantidade de quartos (Regra: exatamente esta
# cláusula OR deve constar na busca).
TERMOS_QUARTOS = ["3 quartos", "3 dorms", "3 qts"]

# Termos de oportunidade específicos para esta busca. É um subconjunto
# curado de config.TERMOS_OPORTUNIDADE (que também inclui "partilha" e
# "motivo de mudança"), conforme especificado originalmente para esta
# consulta.
TERMOS_OPORTUNIDADE_DORK = ["reforma", "original", "inventário", "urgente"]

BAIRROS_PRIORITARIOS = list(BENCHMARKS_M2.keys())


def _clausula_or(termos: list[str]) -> str:
    """Monta uma cláusula "(\"a\" OR \"b\" OR ...)" no formato de operador
    de busca aceito pelo DuckDuckGo (e pela maioria dos motores de busca)."""
    return "(" + " OR ".join(f'"{termo}"' for termo in termos) + ")"


def montar_query() -> str:
    """Monta a query de busca combinando, em cláusulas AND: quartos (OR),
    segmentação geográfica (Vitória + bairros prioritários, OR) e termos de
    oportunidade (OR).

    O termo "elevador" foi removido da query (era um AND literal obrigatório
    junto aos demais). Com EXIGIR_ELEVADOR=False, elevador não é mais
    critério eliminatório em nenhum outro scraper deste projeto — mantê-lo
    como cláusula AND aqui era inconsistente com essa decisão e, em uma
    query já combinando 3 cláusulas OR distintas, reduz ainda mais a chance
    de casar com qualquer resultado real do motor de busca."""
    clausula_quartos = _clausula_or(TERMOS_QUARTOS)
    clausula_bairros = _clausula_or(BAIRROS_PRIORITARIOS)
    clausula_oportunidade = _clausula_or(TERMOS_OPORTUNIDADE_DORK)

    return (
        f"{clausula_quartos} Vitória {clausula_bairros} "
        f"{clausula_oportunidade}"
    )


class DuckDuckGoScraper:
    """Busca oportunidades via DuckDuckGo usando uma query combinando os
    critérios de quartos, elevador, segmentação geográfica e termos de
    oportunidade. Não possui filtro temporal server-side confiável (o
    parâmetro `timelimit` do DuckDuckGo tem granularidade de dia/semana/
    mês/ano, não é possível pedir exatamente 48h) — usa "d" (último dia)
    como aproximação mais restritiva disponível."""

    portal = "duckduckgo"

    def __init__(self, query: str | None = None):
        self.query = query or montar_query()

    def extrair_recentes(self) -> list[dict]:
        try:
            resultados = DDGS(timeout=15).text(
                self.query,
                region="br-pt",
                timelimit="d",
                max_results=10,
                backend="duckduckgo",
            )
        except DDGSException as exc:
            logger.error("falha_requisicao portal=%s erro=%s", self.portal, exc)
            return []

        return [
            item
            for resultado in resultados
            if (item := self._normalizar_resultado(resultado)) is not None
        ]

    def _normalizar_resultado(self, resultado: dict) -> dict | None:
        titulo = resultado.get("title", "")
        url = resultado.get("href", "")
        corpo = resultado.get("body", "")

        if not url:
            return None

        id_origem = f"duckduckgo-{hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]}"

        anuncio = {
            "titulo": titulo,
            "descricao": corpo,
            "atributos_estruturados": [],
        }
        # Elevador não é mais critério eliminatório (config.EXIGIR_ELEVADOR):
        # calculado e repassado, sem descartar o resultado por isso.
        elevador_confirmado = validar_presenca_elevador(anuncio)

        return {
            "id_origem": id_origem,
            "portal": self.portal,
            "titulo": titulo,
            "url": url,
            "snippet": corpo,
            "elevador": elevador_confirmado,
            "data_coleta": datetime.now(timezone.utc).isoformat(),
        }
