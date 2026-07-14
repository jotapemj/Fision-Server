"""
Configuracion del servidor Fision. Todo por variables de entorno para que cada
usuario self-hosted lo ajuste sin tocar codigo.
"""
import os
import sys
import secrets
from pathlib import Path

# Carpeta raiz de la biblioteca: una subcarpeta por hash de audio.
# (Con multiusuario pasara a ser library/<user>/<hash>/ en el paso de aislamiento.)
LIBRARY_DIR = Path(os.environ.get("FISION_LIBRARY", "./library")).resolve()

# Usuario por defecto para el guardado LOCAL (GUI/CLI en tu maquina, un solo
# dueño). El servidor NUNCA usa esto: siempre pasa el user real del token.
# Ponlo a tu username (ej. "jp") para que GUI local y nube compartan biblioteca.
LOCAL_USER = os.environ.get("FISION_LOCAL_USER", "local").strip().lower()

# --- DEPRECADO -------------------------------------------------------------
# Token Bearer estatico antiguo. Ya no se usa: ahora hay login user+password.
# Se deja por compatibilidad, pero auth.py NO lo mira.
API_TOKEN = os.environ.get("FISION_TOKEN", "").strip()

# Comando que lanza el orquestador en modo CLI.
_CLI = os.environ.get("FISION_CLI", "").strip()


def cli_command(input_path, out_dir, config_json):
    base = _CLI.split() if _CLI else [sys.executable, "headless_fision_CLI.py"]
    return base + [str(input_path), str(out_dir), str(config_json)]


HOST = os.environ.get("FISION_HOST", "0.0.0.0")
PORT = int(os.environ.get("FISION_PORT", "8000"))
MAX_UPLOAD_MB = int(os.environ.get("FISION_MAX_UPLOAD_MB", "200"))

LIBRARY_DIR.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------- auth/data ----
# Carpeta persistente para datos del servidor (users, clave de firma).
# En Docker: montala como volumen, igual que library/.
DATA_DIR = Path(os.environ.get("FISION_DATA", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

USERS_FILE = DATA_DIR / "users.json"     # store de usuarios (hash de password)
SECRET_FILE = DATA_DIR / "secret.key"    # clave para firmar tokens (auto-generada)
QUEUE_FILE = DATA_DIR / "queue.json"     # cola persistida (sobrevive reinicios)

# Duracion del token de sesion. 720h = 30 dias (comodo para app movil).
TOKEN_TTL_HOURS = int(os.environ.get("FISION_TOKEN_TTL_HOURS", "720"))

# Nivel de log del servidor.
LOG_LEVEL = os.environ.get("FISION_LOG_LEVEL", "INFO").upper()


def _load_secret():
    """Clave para firmar tokens. Prioridad: env -> fichero -> generar y guardar.

    Persistirla en fichero hace que los tokens sobrevivan a reinicios.
    Si cambias/borras esta clave, TODAS las sesiones caducan (equivale a logout global).
    """
    env = os.environ.get("FISION_SECRET", "").strip()
    if env:
        return env
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text(encoding="utf-8").strip()
    s = secrets.token_urlsafe(48)
    SECRET_FILE.write_text(s, encoding="utf-8")
    try:
        os.chmod(SECRET_FILE, 0o600)     # solo el dueño puede leerla
    except OSError:
        pass
    return s


SECRET = _load_secret()
