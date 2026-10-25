"""
Auth de las peticiones. El cliente manda en cada llamada protegida:

    Authorization: Bearer <token>

El token lo emite /login (user+password). Aqui solo lo verificamos y sacamos
el usuario. REGLA DE ORO: el usuario sale del TOKEN, nunca de la URL ni del body.

Uso en endpoints:
    @app.get("/library")
    def lib_list(user: str = Depends(current_user)):
        ...   # 'user' es de fiar; ya esta autenticado
"""
from fastapi import Header, HTTPException, Depends

from . import security, log


def bearer_token(authorization: str = Header(default="")):
    """Extrae el token del header, o None si no viene bien formado."""
    if not authorization.startswith("Bearer "):
        return None
    tok = authorization[7:].strip()
    return tok or None


async def current_user(authorization: str = Header(default="")) -> str:
    """Devuelve el username autenticado. 401 si el token falta o no vale."""
    token = bearer_token(authorization)
    if not token:
        log.event("auth.reject", reason="missing", user="-")
        raise HTTPException(status_code=401, detail="Not authenticated")
    user, reason = security.check(token)
    if reason:
        # el motivo diagnostica sin adivinar (caducado, revocado...)
        log.event("auth.reject", reason=reason, user=user or "-")
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user
