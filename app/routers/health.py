"""
Router Health — sondes d'exploitation (montées à la racine, hors /api/v1).

  GET /health            réveil / UptimeRobot : aucune dépendance, réponse instantanée
  GET /health/live       liveness Kubernetes : le processus répond — public
  GET /health/ready      readiness Kubernetes : base ET Redis répondent — public
  GET /health/status     état riche (version, environnement, base, Redis) — public
  GET /health/detailed   latences, files, WebSocket, scheduler — admin d'organisation et plus

``/health`` ne touche volontairement ni PostgreSQL ni Redis : un ping de
monitoring toutes les 5 minutes ne doit pas réveiller la base ni consommer
des connexions. Utiliser ``/health/ready`` pour un contrôle fonctionnel.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.deps import get_current_admin_org
from app.models.user import User
from app.services import health_service

router = APIRouter(tags=["🏠 Accueil"])


@router.api_route(
    "/health",
    methods=["GET", "HEAD"],
    summary="Réveil ultra-léger (sans base ni Redis)",
)
def health_check():
    """
    Endpoint de réveil pour UptimeRobot et les load balancers.

    Aucune dépendance n'est interrogée : la réponse est produite par le
    processus FastAPI lui-même. Cela évite de réveiller PostgreSQL/Redis et
    de consommer une connexion à chaque ping.

    HEAD est déclaré explicitement : FastAPI n'ajoute pas HEAD aux routes GET,
    et UptimeRobot sonde d'abord en HEAD. Sans cette ligne, la sonde recevait
    « 405 Method Not Allowed » (en-tête `allow: GET`) et le moniteur basculait
    en échec — alors que le service répondait parfaitement en GET.
    """
    return {"status": "awake", "service": "tonde-api"}


@router.api_route("/health/live", methods=["GET", "HEAD"], summary="Liveness probe")
async def liveness():
    """Le processus répond. Aucune dépendance testée : un redémarrage ne doit pas dépendre de la base."""
    return {"status": "alive"}


@router.get("/health/status", summary="État riche de l'API (base + Redis)")
async def health_status():
    """
    État complet pour les tableaux de bord et l'exploitation.
    Retourne 503 « degraded » si la base ou Redis ne répond pas.
    """
    ready, checks = await health_service.readiness()
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ok" if ready else "degraded",
            "version": settings.APP_VERSION,
            "environment": settings.ENVIRONMENT,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "services": {
                "api": "ok",
                "redis": "ok" if checks["redis"]["status"] == "healthy" else "error",
                "database": "ok" if checks["database"]["status"] == "healthy" else "error",
            },
        },
    )


@router.get("/health/ready", summary="Readiness probe")
async def readiness():
    """Prêt à recevoir du trafic ? 503 tant que la base ou Redis ne répond pas."""
    ready, checks = await health_service.readiness()
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )


@router.get("/health/detailed", summary="Santé détaillée (admin)")
async def detailed_health(
    request: Request,
    current_user: User = Depends(get_current_admin_org),
):
    """
    Latences base/Redis, taille des files, connexions WebSocket, requêtes par minute,
    état du scheduler et configuration des passerelles SMS/push.
    Réservé aux administrateurs d'organisation et à l'équipe Tonde.
    """
    report = await health_service.detailed_report(getattr(request.app.state, "scheduler", None))
    return JSONResponse(status_code=200 if report["status"] == "healthy" else 503, content=report)
