"""
Contrat du mobile — lot 015.

Le mobile ne pouvait pas fonctionner contre ce backend, non par manque de
volonté mais parce que trois besoins élémentaires n'avaient aucune route :

  1. « Quel est mon ticket actif ? » — le mobile devait le deviner à partir d'un
     identifiant gardé localement (perdu à la réinstallation) ou de `/history`
     (pagination). D'où `GET /tickets/active`.
  2. « Quelles organisations puis-je rejoindre ? » — `/organizations` répond 403
     aux clients. D'où `GET /public/organizations`.
  3. « Quelles agences a cette organisation ? » — `/{org_id}/branches` exige un
     rôle administrateur. D'où `GET /public/organizations/{org_id}/branches`.

Ce fichier vérifie aussi ce que ces routes ne disent PAS : une route publique
qui laisse fuir un e-mail, un téléphone ou un code d'invitation est une fuite,
pas une commodité.
"""
import pytest
from sqlalchemy import select

from app.models.branch import Branch
from app.models.organization import Organization
from app.models.ticket import Ticket, TicketStatus
from tests.http_helpers import (  # noqa: F401  (fixtures importées par nom)
    api, auth, make_branch, make_org, make_service, make_user, make_world, real_redis,
)

PUBLIC_ORGANIZATION_FIELDS = {"id", "name", "sector", "city", "logo_url", "support"}
# Bloc ajouté avec le centre d'aide (L6) : l'institution publie elle-même ces
# coordonnées pour être joignable. Le contrat doit rester fermé — une clé en
# plus ici serait une fuite silencieuse.
PUBLIC_SUPPORT_FIELDS = {"phone", "whatsapp", "email", "hours"}
PUBLIC_BRANCH_FIELDS = {
    "id", "name", "address", "city", "sector", "latitude", "longitude",
    "opens_at", "closes_at", "is_open", "waiting_count",
}


# ══ GET /tickets/active ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ticket_actif_absent_renvoie_null_et_non_une_erreur(api, db_session):
    """
    Aucun ticket est un état NORMAL.

    Un 404 forcerait le mobile à traiter « pas de ticket » comme une panne,
    c'est-à-dire à afficher un écran d'erreur au premier lancement de chaque
    utilisateur.
    """
    client = await make_user(db_session)

    response = await api.get("/api/v1/tickets/active", headers=auth(client))

    assert response.status_code == 200, response.text
    assert response.json() == {"success": True, "data": None}


@pytest.mark.asyncio
async def test_ticket_actif_renvoye_avec_position_et_attente(api, db_session):
    """Après une prise de ticket, la route renvoie le ticket complet."""
    org, branch, service, counter, agent, client = await make_world(db_session)

    created = await api.post(
        "/api/v1/tickets",
        json={"agency_id": branch.id, "service_id": service.id},
        headers=auth(client),
    )
    assert created.status_code == 200, created.text

    active = await api.get("/api/v1/tickets/active", headers=auth(client))

    assert active.status_code == 200, active.text
    payload = active.json()
    assert payload["success"] is True
    assert payload["data"]["id"] == created.json()["id"]
    assert payload["data"]["number"] == "A-001"
    assert payload["data"]["position"] == 1
    assert payload["data"]["status"] == "waiting"


@pytest.mark.asyncio
async def test_ticket_actif_disparait_apres_cloture(api, db_session):
    """
    Le ticket clôturé n'est plus « actif » : le mobile doit pouvoir en reprendre
    un. La liste de statuts vient de `ACTIVE_STATUSES`, la même que l'invariant
    de la base — deux définitions divergentes produiraient un mobile qui propose
    une action que le serveur refuse ensuite en 409.
    """
    org, branch, service, counter, agent, client = await make_world(db_session)
    created = await api.post(
        "/api/v1/tickets",
        json={"agency_id": branch.id, "service_id": service.id},
        headers=auth(client),
    )
    ticket_id = created.json()["id"]

    assert (await api.post("/api/v1/tickets/counter/call-next", headers=auth(agent), json={
        "agency_id": branch.id, "service_id": service.id, "counter_id": counter.id,
    })).status_code == 200
    assert (await api.post(f"/api/v1/tickets/{ticket_id}/serving", headers=auth(agent))).status_code == 200
    assert (await api.post(f"/api/v1/tickets/{ticket_id}/done", headers=auth(agent))).status_code == 200

    active = await api.get("/api/v1/tickets/active", headers=auth(client))
    assert active.status_code == 200
    assert active.json()["data"] is None

    # Le ticket reste consultable par son identifiant : « plus actif » ne veut
    # pas dire « effacé ».
    detail = await api.get(f"/api/v1/tickets/{ticket_id}", headers=auth(client))
    assert detail.status_code == 200 and detail.json()["status"] == "done"


@pytest.mark.asyncio
async def test_ticket_actif_exige_un_jeton(api):
    """La route ne dit jamais à un anonyme s'il a un ticket."""
    response = await api.get("/api/v1/tickets/active")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_ticket_actif_ne_masque_pas_le_detail(api, db_session):
    """
    `/active` est déclarée avant `/{ticket_id}`.

    FastAPI résout dans l'ordre de déclaration : si la route littérale passait
    après, `GET /tickets/active` chercherait un ticket dont l'identifiant est
    « active » et répondrait 404 — ou pire, renverrait un ticket réel si un
    identifiant de ce genre existait un jour. Ce test verrouille les deux sens.
    """
    org, branch, service, counter, agent, client = await make_world(db_session)
    created = await api.post(
        "/api/v1/tickets",
        json={"agency_id": branch.id, "service_id": service.id},
        headers=auth(client),
    )
    ticket_id = created.json()["id"]

    detail = await api.get(f"/api/v1/tickets/{ticket_id}", headers=auth(client))
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == ticket_id

    active = await api.get("/api/v1/tickets/active", headers=auth(client))
    assert active.status_code == 200
    assert active.json()["data"]["id"] == ticket_id


# ══ Découverte publique ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_organisations_publiques_sans_jeton(api, db_session):
    """
    Un client non connecté doit pouvoir lister les organisations : c'est le
    premier écran de l'application, avant même la connexion.
    """
    org = await make_org(db_session)

    response = await api.get("/api/v1/public/organizations")

    assert response.status_code == 200, response.text
    items = response.json()["data"]
    assert any(item["id"] == org.id for item in items)


@pytest.mark.asyncio
async def test_organisations_publiques_masquent_les_desactivees(api, db_session):
    """Une organisation désactivée disparaît de la découverte."""
    active = await make_org(db_session)
    inactive = await make_org(db_session)
    inactive.is_active = False
    await db_session.commit()

    items = (await api.get("/api/v1/public/organizations")).json()["data"]
    ids = {item["id"] for item in items}

    assert active.id in ids
    assert inactive.id not in ids


@pytest.mark.asyncio
async def test_organisations_publiques_n_exposent_aucun_champ_sensible(api, db_session):
    """
    Le contrat est volontairement pauvre : ni e-mail, ni téléphone, ni code
    d'invitation. Un code d'invitation exposé publiquement permettrait de
    rejoindre n'importe quelle organisation.
    """
    await make_org(db_session)

    items = (await api.get("/api/v1/public/organizations")).json()["data"]
    assert items, "au moins une organisation pour vérifier la forme"
    for item in items:
        assert set(item) == PUBLIC_ORGANIZATION_FIELDS
        assert set(item["support"]) == PUBLIC_SUPPORT_FIELDS


@pytest.mark.asyncio
async def test_agences_publiques_avec_le_monde_en_attente(api, db_session):
    """
    L'écran « choisir une agence » affiche le nombre de personnes en attente.
    Le compte est un agrégat : aucun numéro de ticket, aucune position ne sort.
    """
    org, branch, service, counter, agent, client = await make_world(db_session)
    await api.post(
        "/api/v1/tickets",
        json={"agency_id": branch.id, "service_id": service.id},
        headers=auth(client),
    )

    response = await api.get(f"/api/v1/public/organizations/{org.id}/branches")

    assert response.status_code == 200, response.text
    items = response.json()["data"]
    assert len(items) == 1
    assert items[0]["id"] == branch.id
    assert items[0]["waiting_count"] == 1
    assert set(items[0]) == PUBLIC_BRANCH_FIELDS


@pytest.mark.asyncio
async def test_agences_publiques_masquent_les_inactives(api, db_session):
    org = await make_org(db_session)
    open_branch = await make_branch(db_session, org.id, name="Agence Ouverte")
    closed_branch = await make_branch(db_session, org.id, name="Agence Fermée")
    closed_branch.is_active = False
    await db_session.commit()

    items = (await api.get(f"/api/v1/public/organizations/{org.id}/branches")).json()["data"]
    ids = {item["id"] for item in items}

    assert open_branch.id in ids
    assert closed_branch.id not in ids


@pytest.mark.asyncio
async def test_agences_publiques_organisation_inconnue(api):
    """Un identifiant inconnu est un 404 explicite, pas une liste vide muette."""
    response = await api.get("/api/v1/public/organizations/inconnue/branches")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "ORGANIZATION_NOT_FOUND"


@pytest.mark.asyncio
async def test_agences_publiques_ne_fuient_aucune_donnee_de_ticket(api, db_session):
    """
    Régression : le compte d'attente est calculé en base. Une jointure mal
    écrite aurait pu renvoyer des lignes de tickets au lieu d'un entier.
    """
    org, branch, service, counter, agent, client = await make_world(db_session)
    await api.post(
        "/api/v1/tickets",
        json={"agency_id": branch.id, "service_id": service.id},
        headers=auth(client),
    )

    items = (await api.get(f"/api/v1/public/organizations/{org.id}/branches")).json()["data"]
    assert isinstance(items[0]["waiting_count"], int)


@pytest.mark.asyncio
async def test_la_decouverte_publique_ne_cree_rien(api, db_session):
    """
    Les routes publiques sont en lecture seule. Ce test échouerait si un jour
    une route « pratique » créait un ticket au passage.
    """
    org = await make_org(db_session)
    branch = await make_branch(db_session, org.id)
    await make_service(db_session, org.id, branch.id)

    before = len((await db_session.execute(select(Ticket))).scalars().all())
    await api.get("/api/v1/public/organizations")
    await api.get(f"/api/v1/public/organizations/{org.id}/branches")
    after = len((await db_session.execute(select(Ticket))).scalars().all())

    assert before == after == 0


# ══ Noms lisibles du triplet ═════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_creation_renvoie_les_noms_du_triplet(api, db_session):
    """
    Un ticket sans noms oblige le mobile à afficher des UUID.

    Il ne peut pas les traduire lui-même : `agency_id` et `service_id` sont des
    identifiants opaques, et l'écran de détail doit afficher « Agence Centre —
    Dépôt » même après une réinstallation, quand le catalogue local est vide.
    """
    org, branch, service, counter, agent, client = await make_world(db_session)

    created = await api.post(
        "/api/v1/tickets",
        json={"agency_id": branch.id, "service_id": service.id},
        headers=auth(client),
    )

    assert created.status_code == 200, created.text
    payload = created.json()
    assert payload["organization_id"] == org.id
    assert payload["organization_name"] == org.name
    assert payload["agency_name"] == branch.name
    assert payload["service_name"] == service.name


@pytest.mark.asyncio
async def test_ticket_actif_et_detail_portent_les_memes_noms(api, db_session):
    """
    Les deux chemins que le mobile emprunte (démarrage et rafraîchissement)
    doivent produire la même chose : sinon l'en-tête du ticket changerait de
    libellé selon la façon dont l'écran a été ouvert.
    """
    org, branch, service, counter, agent, client = await make_world(db_session)
    created = await api.post(
        "/api/v1/tickets",
        json={"agency_id": branch.id, "service_id": service.id},
        headers=auth(client),
    )
    ticket_id = created.json()["id"]

    active = (await api.get("/api/v1/tickets/active", headers=auth(client))).json()["data"]
    detail = (await api.get(f"/api/v1/tickets/{ticket_id}", headers=auth(client))).json()

    for payload in (active, detail):
        assert payload["organization_name"] == org.name
        assert payload["agency_name"] == branch.name
        assert payload["service_name"] == service.name


@pytest.mark.asyncio
async def test_les_noms_survivent_a_une_agence_sans_organisation(api, db_session):
    """
    `branches.org_id` est nullable : un INNER JOIN aurait fait disparaître le
    ticket de la réponse, transformant une donnée incomplète en 404.
    """
    org, branch, service, counter, agent, client = await make_world(db_session)
    created = await api.post(
        "/api/v1/tickets",
        json={"agency_id": branch.id, "service_id": service.id},
        headers=auth(client),
    )

    # Détache l'agence de son organisation, comme le permet le schéma.
    branch.org_id = None
    await db_session.commit()

    detail = await api.get(
        f"/api/v1/tickets/{created.json()['id']}", headers=auth(client)
    )

    assert detail.status_code == 200, detail.text
    assert detail.json()["agency_name"] == branch.name
    assert detail.json()["service_name"] == service.name
    assert detail.json()["organization_name"] is None



# ══ Services publics ═════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_services_publics_portent_le_monde_en_attente(api, db_session):
    """
    L'écran de choix d'un service annonce « N personnes devant vous ».

    `ServiceService.list` ne renvoie que les colonnes du service : sans ce
    compte, le mobile afficherait zéro personne à un client qui en a douze
    devant lui — ou devrait appeler la file complète pour un simple entier.
    """
    org, branch, service, counter, agent, client = await make_world(db_session)
    await api.post(
        "/api/v1/tickets",
        json={"agency_id": branch.id, "service_id": service.id},
        headers=auth(client),
    )

    response = await api.get(f"/api/v1/branches/{branch.id}/services")

    assert response.status_code == 200, response.text
    items = response.json()["data"]
    assert len(items) == 1
    assert items[0]["id"] == service.id
    assert items[0]["waiting_count"] == 1

    # La route existait déjà : enrichir ne doit pas appauvrir. `created_at`
    # était exposé par `ServiceService.list`, et le retirer casserait un
    # consommateur pour un champ que le mobile n'utilise pas.
    assert "created_at" in items[0]
    assert "ticket_prefix" in items[0]


@pytest.mark.asyncio
async def test_services_publics_masquent_les_inactifs(api, db_session):
    """Un service désactivé ne doit pas être proposé : le serveur le refuserait."""
    org, branch, service, counter, agent, client = await make_world(db_session)
    service.is_active = False
    await db_session.commit()

    items = (await api.get(f"/api/v1/branches/{branch.id}/services")).json()["data"]
    assert items == []
