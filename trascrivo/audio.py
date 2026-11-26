"""
Controllo e conversione dei file audio con ffprobe/ffmpeg.
Il tipo si verifica sul contenuto (formato del contenitore), non sull'estensione.
"""

import json
import subprocess
from pathlib import Path

import numpy as np

FREQUENZA = 16000
# nomi dei contenitori secondo ffprobe: opus/ogg, mp3, wav, m4a, webm
FORMATI_AMMESSI = {"ogg", "mp3", "wav", "mov,mp4,m4a,3gp,3g2,mj2", "matroska,webm"}


class AudioNonValido(ValueError):
    pass


def analizza(percorso: Path) -> float:
    """Durata in secondi; AudioNonValido se non è un audio in un formato ammesso."""
    try:
        uscita = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(percorso)],
            capture_output=True, timeout=60, check=True).stdout
        info = json.loads(uscita)
    except (subprocess.SubprocessError, json.JSONDecodeError):
        raise AudioNonValido("Il file non è un audio leggibile.") from None
    formato = info.get("format", {})
    if formato.get("format_name") not in FORMATI_AMMESSI:
        raise AudioNonValido("Formato non supportato: usa opus/ogg, mp3, wav, m4a o webm.")
    if not any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
        raise AudioNonValido("Il file non contiene audio.")
    try:
        durata = float(formato.get("duration") or 0)
    except ValueError:
        durata = 0.0
    if durata <= 0:
        # i webm del browser spesso non hanno la durata nell'intestazione: la si misura decodificando
        durata = _durata_decodificando(percorso)
    if durata <= 0:
        raise AudioNonValido("Il file audio è vuoto.")
    return durata


def _durata_decodificando(percorso: Path) -> float:
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(percorso), "-vn", "-ac", "1",
                            "-ar", "8000", "-f", "s16le", "-"], capture_output=True, timeout=600, check=True)
    except subprocess.SubprocessError:
        raise AudioNonValido("Il file non è un audio leggibile.") from None
    return len(r.stdout) / 2 / 8000


def converti(sorgente: Path, destinazione: Path, normalizza: bool):
    """Qualsiasi formato ammesso -> WAV PCM 16 bit, mono, 16 kHz (il formato che usa Whisper)."""
    filtri = ["-af", "dynaudnorm=f=250:g=15"] if normalizza else []   # voce lontana dal microfono alzata
    comando = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(sorgente), "-vn", *filtri,
               "-ac", "1", "-ar", str(FREQUENZA), "-c:a", "pcm_s16le", str(destinazione)]
    try:
        subprocess.run(comando, capture_output=True, timeout=3600, check=True)
    except subprocess.SubprocessError:
        destinazione.unlink(missing_ok=True)
        raise AudioNonValido("Conversione dell'audio non riuscita.") from None


def leggi_wav(percorso: Path) -> np.ndarray:
    """WAV convertito -> float32 in [-1, 1] (16 kHz mono, ~4 MB al minuto in memoria)."""
    import wave
    with wave.open(str(percorso), "rb") as wf:
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0
