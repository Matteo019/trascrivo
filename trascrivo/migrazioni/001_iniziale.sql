CREATE TABLE utenti (
    id              BIGSERIAL PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    nome            TEXT NOT NULL DEFAULT '',
    hash_password   TEXT NOT NULL,
    ruolo           TEXT NOT NULL CHECK (ruolo IN ('admin', 'user')),
    attivo          BOOLEAN NOT NULL DEFAULT TRUE,
    -- NULL = limite predefinito dal .env
    max_mb          INTEGER,
    max_minuti      INTEGER,
    max_job_giorno  INTEGER,
    creato_il       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sessioni (
    -- hash SHA-256 del token del cookie: chi legge il database non può usare le sessioni
    id          TEXT PRIMARY KEY,
    utente_id   BIGINT NOT NULL REFERENCES utenti(id) ON DELETE CASCADE,
    csrf        TEXT NOT NULL,
    creata_il   TIMESTAMPTZ NOT NULL DEFAULT now(),
    scade_il    TIMESTAMPTZ NOT NULL
);
CREATE INDEX sessioni_utente ON sessioni(utente_id);

CREATE TABLE progetti (
    id              BIGSERIAL PRIMARY KEY,
    proprietario_id BIGINT NOT NULL REFERENCES utenti(id) ON DELETE CASCADE,
    nome            TEXT NOT NULL,
    glossario       TEXT NOT NULL DEFAULT '',
    creato_il       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (proprietario_id, nome)
);

CREATE TABLE job (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proprietario_id BIGINT NOT NULL REFERENCES utenti(id) ON DELETE CASCADE,
    progetto_id     BIGINT REFERENCES progetti(id) ON DELETE SET NULL,
    stato           TEXT NOT NULL DEFAULT 'in_coda'
                    CHECK (stato IN ('in_coda', 'in_elaborazione', 'completato', 'errore', 'annullato')),
    priorita        INTEGER NOT NULL DEFAULT 0,
    lingua          TEXT NOT NULL,
    modello         TEXT NOT NULL,
    durata_s        REAL,
    avanzamento_s   REAL NOT NULL DEFAULT 0,
    errore          TEXT,
    creato_il       TIMESTAMPTZ NOT NULL DEFAULT now(),
    iniziato_il     TIMESTAMPTZ,
    finito_il       TIMESTAMPTZ,
    testo_scade_il  TIMESTAMPTZ
);
CREATE INDEX job_coda ON job(priorita DESC, creato_il) WHERE stato = 'in_coda';
CREATE INDEX job_proprietario ON job(proprietario_id, creato_il DESC);
