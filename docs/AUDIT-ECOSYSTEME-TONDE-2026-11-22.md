# Audit de l'écosystème TONDE — Backend · Desktop · Mobile

**Date :** 22 novembre 2026
**Périmètre :** `tonde-backend` (FastAPI), `tonde-desktop` (Tauri/React), `tonde-mobile` (Flutter)
**Hors périmètre :** `tonde-web` (Next.js — back-office + écran TV), mentionné seulement pour la cohérence documentaire
**Méthode :** lecture intégrale de la structure, des routers, des services critiques, des stores et des couches réseau ; exécution des suites de tests locales ; vérification croisée des contrats REST/WebSocket entre les trois clients.

---

## 1. Résumé exécutif

TONDE est une plateforme SaaS B2B multi-tenant de gestion de files d'attente pour le Burundi et l'Afrique de l'Est. L'écosystème se compose de trois applications qui parlent au même backend : une API FastAPI + PostgreSQL + Redis, un poste guichetier Tauri, une application citoyen Flutter.

**Verdict global : maturité backend très supérieure à la moyenne du marché à ce stade ; les deux clients sont en retard sur le contrat que le backend expose déjà.**

| Projet | Volume | Tests | État | Note |
|---|---:|---:|---|---|
| `tonde-backend` | 93 fichiers · 21 619 lignes | 707 fonctions de test (65 fichiers) | CI complète (PG + Redis + migrations + image) | **A−** |
| `tonde-desktop` | 46 fichiers · 8 625 lignes TS + ~1 000 lignes Rust | 55 tests (8 fichiers) | CI front + Rust ; 4 fichiers non commités | **B+** |
| `tonde-mobile` | 100 fichiers · 15 939 lignes Dart | 171 tests | `flutter analyze` : 0 problème ; **aucune CI** | **B** |

### Les cinq risques qui comptent

1. **Le mobile ne reçoit aucune notification push.** Le backend sait envoyer du FCM (`app/services/fcm_gateway.py`, `PUT /auth/me/fcm-token`), le mobile n'a ni `firebase_messaging` ni appel d'enregistrement. Un client qui ferme l'application pendant l'attente ne sera pas prévenu de son tour → `ABSENT` automatique à 180 s → retour en file → clôture au 2ᵉ échec. **C'est le scénario central du produit qui ne fonctionne pas.**
2. **Aucun lien guichet ↔ service.** Le modèle `Counter` n'a pas de relation vers `Service`. Le poste guichetier compense en choisissant lui-même « le service le plus chargé » (`apiService.ts:1246-1271`). Une règle de dispatch métier vit donc dans un client, et un agent « Dépôts » peut appeler un ticket « Retraits ».
3. **Le mobile n'implémente pas l'inscription par téléphone + OTP**, alors que le backend l'expose (`POST /auth/register/phone`, `POST /auth/verify-otp`) et que l'email est un canal minoritaire au Burundi. Blocage d'adoption.
4. **Le jeton JWT circule dans l'URL des WebSockets** des deux clients (`?token=…`). Il atterrit dans les logs d'accès, les proxys et l'historique navigateur.
5. **`TicketService` fait 3 610 lignes et 60+ méthodes** : moteur de file, absences, AWTE, QAPE, exports, réponses DTO. C'est le point unique de complexité du projet, et le principal risque de régression à mesure que l'équipe grandit.

---

## 2. Vue d'ensemble de l'écosystème

```
                    ┌──────────────────────────────┐
   Citoyen          │  tonde-mobile (Flutter)      │
   Android/iOS      │  Riverpod · Dio · Hive       │
                    │  FR / RN / EN                │
                    └───────────┬──────────────────┘
                                │ REST /api/v1 + WS /tickets/ws/queue/{id}
                                │ (JWT en query string)
                    ┌───────────▼──────────────────┐
   Guichetier       │  tonde-desktop (Tauri 2)     │
   Windows          │  React 19 · Zustand · SQLite │
                    │  outbox hors ligne · keyring │
                    └───────────┬──────────────────┘
                                │ REST + WS /tickets/ws/counter/{id}
                    ┌───────────▼──────────────────┐
                    │  tonde-backend (FastAPI)     │
                    │  ┌────────────────────────┐  │
                    │  │ Routers (16)           │  │
                    │  ├────────────────────────┤  │
                    │  │ Services (31)          │  │
                    │  │  Queue Engine          │  │
                    │  │  AWTE (ETA)            │  │
                    │  │  QAPE (anti-abus)      │  │
                    │  │  Scheduler             │  │
                    │  └────────────────────────┘  │
                    └──────┬───────────────┬───────┘
                           │               │
                  ┌────────▼──────┐  ┌─────▼──────────┐
                  │ PostgreSQL 15 │  │ Redis 7        │
                  │ vérité métier │  │ files + Pub/Sub│
                  └───────────────┘  └────────────────┘
```

**Flux temps réel.** Un service métier publie sur `tonde:events:{org_id}` (Redis Pub/Sub) → chaque instance de l'API relaie à ses WebSockets → les clients reçoivent `queue_update` (personnel), `ticket_called` (salle), `counter_queue_changed` (guichets). Une panne temps réel ne fait jamais échouer une opération métier : le client se resynchronise au prochain `GET` ou à la reconnexion (`send_snapshot()` à l'ouverture).

**Hiérarchie des données.** `Organization → Branch → Service → Counter → Ticket`, avec `org_id` sur chaque entité et un contrôle par branche en plus.

---

## 3. Backend — le socle solide

### 3.1 Points forts vérifiés

**Le moteur de file tient ses promesses, et le code le prouve.**

- Retrait atomique de la file Redis (`ZPOPMIN`) **puis** compare-and-set PostgreSQL (`UPDATE … WHERE status='WAITING'`, `ticket_service.py:2779-2803`) : un ticket ne peut pas être appelé deux fois, même si Redis et la base divergent.
- Si l'écriture échoue après le retrait, le ticket est **remis à son score d'origine** (`restore_to_queue`, `ticket_service.py:2841`) : aucun ticket perdu.
- L'invariant « un seul ticket actif par utilisateur » est **structurel**, pas applicatif : index unique partiel `uq_tickets_active_per_user` (`models/ticket.py:120-126`). Conséquence assumée et documentée : la création de ticket ne dépend plus de Redis.
- Ordre de passage via `queue_sequence` (BigInteger monotone, migration 013) au lieu de l'horodatage : deux tickets créés dans la même milliseconde ne sont plus départagés par un UUID.

**AWTE (estimation dynamique) est honnête.** EWMA persistée par service et par guichet, filtre MAD sur les échantillons, projection de charge sur les guichets ouverts et non en pause. Quand la capacité est inconnue, l'API renvoie `estimated_wait_minutes: null` et `confidence: unavailable` — elle **ne fabrique pas de durée** (`eta_service.py`, `docs/AWTE-PRODUCTION.md`). Les prévisions sont versionnées (`EtaPrediction.version`) et évaluées à l'appel effectif, avec MAE exposée par service et par guichet.

**QAPE (anti-abus) est de niveau industriel.** Intention durable en base avant tout appel Redis, fencing par génération de lease, idempotence par `client_action_id` + hash de payload canonique, reprise sur crash (`qape_fencing.py`, `qape_recovery.py`, `qape_idempotency.py`). Un rejeu ne crée jamais un second ticket, une décision refusée n'est jamais réévaluée silencieusement.

**Sécurité par défaut.** L'application **refuse de démarrer** en production avec un `JWT_SECRET_KEY` faible, `DEBUG=true`, `CREATE_TABLES_ON_STARTUP=true` ou `ALLOWED_ORIGINS=*` (`core/config.py:307-358`). `/docs`, `/redoc` et `/openapi.json` sont désactivés en production. OTP généré par `secrets`, haché, 3 tentatives, blocage 15 min ; un échec SMS renvoie `503` au lieu d'un faux succès.

**Qualité de la documentation interne.** Chaque décision non triviale est justifiée en commentaire par le problème qu'elle évite (« sans cet index, chaque passage balaie tous les tickets », « une échéance est une DONNÉE, pas un calcul refait à chaque lecture »). C'est rare et précieux pour la reprise du projet.

### 3.2 Dettes techniques

| # | Constat | Preuve | Impact |
|---|---|---|---|
| B1 | `TicketService` : 3 610 lignes, 60+ méthodes, 6 responsabilités | `app/services/ticket_service.py` | Revue difficile, couplage fort, tests lents |
| B2 | **Une lecture écrit.** `_build_response` fait `commit()` à chaque `GET` d'un ticket `WAITING` | `ticket_service.py:2968-2969` | Chaque consultation mobile / écran salle ouvre une transaction d'écriture |
| B3 | `_persist_forecasts` prend un `SELECT … FOR UPDATE` sur la ligne `services` | `eta_service.py:605-612` | Sérialise toutes les lectures d'un même service |
| B4 | `_count_waiting_in_db(service_id)` sans filtre `org_id` | `ticket_service.py:2999-3010` | Cohérence avec la règle « org_id partout » (impact réel nul : `service_id` est globalement unique) |
| B5 | Deux racines Alembic fusionnées (`c0d78e94182b`), nommage hétérogène (hashes + numéros) | `migrations/versions/` | Un seul head (`020_counter_pause_until`), mais historique peu lisible |
| B6 | Environnement local en Python 3.11.16 alors que README et CI exigent 3.12 | `.venv/` | Une syntaxe 3.12 passerait la CI et casserait en local (ou l'inverse) |
| B7 | Le taux de limitation WebSocket est par IP (10/min) | `core/config.py:173` | Une salle d'attente entière derrière un NAT peut être bloquée — déjà signalé dans la config, à surveiller en pilote |

**Sur B2/B3**, la mesure est simple : l'écran de salle (`/branches/{id}/display`) et le repli polling mobile (`GET /tickets/{id}` toutes les 15 s) tapent tous deux le même chemin. À 30 clients en attente, cela représente ~2 écritures/seconde par service, sérialisées par le verrou de ligne. Soutenable à l'échelle d'un pilote, à corriger avant la montée en charge. Piste : persister la prévision uniquement si la minute estimée change (la déduplication existe déjà, il manque de sortir le `FOR UPDATE` du chemin de lecture).

---

## 4. Desktop (poste guichetier) — bon produit, couverture de test mince

### 4.1 Points forts

- **Hors-ligne réellement pensé.** Outbox persistée en SQLite (Rust, WAL), rejeu au retour du réseau, 3 tentatives, échecs visibles avec « Réessayer » / « Ignorer » action par action (`store/offlineStore.ts`).
- **Le rejeu est rattaché à l'agent** qui a créé l'action (`belongsToActor`) : un agent qui se connecte sur le même poste ne rejoue jamais les actions de son prédécesseur. Détail rarement traité correctement.
- **Pas de ticket fantôme.** Sur timeout de `call-next`, le poste interroge le guichet et n'adopte un ticket que s'il est confirmé côté serveur (`reconcileCounterTicket`, `ticketStore.ts:82-98`).
- **Sécurité du jeton.** Coffre OS (Windows Credential Manager) via `keyring`, repli documenté sur fichier non chiffré, jamais de jeton en `localStorage` ni dans le state Zustand.
- **Le poste ne recalcule jamais une valeur serveur** : `grace_expires_at`, `recovery_expires_at`, `missed_count`, `queue_sequence` sont recopiés tels quels (`mapTicket`). C'est exactement la bonne posture.

### 4.2 Constats

| # | Constat | Preuve | Impact |
|---|---|---|---|
| D1 | **Le service à appeler est choisi par le client** : `display.services` trié par `waiting` décroissant, puis `call-next` sur le plus chargé | `services/api/apiService.ts:1246-1271` | Règle métier dans le client ; un guichet peut appeler n'importe quel service |
| D2 | `Counter.service_ids` est toujours `[]` (le backend n'a pas la relation) | `apiService.ts` `mapCounter` | L'UI ne peut pas filtrer ni afficher les services du guichet |
| D3 | `mapEmployee` renvoie `name: ""` et `email: ""` | `apiService.ts` `mapEmployee` | Liste d'employés inutilisable pour l'affichage |
| D4 | `TICKET_STATUS_MAP` mappe `transferred` → `CANCELLED` | `apiService.ts` | Sémantique perdue (transitoire côté serveur, donc rarement visible) |
| D5 | Priorités réduites à trois niveaux (0, 1, 2) : `vip` et `emergency` deviennent indiscernables | `types/index.ts:16` | L'agent ne distingue pas un VIP d'une urgence médicale |
| D6 | 4 fichiers modifiés non commités (`WebSocketManager.ts`, `offlineStore.ts`, `ShortcutsHelp.tsx`, nouveau test) | `git status` | Dérive par rapport à la CI ; risque de perte |
| D7 | 55 tests pour 8 625 lignes : aucun test de `counterStore`, `queueStore`, `AgentDashboard` | `npm test` : 8 fichiers | Les flux critiques (ouverture de guichet, transfert) ne sont pas couverts |
| D8 | `call-next` = 3 allers-retours (display → call-next → GET ticket) | `apiService.ts:1246-1301` | Sur un cold start Render (~30 s), l'appel devient très lent |
| D9 | Les entrées `RECALL_TICKET` de l'outbox sont jetées silencieusement au chargement | `offlineStore.ts:166-169` | Une relance hors ligne est perdue sans trace utilisateur |
| D10 | `COUNTER_OPEN` / `COUNTER_PAUSE` ne sont pas traités dans `_dispatch` | `WebSocketManager.ts:180-196` | L'en-tête de guichet peut afficher un statut périmé |

**Sur D1**, c'est le constat le plus structurant du desktop. Deux corrections possibles, par ordre de coût :
1. **Court terme** — rendre `service_id` optionnel dans `POST /tickets/counter/call-next` et laisser le serveur arbitrer (le backend a déjà `_get_callable_counter` et l'ordre de priorité) ;
2. **Cible** — ajouter `counter_services` (many-to-many) et faire respecter l'affectation côté serveur. Le desktop n'aurait plus qu'à appeler « le suivant » sans choisir.

---

## 5. Mobile (citoyen) — architecture propre, périmètre fonctionnel incomplet

### 5.1 Points forts

- **Architecture en couches nette** : `features/<domaine>/{data,domain,presentation}` + `core/` (config, réseau, offline, sécurité, erreurs, logging). Aucune violation détectée par `flutter analyze` (0 problème).
- **Trilinguisme complet** FR / RN (kirundi) / EN, avec un test qui vérifie que les trois ARB exposent **exactement les mêmes clés** (`test/l10n`). Le kirundi est natif, pas une traduction de façade.
- **Le WebSocket a un vrai plan B** : repli automatique sur `GET /tickets/{id}` toutes les 15 s, retente le socket tous les 4 cycles, `ping` toutes les 25 s, et chaque mise à jour porte sa **source** (`live` / `polling`) pour que l'écran puisse dire honnêtement « Direct » ou « Mode secours » (`websocket_queue_tracking_service.dart`).
- **L'ETA n'est jamais inventée à l'affichage** : `estimated_wait_minutes: null` → « Estimation en cours », jamais « 0 min » (`api_ticket_service.dart:158-163`).
- **Idempotence de création** : `client_action_id` généré côté mobile, et sur `TICKET_ALREADY_ACTIVE` le client va chercher le ticket existant au lieu d'afficher une erreur (`api_ticket_service.dart:62-77`).
- **Le renouvellement de jeton est mutualisé** : cinq 401 simultanés ne déclenchent qu'une rotation, et un échec réseau pendant le refresh ne déconnecte pas (`api_client.dart:126-170`).

### 5.2 Constats

| # | Constat | Preuve | Impact |
|---|---|---|---|
| M1 | **Aucune notification push.** Pas de `firebase_messaging`, pas d'appel `PUT /auth/me/fcm-token`. Uniquement `flutter_local_notifications` | `pubspec.yaml`, `local_notification_service.dart` | Le client rate son tour dès que l'app est en arrière-plan → `ABSENT` |
| M2 | **Pas de connexion par téléphone + OTP** malgré le backend prêt | `api_auth_service.dart` (email seul) | Barrière d'adoption au Burundi |
| M3 | **Ticket hors ligne avec numéro inventé** `L-<timestamp>` et position/ETA locales | `cached_ticket_service.dart:84-98` | Contredit la règle « ne jamais inventer » ; risque de conflit au guichet |
| M4 | `absent_warning` et `priority_updated` ignorés par le suivi | `websocket_queue_tracking_service.dart:207-234` | Le client n'est pas averti avant de devenir absent (seul le SMS reste) |
| M5 | `_closedStatuses = {'done','cancelled'}` duplique `ACTIVE_STATUSES` du serveur | `api_ticket_service.dart:16` | Si le backend ajoute un statut, le mobile dérive silencieusement `served` |
| M6 | **Aucune CI** : pas de `.github/workflows` | `.github/` ne contient qu'un `copilot-instructions.md` | Rien ne bloque une régression à la fusion |
| M7 | 1 test instable observé (170/171 au 1ᵉʳ run, 171/171 au 2ᵉ) | `flutter test` ×2 | Flakiness sur un test de widget — à isoler |
| M8 | 4 commits seulement, dont le branchement au backend réel le 20/11 | `git log` | Le mobile est très récent : le périmètre est incomplet par construction |
| M9 | `description: "A new Flutter project"` dans `pubspec.yaml` | `pubspec.yaml:2` | Détail de finition |

**Manques fonctionnels côté mobile** (comparés à ce que le backend expose déjà) : demande de priorité, retour en file après absence (`POST /tickets/{id}/return`), scan de QR (`qr_flutter` ne fait que générer), historique des tickets, réclamation d'un ticket après réinstallation, écran de salle.

---

## 6. Cohérence des contrats entre les trois projets

### 6.1 Ce qui est aligné (vérifié)

| Contrat | Backend | Desktop | Mobile |
|---|---|---|---|
| Création de ticket idempotente | `client_action_id`, index unique | n/a | ✅ généré et rejoué |
| Un seul ticket actif | index partiel + `TICKET_ALREADY_ACTIVE` | n/a | ✅ récupère le ticket existant |
| Absence et reprise | `grace_expires_at`, `recovery_expires_at`, `missed_count` | ✅ recopiés, jamais recalculés | ⚠️ non exploités |
| Pause de guichet | `paused_until`, `/pause`, `/resume` | ✅ utilisés (vrai endpoint) | ✅ affiché (« jusqu'à HH:MM ») |
| ETA nulle | `null` + `confidence` | ✅ `?? undefined` | ✅ « Estimation en cours » |
| WebSocket reprise | snapshot à la connexion | ✅ | ✅ |
| Isolation multi-tenant | `org_id` partout + branche | ✅ contexte org/branche | ✅ via JWT |

### 6.2 Ce qui dérive

**Il n'existe aucun contrat partagé généré.** Chaque client réécrit ses DTO et son mapping à la main (`BackendTicket` + `mapTicket` côté desktop, `ticketFromBackend` côté mobile). Les dérives déjà présentes :

- `transferred` → `CANCELLED` (desktop) vs statut ignoré (mobile) vs statut à part (backend) ;
- `priority` : 4 valeurs serveur, 3 niveaux desktop ;
- `service_ids` : champ desktop toujours vide, absent du modèle serveur ;
- `_closedStatuses` mobile dupliqué du `ACTIVE_STATUSES` serveur ;
- nommage des dépôts : le README backend annonce `tonde-web-admin`, le dossier réel est `tonde-web` (Next.js, qui contient bien le dashboard admin et l'écran TV).

**Recommandation :** générer les types clients depuis `openapi.json` (le backend le produit déjà hors production) et ajouter un test de contrat qui échoue quand un champ requis disparaît. Le backend a déjà `tests/test_phase9_mobile_contract.py` — c'est le bon réflexe, il manque le pendant côté clients.

---

## 7. Sécurité — revue ciblée

| Sujet | État | Détail |
|---|---|---|
| Secret JWT | ✅ | Refus de démarrage si < 32 caractères hors dev |
| OTP | ✅ | CSPRNG, haché, 3 essais, blocage 15 min, `503` si SMS échoue |
| Refresh token | ✅ | Rotation à chaque renouvellement, révocation en base, `/logout/all` |
| Multi-tenant | ✅ | `org_id` + contrôle branche ; anti-escalade de rôle |
| Exports | ✅ | Audités, protection contre l'injection de formules Excel (`=`, `+`, `-`, `@`) |
| **JWT en query string WS** | ⚠️ | `?token=` côté desktop **et** mobile → logs, proxys, historique. Mitigation : ticket WS éphémère à usage unique |
| Rate limiting | ⚠️ | Par IP : 10 WS/min, 120/min sur `/display`. Risque NAT en salle d'attente |
| Jeton desktop au repos | ✅ | Coffre OS, repli fichier documenté comme non chiffré |
| Jeton mobile au repos | ✅ | `flutter_secure_storage` |
| Secrets en clair | ✅ | `.env` ignoré par git ; `.env.example` sans secret |

---

## 8. Tests et CI — état mesuré le 22/11/2026

| Projet | Commande | Résultat |
|---|---|---|
| Backend | `pytest` (sous-ensemble ciblé) | 44/44 passés. **Suite complète non rejouée ici** : le chargement des modules est très lent sur ce poste Windows (antivirus/IO — `import slowapi` bloqué > 20 s, mesuré au `faulthandler`). Dernier relevé connu, ce jour : **722 passés / 43 ignorés**. La CI Linux l'exécute avec PostgreSQL + Redis réels. |
| Desktop | `npm test` | 55/55 passés (8 fichiers, 43 s) |
| Mobile | `flutter analyze` | 0 problème |
| Mobile | `flutter test` | 171/171 au 2ᵉ run ; 1 échec intermittent au 1ᵉʳ |
| CI backend | GitHub Actions | PostgreSQL 15 + Redis 7, migrations, tests, build image, timeout 20 min |
| CI desktop | GitHub Actions | lint + tests + build, puis `cargo check` + `cargo test` sur Windows |
| CI mobile | — | **Absente** |

**Ratios de couverture de test** (fonctions de test / kLOC) : backend ≈ 33, desktop ≈ 6, mobile ≈ 11. L'écart backend/clients est cohérent avec l'âge des dépôts, mais le desktop est le maillon faible alors qu'il porte le risque métier le plus direct (le guichet).

---

## 9. Plan d'action recommandé

### Sprint 0 — débloquer le pilote (1 à 2 semaines)

1. **Push mobile (M1).** Ajouter `firebase_messaging`, enregistrer le jeton via `PUT /auth/me/fcm-token`, gérer les trois événements critiques (`your_turn`, `absent_warning`, `ticket_transferred`). Le backend est prêt : c'est un travail purement client.
2. **Arbitrage serveur du service à appeler (D1).** Rendre `service_id` optionnel dans `call-next` ; le serveur choisit selon la priorité et l'ancienneté. Supprime une règle métier du client et le risque d'appel hors compétence.
3. **CI mobile (M6).** Un workflow `flutter analyze` + `flutter test` sur PR. Coût : 30 minutes.
4. **Committer le travail desktop en cours (D6).**

### Sprint 1 — produit vendable (3 à 4 semaines)

5. **OTP téléphone côté mobile (M2)** — réutiliser `register/phone` + `verify-otp`, écran unique numéro → code.
6. **Ticket hors ligne honnête (M3)** — remplacer `L-xxxxx` par un état « en attente de confirmation » sans numéro, et retirer l'ETA locale.
7. **Avertissement d'absence sur mobile (M4)** — traiter `absent_warning` et afficher le compte à rebours.
8. **Contrat généré** — types TS/Dart depuis `openapi.json` + test de contrat en CI.

### Sprint 2 — industrialisation (1 à 2 mois)

9. **Découper `TicketService` (B1)** en `QueueEngine`, `AbsenceService`, `TicketQueries`, `QapeTicketCreation`. Objectif : aucun fichier > 800 lignes.
10. **Sortir l'écriture du chemin de lecture (B2/B3)** — persistance AWTE asynchrone ou conditionnelle.
11. **`counter_services` (D2)** — affectation guichet ↔ services, côté modèle et côté UI.
12. **Couverture desktop (D7)** — tests sur `counterStore`, `queueStore`, ouverture/fermeture de guichet, transfert.
13. **Ticket WS éphémère** au lieu du JWT en query string.

---

## 10. Conclusion

Le backend est le point fort du projet, et de loin : les invariants y sont structurels (index, contraintes, compare-and-set), les décisions sont documentées par le problème qu'elles évitent, et les cas dégradés (Redis indisponible, capacité inconnue, rejeu après crash) sont traités explicitement plutôt que supposés. C'est un socle sur lequel on peut mettre un pilote réel en production.

Les deux clients sont en retard sur ce contrat, et pas de la même façon : le **desktop** est fonctionnellement complet mais teste peu et compense côté client une décision qui appartient au serveur ; le **mobile** est proprement architecturé mais n'a pas encore les deux fonctions qui font la valeur du produit — être prévenu de son tour sans regarder l'écran, et s'inscrire avec un numéro de téléphone.

Le prochain jalon n'est donc pas une question d'architecture : c'est de brancher le push, d'ajouter la CI mobile, et de rendre au serveur l'arbitrage du dispatch. Trois chantiers courts, à fort effet levier.
