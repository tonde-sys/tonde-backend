"""Tests de l'activation QAPE automatique au bootstrap d'une organisation."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.qape import AbusePolicy
from app.services.bootstrap_service import bootstrap_pilot


def _config(**overrides):
    config = {
        "organization": {
            "name": "Org QAPE Test",
            "slug": "org-qape-test",
            "sector": "other",
        },
        "branch": {
            "name": "Agence QAPE",
            "slug": "agence-qape",
            "is_open": True,
        },
        "services": [
            {"name": "Service QAPE", "ticket_prefix": "Q"},
        ],
        "counters": [
            {"name": "Guichet QAPE"},
        ],
        "users": [
            {
                "name": "Admin QAPE",
                "email": "admin-qape@example.bi",
                "role": "admin_org",
                "password": "QapePass123",
            },
        ],
    }
    config.update(overrides)
    return config


async def test_bootstrap_activates_qape_by_default(db_session):
    """Sans section `qape`, l'org doit recevoir une politique org-wide active."""
    result = await bootstrap_pilot(db_session, _config())

    assert result.qape is not None
    assert result.qape["enabled"] is True
    assert result.qape["scope"] == "organization"
    assert result.created.get("qape_policies") == 1

    policies = (await db_session.scalars(
        select(AbusePolicy).where(AbusePolicy.enabled.is_(True))
    )).all()
    assert len(policies) == 1
    assert policies[0].service_id is None  # org-wide
    assert policies[0].version == 1


async def test_bootstrap_qape_can_be_disabled(db_session):
    """qape.enabled=false doit laisser l'org sans politique."""
    config = _config()
    config["qape"] = {"enabled": False}

    result = await bootstrap_pilot(db_session, config)

    assert result.qape == {"enabled": False, "reason": "already_active_or_disabled"}
    assert "qape_policies" not in result.created
    assert (await db_session.scalars(select(AbusePolicy))).all() == []


async def test_bootstrap_qape_is_idempotent(db_session):
    """Relancer le bootstrap ne doit pas créer une seconde politique active."""
    config = _config()

    first = await bootstrap_pilot(db_session, config)
    assert first.qape["enabled"] is True
    assert first.created.get("qape_policies") == 1

    second = await bootstrap_pilot(db_session, config)
    assert second.qape == {"enabled": False, "reason": "already_active_or_disabled"}
    assert "qape_policies" not in second.created

    policies = (await db_session.scalars(
        select(AbusePolicy).where(AbusePolicy.enabled.is_(True))
    )).all()
    assert len(policies) == 1


async def test_bootstrap_qape_rejects_invalid_config(db_session):
    """Un seuil invalide doit faire échouer le bootstrap, pas écrire à moitié."""
    from app.services.bootstrap_service import BootstrapError

    config = _config()
    config["qape"] = {
        "enabled": True,
        "limits": {
            "bucket_capacity": 0,  # invalide : doit être >= 1
            "bucket_refill_seconds": 30.0,
            "max_requests_per_10m": 3,
            "max_tickets_per_day": 5,
        },
    }
    with pytest.raises(BootstrapError):
        await bootstrap_pilot(db_session, config)
