# TONDE Backend

> **TONDE** signifie *"file d'attente"* en Kirundi.

Plateforme SaaS B2B multi-tenant de gestion intelligente des files d'attente, conçue pour les banques, hôpitaux, universités et administrations du Burundi, de la RDC et de l'Afrique de l'Est.

**Mission :** Transformer le chaos de l'attente en une expérience claire, prévisible et digne.

---

## Stack technique

| Composant | Technologie | Version |
|-----------|-------------|---------|
| Framework | FastAPI | 0.111.0 |
| Runtime | Python | 3.12+ |
| Base de données | PostgreSQL | 15 |
| Cache / File | Redis | 7 |
| ORM | SQLAlchemy async | 2.0.30 |
| Migrations | Alembic | 1.13.1 |
| Validation | Pydantic v2 | 2.7.1 |
| Auth | JWT + OTP SMS | — |
| Temps réel | WebSocket + Redis Pub/Sub | — |
| Conteneurisation | Docker + Docker Compose | — |

---

## Prérequis

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installé et démarré
- [Git](https://git-scm.com/)
- Python 3.12+ (optionnel, pour développement hors Docker)

---

## Installation & démarrage rapide

```bash
# 1. Cloner le projet
git clone https://github.com/tonde-sys/tonde-backend.git
cd tonde-backend

# 2. Configurer les variables d'environnement
cp .env.example .env
# Éditer .env avec vos valeurs

# 3. Lancer l'environnement complet
docker-compose up -d --build

# 4. Appliquer les migrations
docker-compose exec api alembic upgrade head

# 5. Vérifier que l'API est vivante
# Swagger  : http://localhost:8000/docs
# Health   : http://localhost:8000/health          (ultra-léger, sans base)
# Readiness: http://localhost:8000/health/ready    (PostgreSQL + Redis)
# Redis UI : http://localhost:8081
```

> `docker-compose.yml` est l'environnement de **développement** (rechargement à
> chaud, mots de passe de démonstration). Pour une démo gratuite, utiliser le
> Blueprint Render `render.yaml` (plans `free`, UptimeRobot sur `/health`). Pour
> un pilote réel de longue durée, utiliser les plans payants ou
> `docker-compose.prod.yml` — voir **[DEPLOYMENT.md](DEPLOYMENT.md)**.

---

## Structure du projet

```
tonde-backend/
├── app/
│   ├── main.py              # Point d'entrée FastAPI
│   ├── core/                # Config, DB, Redis, Sécurité, Dépendances
│   ├── models/              # Modèles SQLAlchemy (tables PostgreSQL)
│   ├── schemas/             # Schémas Pydantic v2 (validation I/O)
│   ├── services/            # Logique métier
│   ├── routers/             # Endpoints HTTP et WebSocket
│   └── websocket/           # Gestionnaire WebSocket + événements
├── migrations/              # Migrations Alembic
├── tests/                   # Tests pytest
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

### Hiérarchie des données

```
Organization → Branch/Agency → Service → Counter → Ticket
```

Chaque entité possède un `org_id` pour le cloisonnement multi-tenant strict.

---

## Commandes utiles

```bash
# Lancer les tests
docker-compose exec api pytest --tb=short -q

# Créer une migration après modification d'un modèle
docker-compose exec api alembic revision --autogenerate -m "description"

# Appliquer les migrations
docker-compose exec api alembic upgrade head

# Voir les logs de l'API en temps réel
docker-compose logs -f api

# Redémarrer l'API après modification
docker-compose restart api

# Arrêter tous les conteneurs
docker-compose down
```

---

## Variables d'environnement

Copier `.env.example` → `.env` et remplir les valeurs.

| Variable | Description | Exemple |
|----------|-------------|---------|
| `DATABASE_URL` | URL PostgreSQL async | `postgresql+asyncpg://...` |
| `REDIS_URL` | URL Redis | `redis://localhost:6379` |
| `JWT_SECRET_KEY` | Clé secrète JWT | `openssl rand -hex 32` |
| `ENVIRONMENT` | Environnement | `development` / `production` |
| `AFRICAS_TALKING_API_KEY` | Clé SMS Africa's Talking | — |

> ⚠️ Ne jamais committer le fichier `.env`. Il est dans `.gitignore`.

---

## Architecture temps réel

```
Queue Engine (service)
      ↓ publie événement
Redis Pub/Sub
      ↓ listener background
WebSocket Manager
      ↓ diffuse
Clients (mobile Flutter, desktop Tauri)
```

Les événements sont **publiés par les services métier** (jamais par les routers), puis relayés
par chaque instance de l'API à ses clients. Une panne temps réel ne fait jamais échouer une
opération métier : le client récupère son état au prochain `GET` ou à la reconnexion.

### Événements WebSocket

| Événement | Destinataire | Description |
|-----------|--------------|-------------|
| `your_turn` | le client du ticket | C'est votre tour (`timeout_seconds` pour se présenter) |
| `ticket_called` | toute la salle | Un numéro est appelé à un guichet |
| `queue_update` | le client du ticket | Nouvelle position / ETA (un message **individuel** par client) |
| `absent_warning` | le client du ticket | Avertissement avant le passage automatique en absent |
| `ticket_absent` | le client du ticket | Déclaré absent (il peut revenir en file) |
| `ticket_done` | le client du ticket | Service terminé |
| `ticket_transferred` | le client du ticket | Transféré vers un autre service (nouveau numéro) |
| `priority_updated` | le client du ticket | Priorité confirmée / rejetée par un agent |
| `counter_open` / `counter_close` / `counter_pause` | toute la salle | État d'un guichet |
| `broadcast_message` | toute la salle | Message général |

**Reprise après coupure** : à chaque (re)connexion, le serveur envoie immédiatement un
`queue_update` contenant l'état réel du ticket (le WebSocket ne rejoue pas l'historique).
Le client peut aussi envoyer `"sync"` pour redemander cet état, ou `"ping"` (→ `pong`).

---

## États des tickets

```
WAITING ──→ CALLED ──→ SERVING ──→ DONE                       (terminal)
   │          │  ↘         ↘ INCOMPLETE ──→ WAITING (requeue agent)
   │          │   ABSENT ─────────────────→ WAITING (le client revient)
   │          │       └──→ CANCELLED (fenêtre de retour expirée)
   │          └→ TRANSFERRED ──→ WAITING (file du service cible, nouveau numéro)
   └→ CANCELLED                                                 (terminal)
```

Les statuts « actifs » (`WAITING`, `CALLED`, `SERVING`, `ABSENT`, `TRANSFERRED`, `INCOMPLETE`)
bloquent la création d'un nouveau ticket (**1 ticket actif par utilisateur**). Chacun a donc une
sortie, et les tickets bloqués sont clos automatiquement (voir *Expirations*).

---

## Queue Engine — garanties

| Garantie | Mécanisme |
|----------|-----------|
| Un ticket n'est jamais appelé deux fois | `ZPOPMIN` (Redis) **et** compare-and-set `UPDATE … WHERE status='waiting'` (PostgreSQL) |
| Un ticket n'est jamais perdu | Remis à sa place d'origine si l'écriture échoue après le retrait de la file |
| Numéros uniques, sans trou | Compteur Redis `INCR` par jour (heure du Burundi, UTC+2), format `A-007` |
| Pas de double ticket (double tap, retry réseau) | Verrou `SET NX` par utilisateur + idempotence `client_action_id` |
| Redis est un accélérateur, **PostgreSQL est la vérité** | Reconstruction des files au démarrage, à la consultation d'un ticket absent, et à l'appel d'une file vide |
| ETA réaliste | Moyenne glissante des 10 derniers services **réels** (`service_seconds`), lissée par la durée configurée |
| Priorité jamais auto-attribuée | Le client *demande* ; un agent confirme (`POST /tickets/{id}/priority`), l'ancienneté est conservée |
| L'agent n'agit que sur SES tickets | Guichet affecté (`Employee.counter_id`), branche pour superviseurs/admins d'agence |

### Tâches planifiées (`QueueScheduler`, sans Celery)

- **Avertissement** à `ABSENT_WARNING_SECONDS` puis **passage automatique en ABSENT** à `ABSENT_TIMEOUT_SECONDS` après l'appel.
- **Expirations** : `WAITING` trop ancien, `ABSENT` hors fenêtre de retour, `SERVING` oublié → `INCOMPLETE`, `INCOMPLETE` non résolu.
- **Alerte d'attente longue** par SMS (branches avec `enable_sms_reminders`).
- Multi-instances : les échéances sont des ZSET Redis réclamés atomiquement (`ZREM`) ; la maintenance n'est exécutée que par une instance par intervalle.

---

## Endpoints principaux

| Domaine | Endpoints |
|---------|-----------|
| Tickets (client) | `POST /tickets` · `GET /tickets/{id}` · `DELETE /tickets/{id}` · `GET /tickets/history` · `POST /tickets/{id}/return` |
| Tickets (agent) | `POST /tickets/counter/call-next` · `/{id}/serving` · `/done` · `/absent` · `/incomplete` · `/transfer` · `/priority` · `/requeue` · `/close` |
| Public (sans auth) | `GET /tickets/qr/{qr_token}` · `GET /branches/{id}/display` (écran de salle) · `GET /branches/{id}/services` |
| Hors-ligne | `GET /sync/state` · `POST /sync/actions` (idempotent) · `GET /sync/changes?since=` (7 jours max) |
| Notifications | `GET /notifications` · `POST /notifications/{id}/read` · `POST /notifications/read-all` · `PUT /auth/me/fcm-token` |
| Exports (superviseur+) | `GET /organizations/{org}/tickets/export` · `…/branches/{id}/tickets/export` · `…/branches/{id}/stats/export` (`format=csv` ou `xlsx`) |
| Exploitation | `GET /health` (réveil, sans base) · `/health/live` · `/health/ready` (base + Redis) · `/health/status` · `/health/detailed` (admin) |

---

## Sécurité

- **Production** : l'application **refuse de démarrer** si `JWT_SECRET_KEY` < 32 caractères (ou valeur par
  défaut), `DEBUG=true` ou `CREATE_TABLES_ON_STARTUP=true`. `/docs` et `/redoc` sont désactivés.
- **Périmètre** : `org_id` partout + contrôle **par branche** (un superviseur de l'agence A ne voit pas l'agence B).
- **Anti-escalade** : on n'attribue jamais un rôle supérieur au sien ; un utilisateur d'une autre
  organisation ne peut pas être « récupéré » ; guichets/agences d'un employé validés dans l'organisation.
- **OTP** : `secrets` (CSPRNG), stocké haché, 3 tentatives, blocage 15 min ; si le SMS échoue → `503`, pas de faux « code envoyé ».
- **Exports** : audités (`audit_logs`), protégés contre l'injection de formules Excel (`=`, `+`, `-`, `@`).
- Rate limiting par route (login, OTP, refresh, QR public, sync, WebSocket).

---

## Migrations

```bash
docker-compose exec api alembic upgrade head      # depuis une base VIDE ou existante
```

La migration `007` aligne les enums PostgreSQL sur le modèle ORM (les migrations 001/006 les avaient
créés en minuscules, incompatibles avec SQLAlchemy) et ajoute `audit_logs` et les champs d'idempotence.

---

## Tests

```bash
# Dépendances de développement (pytest, aiosqlite)
pip install -r requirements-dev.txt

# Tests unitaires + HTTP de bout en bout (SQLite en mémoire)
pytest

# Les tests d'atomicité et de bout en bout utilisent un VRAI Redis (REDIS_URL) ;
# ils sont ignorés (skip) si Redis est injoignable. En local :
docker run -d -p 6391:6379 redis:7-alpine && REDIS_URL=redis://localhost:6391 pytest
```

---

## Workflow de contribution

Ce projet utilise un workflow **Pull Request obligatoire**. Aucun push direct sur `main`.

```bash
# 1. Partir de main à jour
git checkout main && git pull origin main

# 2. Créer une branche
git checkout -b feat/nom-de-la-feature

# 3. Développer, tester
docker-compose exec api pytest --tb=short -q

# 4. Committer
git add .
git commit -m "feat: description claire"

# 5. Pousser et ouvrir un Pull Request
git push -u origin feat/nom-de-la-feature
```

Conventions de nommage des branches :
- `feat/` — nouvelle fonctionnalité
- `fix/` — correction de bug
- `chore/` — configuration, dépendances
- `docs/` — documentation

---

## Environnements

| Environnement | URL API |
|---------------|---------|
| Local | `http://localhost:8000` |
| Mobile (émulateur Android) | `http://10.0.2.2:8000` |
| Staging | `https://api-staging.tonde.app` |
| Production | `https://api.tonde.app` |

---

## Écosystème TONDE

| Repo | Description | Stack |
|------|-------------|-------|
| `tonde-backend` | API REST + WebSocket | FastAPI + PostgreSQL + Redis |
| `tonde-mobile` | App citoyens | Flutter |
| `tonde-web-admin` | Dashboard admins & superviseurs | React + Vite |
| `tonde-desktop` | Interface guichetiers | Tauri + Rust |

---

## Roadmap

| Version | Fonctionnalités |
|---------|----------------|
| **v1.0 (MVP)** | Auth, Organizations, Queue Engine, WebSocket |
| v1.5 | Mobile Money, Paiements, Réservations |
| v2.0 | ETA prédictif, Analytics avancés |
| v3.0 | Smart Routing IA, API publique |

---

## Licence

Propriété exclusive du projet TONDE.
Chef de projet : **Vital** — Burundi 🇧🇮

---

*Construire l'infrastructure de service de demain pour l'Afrique.*
