"""
Trascrizione di un file audio con checkpoint.

Il file intero passa a faster-whisper con il VAD attivo (salta i silenzi). I segmenti vengono scritti
man mano nel database insieme al punto raggiunto, così un job interrotto (riavvio, crash, errore)
riparte da lì e non dall'inizio. Questo sostituisce la cartella
da_ritrascrivere/ dell'app desktop: stesso scopo, non perdere il lavoro già fatto.
"""

import logging
import re
import time
from collections.abc import Callable, Iterator
from typing import Protocol

import numpy as np

from .audio import FREQUENZA

log = logging.getLogger(__name__)

# Frasi che Whisper «inventa» sul silenzio o sul rumore (dall'app desktop)
INVENTATE = {
    "grazie.", "grazie", "grazie a tutti.", "grazie per la visione.",
    "sottotitoli creati dalla comunità amara.org", "sottotitoli a cura di qtss",
    "thank you.", "thank you", "thanks for watching.", "thank you for watching.", "you", "bye.",
    "subtitles by the amara.org community", "...", "",
}
GLOSSARIO_MAX_CARATTERI = 500     # Whisper usa al massimo ~224 token di prompt
PAUSA_PARAGRAFO_S = 2.0
PARAGRAFO_MAX_S = 60.0
CHECKPOINT_OGNI_S = 3.0          # abbastanza spesso per riprendere, senza martellare il database


def inventato(testo: str) -> bool:
    """Solo frasi inventate, anche ripetute (es. «Grazie a tutti. Grazie a tutti.»)."""
    # firme di sottotitolatori sul silenzio, es. «Grazie www.sito.com»: testo breve con un indirizzo web
    if len(testo) < 80 and re.search(r"(www\.|https?:|\.(com|it|org|net)\b)", testo.lower()):
        return True
    frasi = re.findall(r"[^.!?]+[.!?]*", testo.lower())
    return all(re.sub(r"[!?]+$", ".", f.strip()) in INVENTATE for f in frasi)


def termini_glossario(glossario: str) -> str:
    """Termini (uno per riga o separati da virgola, # = commento) in una riga per Whisper."""
    termini = []
    for riga in glossario.splitlines():
        riga = riga.strip()
        if riga and not riga.startswith("#"):
            termini.extend(t.strip() for t in riga.split(",") if t.strip())
    return ", ".join(termini)[:GLOSSARIO_MAX_CARATTERI]


class Motore(Protocol):
    def segmenti(self, audio: np.ndarray, lingua: str | None, glossario: str) -> Iterator[tuple[float, float, str]]:
        """Segmenti (inizio_s, fine_s, testo) relativi all'audio passato, generati man mano."""


class WhisperLocale:
    """faster-whisper int8 su CPU. Il modello si carica una volta e resta in memoria tra i job."""

    def __init__(self, nome_modello: str, thread: int, beam: int):
        self.nome_modello, self.thread, self.beam = nome_modello, thread, beam
        self._modello = None

    def carica(self):
        if self._modello is None:
            from faster_whisper import WhisperModel
            inizio = time.monotonic()
            parametri = {"device": "cpu", "compute_type": "int8", "cpu_threads": self.thread}
            try:
                # di norma il modello è già nel volume: nessuna connessione a Hugging Face
                self._modello = WhisperModel(self.nome_modello, local_files_only=True, **parametri)
            except Exception:  # noqa: BLE001 — primo avvio o modello cambiato: si scarica una volta sola
                log.info("modello %s non presente: lo scarico (una volta sola)", self.nome_modello)
                self._modello = WhisperModel(self.nome_modello, **parametri)
            log.info("modello %s caricato in %.1f s", self.nome_modello, time.monotonic() - inizio)
        return self._modello

    def segmenti(self, audio, lingua, glossario):
        termini = termini_glossario(glossario)
        segmenti, _ = self.carica().transcribe(
            audio, language=lingua, beam_size=self.beam, temperature=0.0,
            vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
            # niente testo precedente come contesto: evita i cicli di frasi ripetute;
            # il glossario va negli hotwords, che a differenza di initial_prompt valgono per ogni finestra
            condition_on_previous_text=False,
            hotwords=f"Termini: {termini}." if termini else None)
        for s in segmenti:            # generatore pigro: il lavoro avviene qui, segmento per segmento
            yield s.start, s.end, s.text.strip()


class Annullato(Exception):
    pass


Segmento = tuple[float, float, str]


def trascrivi(motore: Motore, audio: np.ndarray, lingua: str, glossario: str, gia_fatti: list[Segmento],
              da_secondi: float, salva: Callable[[list[Segmento], float], bool]) -> list[Segmento]:
    """
    Trascrive da `da_secondi` in poi. `gia_fatti` sono i segmenti confermati delle esecuzioni precedenti.
    Ogni CHECKPOINT_OGNI_S chiama `salva(nuovi_segmenti, punto)`, che li registra insieme al punto raggiunto
    (in una sola transazione) e restituisce False se il job è stato annullato.
    Restituisce tutti i segmenti; quelli dopo l'ultimo checkpoint li salva chi chiama, con il testo finale.
    """
    segmenti = list(gia_fatti)
    da_salvare: list[Segmento] = []
    inizio_campione = int(da_secondi * FREQUENZA)
    ultimo_salvataggio = time.monotonic()
    precedente = segmenti[-1][2] if segmenti else ""
    for a, b, testo in motore.segmenti(audio[inizio_campione:], None if lingua == "auto" else lingua, glossario):
        a, b = a + da_secondi, b + da_secondi
        if testo and not inventato(testo) and testo != precedente:   # niente cicli di frasi uguali
            segmenti.append((a, b, testo))
            da_salvare.append((a, b, testo))
            precedente = testo
        if time.monotonic() - ultimo_salvataggio >= CHECKPOINT_OGNI_S:
            if not salva(da_salvare, b):
                raise Annullato()
            da_salvare = []
            ultimo_salvataggio = time.monotonic()
    return segmenti


def _ora(secondi: float) -> str:
    s = int(secondi)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def formatta(segmenti: list[Segmento]) -> str:
    """Testo finale: paragrafi separati dalle pause, ognuno con l'ora d'inizio."""
    paragrafi: list[tuple[float, list[str]]] = []
    fine_prec = None
    for a, b, testo in segmenti:
        nuovo = (not paragrafi or a - fine_prec >= PAUSA_PARAGRAFO_S or a - paragrafi[-1][0] >= PARAGRAFO_MAX_S)
        if nuovo:
            paragrafi.append((a, [testo]))
        else:
            paragrafi[-1][1].append(testo)
        fine_prec = b
    return "".join(f"[{_ora(a)}] {' '.join(testi)}\n\n" for a, testi in paragrafi)
