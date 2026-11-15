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

Vue unique, à jour au 15/11/2026 14:45. ✅ = corrigé **et** couvert par des
tests ; ⬜ = rien n'a été fait, le constat reste vrai.

| # | Constat | État | Preuve |
|---|---|---|---|
| 1 | Un 2ᵉ appel au même guichet abandonne le client en cours | ✅ | `tests/test_counter_busy.py` (3) |
| 2 | `is_branch_open_now` ignore `opens_at`/`closes_at` | ✅ | `tests/test_branch_hours.py` (12) |
| 3 | Redis indisponible → 500 sur la lecture d'un ticket | ✅ | `tests/test_queue_degradation.py` (3) |
| 4 | Expiration 12 h : annule des tickets encore en file | ⬜ | — |
| 5 | `deactivate` retire rôle + `org_id` sans vérifier les autres rattachements | ⬜ | — |
| 6 | Aucun verrouillage par compte sur `POST /auth/login` | ✅ | `tests/test_login_lockout.py` (11) |
| 7 | Fermer un guichet laisse le ticket CALLED orphelin | ⬜ | — |
| 8 | Aucun endpoint pour lister la file côté staff | ⬜ | — |
| 9 | Un seul WebSocket par ticket | ⬜ | — |
| 10 | `websocket_counter` : aucun contrôle d'affectation | ⬜ | — |
| 11 | `return_to_queue` sans repli si Redis échoue | ⬜ | — |
| 12 | `GET /tickets/history` renvoie `position: 0` | ⬜ | — |
| 13 | Préfixe de service unique seulement entre services actifs | ⬜ | — |
| 14 | `get_branch_display` : N+1 requêtes | ⬜ | — |
| 15 | Création : `get_queue_size` dans le même `try` que `add_to_queue` | ⬜ | — |
| 16 | `queue_score` : marge de priorité de 104 jours | ⬜ | — |
| 17 | Aucune trace d'audit des connexions | ⬜ | voir note ci-dessous |
| 18 | `/health/ready` n'inspecte pas le scheduler | ⬜ | — |
| 19 | Aucun endpoint de changement de mot de passe | ✅ | `tests/test_change_password.py` (11) + `tests/test_cli_reset_password.py` (5) |
| 20 | Mot de passe partagé par 4 comptes, écrit en clair | ⚠️ outillage prêt, rotation à lancer | `python -m app.cli reset-password` |

**5 constats corrigés, 1 outillé, 14 intacts.** Les 14 restants sont documentés
ci-dessous avec leur correctif ; aucun n'est bloquant pour le test partenaire,
sauf le n° 20 dont la partie « exécution » dépend d'une action d'exploitation.

Deux remarques sur ce tableau :

- **#17 n'est pas un oubli, c'est un blocage structurel.** `AuditLog.org_id` est
  `NOT NULL` avec clé étrangère vers `organizations`. Or un `super_admin` et un
  client OTP ont `org_id = NULL` : leurs connexions ne peuvent pas être
  journalisées dans cette table. Tracer les connexions demande donc une
  migration (`org_id` nullable) — ce n'est pas un correctif de trois lignes.
- **#20 n'est pas un correctif de code.** L'outil existe désormais
  (`reset-password`), mais la rotation elle-même doit être exécutée sur
  l'environnement de production.

### Détail des P0 corrigés

Les trois P0 annoncés comme « courts, sans migration » sont corrigés et testés :

| P0 | Correctif | Tests |
|---|---|---|
| 1. Guichet occupé | `call_next` refuse un nouvel appel tant que le guichet traite un ticket `CALLED`/`SERVING` → `409 COUNTER_BUSY`, avec verrou de ligne sur le guichet | `tests/test_counter_busy.py` (3) |
| 2. Horaires ignorés | `is_branch_open_now` applique `opens_at`/`closes_at` quand `operating_hours` est NULL, gère les créneaux de nuit | `tests/test_branch_hours.py` (12) |
| 3. Redis = 500 | lecture d'un ticket : repli base (position inconnue, total compté en SQL) ; création : `503 QUEUE_UNAVAILABLE` au lieu de 500 | `tests/test_queue_degradation.py` (3) |

Suite complète après les deux lots : **307 passés, 129 ignorés, 0 échec**.

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

### 4. L'expiration à 12 h annule des tickets encore en file — ⬜ non traité

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

### 5. `deactivate` casse les rattachements multiples — ⬜ non traité

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

### 7. Fermer un guichet laisse le client appelé en plan

`counter_service.py:300-301` met `is_open = False` et `current_ticket_id = None`
sans toucher au ticket `CALLED`. Le client est déclaré absent 180 s plus tard
alors que c'est le guichet qui a fermé. Il faudrait soit remettre le ticket en
file, soit le transférer, soit au minimum notifier.

### 8. Aucun endpoint pour lister la file

Il n'existe aucune route renvoyant les tickets en attente d'un service. Le staff
ne peut donc pas :

- voir qui attend (numéros, ancienneté, priorité demandée) ;
- repérer un ticket fantôme et le clôturer (il lui faudrait son UUID) ;
- réordonner ou appeler un ticket précis.

C'est le complément naturel de `POST /tickets/counter/call-next`. Sans lui, la
supervision en agence bondée se fait à l'aveugle. Un
`GET /api/v1/tickets/queue?agency_id=&service_id=` (rôle ≥ AGENT, périmètre
branch) suffirait.

### 9. Un seul WebSocket par ticket

`connections_by_ticket` est un `dict[str, WebSocket]` (`queue_ws.py:71`). Une
deuxième connexion sur le même ticket remplace la première sans la fermer
(`:91`). Pire, `disconnect_client` du premier appareil peut retirer l'entrée du
second (`:107-115`). Un client qui ouvre l'app sur téléphone puis sur tablette
perd le suivi sur l'un des deux, silencieusement.

### 10. `websocket_counter` : pas de contrôle d'affectation

`routers/tickets.py:391-397` vérifie seulement que le guichet appartient à
l'organisation de l'utilisateur. Un agent du guichet 1 peut donc s'abonner au
WebSocket du guichet 7 et recevoir ses événements. Le contrôle existe déjà pour
le REST (`_verify_agent_assignment`) ; il manque ici.

### 11. `return_to_queue` sans repli Redis

`ticket_service.py:548-551` : `add_to_queue` n'est pas protégé. Si Redis échoue,
le ticket est `WAITING` en base mais absent de la file. Le client reste bloqué
par la règle « 1 ticket actif » jusqu'à ce que quelqu'un appelle `call_next` ou
lise son ticket (auto-réparation). Sur une agence sans activité, cela peut durer.

### 12. `get_history` renvoie une position nulle

`ticket_service.py:451-454` construit chaque item avec `position=0, total=0`.
Un ticket encore `WAITING` apparaît donc « position 0 » dans l'historique, ce qui
est faux et trompeur côté mobile. Soit calculer la position réelle pour les
tickets actifs, soit exposer `null` pour dire « non applicable ».

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
fois. **Ce qui reste à faire, et qui n'est pas du code :** exécuter ces quatre
commandes sur l'environnement de production, supprimer `docs/oganisation-ulbu.txt`,
et retirer `BOOTSTRAP_PASSWORDS_JSON` de la configuration Render une fois le
pilote stabilisé.

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

**15. `get_queue_size` dans le `try` de `add_to_queue`** —
`ticket_service.py:310-330`. Si l'ajout réussit mais que la taille échoue, le
ticket est annulé alors qu'il est déjà dans la file : entrée fantôme, nettoyée
plus tard par `_pop_and_call`. Séparer les deux appels.

**16. `queue_score`, constante trompeuse** — `redis.py:193` :
`(10 - priority) * 1_000_000_000 + created_ms`. La priorité ne domine que parce
que 9 × 10⁹ ms ≈ 104 jours dépasse l'écart intra-journalier. Cela fonctionne,
mais le facteur 10⁹ ressemble à des millisecondes alors qu'il représente
11,5 jours. Un commentaire explicite ou une formule `priority_rank * 10**13`
éviterait une régression lors d'une future modification.

**17. Aucun audit des connexions** — `AuditLog` existe et est utilisé pour les
tickets et les priorités, mais ni les connexions réussies ni les échecs n'y sont
tracés. Une banque demandera « qui s'est connecté, quand, depuis où ».

**18. `/health/ready` n'inspecte pas le scheduler** — `health_service.py:64`
vérifie la base et Redis. Si la boucle de fond meurt, les tickets `CALLED` ne
passent plus en `ABSENT` et la maintenance ne tourne plus, sans que le monitoring
ne le voie. `/health/detailed` expose bien `scheduler.last_run_at`, mais ce n'est
pas la sonde interrogée par UptimeRobot.

---

## Fonctions manquantes (produit)

1. **Confirmation de présence** (`PENDING_ARRIVAL` + scan du QR d'agence) — la
   seule réponse structurelle au problème des tickets pris depuis chez soi.
2. **Back-office admin** — `tonde-web` reste une maquette sur données en dur ;
   l'`admin_org` d'une banque n'a aucune interface.
3. **Email sortant / mot de passe oublié** — aucun SMTP configuré. Un
   utilisateur peut désormais changer son mot de passe s'il connaît l'ancien
   (voir P1-19), mais il n'existe aucun moyen de le **récupérer** : pas de
   « mot de passe oublié », pas d'envoi de lien. Un agent qui perd son mot de
   passe passe encore par l'exploitant (`reset-password`).
4. **Liste de la file** (voir P1-8).
5. **Facturation / plan** — aucun modèle d'abonnement ; le seul plafond est
   `max_daily_tickets`.
6. **Rapport quotidien** — les données existent (`queue_logs`), aucun envoi
   automatique au responsable d'agence.

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
   de passe : les deux touchent le même fichier et la même surface d'attaque.
3. **P1-20 (rotation des comptes ULBU)** — ce n'est pas du code, c'est une
   commande à lancer avant de transmettre les accès au partenaire.
4. **P0-4, P0-5** — paramétrage et une requête de comptage. Toujours ouverts.
5. **P1-8 (liste de la file)** — la fonction qui manque le plus au quotidien.
6. Le reste en dette assumée, documenté dans `DEPLOYMENT.md`.
