"""
Comandi:
  python -m trascrivo web            API e pagine (uvicorn, porta 8000)
  python -m trascrivo worker         coda delle trascrizioni (un job alla volta)
  python -m trascrivo migra          applica le migrazioni del database
  python -m trascrivo crea-admin EMAIL   crea un amministratore (la password si chiede a terminale)
"""

import getpass
import logging
import os
import sys

from . import config, db, repo, sicurezza


def _log():
    logging.basicConfig(level=os.environ.get("LOG_LIVELLO", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # i log di accesso di uvicorn contengono i percorsi (con gli ID dei job): ok, niente contenuti


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("web", "worker", "migra", "crea-admin"):
        print(__doc__)
        return 2
    _log()
    try:
        cfg = config.carica()
    except config.ErroreConfigurazione as exc:
        print(f"Configurazione non valida: {exc}", file=sys.stderr)
        return 1
    db.migra(cfg.database_url)
    comando = argv[1]
    if comando == "migra":
        return 0

    if comando == "crea-admin":
        if len(argv) != 3:
            print("Uso: python -m trascrivo crea-admin EMAIL", file=sys.stderr)
            return 2
        password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password (almeno 10 caratteri): ")
        if len(password) < sicurezza.PASSWORD_MIN:
            print("Password troppo corta.", file=sys.stderr)
            return 1
        pool = db.crea_pool(cfg.database_url, 1)
        repo.crea_utente(pool, argv[2], "", sicurezza.hash_password(password), "admin")
        print("Amministratore creato.")
        return 0

    cfg.crea_cartelle()
    if comando == "worker":
        from . import worker
        worker.main(cfg, db.crea_pool(cfg.database_url, 2))
        return 0

    import uvicorn

    from .app import crea_app
    app = crea_app(cfg, db.crea_pool(cfg.database_url, 10))
    # nel container si ascolta su tutte le interfacce della rete Docker interna; nessuna porta è
    # pubblicata sull'host (vedi docker-compose.yml): da fuori si arriva solo dal tunnel
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORTA", "8000")),
                proxy_headers=False, server_header=False, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
