"""
Playlists por usuario. Datos de la cuenta, no contenido procesado: viven en
data/, no en library/.

    data/playlists/<user>.json        {"<id>": playlist, ...}
    data/playlists/<user>/<id>.<ext>  cover (JPEG/PNG/WebP, como el avatar)

Una playlist:

    {"id": "a1b2c3d4e5f6", "title": "...", "songs": ["<hash>", ...],
     "created_at": "...", "updated_at": "..."}

`songs` va ordenada (orden de reproduccion) y solo admite hashes que existen
en la biblioteca del usuario en el server. Gana el ultimo que guarda.
"""
import os
import json
import uuid
import shutil
import threading
from datetime import datetime, timezone

from . import config, library, avatars

MAX_TITLE = 100
MAX_SONGS = 2000
MAX_PLAYLISTS = 200

_lock = threading.Lock()          # endpoints sync = threadpool; serializa el RMW


class PlaylistError(ValueError):
    """Error de validacion -> 400. `code` es estable: el cliente traduce por el."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file(user):
    return config.PLAYLISTS_DIR / f"{user}.json"


def _cover_dir(user):
    return config.PLAYLISTS_DIR / user


def _load(user):
    p = _file(user)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _save(user, data):
    p = _file(user)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)                     # escritura atomica


def valid_id(pid):
    return isinstance(pid, str) and len(pid) == 12 and all(
        c in "0123456789abcdef" for c in pid)


def _clean(user, title, songs):
    """Valida y normaliza. PlaylistError con code + mensaje para el 400."""
    title = (title or "").strip()
    if not 1 <= len(title) <= MAX_TITLE:
        raise PlaylistError("title_length",
                            f"The title must have 1 to {MAX_TITLE} characters")
    if not isinstance(songs, list) or len(songs) > MAX_SONGS:
        raise PlaylistError("song_limit", f"At most {MAX_SONGS} songs")
    if len(set(songs)) != len(songs):
        raise PlaylistError("song_duplicate", "Song repeated in the playlist")
    lib = library.for_user(user)
    for h in songs:
        # Solo canciones en la nube del propio usuario
        if not (isinstance(h, str) and library.valid_hash(h) and lib.exists(h)):
            raise PlaylistError("song_not_found",
                                f"Song not found in your library: {h}")
    return title, songs


# ----------------------------------------------------------------- cover ----
def cover_path(user, pid):
    for ext in avatars.AVATAR_EXTS:
        p = _cover_dir(user) / f"{pid}{ext}"
        if p.exists():
            return p
    return None


def delete_cover(user, pid):
    for ext in avatars.AVATAR_EXTS:
        p = _cover_dir(user) / f"{pid}{ext}"
        if p.exists():
            try:
                os.remove(p)
            except OSError:
                pass


def save_cover(user, pid, tmp_path, ext):
    """False si la playlist no existe (el temporal lo borra el llamador)."""
    with _lock:
        data = _load(user)
        if pid not in data:
            return False
        delete_cover(user, pid)                     # limpia otras extensiones
        _cover_dir(user).mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_path), str(_cover_dir(user) / f"{pid}{ext}"))
        data[pid]["updated_at"] = _now()
        _save(user, data)
    return True


# ------------------------------------------------------------------ CRUD ----
def _public(user, p, full):
    # Filtra hashes que ya no existen (borrado a mano en disco, etc.)
    lib = library.for_user(user)
    songs = [h for h in p["songs"] if lib.exists(h)]
    out = {"id": p["id"], "title": p["title"], "count": len(songs),
           "has_cover": cover_path(user, p["id"]) is not None,
           "created_at": p["created_at"], "updated_at": p["updated_at"]}
    if full:
        out["songs"] = songs
    return out


def list_all(user):
    data = _load(user)
    out = [_public(user, p, full=False) for p in data.values()]
    out.sort(key=lambda p: p["updated_at"], reverse=True)
    return out


def get(user, pid):
    p = _load(user).get(pid)
    return _public(user, p, full=True) if p else None


def create(user, title, songs):
    title, songs = _clean(user, title, songs)
    with _lock:
        data = _load(user)
        if len(data) >= MAX_PLAYLISTS:
            raise PlaylistError("playlist_limit", f"At most {MAX_PLAYLISTS} playlists")
        pid = uuid.uuid4().hex[:12]
        now = _now()
        data[pid] = {"id": pid, "title": title, "songs": songs,
                     "created_at": now, "updated_at": now}
        _save(user, data)
    return _public(user, data[pid], full=True)


def update(user, pid, title, songs):
    """None si no existe. Sustituye titulo y canciones enteros."""
    title, songs = _clean(user, title, songs)
    with _lock:
        data = _load(user)
        if pid not in data:
            return None
        data[pid].update(title=title, songs=songs, updated_at=_now())
        _save(user, data)
    return _public(user, data[pid], full=True)


def delete(user, pid):
    with _lock:
        data = _load(user)
        if pid not in data:
            return False
        del data[pid]
        _save(user, data)
        delete_cover(user, pid)
    return True


def count(user):
    return len(_load(user))


def purge(user):
    """Borra TODAS las playlists y covers del usuario (baja con borrado total)."""
    with _lock:
        try:
            os.remove(_file(user))
        except FileNotFoundError:
            pass
        shutil.rmtree(_cover_dir(user), ignore_errors=True)


def remove_song(user, h):
    """Quita una cancion de todas las playlists del usuario. Devuelve cuantas."""
    with _lock:
        data = _load(user)
        n = 0
        now = _now()
        for p in data.values():
            if h in p["songs"]:
                p["songs"].remove(h)
                p["updated_at"] = now
                n += 1
        if n:
            _save(user, data)
    return n
