# Tâches backend — Charle

**Document de travail.** Il décrit ce que tu reprends, dans quel ordre, et à
quelles conditions le travail est accepté. Il ne remplace pas le backlog
(`docs/BACKLOG-BACKEND-2026-11-26.md`), qui explique le *pourquoi* de chaque
chantier : lis-le une fois avant de commencer.

**Base de départ :** `main`, 26/11/2026.
**Livraison :** une branche par lot, une PR par lot. Tu ne merges pas toi-même.

---

## 1. Ce que tu reprends

TONDE est une plateforme de gestion de files d'attente pour les banques,
hôpitaux, universités et administrations du Burundi et de la RDC. Trois
produits : ce backend (FastAPI), une application mobile Flutter pour le client
final, un poste guichetier Tauri pour l'agent.

Le backend n'est **pas** un projet à finir. Les phases 0 à 9 sont livrées :
22 500 lignes d'application, 16 routers, 68 fichiers de test (18 300 lignes),
migrations jusqu'à `023`. Les invariants importants sont en base (index unique
partiel « un seul ticket actif par utilisateur », compare-and-set, outbox), et
les cas dégradés sont traités explicitement (Redis absent, capacité inconnue,
rejeu après crash).

Ton travail n'est donc pas d'ajouter des routes au hasard : c'est de **fermer
des angles morts** sans casser ce qui tient. Les quatre lots ci-dessous sont
classés par ordre d'importance réelle, pas par difficulté.

---

## 2. Mise en route

```bash
git clone https://github.com/tonde-sys/tonde-backend.git
cd tonde-backend
cp .env.example .env

# PostgreSQL 15 + Redis 7 seulement (garde l'API sur ton poste,
# c'est là que tu veux voir les tracebacks)
docker compose up -d db redis
# … ou tout, y compris l'API conteneurisée :
# docker compose up -d

# Python 3.12 obligatoire (la CI y tourne)
python -m venv .venv
source .venv/Scripts/activate        # Windows
# source .venv/bin/activate          # Linux / macOS
pip install -r requirements-dev.txt

alembic upgrade head

# Sans cette variable, les tests PostgreSQL (contraintes, index partiels,
# clés étrangères) sont SKIPPÉS sans le dire — et « tout vert » ne veut
# plus rien dire. La CI la définit, fais pareil en local.
export PG_TEST_URL="postgresql+asyncpg://tonde:tonde123@localhost:5432/tonde_db"
# PowerShell : $env:PG_TEST_URL = "postgresql+asyncpg://tonde:tonde123@localhost:5432/tonde_db"

pytest --tb=short -q
```

Attendu : tout vert, sans exception. Si un test échoue sur une base neuve, c'est
un problème d'environnement, pas un test à ignorer — signale-le avant de coder.

**Piège connu :** le poste de référence a un `.venv` en Python **3.11** alors que
la CI exige 3.12. Une syntaxe 3.12 passerait la CI et casserait en local. Crée
ton venv en 3.12.

**Redis :** Docker l'expose sur **6380** (pour ne pas entrer en conflit avec un
Redis déjà installé sur le poste). `.env.example` est à jour ; si tu avais un
`.env` ancien avec 6379, l'API ne verra pas Redis et le temps réel tombera en
silence sur le repli polling.

**À lire avant de toucher au code** (une heure, pas plus) :
`README.md` · `docs/PLAN-DEV.md` (les décisions et leurs raisons) ·
`docs/AUDIT-ECOSYSTEME-TONDE-2026-11-22.md` §3 · `app/main.py` ·
`tests/conftest.py` (comment les tests obtiennent une base).

---

## 3. Règles de collaboration

Elles ne sont pas décoratives : chacune correspond à un incident évité.

1. **Jamais de commit sur `main`.** Une branche par lot :
   `feat/suppression-compte`, `feat/feedback-admin`,
   `feat/notifications-langue`, `perf/lecture-sans-ecriture`.
2. **Une PR par lot.** Pas une PR de 40 fichiers : je ne peux pas la relire, donc
   je ne peux pas la valider.
3. **`pytest` vert avant de pousser.** La CI se déclenche sur chaque `feat/**`.
   Un test rouge en PR n'est pas « presque fini », c'est non livré.
4. **Une migration se teste dans les deux sens** : `alembic upgrade head` puis
   `alembic downgrade -1` sur une base vide. Une migration non réversible bloque
   un retour arrière en production.
5. **Pas de nouvelle dépendance sans justification** dans la description de PR.
   Chaque paquet ajouté est une surface de mise à jour et de faille.
6. **Ne touche pas au moteur de file** (`qape_*`, absence, outbox, verrous Redis,
   `queue_scheduler`) sans m'en parler avant. Les invariants y sont structurels
   et une « simplification » peut faire passer deux clients sur le même guichet.
7. **Commentaires en français, et ils expliquent *pourquoi*.** Le code dit déjà
   ce qu'il fait ; le commentaire dit quel problème il évite.
8. **Si une consigne est ambiguë, écris la question dans la PR.** Ne devine pas
   en silence : un choix non discuté coûte plus cher qu'une question.

---

## 4. Les lots

### Lot 1 — Suppression de compte et export des données · 2 à 3 jours

**Pourquoi.** Google Play refuse la fiche d'une application avec comptes qui
n'offre pas de chemin de suppression. Aujourd'hui `DELETE /auth/me` n'existe pas.
C'est aussi la première question d'un client institutionnel.

**Le piège à ne pas rater.** `tickets.user_id` est
`ForeignKey("users.id", ondelete="CASCADE")` (`app/models/ticket.py:162`).
Une suppression réelle **efface donc tous les tickets de l'utilisateur** — et
avec eux les statistiques de l'institution, qui ne lui appartiennent pas. Le
même cascade existe sur `feedback.user_id`.

**Approche retenue : anonymisation, pas suppression.** La ligne `users` reste,
vidée de toute donnée identifiante. Les tickets restent rattachés à un compte
fantôme, donc les compteurs de l'agence restent justes.

**À faire.**

- `DELETE /api/v1/auth/me`, corps :
  ```json
  { "confirm": "SUPPRIMER", "password": "…", "otp": "…" }
  ```
  `confirm` obligatoire (protection contre un appel accidentel). Authentification
  supplémentaire : `password` si le compte en a un, sinon `otp` (réutilise
  l'infrastructure OTP existante, `app/core/redis.py`).
- Dans une **seule transaction** :
  - annuler le ticket actif s'il y en a un (réutilise la logique d'annulation
    existante : la file doit être mise à jour, pas seulement la ligne SQL) ;
  - `is_active = False`, `hashed_password = None`, `email = None`, `phone = None`,
    `name = "Compte supprimé"`, `photo_url = None`, `fcm_token = None` ;
  - supprimer les lignes `user_devices` de ce compte ;
  - révoquer tous les `refresh_tokens` ;
  - écrire une ligne dans `audit_logs`.
- `GET /api/v1/auth/me/export` : JSON de tout ce qui concerne l'utilisateur
  (profil, tickets avec numéro/statut/date, retours, nombre de notifications).
  Aucune donnée d'un autre compte, aucun jeton.

**Critères d'acceptation.**
- Après suppression, la connexion échoue avec un code métier explicite, pas une
  500 ni « identifiants invalides ».
- Le téléphone libéré peut servir à créer un nouveau compte (contrainte unique).
- Les tickets restent comptés dans `analytics`, avec une identité vide.
- Aucune ligne ne conserve le téléphone ou l'e-mail en clair.

**Tests à écrire.** Suppression avec mot de passe · avec OTP · `confirm` absent
(400) · mot de passe faux (401) · ticket actif annulé et file cohérente ·
connexion impossible après · export ne contenant que ses données.

**Fichiers.** `app/routers/auth.py`, `app/services/auth_service.py`,
`app/schemas/auth.py`, éventuellement une migration si tu ajoutes un champ
(`deleted_at` est un bon candidat).

---

### Lot 2 — File de traitement des retours · 1 à 2 jours

**Pourquoi.** `FeedbackStatus` existe (`RECEIVED` / `READ` / `RESOLVED`) mais
**rien ne le fait jamais évoluer** — vérifiable : `grep -rn "FeedbackStatus.READ"
app/` ne renvoie rien. Les retours arrivent par e-mail et disparaissent du
produit. Le client, lui, lit « reçu » indéfiniment.

**À faire.**

- `GET /api/v1/feedback` — réservé à `SUPER_ADMIN` (l'équipe TONDE, cf.
  `UserRole` dans `app/models/user.py:20`). Paginé, filtres `status`,
  `category`, `org_id`, `from`, `to`. Tri par date décroissante par défaut.
- `PATCH /api/v1/feedback/{id}` — change le statut. Transitions autorisées :
  `RECEIVED → READ`, `READ → RESOLVED`, `RECEIVED → RESOLVED`. Le premier
  passage à `READ` renseigne `read_at`. Toute transition est tracée dans
  `audit_logs` (qui, quand, avant, après).
- `GET /api/v1/feedback/stats` — volume par catégorie et par statut, délai médian
  entre `created_at` et `read_at`, sur une période.
- Ne change **pas** la forme de `GET /feedback/mine` : le mobile la consomme.

**Critères d'acceptation.**
- Un `CLIENT`, un `AGENT` ou un `ADMIN_ORG` reçoit `403` sur les trois routes.
- Une transition non autorisée (`RESOLVED → READ`) renvoie `409` avec un code
  métier, pas une exception SQL.
- Le client voit son statut changer dans `/feedback/mine` sans redéploiement.

**Tests à écrire.** Garde de rôle (3 rôles refusés) · chaque transition ·
transition interdite · `read_at` renseigné une seule fois · pagination ·
filtres combinés · statistiques sur un jeu de données connu.

**Fichiers.** `app/routers/feedback.py`, `app/services/feedback_service.py`,
`app/schemas/feedback.py`.

---

### Lot 3 — Notifications dans la langue du client · 2 à 3 jours

**Pourquoi.** `users.language` **existe déjà** (`app/models/user.py:64`, défaut
`fr`), est accepté par `PATCH /auth/me` et renvoyé à la connexion — mais
`notification_service.py` ne le lit **jamais**. Tous les textes sont des
f-strings françaises codées en dur. Un client qui utilise l'application en
kirundi reçoit son alerte d'appel en français, au moment le plus important. Et
un SMS français dépasse souvent 160 caractères, donc coûte deux segments.

**À faire.**

- Créer `app/services/notification_templates.py` : un catalogue par événement ×
  langue (`fr`, `en`, `rn`), pour `ticket_called`, `absent_warning`, `absent`,
  `expired`, `cancelled`, `transferred`, `done`, `long_wait`.
- Chaque méthode `notify_*` lit `user.language` (l'utilisateur est déjà chargé
  par `_get_user`, aucun aller-retour supplémentaire).
- Repli : langue absente ou inconnue → français. Jamais de message vide, jamais
  de clé manquante silencieuse.
- Compter les segments GSM-7 avant envoi et journaliser le coût estimé
  (`nombre de segments × nombre de SMS`). Un message de plus de 2 segments doit
  produire un avertissement dans les logs, sans bloquer l'envoi.
- Vérifier que `language` est bien renseigné à l'inscription par téléphone et par
  e-mail (`bootstrap_service.py:582` force `"fr"` — à vérifier, pas à changer
  sans raison).

**Critères d'acceptation.**
- Un utilisateur `rn` reçoit le SMS et le push en kirundi.
- Les trois langues tiennent en **un seul segment** pour chaque événement.
- Un test échoue si un événement n'a pas ses trois traductions (complétude du
  catalogue), et un autre si un texte français subsiste hors du catalogue.

**Tests à écrire.** Test paramétré événement × langue · repli sur langue
inconnue · comptage de segments (message court, message long) · absence de texte
en dur.

**Fichiers.** `app/services/notification_service.py`,
`app/services/notification_templates.py` (nouveau),
`app/services/sms_gateway.py`.

---

### Lot 4 — Sortir l'écriture du chemin de lecture · 2 à 3 jours

**Pourquoi.** Deux défauts mesurés dans l'audit du 22/11 :

- `TicketService._build_response` fait un `commit()` à chaque `GET` d'un ticket
  `WAITING` (`app/services/ticket_service.py:2978`) ;
- `_persist_forecasts` prend un `SELECT … FOR UPDATE` sur la ligne `services`
  (`app/services/eta_service.py:607`).

L'écran de salle et le repli polling mobile tapent tous les deux ce chemin. À
30 clients en attente : ~2 écritures/seconde par service, sérialisées par un
verrou de ligne. C'est le défaut qui n'apparaît qu'en production, un vendredi
matin.

**À faire.**

- La réponse HTTP calcule la prévision **en mémoire** et ne persiste plus rien.
- La persistance devient une écriture séparée, déclenchée par le scheduler ou en
  tâche de fond, et **seulement si la minute estimée a changé** (la déduplication
  existe déjà).
- Supprimer le `FOR UPDATE` du chemin de lecture. S'il reste nécessaire côté
  écriture, il reste — mais plus sur un `GET`.

**Contrat à ne pas casser.** `estimated_wait_minutes: null` signifie « aucune
prévision fiable » : le mobile affiche « Estimation en cours », jamais 0 min. Ne
transforme pas un `null` en `0` pour faire passer un test.

**Critères d'acceptation.**
- Un `GET /tickets/{id}` sur un ticket `WAITING` n'émet plus aucun `INSERT` /
  `UPDATE` (à prouver par un test qui compte les ordres SQL, pas par lecture).
- Les tests ETA existants passent sans modification de leur contrat.
- La valeur renvoyée reste identique à ce que produisait l'ancien chemin, à la
  même seconde.

**Tests à écrire.** Absence d'écriture sur `GET` · prévision inchangée quand rien
ne bouge · prévision recalculée quand la file change · `null` préservé.

**Fichiers.** `app/services/ticket_service.py`, `app/services/eta_service.py`,
`app/services/queue_scheduler.py` si la persistance y déménage.

---

## 5. Définition de « terminé »

Un lot est terminé quand **tout** ce qui suit est vrai :

- [ ] `pytest --tb=short -q` vert en local, sans nouveau test `skip`.
- [ ] `alembic upgrade head` puis `alembic downgrade -1` passent sur une base vide.
- [ ] Les tests ajoutés échouent si on retire la fonctionnalité (je le vérifie).
- [ ] Aucun `print`, aucun `TODO` laissé dans le code livré.
- [ ] La description de PR explique **ce qui casse si on ne fait pas ça**.
- [ ] Aucune donnée d'une organisation visible depuis une autre (`org_id`).

## 6. Ce que je regarde en revue

Autant le dire tout de suite, ça t'évitera un aller-retour :

1. **Les frontières multi-tenant.** Toute requête qui touche une table métier
   filtre-t-elle par `org_id` (ou par une branche de l'org) ?
2. **Les transactions.** Une opération en plusieurs étapes est-elle atomique ?
   Que se passe-t-il si la deuxième échoue ?
3. **Les erreurs.** Codes métier explicites, messages en français, pas de 500
   pour un cas prévu.
4. **Les tests.** Testent-ils la règle, ou seulement le chemin heureux ?
5. **Les migrations.** Réversibles, indexées, sans verrou long sur une table
   chaude.
6. **Le style existant.** Ce dépôt a des conventions (services plutôt que
   routers, commentaires qui expliquent le pourquoi). Les suivre vaut mieux
   qu'importer les tiennes.

## 7. Livraison d'un lot

```bash
git checkout main && git pull
git checkout -b feat/suppression-compte
# … travail …
git add -A
git commit -m "feat(auth): suppression de compte par anonymisation"
git push -u origin feat/suppression-compte
```

Puis une PR vers `main`, avec :

- **Ce que ça change** (3 lignes).
- **Pourquoi** (le problème, pas la solution).
- **Comment le vérifier à la main** (les commandes exactes).
- **Ce que ça casse si on ne le fait pas.**
- **Les risques** que tu as identifiés, même si tu les as écartés.

La CI doit être verte. Je relis, je commente, tu corriges, puis je merge. Tu ne
merges pas toi-même, même si la CI est verte.

## 8. Après ces quatre lots

Ne les commence pas avant que les précédents soient mergés : ils dépendent de
décisions qui peuvent bouger.

- **Dispatch serveur** (`call-next` sans `service_id`) puis **`counter_services`** —
  les deux vont ensemble, dans cet ordre.
- **Rétention et purge** de `queue_logs`, `audit_logs`, `notifications`.
- **Observabilité** : `GET /metrics` au format Prometheus (les compteurs existent
  déjà dans `app/core/metrics.py`, ils ne sont simplement exposés nulle part) et
  remontée d'erreurs.
- **Découpage de `TicketService`** (3 619 lignes) — gros chantier, à faire quand
  tout le reste est stable.

---

**Une dernière chose.** Si en lisant le code tu trouves un défaut qui n'est pas
dans ce document, ne le corrige pas au passage dans une PR de lot : ouvre une
issue ou écris-le dans la PR. Un correctif opportuniste dans une PR de 12
fichiers est un correctif que je ne verrai pas.
