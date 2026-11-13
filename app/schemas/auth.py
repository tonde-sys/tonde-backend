"""
Schémas Pydantic pour l'authentification.
Ces schémas valident les données qui entrent et sortent de l'API.
"""
from pydantic import BaseModel, EmailStr, field_validator, ConfigDict
import re


# ── Inscription par téléphone ─────────────────────────────────────────────────
class RegisterPhoneRequest(BaseModel):
    phone: str
    country_code: str = "BI"  # Burundi par défaut

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v):
        # Nettoyer le numéro (enlever espaces, tirets)
        phone = re.sub(r"[\s\-\(\)]", "", v)
        # Vérifier le format
        if not re.match(r"^\+?[1-9]\d{7,14}$", phone):
            raise ValueError("Numéro de téléphone invalide")
        return phone


class RegisterPhoneResponse(BaseModel):
    success: bool = True
    message: str = "Code OTP envoyé par SMS"
    otp_sent: bool = True
    expires_in_seconds: int = 300  # 5 minutes


# ── Vérification OTP ──────────────────────────────────────────────────────────
class VerifyOtpRequest(BaseModel):
    phone: str
    otp: str
    device_id: str | None = None   # identifiant du device pour le multi-device

    @field_validator("otp")
    @classmethod
    def validate_otp(cls, v):
        if not re.match(r"^\d{6}$", v):
            raise ValueError("Le code OTP doit contenir 6 chiffres")
        return v


# ── Inscription par email ─────────────────────────────────────────────────────
class RegisterEmailRequest(BaseModel):
    email: EmailStr
    password: str
    name: str
    device_id: str | None = None   # identifiant du device pour le multi-device

    @field_validator("password")
    @classmethod
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError("Le mot de passe doit contenir au moins 8 caractères")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        if len(v.strip()) < 2:
            raise ValueError("Le nom doit contenir au moins 2 caractères")
        return v.strip()


# ── Connexion par email ───────────────────────────────────────────────────────
class LoginEmailRequest(BaseModel):
    email: EmailStr
    password: str
    device_id: str | None = None   # identifiant du device pour le multi-device


# ── Refresh Token ─────────────────────────────────────────────────────────────
class RefreshTokenRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    """Body pour POST /auth/logout — révocation d'une session."""
    refresh_token: str


# ── Réponse après connexion réussie ──────────────────────────────────────────
class UserInToken(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str | None
    phone: str | None
    email: str | None
    role: str
    language: str
    is_verified: bool


class AuthResponse(BaseModel):
    success: bool = True
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserInToken
    device_id: str | None = None   # device enregistré pour cette session


class RefreshResponse(BaseModel):
    """Réponse de POST /auth/refresh après rotation du token."""
    success: bool = True
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


# ── Profil utilisateur ────────────────────────────────────────────────────────
SUPPORTED_LANGUAGES = ("fr", "en", "rn", "sw")  # français, anglais, kirundi, swahili


class UpdateProfileRequest(BaseModel):
    """Mise à jour partielle du profil : seuls les champs fournis sont modifiés."""
    name: str | None = None
    language: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not 2 <= len(v) <= 100:
            raise ValueError("Le nom doit contenir entre 2 et 100 caractères")
        return v

    @field_validator("language")
    @classmethod
    def validate_language(cls, v: str | None) -> str | None:
        if v is not None and v not in SUPPORTED_LANGUAGES:
            raise ValueError(f"Langue non supportée. Valeurs acceptées : {', '.join(SUPPORTED_LANGUAGES)}")
        return v


class FcmTokenRequest(BaseModel):
    """Jeton de push de l'appareil. `null` = se désinscrire des notifications push."""
    fcm_token: str | None = None

    @field_validator("fcm_token")
    @classmethod
    def validate_token(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not 20 <= len(v) <= 500:
            raise ValueError("Jeton FCM invalide")
        return v


# ── Réponse erreur standard ───────────────────────────────────────────────────
class ErrorResponse(BaseModel):
    success: bool = False
    error: dict

    @classmethod
    def create(cls, code: str, message: str):
        return cls(error={"code": code, "message": message})
