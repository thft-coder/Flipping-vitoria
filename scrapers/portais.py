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
from collections import Counter
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
        remove o campo transiente "atributos_estruturados" e calcula
        "elevador" com validar_presenca_elevador. Com EXIGIR_ELEVADOR=False
        (critério não é mais eliminatório), o item pode passar mesmo com
        elevador=False — o valor reflete o que foi realmente comprovado no
        texto/atributos, nunca é fixado como True por suposição. Retorna
        None se o item for reprovado em algum critério ainda eliminatório."""
        if not self._aplicar_criterios_obrigatorios(item):
            return None

        elevador_confirmado = validar_presenca_elevador(item)
        item.pop("atributos_estruturados", None)
        item["elevador"] = elevador_confirmado
        return item

    def _buscar_html_com_fallback_playwright(
        self, url: str, params: dict, selector_espera: str | None = None
    ) -> str | None:
        """Busca uma página via requests com headers de navegador; em caso
        de bloqueio HTTP 403 (anti-bot), recorre ao Playwright headless
        para renderizar a página com um navegador real. `selector_espera`
        (opcional) é um seletor CSS aguardado explicitamente após o
        carregamento, para dar tempo à hidratação de conteúdo React antes
        de capturar o HTML final (ver `_buscar_html_via_playwright`)."""
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
            return self._buscar_html_via_playwright(url_completa, selector_espera)

        try:
            resposta.raise_for_status()
        except requests.RequestException as exc:
            logger.error("falha_requisicao portal=%s erro=%s", self.portal, exc)
            return None

        return resposta.text

    # Marcadores textuais de uma página de desafio do Cloudflare (Turnstile/
    # "Just a moment..."), usados apenas para diagnóstico em log — CONFIRMAR
    # contra o HTML real, pois a redação exata pode variar.
    _MARCADORES_BLOQUEIO_CLOUDFLARE = [
        "just a moment",
        "attention required",
        "checking your browser",
        "cf-challenge",
        "cf_chl_opt",
        "turnstile",
    ]

    @classmethod
    def _diagnosticar_bloqueio(cls, html: str) -> str | None:
        """Verifica se o HTML recebido contém marcadores conhecidos de
        página de desafio do Cloudflare. Retorna o marcador encontrado, ou
        None se nenhum bater (o que não garante ausência de bloqueio — pode
        ser um mecanismo diferente, por isso o HTML também é logado)."""
        texto = (html or "").lower()
        for marcador in cls._MARCADORES_BLOQUEIO_CLOUDFLARE:
            if marcador in texto:
                return marcador
        return None

    def _salvar_html_diagnostico(self, html: str) -> None:
        """Salva o HTML recebido em disco (debug_<portal>_html.html) para
        inspeção via artifact do GitHub Actions, e loga um resumo
        estrutural (ids de <script>, prefixos de href mais comuns,
        presença de termos-chave) diretamente no log do job — usado só
        quando a extração normal falha, para diagnosticar a estrutura real
        da página em vez de seguir supondo seletores às cegas."""
        caminho = f"debug_{self.portal}_html.html"
        try:
            with open(caminho, "w", encoding="utf-8") as arquivo:
                arquivo.write(html)
            logger.info("html_diagnostico_salvo portal=%s caminho=%s", self.portal, caminho)
        except OSError as exc:
            logger.warning("falha_salvar_html_diagnostico portal=%s erro=%s", self.portal, exc)

        self._logar_resumo_estrutural(html)

    def _logar_resumo_estrutural(self, html: str) -> None:
        soup = BeautifulSoup(html, "html.parser")

        ids_de_script = [s.get("id") for s in soup.find_all("script") if s.get("id")]
        logger.info(
            "diagnostico_scripts_com_id portal=%s total=%d ids=%r",
            self.portal, len(ids_de_script), ids_de_script[:20],
        )

        hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
        # CONFIRMAR (bug corrigido): a versão anterior exigia >=2 barras no
        # href, excluindo silenciosamente padrões de 1 segmento comuns em
        # anúncios de classificados (ex.: "/titulo-do-anuncio-1234567890").
        prefixos = Counter(
            href.split("?")[0].rsplit("/", 1)[0] or "/"
            for href in hrefs
            if href.startswith("/")
        )
        logger.info(
            "diagnostico_prefixos_href portal=%s total_links=%d mais_comuns=%r",
            self.portal, len(hrefs), prefixos.most_common(15),
        )

        # Sinal mais direto que agregação de prefixo: amostra de hrefs que
        # terminam em sequência longa de dígitos — assinatura comum de ID
        # de anúncio em classificados, independente da estrutura de path.
        links_com_id_numerico = [h for h in hrefs if re.search(r"-\d{6,}/?(?:\?.*)?$", h)]
        logger.info(
            "diagnostico_links_com_id_numerico portal=%s total=%d amostra=%r",
            self.portal, len(links_com_id_numerico), links_com_id_numerico[:10],
        )

        texto_lower = html.lower()
        logger.info(
            "diagnostico_termos_chave portal=%s contem_quartos=%s contem_preco=%s "
            "contem_apartamento=%s",
            self.portal,
            "quartos" in texto_lower,
            "r$" in texto_lower,
            "apartamento" in texto_lower,
        )

    def _buscar_html_via_playwright(self, url: str, selector_espera: str | None = None) -> str | None:
        """Renderiza a página com Chromium headless (Playwright) para
        contornar bloqueios 403 baseados em verificação de navegador real
        (JS/TLS/fingerprint), que simples ajuste de headers não resolve.
        Aplica evasões de fingerprint (playwright-stealth) e simula um
        contexto de navegador real (viewport, locale e fuso horário de
        Vitória-ES) — mitiga detecção por automação, mas não contorna um
        bloqueio por reputação de IP/ASN (datacenter do GitHub Actions),
        que exigiria um proxy residencial, fora do escopo desta mudança."""
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
            from playwright_stealth import Stealth
            stealth = Stealth(navigator_languages_override=("pt-BR", "pt"))
        except ImportError:
            logger.warning(
                "playwright_stealth_indisponivel portal=%s "
                "motivo=biblioteca_nao_instalada_seguindo_sem_evasao",
                self.portal,
            )
            stealth = None

        try:
            with sync_playwright() as playwright:
                navegador = playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-infobars",
                    ],
                )
                try:
                    contexto = navegador.new_context(
                        user_agent=HEADERS_NAVEGADOR["User-Agent"],
                        viewport={"width": 1366, "height": 768},
                        locale="pt-BR",
                        timezone_id="America/Sao_Paulo",
                    )
                    pagina = contexto.new_page()
                    if stealth is not None:
                        stealth.apply_stealth_sync(pagina)

                    # "networkidle" chegou a expirar em 30s em teste real no
                    # runner do GitHub Actions (conexões que nunca ficam
                    # ociosas — analytics, websockets etc.); "domcontentloaded"
                    # só espera o HTML inicial.
                    pagina.goto(url, wait_until="domcontentloaded", timeout=45000)
                    pagina.wait_for_timeout(3000)  # aguarda hidratação do React

                    seletor_encontrado = True
                    if selector_espera:
                        try:
                            # state="attached": basta existir no DOM. O
                            # padrão ("visible") nunca seria satisfeito por
                            # um <script>, que não é visualmente renderizado
                            # mesmo estando presente — testado localmente.
                            pagina.wait_for_selector(
                                selector_espera, state="attached", timeout=15000
                            )
                        except Exception:
                            seletor_encontrado = False

                    html = pagina.content()
                finally:
                    navegador.close()

            marcador_bloqueio = self._diagnosticar_bloqueio(html)
            if marcador_bloqueio:
                logger.error(
                    "bloqueio_cloudflare_detectado portal=%s marcador=%r "
                    "html_tamanho=%d html_inicio=%r",
                    self.portal, marcador_bloqueio, len(html), html[:500],
                )
                self._salvar_html_diagnostico(html)
            elif selector_espera and not seletor_encontrado:
                logger.warning(
                    "seletor_nao_encontrado_apos_espera portal=%s seletor=%r "
                    "html_tamanho=%d html_inicio=%r",
                    self.portal, selector_espera, len(html), html[:500],
                )
                self._salvar_html_diagnostico(html)

            return html
        except Exception as exc:
            # Captura ampla proposital: falhas de infraestrutura do navegador
            # (timeout de navegação, browser não instalado, crash) não devem
            # interromper a execução dos demais scrapers.
            logger.error("falha_playwright portal=%s erro=%s", self.portal, exc)
            return None

    def _card_generico_para_item(
        self,
        href: str,
        texto_card: str,
        url_absoluta: str,
        id_origem: str,
        *,
        exigir_data_relativa: bool = True,
    ) -> dict | None:
        """Constrói um item a partir do texto visível de um card de listagem
        (usado pelos fallbacks HTML da OLX e do ZAP/VivaReal). Extrai preço,
        área, quartos e bairro via regex/casamento de texto.

        Se `exigir_data_relativa` for True (padrão, usado pela OLX), a data
        de publicação vem de badges de tempo relativo no card ("Hoje",
        "Ontem", "há Xh") e o item é descartado sem esse indício — não se
        arrisca a comprovar a janela temporal por omissão. Se for False
        (usado pelo ZAP/VivaReal, cujos cards de busca não expõem esse
        badge), a "novidade" passa a depender inteiramente da combinação
        busca ordenada por mais recentes + deduplicação por id_origem no
        database.py: usa-se o momento da coleta como data_criacao_anuncio,
        o que sempre passa no filtro de janela — o controle real de
        duplicidade fica a cargo de database.ja_processado."""
        if exigir_data_relativa:
            data_publicacao = self._parse_data_relativa(texto_card)
            if data_publicacao is None:
                logger.info(
                    "descartado motivo=sem_indicio_de_data_no_card portal=%s url=%s",
                    self.portal, href,
                )
                return None
        else:
            data_publicacao = datetime.now(timezone.utc)

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

    Os parâmetros `sf`/`pe`/`ros` (ordenação, preço máximo, quartos mínimo)
    usados antes eram supostos, nunca confirmados, e o diagnóstico
    estrutural (rodado em produção via GitHub Actions) mostrou evidência de
    que causavam 0 resultados reais: dos 207 links da página buscada com
    esses parâmetros, nenhum se agrupava em um padrão de anúncio — só
    links de navegação/categoria, sugerindo busca vazia (mesmo padrão do
    problema que já ocorreu com os parâmetros do glue-api do ZAP/VivaReal).
    Por isso a busca aqui usa só a URL base, sem parâmetros de filtro/
    ordenação — preço, quartos e demais critérios continuam sendo
    aplicados do nosso lado em _aplicar_criterios_obrigatorios, como já é
    feito para elevador e bairro.
    """

    portal = "olx"
    BASE_URL = "https://www.olx.com.br/imoveis/venda/estado-es/grande-vitoria/vitoria"
    PARAMS: dict = {}
    # Espera (fallback Playwright) por qualquer um destes indícios de
    # conteúdo carregado. "a[href*=/imoveis/]" é confirmado via diagnóstico
    # real (links de anúncio contêm esse trecho), mas CSS não expressa a
    # condição extra de terminar em ID numérico (ver _REGEX_LINK_ANUNCIO) —
    # esse seletor pode bater em links de navegação/categoria também.
    SELECTOR_ESPERA = 'script#__NEXT_DATA__, div[data-ds-component="DS-AdCard"], a[href*="/imoveis/"]'

    def extrair_recentes(self) -> list[dict]:
        html = self._buscar_html_com_fallback_playwright(
            self.BASE_URL, self.PARAMS, self.SELECTOR_ESPERA
        )
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

    # Confirmado via diagnóstico estrutural real (execução em produção): os
    # links de anúncio da OLX estão em domínio regional (ex.: es.olx.com.br,
    # não www.olx.com.br), caminho "/imoveis/<slug-descritivo>-<id>", e
    # terminam em sequência longa de dígitos — nunca contêm "/vi/", que era
    # uma suposição incorreta (nunca confirmada) usada nas versões anteriores.
    _REGEX_LINK_ANUNCIO = re.compile(r"-\d{6,}/?(?:\?.*)?$")

    def _extrair_cards_html(self, html: str) -> list[dict]:
        """Fallback de última instância quando o JSON embutido não é
        encontrado (ex.: estrutura da página mudou, ou a página renderizada
        é uma tela de bloqueio/captcha). Identifica cards por links contendo
        "/imoveis/" cujo caminho termina em ID numérico longo — padrão
        confirmado contra o HTML real da OLX."""
        soup = BeautifulSoup(html, "html.parser")
        candidatos = []
        vistos = set()

        for link in soup.select('a[href*="/imoveis/"]'):
            href = link.get("href", "")
            if not href or not self._REGEX_LINK_ANUNCIO.search(href) or href in vistos:
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

            # exigir_data_relativa=False: confirmado em produção que os
            # cards de busca da OLX (assim como os do ZAP/VivaReal) não
            # expõem badge de "publicado há X" — todos os anúncios reais
            # eram descartados por sem_indicio_de_data_no_card antes desta
            # mudança. A "novidade" passa a depender de database.ja_processado.
            item = self._card_generico_para_item(
                href, texto_card, url, id_origem, exigir_data_relativa=False
            )
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
    busca (`quartos`/`preco-ate`/`ordem` na própria URL), com o mesmo
    fallback via Playwright usado pela OLX em caso de bloqueio 403.

    Validado em execução real (GitHub Actions): a requisição direta (sem
    Playwright) já retorna ~20 cards reais da página, com preço/área/bairro
    corretos. Porém os cards de busca do VivaReal não expõem nenhum badge
    de "publicado há X" (diferente da OLX) — por isso este scraper NÃO
    exige comprovação de data relativa no card (veja
    `exigir_data_relativa=False` em `_extrair_cards_html`). A "novidade" do
    anúncio passa a depender de dois fatores: (1) o parâmetro `ordem`
    pedindo ordenação pelos mais recentes primeiro, e (2) a deduplicação
    por id_origem em database.py — um anúncio só gera alerta na primeira
    vez que aparecer na busca. Isso significa que um anúncio antigo que
    ainda apareça na primeira página de resultados pode ser processado; o
    parâmetro `ordem` é a única mitigação para isso e seu nome exato está
    marcado como CONFIRMAR (assim como o seletor de card "/imovel/").
    """

    portal = "zap_vivareal"
    BASE_URL = "https://www.vivareal.com.br/venda/espirito-santo/vitoria/apartamento_residencial/"
    PARAMS = {
        "quartos": "3",
        "preco-ate": "750000",
        "ordem": "data-decrescente",  # CONFIRMAR: nome exato do parâmetro de ordenação por mais recentes
    }
    # A URL fornecida também incluía um fragmento "#onde=...": fragmentos
    # (#) são interpretados só no navegador (client-side, ex.: para
    # pré-preencher o mapa) e nunca são enviados ao servidor em uma
    # requisição HTTP — por isso foram omitidos aqui, já que não têm efeito
    # sobre a página retornada por requests.get nem pelo Playwright em modo
    # de navegação simples.

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

            item = self._card_generico_para_item(
                href, texto_card, url, id_origem, exigir_data_relativa=False
            )
            if item is None:
                continue

            item_finalizado = self._finalizar_item(item)
            if item_finalizado is not None:
                candidatos.append(item_finalizado)

        return candidatos
