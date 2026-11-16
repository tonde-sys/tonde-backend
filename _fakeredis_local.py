"""
Plugin pytest — rejoue en local les tests « vrai Redis », sans Docker.

Les tests d'atomicité (`test_queue_atomicity`, `test_queue_engine_features`, …)
exigent un serveur Redis et sont ignorés quand il n'y en a pas : ils ne tournent
donc qu'en CI. Ce plugin branche `redis.asyncio.from_url` sur fakeredis pour les
rejouer sur une machine de développement.

Ce n'est PAS un substitut à la CI : la sémantique de fakeredis n'est pas celle
de Redis 7 (persistance, éviction, subtilités de `ZPOPMIN` sous concurrence).
C'est un filet pour voir rouge avant de pousser, pas une preuve.

Usage :
    pytest -p _fakeredis_local tests/
"""
import fakeredis
import fakeredis.aioredis
import redis.asyncio as _aioredis

# Un seul serveur pour tous les clients : la fixture `real_redis` ouvre un client
# de sonde puis un client de travail, et les deux doivent voir les mêmes données.
_server = fakeredis.FakeServer()


def _fake_from_url(url, **kwargs):
    """Remplace `aioredis.from_url` : ignore l'URL, partage le serveur en mémoire."""
    kwargs.pop("socket_connect_timeout", None)   # pas de réseau, donc pas de délai
    kwargs.pop("encoding", None)                 # fakeredis encode en UTF-8
    return fakeredis.aioredis.FakeRedis(server=_server, **kwargs)


def pytest_configure(config):
    _aioredis.from_url = _fake_from_url
