"""
Worker della coda: un job alla volta, prima per priorità poi per ordine di arrivo.
Gira in un processo separato dal web (stesso codice, comando `python -m trascrivo worker`).
Nei log solo ID dei job: mai testo trascritto né nomi di file.
"""

import logging
import os
import signal
import threading
import time
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool

from . import audio, pipeline, repo
from .config import Config
from .db import CANALE_CODA

log = logging.getLogger(__name__)


def file_job(cfg: Config, job_id) -> dict[str, Path]:
    return {
        "originale": cfg.cartella_audio / f"{job_id}.in",
        "wav": cfg.cartella_lavoro / f"{job_id}.wav",
    }


def elimina_audio(cfg: Config, job_id):
    """Audio originale e audio convertito: si cancellano appena il job è concluso."""
    f = file_job(cfg, job_id)
    for chiave in ("originale", "wav"):
        try:
            f[chiave].unlink(missing_ok=True)
        except OSError:      # file ancora aperto dal worker (solo Windows): lo cancella il worker alla fine
            pass


PULIZIA_OGNI_S = 3600
ORFANI_DOPO_S = 3600       # un file senza job attivo più vecchio di così è un avanzo (upload interrotto, crash)


class Worker:
    def __init__(self, pool: ConnectionPool, cfg: Config, motore: pipeline.Motore,
                 converti=audio.converti, leggi=audio.leggi_wav):
        self.pool, self.cfg, self.motore = pool, cfg, motore
        self._converti, self._leggi = converti, leggi
        self.fermati = threading.Event()
        self._ultima_pulizia = 0.0

    def avvio(self):
        self.cfg.crea_cartelle()
        n = repo.rimetti_in_coda_interrotti(self.pool)
        if n:
            log.info("%d job interrotti rimessi in coda", n)
        self.pulisci()

    def pulisci(self):
        """Conservazione: righe scadute nel database e file audio rimasti senza un job attivo."""
        self._ultima_pulizia = time.monotonic()
        conti = repo.pulizia(self.pool, self.cfg.giorni_conservazione)
        attivi = repo.job_attivi(self.pool)
        limite = time.time() - ORFANI_DOPO_S
        orfani = 0
        for cartella in (self.cfg.cartella_audio, self.cfg.cartella_lavoro):
            for f in cartella.iterdir():
                if f.is_file() and f.name.split(".", 1)[0] not in attivi and f.stat().st_mtime < limite:
                    f.unlink(missing_ok=True)
                    orfani += 1
        if orfani or any(conti.values()):
            log.info("pulizia: %s, file orfani %d", ", ".join(f"{k} {v}" for k, v in conti.items()), orfani)

    def esegui_uno(self) -> bool:
        """Elabora il prossimo job, se c'è. True se ne ha preso uno."""
        job = repo.prendi_prossimo(self.pool)
        if job is None:
            return False
        jid: UUID = job["id"]
        f = file_job(self.cfg, jid)
        log.info("job %s: inizio (ripresa da %.0f s)", jid, job["avanzamento_s"])
        try:
            if not f["wav"].exists():
                self._converti(f["originale"], f["wav"], self.cfg.normalizza_audio)
                f["originale"].unlink(missing_ok=True)      # da qui basta il WAV convertito
            dati = self._leggi(f["wav"])
            repo.imposta_durata(self.pool, jid, len(dati) / audio.FREQUENZA)
            segmenti = pipeline.trascrivi(
                self.motore, dati, job["lingua"], job["glossario"] or "",
                gia_fatti=repo.segmenti_salvati(self.pool, jid), da_secondi=job["avanzamento_s"],
                salva=lambda nuovi, punto: repo.salva_checkpoint(self.pool, jid, nuovi, punto))
            if repo.completa(self.pool, jid, pipeline.formatta(segmenti), self.cfg.giorni_conservazione):
                log.info("job %s: completato", jid)
            else:                                           # annullato proprio alla fine
                log.info("job %s: annullato", jid)
        except pipeline.Annullato:
            log.info("job %s: annullato durante l'elaborazione", jid)
        except audio.AudioNonValido as exc:
            repo.fallisci(self.pool, jid, str(exc))
            log.warning("job %s: audio non valido", jid)
        except Exception as exc:  # noqa: BLE001 — qualsiasi errore chiude il job, non il worker
            repo.fallisci(self.pool, jid, "Errore interno durante la trascrizione.")
            log.error("job %s: errore %s", jid, type(exc).__name__)
        finally:
            # concluso in qualsiasi modo: l'audio non resta sul disco. Se il processo muore qui
            # dentro (riavvio), il finally non gira e il job riprende dal checkpoint.
            elimina_audio(self.cfg, jid)
            if repo.stato_job(self.pool, jid) != "in_elaborazione":
                repo.scarta_segmenti(self.pool, jid)        # concluso: i segmenti di lavoro non servono più
        return True

    def ciclo(self):
        """Esegue job finché ce ne sono, poi aspetta una notifica (o 5 s, per sicurezza)."""
        self.avvio()
        with psycopg.connect(self.cfg.database_url, autocommit=True) as ascolto:
            ascolto.execute(f"LISTEN {CANALE_CODA}")
            while not self.fermati.is_set():
                if time.monotonic() - self._ultima_pulizia >= PULIZIA_OGNI_S:
                    self.pulisci()
                if self.esegui_uno():
                    continue
                for _ in ascolto.notifies(timeout=5, stop_after=1):
                    pass


def main(cfg: Config, pool: ConnectionPool):
    try:
        os.nice(10)       # priorità bassa: Jellyfin e gli altri servizi restano reattivi
    except (AttributeError, OSError):
        pass
    motore = pipeline.WhisperLocale(cfg.modello, cfg.thread, cfg.beam)
    motore.carica()       # all'avvio, non al primo job: l'utente non aspetta il caricamento
    w = Worker(pool, cfg, motore)
    signal.signal(signal.SIGTERM, lambda *_: w.fermati.set())
    log.info("worker pronto: modello %s, %d thread, beam %d", cfg.modello, cfg.thread, cfg.beam)
    w.ciclo()
