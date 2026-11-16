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

Vue unique, à jour au 16/11/2026 15:30. ✅ = corrigé **et** couvert par des
tests ; ⬜ = rien n'a été fait, le constat reste vrai.

**Les 20 constats sont traités, et l'observation 401/403 est corrigée.** Ce
n'est pas une déclaration de perfection : c'est la liste des points que cet
audit avait lui-même relevés, et il n'en reste aucun ouvert. Ce qui reste est
ailleurs — dans ce que l'audit n'a pas regardé, et dans les fonctions produit
qui manquent encore (voir la dernière section).

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
| 10 | `websocket_counter` : aucun contrôle d'affectation | ✅ | `tests/test_phase3_security.py` (5) + `tests/test_phase6_counter_channel.py` (13) |
| 11 | `return_to_queue` sans repli si Redis échoue | ✅ | `tests/test_phase1_corrections.py` (3) |
| 12 | `GET /tickets/history` renvoie `position: 0` | ✅ | `tests/test_phase0_corrections.py` (2) |
| 13 | Préfixe de service unique seulement entre services actifs | ✅ | `tests/test_phase5_service_prefix.py` (10) + migration `010` |
| 14 | `get_branch_display` : N+1 requêtes | ✅ | `tests/test_phase5_branch_display.py` (8) |
| 15 | Création : `get_queue_size` dans le même `try` que `add_to_queue` | ✅ | `tests/test_phase1_corrections.py` (2) |
| 16 | `queue_score` : marge de priorité de 104 jours | ✅ | `tests/test_queue_score_geometry.py` (9) |
| 17 | Aucune trace d'audit des connexions | ✅ | `tests/test_phase3_security.py` (8) + migration `009` |
| 18 | `/health/ready` n'inspecte pas le scheduler | ✅ | `tests/test_phase1_corrections.py` (9) |
| 19 | Aucun endpoint de changement de mot de passe | ✅ | `tests/test_change_password.py` (11) + `tests/test_cli_reset_password.py` (5) |
| 20 | Mot de passe partagé par 4 comptes, écrit en clair | ✅ | rotation exécutée le 15/11 15:03, 4 mots de passe distincts |

**20 constats sur 20 sont traités.** L'observation 401/403, hors constat
numéroté, l'est aussi. La séquence d'exécution et les lots livrés sont dans
`docs/PLAN-DEV.md`.

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

**Suite trouvée plus tard (phase 6).** Le contrôle d'organisation, lui, lisait
`user.org_id` seul — exactement le défaut corrigé par `715dfb1` pour
`/auth/login`, `/auth/me` et `GET /tickets/{id}`, mais oublié ici. Conséquence
concrète : `super_admin` a `org_id = NULL` par construction, donc **l'équipe
Tonde ne pouvait s'abonner au flux d'aucun guichet** — c'est-à-dire que le canal
qui sert à diagnostiquer une salle était fermé à ceux qui diagnostiquent. Le
contrôle lit désormais `get_staff_org_ids` (union de `User.org_id` et des
`Employee` actifs), et la décision est sortie du handler dans
`_authorize_counter_stream` pour être testable sans socket. Détail en fin de
document, « Livraison de la phase 6 ».

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

**13. Préfixe unique seulement entre services actifs** — ✅ corrigé.
`service_service.py:44-51` filtrait `is_active == True`, et `update` ne
vérifiait rien. Désactiver « Caisse » (C), créer « Crédit » (C), puis réactiver
« Caisse » donnait deux services actifs avec le même préfixe, donc deux files
qui produisent des `C-001` différents le même jour. Le préfixe étant immuable
(`UpdateServiceRequest` a `extra="forbid"`), la **réactivation** était le seul
chemin encore ouvert. **Correctif :** `_ensure_prefix_available()` est appelé à
la création **et** lors d'une réactivation, avec un message qui nomme le service
en conflit ; la migration `010_unique_active_service_prefix` ajoute un index
unique **partiel** (`WHERE is_active`) qui rend l'invariant vrai même si un
futur chemin de code oublie l'appel. Partiel et non global : un service
désactivé doit libérer sa lettre, sinon un préfixe serait brûlé à vie.

**14. `get_branch_display` : N+1** — ✅ corrigé.
Une requête par guichet et trois par service : ~30 requêtes pour 12 guichets et
6 services, sur un endpoint **public** interrogé toutes les 7 s par chaque poste
(~85 requêtes/s pour une seule agence). **Correctif :** les numéros en cours,
les derniers appelés et les durées moyennes glissantes sont lus en une requête
groupée chacun (`row_number()` plutôt qu'un `LIMIT 1` par service), et le repli
PostgreSQL en cas de panne Redis est lui aussi groupé — sinon une panne Redis se
transformait en N+1 sur la base, au pire moment. Résultat : **6 requêtes
constantes**, quel que soit le nombre de guichets et de services.

Le cache Redis de 2 s suggéré ici n'a pas été ajouté, volontairement : un écran
qui se rafraîchit toutes les 7 s ne gagne rien à être mis en cache 2 s, et un
cache par instance se paie en invalidation et en tests. Le regroupement suffit
tant que l'agence reste sous la centaine de postes ; le jour où ce n'est plus
vrai, c'est le cache qu'il faudra, pas une nouvelle jointure.

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

**Preuve en production** (après déploiement, `GET /api/v1/audit/logs` avec un
jeton `super_admin`) :

```json
{"action": "password_reset_requested", "org_id": null, "actor_id": null,
 "extra": {"ip": "104.28.209.199", "email": "probe-inexistant@example.com",
           "reason": "no_deliverable_account"}}
```

Cette ligne est celle d'une demande de réinitialisation pour un compte
**inexistant** : `org_id` NULL — donc la migration `009` est bien appliquée en
production, et une tentative d'énumération de comptes laisse une trace
exploitable. Les connexions réussies sont également tracées, y compris celles
du `super_admin` (org NULL) :

```json
{"action": "login_succeeded", "actor_role": "super_admin", "org_id": null,
 "extra": {"ip": "104.28.209.199", "user_agent": "…"}}
{"action": "login_succeeded", "actor_role": "agent",
 "org_id": "cf5243d5-…", "extra": {"ip": "104.28.209.199", "…": "…"}}
```

Contrôles négatifs vérifiés sur la même instance : un jeton d'agent sur
`/audit/logs` → **403** (rôle insuffisant, correct), et une requête sans jeton →
**403** à l'époque — c'est précisément l'observation 401/403, corrigée depuis.

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

**Observation hors constat — 401 vs 403** — ✅ corrigée.
Une requête sans jeton sur une route protégée recevait `403`, pas `401` : c'est
le comportement de `HTTPBearer()` avec `auto_error=True`, qui lève lui-même
l'erreur avant que la dépendance ne s'exécute. Le client ne pouvait donc pas
distinguer « je ne sais pas qui tu es » (401 → se connecter, ou rafraîchir le
jeton) de « je sais qui tu es, mais c'est interdit » (403 → se connecter ne
changera rien). Un mobile qui reçoit 403 sur un jeton expiré n'affiche pas son
écran de connexion : l'utilisateur reste bloqué, et le support reçoit un appel.

**Correctif :** `HTTPBearer(auto_error=False)` et un `401 MISSING_TOKEN` explicite
avec `WWW-Authenticate: Bearer`, conforme à la RFC 7235. Le 403 reste réservé au
rôle insuffisant. Le schéma OpenAPI est préservé — vérifié par un test, parce
qu'un correctif qui fait disparaître le bouton « Authorize » de Swagger casserait
tous les essais manuels.

La crainte initiale (« touche toutes les routes ») était fausse : toutes les
routes protégées passent par `get_current_user`, donc par une seule dépendance.
Deux tests existants qui toléraient `in (401, 403)` ont été resserrés à `401` —
une tolérance qui laisse passer les deux valeurs ne vérifie rien.

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

---

## Livraison de la phase 5 — la dette et le 401

Trois lots de dette, livrés le 16/11. Aucun n'était bloquant pour le pilote ;
tous étaient des questions qu'un évaluateur technique pose.

| Lot | Livré | Tests |
|---|---|---|
| P2-13 | Réactivation refusée si le préfixe a été repris + index unique partiel (migration `010`) | `test_phase5_service_prefix.py` (10) |
| P2-14 | `get_branch_display` : 6 requêtes constantes au lieu de 3 + 2N + M | `test_phase5_branch_display.py` (8) |
| 401/403 | Jeton absent → `401 MISSING_TOKEN` + `WWW-Authenticate`, 403 réservé au rôle | `test_phase5_auth_status.py` (13) |

### Ce que le test du N+1 mesure vraiment

Le test n'affirme pas « moins de 10 requêtes » — une telle assertion passerait
aujourd'hui et sauterait à la première agence un peu grande. Il compare le
nombre de requêtes pour **2 services puis pour 8 services dans la même agence**
et exige l'égalité. C'est la seule formulation qui échoue dès qu'un N+1 revient,
quel que soit le nombre de services ajoutés ensuite.

Le même test est rejoué avec Redis en panne, parce que le repli SQL était
lui-même un N+1 : une panne Redis se transformait en surcharge PostgreSQL, au
moment précis où la base est le seul recours.

### Le préfixe : pourquoi un index partiel et pas un index unique

Un index unique global sur `(branch_id, ticket_prefix)` aurait interdit de
réutiliser la lettre d'un service désactivé — c'est-à-dire de remplacer « Retrait »
par « Retrait express » en gardant le préfixe R, qui est le cas normal d'un
guichet qui change de prestation. L'index est donc partiel (`WHERE is_active`) :
seuls les services actifs se disputent la lettre, et l'invariant « au plus un
service actif par préfixe » devient vrai en base, pas seulement dans le service.

Comme pour la migration `008`, l'index n'est pas créé si des doublons existent
déjà : faire échouer la migration rendrait indéployable la base qu'on cherche à
réparer. Le message de warning donne la requête SQL pour trouver les branches
concernées.

**Vérification.** Suite locale : **460 passés, 138 ignorés, 0 échec**. CI :
**run #37 verte** (2 min 18 s). Point important : la CI exécute
`alembic upgrade head` sur une base PostgreSQL 15 vide, donc la migration `010`
y est réellement jouée — c'est le seul endroit où le SQL de l'index partiel est
validé, SQLite construisant son schéma depuis les modèles.

### Deux angles morts d'exploitation, trouvés en écrivant la procédure

- **L'absence de SMTP était invisible.** `forgot-password` répond 200 avec un
  message neutre que le compte existe ou non — c'est voulu, mais cela rend une
  production sans SMTP indiscernable d'une production correcte. `/health/detailed`
  expose maintenant `email_gateway.status`, comme il le faisait déjà pour SMS et
  push.
- **`/health/ready` refusait `HEAD`.** FastAPI n'ajoute pas `HEAD` aux routes
  `GET`, et les moniteurs sondent en `HEAD` : un moniteur sur la readiness aurait
  reçu 405 et serait passé DOWN alors que le service allait bien. C'est le piège
  déjà documenté pour `/health` ; il restait ouvert sur les deux sondes qui
  détectent réellement une panne.

La leçon est la même que pour la CI en phase 1 : ce qui n'est pas vérifié par un
test n'est pas vérifié du tout, y compris quand le commentaire affirme le
contraire.

### Preuves d'exécution

Suite locale après les phases 3 et 4 : **429 passés, 132 ignorés, 0 échec**
(les 132 ignorés sont les tests qui exigent un Redis et un PostgreSQL réels —
c'est précisément ce que la CI apporte). CI : **run #33 verte en 2 min 38 s**,
avec `postgres:15-alpine` et `redis:7-alpine`, soit 72 tests de plus qu'en
phase 2 (19 + 29 + 24).

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

Les deux nouvelles routes de réinitialisation ont également été éprouvées sur la
production, déployée depuis `main` :

| Appel | Réponse |
|---|---|
| `POST /auth/forgot-password` (compte inexistant) | `200` + réponse neutre, aucun email |
| `POST /auth/reset-password` (jeton inventé) | `400` `INVALID_RESET_TOKEN` |

---

## Livraison de la phase 6 — le canal guichet

Trois défauts signalés par l'agent desktop, tous confirmés dans le code avant
d'écrire une ligne. Ils forment un seul sujet : **le poste de guichet était un
canal à sens unique**, alimenté à la connexion et jamais ensuite.

| Signalement | Vérification | Livré |
|---|---|---|
| `if not user.org_id` sur `/tickets/ws/counter/{id}` | Exact — et `super_admin` a `org_id = NULL`, donc aucun guichet écoutable par l'équipe Tonde | `get_staff_org_ids` + `_authorize_counter_stream` |
| Pas de `POST /tickets/{id}/recall` | Exact — aucun `recall` dans le code hors préférences SMS | `TicketService.recall` + route + `AuditAction.TICKET_RECALLED` |
| `counter_connections` ne reçoit aucun événement métier | Exact — écrit par `connect_counter`, lu par `send_to_counter`, jamais traversé | `COUNTER_QUEUE_CHANGED` + `counter_agency` + `send_to_agency_counters` |

### Pourquoi le premier défaut n'était pas anodin

`User.org_id` est un champ **mono-organisation hérité**. Le modèle `Employee`
autorise plusieurs lignes par utilisateur (une par organisation) et
`get_memberships` renvoie une liste : le staff multi-organisations est un cas
prévu. Un contrôle qui ne lit que `User.org_id` refuse donc un agent légitime
dans deux situations, et la seconde est plus vicieuse que la première :

- `super_admin` : `org_id = NULL` par construction (`bootstrap_service`). Le
  canal de diagnostic était fermé à l'équipe qui diagnostique.
- rattachement multiple : `deactivate` ne réécrit pas `org_id` quand un autre
  rattachement subsiste (P0-5), donc `org_id` peut désigner une organisation dont
  le rattachement a été retiré. L'agent est alors refusé **à son propre guichet**,
  avec le motif « guichet introuvable dans votre organisation ».

Le correctif est une fonction, `get_staff_org_ids`, et non un `or` ajouté dans
le handler : la même règle servira au prochain contrôle multi-organisations.

### Pourquoi la relance devait exister

Quand un client n'entend pas son numéro — salle bruyante, il était dehors — la
seule action disponible était de le déclarer `ABSENT`. C'est-à-dire de le
**renvoyer derrière tout le monde pour un problème d'audition**. Le poste desktop
avait masqué son bouton « Rappeler » hors mode démonstration pour ne pas offrir
un bouton qui échoue.

Une relance rediffuse `your_turn` (téléphone) et `ticket_called` (salle), renvoie
le SMS, et **remet le compte à rebours ABSENT à zéro**. Sans ce dernier point, la
relance serait une annonce dans le vide : le ticket passerait `ABSENT` pendant que
le client arrive.

Le plafond (`MAX_RECALLS_PER_TICKET`, défaut 2) est compté dans `audit_logs`, pas
dans Redis. Chaque relance envoie un vrai SMS ; un redémarrage de Redis ne doit
pas rouvrir le droit d'en envoyer trois de plus au même client. Le journal est la
source durable, et il porte déjà « qui a relancé quoi ».

### Pourquoi le message diffusé est pauvre

L'événement `counter_queue_changed` dit *ce qui* a changé (`reason`, statut,
numéro, guichet), jamais l'état complet de la file. Deux raisons :

- un format d'état complet devient un contrat que le desktop devra suivre à vie,
  pour un gain nul : le poste redemande le détail s'il en a besoin ;
- l'événement est publié dans `_transition`, le point unique par lequel passe
  tout changement d'état — donc aucune transition ne peut être oubliée. La
  contrepartie assumée est qu'une transition annulée par un rollback émet un
  événement de trop. Le client y réagit en relisant l'état : l'annonce
  excédentaire est sans effet, alors qu'une transition **manquée** laisserait le
  poste muet jusqu'au prochain sondage — c'est précisément le défaut corrigé.

### Ce que les tests ont dû apprendre à distinguer

Deux modes de panne de `publish_event` coexistent et ne se testent pas de la même
façon : en production la fonction **avale** ses exceptions et retourne `False`,
elle ne lève pas. La simuler par un `raise` teste le mock, pas le service. Les
deux fixtures existent donc — `canaux_muet` (`False`) pour le comportement réel,
`canaux_en_panne` (`raise`) pour vérifier que les garde-fous locaux tiennent même
si ce contrat changeait un jour.

Les trois correctifs ont ensuite été **retirés un par un** pour confirmer que la
suite tombe : sans cela, 32 tests verts pouvaient aussi bien ne rien tester.

**Vérification.** Suite locale : **492 passés, 138 ignorés, 0 échec** (+32). CI :
**run #39 verte** (2 min 06 s). Les 32 tests de ce lot tournent aussi en local :
la décision d'autorisation et le routage des événements ont été conçus pour ne
pas dépendre d'un Redis réel — c'est ce qui les rend utilisables à chaque
modification, pas seulement en intégration.

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
8. ~~**P2-13, P2-14**~~ — **fait** (phase 5) : unicité du préfixe à la
   réactivation + index partiel, et N+1 de l'écran de salle ramené à 6 requêtes
   constantes.
9. ~~**401 vs 403**~~ — **fait** (phase 5). La crainte « touche toutes les
   routes » était fausse : elles passent toutes par une seule dépendance.

Le détail séquencé, avec les décisions à prendre et les dépendances, est dans
**`docs/PLAN-DEV.md`** — ce document-ci reste le constat, l'autre est le
plan d'exécution.
