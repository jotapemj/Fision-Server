"""
API HTTP de Fision. La app movil habla con estos endpoints.

  --- auth ---
  POST   /login                     user+password -> devuelve token (429 si abusa)
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
  DELETE /library/{hash}            borra la entrada (y la quita de tus playlists)

  --- playlists (data/playlists/<user>) ---
  GET/POST        /playlists              lista / crea
  GET/PUT/DELETE  /playlists/{id}         lee / sustituye / borra
  GET/PUT/DELETE  /playlists/{id}/cover   cover (como el avatar)

El usuario sale SIEMPRE del token (Depends(current_user)); nunca de la URL.
"""
import os
import json
import time
import tempfile

from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from contextlib import asynccontextmanager

from . import (config, library, jobs, users, security, log, avatars, gpu,
               ratelimit, playlists, __version__)
from .auth import current_user, bearer_token


@asynccontextmanager
async def lifespan(app):
    log.setup()
    gpu.detect()                      # una unica deteccion; el cache vive todo el arranque
    info = gpu.info()
    log.event("server.start", gpu=info["gpu"], gpu_name=info["gpu_name"],
              vram_total_mb=info["vram_total_mb"])
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


# --------------------------------------------------------------- server info ----
@app.get("/server/info")
def server_info(user: str = Depends(current_user)):
    """Info cacheada al arrancar + VRAM libre en vivo + version (SemVer)."""
    # vram_free_mb en vivo; vram_min_mb = umbral del gate, para el warning
    return {**gpu.info(),
            "vram_free_mb": gpu.vram_free_mb(),
            "vram_min_mb": config.GPU_MIN_VRAM_MB,
            "version": __version__}


# ------------------------------------------------------------------- auth ----
class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/login")
def login(body: LoginIn, request: Request):
    name = (body.username or "").strip().lower()
    ip = request.client.host if request.client else "-"
    wait = ratelimit.retry_after(ip, name)
    if wait:
        log.event("login.blocked", user=name or "-", ip=ip)
        raise HTTPException(status_code=429, detail="Demasiados intentos",
                            headers={"Retry-After": str(wait)})
    # verify() siempre: con usuario inexistente gasta lo mismo (hash señuelo)
    if users.verify(name, body.password):
        ratelimit.clear(ip, name)
        token = security.issue(name)
        log.event("login.success", user=name)
        return {"token": token, "user": name}
    ratelimit.fail(ip, name)
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


# ---------------------------------------------------------------- avatar ----
@app.get("/auth/avatar")
def avatar_get(user: str = Depends(current_user)):
    p = avatars.path_for(user)
    if not p:
        raise HTTPException(404, "Sin avatar")
    return FileResponse(p, media_type=avatars.media_type(p.suffix))


async def _save_image(file: UploadFile):
    """Sube una imagen a .incoming. Devuelve (ruta, ext real) o 413/400."""
    limit = config.MAX_AVATAR_MB * 1024 * 1024
    tmp = tempfile.NamedTemporaryFile(delete=False, dir=config.INCOMING_DIR)
    size = 0
    head = b""
    try:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            if not head:
                head = chunk[:16]                       # magic bytes vienen al principio
            size += len(chunk)
            if size > limit:
                tmp.close(); os.remove(tmp.name)
                raise HTTPException(413, "Imagen demasiado grande")
            tmp.write(chunk)
    finally:
        tmp.close()

    ext = avatars.sniff_ext(head)
    if not ext:
        os.remove(tmp.name)
        raise HTTPException(400, "Formato no soportado (usa JPEG/PNG/WebP)")
    return tmp.name, ext


@app.put("/auth/avatar")
async def avatar_put(file: UploadFile = File(...),
                     user: str = Depends(current_user)):
    tmp_name, ext = await _save_image(file)
    avatars.save(user, tmp_name, ext)
    log.event("avatar.set", user=user)
    return {"ok": True}


@app.delete("/auth/avatar")
def avatar_delete(user: str = Depends(current_user)):
    avatars.delete(user)
    log.event("avatar.delete", user=user)
    return {"ok": True}


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


# Formatos que decodifica el pipeline (ffmpeg). Resto -> 415 sin escribir nada.
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus",
              ".aiff", ".aif", ".wma"}


def _parse_cfg(config_json):
    """config_json -> dict. Basura o no-objeto -> 400 (antes reventaba en 500)."""
    try:
        cfg = json.loads(config_json or "{}")
    except ValueError:
        raise HTTPException(400, "config_json no es JSON valido")
    if not isinstance(cfg, dict):
        raise HTTPException(400, "config_json debe ser un objeto")
    return cfg


async def _save_upload(file: UploadFile):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in AUDIO_EXTS:
        raise HTTPException(415, "Formato de audio no soportado")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext,
                                      dir=config.INCOMING_DIR)
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
    cfg = _parse_cfg(config_json)
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
    cfg = _parse_cfg(config_json)
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
    n = playlists.remove_song(user, h)        # fuera de la nube = fuera de playlists
    if n:
        log.event("playlist.song_removed", user=user, hash=h[:12], playlists=n)
    return {"hash": h, "borrado": True}


# ------------------------------------------------------------- playlists ----
class PlaylistIn(BaseModel):
    title: str
    songs: list[str] = []


def _pl_error(e):
    # detail como siempre + code estable para traducir en el cliente
    return JSONResponse(status_code=400, content={"detail": str(e), "code": e.code})


def _require_pid(pid):
    if not playlists.valid_id(pid):
        raise HTTPException(404, "Playlist no encontrada")


@app.get("/playlists")
def pl_list(user: str = Depends(current_user)):
    return playlists.list_all(user)


@app.post("/playlists")
def pl_create(body: PlaylistIn, user: str = Depends(current_user)):
    try:
        p = playlists.create(user, body.title, body.songs)
    except playlists.PlaylistError as e:
        return _pl_error(e)
    log.event("playlist.create", user=user, id=p["id"], songs=p["count"])
    return p


@app.get("/playlists/{pid}")
def pl_get(pid: str, user: str = Depends(current_user)):
    _require_pid(pid)
    p = playlists.get(user, pid)
    if not p:
        raise HTTPException(404, "Playlist no encontrada")
    return p


@app.put("/playlists/{pid}")
def pl_update(pid: str, body: PlaylistIn, user: str = Depends(current_user)):
    _require_pid(pid)
    try:
        p = playlists.update(user, pid, body.title, body.songs)
    except playlists.PlaylistError as e:
        return _pl_error(e)
    if not p:
        raise HTTPException(404, "Playlist no encontrada")
    log.event("playlist.update", user=user, id=pid, songs=p["count"])
    return p


@app.delete("/playlists/{pid}")
def pl_delete(pid: str, user: str = Depends(current_user)):
    _require_pid(pid)
    if not playlists.delete(user, pid):
        raise HTTPException(404, "Playlist no encontrada")
    log.event("playlist.delete", user=user, id=pid)
    return {"ok": True}


@app.get("/playlists/{pid}/cover")
def pl_cover_get(pid: str, user: str = Depends(current_user)):
    _require_pid(pid)
    p = playlists.cover_path(user, pid)
    if not p:
        raise HTTPException(404, "Sin cover")
    return FileResponse(p, media_type=avatars.media_type(p.suffix))


@app.put("/playlists/{pid}/cover")
async def pl_cover_put(pid: str, file: UploadFile = File(...),
                       user: str = Depends(current_user)):
    _require_pid(pid)
    tmp_name, ext = await _save_image(file)
    if not playlists.save_cover(user, pid, tmp_name, ext):
        os.remove(tmp_name)
        raise HTTPException(404, "Playlist no encontrada")
    log.event("playlist.cover_set", user=user, id=pid)
    return {"ok": True}


@app.delete("/playlists/{pid}/cover")
def pl_cover_delete(pid: str, user: str = Depends(current_user)):
    _require_pid(pid)
    if not playlists.get(user, pid):
        raise HTTPException(404, "Playlist no encontrada")
    playlists.delete_cover(user, pid)
    log.event("playlist.cover_delete", user=user, id=pid)
    return {"ok": True}


# ---------------------------------------------------------------- lyrics ----
class LyricsIn(BaseModel):
    language: str | None = None                    # ISO code o None (auto)


@app.post("/library/{h}/lyrics")
def lyrics_start(h: str, body: LyricsIn, user: str = Depends(current_user)):
    """Encola un job de transcripcion Whisper sobre vocals.flac vigente.

    409 si vocals no existe todavia (job principal en marcha), o si ya hay
    otro job de lyrics activo para el mismo hash.
    """
    _require_hash(h)
    lib = library.for_user(user)
    if not lib.exists(h):
        raise HTTPException(404, "No existe")
    vocals = lib.stems_dir(h) / "vocals.flac"
    if not vocals.exists():
        raise HTTPException(409, "vocals.flac no disponible todavia; espera a "
                                 "que termine el job principal")
    if jobs.has_active_lyrics(user, h):
        raise HTTPException(409, "Ya hay un job de lyrics en curso para esta cancion")

    lang = (body.language or "").strip() or None
    m = lib.read_meta(h) or {}
    job = jobs.create_job(user, h, m.get("nombre", h),
                          {"language": lang}, reprocess=False, kind="lyrics")
    return {"job_id": job["job_id"], "hash": h, "estado": job["estado"]}


@app.get("/library/{h}/lyrics")
def lyrics_get(h: str, user: str = Depends(current_user)):
    _require_hash(h)
    p = library.for_user(user).stems_dir(h) / "lyrics.json"
    if not p.exists():
        raise HTTPException(404, "Sin lyrics")
    return FileResponse(p, media_type="application/json")


@app.delete("/library/{h}/lyrics")
def lyrics_delete(h: str, user: str = Depends(current_user)):
    _require_hash(h)
    p = library.for_user(user).stems_dir(h) / "lyrics.json"
    if p.exists():
        try:
            p.unlink()
        except OSError as e:
            raise HTTPException(500, str(e))
    return {"hash": h, "ok": True}
