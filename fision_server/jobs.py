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
import time
import uuid
import shutil
import threading
from pathlib import Path
from datetime import datetime, timezone

from . import config, library, pipeline, log, gpu

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
_PERSIST_KEYS = ("job_id", "user", "hash", "nombre", "config", "reprocess", "kind")


def _persist():
    """Vuelca la cola viva (running + queued) a queue.json. Llamar con el lock."""
    items = []
    cur = _jobs.get(_current["job_id"]) if _current["job_id"] else None
    if cur and cur["estado"] in ("running", "waiting_gpu"):
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
def create_job(user, audio_hash, name, cfg, reprocess=False, kind="separation"):
    """Encola un trabajo para 'user'. El user viaja con el job hasta el disco.

    kind:
      - "separation" (por defecto): pipeline completo (stems + beats + chords).
      - "lyrics": transcripcion Whisper sobre el vocals.flac vigente.
    Un job de lyrics NO toca meta ni result.zip: escribe lyrics.json dentro
    de stems/ y punto. La meta principal solo la mueven jobs de separacion.
    """
    jid = uuid.uuid4().hex[:12]
    job = {"job_id": jid, "user": user, "hash": audio_hash, "nombre": name,
           "config": cfg, "estado": "queued", "error": "", "creado": _now(),
           "reprocess": reprocess, "kind": kind}
    with _cond:
        _jobs[jid] = job
        _pending.append(jid)
        _persist()
        _cond.notify()

    # La meta de la biblioteca solo la tocan los jobs de separacion. Un job
    # de lyrics no altera el estado "listo" del resultado principal.
    if kind == "separation":
        lib = library.for_user(user)
        now = _now()
        meta = lib.read_meta(audio_hash) or {"hash": audio_hash}
        meta.setdefault("created_at", now)
        meta.update({"nombre": name, "config": cfg,
                     "estado": "procesando", "updated_at": now})
        meta.update(_describe(lib, audio_hash, name))
        lib.write_meta(audio_hash, meta)
    return job


def has_active_lyrics(user, audio_hash):
    """True si ya hay un job de lyrics vivo (queued/running/waiting_gpu) para
    (user, hash). Usado por el endpoint POST /library/{h}/lyrics para evitar
    duplicados."""
    with _cond:
        for j in _jobs.values():
            if (j.get("kind") == "lyrics"
                    and j.get("user") == user
                    and j.get("hash") == audio_hash
                    and j["estado"] in ("queued", "running", "waiting_gpu")):
                return True
    return False


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
        if job["estado"] in ("running", "waiting_gpu"):
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
        if cur and cur["estado"] in ("running", "waiting_gpu") and cur.get("user") == user:
            out.append({"job_id": cur["job_id"], "hash": cur["hash"],
                        "nombre": cur["nombre"], "estado": cur["estado"],
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


def list_all_active():
    """Trabajos VIVOS de todos los usuarios (panel admin), en orden de la cola."""
    with _cond:
        out = []
        cur = _jobs.get(_current["job_id"]) if _current["job_id"] else None
        if cur and cur["estado"] in ("running", "waiting_gpu"):
            out.append({"job_id": cur["job_id"], "user": cur["user"],
                        "hash": cur["hash"], "nombre": cur["nombre"],
                        "kind": cur.get("kind", "separation"),
                        "estado": cur["estado"], "posicion": 0,
                        "creado": cur.get("creado", "")})
        pos = 0
        for pid in _pending:
            j = _jobs.get(pid)
            if j and j["estado"] == "queued":
                pos += 1
                out.append({"job_id": j["job_id"], "user": j["user"],
                            "hash": j["hash"], "nombre": j["nombre"],
                            "kind": j.get("kind", "separation"),
                            "estado": "queued", "posicion": pos,
                            "creado": j.get("creado", "")})
        return out


# --------------------------------------------------------------- cancelar ----
def cancel_job(jid, user=None):
    revert = None
    to_stop = None
    with _cond:
        job = _jobs.get(jid)
        if not _owned(job, user):
            return False
        if job["estado"] == "queued":
            job["estado"] = "cancelled"
            revert = (job["user"], job["hash"])
            _persist()
        elif job["estado"] in ("running", "waiting_gpu") and _current["job_id"] == jid:
            job["estado"] = "cancelled"
            # Si estaba en waiting_gpu no hay proc: el bucle de espera detecta
            # el cambio a "cancelled" y sale limpio.
            to_stop = _current["proc"]
        else:
            return False

    if to_stop:
        # Fuera del lock y en hilo: stop() puede esperar la gracia del SIGTERM.
        # Mata el grupo entero (orquestador + separador). El worker limpia.
        threading.Thread(target=pipeline.stop, args=(to_stop,), daemon=True).start()
        return True

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
            kind = j.get("kind") or "separation"
            if kind == "separation":
                lib.discard_tmp(j["hash"])    # tira el stems_tmp/ a medias
            # kind=lyrics no toca stems_tmp/: escribe directo en stems/ y si
            # cayo a medias, en el peor caso deja un lyrics.json parcial que
            # el reintento sobreescribe atomicamente.
            job = {"job_id": j["job_id"], "user": j["user"], "hash": j["hash"],
                   "nombre": j.get("nombre") or j["hash"], "config": j["config"],
                   "estado": "queued", "error": "", "creado": _now(),
                   "reprocess": j.get("reprocess", True), "kind": kind}
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

    # Subidas cortadas por la caida: al arrancar no hay ninguna en vuelo.
    for f in config.INCOMING_DIR.iterdir():
        try:
            f.unlink()
        except OSError:
            pass

    log.event("queue.recover", recovered=recovered, swept=swept)
    if recovered:
        with _cond:
            _cond.notify()


# ------------------------------------------------------------------ GPU ----
def _wants_gpu(cfg):
    """Formato rico del cliente lleva 'device'. Plano no -> asume CPU."""
    return isinstance(cfg, dict) and cfg.get("device") == "cuda"


def _force_cpu(cfg):
    """Degrada device a cpu (server sin GPU). Copia defensiva del dict."""
    if isinstance(cfg, dict) and cfg.get("device") == "cuda":
        cfg = dict(cfg)
        cfg["device"] = "cpu"
    return cfg


def _wait_for_gpu(jid, min_vram_mb=None):
    """Espera a que haya VRAM libre >= min_vram_mb (default: GPU_MIN_VRAM_MB).

    True si llega a tiempo, False si timeout o cancelacion. Marca el job como
    'waiting_gpu' mientras espera; lo devuelve a 'running' cuando pasa.

    El umbral es parametrizable para que un job de lyrics use su propio
    minimo (WHISPER_MIN_VRAM_MB, mas bajo que el de separacion).
    """
    if min_vram_mb is None:
        min_vram_mb = config.GPU_MIN_VRAM_MB
    if gpu.vram_free_mb() >= min_vram_mb:
        return True                             # camino rapido: VRAM libre al primer intento

    deadline = time.time() + config.GPU_WAIT_TIMEOUT_SEC
    with _cond:
        job = _jobs.get(jid)
        if not job or job["estado"] == "cancelled":
            return False
        job["estado"] = "waiting_gpu"
        _persist()
    log.event("job.waiting_gpu", job_id=jid)

    while True:
        # Cancelacion durante la espera -> salir limpio.
        with _cond:
            job = _jobs.get(jid)
            if not job or job["estado"] == "cancelled":
                return False
        if time.time() >= deadline:
            log.event("job.gpu_timeout", job_id=jid)
            return False
        time.sleep(config.GPU_POLL_SEC)
        if gpu.vram_free_mb() >= min_vram_mb:
            with _cond:
                job = _jobs.get(jid)
                if not job or job["estado"] == "cancelled":
                    return False
                job["estado"] = "running"
                _persist()
            log.event("job.gpu_ready", job_id=jid)
            return True


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


def _preserve_lyrics(lib, h, tmp_stems):
    """Copia lyrics.json vigente a stems_tmp antes del swap. Idempotente.

    Al reprocesar la separacion queremos que la letra generada previamente
    sobreviva al `commit_result` atomico (que destruye stems/ y lo sustituye
    por stems_tmp/). Copiamos aqui; si no habia lyrics.json vigente, no
    pasa nada.
    """
    src = lib.stems_dir(h) / "lyrics.json"
    if not src.exists():
        return
    dst = Path(tmp_stems) / "lyrics.json"
    try:
        shutil.copy2(src, dst)
    except Exception as e:
        log.event("lyrics.preserve_fail", hash=h, error=str(e))


def _run_separation(job, lib):
    """Camino clasico del pipeline (stems + beats + chords)."""
    jid = job["job_id"]
    h = job["hash"]
    tmp_stems = lib.entry_dir(h) / "stems_tmp"

    # Degrada silenciosamente si el cliente pidio cuda pero el server
    # no tiene GPU (o la deteccion fallo). El pipeline sigue en CPU.
    if _wants_gpu(job["config"]) and not gpu.has_gpu():
        job["config"] = _force_cpu(job["config"])
        log.event("job.gpu_fallback", job_id=jid, reason="no_gpu")

    # Gate de VRAM: si el job pide cuda, espera a tener libre >= minimo.
    if _wants_gpu(job["config"]):
        if not _wait_for_gpu(jid, min_vram_mb=config.GPU_MIN_VRAM_MB):
            if _jobs.get(jid, {}).get("estado") == "cancelled":
                raise RuntimeError("cancelado")
            raise RuntimeError("GPU ocupada")

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

    # Al reprocesar, preserva lyrics.json vigente antes del swap atomico.
    if job.get("reprocess"):
        _preserve_lyrics(lib, h, tmp_stems)

    lib.commit_result(h, tmp_stems)
    now = _now()
    meta = lib.read_meta(h) or {"hash": h}
    meta.setdefault("created_at", now)
    meta.update({"estado": "listo", "config": job["config"],
                 "nombre": job["nombre"], "updated_at": now})
    meta.update(_describe(lib, h, job["nombre"]))
    lib.write_meta(h, meta)


def _run_lyrics(job, lib):
    """Job de lyrics: Whisper sobre vocals.flac vigente.

    Escribe directamente en stems/ (no usa stems_tmp/, no toca zip ni meta).
    Requiere que vocals.flac exista: el endpoint que encola ya lo comprueba
    antes, pero comprobamos otra vez por si el fichero se ha borrado entre
    encolado y ejecucion.
    """
    jid = job["job_id"]
    h = job["hash"]
    vocals = lib.stems_dir(h) / "vocals.flac"
    if not vocals.exists():
        raise RuntimeError("vocals.flac no disponible; procesa la cancion primero")

    # Elegir device: si el server no tiene GPU, degradamos a CPU/int8 igual
    # que hace la separacion. Si la tiene, pasamos por el gate de VRAM.
    if gpu.has_gpu():
        if not _wait_for_gpu(jid, min_vram_mb=config.WHISPER_MIN_VRAM_MB):
            if _jobs.get(jid, {}).get("estado") == "cancelled":
                raise RuntimeError("cancelado")
            raise RuntimeError("GPU ocupada")
        device = config.WHISPER_DEVICE
        compute = config.WHISPER_COMPUTE
    else:
        device = "cpu"
        compute = "int8"
        log.event("job.gpu_fallback", job_id=jid, reason="no_gpu")

    if job["estado"] == "cancelled":
        raise RuntimeError("cancelado")

    lang = None
    if isinstance(job.get("config"), dict):
        lang = job["config"].get("language") or None

    pipeline.process_lyrics(
        vocals, lib.stems_dir(h), language=lang,
        device=device, compute=compute,
        on_proc=lambda p: _current.__setitem__("proc", p))


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
        kind = job.get("kind") or "separation"
        lib = library.for_user(user)
        with _cond:
            job["estado"] = "running"
            _current["job_id"] = jid
            _current["proc"] = None
            _persist()                        # queda registrado como 'running'

        try:
            if kind == "lyrics":
                _run_lyrics(job, lib)
            else:
                _run_separation(job, lib)

            if job["estado"] == "cancelled":
                raise RuntimeError("cancelado")
            job["estado"] = "done"

        except Exception as e:
            if isinstance(e, pipeline.JobTimeout):
                log.event("job.timeout", job_id=jid, kind=kind)
            was_cancelled = job["estado"] == "cancelled"
            job["estado"] = "cancelled" if was_cancelled else "error"
            job["error"] = str(e)
            if kind == "separation":
                tmp_stems = lib.entry_dir(h) / "stems_tmp"
                shutil.rmtree(tmp_stems, ignore_errors=True)
                _revert_meta(user, h, was_cancelled)
            # kind=lyrics no toca meta ni deja tmp; nada que revertir.

        finally:
            with _cond:
                _current["job_id"] = None
                _current["proc"] = None
                _persist()                    # el job ya no esta vivo -> sale de queue.json


def start_worker():
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t
