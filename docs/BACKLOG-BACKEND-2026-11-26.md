# Backlog backend — état au 26 novembre 2026

Ce document liste ce qui reste **réellement** à faire côté `tonde-backend`, dans
l'ordre où je le ferais. Chaque entrée dit le problème, pourquoi il compte, où
il se trouve dans le code, et à quoi ressemble la fin.

Il ne remplace pas `docs/PLAN-DEV.md` (les phases 0 à 9 sont livrées) : il prend
la suite. Les points issus de `AUDIT-ECOSYSTEME-TONDE-2026-11-22.md` sont
rappelés avec leur numéro d'origine (B1, B2, D1…).

**Base de départ :** 22 500 lignes d'application, 18 300 lignes de tests,
68 fichiers de test, 16 routers, migration `023_user_devices`. Le socle est
sain : les invariants sont en base (index unique partiel, compare-and-set), les
cas dégradés sont traités, l'isolation `org_id` est systématique. Le travail qui
reste est moins « ajouter des routes » que « fermer des angles morts ».

---

## P0 — à faire avant un pilote réel

### 1. Suppression de compte et export des données personnelles

**Problème.** Il n'existe aucun moyen de supprimer un compte. `grep -rn "delete.*user"
app/routers/auth.py app/services/auth_service.py` ne renvoie rien.

**Pourquoi ça compte.** Google Play impose, depuis 2024, un chemin de suppression
de compte accessible **dans l'app et hors de l'app** pour toute application
permettant de créer un compte. Sans lui, la fiche Play Store est refusée — donc
l'APK publié en release reste distribution directe. C'est aussi la première
question d'un client institutionnel qui signe un contrat.

**Tâche.**
- `DELETE /api/v1/auth/me` : supprime le compte du titulaire après confirmation
  par mot de passe (ou OTP pour les comptes téléphone).
- Décider ce qui survit : les tickets déjà servis doivent rester dans les
  statistiques de l'institution, mais **sans identité**. Anonymiser
  (`user_id` → `NULL`, nom/téléphone effacés) plutôt que supprimer en cascade.
- `GET /api/v1/auth/me/export` : JSON de tout ce qui concerne l'utilisateur
  (profil, tickets, retours, notifications).
- Page publique de demande de suppression (lien à mettre dans la fiche Play).

**Critères d'acceptation.**
- Un compte supprimé ne peut plus se connecter, son jeton est révoqué partout.
- Les tickets restent comptés dans `analytics`, avec une identité vide.
- Un test vérifie qu'aucune ligne ne conserve le téléphone en clair.

**Fichiers.** `app/routers/auth.py`, `app/services/auth_service.py`,
`app/models/user.py`, migration `024`.

**Estimation.** 2 à 3 jours.

---

### 2. Retours utilisateurs : la file de traitement n'existe pas

**Problème.** `feedback.py` expose deux routes : `POST /feedback` et
`GET /feedback/mine`. Le modèle a un `FeedbackStatus` (`RECEIVED` / `READ` /
`RESOLVED`) que **rien ne fait jamais évoluer**. Les retours arrivent par e-mail
à `FEEDBACK_INBOX`, et c'est tout : pas de liste, pas de filtre, pas de réponse,
pas de marquage. Un retour lu dans Gmail n'existe plus pour le produit.

**Pourquoi ça compte.** Le centre d'aide promet au client « votre retour est
pris en compte ». Aujourd'hui, la seule trace exploitable est une boîte mail
personnelle. Impossible de mesurer combien de retours sont traités, ni de
répondre depuis l'outil.

**Tâche.**
- `GET /api/v1/feedback` (staff TONDE, paginé, filtres `status`, `category`,
  `org_id`, `from`/`to`).
- `PATCH /api/v1/feedback/{id}` : `status`, note interne.
- `GET /api/v1/feedback/stats` : volume par catégorie et par semaine, délai
  médian de traitement.
- Un compte staff TONDE (rôle global, hors organisation) — voir si le modèle
  `Employee` le permet ou s'il faut un rôle dédié.

**Critères d'acceptation.**
- Un retour passe `RECEIVED → READ → RESOLVED` et l'historique est dans
  `audit_logs`.
- Un utilisateur non staff reçoit `403`.
- Le client voit le statut de son retour dans `GET /feedback/mine`.

**Fichiers.** `app/routers/feedback.py`, `app/services/feedback_service.py`,
`app/schemas/feedback.py`.

**Estimation.** 1 à 2 jours.

---

### 3. Les notifications ne parlent qu'en français

**Problème.** `users` n'a **aucune colonne `locale`**. Tous les textes de
`notification_service.py` sont des f-strings françaises codées en dur
(`f"Ticket {ticket.number} appelé"`), et partent tels quels en push **et en SMS**.

**Pourquoi ça compte.** L'application est traduite en français, anglais et
kirundi. Un client de Bujumbura qui utilise l'app en kirundi reçoit un SMS
français au moment le plus important — celui où son numéro est appelé. Sur un
public qui lit peu le français, ce n'est pas un détail de confort : c'est le
canal d'alerte qui échoue. Et un SMS français dépasse souvent 160 caractères,
donc coûte deux segments.

**Tâche.**
- Migration : `users.locale` (`fr` par défaut, `en`, `rn`).
- Le mobile envoie sa langue à l'inscription et dans `PATCH /auth/me`.
- Catalogue de messages par événement × langue (`your_turn`, `absent_warning`,
  `expired`, `cancelled`, `transferred`, `done`), hors base de code ou dans un
  module `notification_templates.py`.
- Compter les segments GSM-7 avant envoi ; journaliser le coût estimé par SMS.
- Repli : langue inconnue → français, jamais de message vide.

**Critères d'acceptation.**
- Un utilisateur `rn` reçoit le SMS en kirundi.
- Le corps SMS tient en un segment pour les trois langues (test paramétré).
- Un test vérifie qu'aucun texte n'est codé en dur hors du catalogue.

**Fichiers.** `app/services/notification_service.py`, `app/models/user.py`,
`app/schemas/auth.py`, `app/services/sms_gateway.py`.

**Estimation.** 3 à 4 jours.

---

### 4. Une lecture qui écrit (B2/B3)

**Problème.** `TicketService._build_response` fait un `commit()` à chaque `GET`
d'un ticket `WAITING` (`ticket_service.py:2978`), et `_persist_forecasts` prend
un `SELECT … FOR UPDATE` sur la ligne `services` (`eta_service.py:607`).

**Pourquoi ça compte.** L'écran de salle et le repli polling mobile tapent tous
les deux ce chemin. À 30 clients en attente, cela fait ~2 écritures/seconde par
service, sérialisées par un verrou de ligne. C'est soutenable pour un pilote,
pas pour une agence chargée — et c'est le genre de défaut qui n'apparaît qu'en
production, un vendredi matin.

**Tâche.** Ne persister la prévision que si la minute estimée change réellement
(la déduplication existe déjà), et sortir le `FOR UPDATE` du chemin de lecture :
la mise à jour de la prévision devient une écriture séparée, faite par le
scheduler ou en tâche de fond.

**Critères d'acceptation.**
- Un `GET /tickets/{id}` sur un ticket `WAITING` n'ouvre plus de transaction
  d'écriture (vérifiable par les logs SQL en test).
- Les tests ETA existants passent sans modification de leur contrat.

**Fichiers.** `app/services/ticket_service.py`, `app/services/eta_service.py`.

**Estimation.** 2 à 3 jours.

---

## P1 — produit vendable

### 5. Le serveur arbitre le service à appeler (D1)

`CallNextRequest.service_id` est obligatoire (`app/schemas/ticket.py:186`). Le
desktop doit donc savoir quel service appeler — une règle métier dans le client,
et un risque d'appel hors compétence du guichetier. Rendre le champ optionnel :
le serveur choisit selon la priorité puis l'ancienneté. **1 à 2 jours.**

### 6. Affectation guichet ↔ services (D2)

Aucune table `counter_services` : un guichet ne sait pas quels services il peut
servir. Nécessaire pour que le dispatch du point 5 ait un sens (un guichet
« dépôts » ne doit pas appeler la file « crédits »). Modèle + endpoints +
validation + tests. Le desktop suivra. **3 à 4 jours.**

### 7. Rétention et purge

`queue_logs`, `audit_logs` et `notifications` grossissent indéfiniment. Aucune
purge n'existe (`grep -rn "purge\|retention" app/` ne renvoie que des
commentaires). À 10 agences, `queue_logs` prend des millions de lignes la
première année.

**Tâche.** Politique explicite et configurable : `queue_logs` agrégés puis
purgés au-delà de N mois, `notifications` au-delà de 90 jours, `audit_logs`
conservés plus longtemps (obligation de traçabilité). Commande CLI idempotente
(`python -m app.cli purge --before … --dry-run`), jamais automatique sans
`--apply`. **2 jours.**

### 8. Observabilité : métriques et erreurs

`app/core/metrics.py` compte déjà les requêtes en mémoire, mais **aucun endpoint
ne les expose** : rien ne peut être scrappé par Prometheus, et une instance
redémarrée perd tout. Aucune remontée d'exception non plus (`grep sentry` →
vide) : une erreur 500 en production ne laisse qu'une ligne de log locale.

**Tâche.** `GET /metrics` (format Prometheus, protégé par jeton ou restreint au
réseau interne), et Sentry (ou équivalent) branché sur les handlers d'exception.
**1 jour.**

### 9. Rapport quotidien : il manque le déclencheur

`app/cli.py` sait produire et envoyer le rapport (`deliver_reports`), mais rien
ne le déclenche : c'est un cron à configurer au déploiement, et ce n'est écrit
nulle part. Soit on documente le cron dans le README de déploiement, soit on le
confie au `QueueScheduler` déjà présent. **0,5 à 1 jour.**

---

## P2 — industrialisation

### 10. Découper `TicketService` (B1)

3 610 lignes, 60+ méthodes, 6 responsabilités. Cible : `QueueEngine`,
`AbsenceService`, `TicketQueries`, `QapeTicketCreation`, aucun fichier > 800
lignes. **Gros chantier, à faire quand la couverture de tests est stable** :
5 à 8 jours, sans changement de contrat.

### 11. Ticket WebSocket éphémère

Le JWT passe aujourd'hui en query string pour le WS, donc dans les logs des
proxys. Émettre un ticket court (30 s, usage unique) via `POST
/auth/ws-ticket`, et l'utiliser à la connexion. **1 à 2 jours.**

### 12. Contrat généré depuis OpenAPI

Le backend produit `openapi.json`. Générer les types TypeScript (desktop) et
Dart (mobile) depuis ce fichier, et faire échouer la CI quand un champ requis
disparaît. `tests/test_phase9_mobile_contract.py` est le bon réflexe, il manque
le pendant côté clients. **2 à 3 jours.**

### 13. Python 3.12 partout (B6)

La CI tourne en 3.12, le `.venv` local en 3.11.16. Une syntaxe 3.12 passerait la
CI et casserait en local (ou l'inverse). Recréer le venv, épingler la version
dans `.python-version`. **0,5 jour.**

### 14. Historique Alembic (B5)

Une seule tête (`023_user_devices`), mais deux racines fusionnées et un nommage
hétérogène (hashes + numéros). Cosmétique, tant que la base est jeune — à faire
avant la première base cliente, après ce sera impossible. **1 jour.**

### 15. Limitation WebSocket par utilisateur (B7)

La limite est par IP (10/min). Toute une salle d'attente derrière un NAT peut
être bloquée. Passer à une limite par compte authentifié, avec un plafond par IP
plus haut en secours. **1 jour.**

---

## Quick wins (moins d'une journée chacun)

- **B4** — `_count_waiting_in_db` sans filtre `org_id`
  (`ticket_service.py:3008`). Impact réel nul (`service_id` est globalement
  unique), mais la règle « org_id partout » doit rester vraie partout, sinon
  elle ne veut plus rien dire.
- **`/health/detailed`** expose déjà Redis et la base : y ajouter le nombre de
  tickets en attente par agence rend le diagnostic téléphonique possible sans
  accès base.
- **Journalisation du coût SMS** : compter les segments envoyés par jour et par
  organisation, avant même la localisation (point 3).

---

## Ce que je ne ferais pas maintenant

- **File d'attente multi-agences / multi-pays** : le modèle `org_id` le permet
  déjà, aucune urgence.
- **Webhooks sortants** : personne ne les demande encore. À rouvrir quand une
  institution voudra brancher TONDE sur son propre système.
- **Rendez-vous (pré-réservation)** : c'est un autre produit. Le moteur de file
  actuel ne sait pas représenter un ticket qui n'existe pas encore.

---

## Ordre recommandé

1. **Semaine 1** — points 1 et 2 (conformité Play Store, retours traitables).
2. **Semaine 2** — point 4 (B2/B3) puis 8 (observabilité) : on ne met pas en
   pilote sans savoir ce qui se passe.
3. **Semaines 3-4** — point 3 (localisation) : c'est le plus long, mais c'est
   celui qui change l'expérience du client final.
4. **Ensuite** — 5, 6, 7, puis le découpage `TicketService` quand le reste est
   stable.
