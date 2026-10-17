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
import re
import sys
import json
import logging
import threading
from collections import deque
from datetime import datetime, timezone

from . import config

_configured = False

# Ultimos eventos AUDIT en memoria, para el panel admin (journald no es
# legible desde el proceso). Se pierden al reiniciar; journald manda.
_recent = deque(maxlen=300)
_recent_lock = threading.Lock()


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


# sin espacios, controles (ESC incluido), comillas, = ni barra invertida
_PLAIN = re.compile(r'^[^\s\x00-\x1f\x7f"=\\]*$')
_MAX_VALUE = 200


def _fmt(v):
    """Valor seguro en una linea de log. Lo que viene del cliente (nombre de
    login, ruta) podia meter saltos de linea y fabricar lineas AUDIT falsas:
    si no es plano va entre comillas con escapes JSON. Tope de longitud."""
    s = str(v)
    if len(s) > _MAX_VALUE:
        s = s[:_MAX_VALUE] + "..."
    return s if _PLAIN.match(s) else json.dumps(s, ensure_ascii=False)


def _kv(d):
    return " ".join(f"{k}={_fmt(v)}" for k, v in d.items() if v is not None and v != "")


def event(name, user=None, **fields):
    """Evento de auditoria. Ej: event('user.create', user='valeria')."""
    setup()
    tail = _kv({"user": user, **fields})
    audit.info("AUDIT %s %s", _fmt(name), tail)
    with _recent_lock:
        _recent.append({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": name, "user": str(user or "")[:_MAX_VALUE],
            "fields": {k: str(v)[:_MAX_VALUE] for k, v in fields.items()
                       if v is not None and v != ""},
        })


def recent(limit=100):
    """Eventos AUDIT desde el arranque, mas nuevos primero."""
    with _recent_lock:
        items = list(_recent)
    return items[::-1][:max(1, int(limit))]


def access_line(method, path, status, user, ms):
    """Una linea por peticion HTTP, con el usuario que la hizo (o '-')."""
    setup()
    access.info("ACCESS user=%s %s %s %s %.0fms",
                _fmt(user or "-"), _fmt(method), _fmt(path), status, ms)
