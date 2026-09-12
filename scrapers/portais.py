"""Scrapers estruturados de portais imobiliários para Vitória-ES.

AVISO SOBRE FUNDAMENTAÇÃO DOS DADOS-FONTE:
Este ambiente de execução não possui acesso de saída (egress) aos domínios
olx.com.br e vivareal.com.br/zapimoveis.com.br (bloqueado pelo proxy de
rede), portanto os seletores, caminhos de JSON e parâmetros de consulta
abaixo NÃO puderam ser validados contra uma requisição real. Eles seguem
padrões estruturais publicamente documentados para esses portais (JSON
embutido em página Next.js para a OLX; endpoint público de busca
"glue-api" para o Grupo ZAP/VivaReal), mas cada trecho marcado com
"CONFIRMAR" precisa ser verificado manualmente (ex.: via aba de rede do
navegador) antes de uso em produção, já que ambos os portais alteram sua
estrutura e mecanismos anti-bot com frequência.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

from config import JANELA_MAX_HORAS
from database import ja_processado

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


class BaseScraper(ABC):
    """Interface padrão para os extratores de portais imobiliários."""

    portal: str

    @abstractmethod
    def extrair_recentes(self) -> list[dict]:
        """Retorna anúncios recentes: dentro da janela de tempo válida,
        ainda não presentes no database.py e normalizados para o schema
        de imoveis_processados."""
        raise NotImplementedError

    @staticmethod
    def _dentro_da_janela(data_publicacao: datetime) -> bool:
        if data_publicacao.tzinfo is None:
            data_publicacao = data_publicacao.replace(tzinfo=timezone.utc)
        limite = datetime.now(timezone.utc) - timedelta(hours=JANELA_MAX_HORAS)
        return data_publicacao >= limite

    def _filtrar_e_logar(self, candidatos: list[dict]) -> list[dict]:
        """Aplica a validação temporal estrita (rejeita anúncios com
        publicação anterior a JANELA_MAX_HORAS) e a deduplicação contra o
        database.py, emitindo log estruturado para cada item descartado."""
        validos = []
        for item in candidatos:
            id_origem = item.get("id_origem")
            data_publicacao = item.pop("data_criacao_anuncio_dt", None)

            if data_publicacao is None:
                logger.warning(
                    "descartado motivo=sem_data_publicacao portal=%s id_origem=%s",
                    self.portal, id_origem,
                )
                continue

            if not self._dentro_da_janela(data_publicacao):
                logger.info(
                    "descartado motivo=fora_da_janela portal=%s id_origem=%s "
                    "data_publicacao=%s janela_horas=%s",
                    self.portal, id_origem, data_publicacao.isoformat(),
                    JANELA_MAX_HORAS,
                )
                continue

            if ja_processado(id_origem):
                logger.info(
                    "descartado motivo=ja_processado portal=%s id_origem=%s",
                    self.portal, id_origem,
                )
                continue

            validos.append(item)

        return validos

    @staticmethod
    def _parse_data_iso(valor) -> datetime | None:
        if not valor:
            return None
        try:
            return datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
        except ValueError:
            return None


class OLXScraper(BaseScraper):
    """Extrator de anúncios de apartamentos e casas em Vitória-ES na OLX.

    A ordenação por "mais recentes" e a extração via JSON embutido
    (`__NEXT_DATA__`) seguem o padrão publicamente documentado de páginas
    Next.js da OLX. O valor do parâmetro `sf` e o caminho `props.pageProps`
    estão marcados como CONFIRMAR: não puderam ser validados neste ambiente
    por bloqueio de egress a olx.com.br.
    """

    portal = "olx"
    BASE_URL = "https://www.olx.com.br/imoveis/venda/estado-es/grande-vitoria/vitoria"
    PARAMS = {"sf": "1"}  # CONFIRMAR: parâmetro de ordenação "mais recentes"

    def extrair_recentes(self) -> list[dict]:
        try:
            resposta = requests.get(
                self.BASE_URL, params=self.PARAMS, headers=HEADERS, timeout=15
            )
            resposta.raise_for_status()
        except requests.RequestException as exc:
            logger.error("falha_requisicao portal=%s erro=%s", self.portal, exc)
            return []

        dados_json = self._extrair_json_embutido(resposta.text)
        if dados_json is None:
            logger.error("json_embutido_nao_encontrado portal=%s", self.portal)
            return []

        anuncios = self._localizar_lista_anuncios(dados_json)
        candidatos = [
            item
            for anuncio in anuncios
            if (item := self._normalizar_anuncio(anuncio)) is not None
        ]
        return self._filtrar_e_logar(candidatos)

    @staticmethod
    def _extrair_json_embutido(html: str) -> dict | None:
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script or not script.string:
            return None
        try:
            return json.loads(script.string)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _localizar_lista_anuncios(dados_json: dict) -> list[dict]:
        # CONFIRMAR: caminho do JSON onde a lista de anúncios é publicada.
        page_props = dados_json.get("props", {}).get("pageProps", {})
        return page_props.get("ads", []) or []

    def _normalizar_anuncio(self, anuncio: dict) -> dict | None:
        try:
            id_origem = f"olx-{anuncio['listId']}"
            data_publicacao = self._parse_data_iso(anuncio.get("date"))
            preco = float(anuncio.get("price", 0) or 0)
            area_m2 = float(anuncio.get("properties", {}).get("size", 0) or 0)
            bairro = anuncio.get("locationDetails", {}).get("neighbourhood", "")
            url = anuncio.get("url", "")
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("descartado motivo=erro_parse portal=%s erro=%s", self.portal, exc)
            return None

        if data_publicacao is None:
            return None

        preco_m2 = round(preco / area_m2, 2) if area_m2 else None

        return {
            "id_origem": id_origem,
            "portal": self.portal,
            "titulo": anuncio.get("title", ""),
            "preco": preco,
            "area_m2": area_m2,
            "preco_m2": preco_m2,
            "bairro": bairro,
            "url": url,
            "descricao": anuncio.get("description", ""),
            "data_criacao_anuncio": data_publicacao.isoformat(),
            "data_criacao_anuncio_dt": data_publicacao,
        }


class ZapVivaRealScraper(BaseScraper):
    """Extrator de anúncios via endpoint público de busca do Grupo ZAP/VivaReal.

    O endpoint `glue-api.vivareal.com/v2/listings` é o utilizado publicamente
    pelo próprio site (VivaReal e ZAP Imóveis compartilham a mesma origem de
    dados) para popular resultados de busca via XHR. Os nomes de parâmetros e
    o caminho dos campos na resposta estão marcados como CONFIRMAR: não
    puderam ser validados neste ambiente por bloqueio de egress a
    vivareal.com.br/zapimoveis.com.br.
    """

    portal = "zap_vivareal"
    BASE_URL = "https://glue-api.vivareal.com/v2/listings"
    PARAMS = {
        "businessType": "SALE",
        "unitTypes": "APARTMENT,HOME",
        "addressCity": "Vitória",
        "addressState": "Espírito Santo",
        "addressCountry": "Brasil",
        "sort": "most_recent",  # CONFIRMAR: valor exato do parâmetro de ordenação
        "size": "50",
        "from": "0",
    }

    def extrair_recentes(self) -> list[dict]:
        try:
            resposta = requests.get(
                self.BASE_URL, params=self.PARAMS, headers=HEADERS, timeout=15
            )
            resposta.raise_for_status()
            payload = resposta.json()
        except (requests.RequestException, ValueError) as exc:
            logger.error("falha_requisicao portal=%s erro=%s", self.portal, exc)
            return []

        resultados = self._localizar_lista_resultados(payload)
        candidatos = [
            item
            for resultado in resultados
            if (item := self._normalizar_resultado(resultado)) is not None
        ]
        return self._filtrar_e_logar(candidatos)

    @staticmethod
    def _localizar_lista_resultados(payload: dict) -> list[dict]:
        # CONFIRMAR: caminho exato do JSON de resposta do glue-api.
        return payload.get("search", {}).get("result", {}).get("listings", []) or []

    def _normalizar_resultado(self, resultado: dict) -> dict | None:
        try:
            listagem = resultado.get("listing", {})
            id_origem = f"zap-{listagem['id']}"
            data_publicacao = self._parse_data_iso(
                listagem.get("updatedAt") or listagem.get("createdAt")
            )
            preco = float((listagem.get("pricingInfos") or [{}])[0].get("price", 0) or 0)
            area_m2 = float((listagem.get("usableAreas") or [0])[0] or 0)
            endereco = listagem.get("address", {})
            bairro = endereco.get("neighborhood", "")
            url = f"https://www.vivareal.com.br/imovel/{listagem['id']}/"
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            logger.warning("descartado motivo=erro_parse portal=%s erro=%s", self.portal, exc)
            return None

        if data_publicacao is None:
            return None

        preco_m2 = round(preco / area_m2, 2) if area_m2 else None

        return {
            "id_origem": id_origem,
            "portal": self.portal,
            "titulo": listagem.get("title", ""),
            "preco": preco,
            "area_m2": area_m2,
            "preco_m2": preco_m2,
            "bairro": bairro,
            "url": url,
            "descricao": listagem.get("description", ""),
            "data_criacao_anuncio": data_publicacao.isoformat(),
            "data_criacao_anuncio_dt": data_publicacao,
        }
