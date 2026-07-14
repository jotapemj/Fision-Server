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
    """Emite un token para un usuario ya autenticado."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "epoch": users.token_epoch(username),
        "iat": now,
        "exp": now + timedelta(hours=config.TOKEN_TTL_HOURS),
    }
    return jwt.encode(payload, config.SECRET, algorithm="HS256")


def verify(token):
    """Devuelve el username si el token es valido y vigente, si no None."""
    try:
        data = jwt.decode(token, config.SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None                    # firma mala, caducado, malformado...
    name = data.get("sub")
    if not name or not users.exists(name):
        return None                    # usuario borrado
    if data.get("epoch") != users.token_epoch(name):
        return None                    # revocado por cambio de password
    return name
