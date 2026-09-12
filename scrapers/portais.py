"""Scrapers estruturados de portais imobiliários para Vitória-ES.

AVISO SOBRE FUNDAMENTAÇÃO DOS DADOS-FONTE:
Este ambiente de execução não possui acesso de saída (egress) aos domínios
olx.com.br e vivareal.com.br/zapimoveis.com.br (bloqueado pelo proxy de
rede), portanto os seletores, caminhos de JSON e parâmetros de consulta
abaixo NÃO puderam ser validados contra uma requisição real. Eles seguem
padrões estruturais publicamente documentados para esses portais (JSON
embutido em página Next.js para a OLX; página pública de busca com cards
HTML para o Grupo ZAP/VivaReal), mas cada trecho marcado com "CONFIRMAR"
precisa ser verificado manualmente (ex.: via aba de rede do navegador)
antes de uso em produção, já que ambos os portais alteram sua estrutura e
mecanismos anti-bot com frequência. O endpoint interno glue-api do
ZAP/VivaReal, usado anteriormente, foi abandonado após retornar HTTP 400
persistente mesmo com headers corretos — indício de parâmetro de consulta
inválido/desatualizado do lado do servidor.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from config import (
    BENCHMARKS_M2,
    EXIGIR_ELEVADOR,
    JANELA_MAX_HORAS,
    PRECO_MAXIMO,
    QUARTOS_MINIMO,
    REGEX_COM_ELEVADOR,
    REGEX_SEM_ELEVADOR,
)
from database import ja_processado

logger = logging.getLogger(__name__)

# Headers para simular um navegador completo. Tanto OLX quanto VivaReal
# passaram a bloquear (403) requisições identificadas como automatizadas
# apenas pelo User-Agent padrão do requests/Playwright.
HEADERS_NAVEGADOR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

# Regra 4 de validar_presenca_elevador: termo "elevador"/"elevadores" isolado,
# sem estar em um contexto de negação (já tratado pela Regra 2).
REGEX_ELEVADOR_ISOLADO = re.compile(r"\belevador(es)?\b", re.IGNORECASE)


def validar_presenca_elevador(anuncio: dict) -> bool:
    """Valida a presença de elevador a partir de atributos estruturados e do
    texto (título + descrição) do anúncio, tratando corretamente contextos de
    negação e afirmação. Espera um dict com as chaves opcionais
    "atributos_estruturados" (list), "titulo" e "descricao".

    Regras, em ordem de prioridade:
    1. Atributo estruturado das amenities contendo 'elevador'/'ELEVATOR' -> True.
    2. Negação explícita no texto (REGEX_SEM_ELEVADOR) -> False imediato.
    3. Afirmação textual (REGEX_COM_ELEVADOR) -> True.
    4. Termo isolado "elevador"/"elevadores", sem negação -> True.
    5. Caso contrário -> False (descarte seguro).
    """
    atributos_estruturados = anuncio.get("atributos_estruturados") or []
    texto = f"{anuncio.get('titulo', '')} {anuncio.get('descricao', '')}"

    for atributo in atributos_estruturados:
        valor = str(atributo).strip().lower()
        if valor in ("elevador", "elevator") or "elevador" in valor:
            return True

    if REGEX_SEM_ELEVADOR.search(texto):
        return False

    if REGEX_COM_ELEVADOR.search(texto):
        return True

    if REGEX_ELEVADOR_ISOLADO.search(texto):
        return True

    return False


class BaseScraper(ABC):
    """Interface padrão para os extratores de portais imobiliários."""

    portal: str

    @abstractmethod
    def extrair_recentes(self) -> list[dict]:
        """Retorna anúncios recentes: dentro da janela de tempo válida,
        atendendo aos critérios obrigatórios de preço/quartos/elevador,
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

    def _aplicar_criterios_obrigatorios(self, item: dict) -> bool:
        """Filtro de descarte imediato: preço máximo, quartos mínimo e
        comprovação estrita de elevador (validar_presenca_elevador), aplicado
        antes de repassar o item ao motor de análise. Retorna False (com log
        estruturado do motivo) se qualquer critério obrigatório não for
        atendido ou não puder ser comprovado a partir dos dados extraídos."""
        id_origem = item.get("id_origem")
        preco = item.get("preco")
        quartos = item.get("quartos")

        if preco is None:
            logger.info(
                "descartado motivo=preco_desconhecido portal=%s id_origem=%s",
                self.portal, id_origem,
            )
            return False

        if preco > PRECO_MAXIMO:
            logger.info(
                "descartado motivo=preco_acima_do_maximo portal=%s id_origem=%s "
                "preco=%s limite=%s",
                self.portal, id_origem, preco, PRECO_MAXIMO,
            )
            return False

        if quartos is None:
            logger.info(
                "descartado motivo=quartos_desconhecido portal=%s id_origem=%s",
                self.portal, id_origem,
            )
            return False

        if quartos < QUARTOS_MINIMO:
            logger.info(
                "descartado motivo=quartos_insuficiente portal=%s id_origem=%s "
                "quartos=%s minimo=%s",
                self.portal, id_origem, quartos, QUARTOS_MINIMO,
            )
            return False

        if EXIGIR_ELEVADOR and not validar_presenca_elevador(item):
            logger.info(
                "descartado motivo=elevador_nao_comprovado portal=%s id_origem=%s",
                self.portal, id_origem,
            )
            return False

        return True

    def _finalizar_item(self, item: dict) -> dict | None:
        """Aplica os critérios obrigatórios e, se aprovado, finaliza o item:
        remove o campo transiente "atributos_estruturados" e marca
        "elevador": True (passar pelos critérios já comprova a presença).
        Retorna None se o item for reprovado."""
        if not self._aplicar_criterios_obrigatorios(item):
            return None

        item.pop("atributos_estruturados", None)
        item["elevador"] = True
        return item

    def _buscar_html_com_fallback_playwright(self, url: str, params: dict) -> str | None:
        """Busca uma página via requests com headers de navegador; em caso
        de bloqueio HTTP 403 (anti-bot), recorre ao Playwright headless
        para renderizar a página com um navegador real."""
        try:
            resposta = requests.get(url, params=params, headers=HEADERS_NAVEGADOR, timeout=15)
        except requests.RequestException as exc:
            logger.error("falha_requisicao portal=%s erro=%s", self.portal, exc)
            return None

        if resposta.status_code == 403:
            logger.warning(
                "bloqueio_403 portal=%s motivo=provavel_anti_bot "
                "tentando_fallback=playwright",
                self.portal,
            )
            url_completa = f"{url}?{urlencode(params)}" if params else url
            return self._buscar_html_via_playwright(url_completa)

        try:
            resposta.raise_for_status()
        except requests.RequestException as exc:
            logger.error("falha_requisicao portal=%s erro=%s", self.portal, exc)
            return None

        return resposta.text

    def _buscar_html_via_playwright(self, url: str) -> str | None:
        """Renderiza a página com Chromium headless (Playwright) para
        contornar bloqueios 403 baseados em verificação de navegador real
        (JS/TLS/fingerprint), que simples ajuste de headers não resolve."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error(
                "playwright_indisponivel portal=%s "
                "motivo=biblioteca_nao_instalada_ou_browsers_ausentes",
                self.portal,
            )
            return None

        try:
            with sync_playwright() as playwright:
                navegador = playwright.chromium.launch(headless=True)
                try:
                    pagina = navegador.new_page(
                        user_agent=HEADERS_NAVEGADOR["User-Agent"],
                        locale="pt-BR",
                    )
                    # "networkidle" chegou a expirar em 30s em teste real no
                    # runner do GitHub Actions (conexões que nunca ficam
                    # ociosas — analytics, websockets etc.); "domcontentloaded"
                    # só espera o HTML inicial, suficiente para o conteúdo
                    # embutido/renderizado no primeiro paint.
                    pagina.goto(url, wait_until="domcontentloaded", timeout=30000)
                    html = pagina.content()
                finally:
                    navegador.close()
            return html
        except Exception as exc:
            # Captura ampla proposital: falhas de infraestrutura do navegador
            # (timeout de navegação, browser não instalado, crash) não devem
            # interromper a execução dos demais scrapers.
            logger.error("falha_playwright portal=%s erro=%s", self.portal, exc)
            return None

    def _card_generico_para_item(self, href: str, texto_card: str, url_absoluta: str, id_origem: str) -> dict | None:
        """Constrói um item a partir do texto visível de um card de listagem
        (usado pelos fallbacks HTML da OLX e do ZAP/VivaReal). Extrai preço,
        área, quartos e bairro via regex/casamento de texto, e a data de
        publicação a partir de badges de tempo relativo. Retorna None se não
        houver indício de data (não se arrisca a comprovar a janela temporal
        por omissão) — CONFIRMAR os formatos exatos contra o site real."""
        data_publicacao = self._parse_data_relativa(texto_card)
        if data_publicacao is None:
            logger.info(
                "descartado motivo=sem_indicio_de_data_no_card portal=%s url=%s",
                self.portal, href,
            )
            return None

        preco_match = re.search(r"R\$\s*([\d.,]+)", texto_card)
        preco = (
            self._to_float_or_none(preco_match.group(1).replace(".", "").replace(",", "."))
            if preco_match else None
        )

        area_match = re.search(r"(\d+)\s*m²", texto_card)
        area_m2 = self._to_float_or_none(area_match.group(1)) if area_match else 0.0

        quartos_match = re.search(r"(\d+)\s*quartos?\b", texto_card, re.IGNORECASE)
        quartos = self._to_int_or_none(quartos_match.group(1)) if quartos_match else None

        bairro = next((b for b in BENCHMARKS_M2 if b.lower() in texto_card.lower()), "")

        preco_m2 = round(preco / area_m2, 2) if preco and area_m2 else None

        return {
            "id_origem": id_origem,
            "portal": self.portal,
            "titulo": texto_card[:200],
            "preco": preco,
            "area_m2": area_m2,
            "preco_m2": preco_m2,
            "quartos": quartos,
            "bairro": bairro,
            "url": url_absoluta,
            "descricao": texto_card,
            "atributos_estruturados": [],
            "data_criacao_anuncio": data_publicacao.isoformat(),
            "data_criacao_anuncio_dt": data_publicacao,
        }

    _REGEX_HOJE = re.compile(r"\bhoje\b", re.IGNORECASE)
    _REGEX_ONTEM = re.compile(r"\bontem\b", re.IGNORECASE)
    _REGEX_HA_HORAS = re.compile(r"h[aá]\s*(\d+)\s*h(?:oras?)?\b", re.IGNORECASE)
    _REGEX_HA_DIAS = re.compile(r"h[aá]\s*(\d+)\s*dias?\b", re.IGNORECASE)

    @classmethod
    def _parse_data_relativa(cls, texto: str) -> datetime | None:
        # CONFIRMAR: formato exato dos badges de tempo relativo de cada portal.
        agora = datetime.now(timezone.utc)

        if cls._REGEX_HOJE.search(texto):
            return agora
        if cls._REGEX_ONTEM.search(texto):
            return agora - timedelta(days=1)

        match = cls._REGEX_HA_HORAS.search(texto)
        if match:
            return agora - timedelta(hours=int(match.group(1)))

        match = cls._REGEX_HA_DIAS.search(texto)
        if match:
            return agora - timedelta(days=int(match.group(1)))

        return None

    @staticmethod
    def _parse_data_iso(valor) -> datetime | None:
        if not valor:
            return None
        try:
            return datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _to_float_or_none(valor) -> float | None:
        if valor is None:
            return None
        try:
            return float(valor)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int_or_none(valor) -> int | None:
        if valor is None:
            return None
        try:
            return int(valor)
        except (TypeError, ValueError):
            return None


class OLXScraper(BaseScraper):
    """Extrator de anúncios de apartamentos e casas em Vitória-ES na OLX.

    A ordenação por "mais recentes" e a extração via JSON embutido
    (`__NEXT_DATA__`) seguem o padrão publicamente documentado de páginas
    Next.js da OLX. O valor do parâmetro `sf`, os parâmetros de preço
    máximo (`pe`) e quartos mínimo (`ros`), e o caminho `props.pageProps`
    estão marcados como CONFIRMAR: não puderam ser validados neste ambiente
    por bloqueio de egress a olx.com.br.
    """

    portal = "olx"
    BASE_URL = "https://www.olx.com.br/imoveis/venda/estado-es/grande-vitoria/vitoria"
    PARAMS = {
        "sf": "1",  # CONFIRMAR: ordenação por "mais recentes"
        "pe": "750000",  # CONFIRMAR: "preço até" (preço máximo)
        "ros": "3",  # CONFIRMAR: quartos mínimo
    }

    def extrair_recentes(self) -> list[dict]:
        html = self._buscar_html_com_fallback_playwright(self.BASE_URL, self.PARAMS)
        if html is None:
            return []

        dados_json = self._extrair_json_embutido(html)
        if dados_json is not None:
            anuncios = self._localizar_lista_anuncios(dados_json)
            candidatos = [
                item
                for anuncio in anuncios
                if (item := self._normalizar_anuncio(anuncio)) is not None
            ]
        else:
            logger.warning(
                "json_embutido_nao_encontrado portal=%s "
                "tentando_fallback=extracao_de_cards_html",
                self.portal,
            )
            candidatos = self._extrair_cards_html(html)

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
            propriedades = anuncio.get("properties", {}) or {}
            preco = self._to_float_or_none(anuncio.get("price"))
            area_m2 = self._to_float_or_none(propriedades.get("size")) or 0.0
            # CONFIRMAR: chave de quartos e de comodidades estruturadas.
            quartos = self._to_int_or_none(propriedades.get("rooms"))
            atributos_estruturados = propriedades.get("amenities") or []
            bairro = anuncio.get("locationDetails", {}).get("neighbourhood", "")
            url = anuncio.get("url", "")
            titulo = anuncio.get("title", "")
            descricao = anuncio.get("description", "")
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("descartado motivo=erro_parse portal=%s erro=%s", self.portal, exc)
            return None

        if data_publicacao is None:
            return None

        preco_m2 = round(preco / area_m2, 2) if preco and area_m2 else None

        item = {
            "id_origem": id_origem,
            "portal": self.portal,
            "titulo": titulo,
            "preco": preco,
            "area_m2": area_m2,
            "preco_m2": preco_m2,
            "quartos": quartos,
            "bairro": bairro,
            "url": url,
            "descricao": descricao,
            "atributos_estruturados": atributos_estruturados,
            "data_criacao_anuncio": data_publicacao.isoformat(),
            "data_criacao_anuncio_dt": data_publicacao,
        }

        return self._finalizar_item(item)

    def _extrair_cards_html(self, html: str) -> list[dict]:
        """Fallback de última instância quando o JSON embutido não é
        encontrado (ex.: estrutura da página mudou, ou a página renderizada
        é uma tela de bloqueio/captcha). Identifica cards por links de
        detalhe do anúncio (padrão conhecido publicamente: URLs de anúncio
        da OLX contêm "/vi/") — CONFIRMAR contra o HTML real."""
        soup = BeautifulSoup(html, "html.parser")
        candidatos = []
        vistos = set()

        for link in soup.select('a[href*="/vi/"]'):
            href = link.get("href", "")
            if not href or href in vistos:
                continue
            vistos.add(href)

            card = link.find_parent(["section", "li", "article", "div"]) or link
            texto_card = card.get_text(" ", strip=True)

            url = href if href.startswith("http") else f"https://www.olx.com.br{href}"
            match_id = re.search(r"-(\d+)/?$", href.rstrip("/"))
            id_origem = (
                f"olx-{match_id.group(1)}" if match_id
                else f"olx-{hashlib.sha1(href.encode('utf-8')).hexdigest()[:16]}"
            )

            item = self._card_generico_para_item(href, texto_card, url, id_origem)
            if item is None:
                continue

            item_finalizado = self._finalizar_item(item)
            if item_finalizado is not None:
                candidatos.append(item_finalizado)

        return candidatos


class ZapVivaRealScraper(BaseScraper):
    """Extrator de anúncios da página pública de busca do VivaReal (Grupo
    ZAP/VivaReal).

    Versão anterior usava o endpoint interno `glue-api.vivareal.com/v2/
    listings`, que retornou HTTP 400 persistente mesmo após ajuste de
    headers — indício de parâmetro de consulta inválido/desatualizado do
    lado do servidor. Esta versão faz scraping direto da página pública de
    busca (`quartos`/`preco-ate` na própria URL), com o mesmo fallback via
    Playwright usado pela OLX em caso de bloqueio 403. Os seletores de card
    (identificados por links "/imovel/") e o formato dos badges de tempo
    relativo estão marcados como CONFIRMAR: não puderam ser validados neste
    ambiente por bloqueio de egress a vivareal.com.br.
    """

    portal = "zap_vivareal"
    BASE_URL = "https://www.vivareal.com.br/venda/espirito-santo/vitoria/apartamento_residencial/"
    PARAMS = {
        "quartos": "3",
        "preco-ate": "750000",
    }

    def extrair_recentes(self) -> list[dict]:
        html = self._buscar_html_com_fallback_playwright(self.BASE_URL, self.PARAMS)
        if html is None:
            return []

        candidatos = self._extrair_cards_html(html)
        return self._filtrar_e_logar(candidatos)

    def _extrair_cards_html(self, html: str) -> list[dict]:
        """CONFIRMAR: seletor de cards não pôde ser validado contra o site
        real. Heurística: links de detalhe do anúncio contêm "/imovel/"
        (padrão publicamente conhecido de URLs de anúncio do VivaReal)."""
        soup = BeautifulSoup(html, "html.parser")
        candidatos = []
        vistos = set()

        for link in soup.select('a[href*="/imovel/"]'):
            href = link.get("href", "")
            if not href or href in vistos:
                continue
            vistos.add(href)

            card = link.find_parent(["section", "li", "article", "div"]) or link
            texto_card = card.get_text(" ", strip=True)

            url = href if href.startswith("http") else f"https://www.vivareal.com.br{href}"
            match_id = re.search(r"-(\d+)/?$", href.rstrip("/")) or re.search(r"id-?(\d+)", href, re.IGNORECASE)
            id_origem = (
                f"zap-{match_id.group(1)}" if match_id
                else f"zap-{hashlib.sha1(href.encode('utf-8')).hexdigest()[:16]}"
            )

            item = self._card_generico_para_item(href, texto_card, url, id_origem)
            if item is None:
                continue

            item_finalizado = self._finalizar_item(item)
            if item_finalizado is not None:
                candidatos.append(item_finalizado)

        return candidatos
