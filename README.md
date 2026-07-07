# Fision

Self-hosted AI music processing. Feed it a song and Fision separates it into
stems (vocals, bass, drums, guitar, piano, winds…), detects **beats** and
**downbeats**, extracts **chords**, and renders **metronomes** and cover art —
all on your own machine. No cloud, no accounts, your music stays yours.

The philosophy is the same as [Immich](https://immich.app): your data, your
server. A mobile app talks to a small server on your PC; the heavy GPU work
never leaves home.

> **Status:** in active development. Runs, but rough edges remain — see each
> module's "pending" notes.

## Architecture

Three pieces, two repos:

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
- **Android app** — the client. Lives in its own repo:
  [Fision Android](https://github.com/jotapemj/Fision-Android) *(coming soon)*.

## What the pipeline does

- **Stem separation** (a fixed model chain, GPU): a RoFormer splits
  vocals / instrumental, HTDemucs `ft` gives bass/drums, HTDemucs `6s` gives
  guitar/piano, an MDX23C drum-separator can split the kit, and a wind model
  pulls brass/woodwinds. `other` is always derived by subtraction.
- **Beats & downbeats** — [beat_this](https://github.com/CPJKU/beat_this)
  (ISMIR 2024), on CPU, in parallel with the GPU separation.
- **Chords** — [BTC](https://github.com/jotapemj/BTC-ISMIR19) (a Transformer
  chord-recognition model), large vocabulary (170 chords), as a git submodule.

## Install

```bash
# 1. Clone WITH submodules (BTC lives as a submodule)
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
first run. The **BTC chord model** weights are not in git (too large): place
`btc_model_large_voca.pt` under `BTC-ISMIR19/test/`. See the BTC repo for the
download link.

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

## Credits & licenses

- **Separation models** served via
  [audio-separator](https://github.com/nomadkaraoke/python-audio-separator)
  (UVR / MDX / Demucs family).
- **beat_this** — Foscarin, Schlüter, Widmer (CPJKU, ISMIR 2024).
- **BTC** — Jonggwon Park et al., *A Bi-Directional Transformer for Musical
  Chord Recognition* (ISMIR 2019), MIT license; used via a
  [compatibility fork](https://github.com/jotapemj/BTC-ISMIR19).

Each model keeps its own license; check upstream before redistribution.
