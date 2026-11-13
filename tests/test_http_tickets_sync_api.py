"""
Tests HTTP de bout en bout — tickets, QR public, écran de salle, synchronisation hors-ligne,
notifications.

Pile complète (routers + JWT + RBAC), base de test, vrai Redis.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.notification import Notification, NotificationChannel, NotificationStatus
from app.models.ticket import Ticket
from app.models.user import UserRole
from tests.http_helpers import (  # noqa: F401  (fixtures importées par nom)
    api, auth, make_branch, make_counter, make_org, make_service, make_staff, make_user, make_world,
    real_redis,
)


def create_body(branch, service, **extra):
    return {"agency_id": branch.id, "service_id": service.id, **extra}


# ══ Parcours client complet (client OTP sans organisation) ════════════════════

@pytest.mark.asyncio
async def test_client_full_journey_over_http(api, db_session):
    """Prendre un ticket → le consulter → être appelé → servi → terminé, comme le mobile."""
    org, branch, service, counter, agent, client = await make_world(db_session)

    created = await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(client))
    assert created.status_code == 200, created.text
    ticket_id = created.json()["id"]
    assert created.json()["number"] == "A-001" and created.json()["position"] == 1

    # Régression 404 : un client sans organisation peut consulter son ticket
    detail = await api.get(f"/api/v1/tickets/{ticket_id}", headers=auth(client))
    assert detail.status_code == 200 and detail.json()["status"] == "waiting"

    called = await api.post("/api/v1/tickets/counter/call-next", headers=auth(agent), json={
        "agency_id": branch.id, "service_id": service.id, "counter_id": counter.id,
    })
    assert called.status_code == 200 and called.json()["ticket_number"] == "A-001"
    assert called.json()["counter_name"] == "Guichet 1"

    assert (await api.post(f"/api/v1/tickets/{ticket_id}/serving", headers=auth(agent))).status_code == 200
    assert (await api.post(f"/api/v1/tickets/{ticket_id}/done", headers=auth(agent))).status_code == 200

    history = await api.get("/api/v1/tickets/history", headers=auth(client))
    assert history.json()["items"][0]["status"] == "done"
    inbox = await api.get("/api/v1/notifications", headers=auth(client))
    assert inbox.json()["data"]["unread"] == 1, "notification « service terminé » dans la boîte"


@pytest.mark.asyncio
async def test_client_can_cancel_and_second_ticket_is_blocked_while_active(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    first = await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(client))
    blocked = await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(client))

    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "TICKET_ALREADY_ACTIVE"
    assert blocked.json()["detail"]["active_ticket_id"] == first.json()["id"]

    cancel = await api.delete(f"/api/v1/tickets/{first.json()['id']}", headers=auth(client))
    again = await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(client))
    assert cancel.status_code == 200 and again.status_code == 200


@pytest.mark.asyncio
async def test_client_cannot_see_or_cancel_another_clients_ticket(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    other = await make_user(db_session)
    ticket = await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(client))
    ticket_id = ticket.json()["id"]

    assert (await api.get(f"/api/v1/tickets/{ticket_id}", headers=auth(other))).status_code == 404
    assert (await api.delete(f"/api/v1/tickets/{ticket_id}", headers=auth(other))).status_code == 404
    assert (await api.get(f"/api/v1/tickets/{ticket_id}", headers=auth(client))).status_code == 200


@pytest.mark.asyncio
async def test_priority_request_flow_over_http(api, db_session):
    """Le client demande vip ; l'agent confirme via /priority ; le client est appelé en premier."""
    org, branch, service, counter, agent, client = await make_world(db_session)
    other = await make_user(db_session)
    await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(other))
    vip = await api.post("/api/v1/tickets", json=create_body(branch, service, priority="vip"), headers=auth(client))
    assert vip.json()["priority"] == "standard" and vip.json()["requested_priority"] == "vip"

    client_attempt = await api.post(f"/api/v1/tickets/{vip.json()['id']}/priority", json={"approve": True},
                                    headers=auth(client))
    assert client_attempt.status_code == 403, "un client ne peut pas confirmer sa propre priorité"

    approved = await api.post(f"/api/v1/tickets/{vip.json()['id']}/priority", json={"approve": True}, headers=auth(agent))
    assert approved.status_code == 200 and approved.json()["position"] == 1

    refreshed = await api.get(f"/api/v1/tickets/{vip.json()['id']}", headers=auth(client))
    assert refreshed.json()["priority"] == "vip" and refreshed.json()["requested_priority"] is None
    called = await api.post("/api/v1/tickets/counter/call-next", headers=auth(agent), json={
        "agency_id": branch.id, "service_id": service.id, "counter_id": counter.id,
    })
    assert called.json()["ticket_id"] == vip.json()["id"]


@pytest.mark.asyncio
async def test_transfer_incomplete_requeue_and_close_over_http(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    credit = await make_service(db_session, org.id, branch.id, "B", "Crédit")
    ticket = await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(client))
    tid = ticket.json()["id"]
    await api.post("/api/v1/tickets/counter/call-next", headers=auth(agent), json={
        "agency_id": branch.id, "service_id": service.id, "counter_id": counter.id})

    transfer = await api.post(f"/api/v1/tickets/{tid}/transfer", json={"target_service_id": credit.id, "reason": "crédit"},
                              headers=auth(agent))
    assert transfer.status_code == 200 and transfer.json()["ticket_number"] == "B-001"
    assert (await api.get(f"/api/v1/tickets/{tid}", headers=auth(client))).json()["number"] == "B-001"

    # Le client est rappelé au service cible, puis le service est interrompu
    await api.post("/api/v1/tickets/counter/call-next", headers=auth(agent), json={
        "agency_id": branch.id, "service_id": credit.id, "counter_id": counter.id})
    await api.post(f"/api/v1/tickets/{tid}/serving", headers=auth(agent))
    incomplete = await api.post(f"/api/v1/tickets/{tid}/incomplete", json={"reason": "panne"}, headers=auth(agent))
    assert incomplete.status_code == 200

    requeue = await api.post(f"/api/v1/tickets/{tid}/requeue", headers=auth(agent))
    assert requeue.status_code == 200 and requeue.json()["position"] == 1
    close = await api.post(f"/api/v1/tickets/{tid}/close", json={"reason": "abandon"}, headers=auth(agent))
    assert close.status_code == 200
    assert (await api.get(f"/api/v1/tickets/{tid}", headers=auth(client))).json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_staff_endpoints_reject_clients_and_unassigned_agents(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    other_counter = await make_counter(db_session, org.id, branch.id, "Guichet 2")
    intruder = await make_staff(db_session, org.id, UserRole.AGENT, branch.id, other_counter.id)
    ticket = await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(client))
    tid = ticket.json()["id"]
    body = {"agency_id": branch.id, "service_id": service.id, "counter_id": counter.id}

    assert (await api.post("/api/v1/tickets/counter/call-next", json=body, headers=auth(client))).status_code == 403
    denied = await api.post("/api/v1/tickets/counter/call-next", json=body, headers=auth(intruder))
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "COUNTER_NOT_ASSIGNED"

    await api.post("/api/v1/tickets/counter/call-next", json=body, headers=auth(agent))
    stolen = await api.post(f"/api/v1/tickets/{tid}/serving", headers=auth(intruder))
    assert stolen.status_code == 403 and stolen.json()["detail"]["code"] == "TICKET_NOT_YOURS"


@pytest.mark.asyncio
async def test_client_return_to_queue_from_absent_over_http(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    ticket = await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(client))
    tid = ticket.json()["id"]
    await api.post("/api/v1/tickets/counter/call-next", headers=auth(agent), json={
        "agency_id": branch.id, "service_id": service.id, "counter_id": counter.id})
    await api.post(f"/api/v1/tickets/{tid}/absent", headers=auth(agent))

    back = await api.post(f"/api/v1/tickets/{tid}/return", headers=auth(client))

    assert back.status_code == 200
    assert (await api.get(f"/api/v1/tickets/{tid}", headers=auth(client))).json()["status"] == "waiting"


# ══ QR code public ════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_qr_scan_is_public_and_exposes_no_personal_data(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    created = await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(client))

    response = await api.get(f"/api/v1/tickets/qr/{created.json()['qr_token']}")   # aucun jeton

    assert response.status_code == 200
    data = response.json()
    assert data["number"] == "A-001" and data["status"] == "waiting" and data["position"] == 1
    assert data["service_name"] == "Dépôt" and data["branch_name"] == "Agence Centre"
    for secret in ("ticket_id", "user_id", "org_id", "qr_token", "phone"):
        assert secret not in data
    assert client.phone not in response.text


@pytest.mark.asyncio
async def test_qr_scan_gives_staff_the_ticket_id_only_in_their_organization(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    stranger = await make_staff(db_session, (await make_org(db_session)).id, UserRole.AGENT)
    created = await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(client))
    url = f"/api/v1/tickets/qr/{created.json()['qr_token']}"

    as_agent = (await api.get(url, headers=auth(agent))).json()
    as_client = (await api.get(url, headers=auth(client))).json()
    as_stranger = (await api.get(url, headers=auth(stranger))).json()

    assert as_agent["ticket_id"] == created.json()["id"] and "service_id" in as_agent
    assert "ticket_id" not in as_client and "ticket_id" not in as_stranger


@pytest.mark.asyncio
async def test_qr_scan_unknown_expired_and_bad_token(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    created = await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(client))
    token = created.json()["qr_token"]

    assert (await api.get("/api/v1/tickets/qr/inconnu")).status_code == 404

    row = (await db_session.execute(select(Ticket).where(Ticket.qr_token == token))).scalar_one()
    row.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
    await db_session.commit()
    expired = await api.get(f"/api/v1/tickets/qr/{token}")
    assert expired.status_code == 404 and expired.json()["detail"]["code"] == "TICKET_NOT_FOUND"

    bad_auth = await api.get(f"/api/v1/tickets/qr/{token}", headers={"Authorization": "Bearer faux.jeton.ici"})
    assert bad_auth.status_code == 401, "un jeton fourni mais invalide n'est pas traité comme anonyme"


# ══ Écran de salle d'attente ══════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_branch_display_shows_counters_and_waiting_counts(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    second_client = await make_user(db_session)
    await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(client))
    await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(second_client))
    await api.post("/api/v1/tickets/counter/call-next", headers=auth(agent), json={
        "agency_id": branch.id, "service_id": service.id, "counter_id": counter.id})

    response = await api.get(f"/api/v1/branches/{branch.id}/display")   # public

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["branch"]["is_open"] is True
    assert data["counters"] == [{"id": counter.id, "name": "Guichet 1", "is_open": True, "now_serving": "A-001"}]
    assert data["services"][0]["waiting"] == 1
    assert data["services"][0]["last_called_number"] == "A-001"
    assert data["services"][0]["estimated_wait_minutes"] == 5, "un nouvel arrivant aurait 1 personne devant"
    assert "phone" not in response.text and "user" not in response.text


@pytest.mark.asyncio
async def test_branch_display_unknown_branch_is_404(api, db_session):
    assert (await api.get("/api/v1/branches/inconnue/display")).status_code == 404


# ══ Synchronisation hors-ligne ════════════════════════════════════════════════

def action(action_id: str, type_: str, **payload):
    return {"client_action_id": action_id, "type": type_, "payload": payload}


@pytest.mark.asyncio
async def test_sync_create_is_idempotent_across_replays(api, db_session):
    """Même action rejouée (coupure réseau) : applied une fois, puis already_applied, jamais de doublon."""
    org, branch, service, counter, agent, client = await make_world(db_session)
    body = {"actions": [action("offline-create-001", "create_ticket", agency_id=branch.id, service_id=service.id)]}

    first = await api.post("/api/v1/sync/actions", json=body, headers=auth(client))
    replay = await api.post("/api/v1/sync/actions", json=body, headers=auth(client))

    assert first.status_code == 200, first.text
    assert first.json()["results"][0]["status"] == "applied"
    assert replay.json()["results"][0]["status"] == "already_applied"
    assert first.json()["results"][0]["ticket"]["id"] == replay.json()["results"][0]["ticket"]["id"]
    tickets = (await db_session.execute(select(Ticket).where(Ticket.user_id == client.id))).scalars().all()
    assert len(tickets) == 1


@pytest.mark.asyncio
async def test_sync_cancel_and_return_are_idempotent(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    ticket = await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(client))
    tid = ticket.json()["id"]
    cancel = {"actions": [action("offline-cancel-01", "cancel_ticket", ticket_id=tid)]}

    first = await api.post("/api/v1/sync/actions", json=cancel, headers=auth(client))
    second = await api.post("/api/v1/sync/actions", json=cancel, headers=auth(client))

    assert first.json()["results"][0]["status"] == "applied"
    assert second.json()["results"][0]["status"] == "already_applied"
    assert second.json()["results"][0]["ticket"]["status"] == "cancelled"

    # return_to_queue sur un ticket déjà en file → already_applied
    waiting = await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(client))
    back = await api.post("/api/v1/sync/actions", headers=auth(client), json={
        "actions": [action("offline-return-01", "return_to_queue", ticket_id=waiting.json()["id"])]})
    assert back.json()["results"][0]["status"] == "already_applied"


@pytest.mark.asyncio
async def test_sync_reports_conflict_with_server_state(api, db_session):
    """Conflits : le serveur fait foi et renvoie l'état réel du ticket."""
    org, branch, service, counter, agent, client = await make_world(db_session)
    ticket = await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(client))
    tid = ticket.json()["id"]
    await api.post("/api/v1/tickets/counter/call-next", headers=auth(agent), json={
        "agency_id": branch.id, "service_id": service.id, "counter_id": counter.id})

    response = await api.post("/api/v1/sync/actions", headers=auth(client), json={"actions": [
        action("offline-cancel-02", "cancel_ticket", ticket_id=tid),                        # déjà appelé
        action("offline-create-02", "create_ticket", agency_id=branch.id, service_id=service.id),  # déjà un ticket actif
    ]})

    cancel_result, create_result = response.json()["results"]
    assert cancel_result["status"] == "conflict" and cancel_result["code"] == "INVALID_TRANSITION"
    assert cancel_result["ticket"]["status"] == "called", "le mobile s'aligne sur l'état serveur"
    assert create_result["status"] == "conflict" and create_result["code"] == "TICKET_ALREADY_ACTIVE"
    assert create_result["ticket"]["id"] == tid


@pytest.mark.asyncio
async def test_sync_rejects_invalid_old_and_foreign_actions_without_stopping_others(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    other = await make_user(db_session)
    foreign = await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(other))
    old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()

    response = await api.post("/api/v1/sync/actions", headers=auth(client), json={"actions": [
        {**action("offline-old-0001", "create_ticket", agency_id=branch.id, service_id=service.id), "client_created_at": old},
        action("offline-bad-0001", "create_ticket", agency_id=branch.id),                          # service_id manquant
        action("offline-foreign-1", "cancel_ticket", ticket_id=foreign.json()["id"]),               # ticket d'un autre
        action("offline-good-001", "create_ticket", agency_id=branch.id, service_id=service.id),   # valide
    ]})

    old_r, bad_r, foreign_r, good_r = response.json()["results"]
    assert old_r["status"] == "rejected" and old_r["code"] == "ACTION_TOO_OLD"
    assert bad_r["status"] == "rejected" and bad_r["code"] == "INVALID_PAYLOAD"
    assert foreign_r["status"] == "rejected" and foreign_r["code"] == "TICKET_NOT_FOUND"
    assert good_r["status"] == "applied", "une action en erreur n'empêche pas les suivantes"
    still_active = await api.get(f"/api/v1/tickets/{foreign.json()['id']}", headers=auth(other))
    assert still_active.json()["status"] == "waiting", "le ticket d'un autre n'a pas été touché"


@pytest.mark.asyncio
async def test_sync_actions_request_validation(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    headers = auth(client)

    assert (await api.post("/api/v1/sync/actions", json={"actions": []}, headers=headers)).status_code == 422
    duplicate = {"actions": [action("same-id-0001", "cancel_ticket", ticket_id="x")] * 2}
    assert (await api.post("/api/v1/sync/actions", json=duplicate, headers=headers)).status_code == 422
    bad_type = {"actions": [{"client_action_id": "abcdefgh1234", "type": "delete_everything", "payload": {}}]}
    assert (await api.post("/api/v1/sync/actions", json=bad_type, headers=headers)).status_code == 422
    too_many = {"actions": [action(f"offline-many-{i:03d}", "cancel_ticket", ticket_id="x") for i in range(51)]}
    assert (await api.post("/api/v1/sync/actions", json=too_many, headers=headers)).status_code == 422
    assert (await api.post("/api/v1/sync/actions", json={"actions": [action("abcdefgh1234", "cancel_ticket")]})).status_code in (401, 403)


@pytest.mark.asyncio
async def test_sync_state_returns_active_ticket_recent_and_unread(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    ticket = await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(client))

    state = await api.get("/api/v1/sync/state", headers=auth(client))

    data = state.json()["data"]
    assert state.status_code == 200
    assert data["active_ticket"]["id"] == ticket.json()["id"]
    assert data["active_ticket"]["position"] == 1
    assert [t["id"] for t in data["recent_tickets"]] == [ticket.json()["id"]]
    assert data["unread_notifications"] == 0 and data["server_time"]


@pytest.mark.asyncio
async def test_sync_changes_lists_transitions_since_a_date(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    since = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    ticket = await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(client))
    await api.post("/api/v1/tickets/counter/call-next", headers=auth(agent), json={
        "agency_id": branch.id, "service_id": service.id, "counter_id": counter.id})

    response = await api.get("/api/v1/sync/changes", params={"since": since}, headers=auth(client))

    data = response.json()["data"]
    assert response.status_code == 200
    assert [c["action"] for c in data["changes"]] == ["ticket_created", "ticket_called"]
    assert data["tickets"][0]["id"] == ticket.json()["id"] and data["tickets"][0]["status"] == "called"
    assert data["has_more"] is False

    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    nothing = await api.get("/api/v1/sync/changes", params={"since": future}, headers=auth(client))
    assert nothing.json()["data"]["changes"] == []


@pytest.mark.asyncio
async def test_sync_changes_only_returns_my_tickets_and_enforces_window(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    other = await make_user(db_session)
    await api.post("/api/v1/tickets", json=create_body(branch, service), headers=auth(other))
    recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

    mine = await api.get("/api/v1/sync/changes", params={"since": recent}, headers=auth(client))
    too_old = await api.get("/api/v1/sync/changes",
                            params={"since": (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()},
                            headers=auth(client))

    assert mine.json()["data"]["changes"] == [], "les changements des autres clients ne fuitent pas"
    assert too_old.status_code == 400 and too_old.json()["detail"]["code"] == "SINCE_TOO_OLD"


# ══ Notifications in-app ══════════════════════════════════════════════════════

async def _add_notification(db, org_id, user_id, title, *, read=False, channel=NotificationChannel.IN_APP):
    note = Notification(org_id=org_id, user_id=user_id, channel=channel, title=title, body=f"{title} — corps",
                        status=NotificationStatus.SENT, is_read=read)
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note


@pytest.mark.asyncio
async def test_notification_inbox_lists_marks_read_and_isolates_users(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    other = await make_user(db_session)
    first = await _add_notification(db_session, org.id, client.id, "Première")
    await _add_notification(db_session, org.id, client.id, "Seconde")
    await _add_notification(db_session, org.id, client.id, "SMS ignoré", channel=NotificationChannel.SMS)
    theirs = await _add_notification(db_session, org.id, other.id, "Privée")

    inbox = await api.get("/api/v1/notifications", headers=auth(client))
    assert inbox.json()["data"]["total"] == 2 and inbox.json()["data"]["unread"] == 2, "les SMS ne sont pas dans la boîte"

    assert (await api.post(f"/api/v1/notifications/{first.id}/read", headers=auth(client))).status_code == 200
    unread = await api.get("/api/v1/notifications", params={"unread_only": True}, headers=auth(client))
    assert [n["title"] for n in unread.json()["data"]["items"]] == ["Seconde"]

    stolen = await api.post(f"/api/v1/notifications/{theirs.id}/read", headers=auth(client))
    assert stolen.status_code == 404, "on ne marque pas la notification de quelqu'un d'autre"

    all_read = await api.post("/api/v1/notifications/read-all", headers=auth(client))
    assert all_read.json()["data"]["updated"] == 1
    assert (await api.get("/api/v1/notifications", headers=auth(client))).json()["data"]["unread"] == 0
    other_inbox = await api.get("/api/v1/notifications", headers=auth(other))
    assert other_inbox.json()["data"]["unread"] == 1, "la boîte de l'autre utilisateur est intacte"
