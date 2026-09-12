"""Configurações centrais do projeto Flipping Vitória."""

import re

# Janela máxima de tempo (em horas) para considerar um anúncio como válido
JANELA_MAX_HORAS = 48

# Bairros-alvo em Vitória (ES) e suas medianas estimadas de preço por m² (R$),
# usadas como benchmark para o cálculo de desconto no motor de análise
BENCHMARKS_M2 = {
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

# Desconto mínimo (fração) aceito quando o texto contém termos de
# oportunidade (regra de aprovação mais permissiva)
THRESHOLD_DESCONTO_OPORTUNIDADE = 0.15

# Preço máximo (R$) aceito para um imóvel ser considerado
PRECO_MAXIMO = 750000.0

# Quantidade mínima de quartos exigida
QUARTOS_MINIMO = 3

# Exigir presença de elevador no edifício
EXIGIR_ELEVADOR = True

# Ausência explícita de elevador no título/descrição do anúncio
REGEX_SEM_ELEVADOR = re.compile(
    r'\b(sem|n[aã]o\s+possui|n[aã]o\s+tem|dispensa)\s+elevador\b', re.IGNORECASE
)

# Afirmação textual de presença de elevador no título/descrição do anúncio
REGEX_COM_ELEVADOR = re.compile(
    r'\b(com\s+elevador|possui\s+elevador|edif[íi]cio\s+com\s+elevador|'
    r'pr[éè]dio\s+com\s+elevador|2\s+elevadores|dois\s+elevadores|'
    r'elevador\s+social)\b',
    re.IGNORECASE,
)
