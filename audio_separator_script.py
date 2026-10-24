"""
Fision AI - audio_separator_script.py

Motor de separacion dirigido por config JSON. Lo lanza el orquestador como
subproceso aislado (evita choques de versiones torch/madmom).

Uso:
    python audio_separator_script.py <input_file> <output_dir> <config.json>

Contrato del config (lo genera la GUI / orquestador):

  Classic:
    {"mode":"classic","preset":"4stem"|"2stem","sep_mode":4|2,"vocals":"single"}

  Personalizada:
    {"mode":"personalized",
     "vocals":"single"|"split",
     "stems":{
        "guitar":{"enabled":bool},
        "bass":  {"enabled":bool},
        "drums": {"enabled":bool,"variant":"full"|"split"},
        "piano": {"enabled":bool},
        "brass": {"enabled":bool}}}

Reglas del flujo (acordadas):
  - Base: vocals_mel_band_roformer -> vocals + instrumental (sin voz).
  - Voces SIEMPRE salen, nunca al other. Variante split -> karaoke gabox.
  - ft (htdemucs_ft) da bass/drums buenos. 6s (htdemucs_6s) da guitar/piano.
  - other = instrumental - (stems extraidos)   [por RESTA, dominio temporal]
  - drums split: MDX23C-DrumSep -> 6 piezas; perc = drums - suma(6 piezas).
  - vientos al FINAL: 17_HP-Wind sobre other -> winds.flac + other (No_Woodwinds).
"""

import os
import sys
import glob
import json
import shutil
import time

import numpy as np
import soundfile as sf
from audio_separator.separator import Separator


# ---------------------------------------------------------------- MODELOS ----
ROFORMER_VOCALS = "vocals_mel_band_roformer.ckpt"            # base: voz / instrumental
KARAOKE         = "mel_band_roformer_karaoke_gabox_v2.ckpt"  # principal / coros
HTDEMUCS_FT     = "htdemucs_ft.yaml"                         # bass/drums/other buenos
HTDEMUCS_6S     = "htdemucs_6s.yaml"                         # guitar/piano
DRUMSEP         = "MDX23C-DrumSep-aufr33-jarredou.ckpt"      # kick/snare/toms/hh/ride/crash
WINDS           = "17_HP-Wind_Inst-UVR.pth"                  # vientos / metales
ALL_MODELS = (ROFORMER_VOCALS, KARAOKE, HTDEMUCS_FT, HTDEMUCS_6S, DRUMSEP, WINDS)


def model_dir():
    """Carpeta de pesos del separador.
    Prioridad: FISION_SEP_MODEL_DIR > FISION_MODELS_DIR/separator > ~/.cache.
    Nunca /tmp (el default de la lib): en tmpfs se pierden en cada reinicio."""
    root = os.environ.get("FISION_MODELS_DIR", "").strip()
    default = (os.path.join(root, "separator") if root
               else "~/.cache/audio-separator-models")
    return os.path.expanduser(
        os.environ.get("FISION_SEP_MODEL_DIR", "").strip() or default)

DRUM_PIECES = ["(kick)", "(snare)", "(toms)", "(hh)", "(ride)", "(crash)"]
SILENCE_THRESHOLD = 1e-4   # por debajo de esto consideramos "silencio" -> se tira


# ------------------------------------------------------------- HELPERS FS ----
def _glob_kw(output_dir, keyword):
    """Ficheros .flac cuyo nombre contiene 'keyword', SIN distinguir mayusculas.

    audio-separator cambia la capitalizacion de los stems entre versiones
    ('(Other)' en unas, '(other)' en otras). En Windows el glob es insensible y
    colaba por accidente; en Linux es sensible y no encontraba nada.
    Comparamos en minusculas para que funcione en ambos.

    Los parentesis del keyword siguen evitando colisiones:
      - '(Woodwinds)' NO casa con '(No Woodwinds)'
      - '(Vocals)'    NO casa con 'vocals.flac' (ya renombrado, sin parentesis)
    """
    kw = keyword.lower()
    return sorted(f for f in glob.glob(os.path.join(output_dir, "*.flac"))
                  if kw in os.path.basename(f).lower())


def rename_and_clean(output_dir, keyword, new_name):
    """Renombra el primer .flac que contenga 'keyword' a 'new_name'. Devuelve ruta o None."""
    for f in _glob_kw(output_dir, keyword):
        dst = os.path.join(output_dir, new_name)
        if os.path.abspath(f) != os.path.abspath(dst):
            shutil.move(f, dst)
        return dst
    return None


def remove_by_keyword(output_dir, keyword):
    """Borra todos los .flac que contengan 'keyword'."""
    for f in _glob_kw(output_dir, keyword):
        _safe_remove(f)


def _safe_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _move(src, dst):
    if src and os.path.exists(src):
        shutil.move(src, dst)
        return dst
    return None


# ---------------------------------------------------------- HELPERS AUDIO ----
def _read(path):
    """Lee wav como float32 (n, ch). Siempre 2D."""
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    return data, sr


def _accumulate(part_paths, ref_len, ref_ch):
    """Suma varias pistas alineadas al minimo comun (longitud y canales)."""
    acc = np.zeros((ref_len, ref_ch), dtype=np.float32)
    for p in part_paths:
        if not p or not os.path.exists(p):
            continue
        x, _ = _read(p)
        n = min(ref_len, x.shape[0])
        c = min(ref_ch, x.shape[1])
        acc[:n, :c] += x[:n, :c]
    return acc


def merge_wavs(part_paths, out_path):
    """other = suma de varias pistas (cuando no hay instrumental de referencia)."""
    paths = [p for p in part_paths if p and os.path.exists(p)]
    if not paths:
        return None
    base, sr = _read(paths[0])
    acc = _accumulate(paths, base.shape[0], base.shape[1])
    sf.write(out_path, acc, sr)
    return out_path


def subtract_wavs(base_path, part_paths, out_path):
    """residual = base - suma(partes). Mismo origen -> alineado de fabrica."""
    base, sr = _read(base_path)
    acc = _accumulate(part_paths, base.shape[0], base.shape[1])
    residual = base - acc
    sf.write(out_path, residual, sr)
    return out_path


def drop_if_silent(path):
    """Si la pista es practicamente silencio, la borra (residuos de demucs)."""
    if not path or not os.path.exists(path):
        return
    try:
        data, _ = _read(path)
        if float(np.max(np.abs(data))) < SILENCE_THRESHOLD:
            print(f"  - {os.path.basename(path)}: silencio, descartado")
            os.remove(path)
    except Exception:
        pass


def drop_demucs_vocals(output_dir):
    """Demucs sobre instrumental escupe una 'voz' vacia. Fuera."""
    remove_by_keyword(output_dir, "(Vocals)")


# -------------------------------------------------------------- SEPARATOR ----
def build_separator(output_dir):
    """Un unico Separator para todo el pipeline.

    Parametros exprimibles via env (prefijo FISION_):
      FISION_SEP_BATCH_SIZE    default 24   (solo DrumSep; RoFormer va
                                             chunk a chunk y lo ignora)
      FISION_SEP_SEGMENT_SIZE  default 512  (sin efecto: override False,
                                             manda el yaml del modelo)
      FISION_SEP_OVERLAP       default 4
      FISION_SEP_AUTOCAST      default 1    (1=True, 0=False)
      FISION_DEMUCS_SHIFTS     default 4    (calidad, no VRAM)
      FISION_DEMUCS_OVERLAP    default 0.5
      FISION_SEP_MODEL_DIR     default $FISION_MODELS_DIR/separator, o
                               ~/.cache/audio-separator-models si no hay
                               raiz (persistente; el de la lib es /tmp)

    El cuello es computo, no VRAM (pico ~3.4 GB en una 3090): mas VRAM
    no acelera. La palanca es FISION_DEMUCS_SHIFTS. En Ampere+ autocast
    fp16 tiene sentido; en Pascal va lentisimo, dejar autocast en 0.
    """
    try:
        import torch
        print(f"CUDA disponible: {torch.cuda.is_available()}")
    except Exception:
        pass

    batch_size     = int(os.environ.get("FISION_SEP_BATCH_SIZE", "24"))
    segment_size   = int(os.environ.get("FISION_SEP_SEGMENT_SIZE", "512"))
    overlap        = int(os.environ.get("FISION_SEP_OVERLAP", "4"))
    autocast       = os.environ.get("FISION_SEP_AUTOCAST", "1").strip() \
                        not in ("0", "false", "False")
    demucs_shifts  = int(os.environ.get("FISION_DEMUCS_SHIFTS", "4"))
    demucs_overlap = float(os.environ.get("FISION_DEMUCS_OVERLAP", "0.5"))
    models      = model_dir()

    print(f"[sep] batch_size={batch_size} segment={segment_size} "
          f"overlap={overlap} autocast={autocast} "
          f"demucs_shifts={demucs_shifts} demucs_overlap={demucs_overlap} "
          f"model_dir={models}")

    return Separator(
        output_dir=output_dir,
        model_file_dir=models,
        output_format="FLAC",
        use_autocast=autocast,
        mdxc_params={                  # RoFormer y DrumSep (MDXC)
            "segment_size": segment_size,
            "override_model_segment_size": False,
            "overlap": overlap,
            "batch_size": batch_size,
            "pitch_shift": 0,
        },
        demucs_params={                # solo afecta a HTDemucs
            "shifts": demucs_shifts,
            "overlap": demucs_overlap,
            "segments_enabled": True,
        },
    )


# -------------------------------------------------------------- PIPELINE ------
def run(input_file, output_dir, cfg):
    os.makedirs(output_dir, exist_ok=True)
    sep = build_separator(output_dir)

    # ---- 1) BASE: voces / instrumental --------------------------------------
    print("\n[BASE] RoFormer: voces + instrumental")
    sep.load_model(model_filename=ROFORMER_VOCALS)
    sep.separate(input_file)

    instrumental = (rename_and_clean(output_dir, "(Instrumental)", "instrumental.flac")
                    or rename_and_clean(output_dir, "(Other)", "instrumental.flac"))
    rename_and_clean(output_dir, "(Vocals)", "vocals.flac")
    if instrumental is None:
        raise RuntimeError("La base no genero instrumental.")

    # ---- 2) VOCES: variante -------------------------------------------------
    if cfg.get("vocals", "single") == "split":
        print("[VOCES] Karaoke gabox: principal + coros")
        sep.load_model(HTDEMUCS_FT) # Loading a different model (not mdxc) to free sep object (bug patch)
        time.sleep(2)
        sep.load_model(model_filename=KARAOKE)
        sep.separate(os.path.join(output_dir, "vocals.flac"))
        _safe_remove(os.path.join(output_dir, "vocals.flac"))
        rename_and_clean(output_dir, "(Vocals)", "vocals.flac")        # principal
        rename_and_clean(output_dir, "(Instrumental)", "vocals2.flac")  # coros

    mode = cfg.get("mode", "personalized")

    # ========================== CLASSIC ======================================
    if mode == "classic":
        if cfg.get("preset") == "2stem":
            print("[CLASSIC] 2 stems listo: vocals + instrumental")
            return
        print("[CLASSIC] 4 stems: HTDemucs ft sobre instrumental")
        sep.load_model(model_filename=HTDEMUCS_FT)
        sep.separate(instrumental)
        rename_and_clean(output_dir, "(Bass)", "bass.flac")
        rename_and_clean(output_dir, "(Drums)", "drums.flac")
        rename_and_clean(output_dir, "(Other)", "other.flac")
        drop_demucs_vocals(output_dir)
        _safe_remove(instrumental)
        return

    # ========================= PERSONALIZADA =================================
    stems = cfg.get("stems", {})

    def want(k):
        return stems.get(k, {}).get("enabled", False)

    drums_variant = stems.get("drums", {}).get("variant", "full")

    need_ft = want("bass") or want("drums")    # ft da el bass/drums bueno
    need_6s = want("guitar") or want("piano")  # 6s da guitar/piano
    kept = []   # stems extraidos -> se restan del instrumental para el other

    # ---- ft: bass / drums ---------------------------------------------------
    if need_ft:
        print("[ft] HTDemucs ft sobre instrumental (bass/drums)")
        sep.load_model(model_filename=HTDEMUCS_FT)
        sep.separate(instrumental)
        if want("bass"):
            kept.append(rename_and_clean(output_dir, "(Bass)", "bass.flac"))
        else:
            remove_by_keyword(output_dir, "(Bass)")
        if want("drums"):
            kept.append(rename_and_clean(output_dir, "(Drums)", "drums.flac"))
        else:
            remove_by_keyword(output_dir, "(Drums)")
        remove_by_keyword(output_dir, "(Other)")   # el other lo recomputamos por resta
        drop_demucs_vocals(output_dir)

    # ---- 6s: guitar / piano -------------------------------------------------
    if need_6s:
        print("[6s] HTDemucs 6s sobre instrumental (guitar/piano)")
        sep.load_model(model_filename=HTDEMUCS_6S)
        sep.separate(instrumental)
        if want("guitar"):
            kept.append(rename_and_clean(output_dir, "(Guitar)", "guitar.flac"))
        else:
            remove_by_keyword(output_dir, "(Guitar)")
        if want("piano"):
            kept.append(rename_and_clean(output_dir, "(Piano)", "piano.flac"))
        else:
            remove_by_keyword(output_dir, "(Piano)")
        # el resto de 6s se descarta: usamos ft para bass/drums y resta para other
        for kw in ("(Bass)", "(Drums)", "(Other)", "(Vocals)"):
            remove_by_keyword(output_dir, kw)

    any_extra = need_ft or need_6s or want("brass")

    # ---- caso solo voces: vocals + instrumental, sin other ------------------
    if not any_extra:
        print("[SOLO VOCES] dejo vocals + instrumental")
        return

    # ---- bucket other (por RESTA) ------------------------------------------
    other_path = os.path.join(output_dir, "other.flac")
    if kept:
        print("[other] instrumental - stems extraidos (resta)")
        subtract_wavs(instrumental, kept, other_path)
        _safe_remove(instrumental)       # instrumental.flac es intermedio aqui
    else:
        # solo brass: other = instrumental entero
        _move(instrumental, other_path)

    # ---- drums split (despues del other, usa drums.flac completo) ------------
    if want("drums") and drums_variant == "split":
        print("[drums] MDX23C: kick/snare/toms/hh/ride/crash + perc(resta)")
        drums_path = os.path.join(output_dir, "drums.flac")
        sep.load_model(model_filename=DRUMSEP)
        sep.separate(drums_path)
        pieces = []
        for kw in DRUM_PIECES:
            p = rename_and_clean(output_dir, kw, kw.strip("()") + ".flac")
            if p:
                pieces.append(p)
        # perc = lo que ningun stem se llevo (pandereta, bongo, bleed...)
        subtract_wavs(drums_path, pieces, os.path.join(output_dir, "perc.flac"))
        _safe_remove(drums_path)         # bateria completa ya descompuesta

    # ---- vientos al FINAL: consume el other --------------------------------
    if want("brass"):
        print("[brass] 17_HP-Wind sobre other.flac")
        sep.load_model(model_filename=WINDS)
        sep.separate(other_path)
        _safe_remove(other_path)         # el other viejo es la entrada, fuera
        rename_and_clean(output_dir, "(Woodwinds)", "winds.flac")
        rename_and_clean(output_dir, "(No Woodwinds)", "other.flac")


    # ---- limpieza: residuos silenciosos (NUNCA tocamos las voces) -----------
    for nm in ("bass.flac", "drums.flac", "guitar.flac", "piano.flac",
               "other.flac", "perc.flac", "winds.flac",
               "kick.flac", "snare.flac", "toms.flac", "hh.flac", "ride.flac", "crash.flac"):
        drop_if_silent(os.path.join(output_dir, nm))

    print("\n[OK] Separacion personalizada completada.")


# ------------------------------------------------------------------ MAIN ------
def main():
    if len(sys.argv) != 4:
        print("Uso: python audio_separator_script.py <input_file> <output_dir> <config.json>")
        sys.exit(1)

    input_file = sys.argv[1]
    output_dir = sys.argv[2]
    config_path = sys.argv[3]

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    print(f"[config] {json.dumps(cfg, ensure_ascii=False)}")
    run(input_file, output_dir, cfg)


if __name__ == "__main__":
    main()
