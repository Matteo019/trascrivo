-- Il testo trascritto e i segmenti in lavorazione stanno nel database, non più in file.
-- Su disco resta solo l'audio, e solo finché il job non è concluso.
ALTER TABLE job ADD COLUMN testo TEXT;

-- segmenti confermati di un job in elaborazione: il checkpoint da cui riprendere dopo un riavvio
CREATE TABLE job_segmenti (
    job_id  UUID NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    inizio  REAL NOT NULL,
    fine    REAL NOT NULL,
    testo   TEXT NOT NULL,
    PRIMARY KEY (job_id, inizio)
);
