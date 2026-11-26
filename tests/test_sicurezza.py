"""Fase 2: rate limiting, inviti, pannello admin, intestazioni di sicurezza."""

from conftest import PASSWORD
from fastapi.testclient import TestClient

from trascrivo import repo


def login(app, email, password, **intestazioni):
    return TestClient(app).post("/api/accesso", json={"email": email, "password": password}, headers=intestazioni)


# --- rate limiting -------------------------------------------------------------

def test_troppi_tentativi_sulla_stessa_email(app, utenti):
    for _ in range(5):
        assert login(app, "anna@esempio.it", "sbagliata").status_code == 401
    # bloccata anche con la password giusta, finché non passa la finestra
    assert login(app, "anna@esempio.it", PASSWORD).status_code == 429
    # le altre email non sono toccate
    assert login(app, "bruno@esempio.it", PASSWORD).status_code == 200


def test_troppi_tentativi_dallo_stesso_ip(app, utenti):
    for i in range(20):
        login(app, f"inesistente{i}@esempio.it", "x")
    assert login(app, "bruno@esempio.it", PASSWORD).status_code == 429


def test_ip_cloudflare_accettato_solo_da_rete_privata(app, utenti):
    # il client dei test non è un IP privato: l'intestazione inventata non deve cambiare l'IP contato
    for i in range(20):
        login(app, f"x{i}@esempio.it", "x", **{"CF-Connecting-IP": f"203.0.113.{i}"})
    assert login(app, "bruno@esempio.it", PASSWORD, **{"CF-Connecting-IP": "198.51.100.7"}).status_code == 429


def test_accesso_riuscito_azzera_i_tentativi(app, utenti):
    for _ in range(4):
        login(app, "anna@esempio.it", "sbagliata")
    assert login(app, "anna@esempio.it", PASSWORD).status_code == 200
    for _ in range(4):
        login(app, "anna@esempio.it", "sbagliata")
    assert login(app, "anna@esempio.it", PASSWORD).status_code == 200


# --- inviti ----------------------------------------------------------------------

def crea_invito(utenti, email="carla@esempio.it", ruolo="user"):
    r = utenti["admin"].post("/api/admin/inviti", json={"email": email, "ruolo": ruolo})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def test_invito_crea_account_una_volta_sola(app, utenti):
    token = crea_invito(utenti)
    nuovo = TestClient(app)
    assert nuovo.get(f"/api/invito/{token}").json()["email"] == "carla@esempio.it"
    r = nuovo.post(f"/api/invito/{token}", json={"nome": "Carla", "password": "una-password-lunga"})
    assert r.status_code == 200
    io = nuovo.get("/api/io").json()                  # già collegata
    assert io["email"] == "carla@esempio.it" and io["ruolo"] == "user"
    # secondo uso dello stesso link: rifiutato
    assert TestClient(app).post(f"/api/invito/{token}", json={"password": "altra-password-lunga"}).status_code == 404
    assert TestClient(app).get(f"/api/invito/{token}").status_code == 404


def test_invito_scaduto_o_inventato(app, utenti, pool):
    token = crea_invito(utenti)
    with pool.connection() as c:
        c.execute("UPDATE inviti SET scade_il = now() - interval '1 minute'")
    assert TestClient(app).post(f"/api/invito/{token}", json={"password": "una-password-lunga"}).status_code == 404
    assert TestClient(app).get("/api/invito/token-inventato").status_code == 404


def test_invito_password_corta(app, utenti):
    token = crea_invito(utenti)
    assert TestClient(app).post(f"/api/invito/{token}", json={"password": "corta"}).status_code == 400
    assert TestClient(app).get(f"/api/invito/{token}").status_code == 200     # non consumato


def test_token_invito_non_salvato_in_chiaro(utenti, pool):
    token = crea_invito(utenti)
    with pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM inviti WHERE id = %s", (token,)).fetchone()["n"] == 0


def test_solo_admin_puo_invitare_e_amministrare(utenti):
    anna = utenti["anna"]
    assert anna.post("/api/admin/inviti", json={"email": "x@esempio.it"}).status_code == 403
    for percorso in ("/api/admin/utenti", "/api/admin/inviti", "/api/admin/coda"):
        assert anna.get(percorso).status_code == 403
    assert anna.put("/api/admin/utenti/1", json={"attivo": False}).status_code == 403


def test_nessuna_registrazione_pubblica(app):
    rotte = {r.path for r in app.routes}
    assert not any("registra" in r or "signup" in r for r in rotte)


# --- pannello admin ------------------------------------------------------------------

def test_disattivazione_scollega_subito(utenti, pool):
    bruno_id = repo.utente_per_email(pool, "bruno@esempio.it")["id"]
    assert utenti["bruno"].get("/api/io").status_code == 200
    assert utenti["admin"].put(f"/api/admin/utenti/{bruno_id}", json={"attivo": False}).status_code == 200
    assert utenti["bruno"].get("/api/io").status_code == 401


def test_admin_non_si_disattiva_da_solo(utenti, pool):
    admin_id = repo.utente_per_email(pool, "admin@esempio.it")["id"]
    assert utenti["admin"].put(f"/api/admin/utenti/{admin_id}", json={"attivo": False}).status_code == 400


def test_limiti_per_utente(utenti, pool):
    anna_id = repo.utente_per_email(pool, "anna@esempio.it")["id"]
    utenti["admin"].put(f"/api/admin/utenti/{anna_id}", json={"attivo": True, "max_job_giorno": 1})
    assert utenti["anna"].carica().status_code == 201
    assert utenti["anna"].carica().status_code == 429
    assert utenti["bruno"].carica().status_code == 201          # gli altri hanno ancora il limite predefinito


def test_admin_cambia_priorita(utenti, pool):
    a = utenti["anna"].carica().json()["id"]
    b = utenti["bruno"].carica().json()["id"]
    assert utenti["admin"].put(f"/api/admin/job/{b}/priorita", json={"priorita": 5}).status_code == 200
    assert str(repo.prendi_prossimo(pool)["id"]) == b
    assert str(repo.prendi_prossimo(pool)["id"]) == a


def test_coda_admin_senza_testo(utenti, worker):
    utenti["anna"].carica()
    worker.avvio()
    worker.esegui_uno()
    coda = utenti["admin"].get("/api/admin/coda").json()
    assert coda[0]["proprietario"] == "anna@esempio.it"
    assert "testo" not in coda[0] and "frase segreta" not in str(coda)


# --- account e intestazioni ------------------------------------------------------

def test_cambio_password(app, utenti):
    anna = utenti["anna"]
    assert anna.post("/api/password", json={"attuale": "sbagliata", "nuova": "nuova-password-123"}).status_code == 400
    assert anna.post("/api/password", json={"attuale": PASSWORD, "nuova": "corta"}).status_code == 400
    assert anna.post("/api/password", json={"attuale": PASSWORD, "nuova": "nuova-password-123"}).status_code == 200
    assert login(app, "anna@esempio.it", PASSWORD).status_code == 401
    assert login(app, "anna@esempio.it", "nuova-password-123").status_code == 200


def test_intestazioni_di_sicurezza(app):
    r = TestClient(app).get("/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "microphone=(self)" in r.headers["permissions-policy"]


def test_cookie_sicuro_in_produzione(cfg, pool, utenti):
    from dataclasses import replace

    from trascrivo.app import crea_app
    app = crea_app(replace(cfg, cookie_sicuro=True), pool)
    r = TestClient(app, base_url="https://testserver").post(
        "/api/accesso", json={"email": "anna@esempio.it", "password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith("__host-sessione=")
    assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert "max-age" in cookie
    assert r.headers["strict-transport-security"]

