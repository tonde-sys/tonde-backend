"""
Schémas Pydantic v2 pour les retours utilisateurs (`/api/v1/feedback`).

Ce sont des contrats d'API, séparés des modèles SQLAlchemy : les noms de champs
et les règles de validation peuvent évoluer sans toucher au schéma de la base.

Règles de validation, et pourquoi elles existent :

  - **20 caractères minimum** : « ça marche pas » n'aide personne. En dessous de
    20 caractères, le support doit rappeler le client pour comprendre, ce qui
    coûte plus cher que le retour lui-même.
  - **1000 caractères maximum** : au-delà, ce n'est plus un retour mais un
    courrier. Le champ est en `Text` côté base, la limite est une décision
    produit, pas une contrainte de stockage.
  - **`platform` et `locale` sont fermés** : ce sont des valeurs que
    l'application renseigne elle-même, jamais l'utilisateur. Une liste blanche
    évite qu'un client modifié n'y écrive n'importe quoi, et garde les
    statistiques exploitables.
"""
from typing import Optional

from pydantic import BaseModel, field_validator

VALID_CATEGORIES = {"review", "bug", "suggestion"}
VALID_PLATFORMS = {"android", "ios", "web", "unknown"}

MESSAGE_MIN = 20
MESSAGE_MAX = 1000


class CreateFeedbackRequest(BaseModel):
    category: str
    message: str
    # Note de 1 à 5. Facultative : un signalement de bug n'a pas à être noté.
    rating: Optional[int] = None
    # Renseignés par l'application, jamais saisis par l'utilisateur.
    org_id: Optional[str] = None
    ticket_id: Optional[str] = None
    app_version: Optional[str] = None
    platform: Optional[str] = None
    device_model: Optional[str] = None
    locale: Optional[str] = None

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        if v not in VALID_CATEGORIES:
            raise ValueError(
                f"Catégorie invalide. Valeurs acceptées : {', '.join(sorted(VALID_CATEGORIES))}"
            )
        return v

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str) -> str:
        cleaned = v.strip()
        if len(cleaned) < MESSAGE_MIN:
            raise ValueError(f"Le message doit contenir au moins {MESSAGE_MIN} caractères")
        if len(cleaned) > MESSAGE_MAX:
            raise ValueError(f"Le message ne doit pas dépasser {MESSAGE_MAX} caractères")
        return cleaned

    @field_validator("rating")
    @classmethod
    def validate_rating(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return v
        if v < 1 or v > 5:
            raise ValueError("La note doit être comprise entre 1 et 5")
        return v

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        lowered = v.strip().lower()
        if lowered not in VALID_PLATFORMS:
            raise ValueError(
                f"Plateforme invalide. Valeurs acceptées : {', '.join(sorted(VALID_PLATFORMS))}"
            )
        return lowered
