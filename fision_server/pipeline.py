"""
Puente al orquestador. Lo llama como SUBPROCESO (aislado): si revienta, no se
lleva por delante al servidor. Mismo patron que la GUI con el separador.
"""
import json
import subprocess
from pathlib import Path

from . import config


def process(input_path, out_stems_dir, cfg, on_proc=None):
    """Genera los stems en out_stems_dir. Bloqueante. Revienta si el CLI falla."""
    out_stems_dir = Path(out_stems_dir)
    out_stems_dir.mkdir(parents=True, exist_ok=True)

    cfg_file = out_stems_dir.parent / "config.run.json"
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)

    cmd = config.cli_command(input_path, out_stems_dir, cfg_file)
    proc = subprocess.Popen(cmd)
    if on_proc:
        on_proc(proc)                 # el worker lo guarda para poder cancelar
    ret = proc.wait()
    if ret != 0:
        raise RuntimeError(f"orquestador fallo (codigo {ret})")


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
    proc = subprocess.Popen(cmd)
    if on_proc:
        on_proc(proc)
    ret = proc.wait()
    if ret != 0:
        raise RuntimeError(f"lyrics CLI fallo (codigo {ret})")
