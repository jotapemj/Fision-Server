"""
Datos en disco de una cuenta, fuera de users.json: biblioteca, playlists
(y sus covers) y avatar. Lo comparten el panel y los CLI.

Borrar la cuenta sin purgar deja estos datos; una cuenta nueva con el mismo
nombre los heredaria. Por eso useradd avisa y userdel --purge los quita.
"""
import json
import shutil

from . import config, playlists, avatars


def leftovers(name):
    """Rutas con datos de 'name' que siguen en disco."""
    out = []
    lib = config.LIBRARY_DIR / name
    if lib.exists():
        out.append(lib)
    for p in (config.PLAYLISTS_DIR / f"{name}.json", config.PLAYLISTS_DIR / name):
        if p.exists():
            out.append(p)
    av = avatars.path_for(name)
    if av:
        out.append(av)
    return out


def purge(name):
    """Borra biblioteca, playlists y avatar de 'name'."""
    shutil.rmtree(config.LIBRARY_DIR / name, ignore_errors=True)
    playlists.purge(name)
    avatars.delete(name)


def queued_jobs(name):
    """Jobs vivos de 'name' segun queue.json (lo que ve un CLI sin el server)."""
    try:
        with open(config.QUEUE_FILE, encoding="utf-8") as f:
            items = json.load(f).get("jobs", [])
    except (OSError, ValueError):
        return 0
    return sum(1 for j in items if j.get("user") == name)
