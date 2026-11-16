"""
Deteccion de GPU. Fuente unica de verdad: el server, no el cliente.

- Cache al arrancar (modelo + VRAM total + driver). El cliente pregunta a
  /server/info y decide si mandar el job en cuda o cpu.
- VRAM libre en VIVO cuando el worker va a lanzar un job (para el gate del
  minimo de 4 GB).

Sin torch (rapido, sin warm-up): NVIDIA por nvidia-smi; AMD (ROCm,
experimental) por sysfs del driver amdgpu, solo si existe /dev/kfd (el
dispositivo de computo de ROCm; sin el, una iGPU Ryzen no cuenta). Torch
para ROCm ve la tarjeta como "cuda": el resto del server no distingue.

Ademas, al arrancar, una sonda en proceso aparte (gpu_probe) comprueba que
torch y Whisper pueden usar CUDA: sin ella, un driver viejo o un torch de CPU
mandaban todo a CPU en silencio. Resultado en gpu_usable / gpu_problem.
"""
import os
import sys
import json
import glob
import threading
import subprocess

from . import config, log
from .gpu_probe import assess

PROBE_TIMEOUT_SEC = 180

_cache = {
    "gpu": False,
    "gpu_vendor": "",        # nvidia | amd | "" (sin GPU)
    "gpu_name": "",
    "vram_total_mb": 0,
    "driver": "",
    "gpu_usable": None,      # None = sonda en curso
    "gpu_problem": "",
}
_whisper_ok = None
_amd_dev = None              # /sys/class/drm/cardN/device de la AMD detectada

_KFD = "/dev/kfd"
_DRM = "/sys/class/drm"


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


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _amd_card():
    """La tarjeta AMD con mas VRAM en sysfs (la dGPU, no la iGPU), o None."""
    if not os.path.exists(_KFD):
        return None
    cards = []
    for dev in glob.glob(f"{_DRM}/card[0-9]*/device"):
        if _read(f"{dev}/vendor") != "0x1002":
            continue
        try:
            total = int(_read(f"{dev}/mem_info_vram_total"))
        except ValueError:
            continue
        cards.append((total, dev))
    return max(cards)[1] if cards else None


def _amd_vram(dev, field):
    """VRAM en MB de sysfs (field: total | used). None si no se puede leer."""
    try:
        return int(_read(f"{dev}/mem_info_vram_{field}")) // (1024 * 1024)
    except ValueError:
        return None


def detect():
    """Detecta la GPU y cachea. Llamar UNA vez al arrancar el server."""
    global _amd_dev
    row = _query("name,memory.total,driver_version")
    if row and len(row) >= 3:
        _cache.update(gpu=True, gpu_vendor="nvidia", gpu_name=row[0], driver=row[2])
        try:
            _cache["vram_total_mb"] = int(row[1])
        except ValueError:
            _cache["vram_total_mb"] = 0
        return
    dev = _amd_card()
    if dev is None:
        return
    _amd_dev = dev
    # Nombre comercial: la sonda lo afina con torch; sysfs solo a veces lo trae
    _cache.update(gpu=True, gpu_vendor="amd",
                  gpu_name=_read(f"{dev}/product_name") or "AMD GPU",
                  vram_total_mb=_amd_vram(dev, "total") or 0,
                  driver=_read("/sys/module/amdgpu/version") or f"kernel {os.uname().release}")


def info():
    """Snapshot cacheado. Lo que ve /server/info."""
    return dict(_cache)


def has_gpu():
    return _cache["gpu"]


def vram_free_mb():
    """VRAM libre AHORA (en vivo, no cache). 0 si no hay GPU o si falla la query."""
    if not _cache["gpu"]:
        return 0
    if _cache["gpu_vendor"] == "amd":
        used = _amd_vram(_amd_dev, "used")
        return max(_cache["vram_total_mb"] - used, 0) if used is not None else 0
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
    vendor = _cache["gpu_vendor"]
    if vendor == "amd" and not probe.get("torch_hip"):
        # torch sin ROCm (imagen cpu/cu129): la AMD no se usara, como sin GPU
        _cache.update(gpu=False, gpu_vendor="", gpu_name="", vram_total_mb=0, driver="")
    elif vendor == "amd" and probe.get("device_name"):
        _cache["gpu_name"] = probe["device_name"]
    usable, problem = assess(_cache["gpu"], probe)
    _cache["gpu_usable"] = usable
    _cache["gpu_problem"] = problem
    _whisper_ok = usable and problem != "whisper_no_cuda"
    log.event("gpu.check", usable=usable, problem=problem, vendor=_cache["gpu_vendor"],
              torch=probe.get("torch"), cuda=probe.get("torch_cuda"),
              hip=probe.get("torch_hip"), cudnn=probe.get("cudnn"),
              capability=probe.get("capability"))
    if problem:
        log.event("gpu.unusable", problem=problem, error=probe.get("error"))


def start_probe():
    """Lanza la sonda en segundo plano (tarda segundos: importa torch)."""
    threading.Thread(target=_probe, name="gpu-probe", daemon=True).start()


def usable():
    """¿La separacion puede ir en GPU? Mientras la sonda no acaba, manda
    la deteccion (nvidia-smi o sysfs)."""
    u = _cache["gpu_usable"]
    return _cache["gpu"] if u is None else u


def whisper_usable():
    """¿Whisper puede ir en GPU?"""
    return usable() if _whisper_ok is None else _whisper_ok
