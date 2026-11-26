#!/bin/sh
# Collega il container di Cloudflare Tunnel alla rete dove c'è solo il servizio web di trascrivo.
# Nella dashboard di Cloudflare la route punta a:  http://trascrivo-web:8000
# Idempotente: se è già collegato non fa niente. Uso: sh strumenti/collega_tunnel.sh [nome-container]
set -eu
TUNNEL="${1:-${TUNNEL_CONTAINER:-cloudflared-tunnel}}"
RETE="trascrivo_pubblica"

if ! docker container inspect "$TUNNEL" >/dev/null 2>&1; then
  echo "Container $TUNNEL non trovato: collega il tunnel a mano con  docker network connect $RETE <container>"
  exit 0
fi
if docker network inspect -f '{{range .Containers}}{{.Name}} {{end}}' "$RETE" | grep -qw "$TUNNEL"; then
  echo "$TUNNEL è già sulla rete $RETE"
else
  docker network connect "$RETE" "$TUNNEL"
  echo "$TUNNEL collegato alla rete $RETE"
fi
