# Audit backend TONDE — 15 novembre 2026

Portée : `app/` complet (services, routers, modèles, websocket, core), migrations,
et exécution réelle de la suite de tests.

Méthode : lecture intégrale des 3 fichiers critiques (`ticket_service.py` 1971 l.,
`redis.py`, `queue_scheduler.py`), de tous les routers, des modèles et des schémas.
Chaque constat ci-dessous cite le fichier et la ligne. Rien n'est déduit d'une
impression de lecture.

État mesuré le 15/11/2026 :

```
pytest -q  →  261 passed, 129 skipped, 0 failed  (9 min 02 s)
migrations →  14 révisions, 1 seule tête (008_unique_ticket_number)
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

---

## P0 — à traiter avant le test partenaire

### 1. Un second appel au même guichet abandonne le client en cours

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

### 2. L'horaire de repli n'est jamais appliqué

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

### 3. Redis indisponible → 500 sur la lecture d'un ticket

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

### 4. L'expiration à 12 h annule des tickets encore en file

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

### 5. `deactivate` casse les rattachements multiples

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

### 6. `POST /auth/login` : pas de limite par compte

`routers/auth.py:80` → `@limiter.limit("10/minute")`, clé = IP
(`middlewares.py:32-35`). Un attaquant avec 100 IP obtient 1 000 tentatives par
minute sur `superadmin@ulbu.bi`. Les comptes staff d'une banque sont précisément
ceux à protéger.

Recommandation : compteur Redis par email (`tonde:login_fail:{email}`), blocage
progressif après 5 échecs (1 min, 5 min, 15 min), et journalisation dans
`AuditLog`. Le mécanisme existe déjà pour l'OTP (`block_phone`), il suffit de le
transposer.

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
3. **Email sortant / mot de passe oublié** — aucun SMTP configuré. Un compte
   staff créé avec un mot de passe transmis à la main ne peut pas le changer.
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

1. **P0-1, P0-2, P0-3** — trois correctifs courts, sans migration, qui évitent
   les pannes les plus visibles pendant le test partenaire.
2. **P0-4, P0-5** — paramétrage et une requête de comptage.
3. **P1-8 (liste de la file)** — la fonction qui manque le plus au quotidien.
4. Le reste en dette assumée, documenté dans `DEPLOYMENT.md`.
