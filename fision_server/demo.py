"""
Cancion demo para las cuentas nuevas.

Plantilla: library/.demo/<hash>/, con la misma forma que una entrada de
usuario (meta.json, input.*, stems/, result.zip) mas NOTICE.txt con la
licencia. Al crear una cuenta (useradd o el panel) se copia a
library/<user>/<hash>/. Sin plantilla no se hace nada.

Audio y zip van con hard link (mismo disco: al instante y sin ocupar mas);
lo pequeno se copia. Ningun audio se reescribe en sitio, pero chords.txt
si (rechords): con hard link cambiaria la demo de todos a la vez.

`prefetch` baja la plantilla oficial de la Release de GitHub si no hay.
"""
import os
import json
import shutil
import hashlib
import zipfile
import urllib.request
from datetime import datetime, timezone

from . import config, library

DEMO_DIR = config.LIBRARY_DIR / ".demo"

# Plantilla oficial: "Airplane Mode" de Josh Woodward (CC BY 4.0).
# El zip lleva <hash>/ sin result.zip (se rehace al bajarlo).
DEMO_URL = ("https://github.com/jotapemj/Fision-Server/releases/download/"
            "v2.1.0/fision-demo.zip")
DEMO_SHA256 = "32ab164fb9506d884bc6016dd3b10d5d7c539f5e933d40cf338ae2689d8e5bb3"

_LINK_EXT = {".flac", ".wav", ".mp3", ".m4a", ".ogg", ".opus", ".aac", ".zip"}


def entries():
    """Carpetas de la plantilla que parecen una entrada valida."""
    if not DEMO_DIR.is_dir():
        return []
    return sorted(d for d in DEMO_DIR.iterdir()
                  if d.is_dir() and library.valid_hash(d.name)
                  and (d / "meta.json").is_file() and (d / "stems").is_dir())


def _place(src, dst):
    if src.suffix.lower() in _LINK_EXT:
        try:
            os.link(src, dst)
            return
        except OSError:
            pass                      # otro disco o sin soporte: copia
    shutil.copy2(src, dst)


def _copy_tree(src, dst):
    dst.mkdir()
    for p in src.iterdir():
        if p.is_dir():
            _copy_tree(p, dst / p.name)
        else:
            _place(p, dst / p.name)


def give(user):
    """Copia la plantilla a la biblioteca de 'user'. Devuelve cuantas copio.

    Lo que el usuario ya tiene (mismo hash) no se toca.
    """
    lib = library.for_user(user)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    done = 0
    for src in entries():
        h = src.name
        dst = lib.entry_dir(h)
        if dst.exists():
            continue
        lib.root.mkdir(parents=True, exist_ok=True)
        tmp = lib.root / f".demo-{h}.tmp"     # el punto lo esconde de la biblioteca
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            _copy_tree(src, tmp)
            if not (tmp / "result.zip").exists():
                library.build_zip(tmp / "stems", tmp / "result.zip")
            # Fechas de la copia: para la app es una cancion recien llegada
            mp = tmp / "meta.json"
            meta = json.loads(mp.read_text(encoding="utf-8"))
            meta["created_at"] = meta["updated_at"] = now
            mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            os.rename(tmp, dst)
            done += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return done


def _safe_extract(zf, dest):
    """Extrae solo <hash>/... sin rutas que escapen (zip-slip)."""
    root = dest.resolve()
    for info in zf.infolist():
        top = info.filename.split("/", 1)[0]
        target = (dest / info.filename).resolve()
        if not library.valid_hash(top) or root not in target.parents:
            raise ValueError(f"unexpected path in demo zip: {info.filename!r}")
    zf.extractall(dest)


def fetch(url=DEMO_URL, sha256=DEMO_SHA256):
    """Baja e instala la plantilla oficial. False si ya habia una."""
    if entries():
        return False
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    part = DEMO_DIR / ".download.part"
    work = DEMO_DIR / ".extract.tmp"
    shutil.rmtree(work, ignore_errors=True)
    try:
        digest = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as f:
            while chunk := r.read(1 << 20):
                digest.update(chunk)
                f.write(chunk)
        if sha256 and digest.hexdigest() != sha256:
            raise ValueError("demo zip checksum mismatch")
        work.mkdir()
        with zipfile.ZipFile(part) as zf:
            _safe_extract(zf, work)
        for d in work.iterdir():
            if not (DEMO_DIR / d.name).exists():
                library.build_zip(d / "stems", d / "result.zip")
                os.rename(d, DEMO_DIR / d.name)
        return True
    finally:
        part.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
