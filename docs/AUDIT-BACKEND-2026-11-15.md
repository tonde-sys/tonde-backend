# Audit backend TONDE — 15 novembre 2026

Portée : `app/` complet (services, routers, modèles, websocket, core), migrations,
et exécution réelle de la suite de tests.

Méthode : lecture intégrale des 3 fichiers critiques (`ticket_service.py` 1971 l.,
`redis.py`, `queue_scheduler.py`), de tous les routers, des modèles et des schémas.
Chaque constat ci-dessous cite le fichier et la ligne. Rien n'est déduit d'une
impression de lecture.

État mesuré le 15/11/2026, à l'ouverture de l'audit :

```
pytest -q  →  261 passed, 129 skipped, 0 failed  (9 min 02 s)
migrations →  14 révisions, 1 seule tête (008_unique_ticket_number)
```

État au 15/11/2026 14:45, après les deux lots de correctifs :

```
pytest -q  →  307 passed, 129 skipped, 0 failed  (5 min 47 s)
migrations →  inchangé (aucune migration ajoutée par les correctifs)
```

(La commande `alembic` n'est pas installée dans le Python par défaut : le graphe
des révisions a été vérifié en lisant `revision` / `down_revision` de chaque
fichier de `migrations/versions/`.)

Les 129 tests ignorés le sont parce que Redis est injoignable dans cet
environnement (`tests/test_queue_atomicity.py:62`) : **le moteur de file n'est pas
couvert quand Redis est absent**. C'est un angle mort à corriger en CI (service
Redis dans le workflow), pas un échec.

---

## Synthèse

| # | Sévérité | Constat | Fichier |
|---|---|---|---|
| 1 | **P0** | Un 2ᵉ appel au même guichet abandonne le client en cours (→ ABSENT auto) | `ticket_service.py:600-661` |
| 2 | **P0** | `is_branch_open_now` ignore `opens_at`/`closes_at` : agence ouverte 24 h/24 | `ticket_service.py:142-151` |
| 3 | **P0** | Redis indisponible → 500 sur la lecture d'un ticket (pas de repli) | `ticket_service.py:1575-1586` |
| 4 | **P0** | Expiration 12 h : annule des tickets encore en file, sans prévenir | `ticket_service.py:1112-1121` |
| 5 | **P0** | `deactivate` retire rôle + `org_id` sans vérifier les autres rattachements | `employee_service.py:344-351` |
| 6 | P1 | Aucun verrouillage par compte sur `POST /auth/login` | `routers/auth.py:80` |
| 7 | P1 | Fermer un guichet laisse le ticket CALLED orphelin | `counter_service.py:300-301` |
| 8 | P1 | Aucun endpoint pour **lister la file** côté staff | — |
| 9 | P1 | Un seul WebSocket par ticket : le 2ᵉ appareil remplace le 1ᵉʳ | `queue_ws.py:91,107` |
| 10 | P1 | `websocket_counter` : aucun contrôle d'affectation au guichet | `routers/tickets.py:391-397` |
| 11 | P1 | `return_to_queue` sans repli si Redis échoue → client bloqué | `ticket_service.py:548-551` |
| 12 | P1 | `GET /tickets/history` renvoie `position: 0` même en file | `ticket_service.py:451-454` |
| 13 | P2 | Préfixe de service unique seulement entre services **actifs** | `service_service.py:44-51` |
| 14 | P2 | `get_branch_display` : N+1 requêtes, pas de cache | `public_service.py:120-147` |
| 15 | P2 | Création : `get_queue_size` dans le même `try` que `add_to_queue` | `ticket_service.py:310-330` |
| 16 | P2 | `queue_score` : marge de priorité de 104 jours, constante trompeuse | `redis.py:193` |
| 17 | P2 | Aucune trace d'audit des connexions (succès/échec) | `auth_service.py` |
| 18 | P2 | `/health/ready` n'inspecte pas le scheduler | `health_service.py:64` |
| 19 | P1 | Aucun endpoint de changement de mot de passe : seul un shell Render le permet | `routers/auth.py` |
| 20 | P1 | Les 4 comptes ULBU partagent le même mot de passe, écrit en clair dans un fichier local | `docs/oganisation-ulbu.txt` |

Les constats 19 et 20 ont été ajoutés en cours de route : ils ne viennent pas de
la lecture du code mais d'une question d'exploitation (« c'est quoi le mot de
passe de l'agent du guichet 1 ? »), et la réponse a exposé les deux problèmes
d'un coup — un mot de passe unique pour quatre comptes de niveaux différents,
qu'aucun utilisateur ne peut changer lui-même.

### État d'avancement

Vue unique, à jour au 15/11/2026 18:40. ✅ = corrigé **et** couvert par des
tests ; ⬜ = rien n'a été fait, le constat reste vrai.

**17 constats sur 20 sont traités.** Restent P2-13 et P2-14 ; l'observation
401/403, hors constat numéroté, reste également ouverte.

| # | Constat | État | Preuve |
|---|---|---|---|
| 1 | Un 2ᵉ appel au même guichet abandonne le client en cours | ✅ | `tests/test_counter_busy.py` (3) |
| 2 | `is_branch_open_now` ignore `opens_at`/`closes_at` | ✅ | `tests/test_branch_hours.py` (12) |
| 3 | Redis indisponible → 500 sur la lecture d'un ticket | ✅ | `tests/test_queue_degradation.py` (3) |
| 4 | Expiration 12 h : annule des tickets encore en file | ✅ | `tests/test_phase0_corrections.py` (4) |
| 5 | `deactivate` retire rôle + `org_id` sans vérifier les autres rattachements | ✅ | `tests/test_phase0_corrections.py` (2) |
| 6 | Aucun verrouillage par compte sur `POST /auth/login` | ✅ | `tests/test_login_lockout.py` (11) |
| 7 | Fermer un guichet laisse le ticket CALLED orphelin | ✅ | `tests/test_phase0_corrections.py` (3) |
| 8 | Aucun endpoint pour lister la file côté staff | ✅ | `tests/test_queue_listing.py` (15) |
| 9 | Un seul WebSocket par ticket | ✅ | `tests/test_phase3_security.py` (6) |
| 10 | `websocket_counter` : aucun contrôle d'affectation | ✅ | `tests/test_phase3_security.py` (5) |
| 11 | `return_to_queue` sans repli si Redis échoue | ✅ | `tests/test_phase1_corrections.py` (3) |
| 12 | `GET /tickets/history` renvoie `position: 0` | ✅ | `tests/test_phase0_corrections.py` (2) |
| 13 | Préfixe de service unique seulement entre services actifs | ⬜ | — |
| 14 | `get_branch_display` : N+1 requêtes | ⬜ | — |
| 15 | Création : `get_queue_size` dans le même `try` que `add_to_queue` | ✅ | `tests/test_phase1_corrections.py` (2) |
| 16 | `queue_score` : marge de priorité de 104 jours | ✅ | `tests/test_queue_score_geometry.py` (9) |
| 17 | Aucune trace d'audit des connexions | ✅ | `tests/test_phase3_security.py` (8) + migration `009` |
| 18 | `/health/ready` n'inspecte pas le scheduler | ✅ | `tests/test_phase1_corrections.py` (9) |
| 19 | Aucun endpoint de changement de mot de passe | ✅ | `tests/test_change_password.py` (11) + `tests/test_cli_reset_password.py` (5) |
| 20 | Mot de passe partagé par 4 comptes, écrit en clair | ✅ | rotation exécutée le 15/11 15:03, 4 mots de passe distincts |

**17 constats sur 20 sont traités, 3 restent ouverts.** Les trois derniers —
P2-13, P2-14 et l'observation 401/403 — sont de la dette assumée : aucun ne
bloque le test partenaire, tous sont documentés avec leur correctif. La séquence
d'exécution est dans `docs/PLAN-DEV.md`.

Deux remarques sur ce tableau :

- **#17 a demandé une migration**, la seule de tout le lot :
  `009_audit_org_nullable`. `AuditLog.org_id` était `NOT NULL` avec clé
  étrangère vers `organizations`, alors qu'un `super_admin` et un client OTP ont
  `org_id = NULL` — leurs connexions, les deux comptes les plus sensibles,
  étaient donc impossibles à journaliser.
- **#20 n'était pas un correctif de code.** L'outil a été livré
  (`reset-password`), la rotation elle-même a été exécutée sur la production.

### Détail des P0 corrigés

Les trois P0 annoncés comme « courts, sans migration » sont corrigés et testés :

| P0 | Correctif | Tests |
|---|---|---|
| 1. Guichet occupé | `call_next` refuse un nouvel appel tant que le guichet traite un ticket `CALLED`/`SERVING` → `409 COUNTER_BUSY`, avec verrou de ligne sur le guichet | `tests/test_counter_busy.py` (3) |
| 2. Horaires ignorés | `is_branch_open_now` applique `opens_at`/`closes_at` quand `operating_hours` est NULL, gère les créneaux de nuit | `tests/test_branch_hours.py` (12) |
| 3. Redis = 500 | lecture d'un ticket : repli base (position inconnue, total compté en SQL) ; création : `503 QUEUE_UNAVAILABLE` au lieu de 500 | `tests/test_queue_degradation.py` (3) |

Suite locale après les trois lots : **318 passés, 129 ignorés, 0 échec**.
CI (avec Redis, donc les 129 tests inclus) : **447 passés, 0 ignoré, 0 échec**
— run 26, commit `eaa4a3c`.

Après les phases 1 et 2 (commit `790a01a`) : **357 passés, 131 ignorés, 0 échec**
en local, et **489 passés, 0 ignoré, 0 échec** en CI (run 43729823352) — soit
42 tests de plus, tous verts avec PostgreSQL et Redis réels.

La CI a d'ailleurs attrapé ce que la suite locale ne pouvait pas voir :
`/health/status` était comparé par égalité exacte dans un test, et la clé
`scheduler` ajoutée par P2-18 faisait échouer l'assertion. Les 131 tests ignorés
en local restent un angle mort : **« vert en local » ne veut pas dire « vert »**.

Effet de bord à connaître : les agences dont `operating_hours` est NULL voient
désormais leur fenêtre `opens_at`/`closes_at` réellement appliquée. ULBU
(Mutanga-Nord) est configurée `10:00 → 14:00` : elle refusera donc les tickets
en dehors de ce créneau, ce qui est le comportement attendu mais peut surprendre
si la valeur était un simple gabarit.

---

## P0 — à traiter avant le test partenaire

### 1. Un second appel au même guichet abandonne le client en cours — ✅ corrigé

`call_next` ne vérifie jamais si le guichet a déjà un ticket `CALLED` ou
`SERVING`. `_pop_and_call` écrase directement `Counter.current_ticket_id`
(`ticket_service.py:1509-1513`).

Conséquence concrète : un agent clique deux fois sur « Appeler le suivant » (ou
appelle sans avoir terminé le client précédent). Le premier client reste en
`CALLED`, plus personne ne le sert, et le scheduler le passe en `ABSENT` 180 s
plus tard (`_schedule_absent_deadlines`). Le client était présent, il est
maintenant absent, avec 30 minutes de pénalité avant de pouvoir revenir.

C'est exactement le scénario « 100 personnes dans la file, 10 sont appelées »
évoqué en discussion, mais provoqué par l'agent et non par le client.

Correctif : refuser l'appel (409 `COUNTER_BUSY`) tant que
`counter.current_ticket_id` pointe sur un ticket `CALLED`/`SERVING`, ou demander
une confirmation explicite. Aucune migration nécessaire.

### 2. L'horaire de repli n'est jamais appliqué — ✅ corrigé

Le modèle documente l'intention (`models/branch.py:73`) :

> « Si NULL, on utilise opens_at/closes_at comme horaire global »

et le schéma de réponse l'expose comme tel (`schemas/branch_config.py:110-111`) :

```python
opens_at: str    # Horaire global (fallback)
closes_at: str   # Horaire global (fallback)
```

Or `is_branch_open_now` (`ticket_service.py:142-151`) fait :

```python
hours = branch.operating_hours
if not hours:
    return True          # ← aucun repli sur opens_at / closes_at
```

Une agence configurée `08:00 → 17:00` mais sans `operating_hours` accepte des
tickets **à toute heure**, tant que l'interrupteur `is_open` est vrai. Les
valeurs par défaut du modèle (`08:00` / `17:00`) donnent donc une fausse
impression de sécurité.

**Vérifié en production le 15/11/2026, sur ULBU :**

```
branche            : Mutanga-Nord   (is_open = true)
opens_at/closes_at : 10:00 -> 14:00
operating_hours    : null

heure locale       : 16:25  (soit 2 h 25 après la fermeture annoncée)
POST /api/v1/tickets -> 200
  { "number": "C-001", "status": "waiting", "position": 1 }
```

Le ticket a été annulé immédiatement après le test. Autrement dit, l'agence
ULBU délivre aujourd'hui des numéros 24 h/24, y compris le dimanche, alors que
sa configuration déclare une fenêtre de quatre heures. Ce n'est pas une lecture
du code : c'est le comportement observé sur l'API en ligne.

Correctif : quand `operating_hours` est NULL, comparer `opens_at`/`closes_at`.
Attention : la validation actuelle interdit `close < open`
(`schemas/branch_config.py:102`), donc pas de créneau de nuit — à décider
explicitement.

### 3. Redis indisponible → 500 sur la lecture d'un ticket — ✅ corrigé

`_build_response` appelle `get_queue_position` puis `get_queue_size` sans
protection (`ticket_service.py:1575-1586`). Toute exception Redis remonte
jusqu'au handler global (`main.py:239`) et devient un `500 SERVER_ERROR`.

Le paradoxe est net : l'écran de salle public dégrade proprement
(`public_service.py:156-170`, repli PostgreSQL), mais l'application cliente
plante. Même chose à la création : `acquire_ticket_creation_lock`
(`redis.py:430-446`) est appelé **avant** le `try` de `create_ticket`
(`ticket_service.py:192`), donc une panne Redis donne un 500 au lieu du
`503 QUEUE_UNAVAILABLE` prévu par le code.

Correctif : envelopper les lectures de position dans un `try/except`, retourner
`position=0` et `total=0` (l'information est cosmétique), et déplacer l'acquisition
du verrou dans le `try` pour retomber sur le 503.

### 4. L'expiration à 12 h annule des tickets encore en file — ✅ corrigé

`expire_stale_tickets` (`ticket_service.py:1112-1121`) applique :

```python
("waiting", WAITING, CANCELLED,
 now - timedelta(hours=settings.WAITING_TICKET_TTL_HOURS), Ticket.created_at)
```

avec `WAITING_TICKET_TTL_HOURS = 12` (`config.py:92`). Le calcul ne regarde ni
l'état d'ouverture de l'agence, ni la position dans la file, et **aucune
notification n'est envoyée** (`_transition` seul, pas de `notify_*`).

Sur une agence ouverte 07:00–19:00 avec une file chargée, un ticket pris à 07:05
est silencieusement annulé à 19:05, même s'il est premier dans la file. Le
client voit son ticket disparaître sans explication, et perd la possibilité de
`/return`.

Correctif minimal : ne jamais expirer un ticket dont la position est ≤ N, et
notifier avant annulation. Correctif propre : expirer à la fermeture de
l'agence, pas sur une durée fixe.

**Correctif livré (décision de l'exploitant : la fermeture).**
`expire_stale_tickets` applique désormais `_expire_waiting_of_closed_branches` :
tout ticket `WAITING` dont l'agence est fermée est clos — aucun nouveau ticket
ne peut être créé hors horaires, donc c'est un reste de la journée que personne
ne viendra appeler. Chaque annulation publie `TICKET_EXPIRED` et envoie une
notification avec le motif ; avant, le ticket disparaissait en silence.

`WAITING_TICKET_TTL_HOURS` passe de 12 à 72 h et devient un **filet de
sécurité** pour une agence qui ne ferme jamais (horaires mal réglés). Ce n'est
plus la règle : sur une agence ouverte plus de douze heures, l'ancienne règle
annulait des tickets en pleine journée.

### 5. `deactivate` casse les rattachements multiples — ✅ corrigé

`employee_service.py:344-351` :

```python
user.role = UserRole.CLIENT
user.org_id = None
```

sans vérifier s'il reste un autre `Employee` **actif** pour cet utilisateur.
Le modèle `Employee` autorise plusieurs lignes par utilisateur (une par org), et
`get_memberships()` (`auth_service.py:505-521`) renvoie explicitement une liste.

Aujourd'hui l'API bloque la création multi-org (`employee_service.py:125-132`),
donc le cas est latent — mais le bootstrap et les données existantes peuvent le
produire, et la correction est de trois lignes : ne rétrograder le `User` que si
`select(count(Employee)).where(user_id=…, status=ACTIVE)` vaut zéro.

**Correctif livré.** Le comptage est fait après `flush()` de la désactivation,
en excluant la ligne courante. Si un autre `Employee` reste `ACTIVE`, le `User`
conserve son `role` et son `org_id`, et un log le signale.

---

## P1 — sérieux, à planifier

### 6. `POST /auth/login` : pas de limite par compte — ✅ corrigé

`routers/auth.py:80` → `@limiter.limit("10/minute")`, clé = IP
(`middlewares.py:32-35`). Un attaquant avec 100 IP obtient 1 000 tentatives par
minute sur `superadmin@ulbu.bi`. Les comptes staff d'une banque sont précisément
ceux à protéger.

Recommandation : compteur Redis par email (`tonde:login_fail:{email}`), blocage
progressif après 5 échecs (1 min, 5 min, 15 min), et journalisation dans
`AuditLog`. Le mécanisme existe déjà pour l'OTP (`block_phone`), il suffit de le
transposer.

**Correctif livré** (`core/redis.py` : `get_login_block`,
`register_login_failure`, `clear_login_failures`) :

| Comportement | Détail |
|---|---|
| Clé | `tonde:login_fail:{email}` — email normalisé en minuscules |
| Seuil | 5 échecs consécutifs sur une fenêtre de 15 min |
| Paliers | 1 min, puis 5 min, puis 15 min (plafond), cumulés sur 24 h |
| Vérification | **avant** le hachage : un compte bloqué répond `429 ACCOUNT_LOCKED` avec `Retry-After`, même si le mot de passe est bon |
| Succès | compteur, paliers et blocage effacés |
| Email inconnu | compté aussi, sinon le compteur ne protégerait que les comptes existants et l'énumération resterait possible |
| Redis en panne | **fail-open** : pas de verrouillage, connexion possible, panne journalisée |

Le dernier point est un arbitrage assumé : couper l'accès de toute une agence
parce que le cache est tombé serait pire que le risque couvert. Il est testé
explicitement (`test_redis_en_panne_ne_bloque_pas_la_connexion`), pas laissé
implicite.

**Vérifié en production le 15/11/2026, déploiement `dep-dbsqls15efls73f270cg`**
(sur un email inexistant, pour ne verrouiller aucun compte réel) :

```
tentative 1: HTTP 401 | INVALID_CREDENTIALS
tentative 2: HTTP 401 | INVALID_CREDENTIALS
tentative 3: HTTP 401 | INVALID_CREDENTIALS
tentative 4: HTTP 401 | INVALID_CREDENTIALS
tentative 5: HTTP 401 | INVALID_CREDENTIALS
tentative 6: HTTP 429 | ACCOUNT_LOCKED | retry_after=59
tentative 7: HTTP 429 | ACCOUNT_LOCKED | retry_after=56
```

Le `retry_after` qui décroît (59 puis 56) prouve que la valeur vient du TTL
Redis réel et non d'une constante codée en dur. `POST /auth/change-password`
répond `403 Not authenticated` sans jeton sur le service en ligne, ce qui
confirme que la route est bien déployée (un 404 signifierait l'inverse).

### 7. Fermer un guichet laisse le client appelé en plan — ✅ corrigé

`counter_service.py:300-301` met `is_open = False` et `current_ticket_id = None`
sans toucher au ticket `CALLED`. Le client est déclaré absent 180 s plus tard
alors que c'est le guichet qui a fermé. Il faudrait soit remettre le ticket en
file, soit le transférer, soit au minimum notifier.

**Correctif livré.** `close()` et `delete()` appellent
`TicketService.requeue_after_counter_close` avant de libérer le guichet : le
ticket repasse en `WAITING`, perd son `counter_id` / `called_at` (donc
l'échéance d'absence cesse de courir) et retrouve **sa place d'origine** — le
score de file utilise `created_at`, pas l'heure de réinsertion. Deux
transitions ont été ajoutées à la machine à états : `CALLED → WAITING` et
`SERVING → WAITING`, sans lesquelles ce retour est impossible.
La remise en file est non bloquante : si elle échoue, la fermeture aboutit
quand même et le scheduler rattrape le ticket.

**Vérifié en production le 15/11/2026 à 16:31** (service en ligne, guichet 1
d'ULBU) :

```
POST /tickets                        -> 200   C-005
POST /tickets/counter/call-next      -> 200   C-005
GET  /tickets/C-005                  -> status = called
POST /counters/{id}/close            -> 200
GET  /tickets/C-005                  -> status = waiting
                                        counter_name = null
                                        position = 1 / 1
```

Le client est revenu en file avec sa position, au lieu de rester `CALLED` puis
de devenir `ABSENT`. Ticket clôturé et guichet rouvert dans la foulée.

### 8. Aucun endpoint pour lister la file — ✅ corrigé

Il n'existe aucune route renvoyant les tickets en attente d'un service. Le staff
ne peut donc pas :

- voir qui attend (numéros, ancienneté, priorité demandée) ;
- repérer un ticket fantôme et le clôturer (il lui faudrait son UUID) ;
- réordonner ou appeler un ticket précis.

C'est le complément naturel de `POST /tickets/counter/call-next`. Sans lui, la
supervision en agence bondée se fait à l'aveugle.

**Correctif livré : `GET /api/v1/tickets/queue?agency_id=&service_id=`**

| Élément | Fichier |
|---|---|
| Route (rôle ≥ AGENT, périmètre branch) | `routers/tickets.py` |
| `TicketService.list_queue` + `_queue_entry` | `services/ticket_service.py` |
| `QueueEntry` / `QueueListResponse` | `schemas/ticket.py` |

Décisions de conception qui comptent :

- **L'ordre est calculé depuis PostgreSQL, pas depuis Redis.** La base est la
  source de vérité, et c'est précisément pendant un incident Redis qu'un
  superviseur a besoin de voir la file. La clé de tri est identique à celle de
  `queue_score` (priorité décroissante, puis `created_at`), donc la première
  ligne est bien le prochain ticket appelé.
- **La route est déclarée avant `/{ticket_id}`.** Placée après, FastAPI
  interpréterait « queue » comme un UUID de ticket et renverrait 404 — un test
  verrouille cet ordre.
- **`position = 0` pour les tickets appelés ou en service** : ils ne sont plus
  dans la file, et afficher 1 serait faux.
- **L'ancienneté se mesure depuis l'entrée dans l'état courant**, pas depuis la
  création : un ticket rappelé après une absence ne doit pas afficher 40 minutes
  d'attente alors qu'il vient de revenir en file.
- **`qr_token` n'est pas exposé** : un agent n'a pas à scanner le QR d'un client,
  et l'ajouter élargirait la surface sans usage.

Reste à faire côté produit (hors de ce dépôt) : l'écran desktop qui consomme la
route (liste, tri, bouton « appeler ce ticket »). Le contrat est figé par
`QueueListResponse`, les 15 tests et deux tests HTTP de bout en bout.

**Preuve en production** (ULBU Mutanga-Nord, agent du Guichet 1, 17:35 locale) :

```
GET /api/v1/tickets/queue?agency_id=335f…&service_id=a248…   -> 200
{"service_name": "Demande de la carte",
 "waiting": 0, "called": 0, "serving": 0,
 "average_wait_minutes": null, "entries": []}
GET (sans jeton)                                             -> 403
GET (agence inconnue)                                        -> 404 BRANCH_NOT_FOUND
```

La file est vide parce que l'agence a fermé à 17:00 — c'est la décision 1 qui
s'applique, et elle se vérifie aussi par cette route.

### 9. Un seul WebSocket par ticket — ✅ corrigé

`connections_by_ticket` est un `dict[str, WebSocket]` (`queue_ws.py:71`). Une
deuxième connexion sur le même ticket remplace la première sans la fermer
(`:91`). Pire, `disconnect_client` du premier appareil peut retirer l'entrée du
second (`:107-115`). Un client qui ouvre l'app sur téléphone puis sur tablette
perd le suivi sur l'un des deux, silencieusement.

**Correctif livré.** Les deux registres (`connections_by_ticket`,
`counter_connections`) contiennent désormais des **ensembles** de sockets, et
les méthodes de déconnexion reçoivent le socket concerné
(`disconnect_client(ticket_id, agency_id, websocket=None)`). Trois
conséquences :

1. Deux appareils suivent le même ticket ; fermer l'un ne touche pas l'autre.
2. Un socket mort est retiré **individuellement** au moment de l'envoi, sans
   emporter ses voisins — l'ancien code supprimait l'entrée entière.
3. `/ws/stats` distingue désormais `tickets` (clés) de `ticket_devices`
   (connexions) : le tableau de bord de supervision ne peut plus confondre
   « 3 tickets suivis » et « 7 appareils connectés ».

Le même traitement s'applique au guichet (`disconnect_counter`) : deux écrans
par guichet — un poste agent et un écran de salle — sont un besoin courant, pas
un cas limite.

### 10. `websocket_counter` : pas de contrôle d'affectation — ✅ corrigé

`routers/tickets.py:391-397` vérifie seulement que le guichet appartient à
l'organisation de l'utilisateur. Un agent du guichet 1 peut donc s'abonner au
WebSocket du guichet 7 et recevoir ses événements. Le contrôle existe déjà pour
le REST (`_verify_agent_assignment`) ; il manque ici.

**Correctif livré.** Après le contrôle d'organisation, un agent qui n'est pas
dans `ANY_COUNTER_ROLES` doit être rattaché au guichet (`employees.counter_id`,
statut `ACTIVE`) ; sinon la connexion est fermée avec le code **4003** et un
motif explicite (« Vous n'êtes pas affecté à ce guichet »). Les rôles larges
(`supervisor`, `admin_org`, `super_admin`) gardent l'accès : la supervision d'un
guichet n'est pas la même chose que son usage.

Le code 4003 est distinct du 4004 déjà utilisé pour « guichet d'une autre
organisation » : côté client, « pas votre guichet » (réaffectation à demander) et
« pas votre organisation » (erreur de configuration) demandent deux réactions
différentes.

### 11. `return_to_queue` sans repli Redis — ✅ corrigé

`ticket_service.py:548-551` : `add_to_queue` n'est pas protégé. Si Redis échoue,
le ticket est `WAITING` en base mais absent de la file. Le client reste bloqué
par la règle « 1 ticket actif » jusqu'à ce que quelqu'un appelle `call_next` ou
lise son ticket (auto-réparation). Sur une agence sans activité, cela peut durer.

**Correctif livré — deux moitiés :**

1. **La réponse dit la vérité.** `return_to_queue` renvoie désormais
   `{"success": true, "ticket_id": …, "queue_degraded": true}` quand Redis est
   tombé. Avant, l'exception remontait en 500 alors que la transition `WAITING`
   était **déjà commitée** : le mobile croyait l'opération échouée et réessayait,
   recevant « transition interdite ». Le client était dans les limbes — présent
   en base, absent de la file, et aucune réponse ne le disait. On ne peut pas
   annuler la transition (`WAITING → ABSENT` n'est pas une transition autorisée),
   donc le service répond la vérité au lieu de simuler un succès complet.
2. **La file se répare toute seule.** La maintenance du scheduler appelle
   désormais `rebuild_queue()` : tout ticket `WAITING` absent du ZSET y est
   réinséré avec son score d'origine (`ZADD NX`, donc jamais d'écrasement de
   score existant). PostgreSQL reste la source de vérité ; le coût est un scan
   de la table des tickets en attente, borné par l'expiration à la fermeture.
   Le compteur `repaired` remonte dans les stats du scheduler et un `WARNING`
   est journalisé quand il est non nul — la dérive devient visible.

### 12. `get_history` renvoie une position nulle — ✅ corrigé

`ticket_service.py:451-454` construit chaque item avec `position=0, total=0`.
Un ticket encore `WAITING` apparaît donc « position 0 » dans l'historique, ce qui
est faux et trompeur côté mobile. Soit calculer la position réelle pour les
tickets actifs, soit exposer `null` pour dire « non applicable ».

**Correctif livré.** La position et la taille réelles sont calculées pour les
tickets `WAITING` ; Redis n'est pas interrogé pour un ticket terminé (aucun sens,
et 20 appels évités par page). En cas de panne Redis, la position retombe à 0
avec un log — l'historique ne doit jamais échouer à cause du cache.

### 19. Aucun endpoint de changement de mot de passe — ✅ corrigé

Constat d'origine, formulé autrement : `routers/auth.py` n'exposait que
`login`, `refresh`, `logout`, `logout/all`, `me`. Aucune route ne permettait à un
utilisateur de changer son propre mot de passe. Le seul chemin existant était :

```
python -m app.cli bootstrap --config … --reset-passwords
```

depuis un shell Render, avec un `BOOTSTRAP_PASSWORDS_JSON` complet. Concrètement,
un agent qui soupçonnait une fuite de son mot de passe devait demander à
l'exploitant de rejouer un bootstrap sur toute l'organisation.

**Correctif livré :**

| Élément | Fichier |
|---|---|
| `POST /api/v1/auth/change-password` (Bearer, `5/minute`) | `routers/auth.py` |
| `AuthService.change_password` + `_revoke_all_sessions` | `services/auth_service.py` |
| `ChangePasswordRequest` (8 caractères min, 72 octets max) | `schemas/auth.py` |
| `python -m app.cli reset-password --email …` | `app/cli.py` |

Trois décisions à connaître :

1. **L'ancien mot de passe est exigé.** Un access token volé ne doit pas
   permettre de verrouiller le propriétaire hors de son compte.
2. **Toutes les sessions sont révoquées**, y compris l'appareil courant. Un
   refresh token émis avant le changement resterait valide sinon, et changer le
   mot de passe après un vol de token ne protégerait de rien. L'access token
   courant survit jusqu'à son expiration, le temps de finir l'action en cours.
3. **72 octets maximum.** bcrypt tronque silencieusement au-delà : deux mots de
   passe partageant les 72 premiers octets sont équivalents. Le schéma refuse
   désormais la saisie plutôt que de laisser croire que la fin compte.

### 20. Un mot de passe unique pour quatre comptes, écrit en clair — ⚠️ outillage livré, rotation à exécuter

`docs/oganisation-ulbu.txt` contient quatre `password_env` identiques, tous
pointant vers la même valeur :

```
superadmin@ulbu.bi   → <même mot de passe>   (super_admin)
admin@ulbu.bi        → <même mot de passe>   (admin_org)
agent1@ulbu.bi       → <même mot de passe>   (agent, Guichet 1)
agent2@ulbu.bi       → <même mot de passe>   (agent, Guichet 2)
```

La valeur elle-même n'est pas recopiée ici : un audit versionné n'est pas
l'endroit où conserver un mot de passe. Elle est dans le fichier de provisioning
(non versionné, désormais couvert par `.gitignore`) et dans
`BOOTSTRAP_PASSWORDS_JSON` sur Render.

Ce n'est pas un détail cosmétique : **un agent de guichet connaît le mot de passe
du super-admin**. Il peut se connecter en `superadmin@ulbu.bi` et contourner
toute la hiérarchie `CLIENT < AGENT < SUPERVISOR < ADMIN_AGENCY < ADMIN_ORG <
SUPER_ADMIN`. La séparation des rôles n'existe alors que dans le code.

Le fichier est en clair sur le disque et n'est pas versionné (vérifié : aucun
secret dans le commit `0b3a182`), mais il reste la source qui a produit le bug.

**Aggravant découvert en rédigeant ce constat : le mot de passe est déjà dans
l'historique Git.** Il apparaît dans `docs/CREER-UNE-ORGANISATION.md` (commit
`c9a731e`, « docs: exemple réel ULBU et pièges du fichier de bootstrap »), cité
comme exemple d'usage erroné de `password_env`. Vérification :

```bash
git log --all --oneline -S "<le mot de passe>"   →  c9a731e
```

Ce n'est donc plus « un fichier local à supprimer » : la valeur est publiée dans
le dépôt. Réécrire l'historique est possible (`git filter-repo`) mais ne sert à
rien si le dépôt a déjà été cloné — **la rotation n'est pas une recommandation,
c'est la seule remédiation efficace.** Les deux fichiers concernés ont été
nettoyés dans ce lot (l'exemple de `CREER-UNE-ORGANISATION.md` et les fixtures
de tests utilisaient la vraie valeur), et `.gitignore` couvre désormais
`docs/oganisation-*.txt`.

**Ce qui est livré :** la commande de rotation, une ligne par compte, qui change
le mot de passe, ferme les sessions et lève un éventuel blocage de connexion.

```bash
python -m app.cli reset-password --email superadmin@ulbu.bi
python -m app.cli reset-password --email admin@ulbu.bi
python -m app.cli reset-password --email agent1@ulbu.bi
python -m app.cli reset-password --email agent2@ulbu.bi
```

Sans `--password`, un mot de passe aléatoire est généré et affiché une seule
fois.

**Rotation exécutée le 15/11/2026 à 15:03**, contre la base de production Neon
(`ep-broad-surf-bahgqsgk-pooler`, eu-central-1), depuis le poste d'exploitation
avec le `DATABASE_URL` lu sur Render :

```
superadmin@ulbu.bi   OK   19 sessions fermées
admin@ulbu.bi        OK   15 sessions fermées
agent1@ulbu.bi       OK   16 sessions fermées
agent2@ulbu.bi       OK    0 session fermée
```

Vérifié dans la foulée par une connexion réelle sur chacun des quatre comptes :
`HTTP 200`, rôles et `memberships` intacts. `BOOTSTRAP_PASSWORDS_JSON` a été mis
à jour sur Render avec les quatre nouvelles valeurs (sinon un futur bootstrap
aurait rétabli l'ancien mot de passe fuité). Les valeurs ne figurent pas ici :
elles sont dans `docs/oganisation-ulbu.txt` (non versionné, couvert par
`.gitignore`).

**Reste à faire côté exploitation :** copier les quatre mots de passe dans un
gestionnaire, puis supprimer `docs/oganisation-ulbu.txt`, et retirer
`BOOTSTRAP_PASSWORDS_JSON` de Render une fois le pilote stabilisé.

---

## P2 — dette technique

**13. Préfixe unique seulement entre services actifs** —
`service_service.py:44-51` filtre `is_active == True`. Désactiver « Caisse » (C),
créer « Crédit » (C), puis réactiver « Caisse » donne deux services actifs avec
le même préfixe, donc deux files qui produisent des `C-001` différents le même
jour. Ajouter la vérification dans `update` lors d'une réactivation.

**14. `get_branch_display` : N+1** — une requête par guichet
(`public_service.py:122-125`) et trois par service (`:139-146`). Pour 12 guichets
et 6 services, ~30 requêtes par appel, appelé toutes les 7 s par chaque poste
desktop. Un `selectinload`/jointure et un cache Redis de 2 s ramèneraient cela à
2 requêtes.

**15. `get_queue_size` dans le `try` de `add_to_queue`** — ✅ corrigé.
`ticket_service.py:310-330`. Si l'ajout réussit mais que la taille échoue, le
ticket est annulé alors qu'il est déjà dans la file : entrée fantôme, nettoyée
plus tard par `_pop_and_call`. **Correctif :** les deux appels sont séparés. Une
erreur sur `zcard` dégrade l'affichage (`total_in_queue` retombe sur la position
connue) mais n'annule plus un ticket déjà en file. La contre-épreuve — échec de
l'**ajout** → annulation + 503 `QUEUE_UNAVAILABLE` — est testée explicitement,
pour que la séparation n'ait pas relâché la protection contre le ticket
`WAITING` hors file.

**16. `queue_score`, constante trompeuse** — ✅ corrigé.
`redis.py` : `(10 - priority) * 1_000_000_000 + created_ms`. Le facteur 10⁹ est
en **millisecondes**, donc 11,57 jours — et non une seconde, l'erreur naturelle
de lecture. **Correctif :** constante nommée `QUEUE_PRIORITY_BAND_MS`, avec
l'invariant démontré en commentaire (un ticket de priorité p2 > p1 créé après un
ticket p1 passe devant tant que l'écart de création reste sous
`(p2 - p1) × MARGE`) et un test qui verrouille la relation avec le TTL :
`WAITING_TICKET_TTL_HOURS` (72 h) **doit** rester sous la marge, sinon un ticket
ancien pourrait être doublé par une priorité supérieure. Relever le TTL au-delà
de 11,57 jours fait échouer `tests/test_queue_score_geometry.py` avec le chiffre
exact à corriger — c'est le seul garde-fou qui empêche une régression silencieuse.

**17. Aucun audit des connexions** — ✅ corrigé.
`AuditLog` existait et servait aux tickets et aux priorités, mais ni les
connexions réussies ni les échecs n'y étaient tracés. Une banque demandera « qui
s'est connecté, quand, depuis où ».

**Correctif livré — trois morceaux, dont une migration :**

1. **Migration `009_audit_org_nullable`.** `audit_logs.org_id` devient nullable.
   C'était le blocage réel : un `super_admin` et un client OTP n'ont aucune
   organisation, donc leurs connexions — les deux comptes les plus sensibles —
   ne pouvaient pas être écrites du tout. Le downgrade refuse de s'exécuter s'il
   reste des lignes à `org_id` NULL : effacer une preuve d'audit pour revenir en
   arrière n'est pas une option.
2. **Six actions** : `login_succeeded`, `login_failed` (avec
   `reason ∈ {unknown_account, bad_password, account_disabled}`),
   `login_blocked`, `password_changed`, `password_reset_requested`,
   `password_reset`. Chaque ligne porte l'IP (première entrée de
   `X-Forwarded-For`, sinon l'adresse du pair) et le User-Agent tronqué à 255
   caractères.
3. **`GET /api/v1/audit/logs`** — lecture paginée avec filtres `action`,
   `actor_id`, `date_from`, `date_to`. Un `admin_org` ne voit que son
   organisation ; `org_id` n'est libre que pour un `super_admin`.

**Décision : l'audit ne bloque jamais une connexion.** `_audit_login` avale ses
propres erreurs (rollback + `ERROR` dans les logs). Une panne du journal ne doit
pas empêcher un agent de servir des clients : le journal est une exigence de
conformité, pas une dépendance du chemin critique. La contrepartie — une panne
d'audit passe inaperçue côté utilisateur — est assumée et surveillée par le
niveau `ERROR`.

**18. `/health/ready` n'inspecte pas le scheduler** — ✅ corrigé.
`health_service.py` vérifiait la base et Redis. Si la boucle de fond mourait, les
tickets `CALLED` ne passaient plus en `ABSENT` et la maintenance ne tournait plus,
sans que le monitoring ne le voie. `/health/detailed` exposait bien
`scheduler.last_run_at`, mais ce n'est pas la sonde interrogée par UptimeRobot.
**Correctif :** un contrôle `scheduler` est ajouté à `/health/ready` (et à
`/health/status`), avec cinq états — `disabled`, `not_wired`, `starting`,
`healthy`, `unhealthy` — et trois diagnostics distincts :

| Diagnostic | Signification |
|---|---|
| `loop_stopped` | la tâche de fond est terminée alors qu'elle devrait tourner |
| `stalled` | boucle vivante mais aucune itération depuis plus de `SCHEDULER_STALE_AFTER_SECONDS` |
| `never_ran` | lancée depuis trop longtemps sans avoir bouclé un seul tour |

Les états neutres (`disabled`, `not_wired`, `starting`) sont rapportés mais ne
font pas échouer la sonde : sinon un `SCHEDULER_ENABLED=false` assumé, ou un
script CLI, suffirait à sortir l'instance du load balancer. Seul `unhealthy`
dégrade la readiness — ce qui, sur Render, redémarre le service et déclenche un
`warm_up` qui reconstruit files et échéances. La panne silencieuse devient une
panne bruyante, puis une panne réparée.

**Preuve en production** (après déploiement, `GET /health/ready`) :

```json
{"status": "ready",
 "checks": {"database": {"status": "healthy", "latency_ms": 16.5},
            "redis":    {"status": "healthy", "latency_ms": 0.7},
            "scheduler":{"status": "healthy", "last_run_age_seconds": 3.7,
                         "stale_after_seconds": 120}}}
```

**Observation hors constat — 401 vs 403.** Une requête sans jeton sur une route
protégée reçoit `403`, pas `401` : c'est le comportement de `HTTPBearer()` avec
`auto_error=True` (FastAPI renvoie « Not authenticated » en 403). C'est cohérent
sur toutes les routes et aucun client ne s'en plaint, mais un audit de sécurité
bancaire le relèvera : « pas de jeton » devrait être `401`, « jeton valide mais
rôle insuffisant » `403`. Le corriger touche toutes les routes à la fois
(`auto_error=False` + 401 explicite) — à planifier, pas à glisser dans un lot.

---

## Livraison des phases 3 et 4

Les deux derniers lots du plan, livrés le 15/11 en fin de journée.

| Lot | Livré | Tests |
|---|---|---|
| P3-10 | Affectation au guichet vérifiée sur le WebSocket (code 4003) | `test_phase3_security.py` (5) |
| P3-9 | N appareils par ticket, N écrans par guichet | `test_phase3_security.py` (6) |
| P3-17 | Audit des connexions + migration `009` + `GET /audit/logs` | `test_phase3_security.py` (8) |
| P4 | Mot de passe oublié (SMTP, jeton usage unique) | `test_phase4_password_reset.py` (29) |
| P4 | Rapport quotidien (`python -m app.cli daily-report`) | `test_phase4_daily_report.py` (24) |

### Deux défauts trouvés par les tests, pas par la relecture

Les tests du mot de passe oublié ont été écrits pour vérifier trois propriétés
(réponse neutre, usage unique, sessions révoquées). Ils en ont révélé deux
autres, que la lecture du code n'avait pas vues :

1. **Le jeton était stocké en clair dans l'index inverse.**
   `tonde:pwdreset_user:{user_id}` contenait le jeton brut, alors que la clé
   principale n'en stockait que le SHA-256. Un dump Redis livrait donc
   directement les liens de réinitialisation — le hachage ne protégeait rien.
   Corrigé : l'index contient désormais le hash, et c'est lui qui construit la
   clé.
2. **Demander un nouveau lien n'invalidait pas l'ancien.** Le docstring
   l'affirmait, le code ne le faisait pas : `save_password_reset_token`
   écrasait l'index sans supprimer le jeton précédent, qui restait valable
   jusqu'à son expiration. Deux liens actifs au lieu d'un, donc deux fois la
   surface d'interception. Corrigé : l'ancien jeton est supprimé **avant**
   l'écriture du nouveau.

La leçon est la même que pour la CI en phase 1 : ce qui n'est pas vérifié par un
test n'est pas vérifié du tout, y compris quand le commentaire affirme le
contraire.

### Preuves d'exécution

Le rapport quotidien a été exécuté de bout en bout sur une base SQLite amorcée
avec une journée type (5 tickets, 2 guichets, 2 services) :

```
TONDE — Rapport quotidien
ULBU Mutanga-Nord — Bujumbura
samedi 14 novembre 2026
==============================================

Activité
  Tickets créés            5
  Clients servis           2
  Annulés                  1
  Absents                  1
  Temps d'attente moyen    17,5 min
  Taux d'absence           50,0 %
  Heure de pointe          10h00 (heure locale)

Par service
  Caisse : 3 ticket(s), attente 10,0 min, absence 100,0 %
  Crédit : 2 ticket(s), attente 25,0 min, absence 0,0 %

Par guichet
  Guichet 1 : 1 client(s) servi(s)
  Guichet 2 : 1 client(s) servi(s)
```

Deux points à vérifier dans cette sortie, et ils le sont : le ticket créé le 13
à 20:00 UTC (22:00 locales) **n'est pas** compté dans le 14, et le pic de 08:00
UTC est bien affiché « 10h00 (heure locale) ».

---

## Fonctions manquantes (produit)

1. **Confirmation de présence** (`PENDING_ARRIVAL` + scan du QR d'agence) — la
   seule réponse structurelle au problème des tickets pris depuis chez soi.
2. **Back-office admin** — `tonde-web` reste une maquette sur données en dur ;
   l'`admin_org` d'une banque n'a aucune interface.
3. ~~**Email sortant / mot de passe oublié**~~ — **livré (phase 4).** Passerelle
   SMTP (`app/services/email_gateway.py`, `smtplib` + `asyncio.to_thread`, aucune
   dépendance ajoutée), `POST /auth/forgot-password` et
   `POST /auth/reset-password`. Le jeton est à usage unique, expirant
   (`PASSWORD_RESET_TTL_MINUTES`, 30 min par défaut), stocké **haché** dans
   Redis, et sa consommation ferme toutes les sessions. La réponse de
   `forgot-password` est identique que le compte existe ou non — sans quoi le
   formulaire deviendrait un annuaire des agents d'une banque
   (`prenom.nom@banque.bi` se devine).
4. ~~**Liste de la file**~~ — **livré** (`GET /tickets/queue`, phase 2).
5. **Facturation / plan** — aucun modèle d'abonnement ; le seul plafond est
   `max_daily_tickets`.
6. ~~**Rapport quotidien**~~ — **livré (phase 4).** `python -m app.cli
   daily-report` construit le rapport d'une agence depuis `queue_logs` et
   l'envoie par email (`--send`) au responsable. La journée est **locale**
   (UTC+2), pas UTC : un rapport « du 14 » couvre 22:00 UTC le 13 → 22:00 UTC le
   14, et l'heure de pointe est exprimée en heure locale.

---

## Ce que cet audit ne couvre pas

- **Charge réelle** : aucune mesure sous 12 postes simultanés. L'estimation N+1
  ci-dessus est analytique, pas mesurée.
- **Comportement multi-instances** : le code est écrit pour (verrous Redis,
  `ZREM` comme verrou d'échéance), mais Render n'expose qu'une instance. Non
  vérifié en conditions réelles.
- **Sécurité offensive** : pas de test d'intrusion, pas de fuzzing des entrées.
- **Migrations sur une base volumineuse** : l'index d'expression de `008` n'a pas
  été mesuré sur des millions de lignes.
- **Neon/PgBouncer sous charge** : la connexion fonctionne (vérifié), le
  comportement du pooler sous concurrence n'a pas été éprouvé.

---

## Ordre proposé

1. ~~**P0-1, P0-2, P0-3**~~ — **fait.** Trois correctifs courts, sans migration,
   qui évitaient les pannes les plus visibles pendant le test partenaire.
2. ~~**P1-6, P1-19**~~ — **fait.** Verrouillage par compte et changement de mot
   de passe : les deux touchent la même surface d'attaque.
3. ~~**P0-4, P0-5, P1-7, P1-12**~~ — **fait** (phase 0).
4. ~~**P1-11, P2-15, P2-16, P2-18**~~ — **fait** (phase 1) : fiabilité du moteur
   et supervision.
5. ~~**P1-8**~~ — **fait** (phase 2) : `GET /tickets/queue`.
6. ~~**P1-9, P1-10, P2-17**~~ — **fait** (phase 3) : multi-appareils,
   affectation guichet, audit des connexions.
7. ~~**Email sortant, mot de passe oublié, rapport quotidien**~~ — **fait**
   (phase 4).
8. **P2-13, P2-14** — dette assumée, mesurable avant d'être optimisée.
9. **401 vs 403** — à traiter comme un lot à part : la correction touche toutes
   les routes protégées à la fois.

Le détail séquencé, avec les décisions à prendre et les dépendances, est dans
**`docs/PLAN-DEV.md`** — ce document-ci reste le constat, l'autre est le
plan d'exécution.
