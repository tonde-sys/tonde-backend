"""
P2-13 — unicité du préfixe de ticket à la réactivation d'un service.

Le bug d'origine : `ServiceService.create` ne regardait que les services ACTIFS.
Désactiver « Caisse » (C), créer « Crédit » (C), puis réactiver « Caisse »
produisait deux services actifs avec le même préfixe — donc deux files qui
émettent des C-001 différents le même jour, et un écran de salle qui affiche
« C-002 au guichet 3 » sans dire de quel service il parle.

La règle retenue est « au plus UN service actif par préfixe et par branch » :
un service désactivé libère sa lettre (réutilisation légitime), mais on ne peut
pas la reprendre tant qu'elle est utilisée par un service actif.
"""
import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.branch import Branch, Service
from app.models.organization import Organization
from app.schemas.service import CreateServiceRequest, UpdateServiceRequest
from app.services.service_service import ServiceService


async def _make_org(db) -> Organization:
    org = Organization(name="Banque Test", slug=f"org-{uuid.uuid4().hex[:8]}", sector="bank")
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


async def _make_branch(db, org_id: str) -> Branch:
    branch = Branch(
        org_id=org_id, name="Agence Centre", slug=f"b-{uuid.uuid4().hex[:8]}",
        sector="bank", is_active=True, is_open=True,
        opens_at="00:00", closes_at="23:59",
    )
    db.add(branch)
    await db.commit()
    await db.refresh(branch)
    return branch


@pytest_asyncio.fixture
async def contexte(db_session):
    """Org + branch + service actif « Caisse » (préfixe C)."""
    org = await _make_org(db_session)
    branch = await _make_branch(db_session, org.id)
    service = ServiceService(db_session)
    caisse = await service.create(
        org_id=org.id, branch_id=branch.id,
        data=CreateServiceRequest(name="Caisse", ticket_prefix="C", avg_duration_minutes=5),
    )
    return org, branch, caisse, service


# ══ Le bug : réactivation avec un préfixe repris entre-temps ═════════════════

@pytest.mark.asyncio
async def test_reactivation_refusee_si_le_prefixe_a_ete_repris(contexte, db_session):
    """
    Scénario exact du constat : Caisse (C) désactivée → Crédit (C) créé →
    réactivation de Caisse refusée en 409, et Caisse reste désactivée.
    """
    org, branch, caisse, service = contexte

    await service.delete(caisse["id"], branch.id, org.id)
    credit = await service.create(
        org_id=org.id, branch_id=branch.id,
        data=CreateServiceRequest(name="Crédit", ticket_prefix="C", avg_duration_minutes=8),
    )
    assert credit["ticket_prefix"] == "C"

    with pytest.raises(HTTPException) as exc_info:
        await service.update(
            caisse["id"], branch.id, org.id, UpdateServiceRequest(is_active=True)
        )

    detail = exc_info.value.detail
    assert exc_info.value.status_code == 409
    assert detail["code"] == "PREFIX_TAKEN"
    assert detail["conflicting_service_id"] == credit["id"]
    assert detail["conflicting_service_name"] == "Crédit"

    # L'état n'a pas bougé : la réactivation refusée ne doit pas laisser le
    # service actif « à moitié ». Un 409 qui active quand même serait pire que
    # pas de contrôle du tout.
    inchange = await db_session.get(Service, caisse["id"])
    await db_session.refresh(inchange)
    assert inchange.is_active is False


@pytest.mark.asyncio
async def test_reactivation_autorisee_si_le_prefixe_est_libre(contexte):
    """Un service désactivé dont personne n'a repris la lettre se réactive normalement."""
    org, branch, caisse, service = contexte
    await service.delete(caisse["id"], branch.id, org.id)

    reactive = await service.update(
        caisse["id"], branch.id, org.id, UpdateServiceRequest(is_active=True)
    )
    assert reactive["is_active"] is True


@pytest.mark.asyncio
async def test_reactivation_d_un_service_deja_actif_ne_se_plaint_pas(contexte):
    """
    PATCH {is_active: true} sur un service déjà actif : la vérification s'exclut
    elle-même, sinon on refuserait de réenregistrer un service inchangé.
    """
    org, branch, caisse, service = contexte

    result = await service.update(
        caisse["id"], branch.id, org.id, UpdateServiceRequest(is_active=True)
    )
    assert result["is_active"] is True


# ══ Ce qui doit rester possible ═══════════════════════════════════════════════

@pytest.mark.asyncio
async def test_reutilisation_du_prefixe_d_un_service_desactive(contexte):
    """
    Un service retiré libère sa lettre : c'est le cas normal d'un guichet qui
    change de prestation. L'unicité porte sur les services ACTIFS, pas sur
    l'historique — sinon un préfixe serait brûlé à vie.
    """
    org, branch, caisse, service = contexte
    await service.delete(caisse["id"], branch.id, org.id)

    nouveau = await service.create(
        org_id=org.id, branch_id=branch.id,
        data=CreateServiceRequest(name="Crédit", ticket_prefix="C"),
    )
    assert nouveau["ticket_prefix"] == "C"
    assert nouveau["is_active"] is True


@pytest.mark.asyncio
async def test_creation_refusee_si_un_service_actif_porte_deja_le_prefixe(contexte):
    """Comportement d'origine conservé : pas de doublon à la création."""
    org, branch, caisse, service = contexte

    with pytest.raises(HTTPException) as exc_info:
        await service.create(
            org_id=org.id, branch_id=branch.id,
            data=CreateServiceRequest(name="Crédit", ticket_prefix="c"),
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "PREFIX_TAKEN"
    assert exc_info.value.detail["conflicting_service_name"] == "Caisse"


@pytest.mark.asyncio
async def test_creation_d_un_service_inactif_refusee_si_le_prefixe_est_actif(contexte):
    """
    Créer un service INACTIF avec un préfixe déjà actif reste refusé : autoriser
    ce cas ne ferait que déplacer le conflit au moment de la réactivation.
    """
    org, branch, caisse, service = contexte

    with pytest.raises(HTTPException) as exc_info:
        await service.create(
            org_id=org.id, branch_id=branch.id,
            data=CreateServiceRequest(name="Crédit", ticket_prefix="C", is_active=False),
        )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_deux_branches_peuvent_utiliser_le_meme_prefixe(contexte, db_session):
    """L'unicité est par branch : deux agences ont chacune leur « Caisse » (C)."""
    org, branch, caisse, service = contexte
    autre = await _make_branch(db_session, org.id)

    jumelle = await service.create(
        org_id=org.id, branch_id=autre.id,
        data=CreateServiceRequest(name="Caisse", ticket_prefix="C"),
    )
    assert jumelle["branch_id"] == autre.id


# ══ Le préfixe reste immuable ════════════════════════════════════════════════

def test_le_prefixe_n_est_pas_modifiable_par_patch():
    """
    `extra="forbid"` : le préfixe est refusé en PATCH. C'est ce qui limite le
    correctif à la seule réactivation — il n'existe aucun autre chemin pour
    changer la lettre d'un service existant.
    """
    with pytest.raises(ValidationError):
        UpdateServiceRequest(ticket_prefix="Z")


# ══ La ceinture : index unique partiel en base ═══════════════════════════════

def test_migration_010_est_la_tete_de_la_chaine():
    """La migration existe et ferme la chaîne après 009."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["010_unique_active_service_prefix"]

    revision = script.get_revision("010_unique_active_service_prefix")
    assert revision.down_revision == "009_audit_org_nullable"


def test_migration_010_cree_un_index_partiel():
    """
    L'index doit être PARTIEL (`WHERE is_active`) : un index unique global
    interdirait de réutiliser le préfixe d'un service désactivé, ce qui casserait
    le cas d'usage légitime testé plus haut.
    """
    from pathlib import Path

    source = Path("migrations/versions/010_unique_active_service_prefix.py").read_text(encoding="utf-8")
    assert "CREATE UNIQUE INDEX" in source
    assert "WHERE is_active" in source
    assert "uq_services_active_prefix" in source
    # Même précaution que la migration 008 : on n'échoue pas si des doublons
    # existent déjà, sinon le déploiement casse sur la base qu'on veut réparer.
    assert "logger.warning" in source
