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

Uso manual:
    python headless_fision_CLI.py <input> <output_dir> [config.json]
                                  [--device cpu|cuda]

Uso desde el servidor (contrato de 3 argumentos):
    python headless_fision_CLI.py <input> <output_dir> <config.json>

El config.json puede ser:
  - el dict de la GUI (sep_config) tal cual:
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
        description="Fision AI - pipeline completo por linea de comandos")
    p.add_argument("input_file", help="Audio de entrada (mp3/wav)")
    p.add_argument("output_dir", help="Carpeta de salida (los stems van aqui)")
    p.add_argument("config", nargs="?", default=None,
                   help="config.json (sep_config de la GUI) o JSON en linea. "
                        "Si se omite -> clasico 2 stems.")
    p.add_argument("--device", default="cpu", choices=["cpu", "cuda"],
                   help="Procesador (cpu o cuda). Lo del config manda si lo trae.")
    p.add_argument("--btc", default="btc_extractor.py",
                   help="Ruta al script btc_extractor.py")
    args = p.parse_args()

    sep_config, dev_cfg = _load_config(args.config)
    device = dev_cfg or args.device          # el config tiene prioridad

    os.makedirs(args.output_dir, exist_ok=True)

    print(f"=== Fision CLI === device={device} chords=BTC "
          f"sep={'(por defecto 2 stems)' if sep_config is None else sep_config.get('mode')}")

    try:
        run_pipeline(args.input_file, args.output_dir,
                     device=device,
                     btc_script=args.btc,
                     sep_config=sep_config)
    except Exception as e:
        print(f"\n[CLI] ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print("\nProceso completo terminado \u2714")
    sys.exit(0)


if __name__ == "__main__":
    main()