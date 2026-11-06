"""
Tokens de sesion. JWT firmado (HS256) con la clave de config.SECRET.

El token lleva:
    sub   -> username
    epoch -> token_epoch del usuario en el momento de emitir
    iat/exp

Al verificar, ademas de la firma y la caducidad, comparamos el epoch con el
actual del usuario. Si el usuario cambio la password (epoch subio), el token
viejo deja de valer. Eso es la revocacion al cambiar contraseña.
"""
from datetime import datetime, timedelta, timezone

import jwt

from . import config, users


def issue(username):
    """Emite un token para un usuario ya autenticado -> (token, exp datetime)."""
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=config.TOKEN_TTL_HOURS)
    payload = {
        "sub": username,
        "epoch": users.token_epoch(username),
        "iat": now,
        "exp": exp,
    }
    return jwt.encode(payload, config.SECRET, algorithm="HS256"), exp


def check(token):
    """(username, None) si vale; (sub o None, motivo) si no.

    Motivos: expired, bad_token (firma mala o malformado), unknown_user
    (usuario borrado), revoked (cambio de password).
    """
    try:
        data = jwt.decode(token, config.SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        # firma buena pero vencido: sacamos el usuario solo para el log
        try:
            data = jwt.decode(token, config.SECRET, algorithms=["HS256"],
                              options={"verify_exp": False})
            return data.get("sub"), "expired"
        except (jwt.PyJWTError, RecursionError):
            return None, "bad_token"
    except (jwt.PyJWTError, RecursionError):
        # RecursionError: JSON muy anidado (PyJWT viejo no lo envuelve)
        return None, "bad_token"
    name = data.get("sub")
    if not name or not users.exists(name):
        return name, "unknown_user"
    if data.get("epoch") != users.token_epoch(name):
        return name, "revoked"
    return name, None


def verify(token):
    """Devuelve el username si el token es valido y vigente, si no None."""
    name, reason = check(token)
    return None if reason else name
