"""
Benchmark di faster-whisper (int8 su CPU) sul server, per scegliere modello e parametri del .env.

Per ogni combinazione di modello, beam e pipeline (normale / batched) trascrive lo stesso spezzone di audio
reale con gli stessi parametri del worker (VAD attivo) e misura:
  - caricamento del modello
  - tempo di trascrizione e RTF = tempo / durata audio (sotto 1 = più veloce del tempo reale)
  - RAM di picco del processo
e stampa l'inizio del testo, per giudicare la qualità a occhio.

Uso nel container (il modello si scarica nel volume la prima volta):
  docker compose run --rm -v "$PWD/prova.mp3:/tmp/prova.mp3:ro" worker \\
      python strumenti/benchmark_whisper.py /tmp/prova.mp3 --modelli small medium

Opzioni utili: --secondi 120 (lunghezza dello spezzone), --beam 1 5, --batched, --thread 3.
L'audio non esce dal server e il testo stampato resta nel terminale.
"""

import argparse
import os
import subprocess
import sys
import threading
import time

import numpy as np
import psutil
from faster_whisper import BatchedInferencePipeline, WhisperModel

FREQUENZA = 16000


def carica_audio(percorso: str, secondi: int) -> np.ndarray:
    """Qualsiasi formato letto da ffmpeg -> float32 mono 16 kHz, primi `secondi` secondi."""
    try:
        pcm = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", percorso, "-t", str(secondi), "-vn",
                              "-ac", "1", "-ar", str(FREQUENZA), "-f", "s16le", "-"],
                             capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Impossibile leggere l'audio con ffmpeg: {exc}")
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


class PiccoRam:
    """Campiona la RAM (RSS) del processo ogni 50 ms e tiene il massimo."""

    def __init__(self):
        self.proc = psutil.Process(os.getpid())
        self.picco = 0
        self._stop = threading.Event()

    def __enter__(self):
        self.picco = self.proc.memory_info().rss
        self._t = threading.Thread(target=self._giro, daemon=True)
        self._t.start()
        return self

    def _giro(self):
        while not self._stop.wait(0.05):
            self.picco = max(self.picco, self.proc.memory_info().rss)

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()
        self.picco = max(self.picco, self.proc.memory_info().rss)


def trascrivi(modello, audio: np.ndarray, beam: int, batched: bool, lingua: str) -> str:
    parametri = dict(language=lingua, beam_size=beam, temperature=0.0, vad_filter=True,
                     vad_parameters={"min_silence_duration_ms": 500}, condition_on_previous_text=False)
    if batched:
        segmenti, _ = BatchedInferencePipeline(model=modello).transcribe(audio, batch_size=8, **parametri)
    else:
        segmenti, _ = modello.transcribe(audio, **parametri)
    return " ".join(s.text.strip() for s in segmenti)      # transcribe è pigro: il lavoro avviene qui


def mb(byte: int) -> str:
    return f"{byte / 2**20:,.0f} MB"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audio", help="file audio con parlato reale (mp3, m4a, wav, ogg...)")
    ap.add_argument("--modelli", nargs="+", default=["small", "medium"])
    ap.add_argument("--beam", nargs="+", type=int, default=[1])
    ap.add_argument("--batched", action="store_true", help="prova anche la pipeline batched")
    ap.add_argument("--secondi", type=int, default=120, help="durata dello spezzone da trascrivere")
    ap.add_argument("--thread", type=int, default=int(os.environ.get("WHISPER_THREAD") or 0)
                    or max(1, psutil.cpu_count(logical=False) or 2))
    ap.add_argument("--lingua", default="it")
    args = ap.parse_args()

    audio = carica_audio(args.audio, args.secondi)
    durata = len(audio) / FREQUENZA
    if durata < 10:
        sys.exit(f"Audio troppo corto ({durata:.1f} s): servono almeno 10 s di parlato.")
    vm = psutil.virtual_memory()
    print(f"CPU: {psutil.cpu_count(logical=False)} core fisici / {psutil.cpu_count()} logici, thread: {args.thread}")
    print(f"RAM: {mb(vm.total)} totali, {mb(vm.available)} libere · audio: {durata:.0f} s\n")

    risultati = []
    for nome in args.modelli:
        base = psutil.Process(os.getpid()).memory_info().rss
        with PiccoRam() as ram:
            t0 = time.perf_counter()
            modello = WhisperModel(nome, device="cpu", compute_type="int8", cpu_threads=args.thread)
            caricamento = time.perf_counter() - t0
            for beam in args.beam:
                for batched in ([False, True] if args.batched else [False]):
                    t0 = time.perf_counter()
                    testo = trascrivi(modello, audio, beam, batched, args.lingua)
                    tempo = time.perf_counter() - t0
                    etichetta = f"{nome} beam {beam}{' batched' if batched else ''}"
                    risultati.append([etichetta, tempo, tempo / durata, 0, caricamento])
                    print(f"=== {etichetta}: {tempo:.1f} s (RTF {tempo / durata:.2f})")
                    print(f"    «{testo[:300]}{'…' if len(testo) > 300 else ''}»\n")
        for r in risultati:
            if r[0].startswith(nome + " ") and r[3] == 0:
                r[3] = ram.picco - base
        del modello

    print("Riepilogo (RTF sotto 1 = più veloce del tempo reale)")
    print(f"{'configurazione':<28}{'tempo':>9}{'RTF':>7}{'RAM':>10}{'caric.':>9}  esito")
    for etichetta, tempo, rtf, ram, caricamento in risultati:
        esito = "ottimo" if rtf < 0.5 else "ok" if rtf < 1 else "più lento del tempo reale"
        print(f"{etichetta:<28}{tempo:>8.1f}s{rtf:>7.2f}{mb(ram):>10}{caricamento:>8.1f}s  {esito}")
    print("\nUn'ora di audio richiede circa RTF × 60 minuti. Scegli nel .env: WHISPER_MODELLO, WHISPER_BEAM.")


if __name__ == "__main__":
    main()
