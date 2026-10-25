"""
API HTTP de Fision. La app movil habla con estos endpoints.

  --- auth ---
  POST   /login                     user+password -> devuelve token (429 si abusa)
  POST   /auth/refresh              token vigente -> token nuevo (sesion deslizante)
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

  --- admin (admin.py; solo redes ADMIN_ALLOW + role=admin) ---
  GET  /admin                    pagina del panel
  ...  /admin/api/...            usuarios, bibliotecas (metadatos), trabajos,
                                 auditoria, configuracion (solo lectura)

El usuario sale SIEMPRE del token (Depends(current_user)); nunca de la URL.
"""
import os
import re
import json
import time
import tempfile

from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from contextlib import asynccontextmanager

from . import (config, library, jobs, users, security, log, avatars, gpu,
               ratelimit, playlists, admin, limits, __version__)
from .auth import current_user, bearer_token


@asynccontextmanager
async def lifespan(app):
    log.setup()
    # Spool del multipart al disco de la biblioteca, nunca a /tmp (tmpfs = RAM)
    tempfile.tempdir = str(config.INCOMING_DIR)
    gpu.detect()                      # una unica deteccion; el cache vive todo el arranque
    info = gpu.info()
    log.event("server.start", gpu=info["gpu"], gpu_name=info["gpu_name"],
              vram_total_mb=info["vram_total_mb"])
    gpu.start_probe()                 # torch/Whisper ven CUDA? (segundo plano)
    if not users.admins():
        # sin admin el panel /admin no tiene dueño: avisar como crearlo
        log.event("admin.missing",
                  hint="python -m fision_server.usermod <user> --admin")
    jobs.recover()                    # recupera cola y limpia huerfanas tras caida
    jobs.start_worker()
    yield


# /docs, /redoc y /openapi.json listan toda la API: apagados salvo FISION_API_DOCS=1
_docs = config.API_DOCS
app = FastAPI(title="Fision AI Server", lifespan=lifespan,
              docs_url="/docs" if _docs else None,
              redoc_url="/redoc" if _docs else None,
              openapi_url="/openapi.json" if _docs else None)
app.include_router(admin.router)          # /admin (pagina) + /admin/api/...


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
    # El navegador no adivina tipos (portadas de tags arbitrarias) y el panel
    # no se deja enmarcar en otra web
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    if request.url.path.startswith("/admin"):
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
    ms = (time.perf_counter() - start) * 1000
    log.access_line(request.method, request.url.path, response.status_code, user, ms)
    return response


# Tope de cuerpo y token de subidas ANTES de leer nada. Va fuera del access
# log (anadido despues = capa exterior): corta sin tocar el resto de la pila.
app.add_middleware(limits.BodyLimit)


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
    wait, scope = ratelimit.retry_after(ip, name)
    if wait:
        log.event("login.blocked", user=name or "-", ip=ip, scope=scope)
        raise HTTPException(status_code=429, detail="Too many attempts",
                            headers={"Retry-After": str(wait)})
    # verify() siempre: con usuario inexistente gasta lo mismo (hash señuelo)
    if users.verify(name, body.password):
        ratelimit.clear(ip, name)
        token, exp = security.issue(name)
        log.event("login.success", user=name)
        return {"token": token, "user": name, "expires_at": exp.isoformat()}
    ratelimit.fail(ip, name)
    log.event("login.fail", user=name or "-", reason="bad_credentials")
    raise HTTPException(status_code=401, detail="Wrong username or password")


@app.post("/auth/refresh")
def refresh(user: str = Depends(current_user)):
    """Token vigente -> uno nuevo con TTL completo. El cliente lo llama al
    abrir la app: si la usa al menos una vez por TTL, nunca caduca. Password
    cambiada o usuario borrado siguen matando el token (current_user)."""
    token, exp = security.issue(user)
    log.event("token.refresh", user=user)
    return {"token": token, "user": user, "expires_at": exp.isoformat()}


class ChangePwIn(BaseModel):
    old_password: str
    new_password: str


@app.post("/auth/change-password")
def change_password(body: ChangePwIn, user: str = Depends(current_user)):
    if not users.verify(user, body.old_password):
        log.event("password_change.fail", user=user, reason="bad_old_password")
        raise HTTPException(status_code=403, detail="Wrong current password")
    try:
        users.set_password(user, body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    token, exp = security.issue(user)             # el cambio mato el token viejo
    log.event("password_change.success", user=user)
    return {"ok": True, "token": token, "expires_at": exp.isoformat()}


# ---------------------------------------------------------------- avatar ----
@app.get("/auth/avatar")
def avatar_get(user: str = Depends(current_user)):
    p = avatars.path_for(user)
    if not p:
        raise HTTPException(404, "No avatar")
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
                raise HTTPException(413, "Image too large")
            tmp.write(chunk)
    finally:
        tmp.close()

    ext = avatars.sniff_ext(head)
    if not ext:
        os.remove(tmp.name)
        raise HTTPException(400, "Unsupported format (use JPEG, PNG or WebP)")
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
    return {"hash": m.get("hash"), "filename": m.get("filename"),
            "title": m.get("title"), "artist": m.get("artist"),
            "cover_url": m.get("cover_url"),
            "state": m.get("state"), "config": m.get("config"),
            "created_at": m.get("created_at"),
            "updated_at": m.get("updated_at")}


def _require_hash(h):
    if not library.valid_hash(h):
        raise HTTPException(404, "Not found")


# Formatos que decodifica el pipeline (ffmpeg). Resto -> 415 sin escribir nada.
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus",
              ".aiff", ".aif", ".wma"}


def _parse_cfg(config_json):
    """config_json -> dict. Basura o no-objeto -> 400 (antes reventaba en 500)."""
    try:
        cfg = json.loads(config_json or "{}")
    except ValueError:
        raise HTTPException(400, "config_json is not valid JSON")
    if not isinstance(cfg, dict):
        raise HTTPException(400, "config_json must be an object")
    return cfg


async def _save_upload(file: UploadFile):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in AUDIO_EXTS:
        raise HTTPException(415, "Unsupported audio format")
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
                raise HTTPException(413, "File too large")
            tmp.write(chunk)
    finally:
        tmp.close()
    return tmp.name, ext


# -------------------------------------------------------------------- jobs ----
def _job_running(live):
    return JSONResponse(status_code=409, content={
        "detail": "The song is already being processed", "code": "job_running",
        "job_id": live["job_id"]})


def _job_limit_hit(user):
    """429 con code estable si el usuario ya tiene el maximo de jobs vivos."""
    lim = config.MAX_JOBS_PER_USER
    if lim > 0 and jobs.count_active(user) >= lim:
        log.event("job.limit", user=user, limit=lim)
        return JSONResponse(status_code=429, content={
            "detail": f"At most {lim} queued jobs per user", "code": "job_limit"})
    return None


def _audio_seconds(path):
    """Duracion por cabecera (mutagen), o None si no se puede leer."""
    try:
        from mutagen import File as _MFile
        m = _MFile(path)
        # ojo: un FileType sin tags es "falso"; mirar None, no la verdad
        return float(m.info.length) if m is not None and m.info.length else None
    except Exception:
        return None


def _enqueue_upload(user, tmp_path, ext, filename, cfg):
    """Hash + cache + encolado. Sync: va al threadpool (hashear 200 MB en el
    bucle de eventos congelaba el server para todos)."""
    lib = library.for_user(user)
    if os.path.getsize(tmp_path) == 0:
        os.remove(tmp_path)
        return JSONResponse(status_code=400, content={
            "detail": "Empty file", "code": "audio_empty"})
    secs = _audio_seconds(tmp_path)
    if secs is not None and secs < config.MIN_AUDIO_SEC:
        # Sin cabecera legible se deja pasar: decide el pipeline
        os.remove(tmp_path)
        return JSONResponse(status_code=400, content={
            "detail": f"Audio too short (minimum {config.MIN_AUDIO_SEC} s)",
            "code": "audio_too_short"})
    h = library.hash_file(tmp_path)
    meta = lib.read_meta(h)                        # cache hit dentro de SU biblioteca
    if meta and meta.get("state") == "ready" and meta.get("config") == cfg:
        os.remove(tmp_path)
        return {"hash": h, "state": "ready", "cached": True}
    live = jobs.find_active(user, h)
    if live:
        # Ya se esta procesando: misma config -> el mismo job; otra -> 409
        os.remove(tmp_path)
        if live["config"] == cfg:
            return {"hash": h, "job_id": live["job_id"],
                    "state": live["state"], "cached": False}
        return _job_running(live)
    limited = _job_limit_hit(user)
    if limited:
        os.remove(tmp_path)
        return limited
    lib.save_input(h, tmp_path, ext)
    job = jobs.create_job(user, h, filename or h, cfg, reprocess=bool(meta))
    return {"hash": h, "job_id": job["job_id"],
            "state": job["state"], "cached": False}


@app.post("/jobs")
async def submit(file: UploadFile = File(...), config_json: str = Form("{}"),
                 user: str = Depends(current_user)):
    cfg = _parse_cfg(config_json)
    tmp_path, ext = await _save_upload(file)
    return await run_in_threadpool(_enqueue_upload, user, tmp_path, ext,
                                   file.filename, cfg)


@app.post("/library/{h}/reprocess")
def reprocess(h: str, config_json: str = Form("{}"),
              user: str = Depends(current_user)):
    _require_hash(h)
    lib = library.for_user(user)
    if not lib.exists(h) or lib.input_path(h) is None:
        raise HTTPException(404, "Audio not found")
    cfg = _parse_cfg(config_json)
    live = jobs.find_active(user, h)
    if live:
        return _job_running(live)
    limited = _job_limit_hit(user)
    if limited:
        return limited
    meta = lib.read_meta(h) or {}
    job = jobs.create_job(user, h, meta.get("filename", h), cfg, reprocess=True)
    return {"hash": h, "job_id": job["job_id"], "state": job["state"]}


@app.get("/jobs")
def jobs_active(user: str = Depends(current_user)):
    return jobs.list_active(user)


@app.get("/jobs/{jid}")
def job_status(jid: str, user: str = Depends(current_user)):
    job = jobs.get_job(jid, user)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"job_id": jid, "hash": job["hash"], "state": job["state"],
            "position": jobs.queue_position(jid, user), "error": job["error"],
            "error_code": job["error_code"]}


@app.delete("/jobs/{jid}")
def job_cancel(jid: str, user: str = Depends(current_user)):
    if not jobs.cancel_job(jid, user):
        raise HTTPException(404, "Job not found or not cancellable")
    return {"job_id": jid, "state": "cancelled"}


# ----------------------------------------------------------------- library ----
@app.get("/library")
def lib_list(user: str = Depends(current_user)):
    return [_public_meta(m) for m in library.for_user(user).list_entries()]


@app.get("/library/{h}")
def lib_get(h: str, user: str = Depends(current_user)):
    _require_hash(h)
    m = library.for_user(user).read_meta(h)
    if not m:
        raise HTTPException(404, "Not found")
    return _public_meta(m)


@app.get("/library/{h}/zip")
def lib_zip(h: str, user: str = Depends(current_user)):
    _require_hash(h)
    lib = library.for_user(user)
    if not lib.zip_path(h).exists():
        raise HTTPException(404, "No result ready")
    m = lib.read_meta(h) or {}
    base = os.path.splitext(m.get("filename") or h)[0]
    return FileResponse(lib.zip_path(h), media_type="application/zip",
                        filename=f"{base}_stems.zip")


@app.get("/library/{h}/stems/{filename}")
def lib_stem_file(h: str, filename: str, user: str = Depends(current_user)):
    _require_hash(h)
    if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
        raise HTTPException(400, "Invalid file name")
    base = library.for_user(user).stems_dir(h).resolve()
    target = (base / filename).resolve()
    if target.parent != base or not target.is_file():
        raise HTTPException(404, "Not found")
    if filename == "artcover.png":
        # Se llama .png pero son los bytes de la tag tal cual (a menudo JPEG)
        with open(target, "rb") as f:
            ext = avatars.sniff_ext(f.read(16))
        return FileResponse(target, media_type=avatars.media_type(ext) if ext
                            else "application/octet-stream")
    return FileResponse(target)


@app.delete("/library/{h}")
def lib_delete(h: str, user: str = Depends(current_user)):
    _require_hash(h)
    lib = library.for_user(user)
    if not lib.exists(h):
        raise HTTPException(404, "Not found")
    # Antes de borrar: fuera sus jobs, y espera a que el worker suelte el que
    # corre (si no, al fallar reescribia meta.json y la cancion "volvia")
    if not jobs.cancel_matching(user, h):
        raise HTTPException(409, "The song is still being processed; try again")
    lib.delete_entry(h)
    n = playlists.remove_song(user, h)        # fuera de la nube = fuera de playlists
    if n:
        log.event("playlist.song_removed", user=user, hash=h[:12], playlists=n)
    return {"hash": h, "deleted": True}


# ------------------------------------------------------------- playlists ----
class PlaylistIn(BaseModel):
    title: str
    songs: list[str] = []


def _pl_error(e):
    # detail como siempre + code estable para traducir en el cliente
    return JSONResponse(status_code=400, content={"detail": str(e), "code": e.code})


def _require_pid(pid):
    if not playlists.valid_id(pid):
        raise HTTPException(404, "Playlist not found")


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
        raise HTTPException(404, "Playlist not found")
    return p


@app.put("/playlists/{pid}")
def pl_update(pid: str, body: PlaylistIn, user: str = Depends(current_user)):
    _require_pid(pid)
    try:
        p = playlists.update(user, pid, body.title, body.songs)
    except playlists.PlaylistError as e:
        return _pl_error(e)
    if not p:
        raise HTTPException(404, "Playlist not found")
    log.event("playlist.update", user=user, id=pid, songs=p["count"])
    return p


@app.delete("/playlists/{pid}")
def pl_delete(pid: str, user: str = Depends(current_user)):
    _require_pid(pid)
    if not playlists.delete(user, pid):
        raise HTTPException(404, "Playlist not found")
    log.event("playlist.delete", user=user, id=pid)
    return {"ok": True}


@app.get("/playlists/{pid}/cover")
def pl_cover_get(pid: str, user: str = Depends(current_user)):
    _require_pid(pid)
    p = playlists.cover_path(user, pid)
    if not p:
        raise HTTPException(404, "No cover")
    return FileResponse(p, media_type=avatars.media_type(p.suffix))


@app.put("/playlists/{pid}/cover")
async def pl_cover_put(pid: str, file: UploadFile = File(...),
                       user: str = Depends(current_user)):
    _require_pid(pid)
    tmp_name, ext = await _save_image(file)
    if not playlists.save_cover(user, pid, tmp_name, ext):
        os.remove(tmp_name)
        raise HTTPException(404, "Playlist not found")
    log.event("playlist.cover_set", user=user, id=pid)
    return {"ok": True}


@app.delete("/playlists/{pid}/cover")
def pl_cover_delete(pid: str, user: str = Depends(current_user)):
    _require_pid(pid)
    if not playlists.get(user, pid):
        raise HTTPException(404, "Playlist not found")
    playlists.delete_cover(user, pid)
    log.event("playlist.cover_delete", user=user, id=pid)
    return {"ok": True}


# ---------------------------------------------------------------- lyrics ----
_LANG_RE = re.compile(r"^[a-z]{2,3}$")       # ISO 639-1/-3, como espera Whisper


class LyricsIn(BaseModel):
    language: str | None = None                    # ISO code o None (auto)


@app.post("/library/{h}/lyrics")
def lyrics_start(h: str, body: LyricsIn, user: str = Depends(current_user)):
    """Encola un job de transcripcion Whisper sobre vocals.flac vigente.

    409 si vocals no existe todavia (job principal en marcha), o si ya hay
    otro job de lyrics activo para el mismo hash.
    """
    _require_hash(h)
    lang = (body.language or "").strip().lower() or None
    if lang == "auto":
        lang = None
    if lang and not _LANG_RE.match(lang):
        # va como argumento al CLI: solo codigos ISO, nada que parezca una opcion
        raise HTTPException(400, "language must be an ISO code (es, en...) or auto")
    lib = library.for_user(user)
    if not lib.exists(h):
        raise HTTPException(404, "Not found")
    vocals = lib.stems_dir(h) / "vocals.flac"
    if not vocals.exists():
        raise HTTPException(409, "vocals.flac not available yet; wait for "
                                 "the main job to finish")
    if jobs.has_active_lyrics(user, h):
        raise HTTPException(409, "A lyrics job is already running for this song")

    limited = _job_limit_hit(user)
    if limited:
        return limited
    m = lib.read_meta(h) or {}
    job = jobs.create_job(user, h, m.get("filename", h),
                          {"language": lang}, reprocess=False, kind="lyrics")
    return {"job_id": job["job_id"], "hash": h, "state": job["state"]}


@app.get("/library/{h}/lyrics")
def lyrics_get(h: str, user: str = Depends(current_user)):
    _require_hash(h)
    p = library.for_user(user).stems_dir(h) / "lyrics.json"
    if not p.exists():
        raise HTTPException(404, "No lyrics")
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
