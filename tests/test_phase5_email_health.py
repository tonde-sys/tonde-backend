"""
SMTP visible dans `/health/detailed`.

L'absence de passerelle email est le seul défaut de configuration qui ne se voit
PAS côté utilisateur : « mot de passe oublié » répond 200 avec un message neutre
que le compte existe ou non, et que l'email parte ou non. C'est voulu (pas
d'énumération de comptes), mais cela signifie qu'une production sans SMTP est
indiscernable d'une production correcte — sauf en lisant les logs.

Ce contrôle rend l'information consultable : `email_gateway.status` vaut
`configured`, `not_configured` ou `simulated`.
"""
import pytest

from app.core.config import settings
from app.services.health_service import check_email_gateway


def test_en_developpement_l_email_est_simule(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    assert check_email_gateway()["status"] == "simulated"


def test_en_production_sans_smtp_le_statut_est_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    monkeypatch.setattr(settings, "SMTP_FROM", "TONDE <no-reply@tonde.app>")

    etat = check_email_gateway()
    assert etat["status"] == "not_configured"
    assert etat["host"] is None
    assert etat["port"] is None


def test_en_production_avec_smtp_le_statut_expose_l_hote_et_le_port(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.brevo.com")
    monkeypatch.setattr(settings, "SMTP_PORT", 587)

    etat = check_email_gateway()
    assert etat["status"] == "configured"
    # L'hôte et le port sont affichés pour qu'un « ça n'envoie pas » se
    # diagnostique sans ouvrir les variables d'environnement de Render.
    assert etat["host"] == "smtp.brevo.com"
    assert etat["port"] == 587


@pytest.mark.parametrize("manquant", ["SMTP_HOST", "SMTP_FROM"])
def test_un_smtp_incomplet_n_est_pas_configure(monkeypatch, manquant):
    """Les deux variables sont nécessaires : un hôte sans expéditeur ne peut pas envoyer."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.brevo.com")
    monkeypatch.setattr(settings, "SMTP_FROM", "TONDE <no-reply@tonde.app>")
    monkeypatch.setattr(settings, manquant, "")

    assert check_email_gateway()["status"] == "not_configured"
