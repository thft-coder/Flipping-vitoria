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
são usados aqui — "lite"/"api" não existem como engines registradas na
versão instalada, confirmado via inspeção do código-fonte).

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
import re
from datetime import datetime, timezone

from ddgs import DDGS
from ddgs.exceptions import DDGSException

from scrapers.portais import BaseScraper, REGEX_QUARTOS, validar_presenca_elevador

logger = logging.getLogger(__name__)


def montar_query() -> str:
    """Query "descomprimida" semanticamente: poucos termos, sem exigir os
    nomes dos bairros nem múltiplas cláusulas OR agrupadas.

    Versão anterior (3 cláusulas OR de bairros + quartos + oportunidade,
    todas em AND) retornou 0 resultados reais mesmo após simplificações
    sucessivas — a interseção de várias frases exatas obrigatórias é
    restritiva demais para o índice do motor de busca. Esta versão foca
    em indexação ampla de anúncios/imobiliárias locais (só "apartamento
    3 quartos venda vitoria" como termos livres, mais uma cláusula OR de
    oportunidade) e delega o filtro de bairro para depois da busca: cada
    URL retornada é inspecionada (_extrair_bairro, REGEX_QUARTOS) e só é
    aprovada se realmente for de um dos 5 bairros monitorados — do mesmo
    jeito que uma pessoa faria lendo o snippet do resultado."""
    return "apartamento 3 quartos venda vitoria (oportunidade OR reforma OR desocupado)"


class DuckDuckGoScraper:
    """Busca oportunidades via DuckDuckGo usando uma query ampla e deixa o
    filtro de bairro, preço e quartos por conta da inspeção do conteúdo de
    cada resultado (título + snippet), do mesmo jeito que os fallbacks HTML
    da OLX e do ZAP/VivaReal fazem com o texto do card.

    Não usa o parâmetro `timelimit` do DuckDuckGo (removido antes: uma
    execução real mostrou 0 resultados com `timelimit="d"` mesmo já com a
    query mais solta). A garantia de novidade depende inteiramente da
    deduplicação por id_origem em database.py."""

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
        texto_completo = f"{titulo} {corpo}"

        # Extração estrutural a partir do título + snippet, com a mesma
        # lógica usada pelos fallbacks HTML da OLX/ZAP (_card_generico_
        # para_item). Necessário: sem preco/area_m2/quartos/bairro
        # preenchidos, o item é reprovado de imediato em
        # engine.avaliar_oportunidade (preco is None) e nunca chega a ser
        # salvo em database.salvar_imovel (que exige esses campos) —
        # nenhum resultado do DuckDuckGo poderia ser aprovado antes desta
        # extração existir, independentemente da query.
        preco_match = re.search(r"R\$\s*([\d.,]+)", texto_completo)
        preco = (
            BaseScraper._to_float_or_none(
                preco_match.group(1).replace(".", "").replace(",", ".")
            )
            if preco_match else None
        )
        if preco is None:
            # Diagnóstico: execuções reais mostraram total_extraido=10 mas
            # quase todos os itens reprovados por preco=None (mesmo padrão
            # de IDs repetido entre execuções, indício de que a busca
            # retorna sempre os mesmos resultados estáticos). Sem ver o
            # texto bruto não dá pra saber se é: página agregadora sem
            # preço isolado no snippet, formatação de preço diferente
            # (ex.: sem "R$", "a partir de"), ou outro motivo — registrar
            # a amostra aqui, em vez de arriscar um ajuste de regex às
            # cegas.
            logger.info(
                "preco_nao_encontrado portal=%s id_origem=%s url=%s "
                "amostra_texto=%r",
                self.portal, id_origem, url, texto_completo[:300],
            )

        area_match = re.search(r"(\d+)\s*m²", texto_completo)
        area_m2 = BaseScraper._to_float_or_none(area_match.group(1)) if area_match else 0.0

        quartos_match = REGEX_QUARTOS.search(texto_completo)
        quartos = BaseScraper._to_int_or_none(quartos_match.group(1)) if quartos_match else None

        # Filtro de bairro delegado à inspeção do conteúdo, não à query:
        # descarta (bairro="") qualquer resultado fora dos 5 bairros
        # monitorados quando chegar em engine.avaliar_oportunidade
        # (motivo=bairro_nao_mapeado), sem precisar do nome do bairro na
        # busca em si.
        bairro = BaseScraper._extrair_bairro(texto_completo, url)

        anuncio = {
            "titulo": titulo,
            "descricao": corpo,
            "atributos_estruturados": [],
        }
        # Elevador não é mais critério eliminatório (config.EXIGIR_ELEVADOR):
        # calculado e repassado, sem descartar o resultado por isso.
        elevador_confirmado = validar_presenca_elevador(anuncio)

        agora = datetime.now(timezone.utc).isoformat()

        return {
            "id_origem": id_origem,
            "portal": self.portal,
            "titulo": titulo,
            "descricao": corpo,
            "preco": preco,
            "area_m2": area_m2,
            "quartos": quartos,
            "bairro": bairro,
            "url": url,
            "elevador": elevador_confirmado,
            "data_criacao_anuncio": agora,
            "data_coleta": agora,
        }
