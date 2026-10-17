"""
Limite de intentos de login fallidos. En memoria: un reinicio lo vacia,
aceptable para un server self-hosted.

Dos contadores en la misma ventana:
  - (IP, usuario): N fallos -> esa cuenta bloqueada desde esa IP.
  - IP sola: M fallos contra cualquier cuenta -> esa IP bloqueada. Frena
    probar una password contra muchas cuentas (y el gasto de argon2).
Bloqueado hasta que el fallo mas viejo caduque. Un login correcto limpia el
contador (IP, usuario), no el de la IP.
"""
import time
import threading

from . import config

_lock = threading.Lock()
_fails = {}                     # (ip, user) o ip -> [timestamps de fallos]
_PRUNE_AT = 1000                # limpia claves caducadas a partir de este tamaño


def _recent(key, now):
    ts = [t for t in _fails.get(key, ()) if now - t < config.LOGIN_WINDOW_SEC]
    if ts:
        _fails[key] = ts
    else:
        _fails.pop(key, None)
    return ts


def _wait(ts, limit, now):
    if limit <= 0 or len(ts) < limit:
        return 0
    return max(1, int(config.LOGIN_WINDOW_SEC - (now - ts[-limit])) + 1)


def retry_after(ip, user):
    """(segundos hasta poder reintentar, scope "ip"|"user"); (0, None) si libre."""
    now = time.monotonic()
    with _lock:
        w_ip = _wait(_recent(ip, now), config.LOGIN_MAX_FAILS_IP, now)
        w_user = _wait(_recent((ip, user), now), config.LOGIN_MAX_FAILS, now)
    if w_ip >= w_user and w_ip:
        return w_ip, "ip"
    if w_user:
        return w_user, "user"
    return 0, None


def fail(ip, user):
    now = time.monotonic()
    with _lock:
        _fails.setdefault((ip, user), []).append(now)
        _fails.setdefault(ip, []).append(now)
        if len(_fails) > _PRUNE_AT:
            # Sin esto, usernames aleatorios harian crecer el dict sin fin
            for k in list(_fails):
                _recent(k, now)


def clear(ip, user):
    with _lock:
        _fails.pop((ip, user), None)
