#!/usr/bin/env python3
"""
Fision AI - Lyrics CLI.

Wrapper FINO sobre `headless_fisionAI.run_lyrics`. Se usa en dos escenarios:

- Como subprocess del servidor: cuando el usuario pide letra desde la app,
  el server lo lanza sobre el vocals.flac vigente de la biblioteca.
- Manual, para usuarios de la GUI/CLI principales que quieran generar la
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
        description="Fision AI - transcripcion de letra sobre voz aislada")
    p.add_argument("vocals", help="vocals.flac (o cualquier audio de voz)")
    p.add_argument("output_dir", help="Carpeta donde escribir lyrics.json")
    p.add_argument("language", nargs="?", default=None,
                   help="Codigo ISO (en/es/fr/...). 'auto' o ausente -> auto-detect.")
    p.add_argument("--model", default="large-v2",
                   help="Checkpoint de faster-whisper (large-v2 por defecto)")
    p.add_argument("--device", default="cuda", choices=["cpu", "cuda"],
                   help="Procesador para la inferencia")
    p.add_argument("--compute", default="float16",
                   help="compute_type: float16 (GPU), int8 (CPU), float32...")
    args = p.parse_args()

    language = args.language
    if language and language.lower() in ("auto", "none", ""):
        language = None

    if not os.path.isfile(args.vocals):
        print(f"[lyrics-CLI] ERROR: no existe el fichero {args.vocals}",
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

    print("\nLyrics generadas correctamente ✔")
    sys.exit(0)


if __name__ == "__main__":
    main()
