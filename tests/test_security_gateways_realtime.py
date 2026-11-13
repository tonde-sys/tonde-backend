"""
Tests — sécurité de la configuration, contrôle d'accès, passerelles SMS / FCM,
notifications et routage temps réel.

Les passerelles sont testées contre un faux serveur HTTP (httpx.MockTransport) : le code
réseau réel s'exécute, aucun appel ne sort de la machine.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwt
from pydantic import ValidationError
from sqlalchemy import select

from app.core import security
from app.core.access import ROLE_RANK, assert_branch_access, assert_can_assign_role, assert_org_access
from app.core.config import Settings, settings
from app.core.redis import _hash_otp, get_otp
from app.models.notification import Notification, NotificationChannel, NotificationStatus
from app.models.ticket import Ticket, TicketStatus
from app.models.user import User, UserRole
from app.schemas.auth import RegisterPhoneRequest
from app.services import fcm_gateway, sms_gateway
from app.services.auth_service import AuthService
from app.services.notification_service import NotificationService
from app.websocket.queue_ws import QueueWebSocketManager
from tests.http_helpers import make_branch, make_org, make_staff, make_user, make_world
from tests.test_queue_atomicity import real_redis  # noqa: F401  (fixture importée par nom)

STRONG_SECRET = "a" * 8 + "b" * 8 + "c" * 8 + "d" * 8 + "e" * 8   # 40 caractères


def make_settings(**overrides) -> Settings:
    """Construit des Settings isolés (ni .env ni variables d'environnement du processus)."""
    values = dict(DATABASE_URL="postgresql+asyncpg://u:p@h/db", JWT_SECRET_KEY=STRONG_SECRET,
                  ENVIRONMENT="development", DEBUG=True)
    values.update(overrides)
    return Settings(_env_file=None, **values)


# ══ Audit de configuration ════════════════════════════════════════════════════

@pytest.mark.parametrize("secret", ["", "x", "changeme", "CHANGE_THIS_IN_PRODUCTION", "court", "z" * 31])
def test_production_and_staging_refuse_weak_jwt_secret(secret):
    for environment in ("production", "staging"):
        with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
            make_settings(ENVIRONMENT=environment, JWT_SECRET_KEY=secret, DEBUG=False)


def test_development_accepts_a_weak_secret():
    assert make_settings(JWT_SECRET_KEY="x").is_development is True


def test_production_refuses_debug_and_auto_table_creation():
    with pytest.raises(ValidationError, match="DEBUG"):
        make_settings(ENVIRONMENT="production", DEBUG=True)
    with pytest.raises(ValidationError, match="CREATE_TABLES_ON_STARTUP"):
        make_settings(ENVIRONMENT="production", DEBUG=False, CREATE_TABLES_ON_STARTUP=True)


def test_valid_production_configuration_starts():
    config = make_settings(ENVIRONMENT="production", DEBUG=False)
    assert config.is_production is True and config.is_development is False


def test_production_refuses_wildcard_cors_origin():
    with pytest.raises(ValidationError, match="ALLOWED_ORIGINS"):
        make_settings(ENVIRONMENT="production", DEBUG=False, ALLOWED_ORIGINS="*")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("postgres://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgresql://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgresql+asyncpg://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
    ],
)
def test_managed_database_urls_are_normalized_for_asyncpg(raw, expected):
    assert make_settings(DATABASE_URL=raw).DATABASE_URL == expected


def test_unknown_environment_is_refused():
    with pytest.raises(ValidationError, match="ENVIRONMENT"):
        make_settings(ENVIRONMENT="prod")        # une faute de frappe ne doit pas passer en « dev » sans bruit


def test_absent_warning_must_precede_timeout():
    with pytest.raises(ValidationError, match="ABSENT_WARNING_SECONDS"):
        make_settings(ABSENT_WARNING_SECONDS=200, ABSENT_TIMEOUT_SECONDS=180)


# ══ OTP ═══════════════════════════════════════════════════════════════════════

def test_otp_uses_cryptographic_generator():
    assert not hasattr(security, "random"), "le module random (prévisible) n'est plus utilisé"
    codes = {security.generate_otp() for _ in range(300)}
    assert all(len(c) == 6 and c.isdigit() for c in codes)
    assert len(codes) > 250, "codes bien répartis (pas de séquence prévisible)"
    with patch("app.core.security.secrets.choice", return_value="7") as choice:
        assert security.generate_otp() == "777777" and choice.call_count == 6


@pytest.mark.asyncio
async def test_otp_sms_failure_returns_503_and_discards_the_code(db_session, real_redis, monkeypatch):
    """Si le SMS n'est pas parti, on ne répond pas « code envoyé » : 503 et OTP supprimé."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    phone = "+25779123456"
    with patch("app.services.auth_service.send_sms", new_callable=AsyncMock,
               side_effect=sms_gateway.SmsDeliveryError("passerelle hors service")):
        with pytest.raises(HTTPException) as exc:
            await AuthService(db_session).register_phone(RegisterPhoneRequest(phone=phone))

    assert exc.value.status_code == 503 and exc.value.detail["code"] == "SMS_UNAVAILABLE"
    assert await get_otp(phone) is None, "aucun OTP ne reste valide pour un SMS jamais envoyé"


@pytest.mark.asyncio
async def test_production_never_exposes_the_dev_otp(db_session, real_redis, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with patch("app.services.auth_service.send_sms", new_callable=AsyncMock):
        result = await AuthService(db_session).register_phone(RegisterPhoneRequest(phone="+25779123457"))

    assert "dev_otp" not in result
    assert await get_otp("+25779123457") != _hash_otp("123456"), "le code de test 123456 n'est pas actif hors développement"


# ══ Contrôle d'accès ══════════════════════════════════════════════════════════

def user_with(role: UserRole) -> User:
    return User(id=f"u-{role.value}", role=role, org_id="org-1")


@pytest.mark.parametrize("actor, target, allowed", [
    (UserRole.SUPER_ADMIN, UserRole.SUPER_ADMIN, True),
    (UserRole.SUPER_ADMIN, UserRole.ADMIN_ORG, True),
    (UserRole.ADMIN_ORG, UserRole.SUPER_ADMIN, False),
    (UserRole.ADMIN_ORG, UserRole.ADMIN_ORG, True),
    (UserRole.ADMIN_ORG, UserRole.AGENT, True),
    (UserRole.ADMIN_AGENCY, UserRole.ADMIN_ORG, False),
    (UserRole.ADMIN_AGENCY, UserRole.ADMIN_AGENCY, False),
    (UserRole.ADMIN_AGENCY, UserRole.SUPERVISOR, True),
    (UserRole.ADMIN_AGENCY, UserRole.AGENT, True),
    (UserRole.SUPERVISOR, UserRole.AGENT, True),
    (UserRole.SUPERVISOR, UserRole.SUPERVISOR, False),
])
def test_role_assignment_matrix(actor, target, allowed):
    if allowed:
        assert_can_assign_role(user_with(actor), target)
    else:
        with pytest.raises(HTTPException) as exc:
            assert_can_assign_role(user_with(actor), target)
        assert exc.value.status_code == 403 and exc.value.detail["code"] == "ROLE_ESCALATION"


def test_role_ranks_are_strictly_increasing():
    ordered = [UserRole.CLIENT, UserRole.AGENT, UserRole.SUPERVISOR, UserRole.ADMIN_AGENCY,
               UserRole.ADMIN_ORG, UserRole.SUPER_ADMIN]
    assert [ROLE_RANK[r] for r in ordered] == sorted(ROLE_RANK.values()) == list(range(6))


def test_org_access_rules():
    own = User(id="u1", role=UserRole.ADMIN_ORG, org_id="org-1")
    assert_org_access(own, "org-1")
    with pytest.raises(HTTPException):
        assert_org_access(own, "org-2")
    with pytest.raises(HTTPException):
        assert_org_access(User(id="u2", role=UserRole.ADMIN_ORG, org_id=None), "org-1")
    assert_org_access(User(id="u3", role=UserRole.SUPER_ADMIN, org_id=None), "org-2")   # équipe Tonde


@pytest.mark.asyncio
async def test_branch_access_by_role(db_session):
    org = await make_org(db_session)
    mine = await make_branch(db_session, org.id, "Mine")
    other = await make_branch(db_session, org.id, "Autre")
    foreign_org = await make_org(db_session)
    foreign_branch = await make_branch(db_session, foreign_org.id)
    supervisor = await make_staff(db_session, org.id, UserRole.SUPERVISOR, mine.id)
    admin_org = await make_staff(db_session, org.id, UserRole.ADMIN_ORG)
    super_admin = await make_user(db_session, UserRole.SUPER_ADMIN)

    assert (await assert_branch_access(db_session, supervisor, org.id, mine.id)).id == mine.id
    assert (await assert_branch_access(db_session, admin_org, org.id, other.id)).id == other.id
    assert (await assert_branch_access(db_session, super_admin, foreign_org.id, foreign_branch.id)).id == foreign_branch.id

    with pytest.raises(HTTPException) as exc:
        await assert_branch_access(db_session, supervisor, org.id, other.id)
    assert exc.value.detail["code"] == "BRANCH_FORBIDDEN"
    with pytest.raises(HTTPException) as exc:        # branch d'une autre organisation : introuvable
        await assert_branch_access(db_session, admin_org, org.id, foreign_branch.id)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:        # autre organisation : refusé avant même la recherche
        await assert_branch_access(db_session, supervisor, foreign_org.id, foreign_branch.id)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_branch_scope_includes_branch_reached_through_counter_and_ignores_inactive(db_session):
    from app.models.employee import Employee, EmployeeStatus
    from tests.http_helpers import make_counter
    org = await make_org(db_session)
    branch = await make_branch(db_session, org.id)
    counter = await make_counter(db_session, org.id, branch.id)
    via_counter = await make_user(db_session, UserRole.SUPERVISOR, org.id)
    db_session.add(Employee(org_id=org.id, user_id=via_counter.id, counter_id=counter.id, role=UserRole.SUPERVISOR))
    await db_session.commit()

    assert (await assert_branch_access(db_session, via_counter, org.id, branch.id)).id == branch.id

    employee = (await db_session.execute(select(Employee).where(Employee.user_id == via_counter.id))).scalar_one()
    employee.status = EmployeeStatus.SUSPENDED
    await db_session.commit()
    with pytest.raises(HTTPException):
        await assert_branch_access(db_session, via_counter, org.id, branch.id)


# ══ Passerelle SMS ════════════════════════════════════════════════════════════

def mock_httpx(monkeypatch, module, handler):
    """Remplace httpx.AsyncClient d'un module par un client branché sur un faux serveur."""
    real_client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))


def at_response(status_code=101, status="Success"):
    return {"SMSMessageData": {"Message": "Sent to 1/1", "Recipients": [
        {"number": "+25779000001", "statusCode": status_code, "status": status}]}}


@pytest.mark.asyncio
async def test_sms_in_development_makes_no_network_call(monkeypatch):
    def boom(request):
        raise AssertionError("aucun appel réseau en développement")
    mock_httpx(monkeypatch, sms_gateway, boom)

    await sms_gateway.send_sms("+25779000001", "bonjour")        # ne lève pas


@pytest.mark.asyncio
async def test_sms_production_success_sends_expected_request(monkeypatch):
    seen = {}

    def handler(request: httpx.Request):
        seen.update(url=str(request.url), key=request.headers["apikey"], body=request.content.decode())
        return httpx.Response(201, json=at_response())

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "AFRICAS_TALKING_API_KEY", "cle-secrete")
    monkeypatch.setattr(settings, "AFRICAS_TALKING_USERNAME", "tonde")
    mock_httpx(monkeypatch, sms_gateway, handler)

    await sms_gateway.send_sms("+25779000001", "Votre ticket A-001")

    assert seen["url"] == "https://api.africastalking.com/version1/messaging"
    assert seen["key"] == "cle-secrete"
    assert "username=tonde" in seen["body"] and "to=%2B25779000001" in seen["body"]


@pytest.mark.asyncio
async def test_sms_sandbox_username_uses_sandbox_endpoint(monkeypatch):
    seen = {}
    monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
    monkeypatch.setattr(settings, "AFRICAS_TALKING_API_KEY", "k")
    monkeypatch.setattr(settings, "AFRICAS_TALKING_USERNAME", "sandbox")
    mock_httpx(monkeypatch, sms_gateway,
               lambda r: (seen.update(url=str(r.url)), httpx.Response(201, json=at_response()))[1])

    await sms_gateway.send_sms("+25779000001", "x")

    assert "api.sandbox.africastalking.com" in seen["url"]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(401, json={"error": "bad key"}),                       # clé refusée
    httpx.Response(500, text="boom"),                                      # panne passerelle
    httpx.Response(201, json=at_response(403, "InvalidPhoneNumber")),      # numéro refusé
    httpx.Response(201, json={"SMSMessageData": {"Message": "x", "Recipients": []}}),   # personne n'a été accepté
    httpx.Response(201, text="pas du json"),                               # réponse illisible
])
async def test_sms_failures_raise_delivery_error(monkeypatch, response):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "AFRICAS_TALKING_API_KEY", "k")
    mock_httpx(monkeypatch, sms_gateway, lambda r: response)

    with pytest.raises(sms_gateway.SmsDeliveryError):
        await sms_gateway.send_sms("+25779000001", "x")


@pytest.mark.asyncio
async def test_sms_without_api_key_fails_explicitly_in_production(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "AFRICAS_TALKING_API_KEY", "")
    with pytest.raises(sms_gateway.SmsDeliveryError, match="API_KEY"):
        await sms_gateway.send_sms("+25779000001", "x")


# ══ Passerelle FCM v1 ═════════════════════════════════════════════════════════

@pytest.fixture
def firebase(monkeypatch):
    """Compte de service Firebase factice (vraie clé RSA) + environnement de production."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "FIREBASE_PROJECT_ID", "tonde-test")
    monkeypatch.setattr(settings, "FIREBASE_SERVICE_ACCOUNT_JSON",
                        json.dumps({"client_email": "svc@tonde-test.iam.gserviceaccount.com", "private_key": pem}))
    fcm_gateway.reset_token_cache()
    yield public_pem
    fcm_gateway.reset_token_cache()


def fcm_handler(calls: list, send_status=200, send_text="{}", public_pem=None):
    def handler(request: httpx.Request):
        calls.append(request)
        if str(request.url) == "https://oauth2.googleapis.com/token":
            if public_pem:
                form = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
                claims = jwt.decode(form["assertion"].replace("%2E", "."), public_pem, algorithms=["RS256"],
                                    audience="https://oauth2.googleapis.com/token")
                assert claims["iss"] == "svc@tonde-test.iam.gserviceaccount.com"
                assert claims["scope"] == "https://www.googleapis.com/auth/firebase.messaging"
            return httpx.Response(200, json={"access_token": "ya29.token", "expires_in": 3600})
        return httpx.Response(send_status, text=send_text)
    return handler


@pytest.mark.asyncio
async def test_fcm_sends_v1_message_with_oauth_token(firebase, monkeypatch):
    calls: list = []
    mock_httpx(monkeypatch, fcm_gateway, fcm_handler(calls, public_pem=firebase))

    await fcm_gateway.send_push("device-token-123", "Ticket A-001", "À vous !", {"ticket_id": 42})

    send = calls[-1]
    assert str(send.url) == "https://fcm.googleapis.com/v1/projects/tonde-test/messages:send"
    assert send.headers["authorization"] == "Bearer ya29.token"
    message = json.loads(send.content)["message"]
    assert message["token"] == "device-token-123"
    assert message["notification"] == {"title": "Ticket A-001", "body": "À vous !"}
    assert message["data"] == {"ticket_id": "42"}, "les valeurs de data doivent être des chaînes"
    assert message["android"]["priority"] == "HIGH"


@pytest.mark.asyncio
async def test_fcm_access_token_is_cached(firebase, monkeypatch):
    calls: list = []
    mock_httpx(monkeypatch, fcm_gateway, fcm_handler(calls))

    await fcm_gateway.send_push("device-token-123", "t", "b")
    await fcm_gateway.send_push("device-token-123", "t", "b")

    token_requests = [c for c in calls if "oauth2" in str(c.url)]
    assert len(token_requests) == 1, "un seul échange de jeton OAuth pour deux envois"


@pytest.mark.asyncio
async def test_fcm_unregistered_device_raises_token_invalid(firebase, monkeypatch):
    mock_httpx(monkeypatch, fcm_gateway, fcm_handler([], 404, '{"error":{"status":"NOT_FOUND","details":[{"errorCode":"UNREGISTERED"}]}}'))
    with pytest.raises(fcm_gateway.FcmTokenInvalid):
        await fcm_gateway.send_push("vieux-jeton", "t", "b")


@pytest.mark.asyncio
async def test_fcm_server_error_raises_delivery_error(firebase, monkeypatch):
    mock_httpx(monkeypatch, fcm_gateway, fcm_handler([], 503, "unavailable"))
    with pytest.raises(fcm_gateway.FcmDeliveryError):
        await fcm_gateway.send_push("jeton", "t", "b")


@pytest.mark.asyncio
async def test_fcm_not_configured_and_development(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "FIREBASE_PROJECT_ID", "")
    monkeypatch.setattr(settings, "FIREBASE_SERVICE_ACCOUNT_JSON", "")
    with pytest.raises(fcm_gateway.FcmNotConfigured):
        await fcm_gateway.send_push("jeton", "t", "b")

    monkeypatch.setattr(settings, "FIREBASE_SERVICE_ACCOUNT_JSON", "pas du json {")
    monkeypatch.setattr(settings, "FIREBASE_PROJECT_ID", "p")
    with pytest.raises(fcm_gateway.FcmNotConfigured):
        await fcm_gateway.send_push("jeton", "t", "b")

    monkeypatch.setattr(settings, "ENVIRONMENT", "development")       # développement : aucun appel
    await fcm_gateway.send_push("jeton", "t", "b")


# ══ Notifications ═════════════════════════════════════════════════════════════

async def make_called_ticket(db_session, fcm_token: str | None = "fcm-token-" + "x" * 30):
    org, branch, service, counter, agent, client = await make_world(db_session)
    client.fcm_token = fcm_token
    ticket = Ticket(number="A-001", prefix="A", sequence=1, user_id=client.id, org_id=org.id, agency_id=branch.id,
                    service_id=service.id, status=TicketStatus.CALLED, counter_name="Guichet 1")
    db_session.add(ticket)
    await db_session.commit()
    return ticket, client


async def notes_of(db_session, user_id):
    return (await db_session.execute(select(Notification).where(Notification.user_id == user_id))).scalars().all()


@pytest.mark.asyncio
async def test_ticket_called_sends_sms_and_push_and_records_both(db_session):
    ticket, client = await make_called_ticket(db_session)

    await NotificationService(db_session).notify_ticket_called(ticket, "Guichet 1")

    channels = {n.channel: n for n in await notes_of(db_session, client.id)}
    assert set(channels) == {NotificationChannel.SMS, NotificationChannel.FCM}
    assert all(n.status == NotificationStatus.SENT for n in channels.values())
    assert "A-001" in channels[NotificationChannel.SMS].body


@pytest.mark.asyncio
async def test_invalid_fcm_token_is_removed_and_failure_recorded(db_session):
    ticket, client = await make_called_ticket(db_session)

    with patch.object(fcm_gateway, "send_push", new_callable=AsyncMock,
                      side_effect=fcm_gateway.FcmTokenInvalid("UNREGISTERED")):
        await NotificationService(db_session).notify_absent_warning(ticket, 60)

    await db_session.refresh(client)
    assert client.fcm_token is None, "un jeton mort est supprimé : plus d'envois inutiles"
    note = (await notes_of(db_session, client.id))[0]
    assert note.channel == NotificationChannel.FCM and note.status == NotificationStatus.FAILED


@pytest.mark.asyncio
async def test_fcm_not_configured_is_silent_and_sms_failure_is_recorded(db_session):
    ticket, client = await make_called_ticket(db_session)

    with patch.object(fcm_gateway, "send_push", new_callable=AsyncMock, side_effect=fcm_gateway.FcmNotConfigured("x")), \
         patch.object(sms_gateway, "send_sms", new_callable=AsyncMock,
                      side_effect=sms_gateway.SmsDeliveryError("passerelle KO")):
        await NotificationService(db_session).notify_ticket_called(ticket, "Guichet 1")   # ne lève pas

    notes = await notes_of(db_session, client.id)
    assert [(n.channel, n.status) for n in notes] == [(NotificationChannel.SMS, NotificationStatus.FAILED)]
    assert "passerelle KO" in notes[0].error_message


@pytest.mark.asyncio
async def test_absent_notification_is_in_app_plus_push(db_session):
    ticket, client = await make_called_ticket(db_session)

    await NotificationService(db_session).notify_ticket_absent(ticket)

    channels = {n.channel for n in await notes_of(db_session, client.id)}
    assert channels == {NotificationChannel.IN_APP, NotificationChannel.FCM}


@pytest.mark.asyncio
async def test_notifications_do_nothing_for_unknown_user_or_missing_contact(db_session):
    ticket, client = await make_called_ticket(db_session, fcm_token=None)
    client.phone = None
    await db_session.commit()

    await NotificationService(db_session).notify_ticket_called(ticket, "Guichet 1")
    await NotificationService(db_session).notify_absent_warning(ticket, 30)
    assert await notes_of(db_session, client.id) == [], "ni téléphone ni jeton : rien à envoyer"

    ticket.user_id = "utilisateur-supprime"
    await NotificationService(db_session).notify_ticket_done(ticket)    # ne lève pas


# ══ Routage temps réel ════════════════════════════════════════════════════════

def fake_ws():
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


def sent(ws) -> list[dict]:
    return [json.loads(call.args[0]) for call in ws.send_text.call_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["your_turn", "queue_update", "ticket_done", "ticket_absent",
                                        "ticket_transferred", "absent_warning", "priority_updated"])
async def test_personal_events_reach_only_the_ticket_owner(event_type):
    """Régression : seuls your_turn / ticket_called étaient routés ; les autres événements se perdaient."""
    manager = QueueWebSocketManager()
    owner, neighbour = fake_ws(), fake_ws()
    manager.connections_by_ticket = {"t-owner": owner, "t-neighbour": neighbour}

    # Événement tel qu'il arrive de Redis : JSON décodé (type = chaîne simple)
    await manager._dispatch_event({"type": event_type, "ticket_id": "t-owner", "agency_id": "a-1"})

    assert [m["type"] for m in sent(owner)] == [event_type]
    assert sent(neighbour) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["ticket_called", "broadcast_message", "counter_open", "counter_close", "counter_pause"])
async def test_room_events_reach_every_client_of_the_branch_only(event_type):
    manager = QueueWebSocketManager()
    here_1, here_2, elsewhere = fake_ws(), fake_ws(), fake_ws()
    manager.connections_by_agency = {"a-1": {here_1, here_2}, "a-2": {elsewhere}}

    await manager._dispatch_event({"type": event_type, "agency_id": "a-1", "counter_name": "G1"})

    assert len(sent(here_1)) == 1 and len(sent(here_2)) == 1 and sent(elsewhere) == []


@pytest.mark.asyncio
async def test_queue_snapshot_becomes_individual_updates_without_leaking_others():
    manager = QueueWebSocketManager()
    first, third = fake_ws(), fake_ws()
    manager.connections_by_ticket = {"t-1": first, "t-3": third}       # t-2 n'est pas connecté ici

    await manager._dispatch_event({
        "type": "queue_snapshot", "agency_id": "a-1", "service_id": "s-1",
        "positions": {"t-1": 1, "t-2": 2, "t-3": 3}, "total_in_queue": 3,
        "current_called_number": "A-007", "eta_seconds_per_position": 150,
    })

    (message_1,), (message_3,) = sent(first), sent(third)
    assert message_1["position"] == 1 and message_1["estimated_wait_minutes"] == 0
    assert message_3["position"] == 3 and message_3["estimated_wait_minutes"] == 5    # 2 devant × 150 s = 5 min
    assert message_1["current_called_number"] == "A-007" and message_1["total_in_queue"] == 3
    for message in (message_1, message_3):
        assert message["type"] == "queue_update"
        assert "positions" not in message and "t-2" not in json.dumps(message), "aucune fuite des autres tickets"


@pytest.mark.asyncio
async def test_queue_snapshot_eta_rounds_up_and_ignores_unlisted_tickets():
    manager = QueueWebSocketManager()
    listed, unlisted = fake_ws(), fake_ws()
    manager.connections_by_ticket = {"t-1": listed, "t-9": unlisted}

    await manager._dispatch_event({"type": "queue_snapshot", "positions": {"t-1": 2}, "total_in_queue": 2,
                                   "eta_seconds_per_position": 61, "agency_id": "a", "service_id": "s"})

    assert sent(listed)[0]["estimated_wait_minutes"] == 2, "61 s arrondi à 2 min"
    assert sent(unlisted) == [], "un ticket absent du snapshot ne reçoit rien"


@pytest.mark.asyncio
async def test_unknown_and_malformed_events_are_ignored_without_crashing():
    manager = QueueWebSocketManager()
    await manager._dispatch_event({"type": "inconnu"})
    await manager._dispatch_event({})
    await manager._dispatch_event({"type": "ticket_called", "agency_id": "personne-connecte"})   # salle vide


@pytest.mark.asyncio
async def test_ws_snapshot_gives_reconnecting_client_its_real_state(db_session, real_redis):
    """À la (re)connexion, le client reçoit son état réel : le WebSocket ne rejoue pas l'historique."""
    from app.schemas.ticket import CreateTicketRequest
    from app.services.ticket_service import TicketService
    org, branch, service, counter, agent, client = await make_world(db_session)
    other = await make_user(db_session)
    service_row = TicketService(db_session)
    await service_row.create_ticket(CreateTicketRequest(agency_id=branch.id, service_id=service.id), other.id, None)
    mine = await service_row.create_ticket(CreateTicketRequest(agency_id=branch.id, service_id=service.id), client.id, None)

    snapshot = await service_row.get_ws_snapshot(mine.id, client.id)
    stranger = await service_row.get_ws_snapshot(mine.id, other.id)

    assert snapshot["type"] == "queue_update" and snapshot["position"] == 2 and snapshot["total_in_queue"] == 2
    assert snapshot["ticket_number"] == "A-002" and snapshot["status"] == "waiting"
    assert snapshot["estimated_wait_minutes"] == 5
    assert stranger is None, "le snapshot d'un ticket n'est donné qu'à son propriétaire"
