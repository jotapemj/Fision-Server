"""
Limite de intentos de login fallidos por (IP, usuario). En memoria: un
reinicio lo vacia, aceptable para un server self-hosted.

N fallos dentro de la ventana -> bloqueado hasta que el mas viejo caduque.
Un login correcto limpia el contador.
"""
import time
import threading

from . import config

_lock = threading.Lock()
_fails = {}                     # (ip, user) -> [timestamps de fallos]
_PRUNE_AT = 1000                # limpia claves caducadas a partir de este tamaño


def _recent(key, now):
    ts = [t for t in _fails.get(key, ()) if now - t < config.LOGIN_WINDOW_SEC]
    if ts:
        _fails[key] = ts
    else:
        _fails.pop(key, None)
    return ts


def retry_after(ip, user):
    """Segundos hasta poder reintentar; 0 si no esta bloqueado."""
    if config.LOGIN_MAX_FAILS <= 0:
        return 0
    now = time.monotonic()
    with _lock:
        ts = _recent((ip, user), now)
        if len(ts) < config.LOGIN_MAX_FAILS:
            return 0
        return max(1, int(config.LOGIN_WINDOW_SEC - (now - ts[0])) + 1)


def fail(ip, user):
    now = time.monotonic()
    with _lock:
        _fails.setdefault((ip, user), []).append(now)
        if len(_fails) > _PRUNE_AT:
            # Sin esto, usernames aleatorios harian crecer el dict sin fin
            for k in list(_fails):
                _recent(k, now)


def clear(ip, user):
    with _lock:
        _fails.pop((ip, user), None)
