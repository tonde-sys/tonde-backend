"""
Router d'authentification — les endpoints /api/v1/auth/...
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.middlewares import client_ip, limiter, user_agent
from app.models.user import User
from app.schemas.auth import (
    RegisterPhoneRequest, VerifyOtpRequest,
    RegisterEmailRequest, LoginEmailRequest,
    RefreshTokenRequest, LogoutRequest,
    UpdateProfileRequest, FcmTokenRequest,
    ChangePasswordRequest,
    ForgotPasswordRequest, ResetPasswordRequest,
)
from app.services.auth_service import AuthService

router = APIRouter()


@router.post("/register/phone", summary="Inscription par téléphone — envoi OTP")
@limiter.limit("5/minute")
async def register_phone(
    request: Request,
    body: RegisterPhoneRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    L'utilisateur entre son numéro → reçoit un SMS avec le code OTP.

    Limites de sécurité :
    - Max 5 requêtes/minute par IP (couche réseau)
    - Max 3 demandes SMS/minute par numéro (couche service)
    - Numéro bloqué 15 min après trop d'échecs OTP

    En développement, le code est toujours 123456.
    Limité à 5 requêtes/minute par IP pour éviter le flood SMS.
    """
    service = AuthService(db)
    return await service.register_phone(body)


@router.post("/verify-otp", summary="Vérifier le code OTP → obtenir JWT")
@limiter.limit("5/minute")
async def verify_otp(
    request: Request,
    body: VerifyOtpRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    L'utilisateur entre le code OTP reçu par SMS.

    Limites de sécurité :
    - Max 10 requêtes/minute par IP (couche réseau)
    - Max 5 tentatives par code OTP par numéro (couche service)
    - Blocage 15 min du numéro après épuisement des tentatives
    - Message clair indiquant le nombre de tentatives restantes

    Si correct → retourne access_token + refresh_token + profil.
    Limité à 5 requêtes/minute par IP contre le brute force OTP.
    """
    service = AuthService(db)
    return await service.verify_otp(body)


@router.post("/register/email", summary="Inscription par email")
@limiter.limit("5/minute")
async def register_email(
    request: Request,
    body: RegisterEmailRequest,
    db: AsyncSession = Depends(get_db)
):
    """Inscription email/mot de passe. Limité à 5 requêtes/minute par IP contre la création de comptes en masse."""
    service = AuthService(db)
    return await service.register_email(body)


@router.post("/login", summary="Connexion par email + mot de passe")
@limiter.limit("10/minute")
async def login_email(
    request: Request,
    body: LoginEmailRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Connexion email + mot de passe.
    Limité à 10 requêtes/minute par IP contre le brute force password.

    Chaque tentative — réussie, échouée ou bloquée — est journalisée dans
    `audit_logs` avec l'IP et le User-Agent (constat P3-17).
    """
    service = AuthService(db)
    return await service.login_email(
        body,
        ip=client_ip(request),
        user_agent=user_agent(request),
    )


@router.post("/forgot-password", summary="Mot de passe oublié — envoi d'un lien par email")
@limiter.limit("5/minute")
async def forgot_password(
    request: Request,
    body: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Envoie un lien de réinitialisation **si** le compte existe.

    La réponse est toujours la même, compte existant ou non : sans cela, le
    formulaire deviendrait un outil d'énumération des comptes d'une banque
    (`prenom.nom@banque.bi` se devine). Le lien expire après
    `PASSWORD_RESET_TTL_MINUTES` (30 min) et ne sert qu'une fois.

    Limité à 5 requêtes/minute par IP : un envoi d'email coûte de l'argent et
    permet de saturer la boîte d'un agent.
    """
    service = AuthService(db)
    return await service.request_password_reset(
        str(body.email),
        ip=client_ip(request),
        user_agent=user_agent(request),
    )


@router.post("/reset-password", summary="Réinitialiser le mot de passe avec le jeton reçu")
@limiter.limit("5/minute")
async def reset_password(
    request: Request,
    body: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Réinitialise le mot de passe à partir du lien reçu par email.

    Le jeton est à usage unique et expirant. Après succès, **toutes les sessions
    sont fermées** : si le lien avait été intercepté, l'attaquant perd l'accès
    au moment où la victime reprend la main.

    Limité à 5 requêtes/minute par IP : un jeton fait 43 caractères aléatoires,
    mais autant ne pas laisser de surface de force brute.
    """
    service = AuthService(db)
    return await service.reset_password(
        body.token,
        body.new_password,
        ip=client_ip(request),
        user_agent=user_agent(request),
    )


@router.post("/refresh", summary="Renouveler le token d'accès (rotation)")
@limiter.limit("20/minute")
async def refresh_token(
    request: Request,
    body: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Rotation sécurisée du refresh token.
    L'ancien token est révoqué, un nouveau est émis et persisté.
    Limité à 20 requêtes/minute par IP contre l'abus de rotation.
    """
    service = AuthService(db)
    return await service.refresh_token(body.refresh_token)


@router.post("/logout", summary="Déconnexion — révoquer la session courante")
async def logout(
    body: LogoutRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Révoque le refresh token fourni.
    N'affecte pas les autres sessions (autres devices) du même utilisateur.
    """
    service = AuthService(db)
    return await service.logout(body.refresh_token)


@router.post("/logout/all", summary="Déconnexion de tous les appareils")
async def logout_all(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Révoque toutes les sessions actives de l'utilisateur connecté.
    Nécessite un access token valide (Bearer).
    """
    service = AuthService(db)
    return await service.logout_all(current_user.id)


@router.post("/change-password", summary="Changer mon mot de passe")
@limiter.limit("5/minute")
async def change_password(
    request: Request,
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Change le mot de passe du compte connecté.

    Exige l'ancien mot de passe. En cas de succès, **toutes les sessions sont
    révoquées** (y compris l'appareil courant) : le refresh token déjà émis
    reste valide sinon, et un mot de passe changé après un vol de token ne
    protégerait rien. L'access token courant reste utilisable jusqu'à son
    expiration, le temps de terminer l'action en cours.

    Limité à 5 requêtes/minute par IP.
    """
    service = AuthService(db)
    return await service.change_password(
        current_user, body.current_password, body.new_password
    )


# ── Profil de l'utilisateur connecté ──────────────────────────────────────────
@router.get("/me", summary="Mon profil")
async def get_me(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Profil de l'utilisateur connecté, avec son contexte de travail.

    `memberships` liste les rattachements staff actifs (organisation, branche,
    guichet, rôle). Le poste guichet s'en sert pour se configurer seul : sans
    cela il fallait saisir l'organisation et le guichet à la main, avec un
    risque d'ouvrir un guichet auquel l'agent n'est pas affecté
    (COUNTER_NOT_ASSIGNED). Un utilisateur peut avoir plusieurs rattachements
    (multi-organisations) : c'est le client qui choisit.
    """
    token = await AuthService(db).build_user_token(current_user)
    return {"success": True, "data": token}


@router.patch("/me", summary="Modifier mon profil")
async def update_me(
    body: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Met à jour le nom et/ou la langue (fr, en, rn, sw). Seuls les champs fournis changent."""
    service = AuthService(db)
    user = await service.update_profile(current_user, body.name, body.language)
    return {
        "success": True,
        "data": await service.build_user_token(user),
        "message": "Profil mis à jour",
    }


@router.put("/me/fcm-token", summary="Enregistrer le jeton de notifications push")
async def set_fcm_token(
    body: FcmTokenRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Enregistre le jeton FCM de l'appareil (obligatoire pour recevoir les notifications push :
    « c'est votre tour », avertissement avant absence…).
    Envoyer `{"fcm_token": null}` à la déconnexion pour ne plus en recevoir.
    """
    await AuthService(db).set_fcm_token(current_user, body.fcm_token)
    return {
        "success": True,
        "message": "Notifications push activées" if body.fcm_token else "Notifications push désactivées",
    }
