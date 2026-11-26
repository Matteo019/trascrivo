"""Connessione a PostgreSQL (pool psycopg) e migrazioni in file .sql numerati."""

import logging
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

CARTELLA_MIGRAZIONI = Path(__file__).parent / "migrazioni"
CANALE_CODA = "coda_job"
_LOCK_MIGRAZIONI = 727201   # advisory lock: web e worker possono partire insieme


def crea_pool(url: str, max_connessioni: int = 5) -> ConnectionPool:
    pool = ConnectionPool(url, min_size=1, max_size=max_connessioni, open=True,
                          kwargs={"row_factory": dict_row})
    pool.wait(timeout=30)
    return pool


def migra(url: str):
    """Applica in ordine le migrazioni non ancora eseguite, ognuna in una transazione."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_MIGRAZIONI,))
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS migrazioni ("
                         "nome TEXT PRIMARY KEY, applicata_il TIMESTAMPTZ NOT NULL DEFAULT now())")
            fatte = {r[0] for r in conn.execute("SELECT nome FROM migrazioni")}
            for file in sorted(CARTELLA_MIGRAZIONI.glob("*.sql")):
                if file.name in fatte:
                    continue
                with conn.transaction():
                    conn.execute(file.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO migrazioni (nome) VALUES (%s)", (file.name,))
                log.info("migrazione applicata: %s", file.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_MIGRAZIONI,))
