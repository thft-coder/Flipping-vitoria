"""Configurações centrais do projeto Flipping Vitória."""

import re

# Janela máxima de tempo (em horas) para considerar um anúncio como válido
JANELA_MAX_HORAS = 48

# Bairros-alvo em Vitória (ES) e suas medianas estimadas de preço por m² (R$)
BAIRROS_ALVO = {
    "Jardim da Penha": 7500,
    "Praia do Canto": 11000,
    "Mata da Praia": 10500,
    "Bento Ferreira": 7000,
    "Jardim Camburi": 8000,
}

# Termos mandatórios que indicam oportunidade no título/descrição do anúncio
TERMOS_OPORTUNIDADE = [
    "reforma",
    "original",
    "inventário",
    "partilha",
    "urgente",
    "motivo de mudança",
]

REGEX_OPORTUNIDADE = re.compile(
    "|".join(re.escape(termo) for termo in TERMOS_OPORTUNIDADE),
    re.IGNORECASE,
)

# Desconto mínimo (fração) exigido em relação à mediana de R$/m² do bairro
THRESHOLD_DESCONTO_MINIMO = 0.25

# Preço máximo (R$) aceito para um imóvel ser considerado
PRECO_MAXIMO = 750000.0

# Quantidade mínima de quartos exigida
QUARTOS_MINIMO = 3

# Exigir presença de elevador no edifício
EXIGIR_ELEVADOR = True

# Termos e comodidades usados para identificar elevador em descrições ou
# atributos técnicos do anúncio
TERMOS_ELEVADOR = [
    "elevador",
    "com elevador",
    "edifício com elevador",
]

REGEX_ELEVADOR = re.compile(
    "|".join(re.escape(termo) for termo in TERMOS_ELEVADOR),
    re.IGNORECASE,
)
