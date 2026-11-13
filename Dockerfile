# syntax=docker/dockerfile:1.7
#
# Image de production TONDE.
#
#   docker build -t tonde-api .
#   docker run --env-file .env.production -p 8000:8000 tonde-api
#
# Caractéristiques :
#   - build multi-étages : les compilateurs ne sont pas dans l'image finale ;
#   - utilisateur non-root (uid 10001) ;
#   - aucune dépendance de test installée ;
#   - écoute sur $PORT (Render l'injecte, 8000 par défaut) ;
#   - migrations exécutées séparément (alembic upgrade head) ou au démarrage
#     avec RUN_MIGRATIONS_ON_STARTUP=true (obligatoire sur Render gratuit,
#     qui ne propose ni shell ni preDeployCommand).
ARG PYTHON_VERSION=3.12

# ── Étape 1 : dépendances ─────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# ── Étape 2 : image d'exécution ───────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 tonde \
    && useradd --uid 10001 --gid tonde --create-home \
           --home-dir /app --shell /usr/sbin/nologin tonde

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=tonde:tonde alembic.ini ./
COPY --chown=tonde:tonde migrations ./migrations
COPY --chown=tonde:tonde app ./app
COPY --chown=tonde:tonde bootstrap ./bootstrap
COPY --chown=tonde:tonde --chmod=755 docker-entrypoint.sh ./docker-entrypoint.sh

USER tonde
EXPOSE 8000

# Healthcheck local (Docker Compose / Kubernetes). Render utilise /health via
# healthCheckPath et ignore cette directive.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/health/live', timeout=3)"

# Un seul worker : le scheduler de file est in-process. Voir DEPLOYMENT.md.
# Sur Render gratuit (pas de preDeployCommand), mettre RUN_MIGRATIONS_ON_STARTUP=true.
# Sur un plan payant ou Docker Compose, les migrations restent un step séparé.
ENTRYPOINT ["./docker-entrypoint.sh"]
