"""
Tests — mot de passe oublié (phase 4, dernier constat produit).

Le trou comblé : jusqu'ici, un agent qui oubliait son mot de passe devait
appeler l'administrateur, qui le régénérait depuis un shell Render avec
`python -m app.cli reset-password`. Pour une organisation partenaire (ULBU) qui
gère elle-même ses guichets, cela veut dire qu'aucun agent ne peut se
dépanner seul un samedi matin.

Ce que ces tests protègent, dans l'ordre d'importance :
  1. **Pas d'énumération de comptes** — la réponse de `forgot-password` ne doit
     jamais varier selon que l'adresse existe ou non, même quand l'envoi SMTP
     échoue. Les emails staff suivent un motif devinable (`prenom.nom@banque.bi`) :
     un formulaire bavard devient un annuaire.
  2. **Usage unique** — un lien ne sert qu'une fois, et demander un nouveau lien
     invalide l'ancien.
  3. **Reprise de contrôle** — après réinitialisation, toutes les sessions
     tombent : si le lien avait été intercepté, l'attaquant est éjecté.
  4. **Traçabilité** — `password_reset_requested` et `password_reset` atterrissent
     dans `audit_logs`, y compris pour un compte inexistant (org_id NULL).

Le backend Redis est remplacé par un dictionnaire en mémoire, mais les
fonctions de `app.core.redis` restent les vraies : c'est leur logique
(hachage du jeton, index inverse, consommation) qu'on veut éprouver, pas un mock.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.models.audit_log import AuditAction, AuditLog
from app.models.refresh_token import RefreshToken
from app.models.user import User, UserRole
from app.schemas.auth import ForgotPasswordRequest, ResetPasswordRequest
from app.services import email_gateway
from app.services.email_gateway import EmailDeliveryError

OLD = "Ancien-Mot-De-Passe-2026"
NEW = "Nouveau-Mot-De-Passe-2026"


# ── Doublures ────────────────────────────────────────────────────────────────

class FakeRedis:
    """
    Le strict minimum utilisé par les helpers de jeton : get / setex / delete.

    Volontairement pas un AsyncMock générique : on veut pouvoir constater
    qu'une clé a bien été supprimée (usage unique) ou qu'elle n'a jamais été
    écrite (compte inconnu). Un mock qui répond `None` à tout ne prouve rien.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str):
        self.store[key] = value
        return True

    async def delete(self, *keys: str):
        """`del` accepte plusieurs clés : `clear_login_failures` en supprime trois."""
        return sum(1 for key in keys if self.store.pop(key, None) is not None)


@pytest.fixture
def fake_redis():
    """Remplace le client Redis par un dictionnaire, en gardant le vrai code."""
    fake = FakeRedis()
    with patch("app.core.redis.get_redis", new_callable=AsyncMock) as factory:
        factory.return_value = fake
        yield fake


@pytest.fixture
def sent_emails():
    """
    Capture les emails sortants sans toucher au réseau.

    Patch dans `app.services.auth_service` : c'est le module qui a importé le
    nom. Patcher `email_gateway.send_password_reset_email` ne changerait rien.
    """
    with patch(
        "app.services.auth_service.send_password_reset_email",
        new_callable=AsyncMock,
    ) as sender:
        yield sender


async def _user(db_session, *, email="agent1@ulbu.bi", password=OLD, active=True):
    user = User(
        email=email,
        name="Agent Guichet 1",
        hashed_password=hash_password(password) if password else None,
        is_active=active,
        is_verified=True,
        role=UserRole.AGENT,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _session(db_session, user, suffix="poste-guichet"):
    token = RefreshToken(
        user_id=user.id,
        token_hash=f"{user.id[:8]}{suffix}".ljust(64, "0")[:64],
        device_id=suffix,
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    db_session.add(token)
    await db_session.commit()
    await db_session.refresh(token)
    return token


async def _audit_actions(db_session) -> list[str]:
    rows = (await db_session.execute(AuditLog.__table__.select())).all()
    return [row.action for row in rows]


async def _reset_token_from(sender: AsyncMock) -> str:
    """Extrait le jeton du lien contenu dans l'email capturé."""
    link = sender.await_args.args[2]
    return link.split("token=", 1)[1]


# ── 1. Pas d'énumération de comptes ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_compte_inconnu_recoit_la_meme_reponse(auth_service, fake_redis, sent_emails):
    """Adresse inconnue : même corps de réponse, et aucun email parti."""
    result = await auth_service.request_password_reset("inconnu@ulbu.bi")

    assert result["success"] is True
    assert "Si un compte existe" in result["message"]
    sent_emails.assert_not_awaited()
    # Aucun jeton écrit : rien à réinitialiser, et rien à deviner.
    assert fake_redis.store == {}


@pytest.mark.asyncio
async def test_reponse_identique_compte_existant_ou_non(
    auth_service, db_session, fake_redis, sent_emails
):
    """
    La réponse doit être *strictement* identique dans les deux cas.

    C'est le cœur de la protection : une différence d'un seul caractère
    (ponctuation, mention des indésirables…) suffirait à distinguer un compte
    existant d'un compte inconnu.
    """
    await _user(db_session)

    with_account = await auth_service.request_password_reset("agent1@ulbu.bi")
    without_account = await auth_service.request_password_reset("fantome@ulbu.bi")

    assert with_account == without_account


@pytest.mark.asyncio
async def test_compte_sans_mot_de_passe_reponse_neutre(
    auth_service, db_session, fake_redis, sent_emails
):
    """
    Un client inscrit par OTP n'a pas de mot de passe : aucun email, mais la
    même réponse — sinon on révélerait son mode d'inscription.
    """
    await _user(db_session, email="client@example.com", password=None)

    result = await auth_service.request_password_reset("client@example.com")

    assert result["success"] is True
    sent_emails.assert_not_awaited()
    assert fake_redis.store == {}


@pytest.mark.asyncio
async def test_echec_smtp_ne_revele_pas_l_existence(
    auth_service, db_session, fake_redis, sent_emails
):
    """
    SMTP en panne : l'appelant reçoit quand même la réponse neutre.

    L'erreur est journalisée pour l'exploitant, pas renvoyée au visiteur. Un
    « envoi impossible » ne concernerait que les comptes existants, donc
    discriminerait.
    """
    await _user(db_session)
    sent_emails.side_effect = EmailDeliveryError("SMTP injoignable")

    result = await auth_service.request_password_reset("agent1@ulbu.bi")

    assert result["success"] is True
    assert "Si un compte existe" in result["message"]


@pytest.mark.asyncio
async def test_email_normalise_avant_recherche(auth_service, db_session, fake_redis, sent_emails):
    """« Agent1@ULBU.bi » doit retrouver le compte : les emails sont insensibles à la casse."""
    await _user(db_session, email="agent1@ulbu.bi")

    await auth_service.request_password_reset("  Agent1@ULBU.BI  ")

    sent_emails.assert_awaited_once()
    assert sent_emails.await_args.args[0] == "agent1@ulbu.bi"


# ── 2. Le jeton : stockage, unicité, expiration ─────────────────────────────

@pytest.mark.asyncio
async def test_jeton_stocke_hache_jamais_en_clair(
    auth_service, db_session, fake_redis, sent_emails
):
    """
    Redis ne doit contenir que le SHA-256 du jeton.

    Un dump Redis volé ne doit pas permettre de réinitialiser les mots de passe
    de tous les comptes — même raisonnement que pour les OTP.
    """
    await _user(db_session)

    await auth_service.request_password_reset("agent1@ulbu.bi")
    raw_token = await _reset_token_from(sent_emails)

    keys = " ".join(fake_redis.store.keys())
    values = " ".join(str(v) for v in fake_redis.store.values())
    assert raw_token not in keys
    assert raw_token not in values
    assert any(k.startswith("tonde:pwdreset:") for k in fake_redis.store)


@pytest.mark.asyncio
async def test_jeton_envoye_avec_le_lien_complet(
    auth_service, db_session, fake_redis, sent_emails
):
    """L'email part vers le bon destinataire, avec le bon lien et le bon TTL."""
    user = await _user(db_session)

    await auth_service.request_password_reset("agent1@ulbu.bi")

    sent_emails.assert_awaited_once()
    to, name, link, ttl = sent_emails.await_args.args
    assert to == "agent1@ulbu.bi"
    assert name == user.name
    assert link.startswith(settings.PASSWORD_RESET_URL)
    assert ttl == settings.PASSWORD_RESET_TTL_MINUTES


@pytest.mark.asyncio
async def test_nouvelle_demande_invalide_l_ancien_lien(
    auth_service, db_session, fake_redis, sent_emails
):
    """
    Deux demandes successives : seul le dernier lien fonctionne.

    Sans cela, un lien intercepté resterait valable après que la victime a
    demandé un nouveau lien — donc indéfiniment, jusqu'à expiration.
    """
    await _user(db_session)

    await auth_service.request_password_reset("agent1@ulbu.bi")
    first_token = await _reset_token_from(sent_emails)
    await auth_service.request_password_reset("agent1@ulbu.bi")
    second_token = await _reset_token_from(sent_emails)

    assert first_token != second_token
    with pytest.raises(HTTPException) as exc_info:
        await auth_service.reset_password(first_token, NEW)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "INVALID_RESET_TOKEN"


# ── 3. Réinitialisation réussie ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reinitialisation_reussie(auth_service, db_session, fake_redis, sent_emails):
    """Le mot de passe est remplacé : l'ancien ne passe plus, le nouveau oui."""
    user = await _user(db_session)

    await auth_service.request_password_reset("agent1@ulbu.bi")
    token = await _reset_token_from(sent_emails)

    result = await auth_service.reset_password(token, NEW)

    assert result["success"] is True
    await db_session.refresh(user)
    assert verify_password(NEW, user.hashed_password)
    assert not verify_password(OLD, user.hashed_password)


@pytest.mark.asyncio
async def test_jeton_a_usage_unique(auth_service, db_session, fake_redis, sent_emails):
    """Rejouer le même lien échoue : c'est la définition d'un jeton à usage unique."""
    await _user(db_session)

    await auth_service.request_password_reset("agent1@ulbu.bi")
    token = await _reset_token_from(sent_emails)
    await auth_service.reset_password(token, NEW)

    with pytest.raises(HTTPException) as exc_info:
        await auth_service.reset_password(token, "Troisieme-Mot-De-Passe-2026")

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "INVALID_RESET_TOKEN"


@pytest.mark.asyncio
async def test_jeton_inconnu_refuse(auth_service, fake_redis):
    """Un jeton inventé ne doit pas passer — évidemment, mais ça se teste."""
    with pytest.raises(HTTPException) as exc_info:
        await auth_service.reset_password("x" * 43, NEW)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "INVALID_RESET_TOKEN"


@pytest.mark.asyncio
async def test_jeton_expire_refuse(auth_service, db_session, fake_redis, sent_emails):
    """
    Expiration : on simule la disparition de la clé (le TTL Redis fait le reste).

    Ce qui compte ici, c'est que l'absence de clé soit traitée comme un refus
    propre — 400 avec un code métier, pas une 500.
    """
    await _user(db_session)
    await auth_service.request_password_reset("agent1@ulbu.bi")
    token = await _reset_token_from(sent_emails)
    fake_redis.store.clear()

    with pytest.raises(HTTPException) as exc_info:
        await auth_service.reset_password(token, NEW)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "INVALID_RESET_TOKEN"


@pytest.mark.asyncio
async def test_toutes_les_sessions_revoquees(auth_service, db_session, fake_redis, sent_emails):
    """
    Reprise de contrôle : les sessions ouvertes avant la réinitialisation tombent.

    C'est la raison d'être de la réinitialisation. Si le lien a été intercepté,
    l'attaquant qui s'est connecté avec est éjecté au moment où la victime
    choisit un nouveau mot de passe.
    """
    user = await _user(db_session)
    first = await _session(db_session, user, "poste-guichet")
    second = await _session(db_session, user, "telephone")

    await auth_service.request_password_reset("agent1@ulbu.bi")
    token = await _reset_token_from(sent_emails)
    result = await auth_service.reset_password(token, NEW)

    assert result["revoked_sessions"] == 2
    await db_session.refresh(first)
    await db_session.refresh(second)
    assert first.revoked_at is not None
    assert second.revoked_at is not None


# ── 4. Comptes inutilisables ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compte_desactive_apres_emission_refuse(
    auth_service, db_session, fake_redis, sent_emails
):
    """
    Compte désactivé entre l'envoi et l'usage du lien : refus, et jeton consommé.

    Sans consommation, le jeton resterait valable jusqu'à son expiration ; si le
    compte était réactivé entre-temps, un lien envoyé à un compte révoqué
    rouvrirait l'accès.
    """
    user = await _user(db_session)
    await auth_service.request_password_reset("agent1@ulbu.bi")
    token = await _reset_token_from(sent_emails)

    user.is_active = False
    await db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await auth_service.reset_password(token, NEW)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "INVALID_RESET_TOKEN"
    assert fake_redis.store == {}


@pytest.mark.asyncio
async def test_compte_sans_mot_de_passe_renvoie_409(
    auth_service, db_session, fake_redis, sent_emails
):
    """
    Compte OTP qui reçoit un jeton par un autre chemin : 409, pas 400.

    La distinction compte : 400 dit « mauvais lien », 409 dit « ce compte
    n'utilise pas de mot de passe » — l'interface peut alors orienter vers le
    bon parcours.
    """
    user = await _user(db_session, email="client@example.com", password=None)

    from app.core.redis import save_password_reset_token

    await save_password_reset_token(user.id, "j" * 43, 600)

    with pytest.raises(HTTPException) as exc_info:
        await auth_service.reset_password("j" * 43, NEW)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "PASSWORD_NOT_SET"


# ── 5. Traçabilité ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_demande_auditee(auth_service, db_session, fake_redis, sent_emails):
    """`password_reset_requested` est journalisé avec l'acteur et l'IP."""
    user = await _user(db_session)

    await auth_service.request_password_reset(
        "agent1@ulbu.bi", ip="41.79.10.4", user_agent="Mozilla/5.0 (Windows NT 10.0)"
    )

    rows = (await db_session.execute(AuditLog.__table__.select())).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.action == AuditAction.PASSWORD_RESET_REQUESTED
    assert row.actor_id == user.id
    assert row.actor_role == UserRole.AGENT.value
    assert row.extra_data["ip"] == "41.79.10.4"
    assert row.extra_data["email"] == "agent1@ulbu.bi"


@pytest.mark.asyncio
async def test_demande_compte_inconnu_auditee_sans_org(auth_service, db_session, fake_redis):
    """
    Un compte inexistant est aussi journalisé, avec `org_id` NULL.

    C'est ce que la migration 009 a rendu possible : avant, la ligne ne pouvait
    pas être écrite (FK non nulle), donc une campagne d'énumération ne laissait
    aucune trace. Et c'est justement la trace qu'on veut pouvoir chercher.
    """
    await auth_service.request_password_reset("prenom.nom@ulbu.bi", ip="102.66.1.9")

    rows = (await db_session.execute(AuditLog.__table__.select())).all()
    assert len(rows) == 1
    assert rows[0].org_id is None
    assert rows[0].actor_id is None
    assert rows[0].extra_data["reason"] == "no_deliverable_account"


@pytest.mark.asyncio
async def test_reinitialisation_auditee(auth_service, db_session, fake_redis, sent_emails):
    """`password_reset` clôt la piste ouverte par la demande."""
    user = await _user(db_session)
    await auth_service.request_password_reset("agent1@ulbu.bi")
    token = await _reset_token_from(sent_emails)

    await auth_service.reset_password(token, NEW, ip="41.79.10.4")

    actions = await _audit_actions(db_session)
    assert actions.count(AuditAction.PASSWORD_RESET_REQUESTED) == 1
    assert actions.count(AuditAction.PASSWORD_RESET) == 1

    reset_row = (
        await db_session.execute(
            AuditLog.__table__.select().where(
                AuditLog.__table__.c.action == AuditAction.PASSWORD_RESET
            )
        )
    ).first()
    assert reset_row.actor_id == user.id


# ── 6. Passerelle email ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_smtp_non_configure_echoue_bruyamment(monkeypatch):
    """
    En production, un SMTP absent est une erreur explicite.

    Un `return` silencieux ferait croire à un envoi : l'utilisateur attendrait
    un email qui n'arriverait jamais, et l'exploitant n'aurait aucun signal.
    """
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SMTP_HOST", "")

    with pytest.raises(EmailDeliveryError) as exc_info:
        await email_gateway.send_email("agent1@ulbu.bi", "Sujet", "Corps")

    assert "SMTP non configuré" in str(exc_info.value)


@pytest.mark.asyncio
async def test_echec_smtp_remonte_en_email_delivery_error(monkeypatch):
    """Une panne réseau devient une `EmailDeliveryError`, pas une exception brute."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(email_gateway, "_send_sync", boom)

    with pytest.raises(EmailDeliveryError):
        await email_gateway.send_email("agent1@ulbu.bi", "Sujet", "Corps")


@pytest.mark.asyncio
async def test_envoi_reussi_ne_leve_pas(monkeypatch):
    """Configuration complète : l'envoi aboutit sans exception."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(email_gateway, "_send_sync", lambda *a, **k: None)

    await email_gateway.send_email("agent1@ulbu.bi", "Sujet", "Corps")


def test_is_configure_sans_host(monkeypatch):
    """`is_configured()` sert au CLI et au rapport de santé : il doit être honnête."""
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    assert email_gateway.is_configured() is False

    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "SMTP_FROM", "TONDE <no-reply@tonde.app>")
    assert email_gateway.is_configured() is True


# ── 7. Validation des schémas ───────────────────────────────────────────────

def test_adresse_invalide_refusee():
    with pytest.raises(ValidationError):
        ForgotPasswordRequest(email="pas-une-adresse")


def test_jeton_trop_court_refuse():
    """Un jeton de 43 caractères ne se confond pas avec une saisie tronquée."""
    with pytest.raises(ValidationError):
        ResetPasswordRequest(token="court", new_password=NEW)


def test_mot_de_passe_trop_court_refuse():
    with pytest.raises(ValidationError):
        ResetPasswordRequest(token="t" * 43, new_password="court")


def test_mot_de_passe_au_dela_de_72_octets_refuse():
    """
    Même garde que `ChangePasswordRequest` : bcrypt tronque à 72 octets, donc
    au-delà les caractères saisis ne comptent pas. Autant le dire.
    """
    with pytest.raises(ValidationError):
        ResetPasswordRequest(token="t" * 43, new_password="a" * 73)


def test_requete_valide_acceptee():
    body = ResetPasswordRequest(token="t" * 43, new_password=NEW)
    assert body.new_password == NEW


# ── 8. Câblage des routes ───────────────────────────────────────────────────

@pytest.mark.parametrize(
    "path",
    ["/api/v1/auth/forgot-password", "/api/v1/auth/reset-password"],
)
def test_route_exposee_et_publique(path):
    """
    Les deux routes doivent exister, accepter POST, et rester **publiques**.

    Un utilisateur qui a oublié son mot de passe n'a pas de jeton d'accès : si
    `get_current_user` se glissait dans la signature lors d'un refactor, la
    fonctionnalité deviendrait inutilisable pour son seul public.
    """
    from app.main import app

    matches = [r for r in app.routes if getattr(r, "path", None) == path]
    assert len(matches) == 1
    route = matches[0]
    assert "POST" in route.methods
    assert not any(
        getattr(dep.call, "__name__", "") in {"get_current_user", "get_current_agent"}
        for dep in route.dependant.dependencies
    )
