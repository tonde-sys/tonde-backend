# Plan de développement TONDE — issu de l'audit du 15/11/2026

Source : `AUDIT-BACKEND-2026-11-15.md` (20 constats) + fonctions manquantes.
État de départ : **5 corrigés, 1 outillé, 14 ouverts.**

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

## Phase 0 — Avant que le partenaire touche le système

Objectif : ne pas griller le test sur un détail visible en dix minutes.

| # | Action | Taille | Pourquoi maintenant |
|---|---|---|---|
| 20 | Rotation des 4 comptes ULBU | ✅ fait | Mot de passe présent dans l'historique Git |
| — | Trancher la fenêtre ULBU `10:00 → 14:00` | S | Désormais réellement appliquée : hors créneau, aucun ticket n'est délivré |
| 7 | Fermer un guichet ne doit pas abandonner le client appelé | S | Pendant un test, un agent ferme son guichet : le client est marqué absent alors qu'il est présent |
| 5 | `deactivate` ne rétrograde que s'il ne reste aucun rattachement actif | S | 3 lignes, évite de casser un compte multi-organisations |
| 12 | `history` : `position` réelle ou `null`, jamais `0` | S | Le mobile affiche « position 0 » à un client qui attend |
| — | Service Redis dans la CI | S | **129 tests du moteur de file ne tournent jamais.** C'est l'angle mort le plus dangereux du projet |

**Point d'attention** : les 129 tests ignorés couvrent précisément l'atomicité de
la file (`ZPOPMIN`, compare-and-set, compensation). Aujourd'hui, une régression
sur le cœur du produit peut passer en vert. C'est S en effort et ça protège tout
le reste — à faire avant la phase 1, pas après.

---

## Phase 1 — Fiabilité du moteur de file

Objectif : le moteur ne perd plus de client, même quand une brique tombe.

| # | Action | Taille | Dépendance |
|---|---|---|---|
| 4 | Expiration 12 h : ne jamais expirer un ticket bien placé, notifier avant annulation | M | Décision produit sur la règle d'expiration |
| 11 | `return_to_queue` : repli si Redis échoue (le ticket reste `WAITING` mais hors file) | S | — |
| 15 | Séparer `get_queue_size` de `add_to_queue` (entrée fantôme si la taille échoue) | S | — |
| 16 | `queue_score` : documenter la marge de priorité (facteur 10⁹ = 11,5 jours) | S | — |
| 18 | `/health/ready` doit voir le scheduler mort | S | — |

Le n° 4 mérite une décision explicite avant le code : **expirer à la fermeture
de l'agence** plutôt que sur une durée fixe est le comportement correct, mais
change la sémantique des tickets pris en fin de journée. À trancher avec le
partenaire, pas dans le code.

---

## Phase 2 — Supervision : la fonction qui manque le plus

| # | Action | Taille | Dépendance |
|---|---|---|---|
| 8 | `GET /api/v1/tickets/queue?agency_id=&service_id=` (rôle ≥ AGENT, périmètre branche) | M | — |
| 8b | Écran desktop correspondant : qui attend, depuis quand, priorité | M | 8 |

C'est le complément naturel de `call-next`. Sans lui, un superviseur en agence
bondée est aveugle : il ne peut ni voir la file, ni repérer un ticket fantôme,
ni appeler un numéro précis. C'est aussi ce qui rend le poste guichet réellement
utile — aujourd'hui il appelle à l'aveugle.

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
| **Confirmation de présence** (`PENDING_ARRIVAL` + QR) | Non commencé | L |

Ordre recommandé : **back-office → distribution desktop → facturation**.
Le back-office d'abord parce que sans lui, chaque nouvelle organisation passe
par `scripts/create_org.py` et par toi. La distribution ensuite parce qu'un MSI
non signé déclenche SmartScreen chez le client. La facturation en dernier : elle
n'a de sens qu'une fois qu'une organisation peut s'inscrire seule.

La **confirmation de présence** est la seule réponse structurelle au problème des
tickets pris depuis chez soi, mais elle change le parcours client. À ne pas lancer
pendant un pilote : elle fausserait les mesures du test.

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

## Décisions à prendre (elles bloquent le code)

1. **Fenêtre d'ouverture ULBU** — `10:00 → 14:00` est maintenant appliquée.
   Si le test se déroule en dehors, aucun ticket ne sera délivré. Garder, élargir
   ou passer par `operating_hours` jour par jour ?
2. **Règle d'expiration** (P0-4) — durée fixe de 12 h, ou expiration à la
   fermeture de l'agence ? La seconde est correcte, la première est en place.
3. **Confirmation de présence** — avant ou après le pilote ? Elle change le
   parcours client et fausserait les mesures si lancée pendant.
4. **`BOOTSTRAP_PASSWORDS_JSON`** — à retirer de Render une fois le pilote
   stabilisé. Tant qu'il existe, les mots de passe vivent dans la config du
   service.

---

## Séquence recommandée, en une ligne

**CI Redis → P0-7/5/12 → P1-4/11/15/16/18 → P1-8 (liste de la file) →
P3-10/9/17 → back-office → desktop signé → facturation.**

Les deux premiers blocs tiennent en une semaine et suppriment le risque de perdre
un client pendant le test. Le troisième rend le produit supervisable. Le reste
est du produit, pas de la correction.
