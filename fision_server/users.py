"""
Store de usuarios en disco (users.json). Un usuario:

    {
      "username": "valeria",
      "pwd_hash": "$argon2id$...",   # NUNCA la password en claro
      "token_epoch": 1,              # sube al cambiar pass -> mata tokens viejos
      "role": "admin",               # opcional; sin campo = usuario normal
      "created_at": "...",
      "updated_at": "..."
    }

El username tambien sera el nombre de su carpeta en library/<user>/, asi que se
valida como slug seguro: [a-z0-9_], 2-32 chars. Cierra path traversal de raiz.

Alta/baja se hacen desde la CAJA (CLI). No hay registro publico.
"""
import os
import re
import json
import functools
import threading
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl                        # POSIX: lock entre procesos (server + CLI)
except ImportError:                     # Windows: solo el lock del proceso
    fcntl = None
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from . import config

_ph = PasswordHasher()
_dummy_hash = None              # señuelo: iguala el tiempo si el usuario no existe
# Verificaciones argon2 a la vez (cada una ~CPU + 64 MiB). Una avalancha de
# /login espera en cola en vez de saturar CPU y RAM.
_ARGON_SLOTS = threading.BoundedSemaphore(4)
_USER_RE = re.compile(r"^[a-z0-9_]{2,32}$")


def valid_username(name):
    return bool(name) and bool(_USER_RE.match(name))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- store ----
# Leer-modificar-guardar bajo lock: los hilos del server y los CLI (useradd,
# userpasswd...) escriben el mismo users.json; sin esto un cambio podia
# pisar a otro (p. ej. perderse un cierre de sesiones).
_rlock = threading.RLock()


@contextmanager
def _locked():
    with _rlock:
        if fcntl is None:
            yield
            return
        lock = Path(config.USERS_FILE).with_suffix(".lock")
        with open(lock, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def _rmw(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        with _locked():
            return fn(*a, **kw)
    return wrapper


def _load():
    p = Path(config.USERS_FILE)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    p = Path(config.USERS_FILE)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)                 # escritura atomica
    try:
        os.chmod(p, 0o600)             # hashes solo legibles por el dueño
    except OSError:
        pass


# --------------------------------------------------------------- lectura ----
def exists(name):
    return name in _load()


def get(name):
    return _load().get(name)


def list_users():
    return sorted(_load().keys())


def token_epoch(name):
    u = _load().get(name)
    return u.get("token_epoch", 1) if u else None


def is_admin(name):
    u = _load().get(name)
    return bool(u) and u.get("role") == "admin"


def admins():
    return sorted(n for n, u in _load().items() if u.get("role") == "admin")


@_rmw
def set_admin(name, flag):
    """Da o quita el rol admin. Nunca deja el servidor sin admin."""
    data = _load()
    u = data.get(name)
    if not u:
        raise ValueError(f"user '{name}' does not exist")
    if not flag and u.get("role") == "admin" and len(admins()) <= 1:
        raise ValueError("cannot remove the last administrator")
    if flag:
        u["role"] = "admin"
    else:
        u.pop("role", None)
    u["updated_at"] = _now()
    _save(data)


@_rmw
def revoke_sessions(name):
    """Sube token_epoch sin tocar la password: cierra todas sus sesiones."""
    data = _load()
    u = data.get(name)
    if not u:
        raise ValueError(f"user '{name}' does not exist")
    u["token_epoch"] = u.get("token_epoch", 1) + 1
    u["updated_at"] = _now()
    _save(data)


# ----------------------------------------------------------------- altas ----
@_rmw
def create(name, password, admin=False):
    """Alta de usuario. Falla si el nombre no es valido o ya existe."""
    if not valid_username(name):
        raise ValueError("invalid username (use [a-z0-9_], 2-32 characters)")
    if not password or len(password) < 8:
        raise ValueError("password too short (minimum 8 characters)")
    data = _load()
    if name in data:
        raise ValueError(f"user '{name}' already exists")
    now = _now()
    data[name] = {
        "username": name,
        "pwd_hash": _ph.hash(password),
        "token_epoch": 1,
        "created_at": now,
        "updated_at": now,
    }
    if admin:
        data[name]["role"] = "admin"
    _save(data)


@_rmw
def delete(name):
    data = _load()
    if name not in data:
        raise ValueError(f"user '{name}' does not exist")
    if data[name].get("role") == "admin" and len(admins()) <= 1:
        raise ValueError("cannot delete the last administrator")
    del data[name]
    _save(data)


# ------------------------------------------------------------ password ----
def verify(name, password):
    """True si la password casa. Rehashea de fondo si argon2 subio parametros."""
    with _ARGON_SLOTS:
        return _verify(name, password)


def _verify(name, password):
    data = _load()
    u = data.get(name)
    if not u:
        # Mismo coste argon2 que un usuario real: no revela si existe
        global _dummy_hash
        if _dummy_hash is None:
            _dummy_hash = _ph.hash(os.urandom(16).hex())
        try:
            _ph.verify(_dummy_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            pass
        return False
    try:
        _ph.verify(u["pwd_hash"], password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    # mantenimiento: si el hash quedo con params viejos, lo refrescamos
    # (relee bajo lock y solo si nadie cambio la password entretanto)
    try:
        old = u["pwd_hash"]
        if _ph.check_needs_rehash(old):
            new = _ph.hash(password)
            with _locked():
                data = _load()
                cur = data.get(name)
                if cur and cur.get("pwd_hash") == old:
                    cur["pwd_hash"] = new
                    cur["updated_at"] = _now()
                    _save(data)
    except Exception:
        pass
    return True


@_rmw
def set_password(name, new_password):
    """Cambia la password y SUBE token_epoch -> invalida todos los tokens viejos."""
    if not new_password or len(new_password) < 8:
        raise ValueError("password too short (minimum 8 characters)")
    data = _load()
    u = data.get(name)
    if not u:
        raise ValueError(f"user '{name}' does not exist")
    u["pwd_hash"] = _ph.hash(new_password)
    u["token_epoch"] = u.get("token_epoch", 1) + 1
    u["updated_at"] = _now()
    _save(data)
