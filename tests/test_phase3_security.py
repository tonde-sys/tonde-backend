"""
Tests — lot « phase 3 » de l'audit : sécurité et conformité.

  P3-9   Un ticket (et un guichet) doit pouvoir être suivi depuis plusieurs
         appareils. Avant, la 2ᵉ connexion remplaçait la 1ʳᵉ sans la fermer, et
         la déconnexion de l'une retirait l'entrée de l'autre : le client
         perdait le suivi sur un appareil, sans message.
  P3-10  `websocket_counter` ne vérifiait que l'organisation : un agent du
         guichet 1 pouvait écouter le flux du guichet 7.
  P3-17  Les connexions (réussies, échouées, bloquées) sont journalisées avec
         IP et User-Agent. `audit_logs.org_id` devient nullable pour les
         comptes sans organisation (super_admin, client OTP).

Aucun test ne nécessite Redis : la file et le compteur d'échecs sont simulés.
"""
from datetime import datetime, timezone
import uuid

import pytest
from fastapi import HTTPException, WebSocketDisconnect
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.security import hash_password
from app.models.audit_log import AuditAction, AuditLog
from app.models.branch import Branch
from app.models.counter import Counter
from app.models.employee import Employee, EmployeeStatus
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.schemas.auth import LoginEmailRequest
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.websocket.queue_ws import QueueWebSocketManager

PASSWORD = "MotDePasseTest-2026"
NOW = datetime.now(timezone.utc)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def no_redis_lockout():
    """Le verrouillage par compte vit dans Redis : neutralisé (fail-open)."""
    with patch("app.services.auth_service.get_login_block", AsyncMock(return_value=0)), \
         patch("app.services.auth_service.register_login_failure", AsyncMock(return_value=(1, 0))), \
         patch("app.services.auth_service.clear_login_failures", AsyncMock()):
        yield


def _ws() -> MagicMock:
    """Faux WebSocket : accepte, enregistre les envois, ne se ferme jamais seul."""
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    ws.client_state.name = "CONNECTED"
    return ws


# ── Décor commun ─────────────────────────────────────────────────────────────

async def _org(db_session, slug=None):
    slug = slug or f"org-{uuid.uuid4().hex[:8]}"
    org = Organization(name=f"Banque {slug}", slug=slug, sector="bank")
    db_session.add(org)
    await db_session.commit()
    await db_session.refresh(org)
    return org


async def _branch(db_session, org_id):
    branch = Branch(org_id=org_id, name="Agence Centre", slug=f"b-{uuid.uuid4().hex[:8]}",
                    sector="bank", is_active=True, is_open=True,
                    opens_at="00:00", closes_at="23:59")
    db_session.add(branch)
    await db_session.commit()
    await db_session.refresh(branch)
    return branch


async def _counter(db_session, org_id, branch_id, name="Guichet 1"):
    counter = Counter(org_id=org_id, agency_id=branch_id, name=name, is_open=True, is_active=True)
    db_session.add(counter)
    await db_session.commit()
    await db_session.refresh(counter)
    return counter


async def _user(db_session, org_id=None, role=UserRole.AGENT, email=None, password=PASSWORD,
                is_active=True, counter_id=None, branch_id=None):
    user = User(
        email=email or f"u{uuid.uuid4().hex[:8]}@ulbu.bi",
        name="Utilisateur test",
        hashed_password=hash_password(password) if password else None,
        is_active=is_active, is_verified=True, role=role, org_id=org_id,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    if counter_id or branch_id:
        db_session.add(Employee(org_id=org_id, user_id=user.id, role=role,
                                counter_id=counter_id, agency_id=branch_id,
                                status=EmployeeStatus.ACTIVE))
        await db_session.commit()
    return user


async def _audit_rows(db_session, action=None):
    from sqlalchemy import select
    query = select(AuditLog)
    if action:
        query = query.where(AuditLog.action == action)
    return (await db_session.execute(query.order_by(AuditLog.created_at))).scalars().all()


# ══ P3-9 — plusieurs appareils par ticket ════════════════════════════════════

@pytest.mark.asyncio
async def test_deux_appareils_sur_le_meme_ticket_recoivent_tous_deux():
    """Téléphone + tablette : les deux suivent le même ticket, sans exclusion."""
    manager = QueueWebSocketManager()
    phone, tablet = _ws(), _ws()

    await manager.connect_client(phone, "ticket-1", "agency-1")
    await manager.connect_client(tablet, "ticket-1", "agency-1")

    assert manager.connections_by_ticket["ticket-1"] == {phone, tablet}

    await manager.send_to_ticket("ticket-1", {"type": "your_turn"})

    phone.send_text.assert_awaited_once()
    tablet.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_deconnecter_un_appareil_garde_l_autre():
    """
    Le défaut d'origine : la déconnexion de l'un supprimait l'entrée de l'autre.

    Le client continuait de croire qu'il suivait son ticket, et ne recevait plus
    rien — sans erreur, sans message.
    """
    manager = QueueWebSocketManager()
    phone, tablet = _ws(), _ws()
    await manager.connect_client(phone, "ticket-1", "agency-1")
    await manager.connect_client(tablet, "ticket-1", "agency-1")

    manager.disconnect_client("ticket-1", "agency-1", phone)

    assert manager.connections_by_ticket["ticket-1"] == {tablet}
    await manager.send_to_ticket("ticket-1", {"type": "queue_update"})
    phone.send_text.assert_not_awaited()
    tablet.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_deconnecter_le_dernier_appareil_nettoie_le_ticket():
    """Pas de fuite mémoire : la clé disparaît avec le dernier appareil."""
    manager = QueueWebSocketManager()
    ws = _ws()
    await manager.connect_client(ws, "ticket-1", "agency-1")

    manager.disconnect_client("ticket-1", "agency-1", ws)

    assert "ticket-1" not in manager.connections_by_ticket
    assert "agency-1" not in manager.connections_by_agency


@pytest.mark.asyncio
async def test_un_appareil_mort_ne_coupe_pas_le_suivi_des_autres():
    """Un appareil en erreur est retiré SEUL : les autres continuent de recevoir."""
    manager = QueueWebSocketManager()
    dead, alive = _ws(), _ws()
    dead.send_text = AsyncMock(side_effect=Exception("connexion fermée"))
    await manager.connect_client(dead, "ticket-1", "agency-1")
    await manager.connect_client(alive, "ticket-1", "agency-1")

    await manager.send_to_ticket("ticket-1", {"type": "queue_update"})

    assert manager.connections_by_ticket["ticket-1"] == {alive}
    alive.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_deux_ecrans_au_meme_guichet():
    """Même défaut côté desktop : deux écrans sur un guichet, pas un seul."""
    manager = QueueWebSocketManager()
    screen_1, screen_2 = _ws(), _ws()
    await manager.connect_counter(screen_1, "counter-1")
    await manager.connect_counter(screen_2, "counter-1")

    await manager.send_to_counter("counter-1", {"type": "ticket_called"})
    screen_1.send_text.assert_awaited_once()
    screen_2.send_text.assert_awaited_once()

    manager.disconnect_counter("counter-1", screen_1)
    assert manager.counter_connections["counter-1"] == {screen_2}

    manager.disconnect_counter("counter-1", screen_2)
    assert "counter-1" not in manager.counter_connections


@pytest.mark.asyncio
async def test_statistiques_distinguent_tickets_et_appareils():
    """`clients_by_ticket` compte les tickets, `ticket_devices` les appareils."""
    manager = QueueWebSocketManager()
    await manager.connect_client(_ws(), "t-1", "a-1")
    await manager.connect_client(_ws(), "t-1", "a-1")
    await manager.connect_client(_ws(), "t-2", "a-1")

    stats = manager.get_stats()

    assert stats["clients_by_ticket"] == 2
    assert stats["ticket_devices"] == 3
    assert stats["total_clients"] == 3


# ══ P3-10 — affectation au guichet sur le WebSocket ══════════════════════════

async def _open_counter_ws(db_session, counter_id, user, monkeypatch):
    """
    Exécute la route `websocket_counter` comme le ferait un client desktop.

    Les dépendances sont injectées : jeton (verify_token), limiteur (Redis) et
    fabrique de session. Le WebSocket est un faux qui se déconnecte aussitôt,
    pour que la route se termine.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    import app.routers.tickets as tickets_router

    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)

    ws = _ws()
    ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect(code=1000))

    with patch.object(tickets_router, "AsyncSessionLocal", factory), \
         patch.object(tickets_router, "check_ws_rate_limit", AsyncMock(return_value=True)), \
         patch("app.core.security.verify_token", return_value=user.id):
        await tickets_router.websocket_counter(ws, counter_id, "jeton")
    return ws


@pytest.mark.asyncio
async def test_agent_non_affecte_ne_peut_pas_ecouter_un_autre_guichet(db_session, monkeypatch):
    """
    Constat P3-10 : seul l'org_id était vérifié. Un agent du guichet 1 pouvait
    s'abonner au flux du guichet 7 et voir les tickets appelés par un collègue.
    """
    org = await _org(db_session)
    branch = await _branch(db_session, org.id)
    counter_1 = await _counter(db_session, org.id, branch.id, "Guichet 1")
    counter_7 = await _counter(db_session, org.id, branch.id, "Guichet 7")
    agent = await _user(db_session, org.id, UserRole.AGENT,
                        counter_id=counter_1.id, branch_id=branch.id)

    ws = await _open_counter_ws(db_session, counter_7.id, agent, monkeypatch)

    ws.accept.assert_not_awaited()
    ws.close.assert_awaited()
    code = ws.close.await_args.kwargs.get("code") or ws.close.await_args.args[0]
    assert code == 4003
    reason = ws.close.await_args.kwargs.get("reason", "")
    assert "affecté" in reason


@pytest.mark.asyncio
async def test_agent_affecte_se_connecte_a_son_guichet(db_session, monkeypatch):
    """Cas nominal : l'agent affecté au guichet ouvre le flux sans erreur."""
    org = await _org(db_session)
    branch = await _branch(db_session, org.id)
    counter = await _counter(db_session, org.id, branch.id)
    agent = await _user(db_session, org.id, UserRole.AGENT,
                        counter_id=counter.id, branch_id=branch.id)

    ws = await _open_counter_ws(db_session, counter.id, agent, monkeypatch)

    ws.accept.assert_awaited_once()
    ws.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_superviseur_accede_a_n_importe_quel_guichet_de_son_org(db_session, monkeypatch):
    """
    Même règle que le REST (`_verify_agent_assignment`) : un superviseur n'est
    pas limité à un guichet. Le WebSocket ne doit pas être plus strict que
    `call_next`, sinon le desktop se connecte mais ne peut pas appeler.
    """
    org = await _org(db_session)
    branch = await _branch(db_session, org.id)
    counter = await _counter(db_session, org.id, branch.id)
    supervisor = await _user(db_session, org.id, UserRole.SUPERVISOR,
                             branch_id=branch.id)

    ws = await _open_counter_ws(db_session, counter.id, supervisor, monkeypatch)

    ws.accept.assert_awaited_once()
    ws.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_ne_peut_pas_ecouter_un_guichet(db_session, monkeypatch):
    """Un client n'a rien à faire sur le flux d'un guichet (rôle refusé)."""
    org = await _org(db_session)
    branch = await _branch(db_session, org.id)
    counter = await _counter(db_session, org.id, branch.id)
    client = await _user(db_session, None, UserRole.CLIENT)

    ws = await _open_counter_ws(db_session, counter.id, client, monkeypatch)

    ws.accept.assert_not_awaited()
    code = ws.close.await_args.kwargs.get("code") or ws.close.await_args.args[0]
    assert code == 4003


@pytest.mark.asyncio
async def test_guichet_d_une_autre_organisation_reste_introuvable(db_session, monkeypatch):
    """Isolation multi-tenant : le guichet d'une autre org n'est pas révélé (4004)."""
    org_a = await _org(db_session)
    org_b = await _org(db_session)
    branch_b = await _branch(db_session, org_b.id)
    counter_b = await _counter(db_session, org_b.id, branch_b.id)
    agent_a = await _user(db_session, org_a.id, UserRole.AGENT)

    ws = await _open_counter_ws(db_session, counter_b.id, agent_a, monkeypatch)

    ws.accept.assert_not_awaited()
    code = ws.close.await_args.kwargs.get("code") or ws.close.await_args.args[0]
    assert code == 4004


# ══ P3-17 — journal d'audit des connexions ═══════════════════════════════════

@pytest.mark.asyncio
async def test_connexion_reussie_est_journalisee_avec_ip_et_user_agent(db_session):
    """« Qui s'est connecté, quand, depuis où » doit être une requête SQL."""
    org = await _org(db_session)
    user = await _user(db_session, org.id, email="agent.audit@ulbu.bi")

    await AuthService(db_session).login_email(
        LoginEmailRequest(email="agent.audit@ulbu.bi", password=PASSWORD),
        ip="41.79.12.5",
        user_agent="TondeDesktop/1.0 (Windows 11)",
    )

    rows = await _audit_rows(db_session, AuditAction.LOGIN_SUCCEEDED)
    assert len(rows) == 1
    row = rows[0]
    assert row.org_id == org.id
    assert row.actor_id == user.id
    assert row.actor_role == "agent"
    assert row.extra_data["ip"] == "41.79.12.5"
    assert "TondeDesktop" in row.extra_data["user_agent"]
    assert row.extra_data["email"] == "agent.audit@ulbu.bi"


@pytest.mark.asyncio
async def test_mot_de_passe_faux_est_journalise(db_session):
    """Un échec doit laisser une trace : c'est la matière d'une détection d'attaque."""
    org = await _org(db_session)
    await _user(db_session, org.id, email="agent.audit2@ulbu.bi")

    with pytest.raises(HTTPException) as exc:
        await AuthService(db_session).login_email(
            LoginEmailRequest(email="agent.audit2@ulbu.bi", password="Mauvais-2026"),
            ip="41.79.12.6",
        )

    assert exc.value.status_code == 401
    rows = await _audit_rows(db_session, AuditAction.LOGIN_FAILED)
    assert len(rows) == 1
    assert rows[0].extra_data["reason"] == "bad_password"
    assert rows[0].extra_data["ip"] == "41.79.12.6"


@pytest.mark.asyncio
async def test_compte_inconnu_est_journalise_sans_organisation(db_session):
    """
    Cas d'usage qui a motivé la migration 009 : aucune organisation à rattacher.

    Avant, `audit_logs.org_id` était NOT NULL : cette ligne ne pouvait pas
    exister, et une tentative sur un compte inexistant ne laissait aucune trace.
    """
    with pytest.raises(HTTPException):
        await AuthService(db_session).login_email(
            LoginEmailRequest(email="inconnu@nulle-part.bi", password="PeuImporte-2026"),
            ip="41.79.12.7",
        )

    rows = await _audit_rows(db_session, AuditAction.LOGIN_FAILED)
    assert len(rows) == 1
    assert rows[0].org_id is None
    assert rows[0].actor_id is None
    assert rows[0].extra_data["reason"] == "unknown_account"


@pytest.mark.asyncio
async def test_compte_desactive_est_journalise(db_session):
    """Un compte désactivé qui tente de se connecter est un signal, pas un détail."""
    org = await _org(db_session)
    await _user(db_session, org.id, email="desactive@ulbu.bi", is_active=False)

    with pytest.raises(HTTPException) as exc:
        await AuthService(db_session).login_email(
            LoginEmailRequest(email="desactive@ulbu.bi", password=PASSWORD)
        )

    assert exc.value.status_code == 403
    rows = await _audit_rows(db_session, AuditAction.LOGIN_FAILED)
    assert rows[0].extra_data["reason"] == "account_disabled"


@pytest.mark.asyncio
async def test_compte_bloque_est_journalise(db_session):
    """Le verrouillage (429) est journalisé : c'est la preuve d'une attaque en cours."""
    org = await _org(db_session)
    await _user(db_session, org.id, email="bloque@ulbu.bi")

    with patch("app.services.auth_service.get_login_block", AsyncMock(return_value=42)):
        with pytest.raises(HTTPException) as exc:
            await AuthService(db_session).login_email(
                LoginEmailRequest(email="bloque@ulbu.bi", password=PASSWORD)
            )

    assert exc.value.status_code == 429
    rows = await _audit_rows(db_session, AuditAction.LOGIN_BLOCKED)
    assert len(rows) == 1
    assert rows[0].extra_data["reason"] == "account_locked"


@pytest.mark.asyncio
async def test_une_panne_du_journal_ne_bloque_pas_la_connexion(db_session):
    """
    Le journal est une exigence de conformité, pas une dépendance du chemin
    critique : s'il tombe, l'agent doit pouvoir se connecter et servir.
    """
    org = await _org(db_session)
    await _user(db_session, org.id, email="agent.panne@ulbu.bi")

    with patch.object(AuditService, "log", AsyncMock(side_effect=RuntimeError("table absente"))):
        response = await AuthService(db_session).login_email(
            LoginEmailRequest(email="agent.panne@ulbu.bi", password=PASSWORD)
        )

    assert response.access_token, "la connexion doit aboutir malgré la panne d'audit"
    assert await _audit_rows(db_session) == []


@pytest.mark.asyncio
async def test_search_filtre_par_organisation_et_par_action(db_session):
    """La lecture est bornée par organisation : pas de fuite inter-tenant."""
    org_a = await _org(db_session)
    org_b = await _org(db_session)
    service = AuditService(db_session)
    await service.log(org_id=org_a.id, action=AuditAction.LOGIN_SUCCEEDED, actor_id="u-1")
    await service.log(org_id=org_b.id, action=AuditAction.LOGIN_SUCCEEDED, actor_id="u-2")
    await service.log(org_id=org_a.id, action=AuditAction.TICKET_EXPORTED, actor_id="u-1")

    rows_a, total_a = await service.search(org_id=org_a.id)
    assert total_a == 2 and {r.org_id for r in rows_a} == {org_a.id}

    rows_b, _ = await service.search(org_id=org_a.id, action=AuditAction.TICKET_EXPORTED)
    assert [r.action for r in rows_b] == [AuditAction.TICKET_EXPORTED]

    rows_all, total_all = await service.search(org_id=None)
    assert total_all == 3 and len(rows_all) == 3


def test_la_migration_d_audit_est_dans_la_chaine_et_la_tete_reste_unique():
    """
    Garde-fou : une seule tête Alembic, et la migration 009 est bien dans la
    chaîne (pas seulement présente sur le disque).

    Deux têtes font échouer `alembic upgrade head` en production — donc un
    déploiement cassé au moment où on en a le moins besoin. La tête elle-même
    avance à chaque lot ; c'est l'unicité qui compte, pas son nom.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)

    heads = script.get_heads()
    assert len(heads) == 1, f"têtes multiples : {heads}"
    assert script.get_revision("009_audit_org_nullable") is not None
