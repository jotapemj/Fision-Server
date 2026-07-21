# Fision Server

Self-hosted, multi-user server for audio stem separation and music analysis.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009688)
![PyTorch](https://img.shields.io/badge/ML-PyTorch-ee4c2c)

> This is the **server module** of Fision. For the processing pipeline, GUI, CLI,
> model downloads and installation, see the **root repository README**.

## Overview

Fision Server is the backend of Fision, a self-hosted alternative to cloud-based
stem-separation services. It receives audio from a client, runs the Fision
pipeline (stem separation, beat/downbeat and chord detection, cover art and
metronomes) and stores the results in a **per-user library** on the host, so they
can be downloaded again without reprocessing. All data stays on the machine that
runs the server.

## How it works

- **Token authentication.** Clients log in with a username and password and
  receive a signed token; every subsequent request carries it as
  `Authorization: Bearer <token>`. The acting user is always derived from the
  token, never from the URL or request body.
- **Non-blocking jobs.** The upload request does not wait for the long-running
  process (which avoids proxy timeouts such as Cloudflare's ~100s limit): it
  enqueues the work and returns a `job_id`. The client polls for status.
- **Single worker, one global queue.** The GPU processes one job at a time.
  Isolation applies to *storage*, not compute: all users share one queue, and the
  reported position is the true global position.
- **Per-user, content-addressed library.** Results live under
  `library/<user>/<sha256>/`. One audio file maps to one hash and one result
  *per user*; the same track uploaded by two accounts is stored independently,
  with no cross-talk and no overwrites.
- **Atomic results.** Processing happens in `stems_tmp/`; the swap into the live
  `stems/` and `result.zip` occurs only on success. A failed or cancelled
  reprocess never destroys the previous result.
- **Crash-safe queue.** The live queue is persisted to `queue.json`. On startup
  the server re-enqueues interrupted jobs (discarding their partial `stems_tmp/`),
  skips jobs that had already completed, and reconciles orphaned in-progress
  entries to their real state (ready when a result exists, otherwise error).
  Completed-job history is not persisted; after a restart the final state is read
  from `/library/{hash}`.

## On-disk layout

```
library/<user>/<hash>/
    input.<ext>     original audio (seed for reprocessing without re-uploading)
    meta.json       display name, current config, state, timestamps
    stems/          current separation
    result.zip      zip of the current separation

data/
    users.json      accounts: username + argon2 password hash + token epoch
    secret.key      token-signing key (auto-generated; deleting it logs everyone out)
    queue.json      live queue (queued + running), so jobs survive a restart
```

## Requirements

The heavy stack (PyTorch with the CUDA index, `audio-separator`, `beat_this`,
BTC, ffmpeg) is installed from the **root repository**. The server itself adds:

```
fastapi  uvicorn  python-multipart  pydantic  mutagen  argon2-cffi  PyJWT
```

## Quick start

```bash
export FISION_LIBRARY="/path/to/library"     # result storage (default: ./library)
export FISION_DATA="/path/to/data"           # accounts + keys (default: ./data)

python -m fision_server.useradd alice        # create the first account
python -m fision_server.run                  # start the server
# or: uvicorn fision_server.app:app --host 0.0.0.0 --port 8000
```

## Configuration

All configuration is read from environment variables.

| Variable                 | Default                  | Description                                        |
|--------------------------|--------------------------|----------------------------------------------------|
| `FISION_LIBRARY`         | `./library`              | Result storage root.                               |
| `FISION_DATA`            | `./data`                 | Accounts, signing key and persisted queue.         |
| `FISION_LOCAL_USER`      | `local`                  | Default owner for local GUI/CLI saves.             |
| `FISION_SECRET`          | *(auto-generated)*       | Token-signing key. Persisted to `data/secret.key`. |
| `FISION_TOKEN_TTL_HOURS` | `720`                    | Session lifetime (default 30 days).                |
| `FISION_LOG_LEVEL`       | `INFO`                   | Log verbosity.                                     |
| `FISION_HOST`            | `0.0.0.0`                | Bind address.                                      |
| `FISION_PORT`            | `8000`                   | Bind port.                                         |
| `FISION_MAX_UPLOAD_MB`   | `200`                    | Maximum upload size.                               |
| `FISION_MAX_AVATAR_MB`   | `2`                      | Maximum avatar upload size.                        |
| `FISION_GPU_MIN_VRAM_MB` | `4096`                   | Minimum free VRAM required to start a CUDA job.    |
| `FISION_GPU_POLL_SEC`    | `30`                     | Poll interval while a job waits for free VRAM.     |
| `FISION_GPU_WAIT_TIMEOUT_SEC` | `600`               | Hard timeout waiting for VRAM; aborts the job.     |
| `FISION_WHISPER_MODEL`   | `large-v3`               | faster-whisper checkpoint used for lyrics.         |
| `FISION_WHISPER_DEVICE`  | `cuda`                   | Device for Whisper inference (`cuda` or `cpu`).    |
| `FISION_WHISPER_COMPUTE` | `float16`                | Whisper compute type (`float16`/`int8`/...).       |
| `FISION_WHISPER_MIN_VRAM_MB` | `3072`               | Minimum free VRAM required to start a lyrics job.  |
| `FISION_CLI`             | `headless_fision_CLI.py` | Command used to invoke the pipeline.               |
| `FISION_LYRICS_CLI`      | `headless_fision_lyrics_CLI.py` | Command used to invoke the Whisper CLI.     |


## Authentication and user management

There is **no public registration**. Accounts are created from the host shell,
and credentials are distributed out of band.

```bash
python -m fision_server.useradd    <username>   # create account (prompts for a password)
python -m fision_server.userpasswd <username>   # reset a password (invalidates old sessions)
python -m fision_server.userdel    <username>   # delete an account (library folder is left intact)
```

Client login flow:

1. `POST /login` with `{username, password}` returns `{token, user}`.
2. Every other request sends `Authorization: Bearer <token>`.
3. `POST /auth/change-password` returns a fresh token (changing the password
   invalidates the previous one via a per-account token epoch).

Usernames are slugs matching `[a-z0-9_]` (2-32 characters); the username also
serves as the library folder name. Passwords are hashed with **argon2** and are
never stored in clear text.

## API

All routes except `/health` and `/login` require `Authorization: Bearer <token>`.

| Method | Path                          | Description                                     |
|--------|-------------------------------|-------------------------------------------------|
| POST   | `/login`                      | Exchange username + password for a token.       |
| POST   | `/auth/change-password`       | Change the password; returns a fresh token.     |
| GET    | `/auth/avatar`                | Serve the caller's avatar image. 404 if unset.  |
| PUT    | `/auth/avatar`                | Upload avatar (multipart `file`, JPEG/PNG/WebP).|
| DELETE | `/auth/avatar`                | Delete the caller's avatar.                     |
| GET    | `/health`                     | Connectivity probe (no token).                  |
| GET    | `/server/info`                | GPU capabilities of the host (cached).          |
| POST   | `/jobs`                       | Upload audio + `config_json`; enqueue or cache. |
| POST   | `/library/{hash}/reprocess`   | Reprocess the stored input with a new config.   |
| GET    | `/jobs`                       | Live queue for the caller (global position).    |
| GET    | `/jobs/{job_id}`              | Job status (owner only).                         |
| DELETE | `/jobs/{job_id}`              | Cancel a queued or running job (owner only).     |
| GET    | `/library`                    | List the caller's songs.                         |
| GET    | `/library/{hash}`             | Metadata for one song.                           |
| GET    | `/library/{hash}/zip`         | Download the current `result.zip`.               |
| GET    | `/library/{hash}/stems/{file}`| Serve a stems file (cover art or audio).         |
| DELETE | `/library/{hash}`             | Delete the entry.                                |
| POST   | `/library/{hash}/lyrics`      | Enqueue a Whisper transcription over `vocals.flac`.|
| GET    | `/library/{hash}/lyrics`      | Serve `lyrics.json` (404 if not generated yet).   |
| DELETE | `/library/{hash}/lyrics`      | Remove the current `lyrics.json`.                 |

### Pipeline contract

The server invokes the pipeline as a subprocess:

```
<FISION_CLI> <input_audio> <output_stems_dir> <config.json>
```

It writes the stems (`.flac`) into `output_stems_dir` together with `beats.txt`,
`downbeats.txt`, `chords*.txt`, the cover `artcover.png` and the metronomes, and
returns exit code 0 on success. All temporary output must stay inside the
provided directory so interrupted runs can be cleaned up safely.

## Hardware detection and GPU gating

The server is the single source of truth for GPU availability. At startup it
queries `nvidia-smi` once and caches the result; clients read that snapshot from
`GET /server/info` and decide which `device` to request per job.

```json
GET /server/info
{
  "gpu": true,
  "gpu_name": "NVIDIA GeForce RTX 3060",
  "vram_total_mb": 12288,
  "driver": "550.54.15"
}
```

When `gpu` is `false`, the other fields are empty. `nvidia-smi` is queried
without loading PyTorch, so the endpoint is cheap and does not warm up CUDA.

Jobs may include `"device": "cuda"` in the config. Before executing such a job
the worker checks live free VRAM against `FISION_GPU_MIN_VRAM_MB` (default
4096 MB). If not enough is free, the job moves to the `waiting_gpu` state,
free VRAM is polled every `FISION_GPU_POLL_SEC` seconds (default 30), and the
job resumes as soon as the threshold is met. If `FISION_GPU_WAIT_TIMEOUT_SEC`
elapses (default 600), the job is aborted with `GPU ocupada`. If the host has
no GPU at all, a job that requested `cuda` is silently downgraded to `cpu` so
processing still completes.

Job states exposed to clients: `queued`, `running`, `waiting_gpu`, `done`,
`error`, `cancelled`. A `waiting_gpu` job counts as position `0` in the queue
and can be cancelled the same way as a running one. Jobs share a `kind` field
internally (`separation` or `lyrics`); it is not exposed in the API but
drives the worker's dispatch and VRAM threshold.

## Avatars

Each account may upload one profile image (JPEG, PNG or WebP; SVG is rejected).
The client is responsible for cropping and resizing; the server stores the file
verbatim under `data/avatars/<user>.<ext>`, where the extension is detected
from the file's magic bytes rather than trusted from the request. Uploading a
new avatar replaces the previous one, even across formats.

```
GET    /auth/avatar          -> image/*, 404 if none
PUT    /auth/avatar          -> multipart 'file'; 413 if >FISION_MAX_AVATAR_MB, 400 if not JPEG/PNG/WebP
DELETE /auth/avatar          -> removes the avatar
```

## Lyrics

Lyrics transcription is **opt-in and off the main pipeline**. When a song is
uploaded, the server runs stem separation, beats and chords — but not lyrics.
The client requests transcription only when the user asks for it (typically
from the lyrics tab of the player).

The endpoint runs Whisper (via `faster-whisper`) over the isolated
`vocals.flac` stem. It requires the main job to have completed first; if
`vocals.flac` is not yet on disk, the endpoint returns **409 Conflict** and
the client must retry once the song is ready.

```
POST /library/{hash}/lyrics
Body: {"language": "en"}   -> ISO code, or null for auto-detect
Return: {"job_id": "...", "hash": "...", "estado": "queued"}
```

Whisper supports 99 languages; some common ISO codes: `en`, `es`, `fr`, `de`,
`it`, `pt`, `nl`, `ru`, `ja`, `ko`, `zh`, `ar`, `tr`, `pl`, `sv`, `hi`. Set
`language` to `null` (or omit it) to let Whisper detect it.

Once the job finishes, `lyrics.json` is written into the song's `stems/`
folder next to the other outputs and is also included in the next
`result.zip`. The client can fetch it directly:

```
GET /library/{hash}/lyrics    -> application/json
DELETE /library/{hash}/lyrics -> removes it (useful for a clean retry)
```

`lyrics.json` schema:

```json
{
  "language": "en",
  "language_probability": 0.98,
  "duration": 234.5,
  "model": "large-v3",
  "segments": [
    {"id": 0, "start": 12.34, "end": 15.67, "text": "I feel the rain on my skin"},
    {"id": 1, "start": 15.89, "end": 18.42, "text": "No one else can feel it for me"}
  ]
}
```

Timestamps are at the **segment (phrase) level**, meant to drive line-by-line
karaoke rendering. Word-level timestamps are not emitted by default; adding
them later would populate an optional `words` field inside each segment
without breaking the current schema.

**Reprocessing a song does not lose the lyrics.** When
`POST /library/{hash}/reprocess` runs, the worker copies any existing
`lyrics.json` into the atomic `stems_tmp/` folder before the commit swap, so
the lyrics survive re-separation.

**GPU gating.** Lyrics jobs share the single global queue but use a separate
VRAM threshold (`FISION_WHISPER_MIN_VRAM_MB`, default 3072 MB) since Whisper
large-v3 in FP16 needs about 3 GB — less than a full separation. The model
is loaded and released per job so it does not permanently occupy VRAM.

**Client contract (Android).**

1. Open the lyrics tab. `GET /library/{hash}/lyrics`:
   - 200 → parse the JSON and render the karaoke view.
   - 404 → show "not generated yet, press Generate".
2. On Generate, let the user pick a language (or "auto"), then
   `POST /library/{hash}/lyrics {"language": "en"}`.
3. Poll `GET /jobs/{job_id}` as with any other job. On `done`, `GET
   /library/{hash}/lyrics` and cache the response (`?v=<updated_at>` to
   invalidate after a `DELETE` + regenerate).

## Logging

Logs are written to stdout, ready for journald or Docker. Two grep-friendly
prefixes are emitted:

```
... AUDIT  server.start gpu=True gpu_name=NVIDIA GeForce RTX 3060 vram_total_mb=12288
... AUDIT  user.create user=alice
... AUDIT  login.success user=alice
... AUDIT  login.fail user=alice reason=bad_credentials
... AUDIT  password_change.success user=alice
... AUDIT  avatar.set user=alice
... AUDIT  avatar.delete user=alice
... AUDIT  queue.recover recovered=1 swept=2
... AUDIT  job.waiting_gpu job_id=ab12cd34ef56
... AUDIT  job.gpu_ready   job_id=ab12cd34ef56
... AUDIT  job.gpu_timeout job_id=ab12cd34ef56
... AUDIT  job.gpu_fallback job_id=ab12cd34ef56 reason=no_gpu
... ACCESS user=alice POST /jobs 200 143ms
```

To follow account events on a running service:

```bash
journalctl -u fision -f | grep AUDIT
```

## Deployment

The token layer applies regardless of how the server is exposed; treat it as the
inner wall and the network layer as the outer one.

- **LAN only** - nothing to expose.
- **Tailscale** - private mesh address, no public exposure (recommended).
- **Cloudflare Tunnel** - public URL, optionally behind Cloudflare Access.
- **WireGuard** - a single UDP port, invisible to unsolicited traffic.

## Roadmap

- Per-user storage quotas and library cleanup (size caps, pruning old entries).
- Hashing of the decoded audio (identical audio in different containers maps to
  the same hash).
- Optional invite codes so an account owner sets their own password.

## License

See the root repository.
