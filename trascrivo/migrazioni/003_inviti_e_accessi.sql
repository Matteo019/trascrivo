-- Inviti: l'unico modo per creare un account oltre al comando crea-admin.
CREATE TABLE inviti (
    -- impronta SHA-256 del token del link: chi legge il database non può usare gli inviti
    id              TEXT PRIMARY KEY,
    email           TEXT NOT NULL,
    ruolo           TEXT NOT NULL DEFAULT 'user' CHECK (ruolo IN ('admin', 'user')),
    creato_da       BIGINT REFERENCES utenti(id) ON DELETE SET NULL,
    creato_il       TIMESTAMPTZ NOT NULL DEFAULT now(),
    scade_il        TIMESTAMPTZ NOT NULL,
    usato_il        TIMESTAMPTZ
);

-- Tentativi di accesso falliti, per il rate limiting (sopravvive ai riavvii).
-- Solo impronte: niente email o IP in chiaro.
CREATE TABLE tentativi_accesso (
    chiave      TEXT NOT NULL,          -- 'ip:<impronta>' oppure 'email:<impronta>'
    istante     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX tentativi_accesso_chiave ON tentativi_accesso(chiave, istante);
