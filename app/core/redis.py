"""
Client Redis — cache, OTP, file d'attente temps réel.

Conventions de nommage des clés :
  tonde:{org_id}:{agency_id}:{service_id}:queue  → Sorted Set (file d'attente)
  tonde:otp:{phone}                               → Hash SHA-256 de l'OTP (jamais en clair)
  tonde:otp_attempts:{phone}                      → Compteur tentatives OTP
  tonde:cache:{key}                               → Cache général

Le préfixe 'tonde:' isole les clés TONDE des autres apps
sur le même Redis. L'org_id garantit l'isolation multi-tenant.
"""
import hashlib
import json
import logging
from typing import Any, Optional
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


# ── Helpers File d'attente ────────────────────────────────────────────────────
def _queue_key(org_id: str, agency_id: str, service_id: str) -> str:
    """
    Génère la clé Redis de la file d'attente, segmentée par service.
    Format : tonde:{org_id}:{agency_id}:{service_id}:queue

    Chaque service a sa propre file Redis indépendante dans la même agence.
    Cela permet à Caisse, Crédit, Conseiller d'avoir des files distinctes.
    """
    return f"tonde:{org_id}:{agency_id}:{service_id}:queue"


async def next_daily_sequence(
    agency_id: str,
    prefix: str,
    day: str,
    db_max: int,
    ttl_seconds: int = 2 * 24 * 3600,
) -> int:
    """
    Retourne le prochain numéro de séquence du jour, de façon ATOMIQUE.

    Pourquoi Redis et pas un COUNT en base : deux guichetiers qui créent un
    ticket au même instant lisaient le même COUNT et recevaient tous les deux
    « A-5 ». Redis INCR est atomique et sérialise les appels concurrents.

    Le compteur est amorcé avec le MAX(sequence) déjà présent en base (et non
    un COUNT : des tickets peuvent avoir été supprimés). `SET NX` garantit
    qu'une seule instance pose l'amorce, même en cas de démarrage simultané
    de plusieurs workers.

    Args:
        agency_id: Agence concernée (chaque agence a sa numérotation)
        prefix: Préfixe du service (A, B, C…)
        day: Jour au format ISO (YYYY-MM-DD) — la séquence repart à 1 chaque jour
        db_max: Plus grand numéro de séquence déjà attribué aujourd'hui en base
        ttl_seconds: Durée de vie de la clé (2 jours : couvre le passage de minuit)

    Returns:
        Le numéro de séquence à utiliser (db_max + 1 au premier appel du jour)
    """
    r = await get_redis()
    key = f"tonde:seq:{agency_id}:{prefix}:{day}"

    # Amorce uniquement si la clé n'existe pas encore
    await r.set(key, db_max, nx=True, ex=ttl_seconds)
    return int(await r.incr(key))


async def add_to_queue(
    org_id: str, agency_id: str, service_id: str, ticket_id: str, priority: int = 0
) -> int:
    """
    Ajoute un ticket dans la file d'attente Redis (Sorted Set).

    Le score garantit l'ordre de service :
      - Priorité haute (emergency=9) → score bas → servi en premier
      - FIFO dans la même priorité via le timestamp

    Args:
        org_id: Isolation multi-tenant
        agency_id: Identifiant de l'agence
        service_id: Identifiant du service (Caisse, Crédit, etc.)
        ticket_id: UUID du ticket
        priority: Score de priorité (0=standard, 3=priority, 5=vip, 9=emergency)

    Returns:
        Position dans la file (commence à 1)
    """
    import time
    r = await get_redis()
    key = _queue_key(org_id, agency_id, service_id)
    score = (10 - priority) * 1_000_000_000 + int(time.time() * 1000)
    await r.zadd(key, {ticket_id: score})
    position = await r.zrank(key, ticket_id)
    return (position or 0) + 1


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
