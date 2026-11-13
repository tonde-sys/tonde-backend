"""
Sécurité : JWT, hachage de mots de passe, OTP.
"""
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

# ── Hachage des mots de passe ─────────────────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Transforme un mot de passe en hash sécurisé. Ne jamais stocker le vrai mot de passe."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Vérifie qu'un mot de passe correspond à son hash."""
    return pwd_context.verify(plain_password, hashed_password)


# ── JWT — Tokens d'accès ──────────────────────────────────────────────────────
def create_access_token(user_id: str) -> str:
    """
    Crée un token JWT d'accès valide 15 minutes.
    Ce token est envoyé dans chaque requête API par le mobile.
    """
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {
        "sub": user_id,        # L'identifiant de l'utilisateur
        "type": "access",      # Type de token
        "exp": expire,         # Expiration
        "iat": datetime.now(timezone.utc),  # Date de création
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    """
    Crée un token de rafraîchissement valide 7 jours.
    Inclut un jti (JWT ID) unique pour garantir l'unicité du hash en DB.
    Permet de renouveler l'access token sans se reconnecter.
    """
    expire = datetime.now(timezone.utc) + timedelta(
        days=settings.REFRESH_TOKEN_EXPIRE_DAYS
    )
    payload = {
        "sub": user_id,
        "type": "refresh",
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "jti": str(uuid.uuid4()),  # ID unique — garantit un hash différent à chaque émission
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def verify_token(token: str, token_type: str = "access") -> Optional[str]:
    """
    Vérifie un token JWT et retourne l'ID utilisateur.
    Retourne None si le token est invalide ou expiré.
    """
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM]
        )
        if payload.get("type") != token_type:
            return None
        user_id: str = payload.get("sub")
        return user_id
    except JWTError:
        return None


# ── OTP — Code de vérification par SMS ───────────────────────────────────────
def generate_otp(length: int = 6) -> str:
    """
    Génère un code OTP à 6 chiffres avec un générateur cryptographiquement sûr.

    `secrets` (CSPRNG du système) et non `random` (Mersenne Twister, prévisible
    à partir de quelques sorties) : un code de vérification doit être imprévisible.
    Exemple : "483921"
    """
    return "".join(secrets.choice(string.digits) for _ in range(length))


# ── En dev : OTP fixe pour les tests ─────────────────────────────────────────
DEV_OTP = "123456"  # En développement, ce code marche toujours
