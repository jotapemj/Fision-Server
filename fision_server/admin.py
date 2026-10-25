"""
Panel de administracion: pagina en /admin y API en /admin/api/...

Doble cerrojo:
  1. Red: solo responde a config.ADMIN_ALLOW (localhost, redes privadas y
     Tailscale por defecto). Fuera de ahi 403, aunque el token sea bueno.
  2. Rol: token de un usuario con role=admin (mismo /login que la app).

El admin ve METADATOS de las bibliotecas ajenas (titulo, estado, tamaño),
nunca descarga audio. La configuracion es de solo lectura: viene del entorno.
"""
import os
import re
import json
import time
import shutil
import secrets
import ipaddress
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import (config, users, library, jobs, playlists, avatars, gpu, log,
               accounts, __version__)
from .auth import current_user

_STARTED = time.time()
_PAGE_DIR = Path(__file__).parent / "admin_ui"
_STATIC_RE = re.compile(r"^[a-zA-Z0-9_.-]+\.(woff2|svg|png|txt|js)$")

_NETS = []
for _n in config.ADMIN_ALLOW.split(","):
    if _n.strip():
        _NETS.append(ipaddress.ip_network(_n.strip(), strict=False))


# ---------------------------------------------------------------- cerrojos ----
def _client_ip(request):
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    return ip.ipv4_mapped or ip if ip.version == 6 else ip


def network_gate(request: Request):
    """Fuera de las redes permitidas el panel no existe (403 sin detalle)."""
    ip = _client_ip(request)
    if ip is None or not any(ip in n for n in _NETS):
        log.event("admin.reject", reason="network", ip=str(ip or "-"))
        raise HTTPException(status_code=403, detail="Forbidden")


async def admin_user(user: str = Depends(current_user)):
    if not users.is_admin(user):
        log.event("admin.reject", reason="not_admin", user=user)
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


router = APIRouter(prefix="/admin", dependencies=[Depends(network_gate)])


def _err(code, detail, status=400):
    """400 con `code` estable (la pagina traduce por code), como playlists."""
    return JSONResponse(status_code=status, content={"detail": detail, "code": code})


# ------------------------------------------------------------- tamaños ----
_size_cache = {}
_size_lock = threading.Lock()


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                st = os.lstat(os.path.join(root, f))
            except OSError:
                continue
            total += st.st_size
    return total


def _cached_size(path, ttl=60):
    """Recorrer GBs en cada peticion es caro: cache corta por ruta."""
    key = str(path)
    now = time.time()
    with _size_lock:
        hit = _size_cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    size = _dir_size(path) if Path(path).exists() else 0
    with _size_lock:
        _size_cache[key] = (now, size)
    return size


def _forget_size(path):
    with _size_lock:
        _size_cache.pop(str(path), None)


# ---------------------------------------------------------------- pagina ----
@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
def page():
    f = _PAGE_DIR / "index.html"
    if not f.exists():
        raise HTTPException(status_code=404, detail="Admin panel not installed")
    return FileResponse(f, media_type="text/html",
                        headers={"Cache-Control": "no-cache"})


@router.get("/static/{name}", include_in_schema=False)
def static(name: str):
    if not _STATIC_RE.match(name):
        raise HTTPException(status_code=404)
    f = _PAGE_DIR / "static" / name
    if not f.exists():
        raise HTTPException(status_code=404)
    # textos (js) cambian con cada version: revalidar; fuentes, una semana
    cache = "no-cache" if name.endswith(".js") else "public, max-age=604800"
    return FileResponse(f, headers={"Cache-Control": cache})


# --------------------------------------------------------------- resumen ----
@router.get("/api/overview")
def overview(admin: str = Depends(admin_user)):
    disk = shutil.disk_usage(config.LIBRARY_DIR)
    names = users.list_users()
    songs = sum(len(library.for_user(n).list_entries()) for n in names
                if library.valid_username(n))
    active = jobs.list_all_active()
    models = os.environ.get("FISION_MODELS_DIR", "").strip()
    return {
        "version": __version__,
        "docker": config.IN_DOCKER,
        "uptime_sec": int(time.time() - _STARTED),
        "gpu": {**gpu.info(), "vram_free_mb": gpu.vram_free_mb(),
                "vram_min_mb": config.GPU_MIN_VRAM_MB},
        "disk": {
            "library_path": str(config.LIBRARY_DIR),
            "library_bytes": _cached_size(config.LIBRARY_DIR),
            "free_bytes": disk.free, "total_bytes": disk.total,
            "models_path": (config.host_path(models) or models) if models else "",
            "models_bytes": _cached_size(models, ttl=600) if models else None,
        },
        "users": len(names),
        "admins": len(users.admins()),
        "songs": songs,
        "jobs": {
            "running": sum(j["state"] == "running" for j in active),
            "waiting_gpu": sum(j["state"] == "waiting_gpu" for j in active),
            "queued": sum(j["state"] == "queued" for j in active),
        },
    }


# -------------------------------------------------------------- usuarios ----
def _user_row(name):
    u = users.get(name) or {}
    lib_dir = config.LIBRARY_DIR / name
    entries = library.for_user(name).list_entries() if library.valid_username(name) else []
    return {
        "username": name,
        "role": u.get("role", "user"),
        "created_at": u.get("created_at", ""),
        "updated_at": u.get("updated_at", ""),
        "songs": len(entries),
        "bytes": _cached_size(lib_dir),
        "playlists": playlists.count(name),
        "has_avatar": avatars.path_for(name) is not None,
        "active_jobs": sum(j["user"] == name for j in jobs.list_all_active()),
    }


@router.get("/api/users")
def list_users(admin: str = Depends(admin_user)):
    return {"users": [_user_row(n) for n in users.list_users()], "me": admin}


class NewUserIn(BaseModel):
    username: str
    admin: bool = False


@router.post("/api/users")
def create_user(body: NewUserIn, admin: str = Depends(admin_user)):
    name = (body.username or "").strip().lower()
    if not users.valid_username(name):
        return _err("invalid_username", "Invalid username")
    if users.exists(name):
        return _err("user_exists", "User already exists")
    pw = secrets.token_urlsafe(12)
    users.create(name, pw, admin=body.admin)
    log.event("user.create", user=name, role="admin" if body.admin else None, by=admin)
    # la password solo viaja en esta respuesta; no se guarda en claro
    return {"username": name, "password": pw, "role": "admin" if body.admin else "user"}


def _target(name):
    return name if users.valid_username(name) and users.exists(name) else None


@router.delete("/api/users/{name}")
def delete_user(name: str, purge: bool = False, admin: str = Depends(admin_user)):
    if not _target(name):
        return _err("user_not_found", "User not found", 404)
    if name == admin:
        return _err("self_delete", "Cannot delete your own account")
    try:
        users.delete(name)
    except ValueError:
        return _err("last_admin", "Cannot delete the last administrator")
    # sus trabajos vivos no deben seguir escribiendo en una cuenta que ya no
    # existe; se espera al que corre para no recrear carpetas tras el borrado
    jobs.cancel_matching(name)
    if purge:
        accounts.purge(name)
        _forget_size(config.LIBRARY_DIR / name)
        _forget_size(config.LIBRARY_DIR)
    log.event("user.delete", user=name, by=admin, purge=purge)
    return {"ok": True, "purged": purge}


@router.post("/api/users/{name}/password")
def reset_password(name: str, admin: str = Depends(admin_user)):
    if not _target(name):
        return _err("user_not_found", "User not found", 404)
    pw = secrets.token_urlsafe(12)
    users.set_password(name, pw)                  # sube el epoch: cierra sesiones
    log.event("password_change.success", user=name, by=admin)
    return {"username": name, "password": pw}


@router.post("/api/users/{name}/revoke")
def revoke(name: str, admin: str = Depends(admin_user)):
    if not _target(name):
        return _err("user_not_found", "User not found", 404)
    users.revoke_sessions(name)
    log.event("sessions.revoke", user=name, by=admin)
    return {"ok": True}


class RoleIn(BaseModel):
    admin: bool


@router.put("/api/users/{name}/role")
def set_role(name: str, body: RoleIn, admin: str = Depends(admin_user)):
    if not _target(name):
        return _err("user_not_found", "User not found", 404)
    if name == admin and not body.admin:
        return _err("self_demote", "Cannot remove the role from your own account")
    try:
        users.set_admin(name, body.admin)
    except ValueError:
        return _err("last_admin", "Cannot remove the last administrator")
    log.event("user.role", user=name, role="admin" if body.admin else "user", by=admin)
    return {"username": name, "role": "admin" if body.admin else "user"}


@router.get("/api/users/{name}/library")
def user_library(name: str, admin: str = Depends(admin_user)):
    """Solo metadatos. Ni audio, ni portadas, ni letras."""
    if not _target(name):
        return _err("user_not_found", "User not found", 404)
    lib = library.for_user(name)
    out = []
    for m in lib.list_entries():
        h = m.get("hash", "")
        try:
            size = _cached_size(lib.entry_dir(h), ttl=300)
            has_lyrics = (lib.stems_dir(h) / "lyrics.json").exists()
        except ValueError:
            size, has_lyrics = 0, False
        out.append({
            "hash": h, "title": m.get("title") or m.get("filename", ""),
            "artist": m.get("artist", ""), "state": m.get("state", ""),
            "created_at": m.get("created_at", ""), "updated_at": m.get("updated_at", ""),
            "bytes": size, "lyrics": has_lyrics,
        })
    return {"username": name, "songs": out}


# Orden de presentacion de las pistas; las desconocidas van al final
_STEM_ORDER = ["vocals", "vocals2", "instrumental", "drums", "kick", "snare", "toms",
               "hh", "ride", "crash", "perc", "bass", "guitar", "piano", "winds", "other"]


def _duration(lib, h):
    """Duracion en segundos: de lyrics.json si existe, si no de las cabeceras."""
    try:
        with open(lib.stems_dir(h) / "lyrics.json", encoding="utf-8") as f:
            return float(json.load(f).get("duration") or 0) or None
    except Exception:
        pass
    try:
        import mutagen
        src = lib.input_path(h)
        return float(mutagen.File(src).info.length) if src else None
    except Exception:
        return None


@router.get("/api/users/{name}/library/{h}")
def song_detail(name: str, h: str, admin: str = Depends(admin_user)):
    """Ficha tecnica de una cancion: que se genero, no el contenido."""
    if not _target(name):
        return _err("user_not_found", "User not found", 404)
    lib = library.for_user(name)
    m = lib.read_meta(h)
    if not m:
        return _err("song_not_found", "Song not found", 404)
    sd = lib.stems_dir(h)
    names = {p.stem for p in sd.glob("*.flac")} if sd.exists() else set()
    stems = sorted((n for n in names if not n.startswith("metronome")),
                   key=lambda n: (_STEM_ORDER.index(n) if n in _STEM_ORDER else 99, n))
    bpm = None
    try:
        with open(sd / "beats.txt", encoding="utf-8") as f:
            bpm = round(float(f.readline()))
    except Exception:
        pass
    lang = None
    if (sd / "lyrics.json").exists():
        try:
            with open(sd / "lyrics.json", encoding="utf-8") as f:
                lang = json.load(f).get("language") or ""
        except Exception:
            lang = ""
    src = lib.input_path(h)
    cfg = m.get("config") or {}
    return {
        "hash": h, "title": m.get("title") or m.get("filename", ""),
        "artist": m.get("artist", ""), "state": m.get("state", ""),
        "created_at": m.get("created_at", ""), "updated_at": m.get("updated_at", ""),
        "bytes": _cached_size(lib.entry_dir(h), ttl=300),
        "format": src.suffix.lstrip(".").upper() if src else "",
        "duration": _duration(lib, h),
        "stems": stems,
        "lyrics": lang is not None, "lyrics_language": lang or "",
        "chords": (sd / "chords.txt").exists(),
        "bpm": bpm,
        "metronome": "metronome" in names,
        "cover": (sd / "artcover.png").exists(),
        "device": cfg.get("device", "") if isinstance(cfg, dict) else "",
    }


# -------------------------------------------------------------- trabajos ----
@router.get("/api/jobs")
def list_jobs(admin: str = Depends(admin_user)):
    out = []
    for j in jobs.list_all_active():
        meta = library.for_user(j["user"]).read_meta(j["hash"]) or {}
        out.append({**j, "title": meta.get("title") or j["filename"],
                    "artist": meta.get("artist", "")})
    return {"jobs": out}


@router.delete("/api/jobs/{jid}")
def cancel(jid: str, admin: str = Depends(admin_user)):
    job = jobs.get_job(jid)
    if not job or not jobs.cancel_job(jid):
        return _err("job_not_found", "Job not found or already finished", 404)
    log.event("job.cancel", user=job["user"], job_id=jid, by=admin)
    return {"ok": True}


# ------------------------------------------------------------- auditoria ----
@router.get("/api/audit")
def audit(limit: int = 100, admin: str = Depends(admin_user)):
    return {"events": log.recent(min(max(limit, 1), 300))}


# --------------------------------------------------------- configuracion ----
# (grupo, variable, valor por defecto). Las descripciones viven en la pagina
# (traducibles). Solo lectura: se cambian en el entorno del servicio.
_CONFIG = [
    ("paths", "FISION_LIBRARY", "./library"),
    ("paths", "FISION_DATA", "./data"),
    ("paths", "FISION_MODELS_DIR", ""),
    ("paths", "FISION_SEP_MODEL_DIR", "~/.cache/audio-separator-models"),
    ("paths", "HF_HOME", "~/.cache/huggingface"),
    ("paths", "TORCH_HOME", "~/.cache/torch"),
    ("server", "FISION_HOST", "0.0.0.0"),
    ("server", "FISION_PORT", "8000"),
    ("server", "FISION_LOG_LEVEL", "INFO"),
    ("server", "FISION_ADMIN_ALLOW", config.ADMIN_ALLOW_DEFAULT),
    ("server", "FISION_MAX_UPLOAD_MB", "200"),
    ("server", "FISION_MAX_AVATAR_MB", "2"),
    ("server", "FISION_API_DOCS", "0"),
    ("auth", "FISION_TOKEN_TTL_HOURS", "720"),
    ("auth", "FISION_LOGIN_MAX_FAILS", "5"),
    ("auth", "FISION_LOGIN_MAX_FAILS_IP", "20"),
    ("auth", "FISION_LOGIN_WINDOW_SEC", "300"),
    ("auth", "FISION_SECRET", ""),
    ("gpu", "FISION_GPU_MIN_VRAM_MB", "4096"),
    ("gpu", "FISION_GPU_POLL_SEC", "30"),
    ("gpu", "FISION_GPU_WAIT_TIMEOUT_SEC", "600"),
    ("jobs", "FISION_MAX_JOBS_PER_USER", "3"),
    ("jobs", "FISION_JOB_TIMEOUT_SEC", "2400"),
    ("jobs", "FISION_LYRICS_TIMEOUT_SEC", "900"),
    ("separation", "FISION_SEP_BATCH_SIZE", "24"),
    ("separation", "FISION_SEP_SEGMENT_SIZE", "512"),
    ("separation", "FISION_SEP_OVERLAP", "4"),
    ("separation", "FISION_SEP_AUTOCAST", "1"),
    ("separation", "FISION_DEMUCS_SHIFTS", "4"),
    ("separation", "FISION_DEMUCS_OVERLAP", "0.5"),
    ("lyrics", "FISION_WHISPER_MODEL", "large-v2"),
    ("lyrics", "FISION_WHISPER_DEVICE", "cuda"),
    ("lyrics", "FISION_WHISPER_COMPUTE", "float16"),
    ("lyrics", "FISION_WHISPER_MIN_VRAM_MB", "3072"),
    ("lyrics", "FISION_WHISPER_BATCH_SIZE", "0"),
    ("chords", "FISION_CHORDS_SMOOTH", "1"),
    ("chords", "FISION_CHORDS_CHANGE_COST", "1.0"),
    ("chords", "FISION_CHORDS_FAMILY_COST", "0.2"),
    ("chords", "FISION_CHORDS_OFFBEAT_MULT", "2.0"),
]
_SECRET_VARS = {"FISION_SECRET"}


def _effective(var):
    """Valor real cuando no viene en el entorno pero se calcula (rutas)."""
    if var == "FISION_LIBRARY":
        return str(config.LIBRARY_DIR), None
    if var == "FISION_DATA":
        return str(config.DATA_DIR), None
    root = os.environ.get("FISION_MODELS_DIR", "").strip()
    sub = {"FISION_SEP_MODEL_DIR": "separator", "HF_HOME": "hf", "TORCH_HOME": "torch"}.get(var)
    if root and sub:
        # misma regla que headless_fisionAI._apply_models_dir()
        return os.path.join(os.path.abspath(os.path.expanduser(root)), sub), "derived"
    return None, None


@router.get("/api/config")
def read_config(admin: str = Depends(admin_user)):
    out = []
    for group, var, default in _CONFIG:
        raw = os.environ.get(var, "").strip()
        if not raw and var not in _SECRET_VARS:
            eff, how = _effective(var)
            if eff:
                out.append({"group": group, "var": var, "value": eff, "default": default,
                            "source": how or "default", "secret": False})
                continue
        if var in _SECRET_VARS:
            # nunca se muestra; solo de donde sale
            value = "env" if raw else ("file" if config.SECRET_FILE.exists() else "")
            out.append({"group": group, "var": var, "value": value,
                        "default": "", "source": "env" if raw else "default",
                        "secret": True})
            continue
        out.append({"group": group, "var": var, "value": raw or default,
                    "default": default, "source": "env" if raw else "default",
                    "secret": False})
    return {"docker": config.IN_DOCKER, "settings": [_docker_view(s) for s in out]}


# Docker: rutas y direccion se cambian en el .env del host, con otras variables
_DOCKER_VARS = {
    "FISION_DATA": ("FISION_DATA_PATH", "data", "./data"),
    "FISION_LIBRARY": ("FISION_LIBRARY_PATH", "library", "./library"),
    "FISION_MODELS_DIR": ("FISION_MODELS_PATH", "models", "./models"),
    "FISION_HOST": ("FISION_BIND", "bind", "127.0.0.1"),
    "FISION_PORT": ("FISION_PUBLISH_PORT", "port", "8000"),
}


def _docker_view(row):
    """Añade el lado del host (host_var / host_value) a lo que lo tiene."""
    if not config.IN_DOCKER:
        return row
    spec = _DOCKER_VARS.get(row["var"])
    if spec:
        host_var, key, default = spec
        value = config.DOCKER_HOST[key]
        return {**row, "host_var": host_var, "host_value": value,
                "source": "default" if value == default else "env"}
    host = config.host_path(row["value"]) if row["group"] == "paths" else None
    return {**row, "host_value": host} if host else row
