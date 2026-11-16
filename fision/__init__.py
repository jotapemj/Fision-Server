"""
Pipeline de Fision: separacion, beats, acordes, metronomos y letra.

    python -m fision <input> <output_dir> [config.json]     # pipeline completo
    python -m fision.lyrics_cli <vocals> <output_dir>       # solo letra

El server lo lanza como subproceso por job (fision_server.runner). Ligero a
proposito: importar el paquete no debe cargar torch ni modelos.
"""
import os
from pathlib import Path

# Raiz del repo: submodulo BTC y resources/ viven aqui, no en el paquete
REPO_DIR = Path(__file__).resolve().parent.parent


def child_env(env=None):
    """Entorno para un subproceso `python -m fision...`: la raiz del repo en
    PYTHONPATH, para que lo encuentre aunque el cwd sea otro."""
    env = dict(os.environ if env is None else env)
    paths = [str(REPO_DIR)] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(paths))
    return env
