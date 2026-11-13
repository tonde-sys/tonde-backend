"""
Client IA de TONDE — API Coarena (compatible OpenAI).

Utilise httpx (déjà une dépendance) plutôt qu'un SDK supplémentaire.
Configuration via AI_BASE_URL / AI_API_KEY / AI_MODEL dans .env.
"""
import logging
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class AIServiceError(Exception):
    """Erreur lors d'un appel à l'API IA."""


def _headers() -> Dict[str, str]:
    if not settings.AI_API_KEY:
        raise AIServiceError("AI_API_KEY non configurée")
    return {
        "Authorization": f"Bearer {settings.AI_API_KEY}",
        "Content-Type": "application/json",
    }


async def list_models() -> List[str]:
    """Retourne les identifiants de modèles disponibles."""
    async with httpx.AsyncClient(timeout=settings.AI_TIMEOUT_SECONDS) as client:
        resp = await client.get(f"{settings.AI_BASE_URL}/models", headers=_headers())
    if resp.status_code != 200:
        raise AIServiceError(f"list_models: HTTP {resp.status_code}")
    return [m["id"] for m in resp.json().get("data", [])]


async def chat(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 1024,
) -> Dict[str, Any]:
    """
    Appelle /chat/completions.
    Retourne {"id": ..., "content": ..., "model": ...}.
    """
    payload = {
        "model": model or settings.AI_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.AI_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{settings.AI_BASE_URL}/chat/completions",
                headers=_headers(),
                json=payload,
            )
    except httpx.HTTPError as exc:
        logger.error("Appel IA échoué: %s", exc)
        raise AIServiceError(str(exc)) from exc

    if resp.status_code != 200:
        logger.error("Appel IA HTTP %s: %s", resp.status_code, resp.text[:300])
        raise AIServiceError(f"chat: HTTP {resp.status_code}")

    data = resp.json()
    return {
        "id": data.get("id"),
        "model": data.get("model"),
        "content": data["choices"][0]["message"]["content"],
    }


async def send_feedback(response_id: str, rating: Any, comment: str = "") -> Dict[str, Any]:
    """Note une réponse via POST /feedback."""
    body: Dict[str, Any] = {"id": response_id, "rating": rating}
    if comment:
        body["note"] = comment
    async with httpx.AsyncClient(timeout=settings.AI_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"{settings.AI_BASE_URL}/feedback", headers=_headers(), json=body
        )
    if resp.status_code >= 400:
        raise AIServiceError(f"feedback: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json() if resp.content else {}
