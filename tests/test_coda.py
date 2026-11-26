"""Coda con priorità, annullamento, riavvio con job a metà, cancellazione dell'audio."""

import logging

from trascrivo import repo
from trascrivo.worker import file_job


def test_job_admin_supera_quelli_degli_utenti(utenti, pool):
    a1 = utenti["anna"].carica().json()["id"]
    b1 = utenti["bruno"].carica().json()["id"]
    adm = utenti["admin"].carica().json()["id"]
    a2 = utenti["anna"].carica().json()["id"]

    ordine = []
    while (job := repo.prendi_prossimo(pool)) is not None:
        ordine.append(str(job["id"]))
    assert ordine == [adm, a1, b1, a2]


def test_posizione_in_coda(utenti):
    utenti["anna"].carica()
    utenti["admin"].carica()
    mio = utenti["bruno"].get("/api/job").json()
    assert len(mio) == 0
    utenti["bruno"].carica()
    mio = utenti["bruno"].get("/api/job").json()
    assert mio[0]["davanti"] == 2


def test_admin_annulla_job_di_un_utente(utenti, cfg):
    job = utenti["anna"].carica().json()["id"]
    assert file_job(cfg, job)["originale"].exists()
    r = utenti["admin"].post(f"/api/job/{job}/annulla")
    assert r.status_code == 200
    assert utenti["anna"].get(f"/api/job/{job}").json()["stato"] == "annullato"
    assert not file_job(cfg, job)["originale"].exists()      # audio di un job annullato cancellato subito


def test_utente_non_annulla_job_altrui(utenti):
    job = utenti["anna"].carica().json()["id"]
    assert utenti["bruno"].post(f"/api/job/{job}/annulla").status_code == 404
    assert utenti["anna"].get(f"/api/job/{job}").json()["stato"] == "in_coda"


def test_worker_completa_e_cancella_audio(utenti, worker, cfg):
    job = utenti["anna"].carica().json()["id"]
    worker.avvio()
    assert worker.esegui_uno()
    dati = utenti["anna"].get(f"/api/job/{job}").json()
    assert dati["stato"] == "completato" and dati["testo_scade_il"]
    f = file_job(cfg, job)
    assert not f["originale"].exists() and not f["wav"].exists() and not f["parziale"].exists()
    testo = utenti["anna"].get(f"/api/job/{job}/testo")
    assert testo.status_code == 200 and "frase segreta numero 5" in testo.text
    assert "attachment" in testo.headers["content-disposition"]


def test_riavvio_con_job_in_elaborazione_riprende_dal_checkpoint(utenti, worker, pool, cfg):
    job = utenti["anna"].carica().json()["id"]
    preso = repo.prendi_prossimo(pool)             # il worker lo prende...
    f = file_job(cfg, job)
    f["wav"].write_bytes(b"wav")                   # ...lo converte...
    # ...e muore dopo aver confermato 25 s; un segmento oltre il checkpoint è scritto ma non confermato
    f["parziale"].write_text("0.00\t9.00\tprima\n10.00\t19.00\tseconda\n20.00\t29.00\tnon confermata\n",
                             encoding="utf-8")
    repo.aggiorna_avanzamento(pool, preso["id"], 25.0)

    worker.avvio()                                 # riavvio: torna in coda
    assert utenti["anna"].get(f"/api/job/{job}").json()["stato"] == "in_coda"
    assert worker.esegui_uno()

    assert worker.motore.ripartenze == [int(35 * 16000)]   # ripartito da 25 s, non da zero
    testo = utenti["anna"].get(f"/api/job/{job}/testo").text
    assert "prima" in testo and "seconda" in testo and "non confermata" not in testo


def test_annullato_durante_elaborazione(utenti, worker, pool, cfg, monkeypatch):
    job = utenti["anna"].carica().json()["id"]
    monkeypatch.setattr("trascrivo.pipeline.CHECKPOINT_OGNI_S", 0)
    originale = worker.motore.segmenti

    def segmenti_con_annullo(*a):
        for i, s in enumerate(originale(*a)):
            if i == 1:
                utenti["admin"].post(f"/api/job/{job}/annulla")
            yield s
    monkeypatch.setattr(worker.motore, "segmenti", segmenti_con_annullo)
    worker.esegui_uno()
    assert utenti["anna"].get(f"/api/job/{job}").json()["stato"] == "annullato"
    f = file_job(cfg, job)
    assert not f["testo"].exists() and not f["parziale"].exists() and not f["wav"].exists()


def test_log_senza_testo_trascritto(utenti, worker, caplog):
    caplog.set_level(logging.DEBUG)
    utenti["anna"].carica()
    worker.avvio()
    worker.esegui_uno()
    assert "job" in caplog.text
    assert "frase segreta" not in caplog.text


def test_audio_non_valido_rifiutato(utenti):
    r = utenti["anna"].carica(b"NOAUDIO...")
    assert r.status_code == 415
    assert utenti["anna"].get("/api/job").json() == []


def test_limite_dimensione(utenti):
    r = utenti["anna"].carica(b"x" * (6 * 2**20))
    assert r.status_code == 413
