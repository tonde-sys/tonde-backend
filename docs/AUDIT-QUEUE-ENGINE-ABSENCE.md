# Audit du Queue Engine existant — face à la spécification « Absence & reprise » v1.0

**Date :** 16/11/2026
**Objet :** Étape A de la spécification. Aucune ligne de code métier n'a été
modifiée pour produire ce rapport.
**Tête de migration :** `010_unique_active_service_prefix`
**Suite de tests :** 492 passés, 138 ignorés, 0 échec

Ce document répond aux cinq questions de la section 0 de la spécification :
modèles réels, statuts/endpoints/tâches/événements/clés Redis, mécanismes de
migration et de transaction, détermination du tenant et des permissions, puis
écarts et risques.

---

## 1. Modèle réel, et correspondance avec la spécification

Le vocabulaire de la spécification ne correspond pas au vocabulaire du dépôt.
La correspondance est **exacte** sur le fond, différente sur les noms :

| Spécification | Dépôt | Remarque |
|---|---|---|
| `Institution` | `Organization` | + `Branch` (agence physique) entre l'organisation et le service |
| `Service` | `Service` | rattaché à une `Branch`, avec `ticket_prefix` unique par branche |
| `Queue / File` | **implicite** : `(org_id, agency_id, service_id)` | pas de table `queues` ; la file est un triplet + une clé Redis |
| `Ticket` | `Ticket` | voir §3 pour les champs manquants |
| `Counter / Guichet` | `Counter` | `is_open` (accepte des tickets) et `is_active` (existe) sont deux colonnes distinctes |
| `Agent` | `Employee` (+ `User.role`) | `Employee` porte `org_id`, `agency_id`, `counter_id`, `role`, `status` |

**Conséquence à retenir :** il n'y a **pas** de `queue_id`. Toute la spécification
qui parle de `queue_id` (historique §2.3, événements §8.2, projection §7.1) doit
être lue comme `(org_id, agency_id, service_id)`. Créer une table `queues`
serait une seconde implémentation — la spécification l'interdit explicitement.

Le dépôt est **multi-tenant à trois niveaux** : `Organization` → `Branch` →
`Service`, avec `Counter` rattaché à une `Branch`. Le tenant effectif d'un ticket
est `org_id` ; `agency_id` et `service_id` sont des périmètres internes.

---

## 2. Machine à états réelle

### 2.1 Statuts présents

```
WAITING → CALLED → SERVING → DONE
             ↓        ↓
          ABSENT   INCOMPLETE
             ↓        ↓
          WAITING  WAITING
CANCELLED, TRANSFERRED  (transitoire → WAITING)
```

| Spécification | Dépôt | Verdict |
|---|---|---|
| `WAITING` | `WAITING` | identique |
| `CALLED` | `CALLED` | identique |
| `ABSENT` | `ABSENT` | identique |
| `SERVED` | **`DONE`** | même sémantique, nom différent |
| `TRANSFERRED` | `TRANSFERRED` | identique |
| — | **`SERVING`** | étape supplémentaire : l'agent confirme la présence du client avant de commencer |
| — | **`INCOMPLETE`** | service interrompu (client parti en cours de route) |
| — | **`CANCELLED`** | annulation, y compris expiration automatique |
| `EXPIRED` | **absent** | l'expiration écrit `CANCELLED` + `reason` dans l'historique |

`SERVING` n'est pas un détail : il porte une décision produit (l'agent confirme
que le client est là avant de servir). Il rend aussi le couple
`CALLED → ABSENT` non exclusif : un client peut être `SERVING` et partir.

**Recommandation : conserver les huit statuts existants.** La spécification
elle-même demande de ne pas renommer les statuts publics sans migration et
justification, et d'« établir une correspondance explicite avant migration ».
`SERVED ≡ DONE` est cette correspondance.

### 2.2 Transitions autorisées — comparaison

`ALLOWED_TRANSITIONS` couvre déjà les dix cas de la spécification §2.2, avec
trois différences :

| Cas | Spécification | Dépôt |
|---|---|---|
| `CALLED → ABSENT` avant le délai de grâce | **refusé** | **accepté** (aucun contrôle de délai) — écart réel, §4.1 |
| `ABSENT → terminal` | `EXPIRED` distinct de `CANCELLED` | `CANCELLED` + `reason=expired_absent` dans `queue_logs` |
| `CALLED → WAITING` (guichet qui ferme) | non prévu | **existe** (P1-7), et c'est nécessaire |

La transition `CALLED → WAITING` est une correction d'audit antérieure : quand un
guichet ferme avec un client appelé, remettre le ticket en file est la seule
alternative à « déclarer absent un client présent ». Elle ne contredit pas la
spécification, elle couvre un cas qu'elle n'a pas envisagé.

---

## 3. Champs du modèle `Ticket` — ce qui manque

| Champ spécification | Présent ? | Équivalent actuel |
|---|---|---|
| `status` | ✅ | `status` (`TicketStatus`) |
| `priority` | ✅ | `priority` (`TicketPriority` : STANDARD/PRIORITY/VIP/EMERGENCY) |
| `queue_sequence` BIGINT | ❌ | **aucun** — ordre porté par le score Redis (voir §5) |
| `called_at` | ✅ | `called_at` |
| `grace_expires_at` | ❌ | **absent** — le délai est recalculé à partir de `called_at` |
| `missed_count` | ❌ | **absent** — aucune limite de reprises |
| `last_missed_at` | ❌ | **absent** |
| `recovery_expires_at` | ❌ | **absent** — la fenêtre est recalculée depuis `called_at` |
| `requeued_at` | ❌ | **absent** |
| `counter_id` | ✅ | `counter_id` + `counter_name` |
| `version` | ❌ | absent (contrôle par état verrouillé, pas par version) |
| `created_at` | ✅ | `created_at` |
| `updated_at` | ❌ | **absent** |

Les cinq champs manquants (`grace_expires_at`, `missed_count`, `last_missed_at`,
`recovery_expires_at`, `requeued_at`) sont le cœur de l'écart : **le dépôt
recalcule ces valeurs à la volée au lieu de les persister**, ce qui produit trois
défauts concrets (§4).

---

## 4. Écarts fonctionnels réels

### 4.1 Aucun contrôle du délai de grâce sur l'absence manuelle

`mark_absent` → `_set_absent` sans aucune vérification de temps. Un agent peut
déclarer absent un client **une seconde** après l'avoir appelé. La spécification
§3.1 exige un refus (`GRACE_PERIOD_NOT_ELAPSED`) sauf dérogation auditée.

Côté automatique, le scheduler passe `CALLED → ABSENT` à
`ABSENT_TIMEOUT_SECONDS` (180 s) sans confirmation de l'agent — l'inverse de ce
que demande la spécification §3.1. Voir la décision D2 plus bas : les deux
mécanismes ne sont pas incompatibles si on les hiérarchise.

### 4.2 La fenêtre de récupération est ancrée au mauvais instant

`expire_stale_tickets` calcule la fin de récupération comme
`called_at + ABSENT_RETURN_WINDOW_MINUTES`. Or la fenêtre doit partir de
**l'absence**, pas de l'appel. Écart de 180 s sur chaque ticket, et surtout :
`called_at` est **écrasé à chaque nouvel appel**, donc une reprise suivie d'un
nouvel appel repousse silencieusement la fenêtre. Avec un champ
`recovery_expires_at` persisté, l'échéance devient une donnée, plus un calcul.

### 4.3 Nombre de reprises illimité

Aucun `missed_count`. Un client peut être appelé, absent, revenir, être rappelé,
absent, revenir… indéfiniment, tant qu'il reste dans la fenêtre de 30 minutes.
La spécification plafonne à `max_missed_count = 2`. Chaque cycle coûte deux SMS
(appel + relance) et bloque un guichet 180 s.

### 4.4 La reprise écrase la priorité

```python
# return_to_queue, ticket_service.py:762
ticket.priority = TicketPriority.STANDARD   # « pénalité »
```

C'est une décision produit assumée, mais elle **contredit la spécification §4.3** :
« un ticket repris retourne en fin de sa tranche de priorité », et « les tickets
de priorité supérieure restent naturellement devant lui ». Ici un VIP redevient
STANDARD définitivement, à vie, après une seule absence.

### 4.5 `ABSENT` n'est jamais « hors file » de façon vérifiable

Le ticket est bien retiré du ZSET Redis (`_leave_queue`), donc non appelable —
c'est conforme. Mais rien en base ne dit *jusqu'à quand* : la seule trace est
`called_at`, réutilisé pour deux sémantiques différentes (dernier appel, fin de
récupération).

---

## 5. Ordre de file et projection Redis

**Format actuel :** ZSET par `(org_id, agency_id, service_id)`, score entier

```
score = (10 - priorité) × 1_000_000_000 + created_ms
```

Ce n'est **pas** la concaténation de flottants que la spécification §4.1 met en
garde d'éviter : le score est un entier exact, et l'invariant est documenté et
testé (`tests/test_queue_score_geometry.py`, 6 tests). La marge de 10⁹ ms couvre
11,57 jours, très au-delà du TTL de 72 h — donc la priorité ne peut jamais être
doublée par l'ancienneté.

**Ce qui manque par rapport à la spécification §4.2 :**

- Pas de `queue_sequence` monotone. L'ordre repose sur l'horloge serveur.
  Conséquence : deux tickets créés dans la **même milliseconde** sont départagés
  par l'ordre lexicographique de leur UUID — stable, mais arbitraire.
- Une reprise ne peut pas recevoir « une nouvelle séquence » puisqu'il n'y a pas
  de séquence : elle reçoit `created_ms = maintenant`, ce qui produit le bon
  résultat pratique (fin de la tranche) sans le garantit formellement.

**Opérations Redis existantes :** `ZADD`, `ZADD NX`, `ZPOPMIN` (atomique),
`ZRANK`, `ZCARD`, `ZREM`, `restore_to_queue` (compensation), `rebuild_queue`
(reconstruction depuis PostgreSQL), `repair_open_branches` (réconciliation
périodique), `add_to_queue_if_absent` (reconstruction sans écraser un score).

**Verdict :** la projection est déjà idempotente, reconstructible et
réconciliée — points 7.2 de la spécification largement couverts. Le seul manque
structurel est la séquence métier.

---

## 6. Transactions, concurrence et idempotence

### 6.1 Ce qui est déjà solide

| Mécanisme | Où | Effet |
|---|---|---|
| `ZPOPMIN` | `pop_next_ticket` | un ticket ne peut être retiré que par un seul appelant |
| Compare-and-set PostgreSQL | `UPDATE … WHERE status='waiting'` | même si Redis désynchronise, pas de double appel |
| Compensation | `restore_to_queue` | si l'écriture échoue, le ticket retrouve sa place exacte |
| Verrou de ligne guichet | `SELECT … FOR UPDATE` sur `Counter` | deux clics sur le même poste ne peuvent pas appeler deux tickets |
| Verrou Redis de création | `SET NX` par utilisateur | deux requêtes simultanées ne créent pas deux tickets actifs |
| Verrou d'échéance | `ZREM` comme jeton | une seule instance traite chaque échéance |
| Verrou de maintenance | `mark_once` (`SET NX`) | une seule instance fait la maintenance par intervalle |

C'est déjà très au-dessus de ce que la spécification exige au §6.4.

### 6.2 Ce qui manque

- **Pas d'Outbox.** `publish_event` est appelé après `commit()` (correct) mais
  avale ses exceptions et retourne `False`. Une panne Redis **au moment du
  commit** perd l'événement : la transition est sûre en base, mais un écran de
  salle ne verra jamais l'appel avant de se resynchroniser. Aucune table
  `outbox`, aucun rejeu.
- **Pas de clé d'idempotence.** Seul `client_action_id` existe, et uniquement
  pour la création. Les cinq commandes critiques (`CALL_NEXT`, `MARK_ABSENT`,
  `REQUEUE`, `MARK_SERVED`, `TRANSFER`) n'ont pas de clé : un rejeu réseau
  produit un second effet métier (par exemple un second SMS de relance).
- **Invariant « un seul ticket actif global » non garanti par la base.** Il est
  tenu par `_get_active_ticket` + verrou Redis. Conséquence mesurable : si Redis
  tombe, `acquire_ticket_creation_lock` lève et la création répond
  `503 QUEUE_UNAVAILABLE` — l'invariant tient, mais la disponibilité tombe. Un
  index unique partiel en base rendrait l'invariant structurel **et** la
  création indépendante de Redis.

---

## 7. API, événements, erreurs

**Routes existantes** (`/api/v1/tickets`) — les sept commandes de la
spécification §8 sont déjà couvertes :

| Commande spécification | Route existante |
|---|---|
| appeler le prochain ticket | `POST /counter/call-next` |
| marquer servi | `POST /{id}/serving` puis `POST /{id}/done` |
| marquer absent | `POST /{id}/absent` |
| **reprendre un ticket absent** | `POST /{id}/return` |
| annuler | `DELETE /{id}` |
| transférer | `POST /{id}/transfer` |
| consulter état + position | `GET /{id}`, `GET /history` |

Plus : `POST /{id}/recall` (relance, lot phase 6), `/{id}/incomplete`,
`/{id}/requeue`, `/{id}/priority`, `/{id}/close`, `GET /queue`, `GET /qr/{token}`.

**Aucune route à créer.** La spécification demande explicitement de compléter
l'existant plutôt que de dupliquer.

**Événements** : `EventType` couvre déjà `ticket_called`, `ticket_done`,
`ticket_absent`, `ticket_transferred`, `ticket_cancelled`, `queue_snapshot`,
`counter_queue_changed` (ajouté phase 6). Manquent au sens strict :
`TICKET_REQUEUED` (le retour en file ne publie qu'un snapshot de file) et
`event_id` / `occurred_at` dans le corps des événements.

**Format d'erreur — conflit assumé.** La spécification §8.1 demande
`{"success": false, "error": {"code", "message"}}`. Le dépôt répond
`{"detail": {"code", "message"}}` (FastAPI `HTTPException`), et **tous les
clients** (desktop, mobile, back-office) consomment ce format. La spécification
dit aussi « respecter le format standard du projet ». Les deux phrases se
contredisent : je conserve le format du projet et je le documente ici.

---

## 8. Tenant et permissions

- Tenant déterminé par `org_id` du `User` **et** des `Employee` actifs
  (`get_staff_org_ids`, ajouté phase 6).
- Périmètre branch par `get_staff_branch_ids` (rattachement direct ou via le
  guichet).
- Affectation guichet par `_verify_agent_assignment` / `_assert_staff_can_handle`.
- `ANY_COUNTER_ROLES` = SUPERVISOR, ADMIN_AGENCY, ADMIN_ORG, SUPER_ADMIN.
- Isolation Redis : les clés de file contiennent `org_id`, et le canal Pub/Sub
  est `tonde:events:{org_id}`.

Conforme au §10. Aucun écart relevé. Un point à noter : les identifiants
`counter_id` / `service_id` envoyés par le client **sont** revalidés côté serveur
(`_get_callable_counter`, `_get_service`), conformément au §10.

---

## 9. Tests existants sur le périmètre

| Fichier | Tests | Couvre |
|---|---|---|
| `test_queue_atomicity.py` | 22 | ZPOPMIN, double appel, compensation |
| `test_counter_busy.py` | 3 | guichet occupé |
| `test_queue_score_geometry.py` | 6 | géométrie du score, marge de priorité |
| `test_queue_degradation.py` | 3 | panne Redis |
| `test_queue_engine_features.py` | 42 | moteur |
| `test_queue_listing.py` | 15 | `GET /queue` |
| `test_ticket_visibility.py` | 10 | périmètres de lecture |
| `test_phase6_counter_channel.py` | 32 | canal guichet temps réel |

**Manquent** (spécification §11) : transitions `ABSENT` bornées, ordre de
reprise, `missed_count`, expiration par `recovery_expires_at`, concurrence sur
vraie PostgreSQL, Outbox.

---

## 10. Plan proposé — cinq lots, dans l'ordre de la spécification

| Lot | Contenu | Migration | Risque |
|---|---|---|---|
| **L1** | Champs d'absence/reprise + index `(status, recovery_expires_at)` | `011` | faible, colonnes nullables |
| **L2** | Grille de lecture : `grace_expires_at` au moment de l'appel, refus de l'absence avant grâce, fenêtre ancrée sur `recovery_expires_at`, plafond `missed_count` | — | moyen : change un refus |
| **L3** | Index unique partiel « un ticket actif global » + repli sans Redis | `012` | moyen : migration de données |
| **L4** | `queue_sequence BIGINT` + ordre déterministe | `013` | élevé : backfill + tous les appels Redis |
| **L5** | Outbox + clés d'idempotence | `014` | moyen, additif |

L1 et L2 sont la valeur immédiate : ils corrigent les trois défauts réels
(§4.1, 4.2, 4.3) sans rien casser. L3 rend un invariant structurel. L4 et L5
sont des garanties de forme que la spécification exige mais que l'existant
approche déjà par d'autres moyens.

---

## 11. Décisions à trancher avant d'écrire du code

Quatre points où le dépôt et la spécification disent des choses différentes, et
où trancher seul reviendrait à modifier le produit sans le dire.

**D1 — Absence automatique.** La spécification veut que l'agent confirme
(§3.1) ; le dépôt déclare absent automatiquement à 180 s. Supprimer
l'automatisme fige un guichet abandonné : un ticket `CALLED` bloque
`call_next` (`COUNTER_BUSY`), donc toute la file de ce guichet.
*Proposition :* garder l'automatisme comme **filet** à 180 s, et ouvrir le bouton
« Client absent » à 90 s (nouveau `grace_period_seconds`). L'agent confirme
pendant 90 s de marge ; le système rattrape s'il n'est plus là.

**D2 — Priorité à la reprise.** `return_to_queue` force STANDARD (pénalité
documentée). La spécification veut conserver la tranche et placer le ticket en
fin de tranche.
*Proposition :* appliquer la spécification — la pénalité de temps est déjà
assurée par le retour en fin de tranche, sans détruire la priorité à vie.

**D3 — `queue_sequence`.** Le score entier actuel est correct et testé ; la
spécification veut une séquence BIGINT pour un ordre formellement déterministe.
*Coût :* une migration avec backfill et la réécriture de tous les appels Redis.
*Bénéfice :* ordre exact, indépendant de l'horloge.

**D4 — Outbox et idempotence.** La spécification les exige ; le dépôt couvre
l'essentiel par d'autres moyens (états verrouillés, compensation) mais perd un
événement si Redis tombe au commit, et rejoue un SMS sur un rejeu réseau.

---

## 12. Décisions retenues (16/11/2026)

| # | Décision | Conséquence |
|---|---|---|
| D1 | **Filet à 180 s + bouton à 90 s** | `GRACE_PERIOD_SECONDS = 90` : à 90 s le bouton « Client absent » s'ouvre et l'agent confirme ; à 180 s le système rattrape si personne n'a agi. Un poste abandonné ne fige plus la file, et l'agent garde la main pendant 90 s. |
| D2 | **Priorité conservée à la reprise** | `return_to_queue` ne force plus STANDARD. Le ticket revient en fin de SA tranche : la pénalité de temps suffit. |
| D3 | **`queue_sequence` reporté** | Traité dans un lot ultérieur, après les correctifs de comportement. Mélanger une migration de backfill avec un changement d'algorithme rendrait impossible l'attribution d'une régression. |
| D4 | **Outbox seule, idempotence ensuite** | La perte d'événement est un défaut d'affichage en salle ; le rejeu réseau est plus rare et déjà partiellement couvert par les états verrouillés. |
| — | **Format d'erreur du projet conservé** | `{"detail": {"code", "message"}}`. La spécification se contredit (§8.1 demande un autre format, puis « respecter le format standard du projet ») ; tous les clients consomment le format actuel. |
| — | **Statuts du projet conservés** | `SERVED ≡ DONE`, `SERVING` et `INCOMPLETE` maintenus. Aucun statut renommé, aucun `EXPIRED` ajouté : la distinction annulation / expiration vit dans `queue_logs.extra_data["reason"]`. |

### Ce que le lot L1+L2 a livré

- Migration `011_ticket_absence_recovery` : `grace_expires_at`, `missed_count`,
  `last_missed_at`, `recovery_expires_at`, `requeued_at`, `updated_at`, index
  `(status, recovery_expires_at)`, et reprise de données pour les `ABSENT` en
  cours (sinon ils seraient devenus immortels).
- `call_next` écrit `grace_expires_at` dans le compare-and-set, et efface
  `recovery_expires_at` (un ticket appelé n'est plus en récupération).
- `mark_absent` refuse avant la grâce (`GRACE_PERIOD_NOT_ELAPSED`), avec
  dérogation superviseur auditée (`TICKET_ABSENT_OVERRIDE`).
- `recall` repose la grâce : relancer puis déclarer absent dans la seconde
  aurait été un contournement.
- `_set_absent` incrémente `missed_count`, date `last_missed_at`, pose
  `recovery_expires_at` **ancré sur l'absence**.
- `return_to_queue` vérifie fenêtre + plafond + absence d'autre ticket actif, et
  conserve la priorité.
- `expire_stale_tickets` se fonde sur `recovery_expires_at`, avec une règle de
  repli pour les lignes héritées sans échéance.
- `TicketResponse` expose `grace_expires_at`, `recovery_expires_at`,
  `missed_count` : le poste guichet peut activer son bouton sans recalculer une
  règle serveur.

**Preuve :** `tests/test_phase7_absence_recovery.py` (18). Les quatre correctifs
ont été retirés un par un (grâce, priorité, ancrage de l'expiration, plafond) et
la suite tombe à chaque fois. Suite complète : **510 passés, 138 ignorés**.
