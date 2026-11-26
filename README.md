# Trascrizioni — servizio web (trascrivo)

Servizio web di **sola trascrizione in differita**, ospitato su un server di casa. Si accede su invito,
si carica un file audio (o si registra dal microfono del browser), il file entra in una coda, un worker lo
trascrive **in locale** con Whisper e si scarica il `.txt`.

**Audio e testo non vanno mai a servizi di trascrizione o di AI di terzi.** L'audio viene cancellato appena
la trascrizione è finita; il testo dopo un numero di giorni configurabile (7 di default) o subito, con un
pulsante.

Progetto personale, licenza MIT, fornito così com'è.

## Differenze rispetto all'app desktop

Questo repository è una copia separata dell'app desktop `trascrizione`, che resta com'è.

| | App desktop | Questo servizio |
|---|---|---|
| Dove gira | PC Windows, dashboard locale | Server Linux, Docker, dal browser ovunque |
| Trascrizione | in tempo reale (microfono, audio del PC) | in differita: file caricati o registrati dal browser |
| Motore | Groq, OpenAI, server locale o Whisper sul PC | **solo Whisper locale** (faster-whisper, int8, CPU) |
| Riassunti, chat, Wikipedia | sì, con provider AI esterni | **no** |
| Utenti | uno | più utenti su invito, ruoli `admin` e `user` |
| Dati | file in `progetti/` | PostgreSQL (testo compreso); audio solo temporaneo |

## Come funziona

```
Browser ─HTTPS─> Cloudflare Tunnel ─> web (FastAPI) ──> PostgreSQL <── worker (faster-whisper)
                                        │                                  │
                                        └──── volume dati: audio temporaneo ┘
```

- **web**: pagine e API, login con sessioni, upload in streaming con limiti, pannello admin.
- **worker**: un job alla volta (prima quelli dell'admin, poi in ordine di arrivo). Converte l'audio con
  ffmpeg, lo trascrive con il VAD attivo e salva un checkpoint ogni pochi secondi: dopo un riavvio
  riprende da dove era. Ogni ora cancella i dati scaduti e gli eventuali file audio rimasti.
- **db**: PostgreSQL 17 con utenti, sessioni, inviti, progetti e glossari, job e testi.
- Nessuna porta è aperta sull'host: si arriva al servizio solo dal tunnel di Cloudflare.

## Installazione sul server

Requisiti: Docker con Compose, un Cloudflare Tunnel già attivo in un container (`cloudflared-tunnel`).

```bash
git clone https://github.com/Matteo019/trascrivo.git ~/trascrivo && cd ~/trascrivo
cp .env.example .env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 32)/" .env
docker compose up -d --build
sh strumenti/collega_tunnel.sh                        # collega cloudflared alla rete del servizio web
docker compose exec web python -m trascrivo crea-admin tua@email.it
```

Poi nella dashboard di Cloudflare (Zero Trust → Networks → Tunnels → il tuo tunnel → *Published
application*): sottodominio a scelta, **Service URL `http://trascrivo-web:8000`**, path vuoto.

Al primo avvio il worker scarica il modello (circa 0,5 GB per `small`) nel volume `modelli`; dopo non si
collega più a internet.

### Aggiornamenti automatici

`.github/workflows/deploy.yml` a ogni push su `main`:

1. esegue test e lint su GitHub, con un PostgreSQL di prova;
2. sul **runner self-hosted** del server ricostruisce e riavvia lo stack, ricollega il tunnel e aspetta che
   il servizio risponda.

Il deploy legge i segreti da `~/trascrivo/.env` sul server (o dal percorso nella variabile di repository
`TRASCRIVO_ENV_FILE`): il `.env` non va mai su GitHub. Il workflow non parte con le pull request, perché
il runner esegue codice sul server. Il runner deve essere registrato per questo repository.

## Configurazione (`.env`)

Tutte le impostazioni sono in [.env.example](.env.example). Le principali:

| Variabile | Default | |
|---|---|---|
| `POSTGRES_PASSWORD` | — | obbligatoria, solo lettere e numeri (`openssl rand -hex 32`) |
| `WHISPER_MODELLO` | `small` | `small`, `medium`, `large-v3-turbo`, `large-v3` |
| `WHISPER_LINGUA` | `it` | `it`, `en`, `auto` |
| `WHISPER_THREAD` / `WHISPER_BEAM` | metà dei core / `1` | velocità contro precisione |
| `LIMITE_MB` / `LIMITE_MINUTI` / `LIMITE_JOB_GIORNO` | 100 / 180 / 20 | limiti predefiniti per utente; l'admin può cambiarli per persona |
| `CONSERVAZIONE_GIORNI` | 7 | dopo quanti giorni si cancella il testo |
| `COOKIE_SICURO` | 1 | **1 in produzione** (HTTPS); 0 solo per provare in locale su `http://` |
| `WORKER_RAM` / `WORKER_CPU` | 4g / 3 | limiti del worker, per lasciare risorse agli altri servizi |

Dopo una modifica: `docker compose up -d`.

### Scegliere il modello

```bash
docker compose run --rm -v "$PWD/prova.mp3:/tmp/prova.mp3:ro" worker \
    python strumenti/benchmark_whisper.py /tmp/prova.mp3 --modelli small medium --beam 1 5
```

Stampa tempo, RTF (tempo di calcolo diviso durata dell'audio) e RAM di picco per ogni combinazione.

## Uso

- **Utente**: carica un file (opus/ogg, mp3, wav, m4a, webm) o registra dal microfono, scegli lingua e
  progetto (il glossario del progetto aiuta Whisper con i termini tecnici), segui la coda e scarica il
  `.txt`. Il pulsante *Elimina* cancella subito testo e dati del job.
- **Admin** (pulsante *Amministrazione*): crea link di invito monouso (da mandare alla persona: il servizio
  non invia email), disattiva utenti, imposta limiti per utente, vede la coda di tutti con i soli
  metadati, cambia le priorità, annulla i job. L'admin **non vede il testo** delle trascrizioni altrui.

## Sicurezza e privacy

- Password con Argon2id; sessioni salvate nel database come impronte; cookie `__Host-` `Secure`,
  `HttpOnly`, `SameSite=Strict`; token CSRF su ogni modifica.
- Limite ai tentativi di accesso falliti: 5 per email e 20 per IP ogni 15 minuti.
- Ogni richiesta su job o progetti di un altro utente risponde 404, anche con ID inventati (verificato dai
  test).
- Intestazioni: CSP senza risorse esterne né script inline, niente iframe, `no-referrer`, HSTS.
- Log solo con ID dei job: niente testo, nomi di file, email o IP.
- Nessuna telemetria; il modello si carica dal volume senza connettersi a Hugging Face.
- Bozze di [informativa privacy](docs/informativa-privacy.md) e [termini d'uso](docs/termini-uso.md):
  **da far controllare a una persona competente** prima della pubblicazione.

## Limiti noti

- **Una trascrizione alla volta**: con la CPU del server un file lungo occupa la coda a lungo; gli altri
  aspettano (vedono la loro posizione).
- **Upload limitato dalla banda di casa** e dal limite di **100 MB per richiesta** di Cloudflare Tunnel
  (piano gratuito): un'ora in opus o mp3 ci sta, un WAV lungo no.
- Nessuna email: gli inviti si mandano a mano.
- Nessun backup automatico del database (i testi scadono comunque). Se serve:
  `docker exec trascrivo-db pg_dump -U trascrivo trascrivo | gzip > backup.sql.gz`

## Sviluppo e test

```bash
pip install -r requirements-dev.txt
DATABASE_URL_TEST=postgresql://utente:password@localhost:5432/postgres pytest -q
ruff check trascrivo tests strumenti
```

I test usano un PostgreSQL vero (database temporaneo creato e distrutto) e un Whisper finto. Coprono:
priorità della coda, annullamento da admin, isolamento tra utenti, ripresa dopo un riavvio, cancellazione
dell'audio, pulizia dei dati scaduti, log senza testo, rate limiting, inviti, permessi admin, intestazioni.

## Stato

| | Stato |
|---|---|
| Rimozione di riassunti, chat, Wikipedia, provider cloud, tempo reale, audio di sistema | ✅ fatto |
| PostgreSQL: utenti, progetti, job, testi, checkpoint | ✅ fatto |
| Coda con priorità admin, un worker, ripresa dopo un riavvio | ✅ fatto |
| Upload con controllo del contenuto, registrazione dal microfono, glossario | ✅ fatto |
| Login, sessioni, CSRF, rate limiting, intestazioni di sicurezza | ✅ fatto |
| Inviti, pannello admin, limiti per utente, cambio password | ✅ fatto |
| Cancellazione dell'audio, scadenza del testo, pulizia automatica, log senza contenuti | ✅ fatto |
| Avvisi in pagina, bozze di informativa e termini | ✅ fatto (bozze da far controllare) |
| Docker, deploy automatico dal runner, collegamento al tunnel | ✅ fatto |
| Benchmark `small`/`medium` sul server | ⏳ da lanciare sul server |
| Route nella dashboard di Cloudflare, `COOKIE_SICURO=1` | ⏳ da fare a mano (vedi Installazione) |
| Grafica della pagina principale | ⏳ da rifare, da decidere insieme |
| Upload a pezzi per file oltre 100 MB | 💡 possibile miglioramento |
| Backup automatico del database | 💡 possibile miglioramento |
