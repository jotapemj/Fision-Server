"""
Store de usuarios en disco (users.json). Un usuario:

    {
      "username": "valeria",
      "pwd_hash": "$argon2id$...",   # NUNCA la password en claro
      "token_epoch": 1,              # sube al cambiar pass -> mata tokens viejos
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
from pathlib import Path
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from . import config

_ph = PasswordHasher()
_dummy_hash = None              # señuelo: iguala el tiempo si el usuario no existe
_USER_RE = re.compile(r"^[a-z0-9_]{2,32}$")


def valid_username(name):
    return bool(name) and bool(_USER_RE.match(name))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- store ----
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


# ----------------------------------------------------------------- altas ----
def create(name, password):
    """Alta de usuario. Falla si el nombre no es valido o ya existe."""
    if not valid_username(name):
        raise ValueError("username invalido (usa [a-z0-9_], 2-32 chars)")
    if not password or len(password) < 8:
        raise ValueError("password muy corta (min 8)")
    data = _load()
    if name in data:
        raise ValueError(f"el usuario '{name}' ya existe")
    now = _now()
    data[name] = {
        "username": name,
        "pwd_hash": _ph.hash(password),
        "token_epoch": 1,
        "created_at": now,
        "updated_at": now,
    }
    _save(data)


def delete(name):
    data = _load()
    if name not in data:
        raise ValueError(f"el usuario '{name}' no existe")
    del data[name]
    _save(data)


# ------------------------------------------------------------ password ----
def verify(name, password):
    """True si la password casa. Rehashea de fondo si argon2 subio parametros."""
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
    try:
        if _ph.check_needs_rehash(u["pwd_hash"]):
            u["pwd_hash"] = _ph.hash(password)
            u["updated_at"] = _now()
            _save(data)
    except Exception:
        pass
    return True


def set_password(name, new_password):
    """Cambia la password y SUBE token_epoch -> invalida todos los tokens viejos."""
    if not new_password or len(new_password) < 8:
        raise ValueError("password muy corta (min 8)")
    data = _load()
    u = data.get(name)
    if not u:
        raise ValueError(f"el usuario '{name}' no existe")
    u["pwd_hash"] = _ph.hash(new_password)
    u["token_epoch"] = u.get("token_epoch", 1) + 1
    u["updated_at"] = _now()
    _save(data)
