# FisionServer reference

Configuration, HTTP API, administration and deployment of the server. For the
overview, installation and credits see the [README](../README.md).

- [Overview](#overview)
- [How it works](#how-it-works)
- [On-disk layout](#on-disk-layout)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Authentication and user management](#authentication-and-user-management)
- [API](#api)
- [Web app](#web-app)
- [Diagnostics](#diagnostics)
- [Hardware detection and GPU gating](#hardware-detection-and-gpu-gating)
- [Avatars](#avatars)
- [Playlists](#playlists)
- [Lyrics](#lyrics)
- [Logging](#logging)
- [Versioning](#versioning)
- [Deployment](#deployment)
- [Roadmap](#roadmap)

## Overview

FisionServer is the backend of Fision, a self-hosted alternative to cloud-based
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
    meta.json       file name, current config, state, timestamps
    stems/          current separation
    result.zip      zip of the current separation

library/.demo/<hash>/  demo song template, same layout as an entry plus
                    NOTICE.txt; copied to every new account (see below)

library/.incoming/  uploads in progress; same filesystem as the library so the
                    final move is an atomic rename. Emptied on startup.

data/
    users.json      accounts: username + argon2 password hash + token epoch
    users.lock      lock file serialising writes to users.json (server and CLI)
    secret.key      token-signing key (auto-generated; deleting it logs everyone out)
    queue.json      live queue (queued + running), so jobs survive a restart
    avatars/        one profile image per account
    playlists/      <user>.json with the account's playlists; covers in <user>/

webapp/             web client pinned by this server version (see Web app);
                    inside the repository checkout, or in the image with Docker
```

## Requirements

The heavy stack (PyTorch with the CUDA index, `audio-separator`, `beat_this`,
BTC, ffmpeg) is installed as described in the [README](../README.md#install).
The server itself adds the following, pinned in the root `requirements.txt`
(transitive dependencies in `constraints.txt`):

```
fastapi  uvicorn  python-multipart  mutagen  argon2-cffi  PyJWT
```

## Quick start

```bash
export FISION_LIBRARY="/path/to/library"     # result storage (default: ./library)
export FISION_DATA="/path/to/data"           # accounts + keys (default: ./data)

python -m fision_server.doctor               # check the installation
python -m fision_server.prefetch             # download every model up front
python -m fision_server.useradd alice        # first account (becomes administrator)
python -m fision_server.run                  # start the server
# or: uvicorn fision_server.app:app --host 0.0.0.0 --port 8000
```

## Configuration

All configuration is read from environment variables.

| Variable                 | Default                  | Description                                        |
|--------------------------|--------------------------|----------------------------------------------------|
| `FISION_LIBRARY`         | `./library`              | Result storage root.                               |
| `FISION_DATA`            | `./data`                 | Accounts, signing key and persisted queue.         |
| `FISION_LOCAL_USER`      | `local`                  | Default owner for local CLI saves.                 |
| `FISION_SECRET`          | *(auto-generated)*       | Token-signing key. Persisted to `data/secret.key`. |
| `FISION_TOKEN_TTL_HOURS` | `720`                    | Session lifetime (default 30 days).                |
| `FISION_LOGIN_MAX_FAILS` | `5`                      | Failed logins per IP and username before `429`. `0` disables the limit. |
| `FISION_LOGIN_MAX_FAILS_IP` | `20`                  | Failed logins per IP, across all usernames, before `429`. `0` disables the limit. |
| `FISION_LOGIN_WINDOW_SEC` | `300`                   | Window for counting failed logins.                 |
| `FISION_LOG_LEVEL`       | `INFO`                   | Log verbosity.                                     |
| `FISION_ADMIN_ALLOW`     | loopback, private ranges, Tailscale | Comma-separated networks allowed to reach `/admin`. Other clients get `403` even with an administrator token. |
| `FISION_HOST`            | `0.0.0.0`                | Bind address.                                      |
| `FORWARDED_ALLOW_IPS`    | `127.0.0.1`              | Read by uvicorn: addresses trusted to send `X-Forwarded-For`, usually the reverse proxy. See [Reverse proxy](#reverse-proxy-and-https). |
| `FISION_PORT`            | `8000`                   | Bind port.                                         |
| `FISION_MAX_UPLOAD_MB`   | `200`                    | Maximum upload size. Enforced before the request body is read. |
| `FISION_MAX_AVATAR_MB`   | `2`                      | Maximum avatar and playlist cover upload size.     |
| `FISION_API_DOCS`        | `0`                      | `1` serves the interactive API documentation at `/docs`, `/redoc` and `/openapi.json`. Disabled by default, since it lists every route. |
| `FISION_GPU_MIN_VRAM_MB` | `4096`                   | Minimum free VRAM required to start a CUDA job.    |
| `FISION_GPU_POLL_SEC`    | `30`                     | Poll interval while a job waits for free VRAM.     |
| `FISION_GPU_WAIT_TIMEOUT_SEC` | `600`               | Hard timeout waiting for VRAM; aborts the job.     |
| `FISION_MAX_JOBS_PER_USER` | `3`                   | Live jobs (queued or running, separation and lyrics) per account. Beyond it, new jobs get `429` with `code: "job_limit"`. `0` disables the limit. |
| `FISION_JOB_TIMEOUT_SEC` | `2400`                   | Maximum run time of a separation job, excluding the wait for VRAM. On expiry the whole process group is killed and the job ends with `timeout`. `0` disables it. |
| `FISION_LYRICS_TIMEOUT_SEC` | `900`                 | Same limit for lyrics jobs.                        |
| `FISION_WHISPER_MODEL`   | `large-v2`               | faster-whisper checkpoint used for lyrics. `large-v2` hallucinates less than `large-v3` on sung vocals. |
| `FISION_WHISPER_DEVICE`  | `cuda`                   | Device for Whisper inference (`cuda` or `cpu`).    |
| `FISION_WHISPER_COMPUTE` | `float16`                | Whisper compute type (`float16`/`int8`/...).       |
| `FISION_WHISPER_MIN_VRAM_MB` | `3072`               | Minimum free VRAM required to start a lyrics job.  |
| `FISION_WHISPER_BATCH_SIZE` | `0`                   | If `>=2`, enable `BatchedInferencePipeline` (parallel chunks; higher VRAM, faster). |
| `FISION_SEP_BATCH_SIZE`  | `24`                     | Batch size of the MDX23C drum separator (split drums only). The RoFormer models process one chunk at a time and ignore it. |
| `FISION_SEP_SEGMENT_SIZE`| `512`                    | Currently without effect: every MDXC model uses the segment size from its own configuration. |
| `FISION_SEP_OVERLAP`     | `4`                      | MDXC overlap (RoFormer vocals and karaoke models). Higher is slower. |
| `FISION_SEP_AUTOCAST`    | `1`                      | Enable autocast in audio-separator (`0` to disable). |
| `FISION_DEMUCS_SHIFTS`   | `4`                      | HTDemucs random shifts (quality, not VRAM).        |
| `FISION_DEMUCS_OVERLAP`  | `0.5`                    | HTDemucs segment overlap.                          |
| `FISION_CHORDS_SMOOTH`   | `1`                      | Synchronize chords to beats (`0` = raw BTC output). |
| `FISION_CHORDS_CHANGE_COST` | `1.0`                 | Cost of a chord change. Higher = fewer, longer chords. |
| `FISION_CHORDS_FAMILY_COST` | `0.2`                 | Cost of a change within the same root and triad (`C` to `C7`). |
| `FISION_CHORDS_OFFBEAT_MULT` | `2.0`                | Multiplier on change costs away from the downbeat and half-bar. |
| `FISION_MODELS_DIR`      | *(unset)*                | Single root for all model weights: `separator/`, `hf/` and `torch/` subdirectories. See [Model weights](../README.md#model-weights). |
| `FISION_SEP_MODEL_DIR`   | `$FISION_MODELS_DIR/separator` or `~/.cache/audio-separator-models` | Directory for separation model weights. Downloaded on first use and reused afterwards. |
| `FISION_CLI`             | `headless_fision_CLI.py` | Command used to invoke the pipeline.               |
| `FISION_LYRICS_CLI`      | `headless_fision_lyrics_CLI.py` | Command used to invoke the Whisper CLI.     |

### Changing the configuration

The server reads these variables once, at startup. They are shown read-only
in the admin panel; to change one, set it where the service is defined and
restart the server:

- **systemd:** add or edit an `Environment=` line in the unit (or a drop-in
  created with `systemctl edit fision`), then run `systemctl daemon-reload`
  and `systemctl restart fision`. See `deploy/fision.service.example`.
- **Docker / Compose:** edit `.env` and run `docker compose up -d`, which
  recreates the container. See [Docker](#docker).
- **Manual start:** export the variable in the shell before
  `python -m fision_server.run`.

Path variables take effect for new jobs only after the restart; files already
in the old locations are not moved automatically.

### Scaling VRAM usage

Separation is bound by compute, not by memory: each model processes the
track chunk by chunk, so a separation job peaks at about 3.5 GB of VRAM
even on a 24 GB card, and more VRAM does not make it faster. Measured on
an RTX 3090 for a 4:45 track: 169 s in total, of which 103 s are
HTDemucs (4 models x `FISION_DEMUCS_SHIFTS`). Lowering the shifts is the
main speed lever, at some cost in quality.

Whisper does benefit from free VRAM through batched inference:

```
# Ampere+, >= 8 GB VRAM
FISION_WHISPER_BATCH_SIZE=16
```

`FISION_WHISPER_BATCH_SIZE >= 2` wraps the model with
`BatchedInferencePipeline`; when unset (or `0`/`1`) Whisper runs in the
classic single-batch mode. `FISION_SEP_AUTOCAST=0` forces fp32 in the
separator (recommended on Pascal, where fp16 is slower). On Pascal, also set
`FISION_WHISPER_COMPUTE=int8`: Whisper has no fast fp16 there and would fall
back to fp32.


## Authentication and user management

There is **no public registration**. Accounts are created from the host shell,
and credentials are distributed out of band.

```bash
python -m fision_server.useradd    <username> [--admin] [--no-demo]  # create account (prompts for a password; Enter generates one)
python -m fision_server.usermod    <username> --admin|--no-admin  # grant or remove the admin role
python -m fision_server.userpasswd <username>   # reset a password (invalidates old sessions)
python -m fision_server.userdel    <username> [--purge]  # delete an account; --purge also removes its library, playlists and avatar
```

These commands read the same environment variables as the server. Run them
with the service's `FISION_DATA` and `FISION_LIBRARY` (for systemd:
`systemctl show fision -p Environment`); otherwise they act on the default
`./data` and `./library`. `useradd` and `userdel` print the paths they use.

At the password prompt of `useradd`, pressing Enter generates a random
password and prints it once. Without a terminal, such as from a script, the
password is read from standard input: an empty line also generates one. With
Docker, add `-T` so the input reaches the command:

```bash
echo | docker compose exec -T fision python -m fision_server.useradd alice
```

The first account created on a fresh installation is an administrator even
without `--admin`, so the admin panel always has an owner. The last
administrator cannot be deleted or demoted. If no administrator exists at
startup, the server logs `AUDIT admin.missing` with the command to fix it.

Without `--purge`, `userdel` keeps the account's library, playlists and avatar
on disk, and a new account with the same name would inherit them; `useradd`
lists such leftovers and asks before continuing. `userdel --purge` refuses to
run while the account has live jobs, since a command-line tool cannot cancel
them; cancel them from the admin panel first.

### Demo song

New accounts, created with `useradd` or from the admin panel, receive a copy
of every entry in `library/.demo/` (the demo song). `useradd --no-demo` skips
it; existing accounts are not changed. Without a template nothing is copied.

`prefetch` downloads the official template ("Airplane Mode" by Josh Woodward,
CC BY 4.0) from the GitHub release when `library/.demo/` is empty, and builds
its `result.zip`. Any processed entry can serve as a template instead: copy
its `<hash>/` folder into `library/.demo/`.

Audio files and `result.zip` are hard-linked when the template is on the same
filesystem as the library, so each copy is instant and takes no extra space;
small files are copied. The copy's `created_at` and `updated_at` are the
account's creation time. The user can delete it like any other song.

### Admin panel

Administrators can manage the server from a browser at `http://<host>:8000/admin`,
signing in with their usual credentials. The panel covers:

- **Overview:** version, uptime, GPU and free VRAM (with a warning below
  `FISION_GPU_MIN_VRAM_MB`), library and model disk usage, accounts and queue.
- **Users:** create accounts (the generated password is shown once), reset a
  password, close all sessions of an account, grant or remove the admin role,
  and delete accounts. Deleting can optionally remove the user's library,
  playlists and avatar from disk.
- **User libraries:** metadata only (title, artist, state, size, date). Each
  song opens a technical summary: duration, original format, separated
  tracks, whether lyrics, chords, metronome and cover were generated, lyrics
  language, tempo and processing device. Audio, covers and lyrics are not
  reachable from the panel.
- **Jobs:** every queued and running job across users, with cancellation.
- **Activity:** audit events since the last start. The full history stays in
  the system log.
- **Settings:** effective configuration, read-only. Values come from the
  environment; secrets are never shown. Changing them means editing the
  service definition and restarting.

Access needs both an administrator account and a client address inside
`FISION_ADMIN_ALLOW`. The default covers loopback, private networks and
Tailscale, so the panel is not reachable from the internet even if the API is.
Behind a reverse proxy, set `FORWARDED_ALLOW_IPS` to the proxy's address or
block `/admin` at the proxy; otherwise every request appears to come from the
proxy (see [Reverse proxy and HTTPS](#reverse-proxy-and-https)).

The panel is available in English, Spanish, Portuguese, Italian, French,
German, Russian, Chinese, Catalan and Ukrainian. It follows the system language and
falls back to English; a different language can be chosen from the account
menu or the sign-in screen and is remembered by the browser. Chinese text uses
the system's CJK font (Noto Sans has no CJK glyphs). It ships with its own copy of Noto Sans (SIL Open Font License, see
`fision_server/admin_ui/static/NotoSans-OFL.txt`) and inline icons, so it
works without internet access.

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

Chord names in `chords.txt` use the MusGlyphs font notation: a `;` raises only
the next character or ligature, so every superscript digit, accidental,
parenthesis or word carries its own (`G;maj;7`, `C;1;3`, `Ebm;7;b;5`,
`G;sus;4`); the root, `m`, `dim0` and the bass after `/` stay on the baseline.
Songs processed before 1.11.5 wrote `Gmaj7` as `G;7` and multi-digit or
altered extensions with a single `;`; regenerating their chords with the
command above fixes them.

Client login flow:

1. `POST /login` with `{username, password}` returns
   `{token, user, expires_at}` (`expires_at` is ISO 8601, UTC).
2. Every other request sends `Authorization: Bearer <token>`.
3. `POST /auth/refresh` with a valid token returns a new
   `{token, user, expires_at}` with a full `FISION_TOKEN_TTL_HOURS` lifetime.
   Clients should call it when the app opens (sliding session): an account
   used at least once per TTL never has to log in again.
4. `POST /auth/change-password` returns a fresh token (changing the password
   invalidates the previous one via a per-account token epoch).
5. Any `401` on an authenticated route means the token is no longer valid
   (expired, revoked or account deleted). Clients should discard the token and
   show the login screen; retrying with the same token will keep failing.
   A `401` on `/login` means wrong credentials.

Usernames are slugs matching `[a-z0-9_]` (2-32 characters); the username also
serves as the library folder name. Passwords are hashed with **argon2** and are
never stored in clear text.

Failed logins are limited per client IP and username: after
`FISION_LOGIN_MAX_FAILS` failures within `FISION_LOGIN_WINDOW_SEC`, `/login`
answers `429` with a `Retry-After` header until the oldest failure expires. A
second counter limits each client IP to `FISION_LOGIN_MAX_FAILS_IP` failures
across all usernames, so one address cannot try a password against many
accounts. A successful login resets the per-username counter only. At most
four password verifications run at the same time; further logins wait their
turn instead of exhausting CPU and memory. The counter is held in memory and resets
when the server restarts. Unknown usernames take the same verification time as
existing ones, so response timing does not reveal which accounts exist.

## API

All routes except `/health`, `/login` and the web app files under `/app/`
require `Authorization: Bearer <token>`.

Request bodies are checked before they are read. Upload routes (`POST /jobs`,
`POST /library/{hash}/reprocess`, `PUT /auth/avatar`,
`PUT /playlists/{id}/cover`) answer `401` without reading the body when the
token is missing or invalid, and `413` as soon as the body exceeds the limit
(`FISION_MAX_UPLOAD_MB` for audio, `FISION_MAX_AVATAR_MB` for images, plus
1 MiB for the form fields), whether the size is declared in `Content-Length`
or only becomes apparent while streaming. Any other request body is limited to
1 MiB. Partial uploads are buffered on the library disk, never in `/tmp`.

`POST /jobs`, `POST /library/{hash}/reprocess` and `POST /library/{hash}/lyrics`
answer `429` with `{"detail": ..., "code": "job_limit"}` when the account
already has `FISION_MAX_JOBS_PER_USER` live jobs. A cache hit on `POST /jobs`
does not count, since it creates no job.

Uploading a song that is already being processed returns the existing job when
the configuration is the same, and `409` with `code: "job_running"` and its
`job_id` when it differs; `POST /library/{hash}/reprocess` answers the same
`409` while a job for that song is live. An empty file gets `400` with
`code: "audio_empty"`, and audio shorter than 15 s gets `400` with
`code: "audio_too_short"`: the separation models fail on clips shorter than
their processing window (about 11 s). Files whose duration cannot be read from
the header are accepted and left to the pipeline.

| Method | Path                          | Description                                     |
|--------|-------------------------------|-------------------------------------------------|
| POST   | `/login`                      | Exchange username + password for a token.       |
| POST   | `/auth/refresh`               | Exchange a valid token for a new one (full TTL).|
| POST   | `/auth/change-password`       | Change the password; returns a fresh token.     |
| GET    | `/auth/avatar`                | Serve the caller's avatar image. 404 if unset.  |
| PUT    | `/auth/avatar`                | Upload avatar (multipart `file`, JPEG/PNG/WebP).|
| DELETE | `/auth/avatar`                | Delete the caller's avatar.                     |
| GET    | `/health`                     | Connectivity probe (no token).                  |
| GET    | `/app/...`                    | [Web app](#web-app) files (no token).           |
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
| DELETE | `/library/{hash}`             | Delete the entry; cancels its live jobs first.   |
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

### Uploading a song

`POST /jobs` takes a multipart form with two fields: `file`, the audio with
its original file name, and `config_json`, a JSON object as text (default
`{}`). `POST /library/{hash}/reprocess` takes only `config_json`.

```bash
curl -H "Authorization: Bearer $TOKEN"      -F "file=@song.mp3"      -F 'config_json={"device": "cuda", "sep": {"mode": "classic"}}'      http://<host>:8000/jobs
```

Top-level keys of `config_json`:

| Key      | Values            | Default | Description |
|----------|-------------------|---------|-------------|
| `device` | `cuda`, `cpu`     | `cpu`   | Where the job runs. Without `cuda` the job runs on the CPU even when the server has a GPU; on a server without one, `cuda` falls back to the CPU. |
| `sep`    | object            | -       | Separation options, below. An object without `sep` is read as the separation options themselves, and runs on the CPU. |

Keys of `sep`:

| Key | Values | Default | Description |
|-----|--------|---------|-------------|
| `mode` | `classic`, `personalized` | `personalized` | `classic`: fixed set of stems. `personalized`: the stems chosen in `stems`. |
| `preset` | `2stem` | - | `classic` only. `2stem` gives `vocals` and `instrumental`; anything else gives `vocals`, `bass`, `drums` and `other`. |
| `vocals` | `single`, `split` | `single` | `split` separates lead vocals (`vocals.flac`) from backing vocals (`vocals2.flac`). |
| `stems.<name>.enabled` | `true`, `false` | `false` | `personalized` only. `<name>` is `bass`, `drums`, `guitar`, `piano` or `brass` (saved as `winds.flac`). |
| `stems.drums.variant` | `full`, `split` | `full` | `split` divides the drums into `kick`, `snare`, `toms`, `hh`, `ride`, `crash` and `perc`. |

In `personalized` mode, whatever the chosen stems leave out is saved as
`other.flac`; with no stem enabled the result is `vocals` and `instrumental`.
Every job, whatever its configuration, also produces beats, downbeats,
chords, metronomes, tags and cover art. Unknown keys are ignored but stored:
a cache hit on `POST /jobs` needs a stored song with exactly the same
`config_json`, and a different one reprocesses the song.

Example used by the Android app (lead and backing vocals, bass, drums and
winds):

```json
{"device": "cuda",
 "sep": {"mode": "personalized", "vocals": "split",
         "stems": {"bass": {"enabled": true}, "drums": {"enabled": true, "variant": "full"},
                   "guitar": {"enabled": false}, "piano": {"enabled": false},
                   "brass": {"enabled": true}}}}
```

### Jobs and library entries

`POST /jobs` returns `{"hash", "job_id", "state", "cached"}`; on a cache hit
there is no `job_id` and `state` is `ready`. `POST /library/{hash}/reprocess`
and `POST /library/{hash}/lyrics` return `{"hash", "job_id", "state"}`.

```json
GET /jobs/{job_id}
{
  "job_id": "3f9c2a7d1b0e",
  "hash": "<sha256>",
  "state": "error",
  "position": null,
  "error": "GPU busy: not enough free VRAM after 600 s",
  "error_code": "gpu_busy"
}
```

`state` is one of `queued`, `running`, `waiting_gpu`, `done`, `error` or
`cancelled`. `position` is the global queue position (1-based) of a queued
job, `0` while it runs or waits for the GPU, and `null` once it has finished.
`GET /jobs` lists the caller's live jobs as
`{"job_id", "hash", "filename", "state", "position", "kind"}`, where `kind` is
`separation` or `lyrics`.

`error` is a human-readable English message and may change between releases;
clients should branch on `error_code`, which is stable. Both are empty unless
the job failed or was cancelled.

| `error_code`      | Cause                                                        |
|-------------------|--------------------------------------------------------------|
| `cancelled`       | The job was cancelled.                                       |
| `gpu_busy`        | Not enough free VRAM within `FISION_GPU_WAIT_TIMEOUT_SEC`.   |
| `timeout`         | `FISION_JOB_TIMEOUT_SEC` or `FISION_LYRICS_TIMEOUT_SEC` elapsed. |
| `pipeline_failed` | The separation pipeline exited with an error.               |
| `lyrics_failed`   | The lyrics transcription exited with an error.              |
| `input_missing`   | The stored input audio of the song is gone.                  |
| `vocals_missing`  | `vocals.flac` is gone; the song must be processed first.     |
| `internal`        | Any other server error.                                      |

A library entry (`GET /library`, `GET /library/{hash}`):

```json
{
  "hash": "<sha256>",
  "filename": "song.mp3",
  "title": "Song",
  "artist": "Artist",
  "cover_url": "/library/<sha256>/stems/artcover.png",
  "state": "ready",
  "config": {"sep": {"mode": "classic"}, "device": "cuda"},
  "created_at": "2026-10-25T10:00:00+00:00",
  "updated_at": "2026-10-25T10:04:12+00:00"
}
```

`state` is `ready`, `processing`, `error` or `cancelled`. A failed or
cancelled reprocess keeps the previous result, so the entry returns to `ready`
when one exists. `DELETE /library/{hash}` returns `{"hash", "deleted": true}`.

### Pipeline contract

The server invokes the pipeline as a subprocess:

```
<FISION_CLI> <input_audio> <output_stems_dir> <config.json>
```

It writes the stems (`.flac`) into `output_stems_dir` together with `beats.txt`,
`downbeats.txt`, `chords*.txt`, the cover `artcover.png` and the metronomes, and
returns exit code 0 on success. All temporary output must stay inside the
provided directory so interrupted runs can be cleaned up safely.

## Web app

The server hosts the [Fision web client](https://github.com/jotapemj/Fision-AppWeb)
at `/app/`, on the same origin as the API. Each server version pins one web
release (version and SHA-256 of its `fision-web.zip`, in
`fision_server/web.py`), so server and web are always updated together. The
Docker image includes it; a native installation downloads it with
`prefetch` into `webapp/` in the repository checkout. Without it, `/app/`
answers `404`.

The browser features the client needs (audio worklets, shared memory for the
multithreaded audio engine, service worker, installation as an app) work only
in a secure context: HTTPS, or `localhost` on the server machine itself. Over
plain HTTP, such as `http://192.168.1.10:8000/app/` on the local network or a
Tailscale address without HTTPS, the browser disables them and the web app
cannot play audio. The Android app is not affected. See
[Reverse proxy and HTTPS](#reverse-proxy-and-https).

Responses under `/app/` carry `Cross-Origin-Opener-Policy: same-origin` and
`Cross-Origin-Embedder-Policy: require-corp`, required for the multithreaded
audio engine. Files under `assets/` have content hashes in their names and are
cached for a year; everything else, the service worker included, is
revalidated on every load, so a server update reaches the browser on the next
visit.

## Diagnostics

Two commands help set up and troubleshoot an installation. Run them with the
same environment as the service; otherwise they look at `./data`, `./library`
and `~/.cache` instead of the real folders (see
[Authentication and user management](#authentication-and-user-management)).

```bash
python -m fision_server.doctor      # check the installation
python -m fision_server.prefetch    # download every model up front
```

`doctor` prints one line per check, marked `OK`, `WARN` (works, worth a look)
or `FAIL` (will not work), with the fix below each problem, and exits with
`1` when anything fails. It covers:

- Python version and `ffmpeg`.
- The data, library and model folders: writable, and free space.
- The GPU as seen by `nvidia-smi`, PyTorch and Whisper, with the codes of
  [Hardware detection](#hardware-detection-and-gpu-gating); a PyTorch built
  for a CUDA version other than 12, and leftovers such as `onnxruntime-gpu`
  or two `nvidia-cudnn` packages.
- The BTC weights (git submodule) and the metronome sample.
- The separation, `beat_this` and Whisper weights that are not downloaded
  yet.
- Accounts: at least one user and one administrator, and whether a demo song
  is available for new accounts.
- The web app: installed, and the version this server expects.

`prefetch` downloads the separation models, the `beat_this` checkpoints and
the Whisper model set in `FISION_WHISPER_MODEL` (several GB) into the model
folders, the [demo song](#demo-song) if `library/.demo/` is empty, and the
[web app](#web-app) pinned by this version if it is missing or outdated.
Without it, the first job downloads them and fails if the network drops.
Files already present are skipped, so it can be run again after an
interrupted download or a model change.

## Hardware detection and GPU gating

The server is the single source of truth for GPU availability. At startup it
queries `nvidia-smi` once and caches the result; clients read that snapshot from
`GET /server/info` and decide which `device` to request per job.

Only the first GPU is used and measured. On a machine with several, the
others stay idle and their VRAM is not reported.

```json
GET /server/info
{
  "gpu": true,
  "gpu_name": "NVIDIA GeForce RTX 3060",
  "vram_total_mb": 12288,
  "driver": "550.54.15",
  "gpu_usable": true,
  "gpu_problem": "",
  "vram_free_mb": 10840,
  "vram_min_mb": 4096,
  "version": "2.0.0"
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

`nvidia-smi` only proves that a card is present. A driver that is too old, a
CPU build of PyTorch or a GPU outside the architectures of the installed wheel
would still send every job to the CPU. Right after startup the server
therefore runs a probe in a separate process that imports PyTorch and
CTranslate2 (Whisper) and checks that both can use CUDA. It takes a few
seconds; until it finishes, `gpu_usable` is `null` and the `nvidia-smi` result
applies. `gpu_problem` is empty or one of these stable codes:

- `torch_error`: PyTorch cannot be imported.
- `torch_no_cuda`: `nvidia-smi` sees a GPU but PyTorch does not (driver too
  old, or the CPU build of PyTorch).
- `arch_unsupported`: the GPU is not among the architectures of the installed
  PyTorch (for example a Pascal card with the `cu129` index).
- `whisper_no_cuda`: separation uses the GPU but Whisper cannot; lyrics run on
  the CPU. `gpu_usable` stays `true`.
- `smi_missing`: PyTorch sees CUDA but `nvidia-smi` is not available, so free
  VRAM cannot be measured (typical of a container without the `utility`
  capability).

When `gpu_usable` is `false`, jobs that request `cuda` run on the CPU, the
admin panel shows a warning and the `gpu.unusable` event is logged. Run
`python -m fision_server.doctor` (see [Diagnostics](#diagnostics)) for the
details and the fix.

Jobs may include `"device": "cuda"` in the config. Before executing such a job
the worker checks live free VRAM against `FISION_GPU_MIN_VRAM_MB` (default
4096 MB). If not enough is free, the job moves to the `waiting_gpu` state,
free VRAM is polled every `FISION_GPU_POLL_SEC` seconds (default 30), and the
job resumes as soon as the threshold is met. If `FISION_GPU_WAIT_TIMEOUT_SEC`
elapses (default 600), the job fails with `error_code: "gpu_busy"`. If the host has
no usable GPU, a job that requested `cuda` is downgraded to `cpu` so
processing still completes; the `job.gpu_fallback` event records the reason
(`no_gpu` or the `gpu_problem` code).

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
{"detail": "Song repeated in the playlist", "code": "song_duplicate"}
```

`detail` is a human-readable message in English and may change; clients should
localize from `code` and fall back to `detail` only for unknown codes. Local-only
songs cannot be added.

Deleting a song with `DELETE /library/{hash}` also removes it from every
playlist of that account. Queued and running jobs for that song are cancelled
first, and the request waits until the running one has stopped; if it does not
stop within 60 s the response is `409` and nothing is deleted. Covers follow the avatar rules: JPEG, PNG or WebP
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
Return: {"job_id": "...", "hash": "...", "state": "queued"}
```

Whisper supports 99 languages; some common ISO codes: `en`, `es`, `fr`, `de`,
`it`, `pt`, `nl`, `ru`, `ja`, `ko`, `zh`, `ar`, `tr`, `pl`, `sv`, `hi`. Set
`language` to `null`, `"auto"` (or omit it) to let Whisper detect it. Any
other value must be a two- or three-letter ISO code; otherwise the response is
`400`.

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
  "model": "large-v2",
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
Whisper writes each sung line with an initial capital and often without
punctuation; when lines are sung back to back there is no silence between
them, so a phrase is also split before a capitalised word whose preceding
word is a held note (at least 0.35 s). Proper nouns usually follow a short
word ("de", "in", "my") and stay on the same line. Known subtitle
boilerplate that Whisper produces over silence ("Amara.org", "Thanks for
watching", "Suscríbete") is dropped, and so are zero-length repetitions of
the previous word, which Whisper emits when it falls into a repetition loop.
When the first word of a line was aligned to the end of the previous line
(a capitalised word at the end of a line followed, after a pause, by a line
starting in lowercase), it is moved to the start of the next line and the
preceding word keeps the held note. Lines starting with a hyphenated
continuation ("-o-us") are joined to the previous one. Phrases longer than
8 s are split again at the most likely line boundary (punctuation or a
capitalised word), never inside a hyphenated word and never leaving fewer
than three words on either side. Words that Whisper writes in full capitals (shouted passages)
are lowercased, keeping the capital at the start of the line. Whisper runs
without an `initial_prompt`: a context prompt was being transcribed as lyrics
in passages without voice.
Word items are always present, but the array may be empty in the rare case
where Whisper does not emit word alignments for a phrase — clients should
gracefully fall back to the segment-level timing in that case.

**Reprocessing a song does not lose the lyrics.** When
`POST /library/{hash}/reprocess` runs, the worker copies any existing
`lyrics.json` into the atomic `stems_tmp/` folder before the commit swap, so
the lyrics survive re-separation.

**GPU gating.** Lyrics jobs share the single global queue but use a separate
VRAM threshold (`FISION_WHISPER_MIN_VRAM_MB`, default 3072 MB) since Whisper
large-v2 in FP16 needs about 3 GB — less than a full separation. The model
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
... AUDIT  gpu.check usable=True torch=2.13.0+cu129 cuda=12.9 cudnn=92000 capability=8.6
... AUDIT  gpu.unusable problem=torch_no_cuda
... AUDIT  user.create user=alice
... AUDIT  user.demo user=alice songs=1 by=cli
... AUDIT  login.success user=alice
... AUDIT  login.fail user=alice reason=bad_credentials
... AUDIT  login.blocked user=alice ip=100.64.0.7 scope=user
... AUDIT  upload.reject reason=missing path=/jobs user=-
... AUDIT  upload.too_large path=/jobs bytes=524288000 limit=210763776
... AUDIT  token.refresh user=alice
... AUDIT  auth.reject reason=expired user=alice
... AUDIT  user.role user=alice role=admin by=cli
... AUDIT  user.delete user=bob by=alice purge=True
... AUDIT  sessions.revoke user=bob by=alice
... AUDIT  admin.reject reason=network ip=203.0.113.9
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
... AUDIT  job.limit user=alice limit=3
... ACCESS user=alice POST /jobs 200 143ms
```

`auth.reject` records why an authenticated request got `401`: `missing` (no
token), `expired`, `revoked` (password changed), `unknown_user` (account
deleted) or `bad_token` (bad signature or malformed). For `expired` the user is
still taken from the token, since its signature is valid.

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

### Docker

`compose.yaml` runs the published image; installation and updates are
described in the [README](../README.md#docker). Inside the container the
paths are fixed (`/data`, `/library`, `/models`) and mapped to host folders
through `FISION_DATA_PATH`, `FISION_LIBRARY_PATH` and `FISION_MODELS_PATH`.
Every other variable in [Configuration](#configuration) goes in `.env`;
`.env.example` lists the usual ones. Changes apply with `docker compose up -d`
(`docker compose restart` keeps the old values).

- **Paths shown:** the admin panel, `doctor`, `prefetch` and the account CLIs
  show the host side of each volume next to the container path. In the
  panel, paths and address appear under the `.env` variable that changes
  them (`FISION_DATA_PATH`, `FISION_BIND`...). Compose passes the host side
  as `FISION_DOCKER_*` variables, for display only.
- **Permissions:** a data or library folder the container cannot write to
  stops the server at startup with the `chown` command that fixes it.

- **Commands:** the CLIs run inside the container, for example
  `docker compose exec fision python -m fision_server.useradd alice`.
- **Address:** `FISION_BIND` and `FISION_PUBLISH_PORT` choose where the port
  is published on the host (`127.0.0.1:8000` by default). `FISION_HOST` and
  `FISION_PORT` are fixed inside the container (`0.0.0.0:8000`) and ignored
  in `.env`. Docker adds its own
  firewall rules, so a host firewall such as UFW does not filter a port
  published on `0.0.0.0`.
- **Client addresses:** with the port published on loopback, connections
  made from the host itself reach the server from the Docker bridge gateway,
  a private address inside `FISION_ADMIN_ALLOW`; published on the Tailscale
  address, they come from that address. Publishing only on loopback or the
  Tailscale address keeps the admin panel off other networks.
- **Web app:** included in the image; nothing to download.
- **User:** the image runs as UID/GID 1000, and the host folders must be
  writable by it. `FISION_UID` and `FISION_GID` change it when building the
  image locally.
- **Logs:** `docker compose logs fision`, in UTC unless `TZ` is set.
- **Without a GPU:** `docker compose -f compose.yaml -f compose.cpu.yaml up -d`
  uses the CPU image and drops the GPU reservation.

### Network exposure

The token layer applies regardless of how the server is exposed; treat it as the
inner wall and the network layer as the outer one.

> [!WARNING]
> The server speaks plain HTTP. With the default `FISION_HOST=0.0.0.0` it also
> listens on the local network, where passwords and tokens travel unencrypted.
> Tailscale encrypts its own traffic; to accept connections only through it,
> set `FISION_HOST` to the machine's Tailscale address (`tailscale ip -4`).

- **LAN only** - nothing to expose.
- **Tailscale** - private mesh address, no public exposure (recommended).
- **Cloudflare Tunnel** - public URL, optionally behind Cloudflare Access. The
  free and Pro plans cap request bodies at 100 MB, below the default
  `FISION_MAX_UPLOAD_MB`, and Cloudflare's terms restrict serving large audio
  files; check both before relying on it. When `cloudflared` runs on another
  host or in a container, its requests come from a private address inside
  `FISION_ADMIN_ALLOW`: block `/admin` in the tunnel configuration.
- **WireGuard** - a single UDP port, invisible to unsolicited traffic.

### Reverse proxy and HTTPS

The server speaks plain HTTP; the web app needs HTTPS. On a Tailscale host,
`tailscale serve` adds HTTPS with a valid certificate for the machine's
`*.ts.net` name, reachable only from the tailnet:

```bash
sudo tailscale serve --bg http://<address>:8000   # the address the server listens on
```

The web app is then at `https://<machine>.<tailnet>.ts.net/app/`.

Without HTTPS, an SSH tunnel also works, since the browser treats `localhost`
as secure:

```bash
ssh -L 8000:<address>:8000 <user>@<server>    # then open http://localhost:8000/app/
```

A proxy hides the client address: every request appears to come from the
proxy, which affects the login limits (all clients share one), the logs and
the admin panel network check. Set `FORWARDED_ALLOW_IPS` to the address the
proxy connects from, and uvicorn takes the client address from
`X-Forwarded-For` instead. Trust only that address: any client it trusts can
claim any address. The default, `127.0.0.1`, already covers a proxy on the
same host in a native installation. With Docker, connections from the proxy
come from the published address (`FISION_BIND`, for `tailscale serve` on the
same host) or from the bridge gateway when published on loopback; put that
address in `.env` and apply it with `docker compose up -d`.

## Roadmap

- Per-user storage quotas and library cleanup (size caps, pruning old entries).
- Hashing of the decoded audio (identical audio in different containers maps to
  the same hash).
- Optional invite codes so an account owner sets their own password.

## License

AGPL-3.0. See [LICENSE](../LICENSE) and the [README](../README.md#license).
