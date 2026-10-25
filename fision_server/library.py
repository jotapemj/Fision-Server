"""
Biblioteca en disco, AISLADA POR USUARIO:

    library/<user>/<hash>/
        input.<ext>   audio original (semilla para reprocesar)
        meta.json     filename, config vigente, state, fechas
        stems/        separacion vigente
        result.zip    zip de la separacion vigente

Cada usuario ve SOLO su carpeta. El <user> sale SIEMPRE del token (nunca de la
URL). El <hash> es SHA-256 del contenido: mismo audio = mismo hash, pero en
carpetas distintas si son de usuarios distintos (sin cruces, sin pisarse).

Interfaz principal: library.for_user(user) -> UserLibrary, con los metodos
ligados a ese usuario. Asi es dificil olvidarse de a quien pertenece cada
escritura.

    lib = library.for_user("valeria")
    lib.read_meta(h); lib.save_input(h, tmp, ext); lib.list_entries()

Un hash = un resultado (por usuario). Reprocesar hace OVERRIDE (la ultima config
manda). Commit atomico: se trabaja en stems_tmp/ y solo al final se cambia.
"""
import os
import re
import json
import shutil
import hashlib
import zipfile
from pathlib import Path
from datetime import datetime, timezone

from . import config

# Slug seguro para nombres de carpeta. Espeja el criterio de users.py.
_USER_RE = re.compile(r"^[a-z0-9_]{2,32}$")
# SHA-256 en hex. Validar el hash cierra path traversal via la URL (/library/{h}).
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def valid_username(name):
    return bool(name) and bool(_USER_RE.match(name))


def valid_hash(h):
    return bool(h) and bool(_HASH_RE.match(h))


# ----------------------------------------------- helpers user-agnostic ----
def hash_file(path, chunk=1 << 20):
    """SHA-256 de los bytes del fichero (huella del contenido)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def read_tags(path):
    """(title, artist) de las etiquetas del audio. (None, None) si no hay."""
    if not path:
        return None, None
    try:
        from mutagen import File as _MFile
        m = _MFile(str(path), easy=True)
        if not m:
            return None, None
        def _first(k):
            v = m.get(k)
            return v[0] if v else None
        return _first("title"), _first("artist")
    except Exception:
        return None, None


def build_zip(src_dir, dst_zip):
    with zipfile.ZipFile(dst_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(Path(src_dir).glob("*")):
            if f.is_file():
                z.write(f, f.name)


def cover_url(h):
    """URL relativa de la portada. OJO: solo hash, SIN usuario.

    El endpoint que la sirve resuelve el usuario del token. Asi la URL no filtra
    a quien pertenece y sigue siendo la misma para la app.
    """
    return f"/library/{h}/stems/artcover.png"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------- UserLibrary ----
class UserLibrary:
    """Vista de la biblioteca ligada a UN usuario. Todo cae bajo library/<user>/."""

    def __init__(self, user):
        if not valid_username(user):
            raise ValueError(f"invalid user: {user!r}")
        self.user = user

    @property
    def root(self):
        return config.LIBRARY_DIR / self.user

    # ---- rutas (validan el hash) ----
    def entry_dir(self, h):
        if not valid_hash(h):
            raise ValueError(f"invalid hash: {h!r}")
        return self.root / h

    def meta_path(self, h):  return self.entry_dir(h) / "meta.json"
    def stems_dir(self, h):  return self.entry_dir(h) / "stems"
    def zip_path(self, h):   return self.entry_dir(h) / "result.zip"
    def cover_file(self, h): return self.stems_dir(h) / "artcover.png"

    def input_path(self, h):
        try:
            d = self.entry_dir(h)
        except ValueError:
            return None
        for f in d.glob("input.*"):
            return f
        return None

    def exists(self, h):
        try:
            return self.meta_path(h).exists()
        except ValueError:
            return False

    # ---- meta ----
    def read_meta(self, h):
        try:
            p = self.meta_path(h)
        except ValueError:
            return None
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def write_meta(self, h, meta):
        d = self.entry_dir(h)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "meta.json.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.meta_path(h))       # escritura atomica

    def list_entries(self):
        """Solo las canciones de ESTE usuario, mas nuevas primero."""
        out = []
        if self.root.exists():
            for d in self.root.iterdir():
                if d.is_dir() and (d / "meta.json").exists():
                    m = self.read_meta(d.name)
                    if m:
                        out.append(m)
        out.sort(key=lambda m: m.get("created_at") or m.get("updated_at") or "",
                 reverse=True)
        return out

    # ---- input ----
    def save_input(self, h, tmp_src, ext):
        d = self.entry_dir(h)
        d.mkdir(parents=True, exist_ok=True)
        dst = d / f"input{ext}"
        if dst.exists():
            os.remove(tmp_src)                    # ya lo teniamos
        else:
            shutil.move(str(tmp_src), str(dst))
        return dst

    # ---- commit ----
    def commit_result(self, h, tmp_stems):
        """Swap ATOMICO: genera el zip y reemplaza stems/ + result.zip de golpe."""
        d = self.entry_dir(h)
        tmp_zip = d / "result.zip.tmp"
        build_zip(tmp_stems, tmp_zip)
        final_stems = self.stems_dir(h)
        if final_stems.exists():
            shutil.rmtree(final_stems)
        os.replace(tmp_stems, final_stems)
        os.replace(tmp_zip, self.zip_path(h))

    def delete_entry(self, h):
        try:
            d = self.entry_dir(h)
        except ValueError:
            return
        if d.exists():
            shutil.rmtree(d)

    # ---- guardar como el server (CLI) ----
    def prepare_entry(self, input_file):
        h = hash_file(input_file)
        d = self.entry_dir(h)
        d.mkdir(parents=True, exist_ok=True)
        ext = os.path.splitext(input_file)[1].lower() or ".bin"
        dst_in = d / f"input{ext}"
        if not dst_in.exists():
            shutil.copy2(input_file, dst_in)      # semilla para reprocesar
        tmp = d / "stems_tmp"
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        return h, tmp

    def finalize_entry(self, h, tmp_stems, cfg, filename):
        self.commit_result(h, Path(tmp_stems))
        now = _now()
        title, artist = read_tags(self.input_path(h))
        meta = self.read_meta(h) or {"hash": h}
        meta.setdefault("created_at", now)
        meta.update({
            "hash": h, "filename": filename, "config": cfg, "state": "ready",
            "updated_at": now, "title": title or Path(filename).stem,
            "artist": artist or "", "cover_url": cover_url(h),
        })
        self.write_meta(h, meta)
        return h

    def discard_tmp(self, h):
        shutil.rmtree(self.entry_dir(h) / "stems_tmp", ignore_errors=True)


def for_user(user):
    """Punto de entrada: library.for_user('valeria').read_meta(h) ..."""
    return UserLibrary(user)


def list_user_dirs():
    """Nombres de usuario que tienen carpeta en library/ (para barridos globales)."""
    root = config.LIBRARY_DIR
    if not root.exists():
        return []
    return [d.name for d in root.iterdir()
            if d.is_dir() and valid_username(d.name)]


# --------------------------------------- back-compat CLI (single-user) ----
# El guardado LOCAL del CLI (un solo dueño en tu maquina) usa config.LOCAL_USER.
# El servidor NO usa estas: siempre for_user(user_del_token).
def prepare_entry(input_file, user=None):
    return for_user(user or config.LOCAL_USER).prepare_entry(input_file)


def finalize_entry(h, tmp_stems, cfg, filename, user=None):
    return for_user(user or config.LOCAL_USER).finalize_entry(h, tmp_stems, cfg, filename)


def discard_tmp(h, user=None):
    return for_user(user or config.LOCAL_USER).discard_tmp(h)
