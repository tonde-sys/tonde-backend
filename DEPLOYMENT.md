# Déploiement et exploitation — TONDE Backend

Ce document décrit la mise en production du backend pour **un pilote réel avec
une seule organisation**. Il couvre deux cibles :

- **Render** (recommandé) : Blueprint `render.yaml`, HTTPS et sauvegardes gérés ;
- **Docker Compose sur VPS** : alternative autonome avec PostgreSQL et Redis.

> **Règle d'or** : les migrations Alembic s'exécutent **avant** le démarrage de
> l'API. Sur Render payant : `preDeployCommand`. Sur Render gratuit : le
> `docker-entrypoint.sh` applique `alembic upgrade head` au démarrage
> (`RUN_MIGRATIONS_ON_STARTUP=true`). En Compose : commande séparée.
> `CREATE_TABLES_ON_STARTUP` reste à `false` en production.

---

## 1. Ce que le déploiement installe

```
Internet
   │ HTTPS
   ▼
API FastAPI (1 worker, non-root)
   ├── PostgreSQL 15  (données, multi-tenant)
   └── Redis 7 / Valkey (files atomiques, OTP, Pub/Sub)
```

- L'API expose `/health` (réveil ultra-léger, sans base ni Redis),
  `/health/live`, `/health/ready` (base + Redis), `/health/status`,
  `/health/detailed` et `/api/v1/...`.
- Le scheduler (absents, maintenance) tourne **dans le processus**. Une seule
  instance/worker doit tourner : ne pas activer l'autoscaling horizontal.
- Les WebSockets passent par Redis Pub/Sub ; plusieurs instances pourraient
  diffuser, mais le scheduler serait dupliqué. Pour le pilote : **1 instance**.

---

## 2. Prérequis

| Élément | Obligatoire | Remarque |
|---|---|---|
| Dépôt `tonde-sys/tonde-backend` | oui | branche `main` |
| Compte Render | oui (Render) | aucune carte requise pour le plan gratuit |
| Domaine ou sous-domaine | recommandé | Render fournit `*.onrender.com` |
| Identifiants Africa's Talking production | oui pour les OTP SMS | compte `sandbox` = SMS non délivrés |
| Compte de service Firebase | non | push FCM, facultatif au pilote |

### Coût Render (Francfort)

**Démo / MVP gratuit** — configuration actuelle de `render.yaml` :

| Ressource | Plan | Coût |
|---|---|---|
| Web service | `free` | 0 $/mois |
| PostgreSQL | `free` | 0 $/mois |
| Key Value | `free` | 0 $/mois |
| **Total** | | **0 $/mois** |

Contreparties : le web s'endort après 15 min sans trafic (UptimeRobot le
réveille), la base gratuite est supprimée après **30 jours**, Redis n'a pas de
persistance, et il n'y a ni shell ni `preDeployCommand`. Les migrations passent
par `RUN_MIGRATIONS_ON_STARTUP=true` dans le `docker-entrypoint.sh`.

**Pilote réel de plus de 30 jours** — plans payants :

| Ressource | Plan | Coût |
|---|---|---|
| Web service | `0.5c-512mb` | 7 $/mois |
| PostgreSQL | `0.5c-1g` | 19 $/mois |
| Stockage PostgreSQL | 5 Go | 1,50 $/mois |
| Key Value | `256mb` | 10 $/mois |
| **Total** | | **≈ 37,50 $/mois** |

---

## 3. Déployer sur Render

### 3.1 Créer le Blueprint

1. Dashboard Render → **New** → **Blueprint**.
2. Sélectionner `tonde-sys/tonde-backend`, branche `main`.
3. Render lit `render.yaml` (plans **free**). Aucune variable manuelle n'est
   demandée : `ALLOWED_ORIGINS` a une valeur par défaut
   (`https://tonde-web.onrender.com`) et le JWT est généré par Render. Pour
   activer les SMS, ajouter ensuite `AFRICAS_TALKING_USERNAME` et
   `AFRICAS_TALKING_API_KEY` dans **Environment**. Remplacer `ALLOWED_ORIGINS`
   par l'URL réelle du front. **Jamais `*`.**
4. Valider. Render crée :
   - `tonde-api-pilot` (web Docker, plan gratuit) ;
   - `tonde-pilot-db` (PostgreSQL 15, plan gratuit) ;
   - `tonde-pilot-kv` (Key Value, plan gratuit, sans persistance).
5. Le premier déploiement applique `alembic upgrade head` au démarrage
   (`RUN_MIGRATIONS_ON_STARTUP=true`), puis lance l'API.
6. Vérifier `https://<service>.onrender.com/health` →
   `{"status":"awake","service":"tonde-api"}` (aucune requête base/Redis).

> Le nom du service peut être changé dans `render.yaml` avant le premier sync.
> Le changer après création recrée un service.
>
> **Déploiement de démonstration actuel** : le nom `tonde-api-pilot` était déjà
> pris globalement sur `onrender.com`, Render a donc suffixé les ressources :
>
> | Ressource | Nom réel | URL / identifiant |
> |---|---|---|
> | API | `tonde-api-pilot-fx1j` | `https://tonde-api-pilot-fx1j.onrender.com` |
> | PostgreSQL | `tonde-pilot-db-fx1j` | — |
> | Key Value | `tonde-pilot-kv-fx1j` | — |
>
> Les commandes ci-dessous utilisent cette URL réelle. Pour un nouveau
> déploiement, remplacer `BASE` par l'URL affichée par Render.

### 3.2 Vérifier les secrets

Dans **tonde-api-pilot-fx1j → Environment** :

- `JWT_SECRET_KEY` doit être présent et généré par Render (ne jamais le lire à voix haute).
- `DATABASE_URL` et `REDIS_URL` sont des références privées Render.
- `ENVIRONMENT=production`, `DEBUG=false`, `CREATE_TABLES_ON_STARTUP=false`.
- `RUN_MIGRATIONS_ON_STARTUP=true` sur le plan gratuit (sinon les tables
  n'existent pas au premier démarrage).
- `BOOTSTRAP_CONFIG_JSON` est optionnel ; s'il est défini, il contient les
  emails et la structure de l'organisation (secret Render).
- `ALLOWED_ORIGINS` ne contient pas `*`.

L'application refuse de démarrer si l'un de ces garde-fous est violé.

### 3.3 Bootstrap de l'organisation pilote

Le bootstrap est **idempotent** et **non destructif** : relancer la commande ne
crée pas de doublon et n'écrase pas un mot de passe existant sans
`--reset-passwords`.

**Render gratuit (pas de shell)** : coller le JSON complet ci-dessous dans la
variable d'environnement `BOOTSTRAP_CONFIG_JSON` du service, puis redéployer.
`docker-entrypoint.sh` exécute le bootstrap à chaque démarrage. Les mots de passe
générés apparaissent **une seule fois** dans les logs du premier démarrage.
Laisser `BOOTSTRAP_PASSWORDS_JSON` vide pour les générer aléatoirement.

**Render payant / VPS** : ouvrir **Shell** sur `tonde-api-pilot-fx1j` et créer
`/tmp/pilot.json` à partir de `bootstrap/pilot.example.json`.

```bash
cat > /tmp/pilot.json <<'JSON'
{
  "organization": {
    "name": "NOM RÉEL DE L'ORGANISATION",
    "slug": "nom-organisation",
    "sector": "bank",
    "country": "Burundi",
    "city": "Bujumbura",
    "phone": "+257...",
    "email": "contact@organisation.bi"
  },
  "branch": {
    "name": "Agence principale",
    "slug": "nom-organisation-principale",
    "address": "Adresse réelle",
    "city": "Bujumbura",
    "opens_at": "08:00",
    "closes_at": "17:00",
    "max_daily_tickets": 500,
    "avg_service_minutes": 5
  },
  "services": [
    {"name": "Caisse", "ticket_prefix": "A", "avg_duration_minutes": 5},
    {"name": "Crédit", "ticket_prefix": "B", "avg_duration_minutes": 12}
  ],
  "counters": [
    {"name": "Guichet 1"},
    {"name": "Guichet 2"}
  ],
  "users": [
    {
      "name": "Super administrateur TONDE",
      "email": "superadmin@organisation.bi",
      "role": "super_admin"
    },
    {
      "name": "Administrateur organisation",
      "email": "admin@organisation.bi",
      "role": "admin_org"
    },
    {
      "name": "Agent Guichet 1",
      "email": "agent1@organisation.bi",
      "role": "agent",
      "counter": "Guichet 1"
    },
    {
      "name": "Agent Guichet 2",
      "email": "agent2@organisation.bi",
      "role": "agent",
      "counter": "Guichet 2"
    }
  ]
}
JSON
```

> Le JSON contient des emails réels : le stocker dans le gestionnaire de
> secrets Render (`BOOTSTRAP_CONFIG_JSON`), jamais dans Git.

**Étapes (Shell payant / VPS) :**

1. Simuler sans écrire :

```bash
python -m app.cli bootstrap --config /tmp/pilot.json --dry-run
```

2. Appliquer :

```bash
python -m app.cli bootstrap --config /tmp/pilot.json
```

Sur Render gratuit, cette étape est automatique au démarrage dès que
`BOOTSTRAP_CONFIG_JSON` est défini. Le bootstrap est rejoué à chaque redémarrage
sans modifier les comptes existants.

La commande affiche **une seule fois** les mots de passe générés. Les copier
immédiatement dans un gestionnaire de secrets, puis les changer après la
première connexion.

Variantes :

```bash
# Fournir les mots de passe par variable d'environnement (recommandé en CI)
export BOOTSTRAP_PASSWORDS_JSON='{"admin@organisation.bi":"MotDePasseFort!2026"}'
python -m app.cli bootstrap --config /tmp/pilot.json

# Tout en flags, sans fichier
python -m app.cli bootstrap \
  --org-name "Organisation" --org-slug organisation --org-sector bank \
  --branch-name "Agence principale" --branch-slug organisation-principale \
  --service "Caisse|A|5" --service "Crédit|B|12" \
  --counter "Guichet 1" --counter "Guichet 2" \
  --admin-email admin@organisation.bi \
  --super-admin-email superadmin@organisation.bi \
  --employee "Agent 1|agent1@organisation.bi|agent|Guichet 1"
```

> Le fichier `bootstrap/pilot.json` est ignoré par Git. Ne jamais committer les
> emails, mots de passe ou identifiants réels.

### 3.4 Vérification post-déploiement

```bash
BASE=https://tonde-api-pilot-fx1j.onrender.com

curl -fsS $BASE/health
curl -fsS $BASE/health/ready
curl -fsS $BASE/health/status
```

Réponses attendues :

- `/health` → `{"status":"awake","service":"tonde-api"}`. Cet endpoint ne touche
  ni PostgreSQL ni Redis : il sert uniquement à réveiller le service Render.
- `/health/ready` → `{"status":"ready",...}` avec `database` et `redis` sains.
- `/health/status` → `{"status":"ok",...}` (vue riche pour le dashboard).

Connexion administrateur :

```bash
curl -fsS -X POST $BASE/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@organisation.bi","password":"<mot-de-passe>"}'
```

Scénario API complet (à jouer une fois avant d'ouvrir au pilote) :

```bash
# 1. Créer un client de test (l'inscription email évite le SMS pour ce test)
curl -fsS -X POST $BASE/api/v1/auth/register/email \
  -H 'Content-Type: application/json' \
  -d '{"name":"Client Test","email":"client.test@organisation.bi","password":"ClientTest!2026"}'

# 2. Avec le token du client, prendre un ticket
curl -fsS -X POST $BASE/api/v1/tickets \
  -H "Authorization: Bearer <ACCESS_TOKEN_CLIENT>" \
  -H 'Content-Type: application/json' \
  -d '{"agency_id":"<BRANCH_ID>","service_id":"<SERVICE_ID>","client_action_id":"demo-0001"}'

# 3. Avec le token de l'agent, appeler le ticket au guichet
curl -fsS -X POST $BASE/api/v1/tickets/counter/call-next \
  -H "Authorization: Bearer <ACCESS_TOKEN_AGENT>" \
  -H 'Content-Type: application/json' \
  -d '{"agency_id":"<BRANCH_ID>","service_id":"<SERVICE_ID>","counter_id":"<COUNTER_ID>"}'

# 4. Confirmer la prise en charge puis terminer
curl -fsS -X POST $BASE/api/v1/tickets/<TICKET_ID>/serving \
  -H "Authorization: Bearer <ACCESS_TOKEN_AGENT>"
curl -fsS -X POST $BASE/api/v1/tickets/<TICKET_ID>/done \
  -H "Authorization: Bearer <ACCESS_TOKEN_AGENT>"
```

Les IDs (`BRANCH_ID`, `SERVICE_ID`, `COUNTER_ID`) sont affichés par le bootstrap.

### 3.5 Vérifier les SMS

L'envoi réel dépend d'Africa's Talking et de la couverture de l'opérateur
burundais. Tester avec un numéro réel :

```bash
curl -fsS -X POST $BASE/api/v1/auth/register/phone \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+257XXXXXXXX","country_code":"BI"}'
```

Si l'API renvoie `503 SMS_DELIVERY_FAILED`, l'OTP n'a pas été accepté par la
passerelle : vérifier le username (pas `sandbox`), la clé API, l'ID expéditeur
et la couverture +257. **Sans SMS, les clients ne peuvent pas s'inscrire par
téléphone** ; l'inscription email reste disponible pour le staff et les tests.

### 3.6 Garder le service éveillé avec UptimeRobot

Le plan gratuit Render endort un web service après **15 minutes** sans trafic.
Un seul moniteur UptimeRobot suffit :

1. Créer un moniteur **HTTP(s)**.
2. URL : `https://tonde-api-pilot-fx1j.onrender.com/health`.
3. Intervalle : **5 minutes** (minimum du plan gratuit).
4. Type de contrôle : **Keyword**, mot-clé `awake` (ou `"status":"awake"`).
5. Région : choisir la plus proche (Europe/Francfort).
6. Activer les alertes e-mail.

Pourquoi `/health` et pas `/health/ready` : `/health` répond depuis le processus
FastAPI sans ouvrir de connexion PostgreSQL ou Redis. Un ping toutes les 5 minutes
ne réveille donc pas la base et ne consomme pas de connexions. Utiliser
`/health/ready` uniquement pour une supervision fonctionnelle (base + Redis),
avec un intervalle plus large (10–15 min) pour ne pas surcharger la base.

> Un service 24/7 consomme ≈ 730 h/mois sur les 750 h gratuites de Render.
> Ne pas multiplier les moniteurs sur le même service.

---

## 4. Alternative : Docker Compose sur VPS

### 4.1 Préparer le serveur

- Ubuntu 22.04/24.04, Docker Engine + plugin Compose.
- Pare-feu : ouvrir **80/443** uniquement. Ne pas exposer 5432/6379.
- Un nom de domaine pointant vers l'IP du serveur.

```bash
git clone https://github.com/tonde-sys/tonde-backend.git
cd tonde-backend
git checkout main
cp .env.production.example .env.production
```

Remplir `.env.production` :

- `POSTGRES_PASSWORD` fort (32+ caractères) ;
- `JWT_SECRET_KEY` (32+ caractères aléatoires) ;
- `ALLOWED_ORIGINS` (domaines réels, jamais `*`) ;
- identifiants Africa's Talking production.

### 4.2 Démarrer

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --build
docker compose --env-file .env.production -f docker-compose.prod.yml ps
curl -fsS http://127.0.0.1:8000/health
```

L'API n'écoute que sur `127.0.0.1:8000` par défaut. Ajouter un reverse proxy TLS.

### 4.3 Reverse proxy Caddy (HTTPS automatique)

```caddyfile
api.tonde.bi {
    reverse_proxy 127.0.0.1:8000
    encode gzip
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Mettre `ALLOWED_ORIGINS` à jour si un cockpit web est servi sur un autre domaine.

### 4.4 Bootstrap

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml \
  exec api python -m app.cli bootstrap --config /tmp/pilot.json --dry-run
```

Créer `/tmp/pilot.json` dans le conteneur avec `docker compose cp`, ou utiliser
les flags de la section 3.3.

---

## 5. Exploitation courante

### 5.1 Migrations

```bash
# Render gratuit : automatique au démarrage (RUN_MIGRATIONS_ON_STARTUP=true)
# Render payant : automatique avant chaque déploiement (preDeployCommand)
# VPS :
docker compose --env-file .env.production -f docker-compose.prod.yml \
  exec api alembic upgrade head
```

Sur Render gratuit, les migrations tournent dans `docker-entrypoint.sh` avant
uvicorn. En cas d'échec, le conteneur ne démarre pas : consulter les logs du
déploiement. Ne jamais lancer deux instances pendant une migration.

Ne jamais utiliser `alembic revision --autogenerate` en production : générer et
tester la migration en local, la committer, puis déployer.

### 5.2 Sauvegardes PostgreSQL

Render (plan gratuit) : **aucune sauvegarde automatique** et base supprimée
après 30 jours. Exporter les données avant l'expiration, ou passer au plan
payant. Render (plan payant) : sauvegardes et PITR activés par défaut. Vérifier
la fenêtre de rétention dans **tonde-pilot-db → Backups**. Export logique manuel :

```bash
pg_dump "<EXTERNAL_DATABASE_URL>" --format=custom --file=tonde-$(date +%F).dump
```

VPS :

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml \
  exec -T db pg_dump -U tonde -d tonde_db -Fc > tonde-$(date +%F).dump
```

Restaurer dans une base vide :

```bash
pg_restore --clean --if-exists --no-owner -d "<DATABASE_URL>" tonde-2026-11-13.dump
```

Tester une restauration au moins une fois avant le pilote.

### 5.3 Journaux et santé

```bash
# Render : Dashboard → Logs, ou CLI
render logs tonde-api-pilot-fx1j --tail

# VPS
docker compose --env-file .env.production -f docker-compose.prod.yml logs -f api
```

Alertes à surveiller : `/health/ready` en 503 (base ou Redis indisponible),
erreurs 5xx, files Redis anormalement longues, scheduler arrêté. `/health` reste
volontairement à 200 même si la base est tombée : c'est un réveil, pas une
readiness probe.

### 5.4 Redéploiement et rollback

- Render : chaque déploiement est un build immutable ; **Rollback** depuis
  l'onglet Deploys.
- VPS : `git pull`, puis `up -d --build`. Conserver l'image précédente
  (`tonde-api:production`) pour un retour arrière rapide.
- Un rollback de code ne annule **pas** une migration : prévoir une migration
  inverse ou restaurer la sauvegarde.

### 5.5 Rotation des secrets

- `JWT_SECRET_KEY` : rotation invalide tous les jetons en circulation (les
  utilisateurs devront se reconnecter). Planifier hors heures de pointe.
- Mots de passe staff : `python -m app.cli bootstrap ... --reset-passwords`
  avec `BOOTSTRAP_PASSWORDS_JSON`.
- Clé Africa's Talking / Firebase : mettre à jour dans Render, redéployer.

---

## 6. Sécurité — checklist avant pilote

- [ ] `ENVIRONMENT=production`, `DEBUG=false`, `CREATE_TABLES_ON_STARTUP=false`.
- [ ] `JWT_SECRET_KEY` aléatoire ≥ 32 caractères, jamais commité.
- [ ] `ALLOWED_ORIGINS` explicite, jamais `*`.
- [ ] PostgreSQL et Redis non joignables depuis Internet.
- [ ] HTTPS actif (Render automatique ; Caddy sur VPS).
- [ ] `/docs`, `/redoc` désactivés en production (comportement automatique).
- [ ] Mots de passe initiaux changés après la première connexion.
- [ ] Sauvegarde testée et accès au gestionnaire de secrets partagé.
- [ ] Journaux consultés après le bootstrap : aucun mot de passe en clair.
- [ ] Limite de connexions WebSocket adaptée au Wi-Fi de la salle
      (`WS_CONNECT_LIMIT_PER_MINUTE`, 120 dans `render.yaml`).

---

## 7. Limites connues du pilote

Ces points ne sont pas des bugs du backend, mais conditionnent l'usage réel :

1. **Aucune interface client/staff exploitable** : l'app mobile est encore le
   scaffold Flutter et le cockpit web n'est pas connecté. Le backend est
   utilisable via API, pas encore par un client final.
2. **Pas de kiosque public** : la création de ticket exige un `user_id`. Le
   staff ne peut pas prendre plusieurs tickets pour des clients anonymes.
3. **Un seul ticket actif par utilisateur** : un client ne peut pas prendre
   deux tickets simultanément.
4. **SMS à confirmer** : sans Africa's Talking production et couverture +257,
   les OTP téléphone échouent. L'inscription email reste un contournement pour
   le staff, pas pour les clients.
5. **Une seule instance** : le scheduler est in-process ; ne pas activer
   l'autoscaling tant qu'un verrou de leader n'est pas ajouté.
6. **Pas d'envoi d'emails** : pas de réinitialisation de mot de passe en libre-
   service ; le bootstrap avec `--reset-passwords` fait foi.
7. **Plan Render gratuit** : base supprimée après 30 jours, Redis sans
   persistance, service endormi sans UptimeRobot. Pour un pilote réel de longue
   durée, passer aux plans payants ou exporter/migrer la base avant expiration.
8. **Réveil dépendant d'UptimeRobot** : si le moniteur est supprimé, le service
   gratuit s'endort et le premier appel suivant prend plusieurs dizaines de
   secondes (cold start).

Pour un pilote « vrai client », la prochaine décision est l'interface :
connecter le cockpit web existant, finaliser le mobile, ou ajouter un mode
kiosque/public au backend.

---

## 8. Résumé des commandes

| Action | Commande |
|---|---|
| Réveil / UptimeRobot | `curl -fsS $BASE/health` |
| Readiness (base + Redis) | `curl -fsS $BASE/health/ready` |
| Migrations (VPS) | `docker compose ... exec api alembic upgrade head` |
| Bootstrap (dry-run) | `python -m app.cli bootstrap --config /tmp/pilot.json --dry-run` |
| Bootstrap (réel) | `python -m app.cli bootstrap --config /tmp/pilot.json` |
| Bootstrap Render gratuit | variable `BOOTSTRAP_CONFIG_JSON` + redéploiement |
| Tests locaux | `pip install -r requirements-dev.txt && pytest -q` |
| Logs (VPS) | `docker compose ... logs -f api` |
| Sauvegarde (VPS) | `docker compose ... exec -T db pg_dump ...` |
