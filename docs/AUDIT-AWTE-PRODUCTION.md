# Audit préalable AWTE

Date : 21 novembre 2026
Périmètre : estimation individuelle du temps d'attente, intégration au Queue Engine.

## Composants existants réutilisés

- `Service` portait déjà `avg_duration_minutes`, la durée de repli configurable.
- `Ticket` fournit le cycle métier et ses horodatages (`created_at`, `called_at`,
  `served_at`, `done_at`), ainsi que le statut, la priorité, le service et le
  guichet. `QueueLog` conserve les actions de service, transfert, absence et
  interruption.
- Le Queue Engine garde PostgreSQL comme source des tickets et Redis comme file
  opérationnelle reconstruisible. Il applique déjà priorité et ordre de file,
  avec protections contre les appels concurrents.
- `Counter` porte l'état ouvert/actif et le ticket courant. Les opérations
  d'ouverture, fermeture et de cycle ticket sont déjà centralisées dans les
  services métier.
- Les réponses ticket, l'écran public et les mises à jour de file passent par
  des services/endpoints existants. Le canal WebSocket et l'outbox déjà présents
  sont réutilisés.

## Lacunes constatées avant finalisation

- Les durées configurées pouvaient alimenter une estimation de position, mais
  aucun historique AWTE durable ni profil EWMA service/guichet n'existait.
- L'estimation de file ne conservait pas la prévision individuelle émise et ne
  mesurait donc pas l'erreur contre l'attente réellement vécue.
- L'état de pause d'un guichet n'était pas représenté comme capacité séparée.
- Le calcul de file n'avait pas de signal explicite « aucune capacité » et les
  comportements de repli pouvaient afficher une durée sans fondement.
- Le contrôle CI existant attendait encore l'ancienne formule fixe. La projection
  multi-guichets a révélé que l'appel déjà en cours et les tickets en attente
  doivent tous contribuer à la prévision.

## Modules concernés

- Modèles et schémas : `app/models/eta.py`, `app/models/branch.py`,
  `app/models/counter.py`, `app/schemas/service.py`, `app/schemas/counter.py`.
- Calcul et transitions : `app/services/eta_service.py`,
  `app/services/ticket_service.py`, `app/services/counter_service.py`,
  `app/services/service_service.py`, `app/services/public_service.py`.
- Contrats : `app/routers/services.py`, `app/routers/counters.py`,
  `app/websocket/events.py`, `app/websocket/publisher.py`,
  `app/websocket/queue_ws.py`.
- Persistance : `migrations/versions/015_awte_production.py`.
- Documentation et couverture : `README.md`, `docs/AWTE-PRODUCTION.md`,
  `tests/test_eta_production.py` et les tests de file existants.

## Migration requise

La révision `015_awte_production` ajoute les paramètres de politique AWTE aux
services, le drapeau `counters.is_paused`, les tables d'observations, profils
EWMA et versions des prévisions individuelles. Elle reprend les tickets DONE
historiques uniquement lorsque `served_at` et `done_at` forment une durée
positive cohérente ; les autres sont conservés comme observations exclues. Les
profils historiques sont initialisés par replay chronologique EWMA avec le même
filtre MAD que les nouvelles observations.

## Risques et dépendances

- L'AWTE est une projection de charge : il ne déplace ni n'appelle de ticket et
  ne remplace pas Redis ni les décisions du Queue Engine.
- Le Queue Engine maintient des files ordonnées par service et ne définit pas de
  règle globale d'arbitrage entre services pour un guichet partagé. Les attentes
  concurrentes entre services sont donc un facteur d'incertitude, affiché par
  une confiance réduite ; elles ne peuvent pas être projetées exactement sans
  décision métier sur l'affectation inter-services.
- Pour un ticket `CALLED`, `served_at` n'existe pas encore. Le temps de passage
  appelé→début de service n'est pas inventé : la durée entière reste comptée et
  la confiance est réduite.
- Les estimations et leur événement outbox sont validés ensemble une fois le
  snapshot construit. La transition de ticket est validée auparavant par le
  Queue Engine ; il subsiste donc une fenêtre entre transaction métier et
  création du snapshot durable. Un outbox transactionnel couvrant chaque
  transition métier nécessiterait un refactoring transversal.
- Une exécution locale SQLite ne valide pas les migrations PostgreSQL, le
  comportement réel Redis, la diffusion WebSocket multi-instance ni les courses
  sous charge. La suite d'intégration contre ces services doit être exécutée
  avant un déploiement général.

## Conclusion de l'audit

Le changement peut compléter le moteur et les contrats existants sans créer un
second Queue Engine. Les risques ci-dessus restent des limites explicites : la
précision inter-services et l'atomicité complète outbox/transition ne doivent
pas être présentées comme garanties par cette livraison.
