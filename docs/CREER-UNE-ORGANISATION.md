# Créer une organisation dans TONDE — informations à collecter

Document à remplir par l'organisation partenaire **avant** la mise en service.
Il liste exactement les données que l'API TONDE exige, avec les contraintes de
validation réellement appliquées par le code.

Hiérarchie des objets :

```
Organization → Branch (agence/site) → Service (file logique) → Counter (guichet) → Ticket
```

Une organisation sans **au moins une Branch**, un **Service** et un **Counter**
ne peut délivrer aucun ticket : ces trois niveaux sont indispensables.

---

## A. Identité de l'organisation (obligatoire)

| Champ | Obligatoire | Contrainte | Exemple |
|---|---|---|---|
| `name` | ✅ | Non vide, 200 car. max | Banque Coopec Burundi |
| `slug` | ✅ | Uniquement `a-z`, `0-9`, `-` — **unique** sur la plateforme | banque-coopec |
| `sector` | ✅ | `bank` \| `hospital` \| `university` \| `administration` \| `other` | bank |

## B. Coordonnées (recommandé)

| Champ | Obligatoire | Contrainte | Exemple |
|---|---|---|---|
| `country` | ❌ (défaut `Burundi`) | 50 car. max | Burundi |
| `city` | ❌ | 100 car. max | Bujumbura |
| `phone` | ❌ | 20 car. max | +25722200000 |
| `email` | ❌ | Email valide | contact@coopec.bi |
| `website` | ❌ | 500 car. max | https://coopec.bi |
| `logo_url` | ❌ | 500 car. max, URL publique | https://…/logo.png |
| `description` | ❌ | Texte libre | Réseau de 12 agences |

> Le `slug` apparaît dans les URLs et les QR codes : il ne doit plus changer
> après la mise en production.

---

## C. Établissements / agences (`Branch`) — au moins 1

Répéter ce bloc pour chaque site physique.

| Champ | Obligatoire | Contrainte | Exemple |
|---|---|---|---|
| `name` | ✅ | 200 car. max | Agence Principale |
| `slug` | ✅ | `a-z0-9-`, **unique globalement** | agence-principale |
| `sector` | ✅ | Même liste que l'organisation | bank |
| `address` | ❌ | 500 car. max | Avenue de la Mission, n°12 |
| `city` | ❌ (défaut `Bujumbura`) | 100 car. max | Bujumbura |
| `country` | ❌ (défaut `Burundi`) | 50 car. max | Burundi |
| `phone` / `email` | ❌ | — | +25722200001 |
| `latitude` / `longitude` | ❌ | Décimaux (géolocalisation mobile) | -3.3822 / 29.3644 |
| `opens_at` | ❌ (défaut `08:00`) | Format `HH:MM` | 07:30 |
| `closes_at` | ❌ (défaut `17:00`) | Format `HH:MM` | 16:30 |
| `max_daily_tickets` | ❌ (défaut `500`) | Entier > 0 | 800 |
| `avg_service_minutes` | ❌ (défaut `5`) | Entier, minutes | 7 |
| `logo_url` | ❌ | URL | — |
| `is_open` | ❌ (défaut `false`) | ⚠️ **à passer à `true`** sinon aucun ticket n'est délivré | true |

### Configuration de la file (fin, optionnel)

| Champ | Défaut | Rôle |
|---|---|---|
| `max_wait_minutes_alert` | 30 | Seuil d'alerte « attente trop longue » pour le superviseur |
| `operating_hours` | `null` | Horaires détaillés par jour (JSON), si différents de `opens_at`/`closes_at` |
| `enable_sms_reminders` | `false` | Rappels SMS au client |
| `reminder_interval_minutes` | 10 | Fréquence des rappels |
| `supported_languages` | `["fr"]` | Langues des messages client (`fr`, `en`, `rn`, `sw`…) |

---

## D. Services / files d'attente (`Service`) — au moins 1

| Champ | Obligatoire | Contrainte | Exemple |
|---|---|---|---|
| `name` | ✅ | 1–200 car. | Opérations / Dépôts |
| `ticket_prefix` | ❌ (défaut `A`) | **1 à 5 lettres A–Z**, unique par agence | A |
| `avg_duration_minutes` | ❌ (défaut `5`) | 1–240 | 6 |
| `description` | ❌ | 1000 car. max | Retraits et dépôts espèces |
| `is_active` | ❌ (défaut `true`) | — | true |

> Le préfixe devient le numéro affiché (`A-001`, `B-014`). Il **ne peut plus
> être modifié** après création : le changer en cours de journée produirait des
> numéros incohérents. À figer avec l'organisation.

## E. Guichets physiques (`Counter`) — au moins 1

| Champ | Obligatoire | Contrainte | Exemple |
|---|---|---|---|
| `name` | ✅ | Non vide | Guichet 1 |
| `description` | ❌ | Texte libre | Guichet entreprises |

Chaque guichet est rattaché à une Branch. Un agent est ensuite **affecté** à un
guichet : c'est cette affectation qui l'autorise à appeler des tickets.

---

## F. Comptes utilisateurs

Rôles disponibles, du moins au plus privilégié :

| Rôle | Usage | Portée |
|---|---|---|
| `client` | Usager mobile | Ses propres tickets |
| `agent` | Guichetier | Son guichet |
| `supervisor` | Responsable de salle | Son agence |
| `admin_agency` | Administrateur d'agence | Une Branch |
| `admin_org` | Administrateur organisation | Toute l'organisation |
| `super_admin` | Équipe TONDE | Plateforme entière |

### Comptes à fournir au minimum

| Rôle | Nombre | Champs requis |
|---|---|---|
| `admin_org` | 1 | `name`, `email`, mot de passe (≥ 8 car.) |
| `admin_agency` | 1 par agence | idem |
| `supervisor` | 1 par agence (optionnel) | idem |
| `agent` | 1 par guichet | idem + `counter` (nom du guichet) |

> `super_admin` n'est **jamais** créé via l'API : il est provisionné par TONDE.
> Un administrateur ne peut pas attribuer un rôle supérieur au sien
> (garde-fou `ROLE_ESCALATION`).

---

## G. Récapitulatif — checklist à envoyer

```
ORGANISATION
  Nom ..............................
  Slug (a-z0-9-) ...................
  Secteur (bank/hospital/university/administration/other) ...
  Pays / Ville .....................
  Téléphone / Email ................
  Logo (URL) .......................

AGENCE 1
  Nom / Slug .......................
  Adresse / Ville ..................
  GPS (lat, long) ..................
  Horaires (HH:MM → HH:MM) .........
  Tickets max/jour .................
  Durée moyenne/service (min) ......
  Nombre de guichets ...............

SERVICES (nom + préfixe 1-5 lettres + durée moyenne)
  1. ...............................  préfixe ....
  2. ...............................  préfixe ....
  3. ...............................  préfixe ....

GUICHETS (nom)
  1. ...............................

COMPTES
  Admin org   : nom / email / mot de passe
  Admin agence: nom / email / mot de passe
  Agents      : nom / email / mot de passe / guichet
```

---

## H. Format JSON prêt à l'emploi (provisionnement automatique)

Le backend sait créer toute l'organisation en une commande à partir de ce
fichier — modèle fourni dans `bootstrap/pilot.example.json`.

```json
{
  "organization": {
    "name": "Banque Coopec Burundi",
    "slug": "banque-coopec",
    "sector": "bank",
    "description": "Réseau d'agences",
    "country": "Burundi",
    "city": "Bujumbura",
    "phone": "+25722200000",
    "email": "contact@coopec.bi"
  },
  "branch": {
    "name": "Agence Principale",
    "slug": "agence-principale",
    "description": "Siège",
    "address": "Avenue de la Mission, n°12",
    "city": "Bujumbura",
    "opens_at": "07:30",
    "closes_at": "16:30",
    "max_daily_tickets": 800,
    "avg_service_minutes": 7,
    "is_open": true
  },
  "services": [
    { "name": "Opérations", "ticket_prefix": "A", "avg_duration_minutes": 6 },
    { "name": "Crédits",    "ticket_prefix": "B", "avg_duration_minutes": 15 }
  ],
  "counters": [
    { "name": "Guichet 1" },
    { "name": "Guichet 2" }
  ],
  "users": [
    { "name": "Admin Organisation", "email": "admin@coopec.bi", "role": "admin_org",
      "password_env": "BOOTSTRAP_ADMIN_PASSWORD" },
    { "name": "Agent Guichet 1", "email": "agent1@coopec.bi", "role": "agent",
      "counter": "Guichet 1", "password_env": "BOOTSTRAP_AGENT1_PASSWORD" }
  ]
}
```

Les mots de passe ne sont **jamais** écrits dans le fichier : ils sont lus
depuis des variables d'environnement (`password_env`).

---

## I. Ordre de création via l'API (si fait manuellement)

1. `POST /api/v1/auth/register/email` — créer le compte du futur `admin_org`
2. `POST /api/v1/organizations` — **token `super_admin` requis**
3. `POST /api/v1/organizations/{org_id}/employees` — rattacher le compte avec
   `role: "admin_org"`
4. `POST /api/v1/organizations/{org_id}/branches` — créer chaque agence
5. `PATCH /api/v1/organizations/{org_id}/branches/{branch_id}/config` — horaires,
   seuils, langues
6. `POST /api/v1/organizations/{org_id}/branches/{branch_id}/services` — les files
7. `POST /api/v1/organizations/{org_id}/branches/{branch_id}/counters` — les guichets
8. `POST /api/v1/organizations/{org_id}/employees` — agents, superviseurs, admins d'agence
9. `POST /api/v1/organizations/{org_id}/invitation-code` — code d'invitation
   pour que les usagers rejoignent l'organisation sans intervention manuelle

---

## J. Points à décider avec l'organisation

- **Préfixes de service** : à figer maintenant, non modifiables ensuite.
- **Découpage des agences** : une Branch = un lieu physique avec sa propre file.
- **Durée moyenne par service** : sert au calcul de l'attente estimée ; une
  valeur trop optimiste donne des ETA irréalistes les premières semaines.
- **Plafond de tickets/jour** : protège contre les abus, mais bloque les
  clients au-delà du seuil.
- **SMS** : nécessite un compte Africa's Talking actif ; sans clés, les
  notifications restent in-app.

---

## K. Exemple réel : l'organisation ULBU (pilote)

Le fichier `bootstrap/ulbu.json` contient la configuration complète de
l'Université Lumière de Bujumbura, validée puis exécutée sur une base vierge :

```
organizations=1  branches=1  services=2  counters=2  users=4  employees=3
```

**Deux pièges rencontrés dans le brouillon initial**, à connaître avant de
préparer le fichier d'une nouvelle organisation :

| Symptôme | Cause | Correction |
|---|---|---|
| Le logo n'apparaît jamais | Le champ s'appelle `logo_url`, pas `logo`. Un champ inconnu est ignoré sans erreur. | `"logo_url": "https://…"` |
| Mot de passe ignoré, un mot de passe aléatoire est généré | `password_env` attend le **nom d'une variable d'environnement**, pas le mot de passe lui-même. `os.getenv("ULBU_Admin@156")` renvoie `None`. | `"password_env": "ULBU_ADMIN_PASSWORD"` puis `export ULBU_ADMIN_PASSWORD='…'` |

### Création sur une base existante

```bash
export ULBU_ADMIN_PASSWORD='le-mot-de-passe'
python -m app.cli bootstrap --config bootstrap/ulbu.json --dry-run   # vérifier
python -m app.cli bootstrap --config bootstrap/ulbu.json            # créer
```

Le bootstrap est **idempotent** : relancé, il ne crée pas de doublon et ne
change pas les mots de passe existants. Il ne complète que les champs vides
d'une fiche déjà présente.

### Création automatique sur Render (sans shell)

Le plan gratuit n'offre ni shell ni `preDeployCommand`. Le point d'entrée
`docker-entrypoint.sh` lit deux variables d'environnement au démarrage :

- `BOOTSTRAP_CONFIG_JSON` — le contenu de `ulbu.json` sur **une seule ligne**
- `BOOTSTRAP_PASSWORDS_JSON` — `{"admin@ulbu.bi":"…","agent1@ulbu.bi":"…"}`

Aucun mot de passe ne doit figurer dans `BOOTSTRAP_CONFIG_JSON` : il est lisible
dans le dashboard. Les deux variables sont lues à chaque redémarrage, donc le
bootstrap se rejoue — sans effet de bord.

### Vérification après création

```bash
curl -X POST https://<service>.onrender.com/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@ulbu.bi","password":"…"}'
```

La réponse doit contenir `user.org_id` et `user.memberships` avec le
`branch_id` — et le `counter_id` pour un agent. C'est ce bloc `memberships` qui
permet au poste desktop d'ouvrir directement le bon guichet.

> Le chemin exact est `/api/v1/auth/login`. `/openapi.json` et `/docs` renvoient
> 404 en production (`DEBUG=false`), il ne faut pas s'en servir pour explorer.
