"""API e pagine del servizio. `crea_app` riceve configurazione e pool: i test ne passano di propri."""

import datetime as dt
import ipaddress
import logging
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
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
# rate limiting dei tentativi falliti (accesso e cambio password)
FINESTRA_MINUTI = 15
MAX_PER_EMAIL = 5
MAX_PER_IP = 20
# niente script, stili o risorse esterne né inline: tutto dal servizio stesso
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "media-src 'self' blob:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


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

    def ip_cliente(request: Request) -> str:
        """Dietro il tunnel l'IP vero è in CF-Connecting-IP; lo si accetta solo se arriva da una rete privata
        (il container cloudflared), mai da un client che si inventa l'intestazione."""
        diretto = request.client.host if request.client else ""
        da_cf = request.headers.get("cf-connecting-ip")
        if cfg.ip_da_cloudflare and da_cf:
            try:
                privato = ipaddress.ip_address(diretto).is_private
            except ValueError:
                privato = False
            if privato:
                return da_cf.strip()[:64]
        return diretto

    def controlla_tentativi(request: Request, email: str) -> tuple[str, str]:
        chiave_ip = "ip:" + sicurezza.impronta(ip_cliente(request))
        chiave_email = "email:" + sicurezza.impronta(email.strip().lower())
        if (repo.tentativi_recenti(pool, chiave_email, FINESTRA_MINUTI) >= MAX_PER_EMAIL
                or repo.tentativi_recenti(pool, chiave_ip, FINESTRA_MINUTI) >= MAX_PER_IP):
            raise HTTPException(429, f"Troppi tentativi: riprova tra {FINESTRA_MINUTI} minuti.")
        return chiave_ip, chiave_email

    def apri_sessione(response: Response, utente_id: int):
        token, csrf = sicurezza.nuovo_token(), sicurezza.nuovo_token()
        repo.crea_sessione(pool, sicurezza.impronta(token), utente_id, csrf, cfg.durata_sessione_ore)
        response.set_cookie(nome_cookie, token, max_age=cfg.durata_sessione_ore * 3600, path="/",
                            secure=cfg.cookie_sicuro, httponly=True, samesite="strict")

    def password_valida(password: str) -> str:
        if len(password) < sicurezza.PASSWORD_MIN:
            raise HTTPException(400, f"La password deve avere almeno {sicurezza.PASSWORD_MIN} caratteri.")
        if len(password) > 256:
            raise HTTPException(400, "Password troppo lunga.")
        return password

    @app.post("/api/accesso", dependencies=[Depends(stessa_origine)])
    def accesso(request: Request, response: Response, dati: dict = Body(...)):
        email, password = str(dati.get("email", ""))[:320], str(dati.get("password", ""))[:256]
        chiave_ip, chiave_email = controlla_tentativi(request, email)
        utente = repo.utente_per_email(pool, email) if email else None
        valida = sicurezza.verifica_password(utente["hash_password"] if utente else None, password)
        if not (utente and valida and utente["attivo"]):
            repo.registra_tentativo(pool, chiave_ip, chiave_email)
            log.warning("accesso non riuscito")         # niente email né IP nei log
            raise HTTPException(401, "Email o password non corrette.")
        repo.azzera_tentativi(pool, chiave_email)
        if sicurezza.da_aggiornare(utente["hash_password"]):
            repo.aggiorna_hash(pool, utente["id"], sicurezza.hash_password(password))
        apri_sessione(response, utente["id"])
        return {"ok": True}

    @app.post("/api/uscita", dependencies=[Depends(stessa_origine)])
    def uscita(request: Request, response: Response):
        token = request.cookies.get(nome_cookie)
        if token:
            repo.elimina_sessione(pool, sicurezza.impronta(token))
        response.delete_cookie(nome_cookie, path="/", secure=cfg.cookie_sicuro, httponly=True, samesite="strict")
        return {"ok": True}

    @app.post("/api/password")
    def cambia_password(request: Request, dati: dict = Body(...), utente: dict = Depends(utente_corrente)):
        chiave_ip, chiave_email = controlla_tentativi(request, utente["email"])
        completo = repo.utente_per_email(pool, utente["email"])
        if not sicurezza.verifica_password(completo["hash_password"], str(dati.get("attuale", ""))[:256]):
            repo.registra_tentativo(pool, chiave_ip, chiave_email)
            raise HTTPException(400, "La password attuale non è corretta.")
        repo.aggiorna_hash(pool, utente["id"], sicurezza.hash_password(password_valida(str(dati.get("nuova", "")))))
        return {"ok": True}

    @app.get("/api/invito/{token}")
    def leggi_invito(token: str):
        invito = repo.invito_valido(pool, sicurezza.impronta(token))
        if invito is None:
            raise HTTPException(404, "Invito non valido, già usato o scaduto.")
        return {"email": invito["email"]}

    @app.post("/api/invito/{token}", dependencies=[Depends(stessa_origine)])
    def accetta_invito(token: str, response: Response, dati: dict = Body(...)):
        nome = str(dati.get("nome", "")).strip()[:100]
        hash_pw = sicurezza.hash_password(password_valida(str(dati.get("password", ""))))
        utente = repo.usa_invito(pool, sicurezza.impronta(token), nome, hash_pw)
        if utente is None:
            raise HTTPException(404, "Invito non valido, già usato o scaduto.")
        log.info("invito accettato: utente %s", utente["id"])
        apri_sessione(response, utente["id"])
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
        elimina_audio(cfg, job["id"])          # testo e segmenti spariscono con la riga del database
        log.info("job %s: eliminato", job["id"])
        return {"ok": True}

    @app.get("/api/job/{job_id}/testo")
    def scarica(job_id: str, utente: dict = Depends(utente_corrente)):
        job = repo.job_con_testo(pool, utente, job_id)
        if job is None:
            raise HTTPException(404, NON_TROVATO)
        nome = f"trascrizione_{dt.date.today():%Y%m%d}_{str(job['id'])[:8]}.txt"
        return Response(job["testo"], media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{nome}"',
                                 "Cache-Control": "no-store"})

    # --- amministrazione --------------------------------------------------

    def admin_corrente(utente: dict = Depends(utente_corrente)) -> dict:
        if utente["ruolo"] != "admin":
            raise HTTPException(403, "Riservato all'amministratore.")
        return utente

    def limite_opzionale(dati: dict, campo: str) -> int | None:
        valore = dati.get(campo)
        if valore in (None, ""):
            return None
        try:
            numero = int(valore)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{campo} deve essere un numero.") from None
        if not 1 <= numero <= 100_000:
            raise HTTPException(400, f"{campo} fuori dai limiti.")
        return numero

    @app.get("/api/admin/utenti")
    def admin_utenti(_admin: dict = Depends(admin_corrente)):
        return repo.elenco_utenti(pool)

    @app.put("/api/admin/utenti/{utente_id}")
    def admin_aggiorna_utente(utente_id: int, dati: dict = Body(...), admin: dict = Depends(admin_corrente)):
        riga = repo.aggiorna_utente(pool, admin, utente_id, bool(dati.get("attivo", True)),
                                    limite_opzionale(dati, "max_mb"), limite_opzionale(dati, "max_minuti"),
                                    limite_opzionale(dati, "max_job_giorno"))
        if riga is None:
            raise HTTPException(400 if utente_id == admin["id"] else 404,
                                "Non puoi disattivare il tuo account." if utente_id == admin["id"] else NON_TROVATO)
        log.info("utente %s aggiornato dall'admin", utente_id)
        return {"ok": True}

    @app.get("/api/admin/inviti")
    def admin_inviti(_admin: dict = Depends(admin_corrente)):
        return repo.inviti_in_attesa(pool)

    @app.post("/api/admin/inviti", status_code=201)
    def admin_crea_invito(dati: dict = Body(...), admin: dict = Depends(admin_corrente)):
        email = str(dati.get("email", "")).strip()[:320]
        ruolo = str(dati.get("ruolo", "user"))
        if "@" not in email or ruolo not in ("user", "admin"):
            raise HTTPException(400, "Email o ruolo non validi.")
        if repo.utente_per_email(pool, email):
            raise HTTPException(409, "Esiste già un utente con questa email.")
        token = sicurezza.nuovo_token()
        invito = repo.crea_invito(pool, admin, sicurezza.impronta(token), email, ruolo, cfg.giorni_invito)
        # il link lo compone la pagina (dopo il #, così il token non finisce nei log del server)
        return {**invito, "token": token}

    @app.delete("/api/admin/inviti/{prefisso}")
    def admin_revoca_invito(prefisso: str, _admin: dict = Depends(admin_corrente)):
        if not repo.revoca_invito(pool, prefisso):
            raise HTTPException(404, NON_TROVATO)
        return {"ok": True}

    @app.get("/api/admin/coda")
    def admin_coda(_admin: dict = Depends(admin_corrente)):
        return repo.coda_admin(pool)

    @app.put("/api/admin/job/{job_id}/priorita")
    def admin_priorita(job_id: str, dati: dict = Body(...), _admin: dict = Depends(admin_corrente)):
        try:
            priorita = int(dati.get("priorita"))
        except (TypeError, ValueError):
            raise HTTPException(400, "Priorità non valida.") from None
        if not -100 <= priorita <= 100:
            raise HTTPException(400, "La priorità va da -100 a 100.")
        if repo.imposta_priorita(pool, job_id, priorita) is None:
            raise HTTPException(404, "Job non trovato o non più in coda.")
        return {"ok": True}

    # --- varie --------------------------------------------------------------

    @app.get("/salute", include_in_schema=False)
    def salute():
        with pool.connection() as c:
            c.execute("SELECT 1")
        return {"ok": True}

    @app.middleware("http")
    async def intestazioni(request: Request, call_next):
        risposta = await call_next(request)
        h = risposta.headers
        if request.url.path.startswith("/api/"):
            h["Cache-Control"] = "no-store"
        h["Content-Security-Policy"] = CSP
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "no-referrer"
        h["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=(), payment=(), usb=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Cross-Origin-Resource-Policy"] = "same-origin"
        if cfg.cookie_sicuro:
            h["Strict-Transport-Security"] = "max-age=31536000"
        return risposta

    @app.exception_handler(HTTPException)
    async def errore_http(_request: Request, exc: HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    app.mount("/", StaticFiles(directory=CARTELLA_WEB, html=True), name="web")
    return app
