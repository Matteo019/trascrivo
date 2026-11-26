"""
Configurazione letta solo dall'ambiente (file .env caricato da Docker Compose).
Si valida tutto all'avvio: un valore sbagliato ferma il servizio con un messaggio chiaro.
"""

import os
from dataclasses import dataclass
from pathlib import Path

LINGUE = ("it", "en", "auto")
MODELLI = ("small", "medium", "large-v3-turbo", "large-v3")


class ErroreConfigurazione(RuntimeError):
    pass


def _testo(nome: str, predefinito: str | None = None) -> str:
    valore = os.environ.get(nome, "").strip()
    if valore:
        return valore
    if predefinito is None:
        raise ErroreConfigurazione(f"Manca la variabile {nome} (vedi .env.example).")
    return predefinito


def _intero(nome: str, predefinito: int, minimo: int = 0) -> int:
    grezzo = _testo(nome, str(predefinito))
    try:
        valore = int(grezzo)
    except ValueError:
        raise ErroreConfigurazione(f"{nome} deve essere un numero intero, non «{grezzo}».") from None
    if valore < minimo:
        raise ErroreConfigurazione(f"{nome} deve essere almeno {minimo}.")
    return valore


def _booleano(nome: str, predefinito: bool) -> bool:
    return _testo(nome, "1" if predefinito else "0").lower() in ("1", "true", "si", "sì", "yes")


def _scelta(nome: str, predefinito: str, ammessi: tuple) -> str:
    valore = _testo(nome, predefinito)
    if valore not in ammessi:
        raise ErroreConfigurazione(f"{nome}={valore} non valido: ammessi {', '.join(ammessi)}.")
    return valore


@dataclass(frozen=True)
class Config:
    database_url: str
    cartella_dati: Path
    # Whisper
    modello: str
    lingua: str
    thread: int
    beam: int
    normalizza_audio: bool
    # limiti predefiniti per utente (sovrascrivibili per singolo utente)
    max_mb: int
    max_minuti: int
    max_job_giorno: int
    # conservazione
    giorni_conservazione: int
    # web
    cookie_sicuro: bool
    durata_sessione_ore: int
    ip_da_cloudflare: bool = False
    giorni_invito: int = 7

    @property
    def cartella_audio(self) -> Path:
        return self.cartella_dati / "audio"

    @property
    def cartella_lavoro(self) -> Path:
        return self.cartella_dati / "lavoro"

    def crea_cartelle(self):
        for c in (self.cartella_audio, self.cartella_lavoro):
            c.mkdir(parents=True, exist_ok=True)


def carica() -> Config:
    core = os.cpu_count() or 2
    return Config(
        database_url=_testo("DATABASE_URL"),
        cartella_dati=Path(_testo("CARTELLA_DATI", "data")).resolve(),
        modello=_scelta("WHISPER_MODELLO", "small", MODELLI),
        lingua=_scelta("WHISPER_LINGUA", "it", LINGUE),
        # di solito i logici sono il doppio dei fisici: Whisper rende meglio sui core fisici
        thread=_intero("WHISPER_THREAD", max(1, core // 2), minimo=1),
        beam=_intero("WHISPER_BEAM", 1, minimo=1),
        normalizza_audio=_booleano("AUDIO_NORMALIZZA", True),
        max_mb=_intero("LIMITE_MB", 100, minimo=1),
        max_minuti=_intero("LIMITE_MINUTI", 180, minimo=1),
        max_job_giorno=_intero("LIMITE_JOB_GIORNO", 20, minimo=1),
        giorni_conservazione=_intero("CONSERVAZIONE_GIORNI", 7, minimo=1),
        cookie_sicuro=_booleano("COOKIE_SICURO", True),
        durata_sessione_ore=_intero("SESSIONE_ORE", 24 * 7, minimo=1),
        ip_da_cloudflare=_booleano("IP_DA_CLOUDFLARE", True),
        giorni_invito=_intero("INVITO_GIORNI", 7, minimo=1),
    )
