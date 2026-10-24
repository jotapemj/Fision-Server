"""
Deteccion de GPU. Fuente unica de verdad: el server, no el cliente.

- Cache al arrancar (modelo + VRAM total + driver). El cliente pregunta a
  /server/info y decide si mandar el job en cuda o cpu.
- VRAM libre en VIVO cuando el worker va a lanzar un job (para el gate del
  minimo de 4 GB).

Usa nvidia-smi (no torch): rapido, no arrastra dependencias pesadas y no
requiere warm-up de CUDA. Si el binario no esta -> sin GPU.

Ademas, al arrancar, una sonda en proceso aparte (gpu_probe) comprueba que
torch y Whisper pueden usar CUDA: sin ella, un driver viejo o un torch de CPU
mandaban todo a CPU en silencio. Resultado en gpu_usable / gpu_problem.
"""
import sys
import json
import threading
import subprocess

from . import config, log
from .gpu_probe import assess

PROBE_TIMEOUT_SEC = 180

_cache = {
    "gpu": False,
    "gpu_name": "",
    "vram_total_mb": 0,
    "driver": "",
    "gpu_usable": None,      # None = sonda en curso
    "gpu_problem": "",
}
_whisper_ok = None


def _query(fields, timeout=3):
    """Consulta a nvidia-smi. Devuelve la primera fila como lista de strings, o None."""
    try:
        r = subprocess.run(
            ["nvidia-smi", f"--query-gpu={fields}",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return [x.strip() for x in r.stdout.splitlines()[0].split(",")]


def detect():
    """Detecta la GPU y cachea. Llamar UNA vez al arrancar el server."""
    row = _query("name,memory.total,driver_version")
    if not row or len(row) < 3:
        return
    _cache["gpu"] = True
    _cache["gpu_name"] = row[0]
    try:
        _cache["vram_total_mb"] = int(row[1])
    except ValueError:
        _cache["vram_total_mb"] = 0
    _cache["driver"] = row[2]


def info():
    """Snapshot cacheado. Lo que ve /server/info."""
    return dict(_cache)


def has_gpu():
    return _cache["gpu"]


def vram_free_mb():
    """VRAM libre AHORA (en vivo, no cache). 0 si no hay GPU o si falla la query."""
    if not _cache["gpu"]:
        return 0
    row = _query("memory.free")
    if not row:
        return 0
    try:
        return int(row[0])
    except ValueError:
        return 0


# ------------------------------------------------------------------ sonda ----
def _probe():
    global _whisper_ok
    try:
        r = subprocess.run([sys.executable, "-m", "fision_server.gpu_probe"],
                           capture_output=True, text=True, timeout=PROBE_TIMEOUT_SEC,
                           cwd=str(config.REPO_DIR))
        probe = json.loads(r.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as e:
        probe = {"error": f"probe: {e}"}
    usable, problem = assess(_cache["gpu"], probe)
    _cache["gpu_usable"] = usable
    _cache["gpu_problem"] = problem
    _whisper_ok = usable and problem != "whisper_no_cuda"
    log.event("gpu.check", usable=usable, problem=problem,
              torch=probe.get("torch"), cuda=probe.get("torch_cuda"),
              cudnn=probe.get("cudnn"), capability=probe.get("capability"))
    if problem:
        log.event("gpu.unusable", problem=problem, error=probe.get("error"))


def start_probe():
    """Lanza la sonda en segundo plano (tarda segundos: importa torch)."""
    threading.Thread(target=_probe, name="gpu-probe", daemon=True).start()


def usable():
    """¿La separacion puede ir en GPU? Mientras la sonda no acaba, manda
    nvidia-smi (comportamiento de siempre)."""
    u = _cache["gpu_usable"]
    return _cache["gpu"] if u is None else u


def whisper_usable():
    """¿Whisper puede ir en GPU?"""
    return usable() if _whisper_ok is None else _whisper_ok
