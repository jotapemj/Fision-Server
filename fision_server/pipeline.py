"""
Puente al orquestador. Lo llama como SUBPROCESO (aislado): si revienta, no se
lleva por delante al servidor.

Cada job corre en su propio grupo de procesos: el orquestador lanza a su vez
el separador como hijo, y cancelar/timeout tiene que matar a todos (si no, el
nieto sigue ocupando GPU).
"""
import os
import json
import signal
import subprocess
from pathlib import Path

from . import config

_POSIX = os.name == "posix"


class JobError(RuntimeError):
    """Fallo de un job. `code` es estable (error_code en /jobs/{id}); el texto no."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class JobTimeout(JobError):
    """El subproceso supero su tope de tiempo."""

    def __init__(self, timeout):
        super().__init__("timeout", f"timed out after {timeout} s")


def _popen(cmd):
    # Grupo propio en POSIX para poder matar hijos y nietos de golpe
    return subprocess.Popen(cmd, start_new_session=_POSIX)


def stop(proc, grace=15):
    """Termina el job entero (grupo de procesos). SIGTERM, y SIGKILL si no cae."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if _POSIX:
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        if _POSIX:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.wait()
    except ProcessLookupError:
        pass


def _run(cmd, timeout, on_proc, code, what):
    proc = _popen(cmd)
    if on_proc:
        on_proc(proc)                 # el worker lo guarda para poder cancelar
    try:
        ret = proc.wait(timeout=timeout if timeout > 0 else None)
    except subprocess.TimeoutExpired:
        stop(proc)
        raise JobTimeout(timeout)
    if ret != 0:
        raise JobError(code, f"{what} failed (exit code {ret})")


def process(input_path, out_stems_dir, cfg, on_proc=None):
    """Genera los stems en out_stems_dir. Bloqueante. Revienta si el CLI falla."""
    out_stems_dir = Path(out_stems_dir)
    out_stems_dir.mkdir(parents=True, exist_ok=True)

    cfg_file = out_stems_dir.parent / "config.run.json"
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)

    cmd = config.cli_command(input_path, out_stems_dir, cfg_file)
    _run(cmd, config.JOB_TIMEOUT_SEC, on_proc, "pipeline_failed", "pipeline")


def process_lyrics(vocals_path, out_stems_dir, language=None, device=None,
                   compute=None, on_proc=None):
    """Invoca el CLI de lyrics sobre vocals.flac. Bloqueante.

    device/compute: opcionales. Si None, los defaults de config (env). El
    worker los sobreescribe cuando degrada a CPU por falta de GPU.
    """
    out_stems_dir = Path(out_stems_dir)
    out_stems_dir.mkdir(parents=True, exist_ok=True)

    cmd = config.lyrics_cli_command(vocals_path, out_stems_dir, language,
                                    device=device, compute=compute)
    _run(cmd, config.LYRICS_TIMEOUT_SEC, on_proc, "lyrics_failed", "lyrics")
