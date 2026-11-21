# QAPE 018 — validation isolée (aucune production)

## Environnement
Python 3.12.16 ; PostgreSQL 15.20 ; Redis 7.0.15. PostgreSQL 127.0.0.1:5543 et Redis 127.0.0.1:6397, bases jetables distinctes. Révision 017 créée sur des copies de test. Aucune copie représentative des données réelles du projet n'a été fournie : les deux cas historiques ci-dessous sont des fixtures synthétiques, NON un inventaire du backfill réel.

## Dry-run
`psql -v ON_ERROR_STOP=1 -f scripts/qape_review_dry_run.sql` sur la copie 017 vide : 0 opérations, 0 doublon, 0 anomalie, 0 auto-décision (sortie `/tmp/qape018-dryrun-empty.log`). La colonne physique `abuse_events.metadata` est JSONB ; pas de colonne `details`.

Copie 017 synthétique avec deux opérations et deux audits : `DENIED` + auto-REJECT (1), `REVIEW_REQUIRED` + auto-APPROVE (1). Aucun autre doublon ou anomalie. Classification `LEGACY_SELF_DECISION`, génération proposée 1 pour DENIED et NULL pour REVIEW_REQUIRED ; ligne bloquante explicitement signalée (`/tmp/qape018-dryrun-synthetic.log`). `alembic upgrade head` sur cette copie échoue avec `Ambiguous QAPE human audit`, même lorsque le drapeau d'autorisation de test est présent. La version Alembic reste 017 : transaction annulée, aucun audit supprimé.

Troisième copie 017 synthétique avec seulement un auto-REJECT terminal : dry-run `LEGACY_SELF_DECISION` / `DENIED` / génération 1 (`/tmp/qape018-terminal-dryrun.log`). Sans autorisation explicite, upgrade refusé ; avec `QAPE_REVIEW_BACKFILL_AUTHORIZED=explicitly-approved-after-dry-run` (sur cette COPIE JETABLE seulement), upgrade 018 réussi, DENIED génération 1, un audit humain historique conservé (`/tmp/qape018-terminal-with-flag.log`). Ne PAS utiliser cette autorisation sur production sans rapport réel et approbation distincte.

## Correctifs et validation additionnelle
La migration 018 vérifie désormais les anomalies via un `SELECT EXISTS (...) OR EXISTS (...) OR EXISTS (...) OR EXISTS (...)` scalaire : la table `abuse_events` vide ne masque plus une opération `HUMAN_APPROVED` ou `HUMAN_REJECTED` sans audit. Elle rejette explicitement `reason_codes IS NULL` et un JSON différent d'un tableau. La contrainte `ck_qape_review_v2_complete` du modèle et de la migration exige soit les trois champs de cycle tous NULL (audit historique), soit un format v2 intégralement renseigné ; aucun état partiel n'est admis. Aucun audit historique n'est réécrit. L'inspection des deux chemins `TicketService` vers `REVIEW_REQUIRED` montre que `QapeCreateOperation.transition` incrémente une fois avant l'audit et le commit ; les rejeux en état REVIEW_REQUIRED échouent avant de réévaluer, et `qape_recovery.py` ne crée pas de revue. Aucune correction supplémentaire d'incrémentation n'a été nécessaire.

Sur le code corrigé, migration depuis base vide réussie (`/tmp/qape018-fix-fresh.log`) et migration d'une base 017 émulant l'ancien schéma sans `review_generation` (colonne et contrainte retirées UNIQUEMENT dans une base jetable) réussie (`/tmp/qape018-fix-old-upgrade.log`). Les `pg_dump --schema-only --no-owner --no-privileges` complets des deux bases, après retrait des seuls jetons aléatoires `\\restrict`/`\\unrestrict`, sont identiques octet pour octet, SHA-256 normalisé commun `6a211c4d494f756994d1ace8d8090dccc525723f182dc619bf904982d1266447`. Le numéro interne `attnum` de la colonne recréée dans la base de simulation 017 peut différer ; aucun serveur historique 017 réel n'était disponible.

Commandes finales : `PG_TEST_URL=postgresql+asyncpg://qape_test@127.0.0.1:5543/qape018_fix_tests DATABASE_URL=postgresql+asyncpg://qape_test@127.0.0.1:5543/qape018_fix_tests REDIS_URL=redis://127.0.0.1:6397/0 QAPE_REDIS_TEST=1 JWT_SECRET_KEY=ci-test-secret-key-not-for-production ENVIRONMENT=development CREATE_TABLES_ON_STARTUP=false /home/user/.venv-qape/bin/pytest -q --tb=short` : **734 passed, 209 warnings, 0 skipped** (`/tmp/qape018-fix-full.log`). Même environnement et `pytest -q --tb=short` suivi des fichiers ciblés revue, migration, création, récupération et Queue Engine : **147 passed, 4 warnings, 0 skipped** (`/tmp/qape018-fix-targeted.log`). Tests migration seuls : **10 passed, 1 warning, 0 skipped** (`/tmp/qape018-fix-migration-tests.log`) : deux opérations sans audit bloquées malgré une table d'audit vide, quatre audits v2 partiels refusés, audit historique à trois champs NULL conservé, transition répétée non incrémentée et revue ultérieure à génération supérieure. Aucune base de production ou historique réel n'a été consulté.

## Catégories illustratives
- `FIRST_REVIEW` : REVIEW_REQUIRED sans audit, génération 1 ; exemple opération créée lors d'un preflight exigeant revue.
- `LEGACY_APPROVE` : PENDING avec unique APPROVE cohérent, génération 1 ; la même requête reste idempotente.
- `NEW_REVIEW_AFTER_LEGACY_APPROVE` : REVIEW_REQUIRED avec unique APPROVE antérieur, génération 2 ; APPROVE(1) est périmé.
- `LEGACY_REJECT` : DENIED avec unique REJECT cohérent, génération 1.
- `LEGACY_SELF_DECISION` : auto-APPROVE non terminal, ambigu et bloquant ; auto-REJECT terminal, conservé et signalé (fixtures vérifiées).
- `AMBIGUOUS_*` : audit en double, décision/codes discordants, métadonnées absentes, portée/provenance/politique discordantes ou état contradictoire ; interrompre le backfill sans modifier les audits.

## Risques résiduels
Pas de copie représentative réelle ni d'effectifs réels de migration. Révision 017 importe dynamiquement le modèle ORM courant : une installation neuve de 017 possède déjà `review_generation` ; la révision 018 tolère cette situation tout en ajoutant le champ aux bases 017 préexistantes. Une bascule doit arrêter TOUS les writers ancienne version. Downgrade 018 interdit pour éviter de perdre la protection. Aucun déploiement effectué.
