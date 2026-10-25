"""
Tope de cuerpo por peticion, ANTES de que FastAPI lo lea.

FastAPI parsea el formulario entero antes de resolver Depends(current_user):
sin esto, cualquiera sin cuenta podia subir GBs (al spool de /tmp) y el tope
de MAX_UPLOAD_MB solo se miraba con el fichero ya recibido.

- Rutas de subida (multipart): sin token valido -> 401 sin leer el cuerpo.
- Content-Length mayor que el tope -> 413 al momento.
- Sin Content-Length (chunked): se cuentan bytes y se corta al pasarse.
- Resto de peticiones con cuerpo (JSON): tope de 1 MiB.
"""
import gc
import re
import json

from starlette.formparsers import MultiPartException

from . import config, security, log
from .auth import bearer_token

_MIB = 1 << 20
_MARGIN = _MIB                  # cabeceras multipart + campos de texto

# (metodo, ruta) -> tope en bytes. Todas exigen token antes de leer.
_UPLOADS = [
    ("POST", re.compile(r"^/jobs$"), lambda: config.MAX_UPLOAD_MB * _MIB + _MARGIN),
    ("POST", re.compile(r"^/library/[^/]+/reprocess$"), lambda: _MARGIN),
    ("PUT", re.compile(r"^/auth/avatar$"), lambda: config.MAX_AVATAR_MB * _MIB + _MARGIN),
    ("PUT", re.compile(r"^/playlists/[^/]+/cover$"), lambda: config.MAX_AVATAR_MB * _MIB + _MARGIN),
]
_DEFAULT = _MIB


class _TooLarge(MultiPartException):
    """Subclase de MultiPartException: asi el parser de starlette cierra (y
    borra) los temporales del spool al cortar; con otra excepcion quedaban
    abiertos (200 MB por subida cortada)."""


def _rule(method, path):
    for m, rx, limit in _UPLOADS:
        if m == method and rx.match(path):
            return limit(), True
    return _DEFAULT, False


async def _reply(send, status, detail):
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})


class BodyLimit:
    """Middleware ASGI puro (no BaseHTTPMiddleware: no lee el cuerpo)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)

        method, path = scope["method"], scope["path"]
        limit, needs_auth = _rule(method, path)
        headers = dict(scope["headers"])

        if needs_auth:
            token = bearer_token(headers.get(b"authorization", b"").decode("latin-1"))
            user, reason = security.check(token) if token else (None, "missing")
            if reason:
                log.event("upload.reject", reason=reason, path=path, user=user or "-")
                return await _reply(send, 401, "Not authenticated")

        cl = headers.get(b"content-length")
        if cl is not None:
            try:
                declared = int(cl)
            except ValueError:
                return await _reply(send, 400, "Invalid Content-Length")
            if declared > limit:
                log.event("upload.too_large", path=path, bytes=declared, limit=limit)
                return await _reply(send, 413, "Request too large")

        state = {"bytes": 0, "over": False, "sent": False}

        async def limited_receive():
            msg = await receive()
            if msg["type"] == "http.request":
                state["bytes"] += len(msg.get("body", b""))
                if state["bytes"] > limit:
                    state["over"] = True
                    raise _TooLarge()
            return msg

        async def guarded_send(msg):
            # Pasado el tope, la respuesta de la app (400 al parsear) se cambia por 413
            if state["over"]:
                if not state["sent"]:
                    state["sent"] = True
                    await _reply(send, 413, "Request too large")
                return
            if msg["type"] == "http.response.start":
                state["sent"] = True
            await send(msg)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _TooLarge:
            pass
        if state["over"]:
            # El traceback del corte retiene el spool hasta el siguiente GC:
            # recoger ya para soltar el temporal (caso raro, coste nulo)
            gc.collect()
            log.event("upload.too_large", path=path, bytes=state["bytes"], limit=limit)
            if not state["sent"]:
                state["sent"] = True
                await _reply(send, 413, "Request too large")
