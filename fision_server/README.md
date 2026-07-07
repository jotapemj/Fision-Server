# Fision · Server

> Server module of Fision. For the full project (pipeline, GUI, CLI,
> installation and models), see the **root README**.

Self-hosted server that receives audio from the mobile app, processes it with
the Fision pipeline (stem separation, beat/downbeat and chord detection, cover
art and metronomes) and stores the results in a "library" on your PC, so you can
download them again without reprocessing.

## Idea

- The HTTP request **does not wait** for the long process (avoids Cloudflare's
  ~100s timeout): it enqueues and returns a `job_id`. The phone polls the status.
- **One worker** processes one at a time (the GPU won't do two at once).
- **Library by hash**: `library/<sha256>/`. One audio = one hash = one result.
  Reprocessing with a different config **overrides** it (the last one wins).
- **Atomic**: work happens in `stems_tmp/` and the swap only happens if it
  succeeds. A failed or cancelled reprocess does **not** destroy what you had.

## On-disk layout

```
library/<hash>/
    input.<ext>     original audio (seed to reprocess without re-uploading)
    meta.json       real name, current config, state, date
    stems/          current separation
    result.zip      zip of the current separation
```

## Running

Dependencies are installed from the **root repo** (the server shares its
environment with the pipeline: torch, audio-separator, beat_this, BTC). Follow
the root README to install PyTorch (CUDA index), ffmpeg and `requirements.txt`.

```bash
export FISION_TOKEN="put-a-long-token-here"       # Bearer the app must send
export FISION_LIBRARY="/path/to/library"          # storage (default: ./library)
# FISION_CLI is optional: it already defaults to headless_fision_CLI.py
# export FISION_CLI="python headless_fision_CLI.py"

python -m fision_server.run
# or: uvicorn fision_server.app:app --host 0.0.0.0 --port 8000
```

### CLI contract (what the orchestrator must fulfil)

The server invokes it as a subprocess:

```
<FISION_CLI> <input_audio> <output_stems_dir> <config.json>
```

It leaves the stems (`.flac`) in `output_stems_dir` along with `beats.txt`,
`downbeats.txt`, `chords*.txt`, the cover `artcover.png` and the metronomes, and
returns exit code 0 on success. (`config.json` carries the same dict the GUI
already produces.)

## Endpoints

| Method | Path                          | What it does                                 |
|--------|-------------------------------|----------------------------------------------|
| POST   | `/jobs`                       | upload audio + `config_json` -> enqueue/cache |
| POST   | `/library/{hash}/reprocess`   | reprocess the stored input with a new config |
| GET    | `/jobs/{job_id}`              | job status                                   |
| DELETE | `/jobs/{job_id}`              | cancel (queued or running)                   |
| GET    | `/library`                    | list of songs ("my cloud")                   |
| GET    | `/library/{hash}`             | metadata of one song                         |
| GET    | `/library/{hash}/zip`         | download the current `result.zip`            |
| DELETE | `/library/{hash}`             | delete the whole entry                       |

All require `Authorization: Bearer <FISION_TOKEN>` (if the token is empty,
open mode, for local testing only).

## Exposing it (pick ONE way, documented in the repo)

- **LAN only**: nothing to open. Home use.
- **Tailscale** (recommended, simple): `http://yourpc:8000`. Zero public exposure.
- **Cloudflare Tunnel**: public URL. Add Cloudflare Access + the Bearer token.
- **WireGuard**: full control, you open a UDP port (silent to strangers).

The Bearer token goes **always**, whatever it sits behind. Defense in depth.

## Pending / v2 ideas

- Library cleanup (size cap, delete old entries).
- Persist the queue on disk (in-flight jobs currently live in memory).
- Hash the decoded audio (same audio as mp3/wav = same hash).