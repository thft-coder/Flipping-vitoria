"""Motor de análise de oportunidades de flipping imobiliário."""

from __future__ import annotations

import logging

from config import (
    BENCHMARKS_M2,
    PRECO_MAXIMO,
    QUARTOS_MINIMO,
    REGEX_OPORTUNIDADE,
    THRESHOLD_DESCONTO_MINIMO,
    THRESHOLD_DESCONTO_OPORTUNIDADE,
)

logger = logging.getLogger(__name__)


def avaliar_oportunidade(dados_imovel: dict) -> dict | None:
    """Aplica os filtros eliminatórios obrigatórios e a regra de aprovação
    de flipping sobre um imóvel já extraído (dict com, no mínimo, preco,
    quartos, elevador, area_m2 e bairro).

    Filtros eliminatórios: preço acima de PRECO_MAXIMO, quartos abaixo de
    QUARTOS_MINIMO, elevador não confirmado, área privativa inválida (<= 0)
    ou bairro fora de config.BENCHMARKS_M2.

    Regra de aprovação: desconto do preço/m² em relação à mediana do
    bairro >= THRESHOLD_DESCONTO_MINIMO (25%), OU desconto >=
    THRESHOLD_DESCONTO_OPORTUNIDADE (15%) com termos de oportunidade
    (config.REGEX_OPORTUNIDADE) no título/descrição.

    Retorna o dict original enriquecido com as métricas calculadas, ou
    None se o imóvel for reprovado em qualquer etapa."""
    id_origem = dados_imovel.get("id_origem")
    preco = dados_imovel.get("preco")
    quartos = dados_imovel.get("quartos")
    elevador = dados_imovel.get("elevador")
    area_m2 = dados_imovel.get("area_m2")
    bairro = dados_imovel.get("bairro")

    if preco is None or preco > PRECO_MAXIMO:
        logger.info(
            "reprovado motivo=preco_acima_do_maximo id_origem=%s preco=%s limite=%s",
            id_origem, preco, PRECO_MAXIMO,
        )
        return None

    if quartos is None or quartos < QUARTOS_MINIMO:
        logger.info(
            "reprovado motivo=quartos_insuficiente id_origem=%s quartos=%s minimo=%s",
            id_origem, quartos, QUARTOS_MINIMO,
        )
        return None

    if elevador is not True:
        logger.info(
            "reprovado motivo=elevador_nao_confirmado id_origem=%s elevador=%s",
            id_origem, elevador,
        )
        return None

    if area_m2 is None or area_m2 <= 0:
        logger.info(
            "reprovado motivo=area_invalida id_origem=%s area_m2=%s",
            id_origem, area_m2,
        )
        return None

    mediana_referencia_m2 = BENCHMARKS_M2.get(bairro)
    if mediana_referencia_m2 is None:
        logger.info(
            "reprovado motivo=bairro_nao_mapeado id_origem=%s bairro=%s",
            id_origem, bairro,
        )
        return None

    preco_m2 = preco / area_m2
    desconto_percentual = (1 - preco_m2 / mediana_referencia_m2) * 100

    texto_completo = f"{dados_imovel.get('titulo', '')} {dados_imovel.get('descricao', '')}"
    termos_oportunidade_encontrados = REGEX_OPORTUNIDADE.findall(texto_completo)
    contem_termo_oportunidade = bool(termos_oportunidade_encontrados)

    gatilho_aprovacao = None
    if desconto_percentual >= THRESHOLD_DESCONTO_MINIMO * 100:
        gatilho_aprovacao = f"desconto >= {THRESHOLD_DESCONTO_MINIMO * 100:.0f}%"
    elif (
        desconto_percentual >= THRESHOLD_DESCONTO_OPORTUNIDADE * 100
        and contem_termo_oportunidade
    ):
        gatilho_aprovacao = (
            f"desconto >= {THRESHOLD_DESCONTO_OPORTUNIDADE * 100:.0f}% "
            f"+ termo de oportunidade ({', '.join(termos_oportunidade_encontrados)})"
        )

    if gatilho_aprovacao is None:
        logger.info(
            "reprovado motivo=desconto_insuficiente id_origem=%s desconto=%.2f%% "
            "oportunidade=%s",
            id_origem, desconto_percentual, contem_termo_oportunidade,
        )
        return None

    resultado = dict(dados_imovel)
    resultado.update(
        {
            "preco_m2": round(preco_m2, 2),
            "mediana_referencia_m2": mediana_referencia_m2,
            "desconto_percentual": round(desconto_percentual, 2),
            "termos_oportunidade_encontrados": termos_oportunidade_encontrados,
            "gatilho_aprovacao": gatilho_aprovacao,
        }
    )

    logger.info(
        "aprovado id_origem=%s bairro=%s desconto=%.2f%% gatilho=%s",
        id_origem, bairro, desconto_percentual, gatilho_aprovacao,
    )
    return resultado
