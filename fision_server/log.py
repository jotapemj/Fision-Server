"""
Logging del servidor. Todo a stdout -> lo captura journald (systemd) o Docker.

Dos lineas de eventos, faciles de grepear:

    ... AUDIT  user.create user=valeria
    ... AUDIT  user.delete user=bob by=jp
    ... AUDIT  password_change.success user=valeria
    ... AUDIT  login.success user=jp
    ... AUDIT  login.fail user=jp reason=bad_credentials
    ... ACCESS user=jp POST /jobs 200 143ms

Ver solo eventos de usuario en el servicio:
    journalctl -u fision -f | grep AUDIT
"""
import sys
import logging

from . import config

_configured = False


def setup():
    """Configura el logger raiz 'fision' una sola vez (idempotente)."""
    global _configured
    if _configured:
        return
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        "%Y-%m-%dT%H:%M:%S%z",
    )
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(fmt)
    root = logging.getLogger("fision")
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    root.handlers.clear()
    root.addHandler(h)
    root.propagate = False
    _configured = True


audit = logging.getLogger("fision.audit")
access = logging.getLogger("fision.access")


def _kv(d):
    return " ".join(f"{k}={v}" for k, v in d.items() if v is not None and v != "")


def event(name, user=None, **fields):
    """Evento de auditoria. Ej: event('user.create', user='valeria')."""
    setup()
    tail = _kv({"user": user, **fields})
    audit.info("AUDIT %s %s", name, tail)


def access_line(method, path, status, user, ms):
    """Una linea por peticion HTTP, con el usuario que la hizo (o '-')."""
    setup()
    access.info("ACCESS user=%s %s %s %s %.0fms",
                user or "-", method, path, status, ms)
