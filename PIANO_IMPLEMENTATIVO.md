# Piano implementativo — trascrivo (servizio web)

> Stato (26/11/2026): **Fasi 1–4 implementate**. Restano a mano: route su Cloudflare, `COOKIE_SICURO=1`,
> benchmark sul server, revisione delle bozze legali, nuova grafica. Dettaglio nella tabella «Stato» del README.
> Repository `trascrivo`, copia separata di `trascrizione` (l'app desktop non si tocca).
> Dominio: `trascrizioni.matteobertotti.tech`.

## 1. Obiettivo

Servizio web di **sola trascrizione in differita**: login su invito → carica un file o registra dal
microfono del browser → il job entra in coda con priorità → un worker lo trascrive in locale con
faster-whisper → l'utente scarica il `.txt`. Audio e testo non escono mai dal server.

## 2. Situazione di partenza (esplorazione)

| Area | Oggi |
|---|---|
| Backend | FastAPI + uvicorn, `src/webapp.py` (674 righe), su 127.0.0.1:8765 |
| Frontend | SPA in JS semplice: `web/app.js` (~2000 righe), `style.css`, `effetti.js` |
| Dati | file in `progetti/<slug>/` + `info.json`, nessun database |
| Trascrizione | `src/trascrittore.py` processo separato; blocchi tagliati nelle pause (RMS); `motori.WhisperPC` = faster-whisper int8 + VAD + `initial_prompt` dal glossario; blocchi falliti in `da_ritrascrivere/` |
| Da rimuovere | `riassunto.py`, `chat.py` (anche Wikipedia), `chiamata.py`, `export_pdf.py`, provider/chiavi in `motori.py`, cattura audio in `comune.py`, `terminale/`, `.bat`, `config/contesti/`, `config/contesto.txt`, `PIANO_IMPLEMENTATIVO_CHAT.md` |
| Segreti in git | **nessuno trovato** (1 commit; cercate chiavi Groq/OpenAI/NVIDIA/Anthropic/Google e assegnazioni token/password/secret; niente `.env`, db o audio committati) |

## 3. Decisioni

Già prese da te (non si discutono): niente riassunti/chat/provider esterni, niente tempo reale né
audio di sistema, accesso solo su invito con ruoli `admin`/`user`, coda con priorità admin,
interfaccia in italiano, **PostgreSQL**, **Docker Compose** sul server di casa (Portainer,
Cloudflare Tunnel, runner GitHub self-hosted).

Mie proposte — **da confermare** (✅ = default che applico se approvi il piano):

| # | Decisione | Proposta |
|---|---|---|
| D1 | Motore | ✅ solo faster-whisper nel worker; niente Speaches (si può aggiungere dopo) |
| D2 | Recupero errori | ✅ **checkpoint** al posto di `da_ritrascrivere/`: segmenti scritti man mano, un job interrotto riparte dall'ultimo secondo trascritto (cambia la tua indicazione "riusa i blocchi": stesso scopo, meccanismo adatto ai file) |
| D3 | Frontend | ✅ pagine nuove e piccole (vanilla JS, riuso `style.css`), `app.js` eliminato |
| D4 | Admin e contenuti | ✅ l'admin vede solo metadati/stato, mai il testo; per assistenza l'utente gli manda il file |
| D5 | Postgres | ✅ container dedicato nello stesso compose (rete interna, nessuna porta esposta), non condiviso con MySite |
| D6 | Accesso DB | ✅ `psycopg` 3 + pool, SQL scritto a mano, migrazioni in file `.sql` numerati con un runner minimo (niente ORM né Alembic) |
| D7 | Rete | ❓ **da te**: `cloudflared` gira in container (quale rete Docker?) o sull'host? Container → stessa rete, nessun `ports:`; host → solo `127.0.0.1:8000:8000` |

## 4. Architettura

```
Browser ──HTTPS──> Cloudflare Tunnel ──> [web]  FastAPI (uvicorn, 1 processo)
                                           │  sessioni, CSRF, upload, pagine, API
                                           ▼
                                        [db]   PostgreSQL 17 (volume pgdata)
                                           ▲
                                        [worker] processo Python con Whisper caricato una volta
                                           │  prende i job con SELECT … FOR UPDATE SKIP LOCKED
                                           ▼
                                        volume data/  (audio temporaneo, testi)
```

- **Web e worker separati** (due servizi dello stesso compose, stessa immagine, comando diverso):
  il web resta reattivo mentre Whisper usa la CPU; il worker si riavvia da solo senza toccare il web.
- **Coda in Postgres**: `ORDER BY priorita DESC, creato_il ASC`, lock con `SKIP LOCKED`;
  `LISTEN/NOTIFY` per svegliare il worker subito (con polling di riserva ogni 5 s).
- **Annullamento di un job in corso**: il web marca `annullato`; il worker lo controlla tra un
  segmento e l'altro (faster-whisper genera i segmenti in modo pigro) e si ferma.
- **Riavvio**: all'avvio il worker rimette `in_coda` i job `in_elaborazione` (e riparte dal checkpoint).

### Schema dati (prima migrazione)

- `utenti`: id, email, nome, hash_password (argon2id), ruolo, attivo, limiti (durata max, job/giorno, MB max; NULL = default da `.env`), creato_il
- `inviti`: token (hash), email, ruolo, scade_il, usato_il, creato_da
- `sessioni`: id (hash del token), utente_id, csrf, scade_il, ultimo_uso — revocabili, cancellate alla disattivazione utente
- `progetti`: id, proprietario_id, nome, glossario, lingua_default
- `job`: id (UUID, non indovinabile), proprietario_id, progetto_id, stato (`in_coda`, `in_elaborazione`, `completato`, `errore`, `annullato`), priorita, lingua, modello, durata_audio_s, avanzamento_s, errore (messaggio breve, mai contenuti), creato_il, iniziato_il, finito_il, testo_scade_il
- `tentativi_login`: per il rate limiting (ip/email, istante) — persistente anche dopo un riavvio

- `job.testo` e `job_segmenti` (migrazione 002, richiesta del 26/11/2026): **tutti i dati stanno in PostgreSQL**,
  compreso il testo trascritto e i segmenti del checkpoint. Su disco resta solo l'audio, e solo finché il job
  non è concluso. Un solo backup (`pg_dump`), nessun file orfano.

## 5. Ottimizzazioni (CPU, 8 GB, nessuna GPU)

1. **Modello caricato una volta** nel worker e riusato tra i job (niente 10–20 s di caricamento a file).
2. **ffmpeg → WAV 16 kHz mono** direttamente (oggi: 48 kHz stereo e poi giù di nuovo).
3. **File intero con VAD di faster-whisper** invece di una chiamata per blocco: più veloce, contesto continuo, meno errori ai bordi; con checkpoint (D2).
4. **Parametri da `.env`**: `WHISPER_MODELLO`, `WHISPER_THREADS` (default: core fisici), `WHISPER_BEAM` (1 veloce / 5 qualità), `WHISPER_BATCHED` (pipeline batched, solo se il benchmark dice che conviene).
5. **Priorità bassa** (`nice`) e limiti `cpus`/`mem_limit` nel compose: Jellyfin e MySite restano reattivi.
6. **Benchmark esteso** (`strumenti/benchmark_whisper.py`): `small`, `medium`, `large-v3-turbo` × beam 1/5 × batched sì/no → RTF, RAM di picco, testo per confronto a occhio. Si lancia nel container sul server.
7. **Avanzamento in %** e stima dell'attesa in coda (somma delle durate davanti × RTF misurato).
8. **Immagine leggera**: via groq, openai, PyAudioWPatch, sounddevice, markdown, xhtml2pdf.

## 6. Pulizia del codice

- Elimino i moduli e i file elencati al punto 2; nessun codice morto né flag "disattivato".
- `comune.py` ridotto (via PID, dispositivi, `.env` scritto dalla dashboard): la configurazione si legge solo dall'ambiente in un modulo `config.py` tipizzato, validato all'avvio (errore chiaro se manca qualcosa).
- `webapp.py` diviso per responsabilità: `app.py` (creazione app, middleware), `auth.py`, `api_job.py`, `api_admin.py`, `pagine.py`.
- Logica della trascrizione in `pipeline.py` (pura: audio in → segmenti), separata da `worker.py` (coda, stati, file). Riusati: filtro delle frasi inventate (`inventato`), costruzione del prompt dal glossario, normalizzazione.
- Accesso al DB solo da `db.py` / `repo_*.py`: ogni query sui job filtra per proprietario in **un unico punto**.
- Nomi in italiano coerenti con il resto; type hints; `ruff` per formattazione e lint (solo dev).
- `requirements.txt` con versioni fissate; `requirements-dev.txt` per test e lint.

## 7. Fasi

Ogni fase: commit piccoli in italiano, poi resoconto e istruzioni per provarla.

### Fase 1 — Pulizia, Postgres, coda e trascrizione
- Rimozioni e ristrutturazione (punto 6).
- `docker-compose.yml` minimo (web, worker, db) per sviluppare da subito contro Postgres.
- Migrazioni, schema, worker con priorità/checkpoint/annullamento/ripresa dopo riavvio.
- Upload in streaming con limite di dimensione; validazione del contenuto con `ffprobe` (non solo estensione); formati: opus/ogg, mp3, wav, m4a, webm.
- Registrazione dal microfono (`MediaRecorder`, opus) con upload a fine registrazione.
- Glossario del progetto come `initial_prompt`.
- Pagine: elenco job con stato e avanzamento, carica/registra, download `.txt`, cancella.
- Utente admin creato da comando (`python -m trascrivo crea-admin`), login semplice provvisorio.

### Fase 2 — Utenti e sicurezza
- Login con sessioni server-side, argon2id, CSRF (token per sessione in header), rate limiting sul login (per IP e per email, con attesa crescente).
- Cookie `__Host-` con `Secure`, `HttpOnly`, `SameSite=Strict`; intestazioni: CSP senza inline, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy` (microfono solo stessa origine), `frame-ancestors 'none'`.
- IP reale letto da `CF-Connecting-IP` **solo** se la richiesta arriva dal tunnel (proxy fidato configurato).
- Isolamento: un job di un altro utente → **404** (non 403), su ogni endpoint; test con ID indovinati.
- Pannello admin: inviti (link monouso con scadenza), disattiva utente, coda, cambia priorità, cancella job.
- Limiti per utente: durata massima, job al giorno, dimensione file.

### Fase 3 — Privacy e conservazione
- Audio cancellato a fine job (completato, errore definitivo o annullato).
- Testo cancellato dopo `CONSERVAZIONE_GIORNI` (default 7) e subito con un pulsante; pulizia oraria delle righe nel database e dei file audio orfani.
- Log solo con ID: niente testo, niente nomi di file originali, niente email nei log del worker.
- Nessuna telemetria: `HF_HUB_DISABLE_TELEMETRY=1`; il modello si scarica una volta nel volume, poi `HF_HUB_OFFLINE=1`.
- Avviso esatto vicino al download + avviso sugli errori della trascrizione automatica.
- `docs/informativa-privacy.md` e `docs/termini-uso.md`: **bozze da far controllare da una persona competente**, senza affermare la conformità al GDPR.

### Fase 4 — Deploy e documentazione
- `Dockerfile` (python slim + ffmpeg, utente non root, healthcheck `/salute`), compose completo: volumi `data`, `modelli`, `pgdata`; limiti di risorse; restart policy.
- `.github/workflows/deploy.yml` sul runner self-hosted: **solo push su `main`**, mai PR (repository pubblico = codice di estranei sul server); build, migrazioni, `up -d`, verifica `/salute`.
- Backup: `pg_dump` giornaliero nel volume (opzionale, i testi scadono comunque).
- `.env.example` completo e senza segreti; `.env`, `data/` e dump in `.gitignore`.
- `README.md` in italiano: cosa fa, differenze dall'app desktop, installazione, configurazione, limiti noti, tabella "stato".

## 8. Test

`pytest` contro un Postgres vero (servizio del compose, database di test creato e distrutto a ogni
sessione); Whisper finto nei test, così non serve caricare modelli.

- ordine della coda: priorità admin prima, poi ordine di arrivo
- cancellazione di un job altrui da parte dell'admin; utente che non può cancellare quelli altrui
- isolamento: lettura, download, cancellazione con ID di un altro utente → 404 su ogni endpoint
- pulizia automatica di testi scaduti e file orfani
- riavvio con job `in_elaborazione` → torna `in_coda` e riprende dal checkpoint
- audio assente dal disco dopo la trascrizione
- log senza testo trascritto (test che cerca una frase nota nei log)
- nessuna dipendenza/chiamata esterna (controllo su import e `requirements.txt`)

## 9. Dipendenze

| | |
|---|---|
| Aggiunte | `psycopg[binary]`, `psycopg-pool`, `argon2-cffi`, `python-multipart` (upload form); dev: `pytest`, `httpx`, `ruff` |
| Tenute | `fastapi`, `uvicorn`, `faster-whisper`, `numpy` |
| Tolte | `groq`, `openai`, `PyAudioWPatch`, `sounddevice`, `soundfile`, `markdown`, `xhtml2pdf`, `av` (decodifica la fa `ffmpeg`) |
| Sistema | `ffmpeg` nell'immagine; Postgres 17 in container |

## 10. Rischi

- **Upload da 100 MB massimo con Cloudflare Tunnel** (piano gratuito): default `MAX_MB=100`; un'ora in opus/mp3 ci sta, un WAV lungo no. Upload a pezzi come possibile miglioramento.
- **RAM**: Postgres ~100–200 MB, `medium` int8 ~2–3 GB, `large-v3` al limite con Jellyfin attivo.
- **Tempi su CPU**: `medium` vicino al tempo reale; un file lungo occupa la coda a lungo. I numeri veri li dà il benchmark sul server.
- **Runner self-hosted con repo pubblico**: mitigato con trigger solo su `main`.

## 11. Criteri di accettazione

- un utente invitato fa login, carica un audio, vede il job avanzare e scarica il `.txt`;
- il job di un admin supera quelli già in coda dei normali utenti;
- un utente non accede ai dati di un altro (verificato da test);
- dopo la trascrizione l'audio non è più sul disco;
- nessuna chiamata a servizi esterni per trascrizione o riassunto, né nel codice né nelle dipendenze;
- `grep` nei log non trova testo trascritto;
- il benchmark gira sul server e riporta tempi e RAM per `small` e `medium`.

## 12. Promemoria

- [ ] **Rifare la grafica della pagina principale** (richiesto da Matteo il 26/11/2026 dopo la prima prova sul server):
  oggi funziona ma è essenziale. Da decidere insieme cosa migliorare (layout, intestazione, elenco dei job,
  area di caricamento) prima di toccarla.
