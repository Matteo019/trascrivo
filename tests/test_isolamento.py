"""Un utente non può mai leggere, scaricare, modificare o cancellare dati di un altro."""

import uuid

import pytest


@pytest.fixture
def job_completato_di_anna(utenti, worker):
    job = utenti["anna"].carica().json()["id"]
    worker.avvio()
    worker.esegui_uno()
    return job


def test_bruno_non_vede_i_job_di_anna(utenti, job_completato_di_anna):
    job = job_completato_di_anna
    bruno = utenti["bruno"]
    assert bruno.get("/api/job").json() == []
    assert bruno.get(f"/api/job/{job}").status_code == 404
    assert bruno.get(f"/api/job/{job}/testo").status_code == 404
    assert bruno.delete(f"/api/job/{job}").status_code == 404
    assert bruno.post(f"/api/job/{job}/annulla").status_code == 404
    # e il job di anna è ancora lì, intatto
    assert utenti["anna"].get(f"/api/job/{job}/testo").status_code == 200


@pytest.mark.parametrize("id_indovinato", [str(uuid.uuid4()), "1", "../../etc/passwd", "%00", "' OR 1=1 --"])
def test_id_indovinati_o_malformati(utenti, id_indovinato):
    for metodo, percorso in (("get", ""), ("get", "/testo"), ("delete", ""), ("post", "/annulla")):
        r = getattr(utenti["bruno"], metodo)(f"/api/job/{id_indovinato}{percorso}")
        # 405: il percorso con ../ esce da /api/job e arriva ai file statici, che accettano solo GET
        assert r.status_code in (404, 405), (metodo, percorso, r.status_code)
        assert "frase" not in r.text


def test_admin_vede_metadati_ma_non_il_testo(utenti, job_completato_di_anna):
    job = job_completato_di_anna
    assert utenti["admin"].get(f"/api/job/{job}").json()["stato"] == "completato"
    assert utenti["admin"].get(f"/api/job/{job}/testo").status_code == 404
    assert utenti["admin"].delete(f"/api/job/{job}").status_code == 404


def test_progetti_isolati(utenti):
    p = utenti["anna"].post("/api/progetti", json={"nome": "Analisi 1", "glossario": "derivata"}).json()
    bruno = utenti["bruno"]
    assert bruno.get("/api/progetti").json() == []
    assert bruno.put(f"/api/progetti/{p['id']}", json={"nome": "rubato"}).status_code == 404
    assert bruno.delete(f"/api/progetti/{p['id']}").status_code == 404
    # bruno non può nemmeno usare il progetto di anna (e quindi il suo glossario)
    assert bruno.carica(progetto=p["id"]).status_code == 404
    assert utenti["anna"].get("/api/progetti").json()[0]["nome"] == "Analisi 1"


def test_senza_accesso(app, utenti):
    from fastapi.testclient import TestClient
    anonimo = TestClient(app)
    assert anonimo.get("/api/job").status_code == 401
    assert anonimo.post("/api/job", content=b"x").status_code == 401


def test_csrf_obbligatorio(utenti):
    anna = utenti["anna"]
    job = anna.carica().json()["id"]
    r = anna.http.post(f"/api/job/{job}/annulla")                    # senza intestazione X-CSRF
    assert r.status_code == 403
    r = anna.http.post(f"/api/job/{job}/annulla", headers={"X-CSRF": "sbagliato"})
    assert r.status_code == 403


def test_login_da_altro_sito_rifiutato(app, utenti):
    from fastapi.testclient import TestClient
    r = TestClient(app).post("/api/accesso", json={"email": "anna@esempio.it", "password": "x"},
                             headers={"Origin": "https://malevolo.example"})
    assert r.status_code == 403


def test_password_sbagliata(app, utenti):
    from fastapi.testclient import TestClient
    r = TestClient(app).post("/api/accesso", json={"email": "anna@esempio.it", "password": "sbagliata"})
    assert r.status_code == 401
