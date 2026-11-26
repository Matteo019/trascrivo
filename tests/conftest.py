"""
Test contro un PostgreSQL vero.
- DATABASE_URL_TEST impostata (es. il Postgres del compose): si crea un database usa-e-getta lì;
- altrimenti, se c'è il pacchetto `pgserver` (solo sviluppo), se ne avvia uno temporaneo.
Whisper e ffmpeg sono finti: i test controllano coda, permessi e file, non la qualità della trascrizione.
"""

import os
import uuid
from dataclasses import replace

import numpy as np
import psycopg
import pytest
from fastapi.testclient import TestClient

from trascrivo import audio, db, repo, sicurezza
from trascrivo.app import crea_app
from trascrivo.config import Config
from trascrivo.worker import Worker

PASSWORD = "password-di-prova-123"


@pytest.fixture(scope="session")
def url_server(tmp_path_factory):
    url = os.environ.get("DATABASE_URL_TEST")
    if url:
        yield url
        return
    pgserver = pytest.importorskip("pgserver", reason="serve DATABASE_URL_TEST oppure il pacchetto pgserver")
    server = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
    yield server.get_uri()


@pytest.fixture(scope="session")
def url_db(url_server):
    nome = f"trascrivo_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(url_server, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{nome}"')
    url = url_server.rsplit("/", 1)[0] + f"/{nome}"
    db.migra(url)
    yield url
    with psycopg.connect(url_server, autocommit=True) as c:
        c.execute(f'DROP DATABASE "{nome}" WITH (FORCE)')


@pytest.fixture
def pool(url_db):
    p = db.crea_pool(url_db, 4)
    with p.connection() as c:
        c.execute("TRUNCATE utenti, sessioni, progetti, job RESTART IDENTITY CASCADE")
    yield p
    p.close()


@pytest.fixture
def cfg(url_db, tmp_path):
    c = Config(database_url=url_db, cartella_dati=tmp_path / "data", modello="small", lingua="it", thread=1,
               beam=1, normalizza_audio=False, max_mb=5, max_minuti=60, max_job_giorno=50,
               giorni_conservazione=7, cookie_sicuro=False, durata_sessione_ore=1)
    c.crea_cartelle()
    return c


@pytest.fixture(autouse=True)
def ffprobe_finto(monkeypatch):
    """Ogni upload «dura» 120 s, salvo i corpi che iniziano con NOAUDIO."""
    def analizza(percorso):
        if percorso.read_bytes().startswith(b"NOAUDIO"):
            raise audio.AudioNonValido("Il file non è un audio leggibile.")
        return 120.0
    monkeypatch.setattr(audio, "analizza", analizza)


class MotoreFinto:
    """Un segmento ogni 10 s di audio; ricorda da dove è ripartito."""
    FRASE = "frase segreta numero"

    def __init__(self):
        self.ripartenze = []

    def segmenti(self, dati, lingua, glossario):
        self.ripartenze.append(len(dati))
        durata = len(dati) / audio.FREQUENZA
        for i in range(int(durata // 10)):
            yield i * 10.0, i * 10.0 + 9.0, f"{self.FRASE} {i} lunghezza {len(dati)}"


@pytest.fixture
def worker(pool, cfg):
    def converti(sorgente, destinazione, _normalizza):
        destinazione.write_bytes(b"wav")

    def leggi(_percorso):
        return np.zeros(int(60 * audio.FREQUENZA), dtype=np.float32)    # 60 s di audio

    return Worker(pool, cfg, MotoreFinto(), converti=converti, leggi=leggi)


def crea_utente(pool, email, ruolo="user"):
    return repo.crea_utente(pool, email, "", sicurezza.hash_password(PASSWORD), ruolo)


class Cliente:
    """TestClient già autenticato, che manda da solo il token CSRF."""

    def __init__(self, app, email):
        self.http = TestClient(app)
        r = self.http.post("/api/accesso", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        self.csrf = self.http.get("/api/io").json()["csrf"]

    def __getattr__(self, metodo):
        def chiama(url, **kw):
            kw.setdefault("headers", {})["X-CSRF"] = self.csrf
            return getattr(self.http, metodo)(url, **kw)
        return chiama

    def carica(self, contenuto=b"audio-finto", **parametri):
        return self.post("/api/job", params=parametri, content=contenuto,
                         headers={"Content-Type": "audio/ogg"})


@pytest.fixture
def app(cfg, pool):
    return crea_app(cfg, pool)


@pytest.fixture
def utenti(pool, app):
    """Un admin e due utenti normali, già collegati."""
    for email, ruolo in (("admin@esempio.it", "admin"), ("anna@esempio.it", "user"), ("bruno@esempio.it", "user")):
        crea_utente(pool, email, ruolo)
    return {nome: Cliente(app, f"{nome}@esempio.it") for nome in ("admin", "anna", "bruno")}


def con_cfg(cfg, **campi):
    return replace(cfg, **campi)
