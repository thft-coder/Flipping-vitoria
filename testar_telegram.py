"""Diagnóstico isolado: envia um alerta de teste ao Telegram sem rodar
scraping nem análise, para verificar rapidamente TELEGRAM_BOT_TOKEN e
TELEGRAM_CHAT_ID sem esperar o pipeline completo (scraping + Playwright)."""

import sys

from notifier import enviar_alerta_telegram

IMOVEL_TESTE = {
    "id_origem": "teste-manual-telegram",
    "bairro": "Jardim da Penha",
    "preco": 500000.0,
    "quartos": 3,
    "elevador": True,
    "preco_m2": 5000.0,
    "mediana_referencia_m2": 7500,
    "desconto_percentual": 33.33,
    # O "_" em "testar_telegram.py" é propositalmente mantido aqui: serve de
    # teste de regressão para o escaping de Markdown em notifier.py (um "_"
    # sem escape quebra o parse_mode="Markdown" do Telegram com 400).
    "gatilho_aprovacao": "teste manual de notificação (testar_telegram.py)",
    "url": "https://exemplo.com/anuncio-de-teste",
}

if __name__ == "__main__":
    sucesso = enviar_alerta_telegram(IMOVEL_TESTE)
    print(f"Alerta de teste enviado com sucesso: {sucesso}")
    sys.exit(0 if sucesso else 1)
