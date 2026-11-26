"""
Tutte le query. L'isolamento tra utenti passa da qui e solo da qui:
ogni lettura o modifica di un job o progetto filtra per proprietario (o ruolo admin, dove ammesso).
Un oggetto di un altro utente non «esiste»: le funzioni restituiscono None e l'API risponde 404.
"""

import datetime as dt
from uuid import UUID

from psycopg_pool import ConnectionPool

from .db import CANALE_CODA

STATI_ATTIVI = ("in_coda", "in_elaborazione")
PRIORITA_ADMIN = 10
PRIORITA_UTENTE = 0

# metadati di un job: mai il testo né il nome del file originale (che non si salva proprio)
_CAMPI_JOB = ("j.id, j.proprietario_id, j.progetto_id, p.nome AS progetto, j.stato, j.priorita, j.lingua, "
              "j.modello, j.durata_s, j.avanzamento_s, j.errore, j.creato_il, j.iniziato_il, j.finito_il, "
              "j.testo_scade_il")


def _uuid(valore) -> UUID | None:
    try:
        return valore if isinstance(valore, UUID) else UUID(str(valore))
    except ValueError:
        return None


def _admin(utente: dict) -> bool:
    return utente["ruolo"] == "admin"


# --- utenti -----------------------------------------------------------------

def crea_utente(pool: ConnectionPool, email: str, nome: str, hash_password: str, ruolo: str) -> dict:
    with pool.connection() as c:
        return c.execute("INSERT INTO utenti (email, nome, hash_password, ruolo) VALUES (lower(%s), %s, %s, %s) "
                         "RETURNING *", (email.strip(), nome, hash_password, ruolo)).fetchone()


def utente_per_email(pool: ConnectionPool, email: str) -> dict | None:
    with pool.connection() as c:
        return c.execute("SELECT * FROM utenti WHERE email = lower(%s)", (email.strip(),)).fetchone()


def aggiorna_hash(pool: ConnectionPool, utente_id: int, hash_password: str):
    with pool.connection() as c:
        c.execute("UPDATE utenti SET hash_password = %s WHERE id = %s", (hash_password, utente_id))


# --- sessioni ---------------------------------------------------------------

def crea_sessione(pool: ConnectionPool, id_hash: str, utente_id: int, csrf: str, ore: int):
    with pool.connection() as c:
        c.execute("DELETE FROM sessioni WHERE scade_il < now()")
        c.execute("INSERT INTO sessioni (id, utente_id, csrf, scade_il) "
                  "VALUES (%s, %s, %s, now() + make_interval(hours => %s))", (id_hash, utente_id, csrf, ore))


def utente_da_sessione(pool: ConnectionPool, id_hash: str) -> dict | None:
    """Utente attivo della sessione non scaduta, con il token CSRF della sessione."""
    with pool.connection() as c:
        return c.execute("SELECT u.id, u.email, u.nome, u.ruolo, u.max_mb, u.max_minuti, u.max_job_giorno, "
                         "s.csrf FROM sessioni s JOIN utenti u ON u.id = s.utente_id "
                         "WHERE s.id = %s AND s.scade_il > now() AND u.attivo", (id_hash,)).fetchone()


def elimina_sessione(pool: ConnectionPool, id_hash: str):
    with pool.connection() as c:
        c.execute("DELETE FROM sessioni WHERE id = %s", (id_hash,))


# --- progetti ---------------------------------------------------------------

def progetti_di(pool: ConnectionPool, utente: dict) -> list[dict]:
    with pool.connection() as c:
        return c.execute("SELECT id, nome, glossario FROM progetti WHERE proprietario_id = %s ORDER BY nome",
                         (utente["id"],)).fetchall()


def crea_progetto(pool: ConnectionPool, utente: dict, nome: str, glossario: str = "") -> dict:
    with pool.connection() as c:
        return c.execute("INSERT INTO progetti (proprietario_id, nome, glossario) VALUES (%s, %s, %s) "
                         "ON CONFLICT (proprietario_id, nome) DO UPDATE SET nome = EXCLUDED.nome "
                         "RETURNING id, nome, glossario", (utente["id"], nome, glossario)).fetchone()


def aggiorna_progetto(pool: ConnectionPool, utente: dict, progetto_id: int, nome: str, glossario: str) -> dict | None:
    with pool.connection() as c:
        return c.execute("UPDATE progetti SET nome = %s, glossario = %s WHERE id = %s AND proprietario_id = %s "
                         "RETURNING id, nome, glossario", (nome, glossario, progetto_id, utente["id"])).fetchone()


def elimina_progetto(pool: ConnectionPool, utente: dict, progetto_id: int) -> bool:
    with pool.connection() as c:
        return c.execute("DELETE FROM progetti WHERE id = %s AND proprietario_id = %s",
                         (progetto_id, utente["id"])).rowcount == 1


# --- job: lato utente -------------------------------------------------------

def job_creati_oggi(pool: ConnectionPool, utente: dict) -> int:
    with pool.connection() as c:
        return c.execute("SELECT count(*) AS n FROM job WHERE proprietario_id = %s "
                         "AND creato_il > now() - interval '1 day'", (utente["id"],)).fetchone()["n"]


def crea_job(pool: ConnectionPool, utente: dict, job_id: UUID, progetto_id: int | None, lingua: str, modello: str,
             durata_s: float) -> dict | None:
    """
    None se il progetto non è dell'utente. I job dell'admin hanno priorità più alta.
    L'ID lo sceglie il chiamante: il file audio deve essere già al suo posto quando il worker viene svegliato.
    """
    priorita = PRIORITA_ADMIN if _admin(utente) else PRIORITA_UTENTE
    with pool.connection() as c, c.transaction():
        if progetto_id is not None and not c.execute(
                "SELECT 1 FROM progetti WHERE id = %s AND proprietario_id = %s",
                (progetto_id, utente["id"])).fetchone():
            return None
        riga = c.execute("INSERT INTO job (id, proprietario_id, progetto_id, priorita, lingua, modello, durata_s) "
                         "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                         (job_id, utente["id"], progetto_id, priorita, lingua, modello, durata_s)).fetchone()
        c.execute("SELECT pg_notify(%s, '')", (CANALE_CODA,))
    return job_visibile(pool, utente, riga["id"])


def elenco_job(pool: ConnectionPool, utente: dict) -> list[dict]:
    """I job dell'utente, con la posizione in coda per quelli in attesa."""
    with pool.connection() as c:
        return c.execute(
            f"SELECT {_CAMPI_JOB}, "
            "CASE WHEN j.stato = 'in_coda' THEN ("
            "  SELECT count(*) FROM job a WHERE a.stato IN ('in_coda', 'in_elaborazione') AND a.id <> j.id "
            "  AND (a.stato = 'in_elaborazione' OR a.priorita > j.priorita "
            "       OR (a.priorita = j.priorita AND a.creato_il < j.creato_il))) END AS davanti "
            "FROM job j LEFT JOIN progetti p ON p.id = j.progetto_id "
            "WHERE j.proprietario_id = %s ORDER BY j.creato_il DESC LIMIT 200", (utente["id"],)).fetchall()


def job_visibile(pool: ConnectionPool, utente: dict, job_id) -> dict | None:
    """Metadati di un job: del proprietario, oppure di chiunque per l'admin (stato e assistenza)."""
    uid = _uuid(job_id)
    if uid is None:
        return None
    with pool.connection() as c:
        return c.execute(f"SELECT {_CAMPI_JOB} FROM job j LEFT JOIN progetti p ON p.id = j.progetto_id "
                         "WHERE j.id = %s AND (j.proprietario_id = %s OR %s)",
                         (uid, utente["id"], _admin(utente))).fetchone()


def job_con_testo(pool: ConnectionPool, utente: dict, job_id) -> dict | None:
    """Il contenuto lo può scaricare solo il proprietario, anche se è admin di qualcun altro."""
    uid = _uuid(job_id)
    if uid is None:
        return None
    with pool.connection() as c:
        return c.execute("SELECT id FROM job WHERE id = %s AND proprietario_id = %s AND stato = 'completato'",
                         (uid, utente["id"])).fetchone()


def annulla_job(pool: ConnectionPool, utente: dict, job_id) -> dict | None:
    """Annulla un job in coda o in elaborazione: il proprietario i suoi, l'admin tutti."""
    uid = _uuid(job_id)
    if uid is None:
        return None
    with pool.connection() as c:
        return c.execute("UPDATE job SET stato = 'annullato', finito_il = now() "
                         "WHERE id = %s AND stato IN ('in_coda', 'in_elaborazione') "
                         "AND (proprietario_id = %s OR %s) RETURNING id",
                         (uid, utente["id"], _admin(utente))).fetchone()


def elimina_job(pool: ConnectionPool, utente: dict, job_id) -> dict | None:
    """Elimina un job concluso del proprietario (riga e, a carico del chiamante, i file)."""
    uid = _uuid(job_id)
    if uid is None:
        return None
    with pool.connection() as c:
        return c.execute("DELETE FROM job WHERE id = %s AND proprietario_id = %s "
                         "AND stato NOT IN ('in_coda', 'in_elaborazione') RETURNING id",
                         (uid, utente["id"])).fetchone()


# --- job: lato worker -------------------------------------------------------

def rimetti_in_coda_interrotti(pool: ConnectionPool) -> int:
    """All'avvio del worker: i job rimasti a metà (riavvio, crash) tornano in coda e riprenderanno."""
    with pool.connection() as c:
        return c.execute("UPDATE job SET stato = 'in_coda' WHERE stato = 'in_elaborazione'").rowcount


def prendi_prossimo(pool: ConnectionPool) -> dict | None:
    """Il prossimo job: prima per priorità, poi per ordine di arrivo. SKIP LOCKED: mai due worker sullo stesso."""
    with pool.connection() as c:
        return c.execute(
            "UPDATE job j SET stato = 'in_elaborazione', iniziato_il = coalesce(j.iniziato_il, now()) "
            "FROM (SELECT id FROM job WHERE stato = 'in_coda' ORDER BY priorita DESC, creato_il "
            "      LIMIT 1 FOR UPDATE SKIP LOCKED) prossimo "
            "WHERE j.id = prossimo.id "
            "RETURNING j.id, j.lingua, j.modello, j.durata_s, j.avanzamento_s, "
            "(SELECT glossario FROM progetti WHERE id = j.progetto_id) AS glossario").fetchone()


def aggiorna_avanzamento(pool: ConnectionPool, job_id: UUID, secondi: float) -> bool:
    """Salva il punto raggiunto; False se nel frattempo il job è stato annullato o eliminato."""
    with pool.connection() as c:
        return c.execute("UPDATE job SET avanzamento_s = %s WHERE id = %s AND stato = 'in_elaborazione'",
                         (secondi, job_id)).rowcount == 1


def imposta_durata(pool: ConnectionPool, job_id: UUID, durata_s: float):
    with pool.connection() as c:
        c.execute("UPDATE job SET durata_s = %s WHERE id = %s", (durata_s, job_id))


def completa(pool: ConnectionPool, job_id: UUID, giorni_conservazione: int) -> bool:
    scade = dt.datetime.now(dt.UTC) + dt.timedelta(days=giorni_conservazione)
    with pool.connection() as c:
        return c.execute("UPDATE job SET stato = 'completato', finito_il = now(), avanzamento_s = coalesce(durata_s, "
                         "avanzamento_s), testo_scade_il = %s WHERE id = %s AND stato = 'in_elaborazione'",
                         (scade, job_id)).rowcount == 1


def fallisci(pool: ConnectionPool, job_id: UUID, errore: str):
    with pool.connection() as c:
        c.execute("UPDATE job SET stato = 'errore', finito_il = now(), errore = %s "
                  "WHERE id = %s AND stato = 'in_elaborazione'", (errore[:300], job_id))


def stato_job(pool: ConnectionPool, job_id: UUID) -> str | None:
    with pool.connection() as c:
        riga = c.execute("SELECT stato FROM job WHERE id = %s", (job_id,)).fetchone()
    return riga["stato"] if riga else None
