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

## Phase 3 — Sécurité et conformité (ce qu'une banque demandera)

| # | Action | Taille | Dépendance |
|---|---|---|---|
| 10 | `websocket_counter` : vérifier l'affectation au guichet | S | — |
| 9 | Un WebSocket par ticket → plusieurs appareils par ticket | M | — |
| 17 | Audit des connexions (succès/échec) | M | **Migration** : `AuditLog.org_id` doit devenir nullable |
| 19 | ~~Changement de mot de passe~~ | ✅ fait | — |
| 6 | ~~Verrouillage par compte~~ | ✅ fait | — |

Le n° 17 n'est pas un oubli de l'audit : `AuditLog.org_id` est `NOT NULL` avec
clé étrangère. Un `super_admin` et un client OTP ont `org_id = NULL`, donc leurs
connexions ne peuvent pas y être écrites. Il faut une migration — c'est la seule
de tout ce plan.

« Qui s'est connecté, quand, depuis quelle IP » est une question qu'un service
informatique bancaire posera pendant l'évaluation, pas après la signature.

---

## Phase 4 — Produit : ce qui bloque une vente

Ces points ne sont pas des bugs. Ce sont des absences.

| Sujet | État | Taille |
|---|---|---|
| **Back-office admin** (`tonde-web`) | Maquette sur données en dur | L |
| **Distribution desktop** (signature, SmartScreen, auto-update) | Non signé | M |
| **Facturation / plan** | Aucun modèle d'abonnement | L |
| **Rapport quotidien** | Données présentes (`queue_logs`), aucun envoi | M |
| **Mot de passe oublié / email sortant** | Aucun SMTP | M |
| ~~Confirmation de présence~~ (`PENDING_ARRIVAL` + QR) | **Abandonnée** (décision 3) | — |

Ordre recommandé : **back-office → distribution desktop → facturation**.
Le back-office d'abord parce que sans lui, chaque nouvelle organisation passe
par `scripts/create_org.py` et par toi. La distribution ensuite parce qu'un MSI
non signé déclenche SmartScreen chez le client. La facturation en dernier : elle
n'a de sens qu'une fois qu'une organisation peut s'inscrire seule.

La **confirmation de présence** est écartée. Le problème des tickets pris depuis
chez soi est traité autrement : appel, puis 180 s pour se présenter, puis
`ABSENT` avec retour possible pendant 30 minutes. C'est moins élégant qu'un scan
de QR, mais ça ne change pas le parcours client et ça ne fausse pas les mesures
du pilote.

---

## Phase 5 — Dette assumée

| # | Action | Taille |
|---|---|---|
| 14 | `get_branch_display` : N+1 → 2 requêtes + cache 2 s | M |
| 13 | Unicité du préfixe de service à la réactivation | S |

Le n° 14 devient urgent dès 12 postes desktop qui interrogent l'écran de salle
toutes les 7 secondes : ~30 requêtes par appel, par poste. À mesurer avant
d'optimiser — l'audit ne l'a pas fait.

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

## Séquence recommandée, en une ligne

**~~CI Redis → P0-7/5/12 → P1-4~~ (fait) → ~~P1-11/15/16/18 → P1-8 (liste de la
file)~~ (fait) → P3-10/9/17 → back-office → desktop signé → facturation.**

Les deux premiers blocs sont livrés : les P0, puis la fiabilité du moteur et la
supervision. Il reste la sécurité/conformité (P3-10, P3-9, P3-17 — le seul point
qui demande une migration), puis du produit, pas de la correction.

## Journal d'avancement

| Lot | Contenu | Commit |
|---|---|---|
| Audit | 20 constats, preuves de production, plan | `3e67f8f` → `3878af0` |
| Phase 0 | P1-7, P0-5, P1-12, P0-4 + décisions 1-4 | `137735b` |
| CI | Correction des tests révélés par la CI (447 passed) | `eaa4a3c` |
| Phase 1 | P1-11, P2-15, P2-16, P2-18 | à venir |
| Phase 2 | P1-8 — `GET /tickets/queue` | à venir |
