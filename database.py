"""Camada de persistência em SQLite para os imóveis processados."""

import sqlite3
from datetime import datetime

DB_PATH = "imoveis.db"


def conectar():
    return sqlite3.connect(DB_PATH)


def criar_tabela():
    with conectar() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS imoveis_processados (
                id_origem TEXT PRIMARY KEY,
                portal TEXT,
                titulo TEXT,
                preco REAL,
                area_m2 REAL,
                preco_m2 REAL,
                bairro TEXT,
                url TEXT,
                data_criacao_anuncio TEXT,
                data_coleta TEXT
            )
            """
        )
        conn.commit()


def ja_processado(id_origem):
    with conectar() as conn:
        cursor = conn.execute(
            "SELECT 1 FROM imoveis_processados WHERE id_origem = ?",
            (id_origem,),
        )
        return cursor.fetchone() is not None


def salvar_imovel(dados):
    with conectar() as conn:
        conn.execute(
            """
            INSERT INTO imoveis_processados (
                id_origem, portal, titulo, preco, area_m2,
                preco_m2, bairro, url, data_criacao_anuncio, data_coleta
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dados["id_origem"],
                dados["portal"],
                dados["titulo"],
                dados["preco"],
                dados["area_m2"],
                dados["preco_m2"],
                dados["bairro"],
                dados["url"],
                dados["data_criacao_anuncio"],
                dados.get("data_coleta", datetime.now().isoformat()),
            ),
        )
        conn.commit()


criar_tabela()
