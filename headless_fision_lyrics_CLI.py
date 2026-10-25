#!/usr/bin/env python3
"""
Fision AI - Lyrics CLI.

Wrapper FINO sobre `headless_fisionAI.run_lyrics`. Se usa en dos escenarios:

- Como subprocess del servidor: cuando el usuario pide letra desde la app,
  el server lo lanza sobre el vocals.flac vigente de la biblioteca.
- Manual, para usuarios del CLI principal que quieran generar la
  letra sobre stems ya procesados.

Uso:

    python headless_fision_lyrics_CLI.py <vocals> <output_dir> [language]
                                         [--model NAME]
                                         [--device cpu|cuda]
                                         [--compute TYPE]

`language` es un codigo ISO ("en", "es", "fr"...). Si se omite o vale "auto",
Whisper detecta el idioma solo.

Codigo de salida: 0 exito, 1 fallo (el server lo usa para saber si el job
termino bien).
"""
import os
import sys
import argparse

# Reutilizamos la funcion del orquestador (una unica fuente de verdad).
from headless_fisionAI import run_lyrics


def main():
    p = argparse.ArgumentParser(
        description="Fision AI - lyrics transcription of an isolated vocal track")
    p.add_argument("vocals", help="vocals.flac (or any vocal audio)")
    p.add_argument("output_dir", help="Folder where lyrics.json is written")
    p.add_argument("language", nargs="?", default=None,
                   help="ISO code (en/es/fr/...). 'auto' or missing -> auto-detect.")
    p.add_argument("--model", default="large-v2",
                   help="faster-whisper checkpoint (large-v2 by default)")
    p.add_argument("--device", default="cuda", choices=["cpu", "cuda"],
                   help="Device for inference")
    p.add_argument("--compute", default="float16",
                   help="compute_type: float16 (GPU), int8 (CPU), float32...")
    args = p.parse_args()

    language = args.language
    if language and language.lower() in ("auto", "none", ""):
        language = None

    if not os.path.isfile(args.vocals):
        print(f"[lyrics-CLI] ERROR: file not found: {args.vocals}",
              file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output_dir, exist_ok=True)

    print(f"=== Fision Lyrics CLI === model={args.model} device={args.device} "
          f"compute={args.compute} language={language or 'auto'}")

    try:
        run_lyrics(args.vocals, args.output_dir, language=language,
                   model_name=args.model, device=args.device,
                   compute_type=args.compute)
    except Exception as e:
        print(f"\n[lyrics-CLI] ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print("\nLyrics generated ✔")
    sys.exit(0)


if __name__ == "__main__":
    main()
