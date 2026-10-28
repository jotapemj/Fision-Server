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

from . import config, users, gpu, demo
from .gpu_probe import run as probe_gpu, assess

_counts = {"OK": 0, "WARN": 0, "FAIL": 0}

# Que hacer ante cada problema de GPU (mismos codes que /server/info)
_GPU_HINTS = {
    "torch_error": "torch cannot be imported: reinstall PyTorch (README, Install)",
    "torch_no_cuda": "torch does not see CUDA: old driver or CPU build of torch. "
                     "Check the index (cu129 / cu126) and the driver",
    "arch_unsupported": "this GPU is not in the torch wheel: "
                        "Pascal and older use the cu126 index",
    "whisper_no_cuda": "Whisper (CTranslate2) does not see CUDA: lyrics will run on the CPU",
    "smi_missing": "torch sees CUDA but nvidia-smi is missing, so VRAM cannot be "
                   "measured. In Docker: NVIDIA_DRIVER_CAPABILITIES=compute,utility",
}

# En Docker: arreglos del lado del host
_GPU_HINTS_DOCKER = {
    "torch_no_cuda": "the container does not see the GPU: install the NVIDIA Container "
                     "Toolkit on the host and run docker compose up -d again",
    "arch_unsupported": "this GPU is not in the image: Pascal and older use "
                        "FISION_IMAGE_TAG=cu126 in .env",
}

_FREE_MIN_GB = 10


def _cmd(c):
    """Comando tal como se escribe: en Docker, dentro del contenedor."""
    return f"docker compose exec fision {c}" if config.IN_DOCKER else c


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
        fix = (f"sudo chown -R {os.getuid()}:{os.getgid()} {config.host_path(path)}"
               if config.IN_DOCKER else "check the owner and permissions of the folder")
        report("FAIL", f"{label}: {config.shown_path(path)} is not writable ({e.strerror})", fix)
        return False
    free = shutil.disk_usage(path).free
    if free < _FREE_MIN_GB * 1024 ** 3:
        report("WARN", f"{label}: {config.shown_path(path)} ({_gb(free)} free)",
               f"less than {_FREE_MIN_GB} GB free")
    else:
        report("OK", f"{label}: {config.shown_path(path)} ({_gb(free)} free)")
    return True


def _installed(name):
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None


# --------------------------------------------------------------- secciones ----
def check_system():
    print("System")
    v = sys.version_info
    if (v.major, v.minor) == (3, 12):
        report("OK", f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        report("WARN", f"Python {v.major}.{v.minor}.{v.micro}",
               "the stack is validated with Python 3.12")
    if shutil.which("ffmpeg"):
        report("OK", "ffmpeg")
    else:
        report("FAIL", "ffmpeg is not in the PATH", "sudo apt install ffmpeg")


def check_paths(models):
    print("\nFolders")
    if not config.IN_DOCKER and (not os.environ.get("FISION_DATA")
                                 or not os.environ.get("FISION_LIBRARY")):
        report("WARN", "FISION_DATA / FISION_LIBRARY not set: using the default "
               "folders in the current directory",
               "run with the service environment: "
               "export $(systemctl show fision -p Environment --value)")
    _writable("data", config.DATA_DIR)
    _writable("library", config.LIBRARY_DIR)
    for label, path in models.items():
        _writable(f"weights ({label})", path)


def check_gpu():
    print("\nGPU")
    gpu.detect()
    info = gpu.info()
    if info["gpu"]:
        report("OK", f"nvidia-smi: {info['gpu_name']}, {info['vram_total_mb']} MB, "
               f"driver {info['driver']}")
    else:
        report("WARN", "nvidia-smi sees no NVIDIA GPU: everything runs on the CPU (slow)",
               "if the host has one: install the NVIDIA Container Toolkit and use "
               "the GPU image" if config.IN_DOCKER else "")

    probe = probe_gpu()
    if probe["torch"]:
        cuda = probe["torch_cuda"] or "cpu"
        report("OK", f"torch {probe['torch']} (CUDA {cuda}, cuDNN {probe['cudnn'] or '-'})")
    _, problem = assess(info["gpu"], probe)
    if problem:
        level = "WARN" if problem == "whisper_no_cuda" else "FAIL"
        report(level, f"GPU not usable: {problem}"
               + (f" ({probe['error']})" if probe["error"] else ""),
               (config.IN_DOCKER and _GPU_HINTS_DOCKER.get(problem)) or _GPU_HINTS[problem])
    elif probe["cuda_available"]:
        report("OK", f"torch and Whisper use the GPU (sm {probe['capability']})")

    # Una sola version de CUDA: Whisper solo existe para CUDA 12
    major = (probe["torch_cuda"] or "").split(".")[0]
    if major and major != "12":
        report("WARN", f"torch built for CUDA {probe['torch_cuda']}",
               "Whisper only ships CUDA 12: install torch from the cu129 (or cu126) index")
    if _installed("nvidia-cudnn-cu12") and _installed("nvidia-cudnn-cu13"):
        report("WARN", "nvidia-cudnn-cu12 and -cu13 installed together",
               "they overwrite each other's files; rebuild the venv as in the README")
    if _installed("onnxruntime-gpu"):
        report("WARN", "onnxruntime-gpu installed", "unused; pip uninstall onnxruntime-gpu")


def check_weights(models):
    print("\nWeights")
    btc = config.REPO_DIR / "BTC-ISMIR19" / "test" / "btc_model_large_voca.pt"
    if btc.is_file():
        report("OK", "BTC (chords)")
    else:
        report("FAIL", "BTC model missing",
               "git submodule update --init --recursive")
    if (config.REPO_DIR / "resources" / "metronome.wav").is_file():
        report("OK", "metronome")
    else:
        report("FAIL", "resources/metronome.wav missing")

    import audio_separator_script as sep
    missing = [m for m in sep.ALL_MODELS if not (Path(models["separator"]) / m).is_file()]
    _weights("separation", missing, len(sep.ALL_MODELS))

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
        report("WARN", f"Whisper {config.WHISPER_MODEL} not downloaded",
               _cmd("python -m fision_server.prefetch"))


def _weights(label, missing, total):
    if not missing:
        report("OK", f"{label}: {total} of {total}")
    else:
        report("WARN", f"{label}: missing {', '.join(missing)}",
               "downloaded by the first job; better beforehand: "
               + _cmd("python -m fision_server.prefetch"))


def check_accounts():
    print("\nAccounts")
    names = users.list_users()
    count = f"{len(names)} user{'' if len(names) == 1 else 's'}"
    if not names:
        report("WARN", "no users", _cmd("python -m fision_server.useradd <name>"))
    elif not users.admins():
        report("WARN", f"{count}, no administrator",
               _cmd("python -m fision_server.usermod <name> --admin"))
    else:
        report("OK", f"{count}, admin: {', '.join(users.admins())}")
    songs = demo.entries()
    if songs:
        report("OK", f"demo song for new accounts: {len(songs)}")
    else:
        report("OK", "no demo song for new accounts (optional)",
               _cmd("python -m fision_server.prefetch"))


def model_dirs():
    """Carpetas de pesos efectivas (FISION_MODELS_DIR rellena las tres)."""
    sys.path.insert(0, str(config.REPO_DIR))
    import headless_fisionAI  # noqa: F401  (aplica FISION_MODELS_DIR)
    import audio_separator_script as sep
    return {
        "separator": sep.model_dir(),
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
