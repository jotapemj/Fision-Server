#!/usr/bin/env python3
"""
Fision AI - CLI.

Wrapper FINO sobre el orquestador. NO duplica la logica del pipeline: importa
y llama a headless_fisionAI.run_pipeline. Una sola fuente de verdad -> el CLI
hereda TODO lo del orquestador (modo paralelo GPU+beats, deteccion de GPU,
separador dirigido por sep_config, casteo a MusGlyphs, metronomo nuevo, etc.)
y nunca se vuelve a desincronizar.

Importar el orquestador trae el pipeline completo (beats via beat_this en CPU,
acordes via BTC). Fuente unica de verdad.

Uso manual (a carpeta):
    python headless_fision_CLI.py <input> <output_dir> [config.json]
                                  [--device cpu|cuda]

Uso manual (a la biblioteca por hash, igual que el servidor):
    python headless_fision_CLI.py <input> [config.json] --save-server
    (usa FISION_LIBRARY; output_dir se ignora en este modo)

Uso desde el servidor (contrato de 3 argumentos, SIN --save-server: el propio
servidor hace su commit a la biblioteca):
    python headless_fision_CLI.py <input> <output_dir> <config.json>

El config.json puede ser:
  - el sep_config tal cual:
        {"mode":"personalized","vocals":"single","stems":{...}}
  - o el formato "rico", que lleva tambien device:
        {"sep": {...sep_config...}, "device":"cuda"}
    (una clave "chords" en el JSON se ignora: chordino deprecado, siempre BTC)

Codigo de salida: 0 si todo fue bien, 1 si fallo (el servidor lo usa para saber
si el job termino correctamente).
"""
import os
import sys
import json
import argparse

# Importar el orquestador trae el pipeline (beats via beat_this, acordes via BTC).
from headless_fisionAI import run_pipeline


def _load_config(arg):
    """Resuelve el config.

    Devuelve (sep_config, device_override). Acepta ruta a fichero, JSON en
    linea o None. Soporta el formato plano (solo sep_config) y el rico (con
    'sep'/'device'). Una clave 'chords' se ignora (chordino deprecado -> BTC).
    """
    if not arg:
        return None, None

    if os.path.isfile(arg):
        with open(arg, "r", encoding="utf-8") as f:
            raw = json.load(f)
    else:
        raw = json.loads(arg)        # JSON en linea

    if isinstance(raw, dict) and "sep" in raw:
        return raw.get("sep"), raw.get("device")
    return raw, None


def main():
    p = argparse.ArgumentParser(
        description="Fision AI - full pipeline from the command line")
    p.add_argument("input_file", help="Input audio (mp3/wav/flac...)")
    p.add_argument("output_dir", nargs="?", default=None,
                   help="Output folder (the stems go here). "
                        "Ignored with --save-server.")
    p.add_argument("config", nargs="?", default=None,
                   help="config.json (a sep_config) or inline JSON. "
                        "If omitted -> classic 2 stems.")
    p.add_argument("--device", default="cpu", choices=["cpu", "cuda"],
                   help="Device (cpu or cuda). The config wins if it sets one.")
    p.add_argument("--btc", default="btc_extractor.py",
                   help="Path to the btc_extractor.py script")
    p.add_argument("--save-server", action="store_true", dest="save_server",
                   help="Save to the library by hash, like the server "
                        "(uses FISION_LIBRARY). Ignores output_dir.")
    args = p.parse_args()

    # -------------------- modo biblioteca (como el servidor) -----------------
    if args.save_server:
        # output_dir no aplica aqui: el config puede venir en cualquier slot.
        sep_config, dev_cfg = _load_config(args.config or args.output_dir)
        device = dev_cfg or args.device
        from fision_server import library     # fuente unica del guardado por hash

        print(f"=== Fision CLI [library] === device={device} chords=BTC "
              f"sep={'(default 2 stems)' if sep_config is None else sep_config.get('mode')}")

        h = None
        try:
            h, tmp = library.prepare_entry(args.input_file)
            run_pipeline(args.input_file, str(tmp),
                         device=device, btc_script=args.btc, sep_config=sep_config)
            library.finalize_entry(h, tmp, sep_config,
                                   os.path.basename(args.input_file))
            print(f"\n[library] saved as {h}")
        except Exception as e:
            if h:
                try:
                    library.discard_tmp(h)      # lo vigente queda intacto
                except Exception:
                    pass
            print(f"\n[CLI] ERROR: {e}", file=sys.stderr)
            sys.exit(1)

        print("\nFull process finished \u2714")
        sys.exit(0)

    # -------------------- modo carpeta (contrato del servidor) ---------------
    if not args.output_dir:
        p.error("output_dir is required (or use --save-server)")

    sep_config, dev_cfg = _load_config(args.config)
    device = dev_cfg or args.device          # el config tiene prioridad

    os.makedirs(args.output_dir, exist_ok=True)

    print(f"=== Fision CLI === device={device} chords=BTC "
          f"sep={'(default 2 stems)' if sep_config is None else sep_config.get('mode')}")

    try:
        run_pipeline(args.input_file, args.output_dir,
                     device=device,
                     btc_script=args.btc,
                     sep_config=sep_config)
    except Exception as e:
        print(f"\n[CLI] ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print("\nFull process finished \u2714")
    sys.exit(0)


if __name__ == "__main__":
    main()