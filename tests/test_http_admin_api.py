"""
Tests HTTP de bout en bout — profil, services, guichets, employés, analytics, erreurs.

Pile complète (routers + JWT + RBAC + gestionnaires d'erreurs), base de test, vrai Redis.
"""
import pytest
from sqlalchemy import select

from app.models.branch import Service
from app.models.counter import Counter
from app.models.employee import Employee
from app.models.user import User, UserRole
from tests.http_helpers import (  # noqa: F401  (fixtures importées par nom)
    api, auth, make_branch, make_counter, make_org, make_service, make_staff, make_user, make_world,
    real_redis,
)


# ══ Profil et jeton push ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_me_returns_profile_and_requires_auth(api, db_session):
    user = await make_user(db_session, name="Jean", language="fr")

    ok = await api.get("/api/v1/auth/me", headers=auth(user))
    anonymous = await api.get("/api/v1/auth/me")

    assert ok.status_code == 200
    assert ok.json()["data"]["id"] == user.id and ok.json()["data"]["name"] == "Jean"
    assert anonymous.status_code in (401, 403)


@pytest.mark.asyncio
async def test_update_profile_changes_only_given_fields_and_validates(api, db_session):
    user = await make_user(db_session, name="Jean", language="fr")

    ok = await api.patch("/api/v1/auth/me", json={"language": "rn"}, headers=auth(user))
    bad_language = await api.patch("/api/v1/auth/me", json={"language": "xx"}, headers=auth(user))
    bad_name = await api.patch("/api/v1/auth/me", json={"name": "J"}, headers=auth(user))

    assert ok.status_code == 200
    assert ok.json()["data"]["language"] == "rn" and ok.json()["data"]["name"] == "Jean"
    assert bad_language.status_code == 422 and bad_name.status_code == 422


@pytest.mark.asyncio
async def test_fcm_token_is_registered_moved_between_accounts_and_cleared(api, db_session):
    first = await make_user(db_session)
    second = await make_user(db_session)
    token = "fcm-device-token-" + "x" * 40

    assert (await api.put("/api/v1/auth/me/fcm-token", json={"fcm_token": token}, headers=auth(first))).status_code == 200
    await db_session.refresh(first)
    assert first.fcm_token == token

    # Même appareil, autre compte : le jeton quitte le premier compte (pas de notifications croisées)
    await api.put("/api/v1/auth/me/fcm-token", json={"fcm_token": token}, headers=auth(second))
    await db_session.refresh(first)
    await db_session.refresh(second)
    assert first.fcm_token is None and second.fcm_token == token

    # Déconnexion : null efface le jeton
    await api.put("/api/v1/auth/me/fcm-token", json={"fcm_token": None}, headers=auth(second))
    await db_session.refresh(second)
    assert second.fcm_token is None

    assert (await api.put("/api/v1/auth/me/fcm-token", json={"fcm_token": "court"}, headers=auth(first))).status_code == 422


# ══ Erreurs métier non masquées ═══════════════════════════════════════════════

@pytest.mark.asyncio
async def test_business_404_keeps_its_code_and_unknown_route_gets_generic_404(api, db_session):
    """Régression : le gestionnaire 404 remplaçait TOUS les codes métier par « route inexistante »."""
    user = await make_user(db_session)

    business = await api.get("/api/v1/tickets/inexistant", headers=auth(user))
    route = await api.get("/api/v1/cette/route/nexiste/pas")

    assert business.status_code == 404
    assert business.json()["detail"]["code"] == "TICKET_NOT_FOUND"
    assert route.status_code == 404
    assert route.json()["error"]["code"] == "NOT_FOUND"


# ══ Services : validation et droits ═══════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_service_validates_input_and_normalizes_prefix(api, db_session):
    org = await make_org(db_session)
    branch = await make_branch(db_session, org.id)
    admin = await make_staff(db_session, org.id, UserRole.ADMIN_ORG)
    url = f"/api/v1/organizations/{org.id}/branches/{branch.id}/services"

    ok = await api.post(url, json={"name": "Crédit", "ticket_prefix": "c"}, headers=auth(admin))
    assert ok.status_code == 201
    assert ok.json()["data"]["ticket_prefix"] == "C", "préfixe normalisé en majuscules"
    assert ok.json()["data"]["avg_duration_minutes"] == 5

    for bad in (
        {"name": ""},                                     # nom vide
        {"name": "   "},                                  # nom blanc
        {"name": "X", "ticket_prefix": "AB1"},            # préfixe non alphabétique
        {"name": "X", "ticket_prefix": "ABCDEFG"},        # préfixe trop long
        {"name": "X", "avg_duration_minutes": 0},         # durée invalide
        {"name": "X", "avg_duration_minutes": -5},
        {"name": "X" * 201},                              # nom trop long
    ):
        assert (await api.post(url, json=bad, headers=auth(admin))).status_code == 422, bad

    duplicate = await api.post(url, json={"name": "Autre", "ticket_prefix": "C"}, headers=auth(admin))
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["code"] == "PREFIX_TAKEN"


@pytest.mark.asyncio
async def test_update_service_is_partial_and_prefix_is_immutable(api, db_session):
    org = await make_org(db_session)
    branch = await make_branch(db_session, org.id)
    service = await make_service(db_session, org.id, branch.id, "A", "Dépôt")
    admin = await make_staff(db_session, org.id, UserRole.ADMIN_ORG)
    url = f"/api/v1/organizations/{org.id}/branches/{branch.id}/services/{service.id}"

    ok = await api.patch(url, json={"avg_duration_minutes": 8}, headers=auth(admin))
    prefix = await api.patch(url, json={"ticket_prefix": "Z"}, headers=auth(admin))   # champ inconnu
    mass_assign = await api.patch(url, json={"org_id": "autre-org"}, headers=auth(admin))

    assert ok.status_code == 200 and ok.json()["data"]["avg_duration_minutes"] == 8
    assert ok.json()["data"]["name"] == "Dépôt", "PATCH partiel : le nom est inchangé"
    assert prefix.status_code == 422, "le préfixe n'est pas modifiable"
    assert mass_assign.status_code == 422, "aucune affectation de masse sur org_id"


@pytest.mark.asyncio
async def test_service_writes_are_limited_to_admins_of_the_branch(api, db_session):
    org = await make_org(db_session)
    branch_a = await make_branch(db_session, org.id, "A")
    branch_b = await make_branch(db_session, org.id, "B")
    admin_a = await make_staff(db_session, org.id, UserRole.ADMIN_AGENCY, branch_a.id)
    supervisor = await make_staff(db_session, org.id, UserRole.SUPERVISOR, branch_a.id)
    body = {"name": "Crédit", "ticket_prefix": "C"}

    own = await api.post(f"/api/v1/organizations/{org.id}/branches/{branch_a.id}/services", json=body, headers=auth(admin_a))
    other_branch = await api.post(f"/api/v1/organizations/{org.id}/branches/{branch_b.id}/services", json=body, headers=auth(admin_a))
    low_role = await api.post(f"/api/v1/organizations/{org.id}/branches/{branch_a.id}/services", json=body, headers=auth(supervisor))

    assert own.status_code == 201
    assert other_branch.status_code == 403 and other_branch.json()["detail"]["code"] == "BRANCH_FORBIDDEN"
    assert low_role.status_code == 403


@pytest.mark.asyncio
async def test_public_service_list_needs_no_auth_and_hides_inactive(api, db_session):
    org = await make_org(db_session)
    branch = await make_branch(db_session, org.id)
    await make_service(db_session, org.id, branch.id, "A", "Dépôt")
    hidden = await make_service(db_session, org.id, branch.id, "B", "Fermé")
    hidden.is_active = False
    await db_session.commit()

    response = await api.get(f"/api/v1/branches/{branch.id}/services")

    assert response.status_code == 200
    assert [s["name"] for s in response.json()["data"]] == ["Dépôt"]


# ══ Guichets : routes, droits, périmètre ══════════════════════════════════════

@pytest.mark.asyncio
async def test_counter_routes_work_with_branch_path_parameter(api, db_session):
    """Régression : les routes déclaraient `agency_id` alors que le préfixe est `{branch_id}` → 422."""
    org, branch, service, counter, agent, client = await make_world(db_session)

    listing = await api.get(f"/api/v1/organizations/{org.id}/branches/{branch.id}/counters", headers=auth(agent))
    detail = await api.get(f"/api/v1/organizations/{org.id}/branches/{branch.id}/counters/{counter.id}", headers=auth(agent))

    assert listing.status_code == 200, listing.text
    assert listing.json()["data"]["total"] == 1
    assert detail.status_code == 200 and detail.json()["data"]["id"] == counter.id


@pytest.mark.asyncio
async def test_only_branch_admins_create_update_delete_counters(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    admin = await make_staff(db_session, org.id, UserRole.ADMIN_AGENCY, branch.id)
    base = f"/api/v1/organizations/{org.id}/branches/{branch.id}/counters"

    as_agent = await api.post(base, json={"name": "Guichet 9"}, headers=auth(agent))
    as_admin = await api.post(base, json={"name": "Guichet 9"}, headers=auth(admin))
    patch_agent = await api.patch(f"{base}/{counter.id}", json={"name": "Piraté"}, headers=auth(agent))
    delete_agent = await api.delete(f"{base}/{counter.id}", headers=auth(agent))
    patch_admin = await api.patch(f"{base}/{counter.id}", json={"name": "Caisse 1"}, headers=auth(admin))

    assert as_agent.status_code == 403
    assert as_admin.status_code == 201
    assert patch_agent.status_code == 403 and delete_agent.status_code == 403
    assert patch_admin.status_code == 200 and patch_admin.json()["data"]["name"] == "Caisse 1"


@pytest.mark.asyncio
async def test_agent_opens_and_closes_only_their_own_counter(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    other = await make_counter(db_session, org.id, branch.id, "Guichet 2", is_open=False)
    base = f"/api/v1/organizations/{org.id}/branches/{branch.id}/counters"

    mine_close = await api.post(f"{base}/{counter.id}/close", headers=auth(agent))
    mine_open = await api.post(f"{base}/{counter.id}/open", headers=auth(agent))
    others = await api.post(f"{base}/{other.id}/open", headers=auth(agent))

    assert mine_close.status_code == 200 and mine_close.json()["data"]["is_open"] is False
    assert mine_open.status_code == 200 and mine_open.json()["data"]["is_open"] is True
    assert others.status_code == 403 and others.json()["detail"]["code"] == "COUNTER_NOT_ASSIGNED"


@pytest.mark.asyncio
async def test_counter_of_another_branch_is_unreachable_through_url(api, db_session):
    """Changer {branch_id} dans l'URL ne permet pas d'atteindre le guichet d'une autre branch."""
    org, branch, service, counter, agent, client = await make_world(db_session)
    other_branch = await make_branch(db_session, org.id, "Annexe")
    admin = await make_staff(db_session, org.id, UserRole.ADMIN_ORG)

    response = await api.get(
        f"/api/v1/organizations/{org.id}/branches/{other_branch.id}/counters/{counter.id}", headers=auth(admin)
    )

    assert response.status_code == 404 and response.json()["detail"]["code"] == "COUNTER_NOT_FOUND"


@pytest.mark.asyncio
async def test_agent_cannot_read_counters_of_another_branch(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    other_branch = await make_branch(db_session, org.id, "Annexe")

    response = await api.get(f"/api/v1/organizations/{org.id}/branches/{other_branch.id}/counters", headers=auth(agent))

    assert response.status_code == 403 and response.json()["detail"]["code"] == "BRANCH_FORBIDDEN"


# ══ Employés : anti-escalade de privilèges ════════════════════════════════════

@pytest.mark.asyncio
async def test_admin_agency_cannot_grant_roles_above_their_own(api, db_session):
    """Faille corrigée : un admin d'agence pouvait se créer (ou créer) un super_admin."""
    org, branch, service, counter, agent, client = await make_world(db_session)
    admin = await make_staff(db_session, org.id, UserRole.ADMIN_AGENCY, branch.id)
    base = f"/api/v1/organizations/{org.id}/employees"

    for forbidden_role in ("super_admin", "admin_org", "admin_agency"):
        victim = await make_user(db_session)
        response = await api.post(base, json={"user_id": victim.id, "role": forbidden_role, "agency_id": branch.id},
                                  headers=auth(admin))
        assert response.status_code == 403, forbidden_role
        assert response.json()["detail"]["code"] == "ROLE_ESCALATION"
        await db_session.refresh(victim)
        assert victim.role == UserRole.CLIENT, "aucun effet de bord sur le compte visé"


@pytest.mark.asyncio
async def test_admin_agency_creates_agent_in_own_branch_only(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    other_branch = await make_branch(db_session, org.id, "Annexe")
    admin = await make_staff(db_session, org.id, UserRole.ADMIN_AGENCY, branch.id)
    base = f"/api/v1/organizations/{org.id}/employees"

    newcomer = await make_user(db_session)
    ok = await api.post(base, json={"user_id": newcomer.id, "role": "agent", "agency_id": branch.id,
                                    "counter_id": counter.id}, headers=auth(admin))
    elsewhere = await api.post(base, json={"user_id": (await make_user(db_session)).id, "role": "agent",
                                           "agency_id": other_branch.id}, headers=auth(admin))
    no_branch = await api.post(base, json={"user_id": (await make_user(db_session)).id, "role": "agent"},
                               headers=auth(admin))

    assert ok.status_code == 201, ok.text
    await db_session.refresh(newcomer)
    assert newcomer.role == UserRole.AGENT and newcomer.org_id == org.id
    assert elsewhere.status_code == 403 and no_branch.status_code == 403


@pytest.mark.asyncio
async def test_admin_org_can_assign_up_to_admin_org_but_not_super_admin(api, db_session):
    org = await make_org(db_session)
    admin_org = await make_staff(db_session, org.id, UserRole.ADMIN_ORG)
    base = f"/api/v1/organizations/{org.id}/employees"

    ok = await api.post(base, json={"user_id": (await make_user(db_session)).id, "role": "admin_org"}, headers=auth(admin_org))
    no = await api.post(base, json={"user_id": (await make_user(db_session)).id, "role": "super_admin"}, headers=auth(admin_org))

    assert ok.status_code == 201
    assert no.status_code == 403 and no.json()["detail"]["code"] == "ROLE_ESCALATION"


@pytest.mark.asyncio
async def test_cannot_hijack_user_of_another_organization(api, db_session):
    org_a = await make_org(db_session)
    org_b = await make_org(db_session)
    admin_b = await make_staff(db_session, org_b.id, UserRole.ADMIN_ORG)
    member_of_a = await make_staff(db_session, org_a.id, UserRole.AGENT)

    response = await api.post(f"/api/v1/organizations/{org_b.id}/employees",
                              json={"user_id": member_of_a.id, "role": "agent"}, headers=auth(admin_b))

    assert response.status_code == 409 and response.json()["detail"]["code"] == "USER_IN_OTHER_ORG"
    await db_session.refresh(member_of_a)
    assert member_of_a.org_id == org_a.id


@pytest.mark.asyncio
async def test_employee_assignment_must_stay_inside_organization_and_branch(api, db_session):
    org_a = await make_org(db_session)
    org_b = await make_org(db_session)
    branch_a = await make_branch(db_session, org_a.id, "A1")
    branch_a2 = await make_branch(db_session, org_a.id, "A2")
    branch_b = await make_branch(db_session, org_b.id, "B1")
    counter_a = await make_counter(db_session, org_a.id, branch_a.id)
    counter_b = await make_counter(db_session, org_b.id, branch_b.id)
    admin = await make_staff(db_session, org_a.id, UserRole.ADMIN_ORG)
    base = f"/api/v1/organizations/{org_a.id}/employees"

    foreign_counter = await api.post(base, json={"user_id": (await make_user(db_session)).id, "role": "agent",
                                                 "counter_id": counter_b.id}, headers=auth(admin))
    foreign_branch = await api.post(base, json={"user_id": (await make_user(db_session)).id, "role": "agent",
                                                "agency_id": branch_b.id}, headers=auth(admin))
    mismatch = await api.post(base, json={"user_id": (await make_user(db_session)).id, "role": "agent",
                                          "agency_id": branch_a2.id, "counter_id": counter_a.id}, headers=auth(admin))
    deduced = await api.post(base, json={"user_id": (await make_user(db_session)).id, "role": "agent",
                                         "counter_id": counter_a.id}, headers=auth(admin))

    assert foreign_counter.status_code == 404 and foreign_counter.json()["detail"]["code"] == "COUNTER_NOT_FOUND"
    assert foreign_branch.status_code == 404 and foreign_branch.json()["detail"]["code"] == "BRANCH_NOT_FOUND"
    assert mismatch.status_code == 400 and mismatch.json()["detail"]["code"] == "COUNTER_BRANCH_MISMATCH"
    assert deduced.status_code == 201
    assert deduced.json()["data"]["agency_id"] == branch_a.id, "l'agence est déduite du guichet"


@pytest.mark.asyncio
async def test_admin_agency_cannot_modify_or_deactivate_higher_ranked_employee(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    admin = await make_staff(db_session, org.id, UserRole.ADMIN_AGENCY, branch.id)
    boss = await make_staff(db_session, org.id, UserRole.ADMIN_AGENCY, branch.id)   # rang égal
    boss_row = (await db_session.execute(select(Employee).where(Employee.user_id == boss.id))).scalar_one()
    base = f"/api/v1/organizations/{org.id}/employees"

    demote = await api.patch(f"{base}/{boss_row.id}", json={"role": "agent"}, headers=auth(admin))
    deactivate = await api.delete(f"{base}/{boss_row.id}", headers=auth(admin))
    promote_self = await api.patch(
        f"{base}/{(await db_session.execute(select(Employee).where(Employee.user_id == agent.id))).scalar_one().id}",
        json={"role": "admin_org"}, headers=auth(admin),
    )

    assert demote.status_code == 403 and deactivate.status_code == 403
    assert promote_self.status_code == 403 and promote_self.json()["detail"]["code"] == "ROLE_ESCALATION"
    await db_session.refresh(boss)
    assert boss.role == UserRole.ADMIN_AGENCY


@pytest.mark.asyncio
async def test_employee_list_is_scoped_to_admin_agency_branches(api, db_session):
    org, branch, service, counter, agent, client = await make_world(db_session)
    other_branch = await make_branch(db_session, org.id, "Annexe")
    await make_staff(db_session, org.id, UserRole.AGENT, other_branch.id)
    admin = await make_staff(db_session, org.id, UserRole.ADMIN_AGENCY, branch.id)
    base = f"/api/v1/organizations/{org.id}/employees"

    own = await api.get(base, headers=auth(admin))
    foreign = await api.get(base, params={"agency_id": other_branch.id}, headers=auth(admin))

    assert own.status_code == 200
    assert {e["agency_id"] for e in own.json()["data"]["items"]} == {branch.id}
    assert foreign.status_code == 403


# ══ Branches et analytics : périmètre ═════════════════════════════════════════

@pytest.mark.asyncio
async def test_only_org_admins_create_or_delete_branches(api, db_session):
    org = await make_org(db_session)
    branch = await make_branch(db_session, org.id)
    admin_agency = await make_staff(db_session, org.id, UserRole.ADMIN_AGENCY, branch.id)
    admin_org = await make_staff(db_session, org.id, UserRole.ADMIN_ORG)
    body = {"name": "Nouvelle agence", "slug": "nouvelle-agence", "sector": "bank"}

    denied_create = await api.post(f"/api/v1/organizations/{org.id}/branches", json=body, headers=auth(admin_agency))
    denied_delete = await api.delete(f"/api/v1/organizations/{org.id}/branches/{branch.id}", headers=auth(admin_agency))
    created = await api.post(f"/api/v1/organizations/{org.id}/branches", json=body, headers=auth(admin_org))

    assert denied_create.status_code == 403 and denied_delete.status_code == 403
    assert created.status_code == 201, created.text


@pytest.mark.asyncio
async def test_branch_queue_config_can_be_read_and_updated(api, db_session):
    """Configuration de la file (horaires, seuil d'alerte…) : lecture et mise à jour partielle."""
    org = await make_org(db_session)
    branch = await make_branch(db_session, org.id)
    admin = await make_staff(db_session, org.id, UserRole.ADMIN_AGENCY, branch.id)
    url = f"/api/v1/organizations/{org.id}/branches/{branch.id}/config"

    before = await api.get(url, headers=auth(admin))
    patched = await api.patch(url, json={"max_wait_minutes_alert": 20, "enable_sms_reminders": True,
                                         "operating_hours": {"monday": {"open": "08:00", "close": "16:00"}}},
                              headers=auth(admin))
    invalid = await api.patch(url, json={"operating_hours": {"monday": {"open": "25:99", "close": "16:00"}}},
                              headers=auth(admin))
    not_text = await api.patch(url, json={"operating_hours": {"monday": {"open": 8, "close": "16:00"}}},
                               headers=auth(admin))
    reversed_hours = await api.patch(url, json={"operating_hours": {"monday": {"open": "17:00", "close": "08:00"}}},
                                     headers=auth(admin))

    assert before.status_code == 200, before.text
    assert before.json()["data"]["max_wait_minutes_alert"] == 30
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["max_wait_minutes_alert"] == 20
    assert patched.json()["data"]["enable_sms_reminders"] is True
    assert patched.json()["data"]["avg_service_minutes"] == 5, "PATCH partiel : le reste est inchangé"
    assert invalid.status_code == 422, "25:99 n'est pas une heure"
    assert not_text.status_code == 422, "une valeur non textuelle donne un 422, pas un 500"
    assert reversed_hours.status_code == 422, "l'ouverture doit précéder la fermeture"


@pytest.mark.asyncio
async def test_admin_agency_updates_only_their_branch(api, db_session):
    org = await make_org(db_session)
    mine = await make_branch(db_session, org.id, "Mine")
    theirs = await make_branch(db_session, org.id, "Autre")
    admin = await make_staff(db_session, org.id, UserRole.ADMIN_AGENCY, mine.id)

    ok = await api.patch(f"/api/v1/organizations/{org.id}/branches/{mine.id}", json={"is_open": False}, headers=auth(admin))
    no = await api.patch(f"/api/v1/organizations/{org.id}/branches/{theirs.id}", json={"is_open": False}, headers=auth(admin))
    cfg = await api.get(f"/api/v1/organizations/{org.id}/branches/{theirs.id}/config", headers=auth(admin))

    assert ok.status_code == 200
    assert no.status_code == 403 and cfg.status_code == 403


@pytest.mark.asyncio
async def test_analytics_are_limited_to_the_supervisors_branch(api, db_session):
    """Faille corrigée : un superviseur de la branch A lisait les stats de la branch B."""
    org = await make_org(db_session)
    branch_a = await make_branch(db_session, org.id, "A")
    branch_b = await make_branch(db_session, org.id, "B")
    supervisor = await make_staff(db_session, org.id, UserRole.SUPERVISOR, branch_a.id)
    admin_org = await make_staff(db_session, org.id, UserRole.ADMIN_ORG)

    def url(branch, path="summary", prefix="branches"):
        return f"/api/v1/organizations/{org.id}/{prefix}/{branch.id}/analytics/{path}"

    own = await api.get(url(branch_a), headers=auth(supervisor))
    other = await api.get(url(branch_b), headers=auth(supervisor))
    legacy = await api.get(url(branch_a, prefix="agencies"), headers=auth(supervisor))
    as_admin = await api.get(url(branch_b), headers=auth(admin_org))
    agent_denied = await api.get(url(branch_a), headers=auth(await make_staff(db_session, org.id, UserRole.AGENT, branch_a.id)))

    assert own.status_code == 200 and legacy.status_code == 200
    assert other.status_code == 403 and other.json()["detail"]["code"] == "BRANCH_FORBIDDEN"
    assert as_admin.status_code == 200
    assert agent_denied.status_code == 403, "les agents ne voient pas les statistiques"
