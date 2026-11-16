# Plan de développement TONDE — issu de l'audit du 15/11/2026

Source : `AUDIT-BACKEND-2026-11-15.md` (20 constats) + fonctions manquantes.
État de départ : **5 corrigés, 1 outillé, 14 ouverts.**
État au 15/11 16:00 : **phase 0 livrée, plus P0-4 (expiration à la fermeture).**
Les quatre décisions bloquantes ont été tranchées (voir la fin du document).

Ce plan n'est pas une liste de courses. Il est ordonné par **ce qui peut casser
pendant le test partenaire**, puis par **ce qui bloque une vente**, puis par la
dette. Un constat P2 peut passer avant un P1 s'il tombe dans le chemin critique.

Trois critères de priorisation, dans cet ordre :

1. **Un client dans une agence peut-il être perdu ou mal servi ?** → prime sur tout.
2. **Une organisation peut-elle refuser le produit pour cette raison ?** (sécurité,
   conformité, absence d'interface)
3. **Le coût augmente-t-il avec le temps ?** (dette qui se propage)

Tailles : **S** ≈ quelques heures, **M** ≈ 1 à 2 jours, **L** ≈ 3 jours et plus.

---

## Phase 0 — Avant que le partenaire touche le système — ✅ LIVRÉE

Objectif : ne pas griller le test sur un détail visible en dix minutes.

| # | Action | Taille | État |
|---|---|---|---|
| 20 | Rotation des 4 comptes ULBU | ✅ fait | Mot de passe présent dans l'historique Git |
| — | Fenêtre ULBU `08:00 → 17:00` | ✅ fait | Décision 1, appliquée en production |
| 7 | Fermer un guichet ne doit pas abandonner le client appelé | ✅ fait | `requeue_after_counter_close`, transition CALLED/SERVING → WAITING |
| 5 | `deactivate` ne rétrograde que s'il ne reste aucun rattachement actif | ✅ fait | comptage avant rétrogradation |
| 12 | `history` : position réelle pour un ticket en file | ✅ fait | Redis interrogé seulement pour `WAITING` |
| — | CI Redis | ✅ déjà en place | **La CI était rouge depuis 4 commits, pas à cause de Redis** |

**Ce que la vérification de la CI a révélé** : le workflow contenait déjà un
service Redis (`ci.yml:34`), donc les 129 tests n'étaient pas « jamais exécutés »
— ils l'étaient, et **ils échouaient**. Le correctif P0-1 (refus du second appel
sur un guichet occupé) invalidait deux tests d'atomicité qui appelaient deux fois
`call-next` sans terminer le premier ticket. Ces tests décrivaient l'ancien
comportement buggé. Ils ont été corrigés pour terminer le ticket entre deux
appels. Leçon : un test local « vert » ne prouve rien quand 129 tests sont
ignorés — vérifier la CI fait partie de la définition de « terminé ».

---

## Phase 1 — Fiabilité du moteur de file

Objectif : le moteur ne perd plus de client, même quand une brique tombe.

| # | Action | Taille | État |
|---|---|---|---|
| 4 | Expiration à la fermeture de l'agence, avec notification | ✅ fait | Décision 2 — voir ci-dessous |
| 11 | `return_to_queue` : repli si Redis échoue | S | ✅ fait |
| 15 | Séparer `get_queue_size` de `add_to_queue` | S | ✅ fait |
| 16 | `queue_score` : documenter la marge de priorité | S | ✅ fait |
| 18 | `/health/ready` doit voir le scheduler mort | S | ✅ fait |

**Ce que « expiration à la fermeture » a changé concrètement** (P0-4) :

- Un ticket `WAITING` n'est plus clos au bout de 12 h, mais quand son agence
  ferme. Un ticket pris à 08:05 dans une agence 08:00–17:00 vit jusqu'à 17:00.
- L'ancienne règle annulait des tickets **en pleine journée** dès qu'une agence
  restait ouverte plus de douze heures. C'était le vrai défaut, plus grave que
  l'absence de notification.
- Chaque annulation est désormais **notifiée** (`TICKET_EXPIRED` + push) avec le
  motif. Un ticket qui disparaît en silence est un client qui appelle.
- `WAITING_TICKET_TTL_HOURS` passe de 12 à **72 h** et devient un simple filet
  de sécurité pour une agence qui ne ferme jamais (horaires mal réglés). Ce
  n'est plus la règle.

**Les trois autres correctifs, en une phrase chacun :**

- **11** — quand Redis tombe, `return_to_queue` répond
  `{"success": true, "queue_degraded": true}` au lieu de laisser remonter un 500
  sur une transition déjà commitée ; la maintenance du scheduler appelle
  `rebuild_queue()` et réinsère tout `WAITING` absent du ZSET (compteur
  `repaired` + `WARNING`). *La réponse dit la vérité, la file se répare seule.*
- **15** — l'échec de `zcard` ne peut plus annuler un ticket déjà dans la file :
  il ne dégrade que `total_in_queue`. L'échec de l'**ajout** annule toujours
  (contre-épreuve testée), sinon un `WAITING` hors file bloquerait son client
  avec la règle « 1 ticket actif ».
- **16** — `QUEUE_PRIORITY_BAND_MS` nomme les 10⁹ ms (11,57 jours) et
  `tests/test_queue_score_geometry.py` verrouille l'invariant
  `TTL (72 h) < marge (11,57 j)`. Relever le TTL sans toucher la marge casse un
  test au lieu de casser la priorité en production.
- **18** — `/health/ready` et `/health/status` rapportent un contrôle
  `scheduler` (`healthy` / `stalled` / `loop_stopped` / `never_ran`, plus les
  états neutres `disabled`, `not_wired`, `starting`). Un scheduler mort sort
  l'instance du load balancer ; Render la redémarre et `warm_up` reconstruit
  files et échéances.

---

## Phase 2 — Supervision : la fonction qui manque le plus

| # | Action | Taille | État |
|---|---|---|---|
| 8 | `GET /api/v1/tickets/queue?agency_id=&service_id=` (rôle ≥ AGENT, périmètre branche) | M | ✅ fait |
| 8b | Écran desktop correspondant : qui attend, depuis quand, priorité | M | ⬜ contrat figé, à câbler |

C'est le complément naturel de `call-next`. Sans lui, un superviseur en agence
bondée est aveugle : il ne peut ni voir la file, ni repérer un ticket fantôme,
ni appeler un numéro précis. C'est aussi ce qui rend le poste guichet réellement
utile — aujourd'hui il appelle à l'aveugle.

**Ce que la route renvoie** (`QueueListResponse`) :

```
agency_id, service_id, service_name,
waiting_count, called_count, serving_count, average_wait_minutes,
entries[] : ticket_id, number, status, priority, requested_priority,
            position, waiting_since_minutes, created_at, counter_name
```

Trois choix qui ont des conséquences :

- **L'ordre vient de PostgreSQL, pas de Redis.** Même clé que `queue_score`
  (priorité puis ancienneté), donc la première ligne est le prochain ticket
  appelé — et la supervision reste utilisable pendant un incident Redis, ce qui
  est exactement le moment où on en a besoin.
- **`position = 0` pour les tickets appelés ou en service** : ils ne sont plus
  dans la file.
- **Pas de `qr_token`** : un agent n'a pas à scanner le QR d'un client.

Reste l'écran desktop (8b) : hors de ce dépôt. Il consomme une réponse stable,
couverte par 15 tests dont 2 de bout en bout (JWT + RBAC réels).

---

## Phase 3 — Sécurité et conformité (ce qu'une banque demandera) — ✅ LIVRÉE

| # | Action | État |
|---|---|---|
| 10 | `websocket_counter` : vérifier l'affectation au guichet | ✅ code 4003 + tests (5) |
| 9 | Un WebSocket par ticket → plusieurs appareils par ticket | ✅ ensembles de sockets + tests (6) |
| 17 | Audit des connexions (succès/échec) | ✅ + migration `009_audit_org_nullable` + tests (8) |
| 19 | ~~Changement de mot de passe~~ | ✅ fait |
| 6 | ~~Verrouillage par compte~~ | ✅ fait |

Le n° 17 n'était pas un oubli de l'audit : `AuditLog.org_id` était `NOT NULL`
avec clé étrangère. Un `super_admin` et un client OTP ont `org_id = NULL`, donc
leurs connexions ne pouvaient pas y être écrites. La migration `009` rend la
colonne nullable — c'est la seule migration de tout ce plan.

Livré au-delà du strict constat : `GET /api/v1/audit/logs` (lecture paginée,
filtres `action`/`actor_id`/`date_from`/`date_to`, cloisonnée par organisation
sauf pour un `super_admin`). Un journal qu'on ne peut pas interroger ne sert
qu'à la case « conforme » d'un questionnaire.

**Décision : l'audit ne bloque jamais une connexion.** Une panne du journal
n'empêche pas un agent de servir des clients ; l'échec part en `ERROR` dans les
logs applicatifs. Le journal est une exigence de conformité, pas une dépendance
du chemin critique.

---

## Phase 4 — Produit : ce qui bloque une vente

Ces points ne sont pas des bugs. Ce sont des absences.

| Sujet | État | Taille |
|---|---|---|
| **Back-office admin** (`tonde-web`) | Maquette sur données en dur — **hors dépôt** | L |
| **Distribution desktop** (signature, SmartScreen, auto-update) | Non signé — **hors dépôt** | M |
| **Facturation / plan** | Aucun modèle d'abonnement — **hors dépôt** | L |
| **Rapport quotidien** | ✅ backend livré (`python -m app.cli daily-report`) | — |
| **Mot de passe oublié / email sortant** | ✅ livré (`forgot-password`, `reset-password`) | — |
| ~~Confirmation de présence~~ (`PENDING_ARRIVAL` + QR) | **Abandonnée** (décision 3) | — |

### Ce qui a été livré, et pourquoi ces deux-là d'abord

**Mot de passe oublié + email sortant.** Jusqu'ici, un agent qui perdait son mot
de passe appelait l'administrateur, qui le régénérait depuis un shell Render.
Pour une organisation partenaire qui gère elle-même ses guichets, cela veut dire
qu'aucun agent ne peut se dépanner seul un samedi matin. Livré : passerelle SMTP
(`smtplib` + `asyncio.to_thread`, aucune dépendance ajoutée), jeton à usage
unique stocké **haché** dans Redis, consommation qui ferme toutes les sessions,
et une réponse de `forgot-password` strictement identique que le compte existe
ou non — sans quoi le formulaire devient un annuaire des agents.

**Rapport quotidien.** `queue_logs` contenait déjà tout ; personne ne le lisait.
Un gérant d'agence ne se connecte pas à un tableau de bord chaque matin, il lit
ses emails. Livré : `python -m app.cli daily-report [--date] [--org] [--branch]
[--send]`, qui construit le rapport depuis `queue_logs` et l'envoie au
responsable. La journée est **locale** (UTC+2) et l'heure de pointe exprimée en
heure locale : en UTC, la journée commencerait à 02:00 locales et l'heure de
pointe serait fausse de deux heures.

Reste à brancher : un cron (Render, plan payant) qui lance `daily-report --send`
chaque matin. Sur le plan gratuit, aucun cron n'est disponible — d'où le CLI
plutôt qu'une boucle in-process, qui serait liée à une instance et se
déclencherait deux fois après un redémarrage.

### Ce qui reste, et qui ne se fait pas ici

Ordre recommandé : **back-office → distribution desktop → facturation**.
Le back-office d'abord parce que sans lui, chaque nouvelle organisation passe
par `scripts/create_org.py` et par toi. La distribution ensuite parce qu'un MSI
non signé déclenche SmartScreen chez le client. La facturation en dernier : elle
n'a de sens qu'une fois qu'une organisation peut s'inscrire seule.

Les trois vivent hors de ce dépôt (`tonde-web`, chaîne de build desktop, modèle
d'abonnement) : rien de ce qui précède ne les débloque.

La **confirmation de présence** est écartée. Le problème des tickets pris depuis
chez soi est traité autrement : appel, puis 180 s pour se présenter, puis
`ABSENT` avec retour possible pendant 30 minutes. C'est moins élégant qu'un scan
de QR, mais ça ne change pas le parcours client et ça ne fausse pas les mesures
du pilote.

---

## Phase 5 — Dette assumée — ✅ LIVRÉE

| # | Action | Taille |
|---|---|---|
| 14 | `get_branch_display` : N+1 → nombre constant de requêtes | M |
| 13 | Unicité du préfixe de service à la réactivation | S |
| — | 401 vs 403 : jeton absent → `401`, 403 réservé au rôle | S |

Le n° 14 devenait urgent dès 12 postes desktop interrogeant l'écran de salle
toutes les 7 secondes : ~30 requêtes par appel, par poste. Mesuré puis corrigé —
**6 requêtes constantes**, contre 3 + 2N + M auparavant. Le cache Redis de 2 s
envisagé dans l'audit n'a pas été retenu : un écran rafraîchi toutes les 7 s n'y
gagne rien, et un cache par instance coûte en invalidation. Il sera rouvert le
jour où une agence dépassera la centaine de postes, pas avant.

---

## Phase 6 — Le canal guichet — ✅ LIVRÉE

Trois défauts signalés par l'agent desktop, tous confirmés dans le code avant
d'écrire une ligne. Ils formaient un seul sujet : **le poste de guichet était un
canal à sens unique**, alimenté à la connexion et jamais ensuite.

| Défaut | Ce qui se passait réellement |
|---|---|
| `websocket_counter` lisait `user.org_id` seul | `super_admin` a `org_id = NULL` par construction → **aucun** guichet écoutable par l'équipe Tonde. Un agent dont `org_id` désignait une organisation périmée (P0-5) était refusé à son propre guichet. |
| Pas de `POST /tickets/{id}/recall` | L'agent qui venait d'appeler un client resté dehors n'avait qu'une option : le déclarer `ABSENT`, donc le renvoyer derrière tout le monde. Le bouton « R » du desktop avait dû être masqué hors mode démonstration. |
| `counter_connections` jamais parcouru | Alimenté par `connect_counter`, lu par `send_to_counter`, et **aucun événement ne le traversait**. D'où le sondage HTTP toutes les 7 s. |

Ce qui a été fait :

- `app.core.access.get_staff_org_ids` — union de `User.org_id` et des `Employee`
  actifs. Source unique du contrôle, au lieu d'un champ hérité mono-organisation
  qui ne peut pas représenter le staff multi-organisations du modèle.
- `_authorize_counter_stream` — la décision d'autorisation sortie du handler
  WebSocket, donc testable sans socket. Le reste du handler n'est que du
  transport.
- `TicketService.recall` + `POST /{id}/recall` — rediffuse `your_turn` et
  `ticket_called`, renvoie le SMS, **remet le compte à rebours ABSENT à zéro**
  (sans quoi la relance est une annonce dans le vide), trace l'action.
- `EventType.COUNTER_QUEUE_CHANGED` + `counter_agency` +
  `send_to_agency_counters` — les écrans de guichet d'une agence reçoivent enfin
  les créations, appels, services terminés, absences, transferts et relances.

Trois arbitrages méritent d'être notés :

- **Le plafond de relance est compté dans `audit_logs`, pas dans Redis.** Chaque
  relance coûte un vrai SMS ; un redémarrage de Redis ne doit pas rouvrir le
  droit d'en envoyer trois de plus au même client. Le journal est la source
  durable, et il porte déjà « qui a relancé quoi ».
- **L'événement de guichet est publié dans `_transition`, pas chez chaque
  appelant.** C'est le point unique par lequel passe tout changement d'état :
  aucune transition ne peut être oubliée. Contrepartie assumée — une transition
  annulée par un rollback émet un événement de trop. Le client y réagit en
  relisant l'état, donc l'annonce excédentaire est sans effet ; une transition
  manquée, elle, laisserait le poste muet jusqu'au prochain sondage.
- **Le message reste pauvre** : il dit *ce qui* a changé (`reason`, statut,
  numéro), pas l'état complet de la file. Inventer ici un format que le desktop
  devra suivre à vie serait un contrat gratuit.

Suite locale : **492 passés, 138 ignorés, 0 échec** (+32). CI : **run #39 verte**
(2 min 06 s), PostgreSQL 15 + Redis 7 réels.

---

## Phase 7 — Algorithme d'absence et de reprise — 🟡 LOT 1+2 LIVRÉ

Spécification externe « Queue Engine — Absence & Recovery » v1.0. Audit complet
dans `docs/AUDIT-QUEUE-ENGINE-ABSENCE.md` ; ce qui suit n'en retient que les
défauts réels et ce qui a été corrigé.

Le dépôt couvrait déjà l'essentiel sous d'autres noms (`SERVED ≡ DONE`,
`Institution ≡ Organization`, la file est le triplet `(org, branch, service)`).
L'atomicité de l'appel était solide. Trois défauts, en revanche, étaient réels :

| Défaut | Ce qui se passait |
|---|---|
| Aucun contrôle du délai de grâce | Un agent pouvait déclarer absent un client **une seconde** après l'avoir appelé. Le client qui traversait la salle finissait derrière tout le monde pour avoir marché trop lentement. |
| Fenêtre de récupération ancrée sur `called_at` | 180 s d'écart, et `called_at` étant écrasé à chaque appel, une reprise suivie d'un nouvel appel repoussait silencieusement l'échéance d'expiration. |
| Aucun `missed_count` | Un client pouvait revenir indéfiniment : deux SMS et 180 s de guichet par cycle, sans fin. |

Ce qui a été fait :

- **Les échéances sont devenues des données** (migration `011`) :
  `grace_expires_at`, `missed_count`, `last_missed_at`, `recovery_expires_at`,
  `requeued_at`, `updated_at`, plus l'index `(status, recovery_expires_at)` qui
  est exactement le prédicat du worker d'expiration.
- **Grâce à 90 s, filet à 180 s.** À 90 s le bouton « Client absent » s'ouvre et
  l'agent confirme ; à 180 s le système rattrape. L'automatisme est conservé
  parce qu'un ticket `CALLED` bloque `call_next` : sans filet, un poste abandonné
  fige toute la file de ce guichet. La spécification voulait la confirmation
  seule ; c'est un écart assumé, documenté.
- **Dérogation superviseur auditée.** Un superviseur peut déclarer absent avant
  la grâce, et l'acte est journalisé (`TICKET_ABSENT_OVERRIDE`) avec le temps
  restant. Un agent, non : il n'a pas autorité pour raccourcir le délai qu'on
  lui impose.
- **La priorité est conservée à la reprise.** Le ticket revient en fin de SA
  tranche. Le code précédent forçait STANDARD à vie : une femme enceinte perdait
  définitivement sa priorité après un appel manqué.
- **Plafond de deux absences**, vérifié avant la reprise.

Deux points méritent d'être notés :

- **La reprise de données de la migration n'est pas cosmétique.** Sans elle,
  tous les `ABSENT` existants auraient eu `recovery_expires_at = NULL` et
  seraient devenus **immortels** : le worker ne les aurait plus jamais vus. Un
  correctif qui crée des zombies n'est pas un correctif — d'où une règle de
  repli qui garde ces lignes expirables.
- **Relancer repose la grâce.** Sans cela, « relancer puis déclarer absent »
  aurait été un contournement du contrôle en deux clics.

**Preuve :** `tests/test_phase7_absence_recovery.py` (19). Les quatre correctifs
ont été retirés un par un (grâce, priorité, ancrage de l'expiration, plafond) et
la suite tombe à chaque fois. Suite locale : **649 passés, 0 ignoré, 0 échec**.

**Le délai de grâce a cassé cinq tests — et c'est la CI qui l'a vu.** Les tests
`mark_absent` après `_call` (libération du guichet, idempotence du scheduler,
périmètre des agents, événements temps réel) attendaient l'absence immédiate :
ils butaient désormais sur le `409 GRACE_PERIOD_NOT_ELAPSED`, ce qui est le
comportement **voulu**. Ces cinq tests vivent dans des fichiers qui exigent un
vrai Redis, donc ignorés en local — d'où deux passages en CI rouge pour un
défaut invisible sur la machine de développement. Ils antidatent maintenant
l'échéance via `_grace_ecoulee()` : ils continuent de tester ce qu'ils testent,
sans simuler un contournement du contrôle.

La cause profonde reste traitée : `_fakeredis_local.py` (plugin pytest, activé
par `-p _fakeredis_local`) branche `aioredis.from_url` sur fakeredis et rejoue
les 138 tests « vrai Redis » sans Docker. Il ne remplace pas la CI — la
sémantique de fakeredis n'est pas celle de Redis 7 — mais il ferme le trou dans
lequel je suis tombé deux fois. Suite locale : **649 passés, 0 ignoré** ;
**CI #43 verte**, toutes les étapes comprises (migrations depuis une base vide et
tests).

Reste de la spécification, dans cet ordre : index unique « un ticket actif
global » (`012`), `queue_sequence BIGINT` (`013`), Outbox (`014`).

---

## Phase 8 — Garanties structurelles — ✅ LOT 3, 4 et 5 LIVRÉS

Les trois lots qui restaient ne corrigent aucun défaut visible : ils transforment
des règles tenues par du CODE en règles tenues par la BASE. La différence
n'apparaît pas en test, elle apparaît le jour où une seconde instance, un script
de maintenance ou un redémarrage au mauvais moment contourne le code.

### `012` — un seul ticket actif, garanti par la base

L'invariant reposait sur un `SELECT` applicatif et un verrou Redis. Deux
conséquences mesurables :

| Défaut | Ce qui se passait |
|---|---|
| Non garanti entre instances | deux instances pouvaient franchir le `SELECT` en même temps ; seule la fenêtre du verrou les séparait |
| Disponibilité liée à Redis | Redis tombait → `503 QUEUE_UNAVAILABLE`. L'invariant tenait, la prise de ticket non |

Un **index unique partiel** `WHERE status IN (actifs)` rend l'invariant
structurel. Trois effets en cascade, tous voulus :

- le verrou Redis devient une **optimisation**, plus une condition : son échec
  n'interrompt plus la création ;
- une violation de contrainte est traduite en `409 TICKET_ALREADY_ACTIVE` **avec
  le ticket gagnant** — le client voit son vrai ticket au lieu d'une erreur 500 ;
- **la création ne dépend plus de Redis du tout.** Il restait un piège : le
  compteur de numéros (`INCR` Redis) levait avant même d'arriver à la file. Un
  repli sur `MAX(sequence)+1` en base a été ajouté. Deux créations strictement
  simultanées pendant une panne peuvent alors partager un numéro affiché — un
  désagrément, pas une corruption (le ticket reste unique par son `id`), et
  infiniment moins grave qu'une agence qui ne délivre plus rien.

La reprise de données n'est pas cosmétique : l'index ne peut pas être créé s'il
existe des doublons, et il en existe. On garde le ticket actif **le plus récent**
de chaque utilisateur — celui qu'il est en train de vivre — on annule les autres
et on **libère les guichets** qui les servaient, sinon un guichet resterait
bloqué sur un ticket clos.

**Effet de bord assumé :** une création qui échoue faute de Redis ne détruit plus
le ticket. Il reste `WAITING`, `queue_degraded = true`, position inconnue, et
`rebuild_queue` le remet en file au retour de Redis. C'est l'arbitrage déjà
retenu pour `return_to_queue` (P1-11) : on dégrade, on ne jette pas le travail
déjà accepté.

### `013` — `queue_sequence`, un ordre qui ne doit rien à l'horloge

Le score Redis était `(10 - priorité) × 10⁹ + created_ms`. Correct presque
toujours — sauf pour deux tickets créés dans la **même milliseconde**, alors
départagés par l'ordre lexicographique de leur UUID. Stable, mais arbitraire, et
sans explication possible au client qui voit passer son voisin.

`queue_sequence` est un entier monotone attribué par une séquence PostgreSQL. Le
backfill vaut `ROW_NUMBER() OVER (ORDER BY created_at, id)` : **l'ordre existant
est figé, aucune file n'est réordonnée par la migration**.

La géométrie devient plus simple et plus forte :

- la séquence est bornée par la marge, donc l'écart maximal possible est
  inférieur à la marge — **la priorité l'emporte inconditionnellement**, alors
  qu'avant elle ne l'emportait que si l'écart de création restait sous 11,57 jours ;
- une reprise en file reçoit une **nouvelle** séquence : « fin de sa tranche »
  n'est plus une conséquence heureuse de `created_ms = maintenant`, c'est une
  valeur strictement supérieure à tout ce qui attend.

### `014` — Outbox : l'événement ne peut plus être perdu

`publish_event` diffuse après le `commit()` et avale ses erreurs. Bon choix pour
le métier, mauvais pour la salle d'attente : Redis qui tombe entre les deux et
l'écran ne voit jamais l'appel.

L'événement est désormais écrit dans `event_outbox` **dans la transaction
métier** — donc les deux réussissent ou échouent ensemble — puis diffusé
immédiatement. Si la diffusion échoue, la ligne reste en attente et le scheduler
la rejoue. Contrat « au moins une fois » : un doublon est possible si le
processus meurt entre la diffusion et la mise à jour ; les consommateurs le
tolèrent (ils relisent l'état).

Deux garde-fous d'exploitation : plafond de 5 tentatives (une ligne irrécupérable
sort du circuit **sans être supprimée** — on veut pouvoir répondre à « pourquoi
cet écran n'a rien reçu ? ») et péremption à 1 h (au-delà, l'écran s'est
resynchronisé dix fois).

**Preuve :** `tests/test_phase8_engine_guarantees.py` (15). Les lots ont été
vérifiés par mutation : retirer le traitement de `IntegrityError`, le repli de
numérotation, ou le rejeu de l'Outbox fait tomber la suite. Suite locale :
**665 passés, 0 ignoré, 0 échec**.

**Trois tests existants ont dû changer de camp** — et c'est instructif : cinq
fixtures créaient plusieurs tickets ACTIFS pour le même client, ce que l'index
interdit désormais. Le code avait raison, les fixtures décrivaient un monde
impossible. Un test (`test_reprise_refusee_si_un_autre_ticket_est_actif`) ne
testait plus qu'une branche devenue inatteignable : il vérifie maintenant que
c'est la BASE qui refuse, là où le refus se produit réellement.

### Preuve locale sur un vrai PostgreSQL 18

Jusqu'ici les trois lots n'étaient prouvés que par la CI et par la restitution
SQL hors ligne — le PostgreSQL local refusait les connexions. Un cluster
jetable (`initdb` dans un répertoire temporaire, port 55432, authentification
`trust`, supprimé après usage) a permis de rejouer la chaîne complète depuis une
base vide : **`alembic upgrade head` applique les 21 migrations jusqu'à
`014_event_outbox`**, sans erreur.

Le schéma réellement produit confirme le piège documenté :

```
CREATE UNIQUE INDEX uq_tickets_active_per_user ON public.tickets
  USING btree (user_id)
  WHERE (status = ANY (ARRAY['WAITING'::ticketstatus, 'CALLED'::ticketstatus,
    'SERVING'::ticketstatus, 'ABSENT'::ticketstatus,
    'TRANSFERRED'::ticketstatus, 'INCOMPLETE'::ticketstatus]))
```

`ticketstatus = WAITING, CALLED, SERVING, DONE, ABSENT, TRANSFERRED, CANCELLED,
INCOMPLETE` — les libellés sont bien les noms de membres en majuscules. Un
prédicat écrit en minuscules aurait compilé et n'aurait filtré aucun ticket.

Cinq assertions fonctionnelles ont ensuite été jouées sur cette base :

| # | Assertion | Résultat |
|---|---|---|
| 1 | un ticket `WAITING` s'insère | accepté |
| 2 | un 2e ticket actif pour le même client | **refusé par la base** (`unique_violation`) |
| 3 | le premier passe en `DONE` | accepté |
| 4 | un nouveau ticket actif ensuite | accepté — l'index est bien partiel |
| 5 | un ticket `CANCELLED` ne bloque pas le client | accepté |
| 6 | `nextval` de `ticket_queue_sequence_seq` | 1 puis 2 |

**Défaut latent trouvé au passage.** `queue_sequence` a pour défaut serveur `0`,
et la séquence n'est consommée que par `TicketService`. Les trois lignes
insérées par le test sont arrivées avec `queue_sequence = 0` alors que la
séquence valait déjà 2 : tout ticket créé hors du service (script de
maintenance, jeu de données, futur point d'entrée) passe donc **devant** les
clients déjà en file. Le correctif tient en une migration `015` donnant à la
colonne le défaut `nextval('ticket_queue_sequence_seq')` ; le modèle garderait
`0` pour rester portable sur SQLite, où la séquence n'existe pas.

---

## Ce que ce plan ne couvre pas

- **Charge réelle** : aucune mesure sous 12 postes simultanés. Le N+1 est une
  estimation analytique.
- **Multi-instances** : le code est écrit pour (verrous Redis, `ZREM` comme verrou
  d'échéance) mais Render n'expose qu'une instance. Non éprouvé.
- **Sécurité offensive** : pas de pentest, pas de fuzzing.
- **Migrations sur base volumineuse** : l'index d'expression de `008` n'a pas été
  mesuré sur des millions de lignes.

---

## Décisions prises le 15/11/2026

1. **Fenêtre d'ouverture ULBU — `08:00 → 17:00`.** Appliquée en production le
   15/11 à 15:52 via `PATCH /organizations/{org}/branches/{id}`. Le test doit se
   dérouler dans ce créneau : hors horaires, `POST /tickets` répond
   `400 BRANCH_CLOSED`.
2. **Expiration à la fermeture de l'agence**, pas sur une durée fixe.
   `WAITING_TICKET_TTL_HOURS` reste à 72 h comme filet de sécurité seulement.
3. **Pas de confirmation de présence** (`PENDING_ARRIVAL`). Le parcours reste
   appel → 180 s → `ABSENT` → retour possible sous 30 min.
4. **`BOOTSTRAP_PASSWORDS_JSON` sera retiré de Render** une fois le pilote
   stabilisé. En attendant il porte les quatre nouveaux mots de passe, pour
   qu'un bootstrap ne rétablisse pas l'ancien.
5. **La journée du rapport est locale, pas UTC.** `REPORT_LOCAL_UTC_OFFSET_HOURS`
   vaut 2 (Burundi, sans heure d'été). Un rapport « du 14 » couvre 22:00 UTC le
   13 → 22:00 UTC le 14, et l'heure de pointe est exprimée en heure locale. En
   UTC, la journée commencerait à 02:00 sur place et couperait la matinée.
   Décalage entier uniquement : un fuseau à demi-heure demanderait `zoneinfo`.
6. **Le rapport quotidien n'est pas envoyé par une boucle in-process.**
   `daily-report --send` est prévu pour un cron externe. Une boucle dans le
   scheduler serait liée à une instance et pourrait envoyer deux fois après un
   redémarrage ; un CLI est idempotent par construction (on choisit la date).

## Séquence recommandée, en une ligne

**~~CI Redis → P0-7/5/12 → P1-4~~ (fait) → ~~P1-11/15/16/18 → P1-8~~ (fait) →
~~P3-10/9/17 → mot de passe oublié + rapport quotidien~~ (fait) →
~~P2-13/14 + 401/403~~ (fait) → ~~canal guichet~~ (fait) → back-office →
desktop signé → facturation.**

Tout ce qui relève de ce dépôt est livré : P0, fiabilité du moteur, supervision,
sécurité/conformité, les deux fonctions produit qui manquaient, la dette
technique, et le canal temps réel du guichet. Ce qui reste — back-office,
distribution desktop, facturation — ne se code pas ici.

## Journal d'avancement

| Lot | Contenu | Commit |
|---|---|---|
| Audit | 20 constats, preuves de production, plan | `3e67f8f` → `3878af0` |
| Phase 0 | P1-7, P0-5, P1-12, P0-4 + décisions 1-4 | `137735b` |
| CI | Correction des tests révélés par la CI (447 passed) | `eaa4a3c` |
| Phase 1 | P1-11, P2-15, P2-16, P2-18 | `790a01a` |
| Phase 2 | P1-8 — `GET /tickets/queue` | `790a01a` |
| Phase 3 | P3-10, P3-9, P3-17 + migration `009_audit_org_nullable` + `GET /audit/logs` | `9f2ae62` |
| Phase 4 | Mot de passe oublié (SMTP, jeton usage unique) + `daily-report` | `9f2ae62` |
| Phase 5 | P2-13 + migration `010`, P2-14, 401/403 | `53f9ff5` |
| Phase 5 bis | SMTP visible dans `/health/detailed`, sondes en `HEAD` | `a69b37d` |
| Phase 6 | Canal guichet : autorisation WS multi-organisations, `POST /recall`, événements de file vers les écrans | `f380e39` |
| Phase 7 (1+2) | Absence/reprise : échéances persistées (migration `011`), grâce, plafond d'absences, priorité conservée | `37b7b03` + `b11ee83` |
| Phase 7 (tests) | Cinq tests d'absence antidatent la grâce ; pont fakeredis pour rejouer les tests « vrai Redis » en local | `6106d8b` (CI #43 verte) |
| Phase 8 | `012` index unique ticket actif + création indépendante de Redis, `013` `queue_sequence`, `014` Outbox | `3712a46` (CI #45 verte, 665 tests) |
| Poste (8b) | Le guichet suit le moteur d'absence : corps d'erreur conservé, grâce respectée avant l'état local, tri par `queue_sequence` | `8736684` — dépôt `tonde-desktop` (CI verte) |

### Ce que les phases 3 et 4 ont appris

Deux défauts réels ont été trouvés par les tests, pas par la relecture :

- **Le jeton de réinitialisation était stocké en clair** dans l'index inverse
  Redis (`tonde:pwdreset_user:{user_id}`), alors que la clé principale n'en
  gardait que le SHA-256. Un dump Redis livrait les liens directement.
- **Demander un nouveau lien n'invalidait pas l'ancien.** Le docstring
  l'affirmait ; le code écrasait l'index sans supprimer le jeton précédent, qui
  restait valable jusqu'à expiration.

Les deux sont corrigés et couverts. La règle qui en sort : une affirmation en
commentaire n'est pas une garantie — seul un test l'est.

Suite locale : **429 passés, 132 ignorés, 0 échec**. CI : **run #33 verte**
(2 min 38 s, PostgreSQL 15 + Redis 7 réels), soit 72 tests de plus qu'en phase 2.

### Ce que la phase 5 a appris

- **Une tolérance de test ne vérifie rien.** Deux tests existants acceptaient
  `status_code in (401, 403)` pour une requête sans jeton. Ils sont passés au
  vert pendant que le comportement était faux, et n'auraient pas bougé si on
  l'avait cassé davantage. Resserrés à `401` avec le code d'erreur attendu.
- **« Ça touche toutes les routes » est une intuition, pas un fait.** Le
  correctif 401/403 redouté comme un lot transverse a tenu en une dépendance :
  toutes les routes protégées passent par `get_current_user`.
- **Un test de performance doit être un test d'invariance.** Compter les requêtes
  pour 2 services puis pour 8 et exiger l'égalité échoue dès qu'un N+1 revient ;
  « moins de 10 requêtes » n'aurait rien empêché.
- **Le repli d'une panne ne doit pas être plus coûteux que la panne.** Le `except`
  Redis de l'écran de salle faisait une requête par service : la panne Redis
  devenait une surcharge PostgreSQL. Regroupé.
- **Un index partiel, pas un index unique global.** Rendre le préfixe unique sur
  toute la table aurait interdit de réutiliser la lettre d'un service désactivé —
  le cas normal d'un guichet qui change de prestation. La contrainte devait
  épouser la règle métier, pas la simplifier.
- **Une panne invisible est une panne qui dure.** Le SMTP non configuré ne se
  voyait nulle part, parce que la réponse neutre de « mot de passe oublié » est
  précisément ce qui empêche de signaler quoi que ce soit. D'où
  `email_gateway.status` dans `/health/detailed`.

Suite locale : **460 passés, 138 ignorés, 0 échec**. CI : **run #37 verte**
(2 min 18 s), `alembic upgrade head` inclus sur PostgreSQL 15.

### Ce que la phase 6 a appris

- **Un canal qui n'écoute rien ne se voit pas dans les tests.** `connect_counter`
  et `send_to_counter` étaient chacun testés et verts ; personne ne testait le
  lien entre les deux. Un dictionnaire alimenté et jamais parcouru est une
  fonctionnalité morte qui a l'air vivante.
- **Un champ dénormalisé ne peut pas porter une règle multi-valuée.**
  `User.org_id` est mono-organisation ; le modèle autorise plusieurs `Employee`.
  Tant qu'un contrôle lit le champ, le cas minoritaire est un bug — et ce
  minoritaire est `super_admin`, c'est-à-dire l'équipe qui doit diagnostiquer.
- **La bonne façon de tester un handler WebSocket est de ne pas le tester.** La
  décision a été extraite dans une fonction pure de transport ; 13 tests
  couvrent l'autorisation sans ouvrir une seule socket.
- **Un plafond doit être compté là où il survit.** Un compteur Redis aurait été
  plus rapide et faux : le redémarrage remet le compteur à zéro, et l'argent
  perdu ne revient pas.
- **Il faut distinguer les deux modes de panne de `publish_event`.** Le simuler
  par un `raise` teste le mock ; en production il retourne `False`. Les deux
  fixtures existent, chacune pour ce qu'elle prouve vraiment.
- **Une mutation vérifie qu'un test mord.** Les trois correctifs ont été retirés
  un par un pour confirmer que la suite tombe : sans cela, 32 tests verts
  pouvaient aussi bien ne rien tester.

### Ce que la phase 8 a appris

- **Un index déplace l'invariant du code vers la base — et démasque les décors
  impossibles.** Cinq fixtures créaient plusieurs tickets actifs pour un même
  client. Le code les tolérait, la base non. Ce n'était pas le comportement
  testé qui était faux, c'était le décor.
- **Un test qui ne tourne qu'en CI ne protège rien pendant le développement.**
  Les 138 tests « vrai Redis » étaient ignorés en local : deux passages rouges
  pour un défaut invisible sur la machine. fakeredis ne remplace pas Redis 7,
  mais il rend la suite locale représentative.
- **Deux quantités dans un même nombre finissent par se contredire.** Le score
  Redis mêlait priorité et millisecondes : une séquence assez grande pouvait
  franchir une bande de priorité. Bande × priorité, puis séquence — l'ordre est
  vrai par construction. Les tests de géométrie passaient des millisecondes là
  où le code attendait une séquence ; ils décrivaient l'ancien contrat.
- **Un rejeu n'existe que si le service en panne n'est pas sur le chemin.**
  L'Outbox ne vaut que parce que le scheduler republie sans Redis, avec un
  abandon explicite (`MAX_ATTEMPTS`, `MAX_AGE_MINUTES`) plutôt qu'une boucle
  silencieuse.

### Ce que l'adaptation du poste a appris

- **Une erreur qui jette son corps oblige l'interface à deviner.** `ApiError`
  ne gardait que `code` : `seconds_left` arrivait bien, personne ne pouvait le
  lire. Le guichet affichait « opération refusée » là où le serveur disait
  « attendez 42 s ».
- **Un contrôle qui n'existe que côté serveur fait clignoter l'écran.** Le refus
  de la grâce était correct, mais le poste vidait l'écran de service avant de le
  restaurer sur le `409`. Le contrôle local n'ajoute aucune règle : il évite un
  aller-retour qui déplace l'affichage sous les yeux de l'agent.
- **Un raccourci clavier contourne tout ce qui n'est pas dans la même
  fonction.** Le bouton respectait la grâce, la touche A non. Les deux appellent
  désormais le même prédicat.
