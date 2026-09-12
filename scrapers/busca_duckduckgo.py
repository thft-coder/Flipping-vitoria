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
# cláusula OR deve constar na busca). Reduzido de 6 para 3 termos: a
# execução real mostrou HTTP 202 (provável mitigação anti-bot do endpoint
# HTML do DuckDuckGo) após ampliar a query para muitas cláusulas OR
# agrupadas; simplificar reduz o número de operadores booleanos na busca.
TERMOS_QUARTOS = ["3 quartos", "3 dormitórios", "3 qts"]

# Termos de oportunidade específicos para esta busca, também reduzidos
# para simplificar a query pelo mesmo motivo.
TERMOS_OPORTUNIDADE_DORK = ["reforma", "urgente", "oportunidade"]

BAIRROS_PRIORITARIOS = list(BENCHMARKS_M2.keys())


def _clausula_or(termos: list[str]) -> str:
    """Monta uma cláusula "(\"a\" OR \"b\" OR ...)" no formato de operador
    de busca aceito pelo DuckDuckGo (e pela maioria dos motores de busca)."""
    return "(" + " OR ".join(f'"{termo}"' for termo in termos) + ")"


def montar_query() -> str:
    """Monta a query de busca combinando, em cláusulas AND: segmentação
    geográfica (Vitória + bairros prioritários, OR), quartos (OR) e termos
    de oportunidade (OR).

    O termo "elevador" foi removido da query (era um AND literal obrigatório
    junto aos demais, inconsistente com EXIGIR_ELEVADOR=False). Query
    simplificada (3 cláusulas OR de 3 termos cada, no lugar de conjuntos
    maiores) após uma execução real retornar HTTP 202 do endpoint HTML do
    DuckDuckGo, provável mitigação anti-bot sensível ao número de
    operadores booleanos agrupados na busca."""
    clausula_quartos = _clausula_or(TERMOS_QUARTOS)
    clausula_bairros = _clausula_or(BAIRROS_PRIORITARIOS)
    clausula_oportunidade = _clausula_or(TERMOS_OPORTUNIDADE_DORK)

    return (
        f"Vitória {clausula_quartos} {clausula_bairros} "
        f"{clausula_oportunidade}"
    )


class DuckDuckGoScraper:
    """Busca oportunidades via DuckDuckGo usando uma query combinando os
    critérios de quartos, segmentação geográfica e termos de oportunidade.

    Não usa mais o parâmetro `timelimit` do DuckDuckGo (removido: uma
    execução real em produção mostrou 0 resultados mesmo após afrouxar a
    query, com `timelimit="d"` restringindo a busca a conteúdo indexado
    nas últimas 24h — a interseção entre "indexado há <1 dia" e "contém uma
    das frases exatas buscadas" é um filtro muito mais restritivo do que
    parece à primeira vista). A garantia de novidade passa a depender
    inteiramente da deduplicação por id_origem em database.py, do mesmo
    modo já usado pelo ZAP/VivaReal e pelo fallback HTML da OLX."""

    portal = "duckduckgo"

    def __init__(self, query: str | None = None):
        self.query = query or montar_query()

    def extrair_recentes(self) -> list[dict]:
        try:
            resultados = DDGS(timeout=15).text(
                self.query,
                region="br-pt",
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
