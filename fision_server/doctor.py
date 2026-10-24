"""
Diagnostico de la instalacion. Lo primero ante un problema:

    python -m fision_server.doctor

Revisa Python, ffmpeg, carpetas, GPU (nvidia-smi, torch, Whisper), pesos y
cuentas. Una linea por punto: OK, WARN (funciona, pero conviene mirarlo) o
FAIL (algo no va a funcionar). Sale con 1 si hay algun FAIL.

Ejecutalo con las mismas envs que el servicio: sin ellas mira ./data,
./library y ~/.cache, no las carpetas reales.
"""
import os
import sys
import shutil
import importlib.metadata as md
from pathlib import Path

from . import config, users, gpu
from .gpu_probe import run as probe_gpu, assess

_counts = {"OK": 0, "WARN": 0, "FAIL": 0}

# Que hacer ante cada problema de GPU (mismos codes que /server/info)
_GPU_HINTS = {
    "torch_error": "torch no se importa: reinstala PyTorch (README, Install)",
    "torch_no_cuda": "torch no ve CUDA: driver viejo o torch de CPU. "
                     "Comprueba el indice (cu129 / cu126) y el driver",
    "arch_unsupported": "esta GPU no esta en el wheel de torch: "
                        "Pascal y anteriores usan el indice cu126",
    "whisper_no_cuda": "Whisper (CTranslate2) no ve CUDA: las letras iran en CPU",
    "smi_missing": "torch ve CUDA pero no hay nvidia-smi: sin el no se mide la "
                   "VRAM. En Docker: NVIDIA_DRIVER_CAPABILITIES=compute,utility",
}

_FREE_MIN_GB = 10


def report(level, msg, hint=""):
    _counts[level] += 1
    print(f"[{level:>4}] {msg}")
    if hint:
        print(f"       -> {hint}")


def _gb(n):
    return f"{n / 1024 ** 3:.1f} GB"


def _writable(label, path):
    path = Path(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".doctor_write_test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        report("FAIL", f"{label}: {path} no se puede escribir ({e})")
        return False
    free = shutil.disk_usage(path).free
    if free < _FREE_MIN_GB * 1024 ** 3:
        report("WARN", f"{label}: {path} ({_gb(free)} libres)",
               f"menos de {_FREE_MIN_GB} GB libres")
    else:
        report("OK", f"{label}: {path} ({_gb(free)} libres)")
    return True


def _installed(name):
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None


# --------------------------------------------------------------- secciones ----
def check_system():
    print("Sistema")
    v = sys.version_info
    if (v.major, v.minor) == (3, 12):
        report("OK", f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        report("WARN", f"Python {v.major}.{v.minor}.{v.micro}",
               "el stack esta validado con Python 3.12")
    if shutil.which("ffmpeg"):
        report("OK", "ffmpeg")
    else:
        report("FAIL", "ffmpeg no esta en el PATH", "sudo apt install ffmpeg")


def check_paths(models):
    print("\nCarpetas")
    if not os.environ.get("FISION_DATA") or not os.environ.get("FISION_LIBRARY"):
        report("WARN", "FISION_DATA / FISION_LIBRARY sin definir: se usan las "
               "carpetas por defecto del directorio actual",
               "ejecuta con las envs del servicio: "
               "export $(systemctl show fision -p Environment --value)")
    _writable("datos", config.DATA_DIR)
    _writable("biblioteca", config.LIBRARY_DIR)
    for label, path in models.items():
        _writable(f"pesos ({label})", path)


def check_gpu():
    print("\nGPU")
    gpu.detect()
    info = gpu.info()
    if info["gpu"]:
        report("OK", f"nvidia-smi: {info['gpu_name']}, {info['vram_total_mb']} MB, "
               f"driver {info['driver']}")
    else:
        report("WARN", "nvidia-smi no ve ninguna GPU NVIDIA: todo ira en CPU (lento)")

    probe = probe_gpu()
    if probe["torch"]:
        cuda = probe["torch_cuda"] or "cpu"
        report("OK", f"torch {probe['torch']} (CUDA {cuda}, cuDNN {probe['cudnn'] or '-'})")
    _, problem = assess(info["gpu"], probe)
    if problem:
        level = "WARN" if problem == "whisper_no_cuda" else "FAIL"
        report(level, f"GPU no utilizable: {problem}"
               + (f" ({probe['error']})" if probe["error"] else ""),
               _GPU_HINTS[problem])
    elif probe["cuda_available"]:
        report("OK", f"torch y Whisper usan la GPU (sm {probe['capability']})")

    # Una sola version de CUDA: Whisper solo existe para CUDA 12
    major = (probe["torch_cuda"] or "").split(".")[0]
    if major and major != "12":
        report("WARN", f"torch con CUDA {probe['torch_cuda']}",
               "Whisper solo trae CUDA 12: instala torch del indice cu129 (o cu126)")
    if _installed("nvidia-cudnn-cu12") and _installed("nvidia-cudnn-cu13"):
        report("WARN", "nvidia-cudnn-cu12 y -cu13 instalados a la vez",
               "se pisan los ficheros; reinstala el venv segun el README")
    if _installed("onnxruntime-gpu"):
        report("WARN", "onnxruntime-gpu instalado", "no se usa; pip uninstall onnxruntime-gpu")


def check_weights(models):
    print("\nPesos")
    btc = config.REPO_DIR / "BTC-ISMIR19" / "test" / "btc_model_large_voca.pt"
    if btc.is_file():
        report("OK", "BTC (acordes)")
    else:
        report("FAIL", "falta el modelo de BTC",
               "git submodule update --init --recursive")
    if (config.REPO_DIR / "resources" / "metronome.wav").is_file():
        report("OK", "metronomo")
    else:
        report("FAIL", "falta resources/metronome.wav")

    import audio_separator_script as sep
    missing = [m for m in sep.ALL_MODELS if not (Path(models["separador"]) / m).is_file()]
    _weights("separacion", missing, len(sep.ALL_MODELS))

    import headless_fisionAI as pipe
    ckpts = Path(models["beat_this"]) / "hub" / "checkpoints"
    missing = [c for c in pipe.BEAT_THIS_CHECKPOINTS
               if not (ckpts / f"beat_this-{c}.ckpt").is_file()]
    _weights("beat_this", missing, len(pipe.BEAT_THIS_CHECKPOINTS))

    try:
        from faster_whisper.utils import download_model
        download_model(config.WHISPER_MODEL, local_files_only=True)
        report("OK", f"Whisper {config.WHISPER_MODEL}")
    except Exception:
        report("WARN", f"Whisper {config.WHISPER_MODEL} sin descargar",
               "python -m fision_server.prefetch")


def _weights(label, missing, total):
    if not missing:
        report("OK", f"{label}: {total} de {total}")
    else:
        report("WARN", f"{label}: faltan {', '.join(missing)}",
               "se bajan en el primer trabajo; mejor antes: "
               "python -m fision_server.prefetch")


def check_accounts():
    print("\nCuentas")
    names = users.list_users()
    if not names:
        report("WARN", "no hay usuarios", "python -m fision_server.useradd <usuario>")
    elif not users.admins():
        report("WARN", f"{len(names)} usuarios, ninguno admin",
               "python -m fision_server.usermod <usuario> --admin")
    else:
        report("OK", f"{len(names)} usuarios, admin: {', '.join(users.admins())}")


def model_dirs():
    """Carpetas de pesos efectivas (FISION_MODELS_DIR rellena las tres)."""
    sys.path.insert(0, str(config.REPO_DIR))
    import headless_fisionAI  # noqa: F401  (aplica FISION_MODELS_DIR)
    import audio_separator_script as sep
    return {
        "separador": sep.model_dir(),
        "Whisper": os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface"),
        "beat_this": os.environ.get("TORCH_HOME") or os.path.expanduser("~/.cache/torch"),
    }


def main():
    from . import __version__
    print(f"Fision {__version__} - doctor\n")
    models = model_dirs()
    check_system()
    check_paths(models)
    check_gpu()
    check_weights(models)
    check_accounts()
    c = _counts
    print(f"\n{c['OK']} OK, {c['WARN']} WARN, {c['FAIL']} FAIL")
    sys.exit(1 if c["FAIL"] else 0)


if __name__ == "__main__":
    main()
