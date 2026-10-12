# Fision Server

Self-hosted, multi-user server for audio stem separation and music analysis.

![Python](https://img.shields.io/badge/python-3.12-blue)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009688)
![PyTorch](https://img.shields.io/badge/ML-PyTorch-ee4c2c)

> This is the **server module** of Fision. For the processing pipeline, GUI, CLI,
> model downloads and installation, see the **root repository README**.

## Overview

Fision Server is the backend of Fision, a self-hosted alternative to cloud-based
stem-separation services. It receives audio from a client, runs the Fision
pipeline (stem separation, beat/downbeat and chord detection, cover art and
metronomes) and stores the results in a **per-user library** on the host, so they
can be downloaded again without reprocessing. On top of the library it offers
opt-in lyrics transcription, per-user playlists and profile avatars. All data
stays on the machine that runs the server.

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
- **Bounded jobs.** Each job runs in its own process group. Cancelling a job or
  exceeding `FISION_JOB_TIMEOUT_SEC` / `FISION_LYRICS_TIMEOUT_SEC` stops the
  whole group, including the separator, so no process keeps holding the GPU.
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

library/.incoming/  uploads in progress; same filesystem as the library so the
                    final move is an atomic rename. Emptied on startup.

data/
    users.json      accounts: username + argon2 password hash + token epoch
    secret.key      token-signing key (auto-generated; deleting it logs everyone out)
    queue.json      live queue (queued + running), so jobs survive a restart
    avatars/        one profile image per account
    playlists/      <user>.json with the account's playlists; covers in <user>/
```

## Requirements

The heavy stack (PyTorch with the CUDA index, `audio-separator`, `beat_this`,
BTC, ffmpeg) is installed from the **root repository**. The server itself adds
the following, pinned in the root `requirements.txt`:

```
fastapi  uvicorn  python-multipart  mutagen  argon2-cffi  PyJWT
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
| `FISION_LOGIN_MAX_FAILS` | `5`                      | Failed logins per IP and username before `429`. `0` disables the limit. |
| `FISION_LOGIN_WINDOW_SEC` | `300`                   | Window for counting failed logins.                 |
| `FISION_LOG_LEVEL`       | `INFO`                   | Log verbosity.                                     |
| `FISION_HOST`            | `0.0.0.0`                | Bind address.                                      |
| `FISION_PORT`            | `8000`                   | Bind port.                                         |
| `FISION_MAX_UPLOAD_MB`   | `200`                    | Maximum upload size.                               |
| `FISION_MAX_AVATAR_MB`   | `2`                      | Maximum avatar upload size.                        |
| `FISION_GPU_MIN_VRAM_MB` | `4096`                   | Minimum free VRAM required to start a CUDA job.    |
| `FISION_GPU_POLL_SEC`    | `30`                     | Poll interval while a job waits for free VRAM.     |
| `FISION_GPU_WAIT_TIMEOUT_SEC` | `600`               | Hard timeout waiting for VRAM; aborts the job.     |
| `FISION_JOB_TIMEOUT_SEC` | `2400`                   | Maximum run time of a separation job, excluding the wait for VRAM. On expiry the whole process group is killed and the job ends with `timeout`. `0` disables it. |
| `FISION_LYRICS_TIMEOUT_SEC` | `900`                 | Same limit for lyrics jobs.                        |
| `FISION_WHISPER_MODEL`   | `large-v3`               | faster-whisper checkpoint used for lyrics.         |
| `FISION_WHISPER_DEVICE`  | `cuda`                   | Device for Whisper inference (`cuda` or `cpu`).    |
| `FISION_WHISPER_COMPUTE` | `float16`                | Whisper compute type (`float16`/`int8`/...).       |
| `FISION_WHISPER_MIN_VRAM_MB` | `3072`               | Minimum free VRAM required to start a lyrics job.  |
| `FISION_WHISPER_BATCH_SIZE` | `0`                   | If `>=2`, enable `BatchedInferencePipeline` (parallel chunks; higher VRAM, faster). |
| `FISION_SEP_BATCH_SIZE`  | `24`                     | MDXC RoFormer batch size (audio-separator).        |
| `FISION_SEP_SEGMENT_SIZE`| `512`                    | MDXC segment size.                                 |
| `FISION_SEP_OVERLAP`     | `4`                      | MDXC overlap.                                      |
| `FISION_SEP_AUTOCAST`    | `1`                      | Enable autocast in audio-separator (`0` to disable). |
| `FISION_DEMUCS_SHIFTS`   | `4`                      | HTDemucs random shifts (quality, not VRAM).        |
| `FISION_DEMUCS_OVERLAP`  | `0.5`                    | HTDemucs segment overlap.                          |
| `FISION_CHORDS_SMOOTH`   | `1`                      | Synchronize chords to beats (`0` = raw BTC output). |
| `FISION_CHORDS_CHANGE_COST` | `1.0`                 | Cost of a chord change. Higher = fewer, longer chords. |
| `FISION_CHORDS_FAMILY_COST` | `0.2`                 | Cost of a change within the same root and triad (`C` to `C7`). |
| `FISION_CHORDS_OFFBEAT_MULT` | `2.0`                | Multiplier on change costs away from the downbeat and half-bar. |
| `FISION_MODELS_DIR`      | *(unset)*                | Single root for all model weights: `separator/`, `hf/` and `torch/` subdirectories. See *Model weights* in the main README. |
| `FISION_SEP_MODEL_DIR`   | `$FISION_MODELS_DIR/separator` or `~/.cache/audio-separator-models` | Directory for separation model weights. Downloaded on first use and reused afterwards. |
| `FISION_CLI`             | `headless_fision_CLI.py` | Command used to invoke the pipeline.               |
| `FISION_LYRICS_CLI`      | `headless_fision_lyrics_CLI.py` | Command used to invoke the Whisper CLI.     |

### Scaling VRAM usage

The defaults are conservative (Pascal / GTX 1050). On modern cards with
more VRAM you can raise throughput by tuning the audio-separator MDXC
batch and segment size, and by enabling Whisper batched inference:

```
# Ampere+, ~16 GB VRAM
FISION_SEP_BATCH_SIZE=48
FISION_SEP_SEGMENT_SIZE=768
FISION_WHISPER_BATCH_SIZE=8
```

`FISION_WHISPER_BATCH_SIZE >= 2` wraps the model with
`BatchedInferencePipeline`; when unset (or `0`/`1`) Whisper runs in the
classic single-batch mode. `FISION_SEP_AUTOCAST=0` forces fp32 in the
separator (recommended on Pascal, where fp16 is slower).


## Authentication and user management

There is **no public registration**. Accounts are created from the host shell,
and credentials are distributed out of band.

```bash
python -m fision_server.useradd    <username>   # create account (prompts for a password)
python -m fision_server.userpasswd <username>   # reset a password (invalidates old sessions)
python -m fision_server.userdel    <username>   # delete an account (library folder is left intact)
```

### Regenerating chords

After changing the chord settings, existing songs can be updated without
separating them again. The command reruns only the chord model over the stored
input and the saved beats, rewrites `chords.txt` and `chords_basic.txt`,
rebuilds `result.zip` and updates `updated_at`. It takes a few seconds per
song.

```bash
python -m fision_server.rechords <username> --all
python -m fision_server.rechords <username> <hash> [<hash> ...]
```

Run it from the repository root with the same environment as the service
(`FISION_LIBRARY`, `FISION_MODELS_DIR`, `FISION_CHORDS_*`). Clients holding a
local copy of a song must download it again to see the new chords.

Client login flow:

1. `POST /login` with `{username, password}` returns `{token, user}`.
2. Every other request sends `Authorization: Bearer <token>`.
3. `POST /auth/change-password` returns a fresh token (changing the password
   invalidates the previous one via a per-account token epoch).

Usernames are slugs matching `[a-z0-9_]` (2-32 characters); the username also
serves as the library folder name. Passwords are hashed with **argon2** and are
never stored in clear text.

Failed logins are limited per client IP and username: after
`FISION_LOGIN_MAX_FAILS` failures within `FISION_LOGIN_WINDOW_SEC`, `/login`
answers `429` with a `Retry-After` header until the oldest failure expires. A
successful login resets the counter. The counter is held in memory and resets
when the server restarts. Unknown usernames take the same verification time as
existing ones, so response timing does not reveal which accounts exist.

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
| GET    | `/playlists`                  | List the caller's playlists (summary).            |
| POST   | `/playlists`                  | Create a playlist.                                |
| GET    | `/playlists/{id}`             | One playlist with its ordered song hashes.        |
| PUT    | `/playlists/{id}`             | Replace title and songs (also used to reorder).   |
| DELETE | `/playlists/{id}`             | Delete a playlist and its cover.                  |
| GET/PUT/DELETE | `/playlists/{id}/cover` | Playlist cover, same rules as the avatar.       |

Request validation on `POST /jobs` and `POST /library/{hash}/reprocess`:

- `config_json` must be a JSON object; anything else returns `400`.
- The uploaded file name must end in `.mp3`, `.wav`, `.flac`, `.m4a`, `.aac`,
  `.ogg`, `.opus`, `.aiff`, `.aif` or `.wma`; otherwise `415` is returned and
  nothing is stored. Clients must send the original file name, extension
  included.
- Files above `FISION_MAX_UPLOAD_MB` return `413`.

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
  "driver": "550.54.15",
  "vram_free_mb": 10840,
  "vram_min_mb": 4096,
  "version": "1.9.0"
}
```

When `gpu` is `false`, the other GPU fields are empty and `vram_free_mb` is
`0`. `gpu_name`, `vram_total_mb` and `driver` come from the startup cache;
`vram_free_mb` is queried live on every request. `vram_min_mb` is the value of
`FISION_GPU_MIN_VRAM_MB`, the threshold used by the job gate described below.
Clients can compare both fields to warn the user before submitting a CUDA job
(`vram_free_mb < vram_min_mb` means the job would enter `waiting_gpu`).
`nvidia-smi` is queried without loading PyTorch, so the endpoint is cheap and
does not warm up CUDA.
The `version` field carries the server's own release (see
[Versioning](#versioning)).

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

## Playlists

Playlists belong to the account and are stored under `data/playlists/`. Each
one holds an ordered list of song hashes; the order is the playback order.

```json
{
  "id": "a1b2c3d4e5f6",
  "title": "Friday rehearsal",
  "count": 2,
  "has_cover": true,
  "created_at": "2026-10-11T13:00:00+00:00",
  "updated_at": "2026-10-11T13:05:00+00:00",
  "songs": ["<sha256>", "<sha256>"]
}
```

`GET /playlists` returns the same objects without `songs`, newest change first.
`POST` and `PUT` take `{"title": "...", "songs": ["<hash>", ...]}`; `PUT`
replaces both fields, so reordering is a `PUT` with the new order. When two
devices edit the same playlist, the last write wins.

Rules (violations return `400` with a stable `code` next to `detail`):

| `code`           | Cause                                                   |
|------------------|---------------------------------------------------------|
| `title_length`   | Title empty or longer than 100 characters.              |
| `song_not_found` | Hash not present in the caller's server library.        |
| `song_duplicate` | The same song appears twice.                            |
| `song_limit`     | More than 2000 songs.                                   |
| `playlist_limit` | The account already has 200 playlists.                  |

```json
{"detail": "Cancion repetida en la playlist", "code": "song_duplicate"}
```

`detail` is a human-readable message in Spanish and may change; clients should
localize from `code` and fall back to `detail` only for unknown codes. Local-only
songs cannot be added.

Deleting a song with `DELETE /library/{hash}` also removes it from every
playlist of that account. Covers follow the avatar rules: JPEG, PNG or WebP
detected by magic bytes, up to `FISION_MAX_AVATAR_MB`, cropped by the client.

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
    {
      "id": 0,
      "start": 12.34,
      "end": 15.67,
      "text": "I feel the rain on my skin",
      "words": [
        {"start": 12.34, "end": 12.55, "text": "I"},
        {"start": 12.55, "end": 12.83, "text": "feel"},
        {"start": 12.83, "end": 13.02, "text": "the"},
        {"start": 13.02, "end": 13.51, "text": "rain"}
      ]
    }
  ]
}
```

Each segment carries both a phrase-level `start`/`end`/`text` (for line-by-line
karaoke rendering) and a `words` array with per-word timestamps (for optional
word-by-word highlighting). Long Whisper segments are split at silences
between consecutive words (default 2 s) so each phrase stays a usable size.
Word items are always present, but the array may be empty in the rare case
where Whisper does not emit word alignments for a phrase — clients should
gracefully fall back to the segment-level timing in that case.

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
... AUDIT  login.blocked user=alice ip=100.64.0.7
... AUDIT  password_change.success user=alice
... AUDIT  avatar.set user=alice
... AUDIT  avatar.delete user=alice
... AUDIT  playlist.create user=alice id=a1b2c3d4e5f6 songs=12
... AUDIT  playlist.song_removed user=alice hash=9f86d081884c playlists=2
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

## Versioning

The server follows [SemVer](https://semver.org/) (`MAJOR.MINOR.PATCH`):

- **MAJOR** - the client contract changes in an incompatible way (endpoint
  removed, field renamed or reshaped, request/response format broken).
- **MINOR** - new backwards-compatible feature (new endpoint, new optional
  field or env, expanded behavior that keeps old clients working).
- **PATCH** - bug fix, dependency bump, refactor or performance change with
  no visible effect on the contract.

Version bumps map from the commit prefix (Conventional Commits): `feat:` ->
MINOR, `feat!:` or `BREAKING CHANGE:` in the body -> MAJOR, `fix:` /
`deps:` / `perf:` / `revert:` -> PATCH. The current version lives in
`fision_server/__init__.py` (`__version__`) and is served through
`GET /server/info["version"]`. Release notes live in the repository's
GitHub Releases page.

Clients should cache the version on login and use it to detect
incompatibilities: mismatching MAJOR means the client must upgrade before
new jobs will be accepted.

## Deployment

### systemd

`deploy/fision.service.example` is a template unit. Copy it to
`/etc/systemd/system/fision.service`, adjust user, paths and environment, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fision
```

Keep `FISION_DATA`, `FISION_LIBRARY` and `FISION_MODELS_DIR` outside the
repository checkout, and list their mount points in `RequiresMountsFor=` so the
service does not start before the disks are mounted.

### Network exposure

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

AGPL-3.0. See [LICENSE](../LICENSE) in the repository root.
