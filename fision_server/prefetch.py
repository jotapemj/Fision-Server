"""
Descarga todos los pesos de una vez, antes del primer trabajo:

    python -m fision_server.prefetch

Separacion (audio-separator), beat_this y el Whisper de FISION_WHISPER_MODEL,
en las carpetas de FISION_MODELS_DIR (o las de cada libreria). Lo que ya esta
no se vuelve a bajar: se puede repetir. Sin esto, el primer trabajo baja
varios GB y falla si se corta la red.

Ejecutalo con las mismas envs que el servicio, o los pesos acaban en otra
carpeta.
"""
import sys
import logging

from . import config
from .doctor import model_dirs


def _step(label, fn):
    print(f"\n== {label}")
    try:
        fn()
    except Exception as e:
        print(f"FAILED {label}: {e}")
        return False
    print(f"ok {label}")
    return True


def fetch_separator():
    import audio_separator_script as sep
    from audio_separator.separator import Separator
    s = Separator(model_file_dir=sep.model_dir(), info_only=True,
                  log_level=logging.WARNING)
    for m in sep.ALL_MODELS:
        print(f"   {m}")
        _, _, _, path, yaml_cfg = s.download_model_files(m)
        # Sin yaml (p. ej. el .pth de vientos) load_model busca los parametros
        # por hash en mdx/vr_model_data.json, que tambien se bajan de la red
        if yaml_cfg is None and not path.endswith(".yaml"):
            s.load_model_data_using_hash(path)


def fetch_beat_this():
    import headless_fisionAI as pipe
    from beat_this.inference import load_checkpoint
    for c in pipe.BEAT_THIS_CHECKPOINTS:
        print(f"   {c}")
        load_checkpoint(c)


def fetch_whisper():
    from faster_whisper.utils import download_model
    print(f"   {config.WHISPER_MODEL}")
    download_model(config.WHISPER_MODEL)


def main():
    from . import __version__
    print(f"Fision {__version__} - prefetch")
    for label, path in model_dirs().items():     # tambien aplica FISION_MODELS_DIR
        print(f"   {label}: {config.shown_path(path)}")
    results = [_step("separation", fetch_separator),
               _step("beat_this", fetch_beat_this),
               _step(f"Whisper {config.WHISPER_MODEL}", fetch_whisper)]
    if all(results):
        print("\nEverything downloaded.")
        sys.exit(0)
    print("\nSomething failed: check the network and run it again (finished downloads are kept).")
    sys.exit(1)


if __name__ == "__main__":
    main()
