import os
import sys
import subprocess
import threading
import time
import copy
from pathlib import Path
from pydub import AudioSegment, utils as pydub_utils
import mutagen
from mutagen.id3 import APIC
import numpy as np
import subprocess
import re

_clic_seg = AudioSegment.from_file('resources/metronome.wav')

def _extract_cover_bytes(input_file: str):
    """Devuelve bytes de la portada si la hay, None si no. Soporta ID3 (mp3),
    Vorbis/FLAC y MP4/M4A. No revienta: cualquier fallo -> None."""
    try:
        audio = mutagen.File(input_file)
    except Exception:
        return None
    if audio is None:
        return None
    # MP3 / ID3: APIC dentro de audio.tags
    tags = getattr(audio, "tags", None)
    if tags is not None:
        try:
            for tag in tags.values():
                if isinstance(tag, APIC):
                    return tag.data
        except Exception:
            pass
        # MP4 / M4A: 'covr' es lista de MP4Cover
        try:
            covr = tags.get("covr")
            if covr:
                return bytes(covr[0])
        except Exception:
            pass
    # FLAC: lista de Picture en .pictures
    pictures = getattr(audio, "pictures", None)
    if pictures:
        try:
            return pictures[0].data
        except Exception:
            pass
    return None


def run_metadata_extractor(input_file: str, output_dir: str):
    # Titulo/artista con fallback al nombre del fichero. Nunca aborta el pipeline.
    titulo = ""
    artista = ""
    try:
        audio_easy = mutagen.File(input_file, easy=True)
        if audio_easy is not None and audio_easy.tags is not None:
            titulo = (audio_easy.tags.get("title") or [""])[0]
            artista = (audio_easy.tags.get("artist") or [""])[0]
    except Exception as e:
        print(f"Warning: cannot read tags ({e}); using filename as title")

    if not titulo:
        nombre_archivo_con_extension = os.path.basename(input_file)
        titulo, _ = os.path.splitext(nombre_archivo_con_extension)

    # metadata.txt SIEMPRE, incluso si no habia tags legibles
    ruta_txt = os.path.join(output_dir, "metadata.txt")
    try:
        with open(ruta_txt, "w", encoding="utf-8") as f:
            f.write(f"Artist: {artista}\n")
            f.write(f"Title: {titulo}\n")
        print(f"Metadata file save in {output_dir}")
    except Exception as e:
        print(f"Error writing metadata.txt: {e}")

    # Portada opcional: si no hay, avisa y sigue
    cover = _extract_cover_bytes(input_file)
    if cover:
        ruta_png = os.path.join(output_dir, "artcover.png")
        try:
            with open(ruta_png, "wb") as img_file:
                img_file.write(cover)
            print(f"Artcover extracted in: {output_dir}")
        except Exception as e:
            print(f"Error writing artcover: {e}")
    else:
        print("Artcover not found")

# ---------- SPINNER ----------
class Spinner:
    def __init__(self, message="Working..."):
        self.message = message
        self.stop_running = False
        self.thread = threading.Thread(target=self.animate)

    def animate(self):
        spinner = ['|', '/', '-', '\\']
        idx = 0
        while not self.stop_running:
            sys.stdout.write(f"\r{self.message} {spinner[idx % len(spinner)]}")
            sys.stdout.flush()
            idx += 1
            time.sleep(0.1)
        sys.stdout.write("\r" + " " * (len(self.message) + 4) + "\r")
        sys.stdout.flush()

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_running = True
        self.thread.join()


# ---------- DEMUCS ----------
def run_demucs(input_file: str, output_dir: str, device: str = "cpu"):
    temp_file = None
    if Path(input_file).suffix.lower() == ".mp3":
        temp_file = Path(output_dir) / "temp.wav"
        AudioSegment.from_mp3(input_file).export(temp_file, format="wav")
        input_file = str(temp_file)

    separator = demucs.api.Separator(model="mdx_extra", segment=12, device=device)
    origin, separated = separator.separate_audio_file(input_file)

    for i, (stem, sources) in enumerate(separated.items(), start=1):
        save_path = Path(output_dir) / f"{stem}.wav"
        demucs.api.save_audio(sources, path=save_path, samplerate=separator.samplerate)
        print(f"[{i}/{len(separated)}] Saved {save_path}")

    return temp_file


# ---------- BEATS ----------
def generate_metronome(beats, duration_ms, velocity, output_dir):
    seg = _clic_seg
    sr = seg.frame_rate
    ch = seg.channels
    sw = seg.sample_width

    # Clic a numpy
    clic = np.array(seg.get_array_of_samples()).reshape(-1, ch)

    # Calcula posiciones (en segundos)
    if velocity == "1x":
        pos_s = list(beats)
        name = "metronome.flac"
    elif velocity == "0.5x":
        pos_s = beats[1::2]          # uno sí uno no
        name = "metronome_half.flac"
    elif velocity == "2x":
            if len(beats) > 1:
                # Calcula los puntos medios entre cada beat
                midpoints = (beats[:-1] + beats[1:]) / 2
                
                # Crea un nuevo array para intercalar beats y midpoints
                pos_s = np.empty(len(beats) + len(midpoints), dtype=beats.dtype)
                pos_s[0::2] = beats
                pos_s[1::2] = midpoints
            else:
                pos_s = list(beats)
            name = "metronome_double.flac"
    else:
        return

    # Buffer total en samples
    total = int(duration_ms / 1000 * sr)
    buf = np.zeros((total, ch), dtype=np.int64)  # int64 evita overflow al sumar

    L = clic.shape[0]
    for p in pos_s:
        start = int(p * sr)
        end = start + L
        if start >= total:
            continue
        if end > total:                 # recorta clic al final
            buf[start:total] += clic[:total - start]
        else:
            buf[start:end] += clic

    # Clip para no romper rango del tipo original
    info = np.iinfo({1: np.int8, 2: np.int16, 4: np.int32}[sw])
    buf = np.clip(buf, info.min, info.max).astype(clic.dtype)

    out_seg = AudioSegment(
        buf.tobytes(),
        frame_rate=sr,
        sample_width=sw,
        channels=ch,
    )
    outpath = Path(output_dir) / name
    out_seg.export(outpath, format="flac")
    print(f"\nGenerated {outpath}")


def run_beats(input_file: str, output_dir: str):
    """Beats y downbeats con beat_this (ISMIR 2024), ensemble de checkpoints.

    - beats.txt    : linea 1 = tempo global (BPM). Resto = un beat por linea.
    - downbeats.txt : un downbeat por linea (formato beat_this). Cada linea
                      ES el "uno" de un compas. La app lo interpreta asi.

    beat_this SIEMPRE corre en CPU. La GPU queda libre para la separacion.
    En vez de un solo checkpoint, corremos los N `finalX` publicados
    (misma arquitectura, distinta semilla) y promediamos sus logits
    frame-level. Sobre ese promedio corre el Postprocessor DBN de
    beat_this, que internamente usa el DBNDownBeatTrackingProcessor de
    madmom (Bock et al.) para tracking global de beat + downbeat con
    modelo Bayesiano dinamico. Esto suaviza el pulso en zonas ambiguas
    y elimina el picks-vecinos que dejaba el peak-picking "minimal".
    """

    info_song = pydub_utils.mediainfo(input_file)
    song_duration = int(float(info_song["duration"]) * 1000)
    print("Song duration:", song_duration, "ms")

    # Import tardio: beat_this arrastra torch (pesado). Solo al necesitarlo.
    from beat_this.inference import Audio2Frames, Postprocessor, load_audio

    BEAT_THIS_DEVICE = "cpu"
    # Los tres checkpoints del paper: mismo modelo, distinta semilla.
    CHECKPOINTS = ("final0", "final1", "final2")

    # Cargar audio una unica vez; cada modelo hace su forward sobre el mismo.
    signal, sr = load_audio(input_file)

    beat_sum = None
    downbeat_sum = None
    for ckpt in CHECKPOINTS:
        print(f"[beat_this] ensemble checkpoint {ckpt}...")
        a2f = Audio2Frames(checkpoint_path=ckpt, device=BEAT_THIS_DEVICE)
        b_logits, d_logits = a2f(signal, sr)
        beat_sum = b_logits if beat_sum is None else beat_sum + b_logits
        downbeat_sum = d_logits if downbeat_sum is None else downbeat_sum + d_logits

    n = len(CHECKPOINTS)
    beat_logits_avg = beat_sum / n
    downbeat_logits_avg = downbeat_sum / n

    # Postprocessor "dbn" = DBNDownBeatTrackingProcessor de madmom con
    # beats_per_bar=[3,4], min_bpm=55, max_bpm=215, transition_lambda=100
    # (parametros por defecto de beat_this, ver postprocessor.py).
    post = Postprocessor(type="dbn", fps=50)
    beats, downbeats = post(beat_logits_avg, downbeat_logits_avg)

    # generate_metronome exige array numpy float (usa slicing y .dtype).
    beats = np.asarray(beats, dtype=float)
    downbeats = np.asarray(downbeats, dtype=float)

    # Tempo global derivado del intervalo mediano entre beats.
    if len(beats) > 1:
        tempo = 60.0 / float(np.median(np.diff(beats)))
    else:
        tempo = 0.0

    out_txt = Path(output_dir) / "beats.txt"
    with open(out_txt, "w") as f:
        f.write(str(tempo) + '\n')
        for t in beats:
            f.write(f"{t}\n")
    print("Beats written to", out_txt)

    # Downbeats: un tiempo por linea, tal cual los da beat_this.
    downbeats_out_txt = Path(output_dir) / "downbeats.txt"
    with open(downbeats_out_txt, "w") as f:
        for t in downbeats:
            f.write(f"{t}\n")
    print("Downbeats written to", downbeats_out_txt)

    print("Generating metronomes")
    
    start = time.time()
    generate_metronome(beats, song_duration, "1x", output_dir)
    print(f"Metronome 1x generated in {time.time() - start:.2f} seconds.")
    start = time.time()
    generate_metronome(beats, song_duration, "0.5x", output_dir)
    print(f"Metronome 0.5x generated in {time.time() - start:.2f} seconds.")
    start = time.time()
    generate_metronome(beats, song_duration, "2x", output_dir)
    print(f"Metronome 2x generated in {time.time() - start:.2f} seconds.")

    print("Global Tempo:", tempo)
    return tempo


# ---------- LYRICS ----------
# Pistas de contexto para Whisper. Se pasan como `initial_prompt` cuando el
# usuario fija idioma. Objetivo: anclar el decoder al registro "letra de
# cancion" y reducir boilerplate de YouTube ("Suscribete", "Amara.org",
# "Subtitulos por..."). No van con language=None (auto) para no sesgar la
# deteccion.
_LYRICS_INITIAL_PROMPTS = {
    "es": "Letra de cancion en espanol.",
    "en": "Song lyrics in English.",
    "fr": "Paroles de chanson en francais.",
    "pt": "Letra de musica em portugues.",
    "it": "Testo di canzone in italiano.",
    "de": "Liedtext auf Deutsch.",
    "ca": "Lletra de canco en catala.",
    "gl": "Letra de cancion en galego.",
    "eu": "Kantuko hitzak euskaraz.",
    "nl": "Nederlands liedtekst.",
    "pl": "Tekst piosenki po polsku.",
    "sv": "Svenska sangtexter.",
    "tr": "Turkce sarki sozleri.",
    "ru": "Tekst pesni na russkom.",
    "ja": "Nihongo no kashi.",
    "ko": "Hangugeo norae gasa.",
    "zh": "Zhongwen gequ geci.",
    "ar": "Kalimat ughniya bilarabiya.",
    "hi": "Hindi gaane ke bol.",
}


def _prepare_vocals_for_whisper(vocals_path):
    """Devuelve (audio_path, tmp_path_or_None).

    Si en el mismo directorio de `vocals_path` existe `vocals2.flac` (coros
    del modo split), suma ambos en el dominio temporal a un WAV temporal y
    devuelve esa ruta. Whisper transcribe mejor con lead + armonias presentes.

    Si no hay vocals2, devuelve el vocals original tal cual.
    Nunca revienta: cualquier fallo en el mix cae de vuelta al vocals unico.
    """
    vocals_path = Path(vocals_path)
    sibling = vocals_path.parent / "vocals2.flac"
    if vocals_path.name != "vocals.flac" or not sibling.exists():
        return vocals_path, None
    try:
        import soundfile as sf
        lead, sr = sf.read(str(vocals_path), always_2d=True)
        cho, sr2 = sf.read(str(sibling), always_2d=True)
        if sr != sr2:
            print(f"[whisper] vocals mix skipped: sr mismatch {sr} vs {sr2}")
            return vocals_path, None
        n = min(len(lead), len(cho))
        ch = min(lead.shape[1], cho.shape[1])
        mix = lead[:n, :ch].astype(np.float32) + cho[:n, :ch].astype(np.float32)
        # Normaliza si se sale de rango; conserva mezcla si cabe.
        peak = float(np.max(np.abs(mix))) if mix.size else 0.0
        if peak > 1.0:
            mix /= peak
        tmp = vocals_path.parent / "vocals_mix.tmp.wav"
        sf.write(str(tmp), mix, sr, subtype="PCM_16")
        print(f"[whisper] mixed vocals+vocals2 -> {tmp.name}")
        return tmp, tmp
    except Exception as e:
        print(f"[whisper] vocals mix failed ({e}); using vocals.flac solo")
        return vocals_path, None


def run_lyrics(vocals_path, output_dir, language=None,
               model_name="large-v3", device="cuda", compute_type="float16"):
    """Transcribe la voz aislada con faster-whisper -> lyrics.json.

    - vocals_path: ruta a vocals.flac (o cualquier audio con solo voz).
    - output_dir : donde escribir lyrics.json.
    - language   : codigo ISO ("en", "es", "fr", ...). None -> auto-detect.
    - model_name : checkpoint de Whisper. "large-v3" es el estandar.
    - device     : "cuda" o "cpu".
    - compute_type: precision de inferencia. "float16" para GPU, "int8" para CPU.

    Formato de salida (`lyrics.json`):
        {"language": "en", "language_probability": 0.98, "duration": 234.5,
         "model": "large-v3",
         "segments": [{"id": 0, "start": 12.34, "end": 15.67, "text": "..."}, ...]}

    Timestamps a nivel de segmento (frase natural). Word-level no se incluye
    para no engordar el JSON; se puede activar en el futuro sin romper el
    formato actual (campo "words" opcional dentro de cada segmento).

    Cada llamada CARGA Y LIBERA el modelo. Justificado: lyrics es opt-in y de
    baja frecuencia; asi liberamos VRAM entre jobs para no bloquear la
    separacion. Coste amortizable ~5 s de carga.
    """
    import json
    import gc
    from faster_whisper import WhisperModel

    print(f"[whisper] loading model={model_name} device={device} compute={compute_type}")
    model = WhisperModel(model_name, device=device, compute_type=compute_type)

    # Mezcla lead + coros si el pipeline los separo (modo vocals=split).
    # Suma en dominio temporal a un WAV temporal; se borra al final. Whisper
    # transcribe mejor con las armonias presentes.
    audio_for_whisper, tmp_mix = _prepare_vocals_for_whisper(vocals_path)

    # Prompt de contexto solo cuando el usuario fija idioma. En auto no
    # inyectamos nada para no sesgar la deteccion automatica.
    prompt = _LYRICS_INITIAL_PROMPTS.get((language or "").lower()) if language else None

    print(f"[whisper] transcribing {audio_for_whisper} "
          f"(language={language or 'auto'}, prompt={'yes' if prompt else 'no'})")
    # Defaults de Whisper + dos ajustes minimos:
    # - condition_on_previous_text=False: rompe los bucles de repeticion
    #   infinita al no arrastrar la salida previa como contexto del
    #   siguiente segmento (causa clasica de alucinacion en cadena).
    # - initial_prompt: pequeno ancla al registro "letra de cancion" para
    #   reducir boilerplate tipo "Subtitulos por..." o "Suscribete al canal".
    segments_iter, info = model.transcribe(
        str(audio_for_whisper),
        language=language,
        word_timestamps=False,
        vad_filter=True,                      # ignora silencios largos, ahorra tiempo
        condition_on_previous_text=False,
        initial_prompt=prompt,
    )

    segments = []
    for i, seg in enumerate(segments_iter):
        text = seg.text.strip()
        if not text:
            continue
        segments.append({
            "id": i,
            "start": round(float(seg.start), 3),
            "end": round(float(seg.end), 3),
            "text": text,
        })

    out = {
        "language": info.language,
        "language_probability": round(float(info.language_probability), 4),
        "duration": round(float(info.duration), 3),
        "model": model_name,
        "segments": segments,
    }

    out_path = Path(output_dir) / "lyrics.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[whisper] lyrics written to {out_path} ({len(segments)} segments)")

    # Libera VRAM al terminar.
    del model
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass

    # Borra el WAV temporal del mix vocals+vocals2 si se creo.
    if tmp_mix is not None:
        try:
            tmp_mix.unlink()
        except OSError:
            pass

    return out


# ---------- CHORDS SECTION -----------------

_QUALITY_MAP = {
    "min": "m", "mi": "m", "MI": "m", "MIN": "m",
    "maj": "",  "Maj": "", "MAJ": "", "MA": "", "ma": "",
    "dim0": "dim0", "dim/": "dim/", "dim": "dim0",
    "aug":  "aug",
    "sus4": "sus4", "sus2": "sus2", "sus": "sus",  # sin ; aquí
}

_SUS_QUALITIES = {"sus", "sus2", "sus4"}

def _parse_extensions(rest: str) -> str:
    """
    Convierte extensiones tipo '7b5', 'maj7', 'add9', '#11' etc.
    en tokens MusGlyphs con ; delante de cada uno.
    
    Tokens reconocidos:
      - maj7, maj9...  → ;maj;N  (maj en superíndice + número)
      - add9, add11... → ;add;N
      - b5, b9, #11   → ;b5, ;#11  (alteración + número, juntos)
      - 7, 9, 11, 13  → ;N
    """
    result = ""
    i = 0
    while i < len(rest):
        # --- prefijos de texto: maj, add ---
        text_prefix = re.match(r"^(maj|add)", rest[i:], re.IGNORECASE)
        if text_prefix:
            prefix = text_prefix.group(1)
            i += len(prefix)
            # número que sigue
            num = re.match(r"^\d+", rest[i:])
            if num:
                result += f";{prefix};{num.group()}"
                i += len(num.group())
            else:
                result += f";{prefix}"
            continue

        # --- alteración + número: b5, #11, b9 ---
        alt_num = re.match(r"^([b#])(\d+)", rest[i:])
        if alt_num:
            result += f";{alt_num.group(1)}{alt_num.group(2)}"
            i += len(alt_num.group())
            continue

        # --- número solo: 7, 9, 11, 13 ---
        num = re.match(r"^\d+", rest[i:])
        if num:
            result += f";{num.group()}"
            i += len(num.group())
            continue

        # --- carácter desconocido: pasar tal cual ---
        result += rest[i]
        i += 1

    return result


def _chord_to_musglyphs(chord: str) -> str:
    if chord in ("N", "X", "NC"):
        return chord

    # Separar bajo
    parts = chord.split("/")
    base = parts[0]
    bass = f"/{parts[1]}" if len(parts) > 1 else ""

    # Raíz: letra + accidental
    m = re.match(r"^([A-G])([#b]?)", base)
    if not m:
        return chord

    root = m.group(1) + m.group(2)
    rest = base[m.end():]

    if not rest:
        return root + bass

    # Calidad
    quality_mg = ""
    matched_quality = ""
    for key in sorted(_QUALITY_MAP, key=len, reverse=True):
        if rest.startswith(key):
            matched_quality = key
            quality_mg = _QUALITY_MAP[key]
            rest = rest[len(key):]
            break
    else:
        # 'm' minúscula suelta → menor
        if rest.startswith("m"):
            quality_mg = "m"
            rest = rest[1:]

    # Si la calidad es sus, va en superíndice
    if matched_quality in _SUS_QUALITIES:
        quality_mg = f";{quality_mg}"

    ext_str = _parse_extensions(rest)

    return root + quality_mg + ext_str + bass
    
def _cast_file_to_musglyphs(path: str):
    """Reescribe cada acorde del fichero en formato MusGlyphs. Tiempos intactos."""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    out_lines = []
    for line in lines:
        stripped = line.rstrip("\n")
        if not stripped.strip():
            out_lines.append(stripped)
            continue
        parts     = stripped.split(maxsplit=1)
        chord     = parts[0]
        remainder = parts[1] if len(parts) > 1 else ""
        out_lines.append(f"{_chord_to_musglyphs(chord)} {remainder}".rstrip())

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines) + "\n")
        
def _simplify_to_triad(chord: str) -> str:
    """Reduce un acorde a su triada basica (raiz + mayor/menor). Tira el bajo."""
    if chord in ("N", "X", "NC"):
        return chord

    base = chord.split("/")[0]            # tirar bajo
    m = re.match(r"^([A-G])([#b]?)", base)
    if not m:
        return chord

    root = m.group(1) + m.group(2)
    rest = base[m.end():]

    if rest.startswith(("maj", "Maj", "M")):
        quality = ""                      # mayor
    elif rest.startswith(("min", "m")):
        quality = "m"                     # menor
    elif rest.startswith(("dim", "\u00b0", "\u00f8")):
        quality = "m"                     # dim -> menor
    elif rest.startswith(("aug", "+")):
        quality = ""                      # aug -> mayor
    else:
        quality = ""                      # 7,9,sus... -> mayor

    return root + quality

# ---------- BTC CHORDS ----------
def run_btc(input_file: str, output_dir: str, btc_script="btc_extractor.py"):
    # Mapa de calidades mir_eval (BTC large voca) -> tu formato corto. Mayor = raiz pelada.
    quality_map = {
        "": "", "maj": "", "min": "m", "maj7": "maj7", "min7": "m7", "7": "7",
        "dim": "dim", "aug": "aug", "dim7": "dim7", "hdim7": "m7b5",
        "min6": "m6", "maj6": "6", "minmaj7": "mMaj7", "sus2": "sus2", "sus4": "sus4",
    }
    enharmonics = {"A#": "Bb", "C#": "Db", "D#": "Eb", "G#": "Ab"}

    script_path = str(Path(os.getcwd()) / btc_script)
    cmd = [sys.executable, script_path, input_file, str(output_dir)]
    print("Running BTC Chord Extractor subprocess...")
    subprocess.run(cmd, check=True)

    # BTC nombra el .lab con el stem del audio de entrada
    input_stem = Path(input_file).stem
    lab_file = Path(output_dir) / f"{input_stem}.lab"
    chords_file = Path(output_dir) / "chords.txt"

    if not lab_file.exists():
        print(f"BTC did not generate the expected .lab file at {lab_file}")
        return [], []

    lines_to_write = []
    chords_list = []

    # Leer .lab (start end chord) y castear al formato de tu app
    with open(lab_file, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) < 3:
                continue
            start_time = parts[0]            # parts[1] (end) se ignora: tu app usa solo el inicio
            raw_chord = parts[2]

            if raw_chord in ("N", "X"):
                clean_chord = raw_chord
            else:
                if ":" in raw_chord:
                    root, quality = raw_chord.split(":", 1)
                else:
                    root, quality = raw_chord, ""          # mayor = raiz pelada
                root = enharmonics.get(root, root)          # sostenido -> bemol
                clean_chord = root + quality_map.get(quality, quality)

            lines_to_write.append(f"{clean_chord} {start_time}\n")
            chords_list.append([clean_chord, start_time])

    # Escribir chords.txt (formato: acorde inicio)
    with open(chords_file, "w", encoding="utf-8") as f:
        f.writelines(lines_to_write)
    print("Chords successfully converted and written to", chords_file)

    # Generar la version basica chords_basic.txt (misma carpeta)
    #analyze_chords_file(str(output_dir))

    # Limpieza: borrar .lab y .mid/.midi temporales
    for temp in [lab_file,
                 Path(output_dir) / f"{input_stem}.mid",
                 Path(output_dir) / f"{input_stem}.midi"]:
        if temp.exists():
            try:
                os.remove(temp)
                print(f"Removed temporary file: {temp.name}")
            except Exception as e:
                print(f"Could not remove {temp.name}: {e}")

    # Quedan en el directorio: chords.txt y chords_basic.txt
    chords_unmodified = copy.deepcopy(chords_list)
    return chords_list, chords_unmodified
        
def analyze_chords_file(chord_file_dir: str):
    """
    1) Genera chords_basic.txt (triadas) desde chords.txt crudo.
    2) Castea chords.txt y chords_basic.txt a MusGlyphs.
    """
    try:
        if os.path.isfile(chord_file_dir):
            chord_file_dir = os.path.dirname(chord_file_dir)

        input_path = os.path.join(chord_file_dir, "chords.txt")
        basic_path = os.path.join(chord_file_dir, "chords_basic.txt")

        if not os.path.isfile(input_path):
            raise FileNotFoundError(f"No existe: {input_path}")

        # --- Paso 1: generar basico desde el CRUDO ---
        with open(input_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        basic_lines = []
        for line in lines:
            stripped = line.rstrip("\n")
            if not stripped.strip():
                basic_lines.append(stripped)
                continue
            parts     = stripped.split(maxsplit=1)
            chord     = parts[0]
            remainder = parts[1] if len(parts) > 1 else ""
            basic_lines.append(f"{_simplify_to_triad(chord)} {remainder}".rstrip())

        with open(basic_path, "w", encoding="utf-8") as f:
            f.write("\n".join(basic_lines) + "\n")

        # --- Paso 2: castear AMBOS a MusGlyphs ---
        _cast_file_to_musglyphs(input_path)
        _cast_file_to_musglyphs(basic_path)

        print(f"[chords] Casteado: {input_path}")
        print(f"[chords_basic] Generado y casteado: {basic_path}")

    except Exception as e:
        print(f"[chords] ERROR: {e}")

# ---------- AUDIO SEPARATOR MODEL ----------

def _gpu_visible():
    """Comprobacion barata: ¿hay GPU NVIDIA? No carga torch."""
    try:
        r = subprocess.run(["nvidia-smi", "-L"],
                           stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL,
                           text=True, timeout=5)
        return r.returncode == 0 and "GPU" in r.stdout
    except Exception:
        return False
        
def _write_sep_config(output_dir, sep_config):
    """Vuelca el dict de la GUI a un JSON que lee audio_separator_script.py."""
    import json
    cfg_path = str(Path(output_dir) / "sep_config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(sep_config, f, ensure_ascii=False)
    return cfg_path


def start_audio_separator(input_file, output_dir, sep_config, device="cpu"):
    """Lanza el separador SIN bloquear. Devuelve el Popen."""
    script_path = str(Path(os.getcwd()) / "audio_separator_script.py")
    cfg_path = _write_sep_config(output_dir, sep_config)
    cmd = [sys.executable, script_path, input_file, output_dir, cfg_path]

    env = os.environ.copy()
    if device != "cuda":
        env["CUDA_VISIBLE_DEVICES"] = "-1"   # CPU de verdad

    mode = sep_config.get("mode", "?")
    print(f"Lanzando audio-separator ({mode}, {device}) en segundo plano...")
    return subprocess.Popen(cmd, env=env)


def run_audio_separator(input_file, output_dir, sep_config, device="cpu", on_proc=None):
    """Version BLOQUEANTE (modo serie). Lanza y espera; revienta si falla."""
    proc = start_audio_separator(input_file, output_dir, sep_config, device)
    if on_proc:
        on_proc(proc)        # la GUI lo guarda para poder cancelar
    ret = proc.wait()
    if ret != 0:
        raise RuntimeError(f"Audio-Separator fallo (codigo {ret})")

# ---------- MAIN PIPELINE ----------
def run_pipeline(input_file: str, output_dir: str, device: str = "cpu",
                 btc_script: str = "btc_extractor.py",
                 sep_config: dict = None,
                 on_proc=None):
    os.makedirs(output_dir, exist_ok=True)

    if sep_config is None:   # retrocompat: por defecto, clasico 2 stems
        sep_config = {"mode": "classic", "preset": "2stem", "sep_mode": 2,
                      "vocals": "single"}

    print("=== Obteining metadata ===")
    run_metadata_extractor(input_file, output_dir)

    # Paralelo solo con GPU real: separacion (GPU) + beats (CPU) a la vez.
    paralelo = (device == "cuda") and _gpu_visible()

    if paralelo:
        print(f"=== Audio-Separator ({sep_config.get('mode')}) [GPU] + beat_this [CPU] paralelo ===")
        proc = start_audio_separator(input_file, output_dir, sep_config, device)
        if on_proc:
            on_proc(proc)                   # para poder cancelar
        try:
            run_beats(input_file, output_dir)   # beat_this en CPU mientras la GPU separa
        finally:
            ret = proc.wait()                   # esperamos siempre a la separacion
        if ret != 0:
            raise RuntimeError(f"Audio-Separator fallo (codigo {ret})")
        print("Audio-Separator terminado.")
    else:
        print(f"=== Audio-Separator ({sep_config.get('mode')}) [serie] ===")
        spinner = Spinner("Running Audio-Separator Pipeline")
        spinner.start()
        try:
            run_audio_separator(input_file, output_dir, sep_config, device,
                                on_proc=on_proc)
        finally:
            spinner.stop()
        print("\n=== beat_this beats/tempo [CPU] ===")
        run_beats(input_file, output_dir)

    beats_input = input_file

    print("\n=== CHORD EXTRACTION (BTC) ===")
    run_btc(beats_input, output_dir, btc_script)

    analyze_chords_file(output_dir)

    print("\nProcess completed...")
