"""
Biblioteca en disco. Una carpeta por hash de audio:

    library/<hash>/
        input.<ext>      el audio original (semilla para reprocesar)
        meta.json        nombre real, config vigente, estado, fecha
        stems/           la separacion vigente
        result.zip       zip de la separacion vigente

Un hash = un resultado. Reprocesar hace OVERRIDE (la ultima config manda).
El commit es atomico: se trabaja en stems_tmp/ y solo al final se cambia.
"""
import os
import json
import shutil
import hashlib
import zipfile
from pathlib import Path

from . import config


# ----------------------------------------------------------------- hash ----
def hash_file(path, chunk=1 << 20):
    """SHA-256 de los bytes del fichero (huella del contenido)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


# ---------------------------------------------------------------- rutas ----
def entry_dir(h):  return config.LIBRARY_DIR / h
def meta_path(h):  return entry_dir(h) / "meta.json"
def stems_dir(h):  return entry_dir(h) / "stems"
def zip_path(h):   return entry_dir(h) / "result.zip"


def input_path(h):
    for f in entry_dir(h).glob("input.*"):
        return f
    return None


def cover_file(h):
    """Ruta en disco de la portada (la genera el pipeline dentro de stems/)."""
    return stems_dir(h) / "artcover.png"


def cover_url(h):
    """URL relativa de la portada, tal como la consume la app."""
    return f"/library/{h}/stems/artcover.png"


def read_tags(path):
    """Devuelve (title, artist) de las etiquetas del audio. (None, None) si no hay."""
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


def exists(h):
    return meta_path(h).exists()


# ----------------------------------------------------------------- meta ----
def read_meta(h):
    p = meta_path(h)
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def write_meta(h, meta):
    d = entry_dir(h)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "meta.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, meta_path(h))      # escritura atomica


def list_entries():
    out = []
    if config.LIBRARY_DIR.exists():
        for d in config.LIBRARY_DIR.iterdir():
            if d.is_dir() and (d / "meta.json").exists():
                out.append(read_meta(d.name))
    # orden por defecto: created_at (fallback updated_at), mas nuevo primero.
    out.sort(key=lambda m: m.get("created_at") or m.get("updated_at") or "",
             reverse=True)
    return out


# --------------------------------------------------------------- input ----
def save_input(h, tmp_src, ext):
    """Mueve el audio subido (en temp) a library/<hash>/input.<ext>."""
    d = entry_dir(h)
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"input{ext}"
    if dst.exists():
        os.remove(tmp_src)             # ya lo teniamos
    else:
        shutil.move(str(tmp_src), str(dst))
    return dst


# ----------------------------------------------------------------- zip ----
def build_zip(src_dir, dst_zip):
    with zipfile.ZipFile(dst_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(Path(src_dir).glob("*")):
            if f.is_file():
                z.write(f, f.name)


def commit_result(h, tmp_stems):
    """Cambio ATOMICO: solo si el proceso fue bien.

    Genera el zip de lo nuevo, y luego reemplaza stems/ y result.zip de golpe.
    Si algo falla antes de llegar aqui, lo vigente queda intacto.
    """
    d = entry_dir(h)
    tmp_zip = d / "result.zip.tmp"
    build_zip(tmp_stems, tmp_zip)

    final_stems = stems_dir(h)
    if final_stems.exists():
        shutil.rmtree(final_stems)
    os.replace(tmp_stems, final_stems)
    os.replace(tmp_zip, zip_path(h))


def delete_entry(h):
    d = entry_dir(h)
    if d.exists():
        shutil.rmtree(d)


# ------------------------------------------------- guardar como el server ----
# Fuente unica para "procesar y guardar en la biblioteca". La usan la GUI y el
# CLI (modo --save-server) para no duplicar la logica del worker del servidor.

def prepare_entry(input_file):
    """Hashea el input, crea library/<hash>/, copia el input (semilla) y
    devuelve (hash, stems_tmp_path) listo para que el pipeline escriba ahi."""
    h = hash_file(input_file)
    d = entry_dir(h)
    d.mkdir(parents=True, exist_ok=True)
    ext = os.path.splitext(input_file)[1].lower() or ".bin"
    dst_in = d / f"input{ext}"
    if not dst_in.exists():
        shutil.copy2(input_file, dst_in)      # semilla para reprocesar
    tmp = d / "stems_tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    return h, tmp


def finalize_entry(h, tmp_stems, cfg, nombre):
    """Commit atomico (stems/ + result.zip) y meta.json = 'listo'.

    Mismo formato de meta que el servidor: title/artist de las tags, cover_url,
    created_at (solo la 1a vez) y updated_at.
    """
    from datetime import datetime, timezone
    commit_result(h, Path(tmp_stems))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    title, artist = read_tags(input_path(h))
    meta = read_meta(h) or {"hash": h}
    meta.setdefault("created_at", now)
    meta.update({
        "hash": h,
        "nombre": nombre,
        "config": cfg,
        "estado": "listo",
        "updated_at": now,
        "title": title or Path(nombre).stem,
        "artist": artist or "",
        "cover_url": cover_url(h),
    })
    write_meta(h, meta)
    return h


def discard_tmp(h):
    """Tira stems_tmp/ tras un fallo/cancelacion. Lo vigente queda intacto."""
    shutil.rmtree(entry_dir(h) / "stems_tmp", ignore_errors=True)
