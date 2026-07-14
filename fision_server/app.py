"""
API HTTP de Fision. La app movil habla con estos endpoints.

  --- auth ---
  POST   /login                     user+password -> devuelve token
  POST   /auth/change-password      cambia tu password (token vigente)

  --- trabajo (todo bajo TU biblioteca: library/<user>/) ---
  POST   /jobs                      sube audio + config -> encola (o cache hit)
  POST   /library/{hash}/reprocess  reprocesa el input guardado con nueva config
  GET    /jobs                      cola viva (solo tus jobs, posicion global)
  GET    /jobs/{job_id}             estado del trabajo (solo si es tuyo)
  DELETE /jobs/{job_id}             cancela (cola o en marcha, solo si es tuyo)
  GET    /library                   lista de TUS canciones
  GET    /library/{hash}            meta de una cancion tuya
  GET    /library/{hash}/zip        descarga tu result.zip vigente
  GET    /library/{hash}/stems/{f}  sirve un fichero de tus stems (portada/audio)
  DELETE /library/{hash}            borra la entrada

El usuario sale SIEMPRE del token (Depends(current_user)); nunca de la URL.
"""
import os
import json
import time
import tempfile

from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from contextlib import asynccontextmanager

from . import config, library, jobs, users, security, log
from .auth import current_user, bearer_token


@asynccontextmanager
async def lifespan(app):
    log.setup()
    log.event("server.start")
    jobs.recover()                    # recupera cola y limpia huerfanas tras caida
    jobs.start_worker()
    yield


app = FastAPI(title="Fision AI Server", lifespan=lifespan)


# --------------------------------------------------------- access logging ----
@app.middleware("http")
async def access_log(request: Request, call_next):
    start = time.perf_counter()
    user = "-"
    token = bearer_token(request.headers.get("authorization", ""))
    if token:
        u = security.verify(token)
        if u:
            user = u
    response = await call_next(request)
    ms = (time.perf_counter() - start) * 1000
    log.access_line(request.method, request.url.path, response.status_code, user, ms)
    return response


@app.get("/health")
def health():
    return {"status": "ok"}


# ------------------------------------------------------------------- auth ----
class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/login")
def login(body: LoginIn):
    name = (body.username or "").strip().lower()
    if users.exists(name) and users.verify(name, body.password):
        token = security.issue(name)
        log.event("login.success", user=name)
        return {"token": token, "user": name}
    log.event("login.fail", user=name or "-", reason="bad_credentials")
    raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")


class ChangePwIn(BaseModel):
    old_password: str
    new_password: str


@app.post("/auth/change-password")
def change_password(body: ChangePwIn, user: str = Depends(current_user)):
    if not users.verify(user, body.old_password):
        log.event("password_change.fail", user=user, reason="bad_old_password")
        raise HTTPException(status_code=403, detail="Contraseña actual incorrecta")
    try:
        users.set_password(user, body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    token = security.issue(user)                  # el cambio mato el token viejo
    log.event("password_change.success", user=user)
    return {"ok": True, "token": token}


# ---------------------------------------------------------------- helpers ----
def _public_meta(m):
    return {"hash": m.get("hash"), "nombre": m.get("nombre"),
            "title": m.get("title"), "artist": m.get("artist"),
            "cover_url": m.get("cover_url"),
            "estado": m.get("estado"), "config": m.get("config"),
            "created_at": m.get("created_at"),
            "updated_at": m.get("updated_at")}


def _require_hash(h):
    if not library.valid_hash(h):
        raise HTTPException(404, "No existe")


async def _save_upload(file: UploadFile):
    ext = os.path.splitext(file.filename or "audio")[1].lower() or ".bin"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    size = 0
    try:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > config.MAX_UPLOAD_MB * 1024 * 1024:
                tmp.close(); os.remove(tmp.name)
                raise HTTPException(413, "Archivo demasiado grande")
            tmp.write(chunk)
    finally:
        tmp.close()
    return tmp.name, ext


# -------------------------------------------------------------------- jobs ----
@app.post("/jobs")
async def submit(file: UploadFile = File(...), config_json: str = Form("{}"),
                 user: str = Depends(current_user)):
    lib = library.for_user(user)
    cfg = json.loads(config_json or "{}")
    tmp_path, ext = await _save_upload(file)
    h = library.hash_file(tmp_path)

    meta = lib.read_meta(h)                        # cache hit dentro de SU biblioteca
    if meta and meta.get("estado") == "listo" and meta.get("config") == cfg:
        os.remove(tmp_path)
        return {"hash": h, "estado": "listo", "cached": True}

    lib.save_input(h, tmp_path, ext)
    job = jobs.create_job(user, h, file.filename or h, cfg, reprocess=bool(meta))
    return {"hash": h, "job_id": job["job_id"],
            "estado": job["estado"], "cached": False}


@app.post("/library/{h}/reprocess")
async def reprocess(h: str, config_json: str = Form("{}"),
                    user: str = Depends(current_user)):
    _require_hash(h)
    lib = library.for_user(user)
    if not lib.exists(h) or lib.input_path(h) is None:
        raise HTTPException(404, "No existe ese audio")
    cfg = json.loads(config_json or "{}")
    meta = lib.read_meta(h) or {}
    job = jobs.create_job(user, h, meta.get("nombre", h), cfg, reprocess=True)
    return {"hash": h, "job_id": job["job_id"], "estado": job["estado"]}


@app.get("/jobs")
def jobs_active(user: str = Depends(current_user)):
    return jobs.list_active(user)


@app.get("/jobs/{jid}")
def job_status(jid: str, user: str = Depends(current_user)):
    job = jobs.get_job(jid, user)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    return {"job_id": jid, "hash": job["hash"], "estado": job["estado"],
            "posicion": jobs.queue_position(jid, user), "error": job["error"]}


@app.delete("/jobs/{jid}")
def job_cancel(jid: str, user: str = Depends(current_user)):
    if not jobs.cancel_job(jid, user):
        raise HTTPException(404, "No cancelable")
    return {"job_id": jid, "estado": "cancelled"}


# ----------------------------------------------------------------- library ----
@app.get("/library")
def lib_list(user: str = Depends(current_user)):
    return [_public_meta(m) for m in library.for_user(user).list_entries()]


@app.get("/library/{h}")
def lib_get(h: str, user: str = Depends(current_user)):
    _require_hash(h)
    m = library.for_user(user).read_meta(h)
    if not m:
        raise HTTPException(404, "No existe")
    return _public_meta(m)


@app.get("/library/{h}/zip")
def lib_zip(h: str, user: str = Depends(current_user)):
    _require_hash(h)
    lib = library.for_user(user)
    if not lib.zip_path(h).exists():
        raise HTTPException(404, "Sin resultado listo")
    m = lib.read_meta(h) or {}
    base = os.path.splitext(m.get("nombre") or h)[0]
    return FileResponse(lib.zip_path(h), media_type="application/zip",
                        filename=f"{base}_stems.zip")


@app.get("/library/{h}/stems/{filename}")
def lib_stem_file(h: str, filename: str, user: str = Depends(current_user)):
    _require_hash(h)
    if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
        raise HTTPException(400, "Nombre invalido")
    base = library.for_user(user).stems_dir(h).resolve()
    target = (base / filename).resolve()
    if target.parent != base or not target.is_file():
        raise HTTPException(404, "No existe")
    return FileResponse(target)


@app.delete("/library/{h}")
def lib_delete(h: str, user: str = Depends(current_user)):
    _require_hash(h)
    lib = library.for_user(user)
    if not lib.exists(h):
        raise HTTPException(404, "No existe")
    lib.delete_entry(h)
    return {"hash": h, "borrado": True}
