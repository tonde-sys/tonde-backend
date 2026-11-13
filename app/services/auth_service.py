"""
Service d'authentification — toute la logique de connexion/inscription.

Flux principal :
  1. register_phone(phone) → envoie OTP SMS
  2. verify_otp(phone, otp) → retourne JWT

Flux secondaire :
  - register_email / login_email pour les agents et admins
  - refresh_token pour renouveler l'access token
"""
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from fastapi import HTTPException, status

from app.models.user import User
from app.models.refresh_token import RefreshToken
from app.core.security import (
    hash_password, verify_password,
    create_access_token, create_refresh_token,
    generate_otp, verify_token, DEV_OTP,
)
from app.core.redis import (
    save_otp, get_otp, delete_otp,
    increment_otp_attempts, _hash_otp,
    is_phone_blocked, block_phone,
    increment_register_attempts, PHONE_REGISTER_MAX, PHONE_BLOCK_SECONDS,
)
from app.core.config import settings
from app.services.sms_gateway import SmsDeliveryError, send_sms
from app.schemas.auth import (
    RegisterPhoneRequest, VerifyOtpRequest,
    RegisterEmailRequest, LoginEmailRequest,
    AuthResponse, UserInToken, RefreshResponse,
)

logger = logging.getLogger(__name__)


class AuthService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── Étape 1 : Inscription par téléphone ───────────────────────────────────
    async def register_phone(self, data: RegisterPhoneRequest) -> dict:
        """
        L'utilisateur entre son numéro → reçoit un OTP par SMS.

        Protections :
          - Max 3 demandes SMS par numéro par minute (anti-spam SMS)
          - Numéro bloqué si trop d'échecs OTP précédents

        En développement (ENVIRONMENT=development) :
          - L'OTP est toujours 123456
          - Aucun SMS n'est envoyé

        Returns:
            Dict avec success, message, expires_in_seconds
            En développement : inclut dev_otp pour les tests

        Raises:
            HTTPException 429: Trop de demandes SMS ou numéro bloqué
        """
        phone = data.phone

        # Couche 1 — Numéro bloqué suite à trop d'échecs OTP ?
        blocked, seconds_left = await is_phone_blocked(phone)
        if blocked:
            minutes_left = (seconds_left + 59) // 60
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "PHONE_BLOCKED",
                    "message": (
                        f"Ce numéro est temporairement bloqué suite à trop de tentatives incorrectes. "
                        f"Réessayez dans {minutes_left} minute(s)."
                    ),
                    "retry_after_seconds": seconds_left,
                },
            )

        # Couche 2 — Trop de demandes de SMS sur ce numéro ?
        sms_count = await increment_register_attempts(phone)
        if sms_count > PHONE_REGISTER_MAX:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "SMS_RATE_LIMIT",
                    "message": (
                        f"Trop de demandes de code pour ce numéro. "
                        f"Vous pouvez demander au maximum {PHONE_REGISTER_MAX} codes par minute. "
                        f"Attendez avant de réessayer."
                    ),
                    "retry_after_seconds": 60,
                },
            )

        # Générer l'OTP
        otp = DEV_OTP if settings.is_development else generate_otp()

        # Stocker dans Redis avec TTL (réinitialise aussi le compteur d'échecs)
        await save_otp(phone, otp)

        # Envoyer le SMS
        await self._send_otp_sms(phone, otp)

        response: dict = {
            "success": True,
            "message": "Code OTP envoyé par SMS",
            "otp_sent": True,
            "expires_in_seconds": settings.OTP_EXPIRE_MINUTES * 60,
        }

        # Exposer l'OTP UNIQUEMENT en développement local
        # Ne jamais exposer en staging ou production
        if settings.ENVIRONMENT == "development":
            response["dev_otp"] = otp
            logger.debug(f"[DEV] OTP pour {phone}: {otp}")

        return response

    # ── Étape 2 : Vérification OTP ────────────────────────────────────────────
    async def verify_otp(self, data: VerifyOtpRequest) -> AuthResponse:
        """
        Vérifie l'OTP et retourne les tokens JWT.
        Crée automatiquement le compte si c'est le premier login.

        Protections :
          - Numéro bloqué si déjà trop d'échecs → 429 immédiat
          - Max OTP_MAX_ATTEMPTS tentatives par code (défaut 5)
          - Blocage 15 min du numéro après épuisement des tentatives

        Args:
            data: phone + otp saisi par l'utilisateur

        Returns:
            AuthResponse avec access_token, refresh_token, user

        Raises:
            HTTPException 400: OTP expiré ou invalide
            HTTPException 429: Numéro bloqué ou trop de tentatives
        """
        phone = data.phone
        otp = data.otp

        # Couche 1 — Numéro déjà bloqué ?
        blocked, seconds_left = await is_phone_blocked(phone)
        if blocked:
            minutes_left = (seconds_left + 59) // 60
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "PHONE_BLOCKED",
                    "message": (
                        f"Ce numéro est temporairement bloqué suite à trop de tentatives incorrectes. "
                        f"Réessayez dans {minutes_left} minute(s)."
                    ),
                    "retry_after_seconds": seconds_left,
                },
            )

        # Couche 2 — OTP existe encore dans Redis ?
        stored_otp = await get_otp(phone)
        if not stored_otp:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "OTP_EXPIRED",
                    "message": "Le code OTP a expiré. Demandez un nouveau code en renvoyant votre numéro.",
                },
            )

        # Couche 3 — Incrémenter et vérifier le compteur de tentatives
        attempts = await increment_otp_attempts(phone)
        if attempts > settings.OTP_MAX_ATTEMPTS:
            # Épuisement total → bloquer le numéro 15 minutes
            await block_phone(phone)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "PHONE_BLOCKED",
                    "message": (
                        "Trop de tentatives incorrectes. "
                        "Ce numéro est bloqué pendant 15 minutes. "
                        "Si vous n'avez pas demandé ce code, ignorez ce message."
                    ),
                    "retry_after_seconds": PHONE_BLOCK_SECONDS,
                },
            )

        # Couche 4 — Comparer les hash SHA-256 (jamais les valeurs en clair)
        if _hash_otp(otp) != stored_otp:
            remaining = settings.OTP_MAX_ATTEMPTS - attempts
            if remaining > 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "INVALID_OTP",
                        "message": (
                            f"Code incorrect. Il vous reste {remaining} tentative(s) "
                            f"avant le blocage temporaire du numéro."
                        ),
                        "attempts_remaining": remaining,
                    },
                )
            else:
                # Dernière tentative épuisée → bloquer maintenant
                await block_phone(phone)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={
                        "code": "PHONE_BLOCKED",
                        "message": (
                            "Code incorrect. Vous avez épuisé toutes vos tentatives. "
                            "Ce numéro est bloqué pendant 15 minutes."
                        ),
                        "retry_after_seconds": PHONE_BLOCK_SECONDS,
                    },
                )

        # OTP correct → supprimer de Redis
        await delete_otp(phone)

        # Chercher ou créer l'utilisateur
        user = await self._get_or_create_user_by_phone(phone)
        user.is_verified = True
        user.last_login = datetime.now(timezone.utc)
        await self.db.flush()

        # Créer la réponse avec tokens
        access_token = create_access_token(user.id)
        refresh_token_str = create_refresh_token(user.id)

        # Persister le refresh token en DB
        await self._persist_refresh_token(
            user_id=user.id,
            token=refresh_token_str,
            device_id=data.device_id if hasattr(data, "device_id") else None,
        )
        await self.db.commit()
        await self.db.refresh(user)

        logger.info(f"Connexion réussie par OTP: {phone} | user_id={user.id}")

        return AuthResponse(
            access_token=access_token,
            refresh_token=refresh_token_str,
            user=UserInToken(
                id=user.id,
                name=user.name,
                phone=user.phone,
                email=user.email,
                role=user.role.value,
                language=user.language,
                is_verified=user.is_verified,
            ),
        )

    # ── Inscription par email ─────────────────────────────────────────────────
    async def register_email(self, data: RegisterEmailRequest) -> AuthResponse:
        """
        Inscription directe par email/mot de passe.
        Utilisé principalement par les agents et admins.
        """
        result = await self.db.execute(
            select(User).where(User.email == data.email)
        )
        if result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "EMAIL_EXISTS", "message": "Cet email est déjà utilisé"},
            )

        user = User(
            email=data.email,
            name=data.name,
            hashed_password=hash_password(data.password),
            is_verified=True,
        )
        self.db.add(user)
        await self.db.flush()

        # Persister le refresh token
        auth_response = self._create_auth_response(user, device_id=data.device_id if hasattr(data, "device_id") else None)
        await self._persist_refresh_token(user_id=user.id, token=auth_response.refresh_token)
        await self.db.commit()
        await self.db.refresh(user)

        logger.info(f"Nouveau compte email: {data.email} | user_id={user.id}")

        return auth_response

    # ── Connexion par email ───────────────────────────────────────────────────
    async def login_email(self, data: LoginEmailRequest) -> AuthResponse:
        """
        Connexion email + mot de passe.

        Raises:
            HTTPException 401: Credentials invalides
            HTTPException 403: Compte désactivé
        """
        result = await self.db.execute(
            select(User).where(User.email == data.email)
        )
        user = result.scalar_one_or_none()

        if not user or not verify_password(data.password, user.hashed_password or ""):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "INVALID_CREDENTIALS", "message": "Email ou mot de passe incorrect"},
            )

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "ACCOUNT_DISABLED", "message": "Ce compte est désactivé"},
            )

        user.last_login = datetime.now(timezone.utc)
        await self.db.flush()

        # Persister le refresh token
        auth_response = self._create_auth_response(user, device_id=data.device_id if hasattr(data, "device_id") else None)
        await self._persist_refresh_token(user_id=user.id, token=auth_response.refresh_token)
        await self.db.commit()

        logger.info(f"Connexion email: {data.email} | user_id={user.id}")

        return auth_response

    # ── Refresh Token avec rotation ───────────────────────────────────────────
    async def refresh_token(self, refresh_token_str: str) -> RefreshResponse:
        """
        Rotation sécurisée du refresh token.

        - Vérifie la signature JWT
        - Vérifie que le token existe en DB et n'est pas révoqué
        - Révoque l'ancien token
        - Émet un nouveau token et le persiste en DB

        Args:
            refresh_token_str: Le refresh token JWT envoyé par le client

        Returns:
            RefreshResponse avec access_token et refresh_token renouvelés

        Raises:
            HTTPException 401: Token invalide, expiré, révoqué ou user inactif
        """
        user_id = verify_token(refresh_token_str, token_type="refresh")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "INVALID_REFRESH_TOKEN", "message": "Token de rafraîchissement invalide ou expiré"},
            )

        # Vérifier en DB que le token n'est pas révoqué
        token_hash = hashlib.sha256(refresh_token_str.encode()).hexdigest()
        result = await self.db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.revoked_at.is_(None),
            )
        )
        stored_token = result.scalar_one_or_none()
        if not stored_token or not stored_token.is_valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "TOKEN_REVOKED", "message": "Cette session a été révoquée. Reconnectez-vous."},
            )

        # Vérifier que le user est toujours actif
        result = await self.db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "ACCOUNT_DISABLED", "message": "Ce compte a été désactivé"},
            )

        # Rotation : révoquer l'ancien token
        stored_token.revoked_at = datetime.now(timezone.utc)
        await self.db.flush()

        # Émettre et persister le nouveau token
        new_access_token = create_access_token(user.id)
        new_refresh_token = create_refresh_token(user.id)
        await self._persist_refresh_token(
            user_id=user.id,
            token=new_refresh_token,
            device_id=stored_token.device_id,
        )
        await self.db.commit()

        logger.info(f"Rotation refresh token: user_id={user.id}")

        return RefreshResponse(
            access_token=new_access_token,
            refresh_token=new_refresh_token,
        )

    # ── Logout — révoquer la session courante ─────────────────────────────────
    async def logout(self, refresh_token_str: str) -> dict:
        """
        Révoque le refresh token fourni.
        N'affecte pas les autres sessions du même utilisateur.

        Args:
            refresh_token_str: Le refresh token à révoquer

        Returns:
            Dict success

        Raises:
            HTTPException 401: Token introuvable ou déjà révoqué
        """
        token_hash = hashlib.sha256(refresh_token_str.encode()).hexdigest()
        result = await self.db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.revoked_at.is_(None),
            )
        )
        stored_token = result.scalar_one_or_none()
        if not stored_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "TOKEN_NOT_FOUND", "message": "Session introuvable ou déjà révoquée"},
            )

        stored_token.revoked_at = datetime.now(timezone.utc)
        await self.db.commit()

        logger.info(f"Logout: token révoqué pour user_id={stored_token.user_id}")
        return {"success": True, "message": "Déconnexion réussie"}

    # ── Logout All — révoquer toutes les sessions ──────────────────────────────
    async def logout_all(self, user_id: str) -> dict:
        """
        Révoque toutes les sessions actives de l'utilisateur.
        Utile en cas de compromission de compte ou déconnexion de tous les appareils.

        Args:
            user_id: ID de l'utilisateur dont révoquer toutes les sessions

        Returns:
            Dict avec le nombre de sessions révoquées
        """
        now = datetime.now(timezone.utc)
        result = await self.db.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
        )
        active_tokens = result.scalars().all()
        count = len(active_tokens)

        for token in active_tokens:
            token.revoked_at = now

        await self.db.commit()

        logger.info(f"Logout all: {count} session(s) révoquée(s) pour user_id={user_id}")
        return {
            "success": True,
            "message": f"{count} session(s) révoquée(s). Tous vos appareils ont été déconnectés.",
            "revoked_count": count,
        }

    # ── Helpers privés ────────────────────────────────────────────────────────
    async def _get_or_create_user_by_phone(self, phone: str) -> User:
        """
        Retourne l'utilisateur existant ou en crée un nouveau.
        C'est le mécanisme d'inscription implicite de TONDE :
        la première connexion par OTP crée automatiquement le compte.
        """
        result = await self.db.execute(
            select(User).where(User.phone == phone)
        )
        user = result.scalar_one_or_none()

        if not user:
            user = User(phone=phone)
            self.db.add(user)
            await self.db.commit()
            await self.db.refresh(user)
            logger.info(f"Nouveau compte créé par OTP: {phone} | user_id={user.id}")

        return user

    def _create_auth_response(self, user: User, device_id: str | None = None) -> AuthResponse:
        """Construit la réponse JWT standard après connexion réussie."""
        return AuthResponse(
            access_token=create_access_token(user.id),
            refresh_token=create_refresh_token(user.id),
            user=UserInToken(
                id=user.id,
                name=user.name,
                phone=user.phone,
                email=user.email,
                role=user.role.value,
                language=user.language,
                is_verified=user.is_verified,
            ),
        )

    async def _persist_refresh_token(
        self,
        user_id: str,
        token: str,
        device_id: str | None = None,
        ip_address: str | None = None,
    ) -> RefreshToken:
        """
        Persiste le hash SHA-256 du refresh token en DB.

        Args:
            user_id: Propriétaire du token
            token: Le JWT brut (jamais stocké — seulement son hash)
            device_id: Identifiant du device pour le tracking multi-device
            ip_address: IP du client pour l'audit

        Returns:
            L'enregistrement RefreshToken créé
        """
        expires_at = datetime.now(timezone.utc) + timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS
        )
        token_record = RefreshToken(
            user_id=user_id,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            device_id=device_id,
            ip_address=ip_address,
            expires_at=expires_at,
        )
        self.db.add(token_record)
        return token_record

    async def _send_otp_sms(self, phone: str, otp: str) -> None:
        """
        Envoie l'OTP par SMS via la passerelle (httpx async, non bloquant).

        En développement, log seulement — aucun SMS envoyé.
        Si l'envoi échoue, l'OTP stocké est supprimé et une erreur 503 est renvoyée :
        répondre « code envoyé » alors que rien n'est parti laisserait l'utilisateur
        attendre un SMS qui n'arrivera jamais.

        Raises:
            HTTPException 503: La passerelle SMS n'a pas accepté le message
        """
        message = (
            f"TONDE: Votre code de vérification est {otp}. "
            f"Valide {settings.OTP_EXPIRE_MINUTES} minutes. "
            f"Ne le partagez jamais."
        )
        try:
            await send_sms(phone, message)
        except SmsDeliveryError as exc:
            logger.error(f"Échec envoi SMS OTP vers {phone}: {exc}")
            await delete_otp(phone)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "SMS_UNAVAILABLE",
                    "message": "Impossible d'envoyer le SMS pour le moment. Réessayez dans quelques instants.",
                },
            ) from exc

    # ── Profil ────────────────────────────────────────────────────────────────
    async def update_profile(self, user: User, name: str | None, language: str | None) -> User:
        """
        Met à jour le nom et/ou la langue de l'utilisateur connecté.

        Args:
            user: Utilisateur connecté
            name: Nouveau nom (None = inchangé)
            language: Nouvelle langue (None = inchangée)

        Returns:
            L'utilisateur mis à jour
        """
        if name is not None:
            user.name = name
        if language is not None:
            user.language = language
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def set_fcm_token(self, user: User, fcm_token: str | None) -> None:
        """
        Enregistre (ou efface) le jeton de push de l'appareil de l'utilisateur.

        Un jeton identifie UN appareil : s'il était associé à un autre compte
        (téléphone partagé, changement de compte), il est retiré de cet autre compte
        pour que les notifications d'un utilisateur n'arrivent pas chez un autre.

        Args:
            user: Utilisateur connecté
            fcm_token: Jeton FCM, ou None pour se désinscrire des push (déconnexion)
        """
        if fcm_token:
            await self.db.execute(
                update(User)
                .where(User.fcm_token == fcm_token, User.id != user.id)
                .values(fcm_token=None)
            )
        user.fcm_token = fcm_token
        await self.db.commit()
