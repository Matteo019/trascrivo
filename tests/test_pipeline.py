from trascrivo import pipeline


def test_frasi_inventate():
    assert pipeline.inventato("Grazie a tutti. Grazie a tutti.")
    assert pipeline.inventato("Sottotitoli a cura di QTSS")
    assert pipeline.inventato("Grazie www.sito.com")
    assert not pipeline.inventato("Grazie, passiamo al secondo teorema.")


def test_glossario():
    g = "# commento\nderivata, integrale\n\nlimite\n"
    assert pipeline.termini_glossario(g) == "derivata, integrale, limite"
    assert len(pipeline.termini_glossario("x" * 2000)) == pipeline.GLOSSARIO_MAX_CARATTERI


def test_formatta_paragrafi():
    seg = [(0, 4, "Buongiorno."), (4.5, 8, "Oggi limiti."), (12, 15, "Nuovo argomento."), (3700, 3702, "Fine.")]
    testo = pipeline.formatta(seg)
    assert testo == ("[00:00:00] Buongiorno. Oggi limiti.\n\n[00:00:12] Nuovo argomento.\n\n"
                     "[01:01:40] Fine.\n\n")


def test_nessuna_dipendenza_esterna():
    """Né codice né dipendenze verso servizi esterni di trascrizione o AI."""
    from pathlib import Path
    radice = Path(__file__).resolve().parent.parent
    vietati = ("groq", "openai", "anthropic", "nvidia", "gemini", "mistral", "deepseek", "openrouter",
               "wikipedia", "speaches")
    requisiti = (radice / "requirements.txt").read_text().lower()
    codice = "\n".join(p.read_text(encoding="utf-8").lower()
                       for d in ("trascrivo", "web") for p in (radice / d).rglob("*") if p.suffix in (".py", ".js", ".html"))
    for nome in vietati:
        assert nome not in requisiti, nome
        assert nome not in codice, nome
