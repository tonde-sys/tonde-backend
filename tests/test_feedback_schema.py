"""
Validation des retours côté API.

Ces règles sont la première ligne de défense : elles s'appliquent avant toute
écriture, et une requête refusée ici ne coûte pas une transaction. Elles sont
volontairement testées séparément du service — une régression sur une longueur
minimale doit se voir immédiatement, sans monter une base.
"""
import pytest
from pydantic import ValidationError

from app.schemas.feedback import CreateFeedbackRequest


def _base(**overrides) -> dict:
    payload = {
        "category": "bug",
        "message": "Le bouton de prise de ticket ne repond pas du tout",
    }
    payload.update(overrides)
    return payload


def test_un_message_trop_court_est_refuse():
    with pytest.raises(ValidationError) as exc:
        CreateFeedbackRequest(**_base(message="ca marche pas"))
    assert "20 caractères" in str(exc.value)


def test_un_message_de_20_caracteres_passe():
    # La borne est inclusive : 20 caractères exactement doivent être acceptés,
    # sinon le message d'erreur ment sur la règle qu'il énonce.
    exact = "a" * 20
    assert CreateFeedbackRequest(**_base(message=exact)).message == exact


def test_un_message_trop_long_est_refuse():
    with pytest.raises(ValidationError) as exc:
        CreateFeedbackRequest(**_base(message="a" * 1001))
    assert "1000" in str(exc.value)


def test_les_espaces_de_bord_ne_comptent_pas_dans_la_longueur():
    # 20 caractères utiles entourés d'espaces : le nettoyage se fait AVANT la
    # mesure, sinon il suffirait d'ajouter des espaces pour passer la règle.
    with pytest.raises(ValidationError):
        CreateFeedbackRequest(**_base(message="   " + "a" * 19 + "   "))


def test_une_note_hors_bornes_est_refusee():
    for note in (0, 6, -1):
        with pytest.raises(ValidationError):
            CreateFeedbackRequest(**_base(rating=note))


def test_une_note_absente_est_acceptee():
    # Un signalement de bug n'a pas à être noté.
    assert CreateFeedbackRequest(**_base()).rating is None


def test_une_categorie_inconnue_est_refusee():
    with pytest.raises(ValidationError) as exc:
        CreateFeedbackRequest(**_base(category="plainte"))
    assert "Catégorie invalide" in str(exc.value)


def test_la_plateforme_est_normalisee_et_fermee():
    assert CreateFeedbackRequest(**_base(platform="ANDROID")).platform == "android"
    with pytest.raises(ValidationError):
        CreateFeedbackRequest(**_base(platform="symbian"))
