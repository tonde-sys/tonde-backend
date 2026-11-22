# \[BACKEND · PRODUCTION\] Finalisation complète du moteur intelligent d'estimation du temps d'attente (AWTE)

**Projet :** TONDE\
**Composant :** Adaptive Waiting Time Engine (AWTE)\
**Priorité :** Haute · Préparation au déploiement en production\
**Périmètre :** Backend, persistance, calculs, événements temps réel et
tests\
**Statut initial :** Fonctionnalité partiellement implémentée, à auditer
avant toute modification

------------------------------------------------------------------------

## 1. Objectif de la tâche

Finaliser le moteur AWTE afin que TONDE fournisse à chaque utilisateur
une estimation individuelle, dynamique et continuellement actualisée de
son temps d'attente.

Le moteur doit exploiter les paramètres configurés par chaque
organisation ainsi que les données opérationnelles réellement observées.
Il doit tenir compte de l'état réel des files, des guichets, des tickets
en cours, des priorités et des interruptions opérationnelles.

**Exigence de livraison :** l'implémentation doit être réellement
fonctionnelle, persistante, testée et exploitable en production. Aucune
simulation, donnée fictive, réponse mockée, valeur métier codée en dur
ou placeholder ne doit être introduite dans le chemin d'exécution
destiné à la production.

Le travail doit compléter l'existant, et non créer un second moteur
parallèle.

## 2. Consignes impératives avant toute modification

### 2.1. Audit préalable obligatoire

Avant d'écrire ou de modifier du code, analyser en profondeur
l'implémentation actuelle et documenter les éléments suivants :

-   Les modèles SQLAlchemy et le schéma PostgreSQL liés aux
    organisations, services, guichets, tickets et événements de cycle de
    vie.
-   Les transitions de statut des tickets et les horodatages réellement
    enregistrés.
-   Le Queue Engine et ses règles de priorité, d'affectation, de
    transfert et de concurrence.
-   Les mécanismes existants de démarrage, d'appel, de mise en pause, de
    reprise, de transfert et de clôture des tickets.
-   Les clés, structures, TTL et stratégies d'invalidation Redis déjà
    utilisées.
-   Les gestionnaires WebSocket, les canaux de diffusion et les formats
    de payload existants.
-   Les endpoints de configuration et de consultation déjà disponibles.
-   Les tâches asynchrones, événements métier, transactions et
    mécanismes de synchronisation existants.
-   Les tests existants et les éventuelles lacunes de couverture.

Produire un court rapport d'audit indiquant : 1. Les composants déjà
implémentés et réutilisables. 2. Les fonctionnalités incomplètes ou
incorrectes. 3. Les fichiers et modules qui devront être modifiés. 4.
Les migrations de base de données nécessaires. 5. Les risques de
régression et les dépendances avec le Queue Engine.

Ne pas dupliquer une responsabilité déjà prise en charge par un
composant existant. Respecter les conventions architecturales et le
style du projet.

### 2.2. Exigences de production

-   **Aucune simulation :** ne pas utiliser de données simulées, de
    mocks dans le code de production, de valeurs arbitraires ou de
    résultats préfabriqués.
-   **Multi-tenant strict :** toute lecture, écriture, agrégation, mise
    en cache et diffusion doit être isolée par `tenant_id` et par les
    identifiants de service/guichet concernés.
-   **Source de vérité :** PostgreSQL doit conserver les configurations,
    les observations et les données nécessaires à l'audit. Redis peut
    accélérer les lectures et distribuer les projections temps réel,
    mais ne doit pas devenir l'unique source de vérité.
-   **Cohérence métier :** le moteur AWTE ne doit jamais contourner les
    transitions autorisées du Queue Engine ni modifier lui-même l'ordre
    des tickets en dehors des règles métier existantes.
-   **Robustesse :** les mises à jour doivent être sûres en cas de
    concurrence, de répétition d'événements, de redémarrage du serveur
    ou d'indisponibilité temporaire de Redis.
-   **Sécurité :** vérifier les autorisations côté serveur. Un
    utilisateur ne doit jamais pouvoir consulter les estimations ou les
    données opérationnelles d'un autre tenant.
-   **Compatibilité :** préserver les contrats API et WebSocket
    existants, sauf évolution explicitement documentée et coordonnée
    avec les clients.

------------------------------------------------------------------------

## 3. Spécifications fonctionnelles et techniques

## 3.1. Configuration initiale par service

Lors de la création ou de la configuration d'un service,
l'administrateur autorisé doit pouvoir définir une durée moyenne
initiale de traitement.

Exemples indicatifs de configuration :

  Service                 Durée initiale
  --------------------- ----------------
  Retrait                      5 minutes
  Ouverture de compte         15 minutes
  Consultation                12 minutes

Ces valeurs sont des exemples de configuration, pas des constantes à
intégrer au code.

La durée configurée doit :

-   Être enregistrée dans PostgreSQL et rattachée au tenant et au
    service concernés.
-   Être exprimée dans une unité cohérente, de préférence en secondes
    dans les calculs internes.
-   Être validée côté backend : valeur strictement positive, bornes
    raisonnables et type numérique adapté.
-   Être modifiable uniquement par un rôle autorisé.
-   Être utilisée comme estimation initiale tant que les observations
    réelles sont insuffisantes.
-   Être conservée comme référence de repli lorsque l'historique
    disponible ne permet pas une estimation suffisamment fiable.

Les paramètres du moteur, notamment le coefficient EWMA et les seuils
d'échantillonnage, doivent être configurables et persistés selon la
politique retenue par l'organisation. Ne pas disperser ces paramètres
dans le code.

## 3.2. Adaptation dynamique des durées de traitement avec EWMA

Implémenter ou finaliser l'algorithme **Exponentially Weighted Moving
Average (EWMA)** pour adapter la durée moyenne estimée de traitement à
partir des tickets effectivement terminés.

Formule :

\[ AWT_t = `\alpha `{=tex}D_t + (1-`\alpha`{=tex})AWT\_{t-1} \]

Où :

-   (D_t) : durée réelle de traitement du ticket terminé.
-   (AWT_t) : nouvelle durée moyenne estimée de traitement.
-   (AWT\_{t-1}) : estimation précédente.
-   (`\alpha`{=tex}) : coefficient de lissage, configurable. La plage
    initiale recommandée est de 0,2 à 0,3.

Initialisation :

\[ AWT_0 = T\_{`\text{configured}`{=tex}} \]

Les observations récentes doivent avoir davantage de poids, sans qu'une
seule observation anormale puisse déformer excessivement l'estimation.

### Règles de calcul

-   Calculer la durée de traitement à partir des horodatages métier
    fiables correspondant au début effectif et à la fin effective du
    traitement.
-   Ne pas confondre durée de traitement et temps d'attente.
-   Ne mettre à jour l'EWMA qu'une seule fois par ticket terminé.
-   Éviter les doubles mises à jour lorsqu'un événement est rejoué ou
    reçu plusieurs fois.
-   Ne pas utiliser les tickets annulés, absents, abandonnés ou
    interrompus administrativement comme des traitements terminés
    normaux.
-   Contrôler les durées aberrantes selon une politique documentée et
    configurable.
-   Conserver les observations brutes nécessaires à l'audit, même
    lorsqu'une observation est exclue du calcul statistique.

### Niveaux d'estimation

Le moteur doit prendre en charge les niveaux suivants :

1.  **Niveau guichet :** estimation propre au rythme de traitement du
    guichet.
2.  **Niveau service :** estimation agrégée du service, utilisée comme
    référence lorsque l'historique du guichet est insuffisant.
3.  **Niveau organisation :** paramètres et politiques de configuration
    propres au tenant.

Définir explicitement les seuils minimaux d'observations nécessaires
avant d'utiliser une estimation locale. Tant que ce seuil n'est pas
atteint, utiliser le niveau de repli approprié, sans présenter une
estimation locale comme statistiquement fiable.

Les agrégations doivent respecter le tenant, le service, la période et
les règles d'éligibilité des observations.

## 3.3. Calcul du temps d'attente individuel

La durée moyenne de traitement ne représente pas le temps d'attente
individuel. Le moteur doit estimer la charge de travail qui reste à
traiter avant que le ticket concerné puisse être appelé.

Le calcul doit tenir compte au minimum de :

-   La position réelle du ticket selon les règles de priorité
    applicables.
-   Les tickets qui le précèdent effectivement.
-   La durée de traitement estimée de ces tickets.
-   Les tickets déjà en cours de traitement.
-   Le temps restant estimé des traitements en cours.
-   Le nombre de guichets réellement actifs et compétents pour le
    service.
-   Les différences de capacité ou de vitesse entre guichets.
-   Les règles d'affectation des tickets aux guichets.
-   Les priorités, transferts et changements d'ordre autorisés.
-   Les pauses, indisponibilités et interruptions opérationnelles.
-   Les contraintes propres au service et à l'organisation.

### Exigences de calcul

-   Ne pas utiliser une simple multiplication
    `nombre_de_tickets × durée_moyenne` comme méthode générale.
-   Ne pas supposer que tous les guichets ont la même capacité.
-   Ne pas considérer un guichet en pause, désactivé ou indisponible
    comme une ressource active.
-   Ne pas compter deux fois un ticket en cours ou déjà affecté.
-   Respecter exactement les règles de priorité du Queue Engine.
-   Tenir compte des affectations réelles lorsqu'elles existent.
-   Si une affectation n'existe pas encore, utiliser une méthode de
    répartition compatible avec les règles métier et les capacités
    disponibles.
-   Retourner une estimation cohérente, non négative, accompagnée d'un
    horodatage de calcul et, si le système le permet, d'un indicateur de
    fiabilité ou de niveau de confiance.

Le calcul doit être conçu pour les files multi-guichets. Si les données
disponibles ne permettent pas une estimation suffisamment précise,
utiliser une stratégie de repli explicite et traçable plutôt que
produire une précision artificielle.

## 3.4. Actualisation événementielle et temps réel

Les statistiques de traitement et les estimations individuelles doivent
être actualisées à partir des événements métier réels.

Déclencheurs obligatoires :

1.  Appel d'un nouveau ticket.
2.  Fin effective du traitement d'un ticket.
3.  Activation ou désactivation d'un guichet.
4.  Transfert ou changement d'affectation d'un ticket.
5.  Modification de l'ordre de passage ou des priorités.

Vérifier également les événements de pause, de reprise et
d'indisponibilité des guichets. Ils doivent déclencher une actualisation
lorsqu'ils modifient la capacité réellement disponible.

Pour chaque événement pertinent :

-   Mettre à jour les projections nécessaires.
-   Recalculer les estimations affectées, sans recalcul global inutile.
-   Persister les observations et les résultats requis.
-   Mettre à jour les projections Redis.
-   Diffuser les nouvelles estimations aux clients concernés via les
    mécanismes WebSocket existants.

Les mises à jour doivent être idempotentes et protégées contre les
traitements concurrents. Utiliser les mécanismes transactionnels et
événementiels déjà présents dans le projet. Si une diffusion doit
survivre à un crash entre la transaction PostgreSQL et l'envoi
WebSocket, étudier un mécanisme de type transactional outbox plutôt que
supposer que les deux opérations sont atomiques.

## 3.5. Persistance et utilisation de Redis

### PostgreSQL

Persister les informations nécessaires à la fiabilité, à la
reproductibilité et à l'audit :

-   Configuration initiale par service.
-   Coefficient EWMA et paramètres associés.
-   Estimations agrégées par service et par guichet.
-   Observations réelles éligibles et non éligibles, avec leur statut.
-   Durées de traitement calculées.
-   Motifs d'exclusion des observations aberrantes.
-   Estimations individuelles émises, avec horodatage et contexte utile
    à l'évaluation.
-   Temps d'attente réel observé lorsque le ticket est effectivement
    appelé.
-   Indicateurs de précision calculés.

Adapter les tables et relations au modèle de données existant. Ne pas
créer de structures redondantes sans justification.

### Redis

Redis peut être utilisé pour :

-   Les projections opérationnelles fréquemment consultées.
-   Les estimations courantes des files.
-   La distribution rapide des mises à jour.
-   Les mécanismes de coordination déjà compatibles avec l'architecture
    existante.

Toutes les clés doivent être isolées par tenant et par périmètre métier.
Définir une stratégie claire d'expiration, d'invalidation et de
reconstruction depuis PostgreSQL.

Le système doit pouvoir reconstruire les projections après une perte ou
une indisponibilité de Redis.

## 3.6. Diffusion WebSocket

À chaque modification pertinente d'une estimation, diffuser le résultat
aux clients autorisés :

-   Application mobile.
-   Interface de guichet Tauri.
-   Tableau de bord administratif.

Le payload doit suivre les conventions déjà utilisées par TONDE et
contenir, selon les besoins réels des clients :

-   Identifiant du ticket.
-   Identifiant du service.
-   Identifiant du tenant lorsque nécessaire au routage interne.
-   Temps d'attente estimé.
-   Heure de calcul.
-   Éventuel niveau de fiabilité.
-   Version ou identifiant d'événement permettant de détecter les mises
    à jour obsolètes.

Ne pas diffuser de données personnelles ou d'informations sur d'autres
tickets qui ne sont pas nécessaires au client destinataire.

Les canaux et abonnements doivent être autorisés côté serveur et
strictement isolés par tenant. Documenter le comportement en cas de
reconnexion : le client doit pouvoir récupérer une estimation actuelle
depuis une source autorisée, sans dépendre exclusivement d'un événement
WebSocket manqué.

## 3.7. Fiabilité statistique et mesure de précision

Le moteur doit distinguer clairement :

-   La durée de traitement : début effectif du traitement jusqu'à sa
    fin.
-   Le temps d'attente : création ou entrée effective dans la file
    jusqu'au premier appel effectif, selon la définition métier retenue
    et documentée.

Pour évaluer une estimation du temps d'attente, comparer une prévision
enregistrée à l'instant où elle est émise ou actualisée avec le temps
d'attente réellement observé pour le même ticket.

Erreur absolue :

\[ AE_i = \|`\widehat{W_i}`{=tex} - W_i\| \]

Erreur absolue moyenne :

\[ MAE =
`\frac{1}{n}`{=tex}`\sum`{=tex}\_{i=1}\^{n}\|`\widehat{W_i}`{=tex}-W_i\|
\]

Où :

-   (`\widehat{W_i}`{=tex}) : temps d'attente estimé et enregistré pour
    le ticket (i).
-   (W_i) : temps d'attente réellement observé pour ce ticket.
-   \(n\) : nombre de prévisions évaluables.

Ne pas calculer la MAE en comparant une durée de traitement EWMA à un
temps d'attente réel : ce sont deux mesures différentes.

Prévoir une méthode permettant d'associer chaque prévision à son
résultat réel. Définir les règles de sélection des prévisions évaluées,
notamment lorsqu'un même ticket possède plusieurs estimations
successives.

Les métriques doivent pouvoir être ventilées par tenant, service,
guichet et période, dans le respect des autorisations.

## 3.8. Gestion des anomalies et observations aberrantes

Mettre en place un traitement explicite des observations anormales.

Cas à considérer :

-   Ticket laissé ouvert par oubli.
-   Pause ou interruption non correctement enregistrée.
-   Horodatages incohérents.
-   Durée négative ou nulle lorsqu'elle est impossible selon le cycle
    métier.
-   Traitement exceptionnellement long.
-   Événement de clôture reçu plusieurs fois.
-   Ticket transféré ou réaffecté pendant son traitement.

Le filtrage ne doit pas supprimer silencieusement les données
historiques. Enregistrer l'observation, son statut d'éligibilité et, si
elle est exclue, le motif correspondant.

Les seuils et règles doivent être configurables ou fondés sur une
méthode statistique documentée. Ne pas imposer un plafond arbitraire non
justifié.

## 4. API et intégration backend

Après audit, compléter les endpoints existants ou en créer uniquement si
nécessaire.

Le backend doit permettre, selon les conventions du projet :

-   La configuration de la durée initiale et des paramètres EWMA par
    service.
-   La consultation des paramètres et statistiques autorisés.
-   La récupération de l'estimation courante d'un ticket.
-   La consultation des métriques de précision pour les rôles autorisés.

Pour chaque endpoint :

-   Valider les entrées avec les schémas du projet.
-   Appliquer l'authentification et les permissions.
-   Déduire ou vérifier le tenant depuis le contexte d'authentification,
    et non depuis une valeur client non fiable.
-   Contrôler l'appartenance des services, guichets et tickets au tenant
    courant.
-   Utiliser les transactions et conventions de gestion d'erreurs
    existantes.
-   Documenter les réponses et codes d'erreur.
-   Éviter les requêtes N+1 et les recalculs coûteux non nécessaires.

Ne pas créer de nouveaux endpoints si les endpoints existants
remplissent déjà correctement ces responsabilités.

## 5. Exigences de sécurité, cohérence et performance

-   Garantir l'isolation multi-tenant dans toutes les requêtes SQL,
    opérations Redis et diffusions WebSocket.
-   Prévenir les accès directs à des tickets ou estimations appartenant
    à un autre tenant.
-   Rendre les mises à jour EWMA idempotentes.
-   Protéger les calculs contre les événements concurrents et les
    doubles traitements.
-   Préserver l'intégrité des transitions du Queue Engine.
-   Ne pas bloquer les opérations critiques du Queue Engine avec des
    calculs statistiques lourds.
-   Éviter les recalculs complets lorsque seule une partie de la file
    est affectée.
-   Prévoir la reconstruction des caches et projections après incident.
-   Ajouter des logs structurés et des métriques techniques sans exposer
    de données personnelles.
-   Ne jamais considérer Redis ou WebSocket comme la seule preuve qu'une
    opération métier a réussi.

------------------------------------------------------------------------

## 6. Tests obligatoires

Les tests doivent utiliser une base de test et des fixtures contrôlées.
Les mocks peuvent être employés dans les tests unitaires lorsqu'ils sont
pertinents, mais aucune simulation ne doit remplacer le fonctionnement
réel dans le code de production. Les tests d'intégration doivent
vérifier les interactions réelles avec PostgreSQL, Redis et les
composants concernés.

### 6.1. Tests unitaires

-   Initialisation EWMA depuis la durée configurée.
-   Convergence EWMA après plusieurs observations.
-   Respect du coefficient de lissage configuré.
-   Exclusion des tickets non éligibles.
-   Filtrage et traçabilité des observations aberrantes.
-   Calcul des durées de traitement.
-   Calcul des erreurs absolues et de la MAE.
-   Calcul d'estimation avec plusieurs guichets de capacités
    différentes.
-   Gestion des priorités et des tickets en cours.

### 6.2. Tests d'intégration

-   Configuration et persistance PostgreSQL.
-   Lecture des paramètres par tenant et service.
-   Mise à jour EWMA après clôture effective d'un ticket.
-   Absence de double mise à jour lors du rejeu d'un événement.
-   Repli du niveau guichet vers le niveau service lorsque l'historique
    est insuffisant.
-   Prise en compte d'un guichet actif, en pause, désactivé ou
    indisponible.
-   Recalcul après chacun des événements métier listés dans cette
    spécification.
-   Mise à jour et reconstruction des projections Redis.
-   Diffusion WebSocket vers les clients autorisés.
-   Récupération de l'état courant après reconnexion.
-   Isolation stricte entre deux tenants.

### 6.3. Tests de robustesse

-   Événements reçus en double.
-   Événements reçus dans un ordre inattendu.
-   Plusieurs clôtures concurrentes.
-   Indisponibilité temporaire de Redis.
-   Redémarrage du backend pendant une mise à jour.
-   Échec de diffusion WebSocket après persistance.
-   Données historiques insuffisantes.
-   Absence de guichet disponible.
-   Changements de priorité pendant l'attente.
-   Transfert d'un ticket entre guichets.

### 6.4. Tests de non-régression

Vérifier que la finalisation AWTE ne casse pas :

-   La création et l'émission des tickets.
-   La règle d'un seul ticket actif global par utilisateur.
-   L'appel et la clôture des tickets.
-   Les transferts et changements de priorité.
-   La mise en pause et la reprise des guichets.
-   Les abonnements WebSocket existants.
-   Les autres fonctionnalités du Queue Engine.

------------------------------------------------------------------------

## 7. Critères d'acceptation --- Definition of Done

La tâche ne peut être considérée comme terminée que lorsque tous les
critères applicables sont satisfaits.

-   [ ] **Audit réalisé :** rapport des composants existants, lacunes,
    fichiers concernés et risques de régression.
-   [ ] **Configuration persistante :** durée initiale, coefficient
    EWMA, seuils et politiques nécessaires enregistrés dans PostgreSQL.
-   [ ] **Initialisation correcte :** (AWT_0 =
    T\_{`\text{configured}`{=tex}}) lorsqu'aucun historique exploitable
    n'est disponible.
-   [ ] **EWMA fonctionnel :** mise à jour à partir des durées réelles
    de tickets éligibles.
-   [ ] **Idempotence :** un ticket terminé ne met à jour les
    statistiques qu'une seule fois.
-   [ ] **Niveaux d'estimation :** gestion correcte des niveaux guichet,
    service et organisation, avec seuils de repli persistés.
-   [ ] **Estimation individuelle :** calcul tenant compte de la file
    réelle, des priorités, des traitements en cours et des capacités
    disponibles.
-   [ ] **État des guichets :** les pauses, désactivations et
    indisponibilités sont correctement prises en compte.
-   [ ] **Gestion des anomalies :** observations aberrantes détectées,
    tracées et traitées selon une politique documentée.
-   [ ] **Déclencheurs événementiels :** recalcul après chacun des
    événements opérationnels pertinents.
-   [ ] **Redis :** projections actualisées, isolées par tenant et
    reconstructibles depuis PostgreSQL.
-   [ ] **WebSockets :** diffusion des estimations actualisées aux
    clients autorisés.
-   [ ] **MAE :** calcul fondé sur les prévisions enregistrées et les
    temps d'attente réellement observés, sans confusion avec les durées
    de traitement.
-   [ ] **Sécurité multi-tenant :** aucun accès ou flux de données entre
    tenants.
-   [ ] **Tests automatisés :** tests unitaires, intégration, robustesse
    et non-régression exécutés avec succès.
-   [ ] **Migrations :** migrations de base de données versionnées et
    vérifiées.
-   [ ] **Observabilité :** logs et métriques utiles au diagnostic et au
    suivi de la précision.
-   [ ] **Documentation :** configuration, endpoints, événements, règles
    de calcul et limites documentés.
-   [ ] **Rapport final :** fichiers créés/modifiés, choix techniques,
    migrations, tests exécutés, résultats et éventuels points restant à
    traiter.

Aucun critère ne doit être déclaré satisfait sur la seule base d'une
compilation réussie ou d'une démonstration manuelle.

------------------------------------------------------------------------

## 8. Livrables attendus du développeur

1.  Rapport d'audit de l'implémentation existante.
2.  Implémentation backend AWTE finalisée et intégrée au Queue Engine.
3.  Migrations PostgreSQL nécessaires.
4.  Gestion des projections Redis.
5.  Intégration des événements et diffusions WebSocket.
6.  Tests automatisés couvrant les scénarios spécifiés.
7.  Documentation technique et opérationnelle.
8.  Rapport final de livraison avec preuves d'exécution des tests.

## 9. Ordre d'exécution demandé

Respecter cet ordre afin de limiter les régressions :

1.  Auditer l'existant et présenter les constats.
2.  Identifier les composants réutilisables et proposer les
    modifications nécessaires.
3.  Vérifier les règles métier et les horodatages réellement
    disponibles.
4.  Implémenter les modèles et migrations manquants.
5.  Finaliser le calcul EWMA et les niveaux de repli.
6.  Finaliser le calcul individuel multi-guichets.
7.  Brancher les événements métier et les mises à jour temps réel.
8.  Finaliser la persistance, Redis et les WebSockets.
9.  Ajouter et exécuter les tests.
10. Effectuer la revue de sécurité, la non-régression et documenter la
    livraison.

**Ne pas commencer par réécrire le moteur.** L'objectif est de compléter
proprement l'implémentation existante, en conservant les composants
fiables et en corrigeant les lacunes identifiées.

## 10. Résultat final attendu

À la livraison, AWTE doit être un composant backend réellement
opérationnel de TONDE.

Pour chaque ticket concerné, le système doit pouvoir fournir une
estimation individuelle cohérente avec l'état réel de la file, apprendre
progressivement des durées de traitement observées, réagir aux
changements opérationnels et diffuser les nouvelles estimations en temps
réel.

Les données doivent être persistantes, les calculs traçables, les accès
isolés par tenant et le comportement couvert par des tests automatisés.

**La fonctionnalité doit être prête pour une mise en production
contrôlée.** Toute limite non résolue, dépendance manquante ou condition
empêchant de garantir ce niveau de qualité doit être signalée
explicitement avant la clôture de la tâche.
