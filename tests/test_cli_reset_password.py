"""
Tests — commande `python -m app.cli reset-password`.

Cette commande est l'outil de rotation : elle change un mot de passe sans
rejouer tout le bootstrap. Les tests ci-dessous couvrent le câblage de l'argparse
et les refus de validation, qui s'appliquent AVANT toute connexion à la base —
donc sans PostgreSQL ni Redis.
"""
import argparse

import pytest

from app.cli import _build_parser, _run_reset_password
from app.services.bootstrap_service import BootstrapError


def test_commande_analysee():
    args = _build_parser().parse_args(
        ["reset-password", "--email", "agent1@ulbu.bi", "--password", "Nouveau-Mot-2026"]
    )
    assert args.command == "reset-password"
    assert args.email == "agent1@ulbu.bi"
    assert args.password == "Nouveau-Mot-2026"


def test_email_obligatoire():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["reset-password"])


def test_mot_de_passe_omis_signifie_generation():
    """Sans --password, la commande génère un mot de passe aléatoire."""
    args = _build_parser().parse_args(["reset-password", "--email", "agent1@ulbu.bi"])
    assert args.password is None


def test_mot_de_passe_trop_court_refuse():
    """Le refus tombe avant toute connexion à la base."""
    args = argparse.Namespace(email="agent1@ulbu.bi", password="court", json=False)
    with pytest.raises(BootstrapError) as exc_info:
        import asyncio
        asyncio.run(_run_reset_password(args))
    assert "8 caractères" in str(exc_info.value)


def test_mot_de_passe_au_dela_de_72_octets_refuse():
    """bcrypt tronque au-delà de 72 octets : on refuse plutôt que de tromper."""
    args = argparse.Namespace(email="agent1@ulbu.bi", password="a" * 73, json=False)
    with pytest.raises(BootstrapError) as exc_info:
        import asyncio
        asyncio.run(_run_reset_password(args))
    assert "72 octets" in str(exc_info.value)
