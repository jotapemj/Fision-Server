"""
Deteccion de GPU. Fuente unica de verdad: el server, no el cliente.

- Cache al arrancar (modelo + VRAM total + driver). El cliente pregunta a
  /server/info y decide si mandar el job en cuda o cpu.
- VRAM libre en VIVO cuando el worker va a lanzar un job (para el gate del
  minimo de 4 GB).

Usa nvidia-smi (no torch): rapido, no arrastra dependencias pesadas y no
requiere warm-up de CUDA. Si el binario no esta -> sin GPU.
"""
import subprocess

_cache = {
    "gpu": False,
    "gpu_name": "",
    "vram_total_mb": 0,
    "driver": "",
}


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
