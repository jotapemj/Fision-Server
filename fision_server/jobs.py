"""
Cola de trabajos + worker en segundo plano. MULTIUSUARIO y PERSISTENTE.

La GPU procesa de UNA en una: un solo worker consume UNA cola GLOBAL en serie.
El aislamiento es de ALMACENAMIENTO (cada job escribe en library/<user>/), NO de
computo: los jobs de todos comparten la misma fila. La posicion mostrada es la
posicion REAL global (honesta).

Cada job lleva su "user". El worker escribe SIEMPRE en la biblioteca de ese
usuario. Las consultas comprueban propiedad: nadie ve ni toca los jobs de otro.

PERSISTENCIA / RECUPERACION (caida a medias):
- La cola de jobs vivos (queued + running) se persiste en queue.json tras cada
  cambio. Solo campos ligeros; el audio ya esta en disco (input.<ext>).
- Al arrancar, recover():
    1. Reencola los jobs que quedaron a medias (limpia su stems_tmp/).
    2. Si un job ya se habia completado antes del crash (meta listo, misma
       config), lo salta (no reprocesa).
    3. Barre metas huerfanas en "procesando" que no esten en la cola y las
       revierte (listo si hay resultado vigente, si no error).
- El historial de jobs terminados NO se persiste (es efimero): tras un reinicio
  /jobs/{id} da 404 y la app debe mirar /library/{hash} (meta = fuente de verdad).

Regla de oro (JP): se procesa en stems_tmp/ y SOLO si sale bien se hace el commit
atomico. Un reproceso fallido o cancelado NO destruye el resultado vigente.
"""
import os
import json
import uuid
import shutil
import threading
from pathlib import Path
from datetime import datetime, timezone

from . import config, library, pipeline, log

_jobs = {}                       # job_id -> dict
_pending = []                    # ids en orden de llegada (los 'queued')
_cond = threading.Condition()    # señaliza trabajo nuevo y protege la cola
_current = {"job_id": None, "proc": None}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _describe(lib, h, name):
    """Campos descriptivos del meta: title/artist (de las tags) y cover_url."""
    title, artist = library.read_tags(lib.input_path(h))
    return {
        "title": title or Path(name).stem,
        "artist": artist or "",
        "cover_url": library.cover_url(h),
    }


# ------------------------------------------------------------ persistencia ----
_PERSIST_KEYS = ("job_id", "user", "hash", "nombre", "config", "reprocess")


def _persist():
    """Vuelca la cola viva (running + queued) a queue.json. Llamar con el lock."""
    items = []
    cur = _jobs.get(_current["job_id"]) if _current["job_id"] else None
    if cur and cur["estado"] == "running":
        items.append({k: cur.get(k) for k in _PERSIST_KEYS})
    for jid in _pending:
        j = _jobs.get(jid)
        if j and j["estado"] == "queued":
            items.append({k: j.get(k) for k in _PERSIST_KEYS})
    tmp = Path(str(config.QUEUE_FILE) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"jobs": items}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config.QUEUE_FILE)      # escritura atomica


def _load_queue():
    p = Path(config.QUEUE_FILE)
    if not p.exists():
        return []
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("jobs", [])
    except Exception:
        return []                            # queue.json corrupto -> el barrido salva


# ----------------------------------------------------------------- crear ----
def create_job(user, audio_hash, name, cfg, reprocess=False):
    """Encola un trabajo para 'user'. El user viaja con el job hasta el disco."""
    jid = uuid.uuid4().hex[:12]
    job = {"job_id": jid, "user": user, "hash": audio_hash, "nombre": name,
           "config": cfg, "estado": "queued", "error": "", "creado": _now(),
           "reprocess": reprocess}
    with _cond:
        _jobs[jid] = job
        _pending.append(jid)
        _persist()
        _cond.notify()

    # Reflejar en meta que esta en cola/proceso, SIN tocar el resultado vigente.
    lib = library.for_user(user)
    now = _now()
    meta = lib.read_meta(audio_hash) or {"hash": audio_hash}
    meta.setdefault("created_at", now)
    meta.update({"nombre": name, "config": cfg,
                 "estado": "procesando", "updated_at": now})
    meta.update(_describe(lib, audio_hash, name))
    lib.write_meta(audio_hash, meta)
    return job


# --------------------------------------------------------------- consulta ----
def _owned(job, user):
    return job is not None and (user is None or job.get("user") == user)


def get_job(jid, user=None):
    with _cond:
        job = _jobs.get(jid)
        return job if _owned(job, user) else None


def queue_position(jid, user=None):
    """Posicion 1-based GLOBAL entre los 'queued'. 0 si corre, None si no aplica."""
    with _cond:
        job = _jobs.get(jid)
        if not _owned(job, user):
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


def list_active(user):
    """Trabajos VIVOS del usuario (proceso + cola), con posicion GLOBAL real."""
    with _cond:
        out = []
        cur = _jobs.get(_current["job_id"]) if _current["job_id"] else None
        if cur and cur["estado"] == "running" and cur.get("user") == user:
            out.append({"job_id": cur["job_id"], "hash": cur["hash"],
                        "nombre": cur["nombre"], "estado": "running",
                        "posicion": 0})
        pos = 0
        for pid in _pending:
            j = _jobs.get(pid)
            if j and j["estado"] == "queued":
                pos += 1
                if j.get("user") == user:
                    out.append({"job_id": j["job_id"], "hash": j["hash"],
                                "nombre": j["nombre"], "estado": "queued",
                                "posicion": pos})
        return out


# --------------------------------------------------------------- cancelar ----
def cancel_job(jid, user=None):
    revert = None
    with _cond:
        job = _jobs.get(jid)
        if not _owned(job, user):
            return False
        if job["estado"] == "queued":
            job["estado"] = "cancelled"
            revert = (job["user"], job["hash"])
            _persist()
        elif job["estado"] == "running" and _current["job_id"] == jid:
            job["estado"] = "cancelled"
            proc = _current["proc"]
            if proc:
                try:
                    proc.terminate()
                except Exception:
                    pass
            return True                      # el worker limpia y persiste al caer
        else:
            return False

    if revert:
        _revert_meta(revert[0], revert[1], was_cancelled=True)
    return True


def _revert_meta(user, h, was_cancelled):
    """Tras fallo/cancelacion: el resultado vigente manda."""
    lib = library.for_user(user)
    now = _now()
    meta = lib.read_meta(h) or {"hash": h}
    meta.setdefault("created_at", now)
    if lib.zip_path(h).exists():
        estado = "listo"
    else:
        estado = "cancelado" if was_cancelled else "error"
    meta.update({"estado": estado, "updated_at": now})
    lib.write_meta(h, meta)


# ------------------------------------------------------------- recuperacion ----
def recover():
    """Arranque tras (posible) caida. Reencola jobs a medias y limpia huerfanas."""
    known = set()
    recovered = 0
    with _cond:
        for j in _load_queue():
            if not all(k in j and j[k] is not None
                       for k in ("job_id", "user", "hash", "config")):
                continue
            if not library.valid_username(j["user"]) or not library.valid_hash(j["hash"]):
                continue
            lib = library.for_user(j["user"])
            # ya completado antes del crash (mismo config)? no reprocesar
            meta = lib.read_meta(j["hash"])
            if meta and meta.get("estado") == "listo" and meta.get("config") == j["config"]:
                known.add((j["user"], j["hash"]))
                continue
            lib.discard_tmp(j["hash"])        # tira el stems_tmp/ a medias
            job = {"job_id": j["job_id"], "user": j["user"], "hash": j["hash"],
                   "nombre": j.get("nombre") or j["hash"], "config": j["config"],
                   "estado": "queued", "error": "", "creado": _now(),
                   "reprocess": j.get("reprocess", True)}
            _jobs[job["job_id"]] = job
            _pending.append(job["job_id"])
            known.add((j["user"], j["hash"]))
            recovered += 1
        _persist()

    # Barrido de metas huerfanas fuera del lock (I/O de disco).
    swept = 0
    for user in library.list_user_dirs():
        lib = library.for_user(user)
        for meta in lib.list_entries():
            if meta.get("estado") == "procesando" and (user, meta.get("hash")) not in known:
                lib.discard_tmp(meta["hash"])
                _revert_meta(user, meta["hash"], was_cancelled=False)
                swept += 1

    log.event("queue.recover", recovered=recovered, swept=swept)
    if recovered:
        with _cond:
            _cond.notify()


# ----------------------------------------------------------------- worker ----
def _next_job():
    with _cond:
        while True:
            while _pending:
                head = _jobs.get(_pending[0])
                if not head or head["estado"] != "queued":
                    _pending.pop(0)
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
            with _cond:
                _persist()
            continue

        user = job["user"]
        h = job["hash"]
        lib = library.for_user(user)
        with _cond:
            job["estado"] = "running"
            _current["job_id"] = jid
            _current["proc"] = None
            _persist()                        # queda registrado como 'running'
        tmp_stems = lib.entry_dir(h) / "stems_tmp"

        try:
            if tmp_stems.exists():
                shutil.rmtree(tmp_stems)

            inp = lib.input_path(h)
            if inp is None:
                raise RuntimeError("no hay input para este hash")

            pipeline.process(
                inp, tmp_stems, job["config"],
                on_proc=lambda p: _current.__setitem__("proc", p))

            if job["estado"] == "cancelled":
                raise RuntimeError("cancelado")

            lib.commit_result(h, tmp_stems)
            job["estado"] = "done"
            now = _now()
            meta = lib.read_meta(h) or {"hash": h}
            meta.setdefault("created_at", now)
            meta.update({"estado": "listo", "config": job["config"],
                         "nombre": job["nombre"], "updated_at": now})
            meta.update(_describe(lib, h, job["nombre"]))
            lib.write_meta(h, meta)

        except Exception as e:
            was_cancelled = job["estado"] == "cancelled"
            job["estado"] = "cancelled" if was_cancelled else "error"
            job["error"] = str(e)
            shutil.rmtree(tmp_stems, ignore_errors=True)
            _revert_meta(user, h, was_cancelled)

        finally:
            with _cond:
                _current["job_id"] = None
                _current["proc"] = None
                _persist()                    # el job ya no esta vivo -> sale de queue.json


def start_worker():
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t
