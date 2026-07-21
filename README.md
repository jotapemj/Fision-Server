# Fision

Self-hosted AI music processing. Feed it a song and Fision separates it into
stems (vocals, bass, drums, guitar, piano, winds…), detects **beats** and
**downbeats**, extracts **chords**, and renders **metronomes** and cover art —
all on your own machine. No cloud, no accounts, your music stays yours.

> **Status:** in active development. Runs, but rough edges remain — see each
> module's "pending" notes.

## Architecture

Three pieces:

```
  ┌─────────────────┐        HTTP + Bearer token         ┌──────────────────────┐
  │  Fision Android │  ───────────────────────────────►  │   Fision Server      │
  │   (mobile app)  │        (Tailscale / Cloudflare)    │   FastAPI + queue    │
  └─────────────────┘                                    └──────────┬───────────┘
                                                                    │ subprocess
                                                                    ▼
                                                         ┌──────────────────────┐
                                                         │   Fision pipeline    │
                                                         │  separation + beats  │
                                                         │  + chords (GPU)      │
                                                         └──────────────────────┘
```

- **Server** (`fision_server/`) — receives audio, queues jobs, keeps a
  hash-based library so the same song is never processed twice. This is the
  main entry point. See [`fision_server/README.md`](fision_server/README.md).
- **Pipeline** — the actual audio work, run as an isolated subprocess.
- **Android app** — the client, in its own repo *(coming soon)*.

## What the pipeline does

- **Stem separation** (a fixed model chain, GPU): a RoFormer splits
  vocals / instrumental, HTDemucs `ft` gives bass/drums, HTDemucs `6s` gives
  guitar/piano, an MDX23C drum-separator can split the kit, and a wind model
  pulls brass/woodwinds. `other` is always derived by subtraction.
- **Beats & downbeats** — beat_this (ISMIR 2024), on CPU, in parallel with the
  GPU separation. Frame-level logits are averaged across the three published
  checkpoints (`final0`, `final1`, `final2`) and fed into a Dynamic Bayesian
  Network postprocessor (madmom's `DBNDownBeatTrackingProcessor`), which
  performs joint beat and downbeat tracking with Viterbi decoding over a
  tempo/phase state space. Time signature is auto-detected between 3/4 and
  4/4; tempo range is 55-215 BPM.
- **Chords** — BTC, a Transformer chord-recognition model, large vocabulary
  (170 chords), included as a git submodule.
- **Lyrics (opt-in)** — Whisper (via `faster-whisper`, CTranslate2 engine) on
  the isolated `vocals.flac` stem. Not part of the main pipeline; runs only
  when the client explicitly requests transcription. Segment-level timestamps
  (one entry per phrase) suitable for karaoke rendering. Model loads and
  releases per request so it does not permanently occupy VRAM. Whisper
  supports 99 languages; the client passes an ISO code (`en`, `es`, `fr`, ...)
  or lets it auto-detect.

## Install

```bash
# 1. Clone WITH submodules (BTC lives as a submodule, weights included)
git clone --recurse-submodules https://github.com/jotapemj/Fision.git
cd Fision
# (if you forgot --recurse-submodules)
git submodule update --init --recursive

# 2. Python 3.10+ virtual environment
python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate

# 3. System deps (not pip)
sudo apt install ffmpeg python3-tk  # ffmpeg for audio; python3-tk for the GUI

# 4. PyTorch FIRST, with the CUDA index for your GPU
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128

# 5. Everything else
pip install -r requirements.txt
```

### Model weights

The separation models are downloaded automatically by `audio-separator` on
first run. The **BTC chord model** weights ship inside the submodule
(`BTC-ISMIR19/test/btc_model_large_voca.pt`), so a recursive clone already has
everything — nothing else to download.

## Usage

### 1. Server (recommended)

The real way to run Fision: hash-based library, caching, queue, mobile app.

```bash
export FISION_TOKEN="put-a-long-token-here"
python -m fision_server.run          # listens on 0.0.0.0:8000
```

Full details, endpoints and how to expose it (Tailscale / Cloudflare Tunnel /
WireGuard) are in [`fision_server/README.md`](fision_server/README.md).

### 2. Desktop GUI (local / debug)

A tkinter GUI runs the pipeline directly on one file — **no library, no hashes,
no cache** (it reprocesses every time). Handy for quick local tests, debugging,
or trying a single song without setting up the server or the app.

```bash
python headless_fisionAI_gui.py
```

> Needs a graphical environment. A headless server (no desktop) can't open it.

### 3. Command line

```bash
python headless_fision_CLI.py <input_audio> <output_dir> [config.json]
```

### 4. Lyrics (Whisper, standalone)

Lyrics generation is opt-in and lives outside the main pipeline. Once a song
has been processed and `vocals.flac` exists, the same transcription used by
the server can be invoked manually:

```bash
python headless_fision_lyrics_CLI.py <vocals.flac> <output_dir> [language]
```

`language` is an ISO code (`en`, `es`, `fr`, ...). Omit it or pass `auto` to
let Whisper detect the language. The command writes `lyrics.json` (with
per-segment timestamps) into `<output_dir>`.

## Credits & licenses

- **Separation models** served via `audio-separator` (UVR / MDX / Demucs family).
- **beat_this** — Foscarin, Schlüter, Widmer (CPJKU, ISMIR 2024).
- **madmom** — CPJKU/OFAI (BSD 3-clause); used for its Dynamic Bayesian
  Network beat/downbeat tracker, invoked by beat_this's DBN postprocessor.
- **BTC** — Jonggwon Park et al., *A Bi-Directional Transformer for Musical
  Chord Recognition* (ISMIR 2019), MIT license; used via a compatibility fork.
- **Whisper** — OpenAI (MIT); used via `faster-whisper` (CTranslate2 engine,
  same weights, MIT).

Each model keeps its own license; check upstream before redistribution.
