"""
Configuracion del servidor Fision. Todo por variables de entorno para que cada
usuario self-hosted lo ajuste sin tocar codigo.
"""
import os
import sys
from pathlib import Path

# Carpeta raiz de la biblioteca: una subcarpeta por hash de audio.
LIBRARY_DIR = Path(os.environ.get("FISION_LIBRARY", "./library")).resolve()

# Token Bearer que la app movil debe enviar en cada peticion.
# Si esta vacio -> modo abierto (solo para pruebas en local).
API_TOKEN = os.environ.get("FISION_TOKEN", "").strip()

# Comando que lanza el orquestador en modo CLI.
# Debe aceptar:  <input_audio> <output_stems_dir> <config.json>
# Ajusta FISION_CLI a tu fichero real (p.ej. "python fision_cli.py").
_CLI = os.environ.get("FISION_CLI", "").strip()


def cli_command(input_path, out_dir, config_json):
    base = _CLI.split() if _CLI else [sys.executable, "headless_fision_CLI.py"]
    return base + [str(input_path), str(out_dir), str(config_json)]


HOST = os.environ.get("FISION_HOST", "0.0.0.0")
PORT = int(os.environ.get("FISION_PORT", "8000"))
MAX_UPLOAD_MB = int(os.environ.get("FISION_MAX_UPLOAD_MB", "200"))

LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
