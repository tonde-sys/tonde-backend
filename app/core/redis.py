"""
Client Redis — cache, OTP, file d'attente temps réel.

Conventions de nommage des clés :
  tonde:{org_id}:{agency_id}:{service_id}:queue  → Sorted Set (file d'attente)
  tonde:otp:{phone}                               → Hash SHA-256 de l'OTP (jamais en clair)
  tonde:otp_attempts:{phone}                      → Compteur tentatives OTP
  tonde:cache:{key}                               → Cache général
  tonde:seq:{agency_id}:{prefix}:{AAAAMMJJ}       → Compteur atomique de numéros de ticket (INCR)
  tonde:lock:ticket_create:{user_id}              → Verrou court anti double création (SET NX)
  tonde:deadlines:{absent|warning}                → ZSET des échéances (score = timestamp limite)
  tonde:once:{name}                               → Marqueur « déjà fait » (SET NX + TTL)
  tonde:login_fail:{email}                        → Compteur d'échecs de connexion (verrouillage par compte)
  tonde:login_strikes:{email}                     → Paliers de blocage déjà atteints
  tonde:login_blocked:{email}                     → Drapeau de blocage temporaire du compte

Le préfixe 'tonde:' isole les clés TONDE des autres apps
sur le même Redis. L'org_id garantit l'isolation multi-tenant.
"""
import hashlib
import json
import logging
from typing import Any, Awaitable, Callable, Optional
import redis.asyncio as aioredis
from app.core.config import settings

logger = logging.getLogger(__name__)

# ── Connexion Redis ───────────────────────────────────────────────────────────
redis_client: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    """Retourne le client Redis. Crée la connexion si elle n'existe pas."""
    global redis_client
    if redis_client is None:
        redis_client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
    return redis_client


async def close_redis() -> None:
    """Ferme la connexion Redis proprement à l'arrêt de l'app."""
    global redis_client
    if redis_client:
        await redis_client.aclose()
        redis_client = None
        logger.info("Redis — connexion fermée")


# ── Helpers OTP ───────────────────────────────────────────────────────────────
def _hash_otp(otp: str) -> str:
    """
    Calcule le hash SHA-256 d'un OTP.

    L'OTP ne doit jamais être stocké en clair dans Redis.
    Cette fonction est l'unique point de hashage — toute persistance OTP passe par ici.

    Args:
        otp: Code OTP en clair (ex: "123456")

    Returns:
        Chaîne hexadécimale de 64 caractères (SHA-256)
    """
    return hashlib.sha256(otp.encode()).hexdigest()


async def save_otp(phone: str, otp: str) -> None:
    """
    Sauvegarde le HASH SHA-256 de l'OTP dans Redis avec TTL automatique.
    Réinitialise aussi le compteur de tentatives.

    L'OTP en clair n'est jamais persisté. Si Redis est compromis,
    les hash stockés ne permettent pas de reconstituer les codes originaux.

    Args:
        phone: Numéro de téléphone normalisé
        otp: Code OTP en clair (hashé avant persistance)
    """
    r = await get_redis()
    expire_seconds = settings.OTP_EXPIRE_MINUTES * 60
    await r.setex(f"tonde:otp:{phone}", expire_seconds, _hash_otp(otp))
    await r.setex(f"tonde:otp_attempts:{phone}", expire_seconds, "0")


async def get_otp(phone: str) -> Optional[str]:
    """
    Récupère le hash SHA-256 de l'OTP stocké pour un numéro.

    Returns:
        Hash SHA-256 (64 caractères hexadécimaux) ou None si expiré/absent.
        La valeur retournée est un hash, jamais le code OTP en clair.
    """
    r = await get_redis()
    return await r.get(f"tonde:otp:{phone}")


async def increment_otp_attempts(phone: str) -> int:
    """Incrémente le compteur de tentatives OTP. Retourne le nouveau compteur."""
    r = await get_redis()
    return await r.incr(f"tonde:otp_attempts:{phone}")


async def delete_otp(phone: str) -> None:
    """Supprime l'OTP après vérification réussie."""
    r = await get_redis()
    await r.delete(f"tonde:otp:{phone}", f"tonde:otp_attempts:{phone}")


# ── Helpers Rate Limiting / Blocage ──────────────────────────────────────────
PHONE_BLOCK_SECONDS = 15 * 60       # 15 minutes de blocage après trop d'échecs
PHONE_REGISTER_WINDOW = 60          # Fenêtre 1 minute pour register/phone
PHONE_REGISTER_MAX = 3              # Max 3 demandes de SMS par numéro / minute


async def is_phone_blocked(phone: str) -> tuple[bool, int]:
    """
    Vérifie si un numéro est en cooldown suite à trop d'échecs OTP.

    Returns:
        (True, seconds_remaining) si bloqué
        (False, 0) si libre
    """
    r = await get_redis()
    ttl = await r.ttl(f"tonde:blocked:{phone}")
    if ttl > 0:
        return True, ttl
    return False, 0


async def block_phone(phone: str) -> None:
    """
    Bloque un numéro pour PHONE_BLOCK_SECONDS secondes.
    Appelé après OTP_MAX_ATTEMPTS échecs consécutifs.
    """
    r = await get_redis()
    await r.setex(f"tonde:blocked:{phone}", PHONE_BLOCK_SECONDS, "1")
    logger.warning(f"[SECURITY] Numéro bloqué 15 min suite à trop d'échecs OTP: {phone}")


async def increment_register_attempts(phone: str) -> int:
    """
    Compteur d'envois de SMS pour un numéro sur la fenêtre glissante.
    Protège contre le spam de SMS (coût Africa's Talking).

    Returns:
        Nombre de tentatives dans la fenêtre courante
    """
    r = await get_redis()
    key = f"tonde:sms_attempts:{phone}"
    count = await r.incr(key)
    if count == 1:
        # Première tentative → démarrer le TTL
        await r.expire(key, PHONE_REGISTER_WINDOW)
    return count


async def get_register_attempts(phone: str) -> int:
    """Retourne le nombre d'envois SMS dans la fenêtre courante."""
    r = await get_redis()
    val = await r.get(f"tonde:sms_attempts:{phone}")
    return int(val) if val else 0


# ── Verrouillage par compte (connexion email) ────────────────────────────────
#
# Le rate limit du routeur (`10/minute`) est indexé sur l'IP : un attaquant qui
# dispose de 100 IP obtient 1 000 tentatives par minute sur le même compte.
# Ces helpers ajoutent la dimension qui manque : le COMPTE.
#
# Clés :
#   tonde:login_fail:{email}     → compteur d'échecs consécutifs (TTL 15 min)
#   tonde:login_strikes:{email}  → nombre de paliers déjà atteints (TTL 24 h)
#   tonde:login_blocked:{email}  → drapeau de blocage (TTL = durée du palier)
#
LOGIN_MAX_ATTEMPTS = 5                  # Échecs avant le premier blocage
LOGIN_FAILURE_WINDOW = 15 * 60          # Les échecs se cumulent sur 15 minutes
LOGIN_STRIKE_TTL = 24 * 60 * 60         # Les paliers se cumulent sur 24 heures
LOGIN_BACKOFF_LADDER = (60, 300, 900)   # 1 min, puis 5 min, puis 15 min


def _login_email(email: str) -> str:
    """Normalise l'email pour qu'une clé Redis ne dépende pas de la casse."""
    return email.strip().lower()


def _login_fail_key(email: str) -> str:
    return f"tonde:login_fail:{_login_email(email)}"


def _login_block_key(email: str) -> str:
    return f"tonde:login_blocked:{_login_email(email)}"


def _login_strikes_key(email: str) -> str:
    return f"tonde:login_strikes:{_login_email(email)}"


async def get_login_block(email: str) -> int:
    """
    Secondes de blocage restantes pour ce compte.

    Returns:
        > 0 si le compte est bloqué, 0 sinon.
    """
    r = await get_redis()
    ttl = await r.ttl(_login_block_key(email))
    return ttl if ttl > 0 else 0


async def register_login_failure(email: str) -> tuple[int, int]:
    """
    Enregistre un échec de connexion et bloque le compte si le seuil est atteint.

    Paliers progressifs : 5 échecs → 1 min, puis 5 de plus → 5 min, puis 15 min.
    Le palier redescend tout seul après LOGIN_STRIKE_TTL sans nouvel échec.

    Returns:
        (échecs_dans_la_fenêtre, secondes_de_blocage) — le second vaut 0
        tant que le seuil n'est pas atteint.
    """
    r = await get_redis()
    key = _login_fail_key(email)
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, LOGIN_FAILURE_WINDOW)

    if count < LOGIN_MAX_ATTEMPTS:
        return count, 0

    strikes_key = _login_strikes_key(email)
    strikes = await r.incr(strikes_key)
    if strikes == 1:
        await r.expire(strikes_key, LOGIN_STRIKE_TTL)

    delay = LOGIN_BACKOFF_LADDER[min(strikes - 1, len(LOGIN_BACKOFF_LADDER) - 1)]
    await r.setex(_login_block_key(email), delay, "1")
    # Le compteur repart de zéro : le blocage lui-même fait office de sanction.
    await r.delete(key)
    logger.warning(
        f"[SECURITY] Compte bloqué {delay}s après {count} échecs de connexion : {_login_email(email)}"
    )
    return count, delay


async def clear_login_failures(email: str) -> None:
    """
    Remet le compte à zéro après une connexion réussie.

    Supprime aussi le compteur de paliers : un utilisateur légitime qui se
    trompe cinq fois puis réussit ne doit pas conserver de pénalité.
    """
    r = await get_redis()
    await r.delete(
        _login_fail_key(email),
        _login_block_key(email),
        _login_strikes_key(email),
    )


# ── Helpers File d'attente ────────────────────────────────────────────────────
def _queue_key(org_id: str, agency_id: str, service_id: str) -> str:
    """
    Génère la clé Redis de la file d'attente, segmentée par service.
    Format : tonde:{org_id}:{agency_id}:{service_id}:queue

    Chaque service a sa propre file Redis indépendante dans la même agence.
    Cela permet à Caisse, Crédit, Conseiller d'avoir des files distinctes.
    """
    return f"tonde:{org_id}:{agency_id}:{service_id}:queue"


# ── Géométrie du score de file ────────────────────────────────────────────────
# Le score est un entier : (10 - priorité) × MARGE + created_ms.
# La MARGE est exprimée en MILLISECONDES (10⁹ ms = 11,57 jours), et non dans une
# unité abstraite : c'est ce qui rend le facteur trompeur à la lecture.
#
# Invariant garanti par cette marge :
#   un ticket de priorité p2 > p1 créé APRÈS un ticket de priorité p1 passe quand
#   même devant lui, À CONDITION que l'écart de création reste sous la marge.
#   Démonstration : score1 - score2 = (p2 - p1) × MARGE - (t2 - t1) > 0
#                  dès que (t2 - t1) < (p2 - p1) × MARGE.
#
# Conséquence pratique : la marge la plus faible est celle entre deux niveaux
# consécutifs, soit UNE marge = 11,57 jours. Un ticket ne peut donc être doublé
# par une priorité supérieure que s'il attend plus de 11,57 jours. Comme
# WAITING_TICKET_TTL_HOURS (72 h = 3 jours) expire bien avant, l'invariant tient
# par construction — voir tests/test_queue_score_geometry.py, qui échoue si
# quelqu'un relève le TTL au-delà de la marge sans toucher à cette constante.
#
# Ne pas réduire cette constante : 10⁹ « ressemble » à des millisecondes mais
# couvre 11,57 jours d'ancienneté. Diviser par 1000 (1,157 jour) casserait la
# priorité dès qu'un ticket attend une journée.
QUEUE_PRIORITY_BAND_MS = 1_000_000_000
QUEUE_PRIORITY_BAND_DAYS = QUEUE_PRIORITY_BAND_MS / 86_400_000  # 11,574 jours


def queue_score(priority: int, created_ms: Optional[int] = None) -> int:
    """
    Calcule le score Redis d'un ticket : priorité d'abord, puis FIFO.

    Plus le score est bas, plus le ticket passe tôt. Le timestamp en ms départage
    les tickets de même priorité. Passer `created_ms` (date de création réelle du
    ticket) permet de reconstruire la file ou de changer une priorité SANS faire
    perdre au client son ancienneté.

    Le score reste un entier exact en JSON/Redis (double 53 bits) : la valeur
    maximale est 10 × 10⁹ + 1,7 × 10¹² ≈ 1,7 × 10¹², très loin de 9 × 10¹⁵.

    Args:
        priority: Score de priorité (0=standard, 3=priority, 5=vip, 9=emergency)
        created_ms: Horodatage de référence en ms (défaut : maintenant)
    """
    import time
    if created_ms is None:
        created_ms = int(time.time() * 1000)
    return (10 - priority) * QUEUE_PRIORITY_BAND_MS + created_ms


async def add_to_queue(
    org_id: str,
    agency_id: str,
    service_id: str,
    ticket_id: str,
    priority: int = 0,
    created_ms: Optional[int] = None,
) -> int:
    """
    Ajoute (ou repositionne) un ticket dans la file d'attente Redis (Sorted Set).

    Le score garantit l'ordre de service :
      - Priorité haute (emergency=9) → score bas → servi en premier
      - FIFO dans la même priorité via le timestamp

    Args:
        org_id: Isolation multi-tenant
        agency_id: Identifiant de l'agence
        service_id: Identifiant du service (Caisse, Crédit, etc.)
        ticket_id: UUID du ticket
        priority: Score de priorité (0=standard, 3=priority, 5=vip, 9=emergency)
        created_ms: Horodatage de référence (défaut : maintenant)

    Returns:
        Position dans la file (commence à 1)
    """
    r = await get_redis()
    key = _queue_key(org_id, agency_id, service_id)
    await r.zadd(key, {ticket_id: queue_score(priority, created_ms)})
    position = await r.zrank(key, ticket_id)
    return (position or 0) + 1


async def add_to_queue_if_absent(
    org_id: str,
    agency_id: str,
    service_id: str,
    ticket_id: str,
    score: float,
) -> bool:
    """
    Ajoute un ticket seulement s'il n'est pas déjà dans la file (ZADD NX).

    Utilisé pour reconstruire la file depuis PostgreSQL sans jamais écraser un
    score existant. Retourne True si le ticket a été ajouté.
    """
    r = await get_redis()
    added = await r.zadd(_queue_key(org_id, agency_id, service_id), {ticket_id: score}, nx=True)
    return bool(added)


async def get_queue_position(org_id: str, agency_id: str, service_id: str, ticket_id: str) -> int:
    """Retourne la position du ticket dans la file (commence à 1, 0 si absent)."""
    r = await get_redis()
    position = await r.zrank(_queue_key(org_id, agency_id, service_id), ticket_id)
    if position is None:
        return 0
    return position + 1


async def get_queue_size(org_id: str, agency_id: str, service_id: str) -> int:
    """Retourne le nombre de tickets en attente dans la file du service."""
    r = await get_redis()
    return await r.zcard(_queue_key(org_id, agency_id, service_id))


async def remove_from_queue(org_id: str, agency_id: str, service_id: str, ticket_id: str) -> None:
    """Retire un ticket de la file du service (appelé, annulé ou expiré)."""
    r = await get_redis()
    await r.zrem(_queue_key(org_id, agency_id, service_id), ticket_id)


async def get_next_ticket(org_id: str, agency_id: str, service_id: str) -> Optional[str]:
    """
    Retourne l'ID du prochain ticket à servir dans la file du service.
    Ne le retire pas de la file — c'est remove_from_queue() qui le fait.
    """
    r = await get_redis()
    results = await r.zrange(_queue_key(org_id, agency_id, service_id), 0, 0)
    return results[0] if results else None


async def pop_next_ticket(
    org_id: str, agency_id: str, service_id: str
) -> Optional[tuple[str, float]]:
    """
    Retire ET retourne atomiquement le prochain ticket de la file (ZPOPMIN).

    Contrairement à get_next_ticket() + remove_from_queue() (deux commandes,
    donc une fenêtre où deux guichets lisent le même ticket), ZPOPMIN est une
    seule commande Redis : un ticket ne peut être servi qu'à un seul appelant.

    Returns:
        (ticket_id, score) ou None si la file est vide. Le score permet de
        remettre le ticket à sa place d'origine si la suite du traitement échoue.
    """
    r = await get_redis()
    popped = await r.zpopmin(_queue_key(org_id, agency_id, service_id), 1)
    if not popped:
        return None
    member, score = popped[0]
    return member, float(score)


async def restore_to_queue(
    org_id: str, agency_id: str, service_id: str, ticket_id: str, score: float
) -> None:
    """
    Remet un ticket dans la file avec son score d'origine (compensation).

    Utilisé quand pop_next_ticket() a retiré un ticket mais que son passage
    à CALLED a échoué en base : le ticket retrouve sa place exacte au lieu
    de disparaître de la file.
    """
    r = await get_redis()
    await r.zadd(_queue_key(org_id, agency_id, service_id), {ticket_id: score})


# ── Échéances (auto-absent, avertissements) ───────────────────────────────────
# Deux ZSET : le score est l'heure limite (timestamp UNIX), le membre est
# "{org_id}:{ticket_id}". Plusieurs instances de l'API peuvent interroger la même
# échéance : ZREM sert de verrou atomique — une seule instance « réclame » chaque échéance.
DEADLINE_ABSENT = "absent"
DEADLINE_WARNING = "warning"


def _deadline_key(kind: str) -> str:
    return f"tonde:deadlines:{kind}"


async def set_deadline(kind: str, org_id: str, ticket_id: str, when_ts: float) -> None:
    """Programme une échéance (ex. passage en ABSENT) pour un ticket."""
    r = await get_redis()
    await r.zadd(_deadline_key(kind), {f"{org_id}:{ticket_id}": when_ts})


async def clear_deadlines(org_id: str, ticket_id: str) -> None:
    """Annule toutes les échéances d'un ticket (présenté, terminé, transféré, annulé…)."""
    r = await get_redis()
    member = f"{org_id}:{ticket_id}"
    await r.zrem(_deadline_key(DEADLINE_ABSENT), member)
    await r.zrem(_deadline_key(DEADLINE_WARNING), member)


async def claim_due_deadlines(kind: str, now_ts: float, limit: int = 100) -> list[tuple[str, str]]:
    """
    Réclame atomiquement les échéances arrivées à terme.

    Pour chaque échéance due, ZREM retourne 1 pour UNE seule instance : celle-ci
    gagne le droit de traiter le ticket, les autres l'ignorent.

    Returns:
        Liste de (org_id, ticket_id) réclamés par CET appelant.
    """
    r = await get_redis()
    key = _deadline_key(kind)
    due = await r.zrangebyscore(key, "-inf", now_ts, start=0, num=limit)
    claimed: list[tuple[str, str]] = []
    for member in due:
        if await r.zrem(key, member) == 1:
            org_id, _, ticket_id = member.partition(":")
            claimed.append((org_id, ticket_id))
    return claimed


async def mark_once(name: str, ttl_seconds: int) -> bool:
    """
    Pose un marqueur « déjà fait » (SET NX). Retourne True la PREMIÈRE fois seulement.

    Sert à ne pas renvoyer deux fois la même alerte (ex. SMS d'attente longue).
    """
    r = await get_redis()
    return bool(await r.set(f"tonde:once:{name}", "1", nx=True, ex=ttl_seconds))


async def count_all_queued_tickets(max_keys: int = 5000) -> int:
    """
    Nombre total de tickets en file, toutes organisations confondues (monitoring).

    Parcourt les clés de file avec SCAN (non bloquant), bornée à `max_keys`.
    """
    r = await get_redis()
    total = 0
    scanned = 0
    async for key in r.scan_iter(match="tonde:*:queue", count=500):
        total += await r.zcard(key)
        scanned += 1
        if scanned >= max_keys:
            break
    return total


# ── Numérotation atomique des tickets ─────────────────────────────────────────
SEQUENCE_TTL_SECONDS = 48 * 3600  # La clé du jour expire après 48 h


async def next_ticket_sequence(
    agency_id: str,
    prefix: str,
    day: str,
    load_seed: Callable[[], Awaitable[int]],
) -> int:
    """
    Retourne le prochain numéro de séquence du jour, de façon atomique (INCR).

    Un COUNT en base suivi d'un +1 donne le même numéro à deux créations
    simultanées. INCR est atomique : chaque appelant obtient un numéro unique.

    Si la clé du jour n'existe pas (premier ticket, ou Redis redémarré), elle est
    initialisée depuis la base via `load_seed` (plus grande séquence du jour).
    SET NX garantit qu'une initialisation concurrente ne peut pas écraser un
    compteur déjà en cours.

    Args:
        agency_id: Branch concernée
        prefix: Préfixe du service (A, B, C…)
        day: Jour local au format AAAAMMJJ (la séquence repart à 1 chaque jour)
        load_seed: Coroutine retournant la plus grande séquence déjà en base

    Returns:
        Numéro de séquence (commence à 1)
    """
    r = await get_redis()
    key = f"tonde:seq:{agency_id}:{prefix}:{day}"
    if not await r.exists(key):
        seed = await load_seed()
        await r.set(key, seed, nx=True, ex=SEQUENCE_TTL_SECONDS)
    return int(await r.incr(key))


# ── Verrou anti double création de ticket ─────────────────────────────────────
TICKET_CREATION_LOCK_TTL = 10  # secondes — filet de sécurité si le process meurt


async def acquire_ticket_creation_lock(user_id: str) -> bool:
    """
    Prend un verrou court (SET NX) pour la création de ticket d'un utilisateur.

    Protège la règle « 1 seul ticket actif » : sans verrou, deux requêtes
    simultanées (double tap, retry réseau) passent toutes les deux le contrôle
    « existe-t-il un ticket actif ? » avant qu'aucune n'ait créé le sien.

    Returns:
        True si le verrou est acquis, False si une création est déjà en cours.
    """
    r = await get_redis()
    return bool(
        await r.set(
            f"tonde:lock:ticket_create:{user_id}", "1", nx=True, ex=TICKET_CREATION_LOCK_TTL
        )
    )


async def release_ticket_creation_lock(user_id: str) -> None:
    """Libère le verrou de création de ticket de l'utilisateur."""
    r = await get_redis()
    await r.delete(f"tonde:lock:ticket_create:{user_id}")


async def get_queue_snapshot(org_id: str, agency_id: str, service_id: str) -> list[str]:
    """
    Retourne tous les IDs de tickets dans la file du service, dans l'ordre de service.
    Utilisé pour les mises à jour WebSocket périodiques.
    """
    r = await get_redis()
    return await r.zrange(_queue_key(org_id, agency_id, service_id), 0, -1)


# ── Cache général ─────────────────────────────────────────────────────────────
async def cache_set(key: str, value: Any, expire_seconds: int = 300) -> None:
    """Sauvegarde une valeur sérialisée dans le cache pendant X secondes."""
    r = await get_redis()
    await r.setex(f"tonde:cache:{key}", expire_seconds, json.dumps(value))


async def cache_get(key: str) -> Optional[Any]:
    """Récupère une valeur du cache. Retourne None si absente ou expirée."""
    r = await get_redis()
    value = await r.get(f"tonde:cache:{key}")
    return json.loads(value) if value else None


async def cache_delete(key: str) -> None:
    """Supprime une valeur du cache."""
    r = await get_redis()
    await r.delete(f"tonde:cache:{key}")
