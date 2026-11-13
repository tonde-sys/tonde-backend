#!/bin/sh
# Point d'entrée de production TONDE.
#
# Variables :
#   RUN_MIGRATIONS_ON_STARTUP=true  applique `alembic upgrade head` avant l'API.
#                                   Obligatoire sur Render gratuit (pas de shell,
#                                   pas de preDeployCommand).
#   BOOTSTRAP_CONFIG_JSON=<json>    provisionne une organisation pilote au
#                                   démarrage. Idempotent : les redémarrages ne
#                                   créent pas de doublon et ne changent pas les
#                                   mots de passe existants.
set -eu

if [ "${RUN_MIGRATIONS_ON_STARTUP:-false}" = "true" ]; then
    echo "[entrypoint] application des migrations Alembic..."
    alembic upgrade head
fi

if [ -n "${BOOTSTRAP_CONFIG_JSON:-}" ]; then
    echo "[entrypoint] bootstrap de l'organisation pilote..."
    printf '%s' "$BOOTSTRAP_CONFIG_JSON" > /tmp/tonde-bootstrap.json
    python -m app.cli bootstrap --config /tmp/tonde-bootstrap.json
    rm -f /tmp/tonde-bootstrap.json
fi

# Docker Compose peut fournir sa propre commande (migrations + uvicorn).
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --proxy-headers \
    --forwarded-allow-ips='*'
