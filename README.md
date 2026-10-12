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
  (170 chords), included as a git submodule. BTC labels audio frames of about
  0.09 s without knowledge of the beat grid, which in dense mixes produces
  chords that flicker within a beat or alternate between a chord and its
  seventh (`C`, `C7`, `C`). Fision synchronizes the result to the detected
  beats:
  1. Frame probabilities are averaged within each beat.
  2. A Viterbi pass over the beats picks the most likely chord sequence, with
     a cost for every change. Changes are cheaper on the downbeat and at
     half-bar, and much cheaper within a chord family (same root and triad:
     `C`, `C7`, `Cmaj7`, `C6`).
  3. Consecutive chords of the same family are merged and labeled with the
     member that accumulates the most probability.

  Chords therefore change only on beats. The strength is set with
  `FISION_CHORDS_*` variables (see `fision_server/README.md`);
  `FISION_CHORDS_SMOOTH=0` restores the raw BTC output.
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

# 2. Python 3.12 virtual environment
python3.12 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate

# 3. System deps (not pip)
sudo apt install ffmpeg python3-tk  # ffmpeg for audio; python3-tk for the GUI

# 4. PyTorch FIRST, with the CUDA index for your GPU (table below)
pip install torch==2.13.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu130

# 5. Everything else
pip install -r requirements.txt
```

### Choosing the PyTorch index

The index depends on the GPU generation and the NVIDIA driver installed.

| GPU                                             | Driver   | Index   |
|-------------------------------------------------|----------|---------|
| Turing and newer (RTX 20xx, 30xx, 40xx, 50xx)   | >= 580   | `cu130` |
| Maxwell, Pascal, Volta (GTX 9xx, GTX 10xx, V100), or an older driver | >= 560 | `cu126` |
| No NVIDIA GPU                                   | -        | `cpu`   |

`cu130` wheels ship kernels for `sm_75` to `sm_120` only. Check the driver with
`nvidia-smi` and the result with
`python -c "import torch; print(torch.cuda.get_arch_list())"`.
`torchaudio` stays at 2.11.0: no newer release exists, and it is only used by
`beat_this`.

All other dependencies are pinned in `requirements.txt` to the validated stack.
Whisper ships its own CUDA 12 libraries (`nvidia-cublas-cu12`,
`nvidia-cudnn-cu12`), independent of the PyTorch index.

### Model weights

The separation models (`audio-separator`), the Whisper checkpoint
(`faster-whisper`) and the `beat_this` checkpoints are downloaded automatically
on first use. The **BTC chord model** weights ship inside the submodule
(`BTC-ISMIR19/test/btc_model_large_voca.pt`), so a recursive clone already has
them.

The download location is set with `FISION_MODELS_DIR`. When it is defined, all
weights live under a single root:

| Subdirectory | Contents                  | Overridden by          |
|--------------|---------------------------|------------------------|
| `separator/` | `audio-separator` models  | `FISION_SEP_MODEL_DIR` |
| `hf/`        | Whisper (Hugging Face)    | `HF_HOME`              |
| `torch/`     | `beat_this` checkpoints   | `TORCH_HOME`           |

A specific variable, when set, takes precedence over the root. When
`FISION_MODELS_DIR` is not defined, each library keeps its usual default
(`~/.cache/audio-separator-models`, `~/.cache/huggingface`, `~/.cache/torch`).
Separation weights total several GB; a persistent disk is required, never a
RAM-backed `/tmp`.

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

## License

Fision is licensed under the
[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0). Running a
modified version as a network service requires making its complete source
code available to the users of that service.

The BTC submodule keeps its original MIT license.
