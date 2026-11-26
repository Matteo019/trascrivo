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

# metadati di un job: mai il testo (colonna job.testo, letta solo da job_con_testo per il proprietario)
# né il nome del file originale (che non si salva proprio)
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
        return c.execute("SELECT id, testo FROM job WHERE id = %s AND proprietario_id = %s AND stato = 'completato' "
                         "AND testo IS NOT NULL",
                         (uid, utente["id"])).fetchone()


def annulla_job(pool: ConnectionPool, utente: dict, job_id) -> dict | None:
    """Annulla un job in coda o in elaborazione: il proprietario i suoi, l'admin tutti."""
    uid = _uuid(job_id)
    if uid is None:
        return None
    with pool.connection() as c, c.transaction():
        riga = c.execute("UPDATE job SET stato = 'annullato', finito_il = now() "
                         "WHERE id = %s AND stato IN ('in_coda', 'in_elaborazione') "
                         "AND (proprietario_id = %s OR %s) RETURNING id",
                         (uid, utente["id"], _admin(utente))).fetchone()
        if riga:
            c.execute("DELETE FROM job_segmenti WHERE job_id = %s", (uid,))
        return riga


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


def segmenti_salvati(pool: ConnectionPool, job_id: UUID) -> list[tuple[float, float, str]]:
    """Segmenti confermati di un job ripreso dopo un'interruzione, in ordine."""
    with pool.connection() as c:
        righe = c.execute("SELECT inizio, fine, testo FROM job_segmenti WHERE job_id = %s ORDER BY inizio",
                          (job_id,)).fetchall()
    return [(r["inizio"], r["fine"], r["testo"]) for r in righe]


def salva_checkpoint(pool: ConnectionPool, job_id: UUID, nuovi: list[tuple[float, float, str]],
                     secondi: float) -> bool:
    """Segmenti nuovi e punto raggiunto, insieme. False se il job è stato annullato o eliminato."""
    with pool.connection() as c, c.transaction():
        if c.execute("UPDATE job SET avanzamento_s = %s WHERE id = %s AND stato = 'in_elaborazione'",
                     (secondi, job_id)).rowcount != 1:
            return False
        with c.cursor() as cur:
            cur.executemany("INSERT INTO job_segmenti (job_id, inizio, fine, testo) VALUES (%s, %s, %s, %s) "
                            "ON CONFLICT DO NOTHING", [(job_id, a, b, t) for a, b, t in nuovi])
    return True


def imposta_durata(pool: ConnectionPool, job_id: UUID, durata_s: float):
    with pool.connection() as c:
        c.execute("UPDATE job SET durata_s = %s WHERE id = %s", (durata_s, job_id))


def completa(pool: ConnectionPool, job_id: UUID, testo: str, giorni_conservazione: int) -> bool:
    """Salva il testo finale e toglie i segmenti di lavoro. False se il job è stato annullato nel frattempo."""
    scade = dt.datetime.now(dt.UTC) + dt.timedelta(days=giorni_conservazione)
    with pool.connection() as c, c.transaction():
        c.execute("DELETE FROM job_segmenti WHERE job_id = %s", (job_id,))
        return c.execute("UPDATE job SET stato = 'completato', finito_il = now(), testo = %s, "
                         "avanzamento_s = coalesce(durata_s, avanzamento_s), testo_scade_il = %s "
                         "WHERE id = %s AND stato = 'in_elaborazione'", (testo, scade, job_id)).rowcount == 1


def scarta_segmenti(pool: ConnectionPool, job_id: UUID):
    """Job concluso senza testo (errore, annullato): i segmenti parziali non servono più."""
    with pool.connection() as c:
        c.execute("DELETE FROM job_segmenti WHERE job_id = %s", (job_id,))


def fallisci(pool: ConnectionPool, job_id: UUID, errore: str):
    with pool.connection() as c:
        c.execute("UPDATE job SET stato = 'errore', finito_il = now(), errore = %s "
                  "WHERE id = %s AND stato = 'in_elaborazione'", (errore[:300], job_id))


def stato_job(pool: ConnectionPool, job_id: UUID) -> str | None:
    with pool.connection() as c:
        riga = c.execute("SELECT stato FROM job WHERE id = %s", (job_id,)).fetchone()
    return riga["stato"] if riga else None


# --- rate limiting dell'accesso ---------------------------------------------

def tentativi_recenti(pool: ConnectionPool, chiave: str, minuti: int) -> int:
    with pool.connection() as c:
        return c.execute("SELECT count(*) AS n FROM tentativi_accesso WHERE chiave = %s "
                         "AND istante > now() - make_interval(mins => %s)", (chiave, minuti)).fetchone()["n"]


def registra_tentativo(pool: ConnectionPool, *chiavi: str):
    with pool.connection() as c, c.transaction():
        c.execute("DELETE FROM tentativi_accesso WHERE istante < now() - interval '1 day'")
        with c.cursor() as cur:
            cur.executemany("INSERT INTO tentativi_accesso (chiave) VALUES (%s)", [(k,) for k in chiavi])


def azzera_tentativi(pool: ConnectionPool, chiave: str):
    with pool.connection() as c:
        c.execute("DELETE FROM tentativi_accesso WHERE chiave = %s", (chiave,))


# --- inviti -----------------------------------------------------------------

def crea_invito(pool: ConnectionPool, admin: dict, id_hash: str, email: str, ruolo: str, giorni: int) -> dict:
    with pool.connection() as c:
        return c.execute("INSERT INTO inviti (id, email, ruolo, creato_da, scade_il) "
                         "VALUES (%s, lower(%s), %s, %s, now() + make_interval(days => %s)) "
                         "RETURNING email, ruolo, scade_il", (id_hash, email.strip(), ruolo, admin["id"], giorni)
                         ).fetchone()


def invito_valido(pool: ConnectionPool, id_hash: str) -> dict | None:
    with pool.connection() as c:
        return c.execute("SELECT email, ruolo, scade_il FROM inviti WHERE id = %s AND usato_il IS NULL "
                         "AND scade_il > now()", (id_hash,)).fetchone()


def usa_invito(pool: ConnectionPool, id_hash: str, nome: str, hash_password: str) -> dict | None:
    """Crea l'utente e consuma l'invito, tutto o niente. None se l'invito non vale o l'email esiste già."""
    with pool.connection() as c, c.transaction():
        invito = c.execute("SELECT email, ruolo FROM inviti WHERE id = %s AND usato_il IS NULL "
                           "AND scade_il > now() FOR UPDATE", (id_hash,)).fetchone()
        if invito is None or c.execute("SELECT 1 FROM utenti WHERE email = %s", (invito["email"],)).fetchone():
            return None
        c.execute("UPDATE inviti SET usato_il = now() WHERE id = %s", (id_hash,))
        return c.execute("INSERT INTO utenti (email, nome, hash_password, ruolo) VALUES (%s, %s, %s, %s) "
                         "RETURNING id, email", (invito["email"], nome, hash_password, invito["ruolo"])).fetchone()


def inviti_in_attesa(pool: ConnectionPool) -> list[dict]:
    with pool.connection() as c:
        return c.execute("SELECT left(id, 12) AS id, email, ruolo, creato_il, scade_il FROM inviti "
                         "WHERE usato_il IS NULL AND scade_il > now() ORDER BY creato_il DESC").fetchall()


def revoca_invito(pool: ConnectionPool, prefisso: str) -> bool:
    with pool.connection() as c:
        return c.execute("DELETE FROM inviti WHERE left(id, 12) = %s AND usato_il IS NULL",
                         (prefisso,)).rowcount == 1


# --- amministrazione: utenti e coda -----------------------------------------

def elenco_utenti(pool: ConnectionPool) -> list[dict]:
    with pool.connection() as c:
        return c.execute(
            "SELECT u.id, u.email, u.nome, u.ruolo, u.attivo, u.max_mb, u.max_minuti, u.max_job_giorno, u.creato_il, "
            "(SELECT count(*) FROM job j WHERE j.proprietario_id = u.id AND j.creato_il > now() - interval '1 day')"
            " AS job_oggi FROM utenti u ORDER BY u.creato_il").fetchall()


def aggiorna_utente(pool: ConnectionPool, admin: dict, utente_id: int, attivo: bool,
                    max_mb: int | None, max_minuti: int | None, max_job_giorno: int | None) -> dict | None:
    """Disattiva o riattiva e imposta i limiti. Un admin non può disattivare sé stesso."""
    if utente_id == admin["id"] and not attivo:
        return None
    with pool.connection() as c, c.transaction():
        riga = c.execute("UPDATE utenti SET attivo = %s, max_mb = %s, max_minuti = %s, max_job_giorno = %s "
                         "WHERE id = %s RETURNING id", (attivo, max_mb, max_minuti, max_job_giorno, utente_id)
                         ).fetchone()
        if riga and not attivo:      # disattivato: fuori subito da tutte le sessioni
            c.execute("DELETE FROM sessioni WHERE utente_id = %s", (utente_id,))
        return riga


def coda_admin(pool: ConnectionPool) -> list[dict]:
    """Job attivi e delle ultime 24 ore di tutti gli utenti: solo metadati, mai il testo."""
    with pool.connection() as c:
        return c.execute(
            f"SELECT {_CAMPI_JOB}, u.email AS proprietario FROM job j JOIN utenti u ON u.id = j.proprietario_id "
            "LEFT JOIN progetti p ON p.id = j.progetto_id "
            "WHERE j.stato IN ('in_coda', 'in_elaborazione') OR j.creato_il > now() - interval '1 day' "
            "ORDER BY (j.stato = 'in_elaborazione') DESC, (j.stato = 'in_coda') DESC, "
            "j.priorita DESC, j.creato_il LIMIT 300").fetchall()


def imposta_priorita(pool: ConnectionPool, job_id, priorita: int) -> dict | None:
    uid = _uuid(job_id)
    if uid is None:
        return None
    with pool.connection() as c:
        return c.execute("UPDATE job SET priorita = %s WHERE id = %s AND stato = 'in_coda' RETURNING id",
                         (priorita, uid)).fetchone()


# --- conservazione ----------------------------------------------------------

def pulizia(pool: ConnectionPool, giorni_conservazione: int) -> dict:
    """
    Cancella quello che non serve più: job completati col testo scaduto (riga e testo), job in errore o
    annullati più vecchi della conservazione, sessioni e inviti scaduti, tentativi di accesso vecchi.
    """
    with pool.connection() as c, c.transaction():
        conti = {
            "job_scaduti": c.execute("DELETE FROM job WHERE stato = 'completato' AND testo_scade_il < now()"
                                     ).rowcount,
            "job_chiusi": c.execute("DELETE FROM job WHERE stato IN ('errore', 'annullato') "
                                    "AND finito_il < now() - make_interval(days => %s)",
                                    (giorni_conservazione,)).rowcount,
            "sessioni": c.execute("DELETE FROM sessioni WHERE scade_il < now()").rowcount,
            "inviti": c.execute("DELETE FROM inviti WHERE scade_il < now() "
                                "OR usato_il < now() - interval '1 day'").rowcount,
            "tentativi": c.execute("DELETE FROM tentativi_accesso WHERE istante < now() - interval '1 day'"
                                   ).rowcount,
        }
    return conti


def job_attivi(pool: ConnectionPool) -> set[str]:
    """ID dei job ancora in coda o in elaborazione: i loro file audio non si toccano."""
    with pool.connection() as c:
        return {str(r["id"]) for r in c.execute(
            "SELECT id FROM job WHERE stato IN ('in_coda', 'in_elaborazione')").fetchall()}
