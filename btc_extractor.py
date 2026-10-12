# btc_extractor.py
import os
import sys
import torch
import numpy as np
import warnings

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BTC_REPO_PATH = os.path.join(SCRIPT_DIR, "BTC-ISMIR19")

if BTC_REPO_PATH not in sys.path:
    sys.path.insert(0, BTC_REPO_PATH)

# Imports del repo BTC
from btc_model import *
from utils import logger
from utils.mir_eval_modules import audio_file_to_features, idx2chord, idx2voca_chord
import chord_smoothing

warnings.filterwarnings('ignore')
logger.logging_verbosity(0)  # Consola limpia

# True  -> vocabulario grande (170 acordes: 7th, dim, aug, sus, hdim7, etc.)
# False -> maj/min (25 acordes)
USE_LARGE_VOCA = True


def main(file_path, carpeta):
    print("--- Fision AI Chord Extractor. Powered by BTC (Transformer AI) ---")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # --- Config ---
    config_path = os.path.join(BTC_REPO_PATH, "run_config.yaml")
    if not os.path.exists(config_path):
        config_path = os.path.join(SCRIPT_DIR, "run_config.yaml")  # fallback
    config = HParams.load(config_path)

    # --- Vocabulario: flag + num_chords + modelo + indice DEBEN ir juntos ---
    config.feature['large_voca'] = USE_LARGE_VOCA
    if USE_LARGE_VOCA:
        config.model['num_chords'] = 170
        model_file = os.path.join(BTC_REPO_PATH, "test", "btc_model_large_voca.pt")
        idx_to_chord = idx2voca_chord()
    else:
        config.model['num_chords'] = 25
        model_file = os.path.join(BTC_REPO_PATH, "test", "btc_model.pt")
        idx_to_chord = idx2chord
    if not os.path.isfile(model_file):
        model_file = os.path.join(SCRIPT_DIR, os.path.basename(model_file))  # fallback

    # --- Modelo ---
    model = BTC_model(config=config.model).to(device)
    if os.path.isfile(model_file):
        # weights_only=False: desde torch 2.6 el default es True y rompe la carga
        # del checkpoint de BTC (es un dict con mean/std/model, no solo tensores).
        checkpoint = torch.load(model_file, map_location=device, weights_only=False)
        mean = checkpoint['mean']
        std = checkpoint['std']
        model.load_state_dict(checkpoint['model'])
    else:
        print(f"Error: No se encontro el modelo BTC en {model_file}")
        sys.exit(1)

    # --- Features ---
    feature, feature_per_second, song_length_second = audio_file_to_features(file_path, config)
    feature = feature.T
    feature = (feature - mean) / std
    time_unit = feature_per_second
    n_timestep = config.model['timestep']

    num_pad = n_timestep - (feature.shape[0] % n_timestep)
    feature = np.pad(feature, ((0, num_pad), (0, 0)), mode="constant", constant_values=0)
    num_instance = feature.shape[0] // n_timestep

    # Suavizado por beats: solo con vocabulario grande y beats.txt presente
    # (el pipeline corre beat_this antes que BTC). Si no, camino crudo.
    smooth_cfg = chord_smoothing.settings()
    beat_grid = chord_smoothing.read_beats(carpeta) if USE_LARGE_VOCA else None
    use_smooth = smooth_cfg["enabled"] and beat_grid is not None
    frame_probs = []

    # --- Inferencia (frames -> tramos por cambio de acorde) ---
    start_time = 0.0
    lines = []
    with torch.no_grad():
        model.eval()
        feature = torch.tensor(feature, dtype=torch.float32).unsqueeze(0).to(device)
        for t in range(num_instance):
            self_attn_output, _ = model.self_attn_layers(feature[:, n_timestep * t:n_timestep * (t + 1), :])
            prediction, _ = model.output_layer(self_attn_output)
            if use_smooth:
                logits = model.output_layer.output_projection(self_attn_output)
                frame_probs.append(torch.softmax(logits, -1).squeeze(0).cpu().numpy())
            prediction = prediction.squeeze()
            for i in range(n_timestep):
                if t == 0 and i == 0:
                    prev_chord = prediction[i].item()
                    continue
                if prediction[i].item() != prev_chord:
                    lines.append('%.3f %.3f %s\n' % (start_time, time_unit * (n_timestep * t + i), idx_to_chord[prev_chord]))
                    start_time = time_unit * (n_timestep * t + i)
                    prev_chord = prediction[i].item()
                if t == num_instance - 1 and i + num_pad == n_timestep:
                    if start_time != time_unit * (n_timestep * t + i):
                        lines.append('%.3f %.3f %s\n' % (start_time, time_unit * (n_timestep * t + i), idx_to_chord[prev_chord]))
                    break

    if use_smooth:
        n_real = num_instance * n_timestep - num_pad     # fuera el padding
        probs = np.concatenate(frame_probs)[:n_real]
        segs = chord_smoothing.smooth(
            probs, time_unit, song_length_second, *beat_grid,
            change=smooth_cfg["change"], family=smooth_cfg["family"],
            off_mult=smooth_cfg["off_mult"])
        lines = ['%.3f %.3f %s\n' % s for s in segs]
        print(f"Chords smoothed to beats: {len(segs)} segments "
              f"(change={smooth_cfg['change']} family={smooth_cfg['family']} "
              f"off_mult={smooth_cfg['off_mult']})")

    # --- Guardar .lab (start end chord). run_btc lo postprocesa. ---
    os.makedirs(carpeta, exist_ok=True)
    stem = os.path.splitext(os.path.basename(file_path))[0]
    lab_path = os.path.join(carpeta, f"{stem}.lab")
    with open(lab_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    print(f"Success! BTC .lab saved: {lab_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python btc_extractor.py <file_path> <output_dir>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
