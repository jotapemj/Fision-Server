"""
Auth simple por token Bearer. La app movil manda en cada peticion:

    Authorization: Bearer <FISION_TOKEN>

Defensa en profundidad: aunque el servidor este detras de Tailscale, Cloudflare
Access o WireGuard, el endpoint pide SU token. No dependemos de la capa de red.
"""
from fastapi import Header, HTTPException

from . import config


async def require_token(authorization: str = Header(default="")):
    if not config.API_TOKEN:
        return                         # modo abierto (solo pruebas locales)
    if authorization != f"Bearer {config.API_TOKEN}":
        raise HTTPException(status_code=401, detail="Token invalido")
