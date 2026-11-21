# AWTE — calcul et exploitation

AWTE (Adaptive Waiting Time Engine) estime, pour chaque ticket en attente, le
temps restant avant son premier appel. C'est un calcul statistique déterministe
fondé sur les événements réellement enregistrés ; il ne s'agit pas d'un modèle
d'IA autonome. Il ne change jamais l'ordre ni les transitions du Queue Engine.

## 1. Durée de traitement apprise

Une observation normale est la durée `done_at - served_at` d'un ticket terminé.
`service_seconds` n'est pas utilisé comme substitut si les horodatages métier
manquent ou sont incohérents. Les tickets annulés, absents, interrompus ou
transférés sont conservés avec `eligible=false` et un motif d'exclusion ; ils ne
modifient pas les profils.

L'EWMA démarre à `avg_duration_minutes × 60`, durée configurée du service, puis
chaque observation valide applique :

```text
nouvelle_durée = alpha × durée_réelle + (1 - alpha) × ancienne_durée
```

`alpha` est configurable par service (validation backend : 0,2 à 0,3 ; défaut
0,25). Le profil service est utilisé après `eta_min_service_samples` observations
(défaut 3). Le profil du guichet remplace celui du service après
`eta_min_counter_samples` observations de ce service à ce guichet (défaut 10).
Sinon le moteur revient au service puis à la durée configurée. Les seuils sont
stockés dans PostgreSQL avec le service.

Les durées extrêmes ne sont ni plafonnées arbitrairement ni effacées. Après au
moins `eta_outlier_min_samples` observations éligibles (défaut 10), les
100 observations éligibles les plus récentes servent à calculer médiane et MAD.
Une nouvelle durée est exclue si son écart à la médiane dépasse
`eta_outlier_mad_multiplier × 1,4826 × MAD` (défaut 3). Un MAD nul ne permet pas
de distinguer une nouvelle réalité durable d'une anomalie : la donnée est
conservée et admise. Toute exclusion reste auditée dans `eta_observations`.

## 2. Prévision individuelle

Pour chaque service ciblé, le moteur lit les tickets WAITING en base, puis les
ordonne par priorité effective du Queue Engine, séquence de file, date de
création et identifiant. Les guichets comptés comme capacité sont actifs,
ouverts et hors pause.

Chaque guichet est représenté par son délai estimé avant disponibilité :

- libre : zéro seconde ;
- ticket `SERVING` : moyenne des durées historiques comparables qui dépassent
  déjà le temps écoulé, moins le temps écoulé ; si aucune observation ne survit,
  le profil appris/configuré sert de repli avec confiance abaissée ;
- ticket `CALLED` : sa durée de service est comptée en entier, car l'heure de
  début de service n'est pas connue. Aucun délai fixe de prise en charge n'est
  inventé.

Chaque ticket précédent est affecté à la voie actuellement estimée la plus
rapide ; sa durée EWMA est ajoutée à cette voie. Sa prévision est le délai de la
voie avant l'ajout de son propre traitement. Les minutes affichées sont arrondies
à la minute supérieure. La position du prochain ticket arrivé est calculée
après les tickets déjà en attente.

Si aucun guichet utilisable n'existe, le résultat est `null` / `unavailable`.
Cela signifie qu'aucune prévision fondée sur la capacité actuelle n'est
disponible ; il n'y a pas de file virtuelle fictive.
Une erreur temporaire du calculateur produit également une ETA `null` : le
backend conserve la prise de ticket, mais ne remplace pas une prévision
indisponible par une multiplication de la position et d'une durée moyenne.

## 3. Persistance, métriques et événements

- `eta_observations` conserve chaque durée observée, son éligibilité et sa
  justification ; `event_key` rend le traitement idempotent.
- `eta_service_profiles` et `eta_counter_profiles` conservent les EWMA et les
  nombres d'observations.
- `eta_predictions` conserve chaque version significative de prévision par
  ticket, avec niveau du profil, confiance, version et horodatage. À l'appel,
  la dernière version est évaluée contre le temps écoulé depuis son calcul. Pour
  éviter qu'un ticket avec beaucoup d'actualisations pèse plusieurs fois, la MAE
  ne compte que la dernière version de chaque ticket ; les versions précédentes
  restent auditables.
- Au premier appel du ticket, l'attente réelle évaluée est le temps entre
  `calculated_at` de la dernière prévision enregistrée et `called_at`. L'erreur
  absolue est `|prévision - attente réelle|`. La MAE est la moyenne des erreurs
  absolues évaluables ; les prévisions `null` ne contribuent pas à l'erreur.

Les prévisions sont exposées dans les réponses ticket et sur l'écran public
`GET /api/v1/branches/{branch_id}/display`. Les snapshots WebSocket utilisent le
canal de file existant et comprennent ETA par ticket, confiance, heure et
version. L'endpoint de lecture autorisée est :

```text
GET /api/v1/organizations/{org_id}/branches/{branch_id}/services/{service_id}/eta/metrics
    ?since=<date ISO-8601>&until=<date ISO-8601>
```

Il retourne la configuration, les profils et la MAE. L'accès est contrôlé par
les mêmes dépendances de rôle et d'appartenance de branche que l'administration
des services. Les lectures d'observations, de files et de prévisions sont
scopées par organisation et service. `waiting_time_accuracy.by_counter` ventile
la MAE par guichet effectivement attribué au ticket lors de son premier appel.

L'outbox existante garde les snapshots AWTE après leur mise en file durable, puis
les publie par le diffuseur existant. Les API de lecture recalculent la vue
courante depuis PostgreSQL ; après reconnexion, le client peut donc récupérer un
état actuel sans dépendre d'un message manqué. Redis garde son rôle de projection
opérationnelle et de distribution, pas de base d'apprentissage.

## 4. Changement de capacité

Les endpoints admin de guichet incluent :

```text
POST /api/v1/organizations/{org_id}/branches/{branch_id}/counters/{counter_id}/pause
POST /api/v1/organizations/{org_id}/branches/{branch_id}/counters/{counter_id}/resume
```

Pause requiert un guichet ouvert et libre. Un guichet en pause, fermé ou
désactivé n'est pas disponible pour une nouvelle charge ; sa reprise déclenche
un snapshot actualisé. Les changements d'ouverture, de fermeture,
d'activation et de paramètres AWTE rafraîchissent aussi les files concernées.

## 5. Limites connues avant déploiement général

1. Le Queue Engine ordonne séparément les files par service et ne formalise pas
   de politique globale d'arbitrage entre services pour un même guichet. Un
   guichet ouvert est projeté pour chaque service ; si d'autres files attendent
   dans l'agence, la confiance baisse. Tant qu'une règle métier d'affectation
   inter-services n'existe pas, cette ETA est moins précise qu'une planification
   globale de capacité.
2. Un ticket appelé sans début `served_at` consomme sa durée entière dans la
   projection ; le temps d'appel au début de prise en charge est inconnu et
   n'est pas simulé.
3. Le snapshot est persisté dans l'outbox avec ses prévisions, mais après le
   commit de transition du Queue Engine. La garantie d'atomicité entre toute
   transition métier et son événement nécessite d'intégrer l'outbox dans chacune
   de ces transactions.
4. Les tests locaux n'établissent pas à eux seuls les garanties d'intégration
   PostgreSQL + Redis + WebSocket en environnement multi-instance. Une migration
   PostgreSQL et les scénarios de concurrence doivent être validés dans la CI
   utilisant ces services avant une mise en production générale.

Le rapport des composants réutilisés et des lacunes initiales est dans
[l'audit AWTE](AUDIT-AWTE-PRODUCTION.md). La migration à appliquer est
`015_awte_production` (`alembic upgrade head`).
