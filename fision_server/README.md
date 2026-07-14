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
| `FISION_CLI`             | `headless_fision_CLI.py` | Command used to invoke the pipeline.               |


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
| GET    | `/health`                     | Connectivity probe (no token).                  |
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

### Pipeline contract

The server invokes the pipeline as a subprocess:

```
<FISION_CLI> <input_audio> <output_stems_dir> <config.json>
```

It writes the stems (`.flac`) into `output_stems_dir` together with `beats.txt`,
`downbeats.txt`, `chords*.txt`, the cover `artcover.png` and the metronomes, and
returns exit code 0 on success. All temporary output must stay inside the
provided directory so interrupted runs can be cleaned up safely.

## Logging

Logs are written to stdout, ready for journald or Docker. Two grep-friendly
prefixes are emitted:

```
... AUDIT  user.create user=alice
... AUDIT  login.success user=alice
... AUDIT  login.fail user=alice reason=bad_credentials
... AUDIT  password_change.success user=alice
... AUDIT  queue.recover recovered=1 swept=2
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
