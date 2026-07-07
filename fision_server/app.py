"""
API HTTP de Fision. La app movil habla con estos endpoints.

  POST   /jobs                      sube audio + config -> encola (o cache hit)
  POST   /library/{hash}/reprocess  reprocesa el input guardado con nueva config
  GET    /jobs/{job_id}             estado del trabajo
  DELETE /jobs/{job_id}             cancela (cola o en marcha)
  GET    /library                   lista de canciones en "mi nube"
  GET    /library/{hash}            meta de una cancion
  GET    /library/{hash}/zip        descarga el result.zip vigente
  DELETE /library/{hash}            borra la entrada entera
"""
import os
import json
import tempfile

from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager

from . import config, library, jobs
from .auth import require_token


@asynccontextmanager
async def lifespan(app):
    jobs.start_worker()               # arranca el worker al levantar el server
    yield


app = FastAPI(title="Fision AI Server", lifespan=lifespan)


@app.get("/health")
def health():
    """Ping de conexion para el icono verde/rojo de la app. Sin token."""
    return {"status": "ok"}


def _public_meta(m):
    return {"hash": m.get("hash"), "nombre": m.get("nombre"),
            "title": m.get("title"), "artist": m.get("artist"),
            "cover_url": m.get("cover_url"),
            "estado": m.get("estado"), "config": m.get("config"),
            "created_at": m.get("created_at"),
            "updated_at": m.get("updated_at")}


async def _save_upload(file: UploadFile):
    """Vuelca el upload a un temporal y devuelve (ruta_temp, extension)."""
    ext = os.path.splitext(file.filename or "audio")[1].lower() or ".bin"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    size = 0
    try:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > config.MAX_UPLOAD_MB * 1024 * 1024:
                tmp.close(); os.remove(tmp.name)
                raise HTTPException(413, "Archivo demasiado grande")
            tmp.write(chunk)
    finally:
        tmp.close()
    return tmp.name, ext


@app.post("/jobs", dependencies=[Depends(require_token)])
async def submit(file: UploadFile = File(...), config_json: str = Form("{}")):
    cfg = json.loads(config_json or "{}")
    tmp_path, ext = await _save_upload(file)
    h = library.hash_file(tmp_path)

    # Cache hit: mismo audio + misma config ya listos -> no reprocesar.
    meta = library.read_meta(h)
    if meta and meta.get("estado") == "listo" and meta.get("config") == cfg:
        os.remove(tmp_path)
        return {"hash": h, "estado": "listo", "cached": True}

    library.save_input(h, tmp_path, ext)          # guarda input (si es nuevo)
    job = jobs.create_job(h, file.filename or h, cfg, reprocess=bool(meta))
    return {"hash": h, "job_id": job["job_id"],
            "estado": job["estado"], "cached": False}


@app.post("/library/{h}/reprocess", dependencies=[Depends(require_token)])
async def reprocess(h: str, config_json: str = Form("{}")):
    if not library.exists(h) or library.input_path(h) is None:
        raise HTTPException(404, "No existe ese audio")
    cfg = json.loads(config_json or "{}")
    meta = library.read_meta(h) or {}
    job = jobs.create_job(h, meta.get("nombre", h), cfg, reprocess=True)
    return {"hash": h, "job_id": job["job_id"], "estado": job["estado"]}


@app.get("/jobs", dependencies=[Depends(require_token)])
def jobs_active():
    """Cola viva: lo que se procesa + lo que espera, con posicion."""
    return jobs.list_active()


@app.get("/jobs/{jid}", dependencies=[Depends(require_token)])
def job_status(jid: str):
    job = jobs.get_job(jid)
    if not job:
        raise HTTPException(404, "Job no encontrado")
    return {"job_id": jid, "hash": job["hash"], "estado": job["estado"],
            "posicion": jobs.queue_position(jid), "error": job["error"]}


@app.delete("/jobs/{jid}", dependencies=[Depends(require_token)])
def job_cancel(jid: str):
    if not jobs.cancel_job(jid):
        raise HTTPException(404, "No cancelable")
    return {"job_id": jid, "estado": "cancelled"}


@app.get("/library", dependencies=[Depends(require_token)])
def lib_list():
    return [_public_meta(m) for m in library.list_entries()]


@app.get("/library/{h}", dependencies=[Depends(require_token)])
def lib_get(h: str):
    m = library.read_meta(h)
    if not m:
        raise HTTPException(404, "No existe")
    return _public_meta(m)


@app.get("/library/{h}/zip", dependencies=[Depends(require_token)])
def lib_zip(h: str):
    if not library.zip_path(h).exists():
        raise HTTPException(404, "Sin resultado listo")
    m = library.read_meta(h) or {}
    base = os.path.splitext(m.get("nombre") or h)[0]
    return FileResponse(library.zip_path(h), media_type="application/zip",
                        filename=f"{base}_stems.zip")


@app.get("/library/{h}/stems/{filename}", dependencies=[Depends(require_token)])
def lib_stem_file(h: str, filename: str):
    """Sirve un fichero de stems/ (portada artcover.png o un stem para reproducir).

    Coincide con la cover_url del meta: /library/<hash>/stems/artcover.png
    """
    # anti path-traversal: solo un nombre simple, sin separadores
    if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
        raise HTTPException(400, "Nombre invalido")
    base = library.stems_dir(h).resolve()
    target = (base / filename).resolve()
    if target.parent != base or not target.is_file():
        raise HTTPException(404, "No existe")
    return FileResponse(target)


@app.delete("/library/{h}", dependencies=[Depends(require_token)])
def lib_delete(h: str):
    if not library.exists(h):
        raise HTTPException(404, "No existe")
    library.delete_entry(h)
    return {"hash": h, "borrado": True}
