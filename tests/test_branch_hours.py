"""
Fenêtre d'ouverture d'une agence — `is_branch_open_now` / `is_within_window`.

Ces tests sont volontairement purs (aucune base, aucun Redis) : la décision
« cette agence accepte-t-elle un ticket maintenant ? » ne doit dépendre que de
la configuration et de l'heure injectée.

Le défaut corrigé ici : quand `operating_hours` était NULL, la fonction renvoyait
True sans jamais lire `opens_at`/`closes_at`. Une agence déclarant 10:00 → 14:00
acceptait donc des tickets à 3 h du matin (vérifié en production sur ULBU).
"""
from datetime import datetime

import pytest

from app.services.ticket_service import is_branch_open_now, is_within_window


def _branch(**kwargs):
    """Branch transitoire (non persistée) avec des valeurs par défaut explicites."""
    from app.models.branch import Branch

    params = {
        "name": "Agence test",
        "slug": "agence-test",
        "sector": "bank",
        "is_active": True,
        "is_open": True,
        "opens_at": "08:00",
        "closes_at": "17:00",
        "operating_hours": None,
    }
    params.update(kwargs)
    return Branch(**params)


LUNDI_10H = datetime(2026, 11, 9, 10, 0)
LUNDI_18H = datetime(2026, 11, 9, 18, 0)
MARDI_10H = datetime(2026, 11, 10, 10, 0)


# ══ Horaires détaillés par jour ═══════════════════════════════════════════════

def test_operating_hours_drive_ouverture():
    """Avec `operating_hours`, seul le créneau du jour compte."""
    branch = _branch(operating_hours={"monday": {"open": "08:00", "close": "17:00"}})

    assert is_branch_open_now(branch, LUNDI_10H) is True
    assert is_branch_open_now(branch, LUNDI_18H) is False
    # Mardi absent des horaires = fermé ce jour-là
    assert is_branch_open_now(branch, MARDI_10H) is False


def test_operating_hours_prime_sur_la_fenetre_globale():
    """`operating_hours` est prioritaire sur `opens_at`/`closes_at`."""
    branch = _branch(
        opens_at="00:00",
        closes_at="23:59",
        operating_hours={"monday": {"open": "08:00", "close": "12:00"}},
    )

    assert is_branch_open_now(branch, LUNDI_10H) is True
    assert is_branch_open_now(branch, datetime(2026, 11, 9, 15, 0)) is False


# ══ Repli sur la fenêtre globale (le défaut corrigé) ══════════════════════════

def test_sans_operating_hours_la_fenetre_globale_s_applique():
    """Le repli documenté par le modèle est désormais réellement appliqué."""
    branch = _branch(opens_at="10:00", closes_at="14:00")

    assert is_branch_open_now(branch, datetime(2026, 11, 9, 9, 59)) is False
    assert is_branch_open_now(branch, datetime(2026, 11, 9, 12, 0)) is True
    assert is_branch_open_now(branch, datetime(2026, 11, 9, 16, 25)) is False, (
        "c'est exactement le cas constaté en production : 16:25 hors fenêtre"
    )


def test_fenetre_globale_inclut_les_bornes():
    """Ouverture et fermeture sont inclusives (une agence ouverte jusqu'à 17:00 l'est à 17:00)."""
    branch = _branch(opens_at="08:00", closes_at="17:00")

    assert is_branch_open_now(branch, datetime(2026, 11, 9, 8, 0)) is True
    assert is_branch_open_now(branch, datetime(2026, 11, 9, 17, 0)) is True


def test_fenetre_de_nuit():
    """Un créneau qui enjambe minuit (22:00 → 02:00) est correctement interprété."""
    branch = _branch(opens_at="22:00", closes_at="02:00")

    assert is_branch_open_now(branch, datetime(2026, 11, 9, 23, 30)) is True
    assert is_branch_open_now(branch, datetime(2026, 11, 10, 1, 0)) is True
    assert is_branch_open_now(branch, datetime(2026, 11, 9, 12, 0)) is False


def test_horaires_absents_ou_identiques_signifient_24h():
    """Sans horaires du tout, ou avec open == close, l'agence est ouverte toute la journée."""
    assert is_branch_open_now(_branch(opens_at=None, closes_at=None), LUNDI_10H) is True
    assert is_branch_open_now(_branch(opens_at="00:00", closes_at="00:00"), LUNDI_10H) is True


# ══ Interrupteurs ═════════════════════════════════════════════════════════════

def test_interrupteur_ferme_ou_agence_inactive():
    """`is_open=False` ou `is_active=False` ferme l'agence, quelles que soient les heures."""
    assert is_branch_open_now(_branch(is_open=False), LUNDI_10H) is False
    assert is_branch_open_now(_branch(is_active=False), LUNDI_10H) is False


# ══ Fonction pure ═════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "now, opens, closes, attendu",
    [
        ("12:00", "08:00", "17:00", True),
        ("07:59", "08:00", "17:00", False),
        ("17:01", "08:00", "17:00", False),
        ("23:00", "22:00", "02:00", True),
        ("03:00", "22:00", "02:00", False),
        ("00:00", "00:00", "00:00", True),
        ("12:00", None, None, True),
    ],
)
def test_is_within_window(now, opens, closes, attendu):
    assert is_within_window(now, opens, closes) is attendu
