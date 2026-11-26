"""API e pagine del servizio. `crea_app` riceve configurazione e pool: i test ne passano di propri."""

import datetime as dt
import logging
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg.errors import UniqueViolation
from psycopg_pool import ConnectionPool

from . import audio, repo, sicurezza
from .config import LINGUE, Config
from .worker import elimina_audio, file_job

log = logging.getLogger(__name__)
CARTELLA_WEB = Path(__file__).resolve().parent.parent / "web"
METODI_SICURI = {"GET", "HEAD", "OPTIONS"}
NON_TROVATO = "Non trovato."


def crea_app(cfg: Config, pool: ConnectionPool) -> FastAPI:
    app = FastAPI(title="trascrivo", docs_url=None, redoc_url=None, openapi_url=None)
    nome_cookie = "__Host-sessione" if cfg.cookie_sicuro else "sessione"

    # --- autenticazione -----------------------------------------------------

    def utente_corrente(request: Request) -> dict:
        token = request.cookies.get(nome_cookie)
        utente = repo.utente_da_sessione(pool, sicurezza.impronta(token)) if token else None
        if utente is None:
            raise HTTPException(401, "Accesso richiesto.")
        if request.method not in METODI_SICURI and not sicurezza.stesso_token(
                request.headers.get("x-csrf", ""), utente["csrf"]):
            raise HTTPException(403, "Richiesta non valida (CSRF): ricarica la pagina.")
        return utente

    def stessa_origine(request: Request):
        """Login e logout senza sessione CSRF: si accettano solo dalla pagina del servizio."""
        origine = request.headers.get("origin")
        if origine is not None and urlsplit(origine).netloc != request.headers.get("host"):
            raise HTTPException(403, "Richiesta da un altro sito.")

    def limiti(utente: dict) -> dict:
        return {"max_mb": utente["max_mb"] or cfg.max_mb,
                "max_minuti": utente["max_minuti"] or cfg.max_minuti,
                "max_job_giorno": utente["max_job_giorno"] or cfg.max_job_giorno}

    @app.post("/api/accesso", dependencies=[Depends(stessa_origine)])
    def accesso(response: Response, dati: dict = Body(...)):
        email, password = str(dati.get("email", "")), str(dati.get("password", ""))
        utente = repo.utente_per_email(pool, email) if email else None
        valida = sicurezza.verifica_password(utente["hash_password"] if utente else None, password)
        if not (utente and valida and utente["attivo"]):
            raise HTTPException(401, "Email o password non corrette.")
        if sicurezza.da_aggiornare(utente["hash_password"]):
            repo.aggiorna_hash(pool, utente["id"], sicurezza.hash_password(password))
        token, csrf = sicurezza.nuovo_token(), sicurezza.nuovo_token()
        repo.crea_sessione(pool, sicurezza.impronta(token), utente["id"], csrf, cfg.durata_sessione_ore)
        response.set_cookie(nome_cookie, token, max_age=cfg.durata_sessione_ore * 3600, path="/",
                            secure=cfg.cookie_sicuro, httponly=True, samesite="strict")
        return {"ok": True}

    @app.post("/api/uscita", dependencies=[Depends(stessa_origine)])
    def uscita(request: Request, response: Response):
        token = request.cookies.get(nome_cookie)
        if token:
            repo.elimina_sessione(pool, sicurezza.impronta(token))
        response.delete_cookie(nome_cookie, path="/", secure=cfg.cookie_sicuro, httponly=True, samesite="strict")
        return {"ok": True}

    @app.get("/api/io")
    def io(utente: dict = Depends(utente_corrente)):
        return {"email": utente["email"], "nome": utente["nome"], "ruolo": utente["ruolo"],
                "csrf": utente["csrf"], "limiti": limiti(utente), "lingue": LINGUE,
                "lingua_predefinita": cfg.lingua, "conservazione_giorni": cfg.giorni_conservazione}

    # --- progetti -----------------------------------------------------------

    def dati_progetto(dati: dict) -> tuple[str, str]:
        nome = str(dati.get("nome", "")).strip()[:100]
        if not nome:
            raise HTTPException(400, "Il nome del progetto è obbligatorio.")
        return nome, str(dati.get("glossario", ""))[:5000]

    @app.get("/api/progetti")
    def progetti(utente: dict = Depends(utente_corrente)):
        return repo.progetti_di(pool, utente)

    @app.post("/api/progetti")
    def crea_progetto(dati: dict = Body(...), utente: dict = Depends(utente_corrente)):
        return repo.crea_progetto(pool, utente, *dati_progetto(dati))

    @app.put("/api/progetti/{progetto_id}")
    def aggiorna_progetto(progetto_id: int, dati: dict = Body(...), utente: dict = Depends(utente_corrente)):
        try:
            riga = repo.aggiorna_progetto(pool, utente, progetto_id, *dati_progetto(dati))
        except UniqueViolation:
            raise HTTPException(409, "Hai già un progetto con questo nome.") from None
        if riga is None:
            raise HTTPException(404, NON_TROVATO)
        return riga

    @app.delete("/api/progetti/{progetto_id}")
    def elimina_progetto(progetto_id: int, utente: dict = Depends(utente_corrente)):
        if not repo.elimina_progetto(pool, utente, progetto_id):
            raise HTTPException(404, NON_TROVATO)
        return {"ok": True}

    # --- job ----------------------------------------------------------------

    @app.post("/api/job", status_code=201)
    async def carica(request: Request, lingua: str = "", progetto: int | None = None,
                     utente: dict = Depends(utente_corrente)):
        """Il corpo della richiesta è il file audio, letto a pezzi con un limite di dimensione."""
        lim = limiti(utente)
        lingua = lingua or cfg.lingua
        if lingua not in LINGUE:
            raise HTTPException(400, "Lingua non valida.")
        if await run_in_threadpool(repo.job_creati_oggi, pool, utente) >= lim["max_job_giorno"]:
            raise HTTPException(429, f"Hai raggiunto il limite di {lim['max_job_giorno']} trascrizioni al giorno.")
        massimo = lim["max_mb"] * 2**20
        if int(request.headers.get("content-length") or 0) > massimo:
            raise HTTPException(413, f"Il file supera {lim['max_mb']} MB.")

        job_id = uuid.uuid4()
        destinazione = file_job(cfg, job_id)["originale"]
        temporaneo = destinazione.with_suffix(".caricamento")
        scritti = 0
        try:
            with open(temporaneo, "wb") as f:
                async for pezzo in request.stream():
                    scritti += len(pezzo)
                    if scritti > massimo:
                        raise HTTPException(413, f"Il file supera {lim['max_mb']} MB.")
                    f.write(pezzo)
            if scritti == 0:
                raise HTTPException(400, "Il file è vuoto.")
            try:
                durata = await run_in_threadpool(audio.analizza, temporaneo)
            except audio.AudioNonValido as exc:
                raise HTTPException(415, str(exc)) from None
            if durata > lim["max_minuti"] * 60:
                raise HTTPException(413, f"L'audio supera {lim['max_minuti']} minuti.")
            temporaneo.replace(destinazione)
            job = await run_in_threadpool(repo.crea_job, pool, utente, job_id, progetto, lingua, cfg.modello, durata)
            if job is None:
                destinazione.unlink(missing_ok=True)
                raise HTTPException(404, "Progetto non trovato.")
        finally:
            temporaneo.unlink(missing_ok=True)
        log.info("job %s: in coda", job_id)
        return job

    @app.get("/api/job")
    def elenco(utente: dict = Depends(utente_corrente)):
        return repo.elenco_job(pool, utente)

    @app.get("/api/job/{job_id}")
    def dettaglio(job_id: str, utente: dict = Depends(utente_corrente)):
        job = repo.job_visibile(pool, utente, job_id)
        if job is None:
            raise HTTPException(404, NON_TROVATO)
        return job

    @app.post("/api/job/{job_id}/annulla")
    def annulla(job_id: str, utente: dict = Depends(utente_corrente)):
        job = repo.annulla_job(pool, utente, job_id)
        if job is None:
            raise HTTPException(404, NON_TROVATO)
        # se era in coda l'audio si cancella qui; se era in elaborazione ci pensa anche il worker
        elimina_audio(cfg, job["id"])
        log.info("job %s: annullato", job["id"])
        return {"ok": True}

    @app.delete("/api/job/{job_id}")
    def elimina(job_id: str, utente: dict = Depends(utente_corrente)):
        job = repo.elimina_job(pool, utente, job_id)
        if job is None:
            raise HTTPException(404, NON_TROVATO)
        elimina_audio(cfg, job["id"])
        file_job(cfg, job["id"])["testo"].unlink(missing_ok=True)
        log.info("job %s: eliminato", job["id"])
        return {"ok": True}

    @app.get("/api/job/{job_id}/testo")
    def scarica(job_id: str, utente: dict = Depends(utente_corrente)):
        job = repo.job_con_testo(pool, utente, job_id)
        percorso = file_job(cfg, job["id"])["testo"] if job else None
        if percorso is None or not percorso.exists():
            raise HTTPException(404, NON_TROVATO)
        nome = f"trascrizione_{dt.date.today():%Y%m%d}_{str(job['id'])[:8]}.txt"
        return FileResponse(percorso, media_type="text/plain; charset=utf-8", filename=nome,
                            headers={"Cache-Control": "no-store"})

    # --- varie --------------------------------------------------------------

    @app.get("/salute", include_in_schema=False)
    def salute():
        with pool.connection() as c:
            c.execute("SELECT 1")
        return {"ok": True}

    @app.middleware("http")
    async def intestazioni(request: Request, call_next):
        risposta = await call_next(request)
        if request.url.path.startswith("/api/"):
            risposta.headers["Cache-Control"] = "no-store"
        risposta.headers["X-Content-Type-Options"] = "nosniff"
        return risposta

    @app.exception_handler(HTTPException)
    async def errore_http(_request: Request, exc: HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    app.mount("/", StaticFiles(directory=CARTELLA_WEB, html=True), name="web")
    return app
