"""
Cola de trabajos + worker en segundo plano.

La GPU procesa de UNA en una: un solo worker consume la cola en serie.
La peticion HTTP nunca espera el proceso largo (evita el corte de ~100s de
Cloudflare): encola y devuelve un job_id al instante.

Estados de job:  queued -> running -> done | error | cancelled

Cola con POSICION real: se mantiene una lista ordenada `_pending`. Cancelar un
job en cola lo marca `cancelled`; el worker lo salta y deja de contar para la
posicion, asi que los de detras suben solos (1,2,3,4 -> borro 2 -> 1,3,4).

Regla de oro (lo que pidio JP): se procesa en stems_tmp/ y SOLO si sale bien
se hace el commit atomico. Un reproceso fallido o cancelado NO destruye el
resultado vigente.
"""
import time
import uuid
import shutil
import threading
from datetime import datetime, timezone

from . import library, pipeline

_jobs = {}                       # job_id -> dict
_pending = []                    # ids en orden de llegada (los 'queued')
_cond = threading.Condition()    # señaliza trabajo nuevo y protege la cola
_current = {"job_id": None, "proc": None}


def _now():
    # ISO8601 con zona (UTC), precision de segundos: 2026-06-29T13:32:45+00:00
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _describe(h, name):
    """Campos descriptivos del meta: title/artist (de las tags) y cover_url."""
    title, artist = library.read_tags(library.input_path(h))
    from pathlib import Path
    return {
        "title": title or Path(name).stem,   # sin tag -> nombre del fichero
        "artist": artist or "",
        "cover_url": library.cover_url(h),
    }


# ----------------------------------------------------------------- crear ----
def create_job(audio_hash, name, cfg, reprocess=False):
    jid = uuid.uuid4().hex[:12]
    job = {"job_id": jid, "hash": audio_hash, "nombre": name, "config": cfg,
           "estado": "queued", "error": "", "creado": _now(),
           "reprocess": reprocess}
    with _cond:
        _jobs[jid] = job
        _pending.append(jid)
        _cond.notify()

    # Reflejar en meta que esta en cola/proceso, SIN tocar el resultado vigente.
    now = _now()
    meta = library.read_meta(audio_hash) or {"hash": audio_hash}
    meta.setdefault("created_at", now)           # solo la 1a vez (upload original)
    meta.update({"nombre": name, "config": cfg,
                 "estado": "procesando", "updated_at": now})
    meta.update(_describe(audio_hash, name))     # title / artist / cover_url
    library.write_meta(audio_hash, meta)
    return job


# --------------------------------------------------------------- consulta ----
def get_job(jid):
    with _cond:
        return _jobs.get(jid)


def queue_position(jid):
    """Posicion 1-based entre los 'queued' (sin contar cancelados).

    Devuelve 0 si esta corriendo, None si ya termino o no aplica.
    """
    with _cond:
        job = _jobs.get(jid)
        if not job:
            return None
        if job["estado"] == "running":
            return 0
        if job["estado"] != "queued":
            return None
        pos = 0
        for pid in _pending:
            pj = _jobs.get(pid)
            if pj and pj["estado"] == "queued":
                pos += 1
                if pid == jid:
                    return pos
        return None


def list_active():
    """Trabajos vivos (en proceso + en cola), con su posicion, en orden."""
    with _cond:
        out = []
        if _current["job_id"]:
            j = _jobs.get(_current["job_id"])
            if j and j["estado"] == "running":
                out.append({"job_id": j["job_id"], "hash": j["hash"],
                            "nombre": j["nombre"], "estado": "running",
                            "posicion": 0})
        pos = 0
        for pid in _pending:
            j = _jobs.get(pid)
            if j and j["estado"] == "queued":
                pos += 1
                out.append({"job_id": j["job_id"], "hash": j["hash"],
                            "nombre": j["nombre"], "estado": "queued",
                            "posicion": pos})
        return out


# --------------------------------------------------------------- cancelar ----
def cancel_job(jid):
    revert_hash = None
    with _cond:
        job = _jobs.get(jid)
        if not job:
            return False
        if job["estado"] == "queued":
            job["estado"] = "cancelled"     # el worker lo saltara; revertimos meta
            revert_hash = job["hash"]
        elif job["estado"] == "running" and _current["job_id"] == jid:
            job["estado"] = "cancelled"
            proc = _current["proc"]
            if proc:
                try:
                    proc.terminate()        # el worker revertira el meta al caer
                except Exception:
                    pass
            return True
        else:
            return False

    # Caso en cola: nunca llego a correr -> revertir meta fuera del lock.
    if revert_hash:
        _revert_meta(revert_hash, was_cancelled=True)
    return True


def _revert_meta(h, was_cancelled):
    """Tras fallo/cancelacion: el resultado vigente manda."""
    now = _now()
    meta = library.read_meta(h) or {"hash": h}
    meta.setdefault("created_at", now)
    if library.zip_path(h).exists():
        estado = "listo"                    # habia un resultado bueno: sigue ahi
    else:
        estado = "cancelado" if was_cancelled else "error"
    meta.update({"estado": estado, "updated_at": now})
    library.write_meta(h, meta)


# ----------------------------------------------------------------- worker ----
def _next_job():
    """Saca el primer job 'queued' real, descartando los cancelados del frente."""
    with _cond:
        while True:
            while _pending:
                head = _jobs.get(_pending[0])
                if not head or head["estado"] != "queued":
                    _pending.pop(0)         # cancelado o invalido: fuera
                else:
                    break
            if _pending:
                return _pending.pop(0)
            _cond.wait()


def _worker():
    while True:
        jid = _next_job()
        with _cond:
            job = _jobs.get(jid)
        if not job or job["estado"] == "cancelled":
            continue

        h = job["hash"]
        job["estado"] = "running"
        _current["job_id"] = jid
        _current["proc"] = None
        tmp_stems = library.entry_dir(h) / "stems_tmp"

        try:
            if tmp_stems.exists():
                shutil.rmtree(tmp_stems)

            inp = library.input_path(h)
            if inp is None:
                raise RuntimeError("no hay input para este hash")

            pipeline.process(
                inp, tmp_stems, job["config"],
                on_proc=lambda p: _current.__setitem__("proc", p))

            if job["estado"] == "cancelled":
                raise RuntimeError("cancelado")

            library.commit_result(h, tmp_stems)   # swap atomico SOLO si OK
            job["estado"] = "done"
            now = _now()
            meta = library.read_meta(h) or {"hash": h}
            meta.setdefault("created_at", now)
            meta.update({"estado": "listo", "config": job["config"],
                         "nombre": job["nombre"], "updated_at": now})
            meta.update(_describe(h, job["nombre"]))   # title / artist / cover_url
            library.write_meta(h, meta)

        except Exception as e:
            was_cancelled = job["estado"] == "cancelled"
            job["estado"] = "cancelled" if was_cancelled else "error"
            job["error"] = str(e)
            shutil.rmtree(tmp_stems, ignore_errors=True)   # tirar el temporal
            _revert_meta(h, was_cancelled)                 # vigente intacto

        finally:
            _current["job_id"] = None
            _current["proc"] = None


def start_worker():
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t
