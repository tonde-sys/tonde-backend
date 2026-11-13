"""
Métriques de requêtes en mémoire (par instance de l'API).

Alimente /health/detailed : requêtes par minute, répartition des codes HTTP, uptime.
Volontairement simple (pas de dépendance, pas d'appel réseau) : chaque instance
expose ses propres chiffres, un outil de monitoring les agrège.
"""
import time
from collections import Counter, deque


class RequestMetrics:
    """Compteurs de requêtes HTTP sur une fenêtre glissante d'une minute."""

    WINDOW_SECONDS = 60

    def __init__(self) -> None:
        self.started_at = time.time()
        self._timestamps: deque[float] = deque()
        self.total_requests = 0
        self.by_status_class: Counter[str] = Counter()

    def record(self, status_code: int) -> None:
        """Enregistre une requête terminée avec son code HTTP."""
        now = time.time()
        self._timestamps.append(now)
        self.total_requests += 1
        self.by_status_class[f"{status_code // 100}xx"] += 1
        self._trim(now)

    def requests_last_minute(self) -> int:
        """Nombre de requêtes sur les 60 dernières secondes."""
        self._trim(time.time())
        return len(self._timestamps)

    def uptime_seconds(self) -> int:
        """Secondes écoulées depuis le démarrage de l'instance."""
        return int(time.time() - self.started_at)

    def _trim(self, now: float) -> None:
        cutoff = now - self.WINDOW_SECONDS
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()


# Instance unique de l'application
request_metrics = RequestMetrics()
