"""
Avatar por usuario. Fichero en data/avatars/<user>.<ext>.

- Un unico avatar por usuario (una extension a la vez). Al subir uno nuevo, se
  borra el anterior aunque venga en otra extension.
- El cliente se encarga del crop y del resize; el server guarda la imagen tal
  cual, sin tocarla (sin Pillow como dependencia).
- Formatos aceptados: JPEG, PNG, WebP. SVG se rechaza (puede llevar scripts).
- La extension REAL se deduce por magic bytes: no nos fiamos del Content-Type
  ni del nombre de fichero que mande el cliente.
"""
import os
import shutil

from . import config


AVATAR_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# Content-Type que sirvemos segun la extension guardada en disco.
_MEDIA = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".webp": "image/webp",
}


def sniff_ext(head_bytes):
    """Detecta la extension real por magic bytes. None si no es imagen soportada."""
    if head_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg"                                   # JPEG
    if head_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"                                   # PNG
    if head_bytes[:4] == b"RIFF" and head_bytes[8:12] == b"WEBP":
        return ".webp"                                  # WebP (RIFF container)
    return None


def media_type(ext):
    return _MEDIA.get(ext.lower())


def path_for(user):
    """Ruta al avatar en disco, o None si el usuario no tiene."""
    for ext in AVATAR_EXTS:
        p = config.AVATARS_DIR / f"{user}{ext}"
        if p.exists():
            return p
    return None


def save(user, tmp_path, ext):
    """Mueve el temporal a data/avatars/<user><ext>. Borra el anterior si lo habia."""
    delete(user)                                        # limpia otras extensiones
    dst = config.AVATARS_DIR / f"{user}{ext}"
    shutil.move(str(tmp_path), str(dst))
    return dst


def delete(user):
    for ext in AVATAR_EXTS:
        p = config.AVATARS_DIR / f"{user}{ext}"
        if p.exists():
            try:
                os.remove(p)
            except OSError:
                pass
