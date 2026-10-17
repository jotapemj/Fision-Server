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


def _apply_models_dir():
    """FISION_MODELS_DIR -> una raiz para todos los pesos.

    Rellena solo lo que no venga ya fijado (las envs concretas mandan).
    Antes de cualquier import pesado: HF lee HF_HOME al importarse. Los
    subprocesos (separador) heredan os.environ.
    """
    root = os.environ.get("FISION_MODELS_DIR", "").strip()
    if not root:
        return
    root = os.path.abspath(os.path.expanduser(root))
    for env, sub in (("FISION_SEP_MODEL_DIR", "separator"),   # audio-separator
                     ("HF_HOME", "hf"),                        # faster-whisper
                     ("TORCH_HOME", "torch")):                 # beat_this
        if not os.environ.get(env, "").strip():
            os.environ[env] = os.path.join(root, sub)


_apply_models_dir()

# Rutas del repo, no del directorio de trabajo: el server o un CLI lanzado
# desde otra carpeta no encontraban los recursos.
_HERE = Path(__file__).resolve().parent

_clic_seg = AudioSegment.from_file(str(_HERE / "resources" / "metronome.wav"))

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
# Umbral (en segundos) de silencio entre palabras para partir un segmento de
# Whisper en sub-frases. Con word_timestamps=True el decoder tiende a agrupar
# multiples frases en un unico segment de decenas de segundos; el split por
# gap las devuelve a un tamano usable para karaoke por linea.
_LYRICS_WORD_GAP_S = 2.0

# Post-proc del split: cuando el cantante suelta la primera palabra del verso
# y hace pausa antes de rematar ("Es" ... "mentira que hoy..."), el split la
# deja huerfana. Se fusiona hacia adelante si tiene menos de _MIN_WORDS y el
# gap al siguiente es corto (< _MERGE_GAP_S). Si el gap es grande (p.ej.
# 27 s antes del estribillo), NO se fusiona: es una pausa musical real.
_LYRICS_MERGE_MIN_WORDS = 2
_LYRICS_MERGE_GAP_S = 6.5

# Cap duro de duracion por sub-segment. Whisper a veces resuelve un verso
# entero de 10-11 s en un unico segment denso. Para karaoke por linea eso no
# vale: si un sub-segment excede este limite se parte en la mejor frontera
# de frase (ver _split_score), recursivo hasta que todo cabe.
_LYRICS_MAX_SEG_S = 8.0
# Palabras minimas a cada lado de un corte: nunca deja lineas de 1 palabra.
_LYRICS_SPLIT_MIN_WORDS = 3
# Pronombres ingleses siempre en mayuscula: no marcan inicio de verso.
_LYRICS_ALWAYS_UPPER = {"i", "i'm", "i'll", "i've", "i'd"}


def _word_to_dict(w):
    """Serializa un faster_whisper.Word a dict del JSON de salida."""
    return {
        "start": round(float(w.start), 3),
        "end": round(float(w.end), 3),
        "text": w.word.strip(),
    }


def _sub_seg_from_words(words):
    """Construye un sub-segmento (start/end/text/words) a partir de una lista
    contigua de faster_whisper.Word."""
    text = "".join(w.word for w in words).strip()
    return {
        "start": round(float(words[0].start), 3),
        "end": round(float(words[-1].end), 3),
        "text": text,
        "words": [_word_to_dict(w) for w in words],
    }


def _join_words(ws):
    """Une palabras ya serializadas. Las que empiezan por - o ' son
    continuacion de la anterior en Whisper ("Uh" "-huh", "y" "'all")."""
    text = ""
    for w in ws:
        t = w["text"]
        text += t if not text or t[:1] in "-'’" else " " + t
    return text.strip()


def _split_score(words, i, t0, t1):
    """Puntua cortar antes de words[i]. Whisper marca el inicio de verso con
    mayuscula y el final con coma/punto; el gap ayuda poco (al encadenar
    frases las palabras van pegadas, gap 0). Desempate: corte equilibrado."""
    prev, cur = words[i - 1]["text"], words[i]["text"]
    mid = abs(float(words[i]["start"]) - (t0 + t1) / 2) / max(t1 - t0, 1e-6)
    if cur[:1] == "-":                        # "Ho" "-o-us": misma palabra
        return -10.0 - mid
    score = 0.0
    if prev[-1:] in ".,;:!?":
        score += 3
    if cur[:1].isupper() and cur.lower().strip(".,;:!?") not in _LYRICS_ALWAYS_UPPER:
        score += 2
    score += min(max(float(words[i]["start"]) - float(words[i - 1]["end"]), 0.0), 1.0)
    return score - mid                        # mid en [0, 0.5]


def _split_long_sub(sub, max_s=_LYRICS_MAX_SEG_S, min_words=_LYRICS_SPLIT_MIN_WORDS):
    """Parte un sub-segment demasiado largo en la mejor frontera de frase.
    Recursivo. Si no hay corte que deje min_words a cada lado, lo deja tal
    cual (linea larga mejor que palabras sueltas)."""
    t0, t1 = float(sub["start"]), float(sub["end"])
    words = sub.get("words") or []
    if t1 - t0 <= max_s or len(words) < 2 * min_words:
        return [sub]
    best_i = max(range(min_words, len(words) - min_words + 1),
                 key=lambda i: _split_score(words, i, t0, t1))
    if _split_score(words, best_i, t0, t1) <= -10 and t1 - t0 <= 2 * max_s:
        return [sub]                          # solo cortes dentro de palabra
    parts = []
    for ws in (words[:best_i], words[best_i:]):
        parts.append({
            "start": ws[0]["start"],
            "end": ws[-1]["end"],
            "text": _join_words(ws),
            "words": ws,
        })
    return _split_long_sub(parts[0], max_s, min_words) + _split_long_sub(parts[1], max_s, min_words)


# Salto de verso. Whisper escribe cada verso con mayuscula inicial y sin
# puntuacion (formato de letras), pero si el cantante encadena no hay
# silencio y dos versos caen en la misma linea. Se parte en la mayuscula
# solo si la palabra anterior es nota sostenida: los versos acaban en nota
# larga (0.4-1.6 s medido) y los nombres propios van tras "de", "en", "mi",
# "hasta" (~0.2 s). Asi "de Bahrein" o "el Nueva York" no se parten.
_LYRICS_VERSE_HOLD_S = 0.35


def _is_verse_start(prev, cur):
    t = cur["text"]
    if t[:1] == "-":                          # "-Ra": sigue la palabra previa
        return False
    k = next((n for n, c in enumerate(t) if c.isalpha()), None)
    if k is None or not t[k].isupper():
        return False
    if t.lower().strip(".,;:!?\"'") in _LYRICS_ALWAYS_UPPER:
        return False
    return (prev["text"][-1:] in ".!?"
            or float(prev["end"]) - float(prev["start"]) >= _LYRICS_VERSE_HOLD_S)


def _split_verses(sub, min_words=_LYRICS_SPLIT_MIN_WORDS):
    """Parte un sub-segment en cada inicio de verso, dejando al menos
    min_words palabras por pieza."""
    words = sub.get("words") or []
    cuts, last = [], 0
    for i in range(min_words, len(words) - min_words + 1):
        if i - last >= min_words and _is_verse_start(words[i - 1], words[i]):
            cuts.append(i)
            last = i
    if not cuts:
        return [sub]
    out = []
    for a, b in zip([0] + cuts, cuts + [len(words)]):
        ws = words[a:b]
        out.append({"start": ws[0]["start"], "end": ws[-1]["end"],
                    "text": _join_words(ws), "words": ws})
    return out


# Coletillas de subtitulos de YouTube que Whisper alucina en tramos sin voz.
# Patrones explicitos: nada de umbrales que puedan comerse letra real
# ("te amara" es letra; "Amara.org" no).
_LYRICS_BOILERPLATE = re.compile(
    r"amara\s*\.\s*org|^\W*subt[ií]tul(os|ado)\b.*\bpor\b|"
    r"^\W*subtitles\s+by\b|\bsuscr[ií]bete\b|\bthanks\s+for\s+watching\b|"
    r"\bgracias\s+por\s+ver\b", re.IGNORECASE)


def _is_boilerplate(sub):
    return bool(_LYRICS_BOILERPLATE.search(sub["text"]))


def _sub_from_dicts(ws):
    return {"start": ws[0]["start"], "end": ws[-1]["end"],
            "text": _join_words(ws), "words": ws}


# Bucles de Whisper ("Judas" x39, "ya" x66): las repeticiones sobrantes salen
# con duracion ~0 (el alineador no les encuentra sitio). Se tira la palabra
# de duracion < _LOOP_DUR_S que repite la anterior; las cantadas de verdad
# tienen duracion y se quedan.
_LYRICS_LOOP_DUR_S = 0.02


def _word_key(text):
    return re.sub(r"\W", "", text.lower())


def _drop_loop_words(subs):
    out, prev = [], None
    for sub in subs:
        ws = sub.get("words") or []
        if not ws:
            out.append(sub)
            continue
        kept = []
        for w in ws:
            k = _word_key(w["text"])
            if k == prev and float(w["end"]) - float(w["start"]) < _LYRICS_LOOP_DUR_S:
                continue
            kept.append(w)
            prev = k
        if kept:
            out.append(sub if len(kept) == len(ws) else _sub_from_dicts(kept))
    return out


# Primera palabra de verso pegada al final del anterior: el alineador le da
# la cola de la nota sostenida previa ("...down A" / "king with no crown").
# Si la linea acaba en inicio de verso y la siguiente empieza en minuscula o
# "-", la palabra pasa a la siguiente, justo antes de su primera palabra
# (max _LEAD_MAX_S); la palabra previa recupera su nota. Desactivable aqui.
_LYRICS_MOVE_LEADS = True
_LYRICS_LEAD_MAX_S = 0.5


def _move_stray_leads(subs):
    for a, b in zip(subs, subs[1:]):
        wa, wb = a.get("words") or [], b.get("words") or []
        if len(wa) < 2 or not wb or b["start"] <= a["end"]:
            continue
        nxt = wb[0]["text"]
        if not (nxt[:1] == "-" or nxt[:1].islower()):
            continue
        p = wa[-2]
        if wa[-1]["text"].lower() in _LYRICS_ALWAYS_UPPER:   # "I" / "want" (sin coma)
            ok = (p["text"][-1:] in ".!?"
                  or float(p["end"]) - float(p["start"]) >= _LYRICS_VERSE_HOLD_S)
        else:
            ok = _is_verse_start(p, wa[-1])
        if not ok:
            continue
        w, prev = dict(wa[-1]), dict(wa[-2])
        prev["end"] = w["end"]                # la nota previa dura hasta aqui
        dur = min(float(w["end"]) - float(w["start"]), _LYRICS_LEAD_MAX_S)
        w["end"] = wb[0]["start"]
        w["start"] = round(float(w["end"]) - dur, 3)
        a.update(_sub_from_dicts(wa[:-2] + [prev]))
        b.update(_sub_from_dicts([w] + wb))
    return subs


def _merge_hyphen_tails(subs, merge_gap_s=_LYRICS_MERGE_GAP_S):
    """Linea que empieza por "-o-us" es la cola de la palabra anterior
    ("Ho-o-o" ... "-o-us and Judas"): se une si la pausa es corta."""
    out = []
    for sub in subs:
        ws = sub.get("words") or []
        if (out and ws and out[-1].get("words") and ws[0]["text"][:1] == "-"
                and sub["start"] - out[-1]["end"] < merge_gap_s):
            out[-1] = _sub_from_dicts(out[-1]["words"] + ws)
        else:
            out.append(sub)
    return out


def _split_long_subs(subs, max_s=_LYRICS_MAX_SEG_S):
    out = []
    for sub in subs:
        out.extend(_split_long_sub(sub, max_s))
    return out


def _merge_orphan_leads(subs, min_words=_LYRICS_MERGE_MIN_WORDS,
                        merge_gap_s=_LYRICS_MERGE_GAP_S):
    """Fusiona sub-segments cortos con el siguiente cuando la pausa entre
    ambos es breve. Objetivo: evitar sub-segments de 1 palabra ("Es", "Pero")
    que Whisper produce cuando el cantante suelta la primera palabra del
    verso y respira antes de continuar. Si el gap con el siguiente supera
    `merge_gap_s`, la pausa se considera pausa musical real y NO se fusiona
    (caso "Porque ... [27 s de instrumental] ... aquello que un dia...")."""
    if not subs:
        return subs
    out = []
    i = 0
    while i < len(subs):
        cur = subs[i]
        if (i + 1 < len(subs)
                and len(cur["words"]) < min_words
                and (subs[i + 1]["start"] - cur["end"]) < merge_gap_s):
            nxt = subs[i + 1]
            sep = " " if cur["text"] and nxt["text"] else ""
            out.append({
                "start": cur["start"],
                "end": nxt["end"],
                "text": (cur["text"] + sep + nxt["text"]).strip(),
                "words": cur["words"] + nxt["words"],
            })
            i += 2
        else:
            out.append(cur)
            i += 1
    return out


_CAPS_WORD = re.compile(r"\b[^\W\d_]{3,}\b")


def _lower_caps(text):
    """Pasa a minuscula las palabras en MAYUSCULAS de 3+ letras (las de 1-2
    se quedan: siglas tipo DJ, pronombre I)."""
    return _CAPS_WORD.sub(lambda m: m.group().lower() if m.group().isupper() else m.group(), text)


def _uncap(sub):
    """Whisper escribe lo gritado en MAYUSCULAS. Se pasa a minuscula palabra
    a palabra, conservando la mayuscula inicial de la linea. Coste: nombres
    propios gritados quedan en minuscula (MEDELLIN -> medellin)."""
    text = _lower_caps(sub["text"])
    if text == sub["text"]:
        return sub
    k = next((n for n, c in enumerate(text) if c.isalpha()), None)
    if k is not None and sub["text"][k].isupper():
        text = text[:k] + text[k].upper() + text[k + 1:]
    sub["text"] = text
    for n, w in enumerate(sub["words"]):
        t = _lower_caps(w["text"])
        if n == 0 and t != w["text"]:
            k = next((m for m, c in enumerate(t) if c.isalpha()), 0)
            t = t[:k] + t[k:k + 1].upper() + t[k + 1:]
        w["text"] = t
    return sub


def _split_by_word_gaps(seg, gap_s=_LYRICS_WORD_GAP_S):
    """Parte un segment de Whisper en sub-segments cuando el silencio entre
    palabras consecutivas supera `gap_s`. Si no hay words (segment vacio o
    modelo raro), devuelve un unico dict sin `words`."""
    words = list(getattr(seg, "words", None) or [])
    if not words:
        return [{
            "start": round(float(seg.start), 3),
            "end": round(float(seg.end), 3),
            "text": seg.text.strip(),
            "words": [],
        }]
    bucket = [words[0]]
    out = []
    for w in words[1:]:
        if float(w.start) - float(bucket[-1].end) >= gap_s:
            out.append(_sub_seg_from_words(bucket))
            bucket = [w]
        else:
            bucket.append(w)
    out.append(_sub_seg_from_words(bucket))
    return out


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
               model_name="large-v2", device="cuda", compute_type="float16"):
    """Transcribe la voz aislada con faster-whisper -> lyrics.json.

    - vocals_path: ruta a vocals.flac (o cualquier audio con solo voz).
    - output_dir : donde escribir lyrics.json.
    - language   : codigo ISO ("en", "es", "fr", ...). None -> auto-detect.
    - model_name : checkpoint de Whisper. "large-v2": mas robusto que v3 en
                   canto (menos alucinacion).
    - device     : "cuda" o "cpu".
    - compute_type: precision de inferencia. "float16" para GPU, "int8" para CPU.

    Formato de salida (`lyrics.json`):
        {"language": "en", "language_probability": 0.98, "duration": 234.5,
         "model": "large-v2",
         "segments": [
           {"id": 0, "start": 12.34, "end": 15.67, "text": "...",
            "words": [{"start": 12.34, "end": 12.55, "text": "I"}, ...]},
           ...
         ]}

    Timestamps a nivel de segmento (frase natural) Y a nivel de palabra
    dentro de cada segment. Los segments largos que Whisper devuelve al
    activar word_timestamps se re-parten por gap de silencio entre palabras
    (`_LYRICS_WORD_GAP_S`, por defecto 2 s), para karaoke por linea con
    tramos limpios y `words` para resaltado word-by-word opcional.

    Cada llamada CARGA Y LIBERA el modelo. Justificado: lyrics es opt-in y de
    baja frecuencia; asi liberamos VRAM entre jobs para no bloquear la
    separacion. Coste amortizable ~5 s de carga.
    """
    import gc
    from faster_whisper import WhisperModel

    # Batched inference opcional: FISION_WHISPER_BATCH_SIZE >= 2 activa
    # BatchedInferencePipeline (paraleliza chunks del VAD en la GPU, gasta
    # mas VRAM a cambio de menos tiempo). 0/1 -> mono-batch clasico.
    batch_size = int(os.environ.get("FISION_WHISPER_BATCH_SIZE", "0"))

    print(f"[whisper] loading model={model_name} device={device} "
          f"compute={compute_type} batch={batch_size or 'off'}")
    model = WhisperModel(model_name, device=device, compute_type=compute_type)

    pipeline = None
    if batch_size >= 2:
        try:
            from faster_whisper import BatchedInferencePipeline
            pipeline = BatchedInferencePipeline(model=model)
        except Exception as e:
            print(f"[whisper] BatchedInferencePipeline no disponible ({e}); "
                  f"cayendo a mono-batch")
            pipeline = None

    # Mezcla lead + coros si el pipeline los separo (modo vocals=split).
    # Suma en dominio temporal a un WAV temporal; se borra al final. Whisper
    # transcribe mejor con las armonias presentes.
    audio_for_whisper, tmp_mix = _prepare_vocals_for_whisper(vocals_path)
    try:
        return _transcribe(model, pipeline, batch_size, audio_for_whisper,
                           output_dir, language, model_name)
    finally:
        # Libera VRAM y borra el WAV del mix aunque Whisper reviente
        del model, pipeline
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        if tmp_mix is not None:
            try:
                tmp_mix.unlink()
            except OSError:
                pass


def _transcribe(model, pipeline, batch_size, audio_for_whisper, output_dir,
                language, model_name):
    import json
    print(f"[whisper] transcribing {audio_for_whisper} "
          f"(language={language or 'auto'})")
    # Defaults de Whisper + ajustes minimos:
    # - condition_on_previous_text=False: rompe los bucles de repeticion
    #   infinita al no arrastrar la salida previa como contexto del
    #   siguiente segmento (causa clasica de alucinacion en cadena).
    # - SIN initial_prompt: Whisper lo copiaba como letra en tramos sin voz
    #   ("Song lyrics in English."), sobre todo en modo batched, que lo
    #   inyecta en cada chunk.
    # - word_timestamps=True: fija seg.end al fin de la ultima palabra,
    #   evitando que Whisper infle el end hasta el siguiente segmento
    #   (o hasta el final del track para el ultimo). Los words se usan
    #   solo internamente; el JSON de salida no cambia de forma.
    transcribe_kwargs = dict(
        language=language,
        word_timestamps=True,
        vad_filter=True,                      # ignora silencios largos, ahorra tiempo
        condition_on_previous_text=False,
    )
    if pipeline is not None:
        segments_iter, info = pipeline.transcribe(
            str(audio_for_whisper), batch_size=batch_size, **transcribe_kwargs,
        )
    else:
        segments_iter, info = model.transcribe(
            str(audio_for_whisper), **transcribe_kwargs,
        )

    subs = []
    for seg in segments_iter:
        for sub in _split_by_word_gaps(seg):
            if not sub["text"]:
                continue
            subs.append(sub)

    subs = _drop_loop_words(subs)
    subs = [sub for sub in subs if not _is_boilerplate(sub)]
    if _LYRICS_MOVE_LEADS:
        subs = _move_stray_leads(subs)
    subs = _merge_hyphen_tails(subs)
    subs = _merge_orphan_leads(subs)
    subs = [v for sub in subs for v in _split_verses(sub)]
    subs = _split_long_subs(subs)
    subs = [_uncap(sub) for sub in subs]

    segments = []
    for sid, sub in enumerate(subs):
        segments.append({
            "id": sid,
            "start": sub["start"],
            "end": sub["end"],
            "text": sub["text"],
            "words": sub["words"],
        })

    out = {
        "language": info.language,
        "language_probability": round(float(info.language_probability), 4),
        "duration": round(float(info.duration), 3),
        "model": model_name,
        "segments": segments,
    }

    # Atomico: un corte a medias nunca deja un lyrics.json truncado
    out_path = Path(output_dir) / "lyrics.json"
    tmp_path = out_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, out_path)
    print(f"[whisper] lyrics written to {out_path} ({len(segments)} segments)")
    return out


# ---------- CHORDS SECTION -----------------

# Reglas MusGlyphs 2.1 (comprobadas por JP con la fuente): ";" sube SOLO el
# siguiente caracter o ligadura. Cada digito lleva el suyo (C;1;3 = 13; C;13
# solo sube el 1), igual alteraciones y texto: C;maj;7, Ebm;7;b;5, C;sus;4.
_QUALITY_MAP = {
    "min": "m", "mi": "m", "MI": "m", "MIN": "m",
    "maj": "",  "Maj": "", "MAJ": "", "MA": "", "ma": "",
    "dim0": "dim0", "dim/": "dim/", "dim": "dim0",
    "aug":  "aug",
    "sus": "sus",
}
_MAJOR_KEYS = {"maj", "Maj", "MAJ", "MA", "ma"}


def _parse_extensions(rest: str) -> str:
    """Extensiones ('7b5', 'maj7', 'add11', '#11', '(b9)') -> tokens
    MusGlyphs, un ';' por digito, alteracion, parentesis o palabra."""
    result = ""
    i = 0
    while i < len(rest):
        word = re.match(r"^(maj|add|sus|alt)", rest[i:], re.IGNORECASE)
        if word:
            result += ";" + word.group(1)
            i += len(word.group(1))
            continue
        c = rest[i]
        if c.isdigit() or c in "b#()+-":
            result += ";" + c
        else:
            result += c                       # desconocido: tal cual
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

    # Calidad en linea (m, dim0, aug). "maj" seguido de numero no es calidad:
    # es la extension maj7 (antes se comia el maj y Gmaj7 salia como G7).
    quality_mg = ""
    for key in sorted(_QUALITY_MAP, key=len, reverse=True):
        if rest.startswith(key):
            after = rest[len(key):]
            if key in _MAJOR_KEYS and after[:1].isdigit():
                break
            if key == "sus":
                break                         # sus va arriba: lo hace _parse_extensions
            quality_mg = _QUALITY_MAP[key]
            rest = after
            break
    else:
        # 'm' minúscula suelta → menor
        if rest.startswith("m") and not rest.startswith("maj"):
            quality_mg = "m"
            rest = rest[1:]

    return root + quality_mg + _parse_extensions(rest) + bass


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

    script_path = str(_HERE / btc_script)
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
    script_path = str(_HERE / "audio_separator_script.py")
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
