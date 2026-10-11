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

# Comando que lanza el orquestador en modo CLI (separacion + beats + chords).
_CLI = os.environ.get("FISION_CLI", "").strip()

# Comando que lanza el CLI de lyrics (Whisper sobre vocals.flac).
_LYRICS_CLI = os.environ.get("FISION_LYRICS_CLI", "").strip()


def cli_command(input_path, out_dir, config_json):
    base = _CLI.split() if _CLI else [sys.executable, "headless_fision_CLI.py"]
    return base + [str(input_path), str(out_dir), str(config_json)]


def lyrics_cli_command(vocals_path, out_dir, language=None, device=None,
                       compute=None):
    """Comando para el subprocess de lyrics.

    language=None -> "auto" (Whisper detecta solo).
    device/compute: si None se usan los defaults de env (WHISPER_DEVICE/
    WHISPER_COMPUTE). El worker los sobreescribe cuando degrada a CPU.
    """
    base = (_LYRICS_CLI.split() if _LYRICS_CLI
            else [sys.executable, "headless_fision_lyrics_CLI.py"])
    lang = language or "auto"
    return base + [str(vocals_path), str(out_dir), lang,
                   "--model", WHISPER_MODEL,
                   "--device", device or WHISPER_DEVICE,
                   "--compute", compute or WHISPER_COMPUTE]


HOST = os.environ.get("FISION_HOST", "0.0.0.0")
PORT = int(os.environ.get("FISION_PORT", "8000"))
MAX_UPLOAD_MB = int(os.environ.get("FISION_MAX_UPLOAD_MB", "200"))
# Tope aparte para avatares. Nada de reutilizar el del audio.
MAX_AVATAR_MB = int(os.environ.get("FISION_MAX_AVATAR_MB", "2"))

# ------------------------------------------------------------------- GPU ----
# Minimo de VRAM libre para dejar arrancar un job en cuda (MB).
GPU_MIN_VRAM_MB = int(os.environ.get("FISION_GPU_MIN_VRAM_MB", "4096"))
# Cada cuanto revisar VRAM cuando el job esta en waiting_gpu (segundos).
GPU_POLL_SEC = int(os.environ.get("FISION_GPU_POLL_SEC", "30"))
# Timeout duro esperando VRAM (segundos). Si se supera -> aborta con error.
GPU_WAIT_TIMEOUT_SEC = int(os.environ.get("FISION_GPU_WAIT_TIMEOUT_SEC", "600"))

# --------------------------------------------------------------- WHISPER ----
# Checkpoint de faster-whisper para lyrics. Se descarga en primer arranque
# y queda en ~/.cache/huggingface/hub/.
WHISPER_MODEL = os.environ.get("FISION_WHISPER_MODEL", "large-v3").strip()
# Dispositivo para la inferencia. "cuda" es lo esperado en el server con GPU.
WHISPER_DEVICE = os.environ.get("FISION_WHISPER_DEVICE", "cuda").strip()
# Precision. "float16" en GPU (mejor calidad/velocidad); "int8" si vas a CPU.
WHISPER_COMPUTE = os.environ.get("FISION_WHISPER_COMPUTE", "float16").strip()
# Minimo de VRAM libre para arrancar un job de lyrics (independiente del
# GPU_MIN_VRAM_MB de separacion). large-v3 en fp16 pide ~3 GB.
WHISPER_MIN_VRAM_MB = int(os.environ.get("FISION_WHISPER_MIN_VRAM_MB", "3072"))

LIBRARY_DIR.mkdir(parents=True, exist_ok=True)

# Subidas a medias. Mismo disco que la biblioteca: mover = rename atomico y
# sin pasar por /tmp (que puede ser tmpfs = RAM). Se vacia al arrancar.
INCOMING_DIR = LIBRARY_DIR / ".incoming"
INCOMING_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- timeouts ----
# Tope de un job (sin contar la espera de GPU). Al vencer se mata el grupo
# de procesos entero y el job queda en error. Holgado para GPUs lentas / CPU.
JOB_TIMEOUT_SEC = int(os.environ.get("FISION_JOB_TIMEOUT_SEC", "2400"))
LYRICS_TIMEOUT_SEC = int(os.environ.get("FISION_LYRICS_TIMEOUT_SEC", "900"))

# --------------------------------------------------------------- auth/data ----
# Carpeta persistente para datos del servidor (users, clave de firma).
# En Docker: montala como volumen, igual que library/.
DATA_DIR = Path(os.environ.get("FISION_DATA", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

USERS_FILE = DATA_DIR / "users.json"     # store de usuarios (hash de password)
SECRET_FILE = DATA_DIR / "secret.key"    # clave para firmar tokens (auto-generada)
QUEUE_FILE = DATA_DIR / "queue.json"     # cola persistida (sobrevive reinicios)
AVATARS_DIR = DATA_DIR / "avatars"       # foto de perfil por usuario
AVATARS_DIR.mkdir(parents=True, exist_ok=True)

# Login: N fallos por (IP, usuario) dentro de la ventana -> 429. 0 = sin limite.
LOGIN_MAX_FAILS = int(os.environ.get("FISION_LOGIN_MAX_FAILS", "5"))
LOGIN_WINDOW_SEC = int(os.environ.get("FISION_LOGIN_WINDOW_SEC", "300"))

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
