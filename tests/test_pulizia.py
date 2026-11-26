"""Fase 3: conservazione dei dati e pulizia automatica."""

import os
import time
import uuid

from trascrivo.worker import file_job


def _conta(pool, sql, *parametri):
    with pool.connection() as c:
        return c.execute(sql, parametri).fetchone()["n"]


def test_testo_scaduto_cancellato(utenti, worker, pool):
    vecchio = utenti["anna"].carica().json()["id"]
    recente = utenti["anna"].carica().json()["id"]
    worker.avvio()
    worker.esegui_uno()
    worker.esegui_uno()
    with pool.connection() as c:
        c.execute("UPDATE job SET testo_scade_il = now() - interval '1 minute' WHERE id = %s", (vecchio,))
    worker.pulisci()
    assert _conta(pool, "SELECT count(*) AS n FROM job WHERE id = %s", vecchio) == 0      # riga e testo spariti
    assert utenti["anna"].get(f"/api/job/{vecchio}/testo").status_code == 404
    assert utenti["anna"].get(f"/api/job/{recente}/testo").status_code == 200           # non ancora scaduto


def test_job_in_errore_o_annullati_vecchi_cancellati(utenti, pool, worker, cfg):
    job = utenti["anna"].carica().json()["id"]
    utenti["anna"].post(f"/api/job/{job}/annulla")
    worker.pulisci()
    assert _conta(pool, "SELECT count(*) AS n FROM job") == 1                         # appena annullato: resta
    with pool.connection() as c:
        c.execute("UPDATE job SET finito_il = now() - make_interval(days => %s)", (cfg.giorni_conservazione + 1,))
    worker.pulisci()
    assert _conta(pool, "SELECT count(*) AS n FROM job") == 0


def test_file_orfani_cancellati_quelli_attivi_no(utenti, worker, cfg):
    attivo = utenti["anna"].carica().json()["id"]
    file_attivo = file_job(cfg, attivo)["originale"]
    orfano_vecchio = cfg.cartella_lavoro / f"{uuid.uuid4()}.wav"
    orfano_recente = cfg.cartella_audio / f"{uuid.uuid4()}.caricamento"
    for f in (orfano_vecchio, orfano_recente):
        f.write_bytes(b"x")
    due_ore_fa = time.time() - 7200
    for f in (orfano_vecchio, file_attivo):
        os.utime(f, (due_ore_fa, due_ore_fa))
    worker.pulisci()
    assert not orfano_vecchio.exists()
    assert orfano_recente.exists()            # potrebbe essere un upload in corso
    assert file_attivo.exists()               # job ancora in coda


def test_sessioni_inviti_e_tentativi_scaduti(utenti, pool, worker):
    utenti["admin"].post("/api/admin/inviti", json={"email": "z@esempio.it"})
    with pool.connection() as c:
        c.execute("UPDATE sessioni SET scade_il = now() - interval '1 minute'")
        c.execute("UPDATE inviti SET scade_il = now() - interval '1 minute'")
        c.execute("INSERT INTO tentativi_accesso (chiave, istante) VALUES ('ip:x', now() - interval '2 days')")
    worker.pulisci()
    for tabella in ("sessioni", "inviti", "tentativi_accesso"):
        assert _conta(pool, f"SELECT count(*) AS n FROM {tabella}") == 0, tabella
    assert utenti["anna"].get("/api/io").status_code == 401
