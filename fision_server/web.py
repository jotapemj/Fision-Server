"""
Cliente web (Fision-AppWeb), servido en /app.

Cada version del server fija una version de la web (URL + sha256 de
`fision-web.zip` de su Release): server y web van siempre alineados. La
imagen Docker la trae dentro; en nativo la baja `prefetch`:

    python -m fision_server.web        (o python -m fision_server.prefetch)

Va en <repo>/webapp/ con `.fision-web.json` (version + sha) para saber que
hay instalado. Solo stdlib: el Dockerfile lo ejecuta sin config (que crearia
secret.key dentro de la imagen).
"""
import json
import shutil
import hashlib
import zipfile
import urllib.request
from pathlib import Path

WEB_VERSION = "0.33.0"
WEB_URL = ("https://github.com/jotapemj/Fision-AppWeb/releases/download/"
           f"v{WEB_VERSION}/fision-web.zip")
WEB_SHA256 = "680ce9cffecf8b9611cfd3bcc8a8579a5fb09d10486c9842b40638b2c3960d2f"

WEB_DIR = Path(__file__).resolve().parent.parent / "webapp"
_MARKER = ".fision-web.json"


def installed():
    """{'version', 'sha256'} de lo instalado, o None."""
    try:
        return json.loads((WEB_DIR / _MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_current():
    return (installed() or {}).get("sha256") == WEB_SHA256


def _safe_extract(zf, dest):
    """Ficheros sueltos bajo dest, sin rutas que escapen (zip-slip)."""
    root = dest.resolve()
    for info in zf.infolist():
        target = (dest / info.filename).resolve()
        if root not in target.parents:
            raise ValueError(f"unexpected path in web zip: {info.filename!r}")
    zf.extractall(dest)


def fetch(url=WEB_URL, sha256=WEB_SHA256, version=WEB_VERSION):
    """Baja e instala la web fijada. False si ya estaba."""
    if is_current():
        return False
    parent = WEB_DIR.parent
    part = parent / ".webapp.part"
    work = parent / ".webapp.tmp"
    old = parent / ".webapp.old"
    for p in (work, old):
        shutil.rmtree(p, ignore_errors=True)
    try:
        digest = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as f:
            while chunk := r.read(1 << 20):
                digest.update(chunk)
                f.write(chunk)
        if digest.hexdigest() != sha256:
            raise ValueError("web zip checksum mismatch")
        work.mkdir()
        with zipfile.ZipFile(part) as zf:
            _safe_extract(zf, work)
        if not (work / "index.html").is_file():
            raise ValueError("web zip without index.html")
        (work / _MARKER).write_text(json.dumps({"version": version, "sha256": sha256}),
                                    encoding="utf-8")
        # Cambio casi atomico: la vieja sirve hasta el ultimo momento
        if WEB_DIR.exists():
            WEB_DIR.rename(old)
        work.rename(WEB_DIR)
        return True
    finally:
        part.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)


if __name__ == "__main__":
    print(f"web app {WEB_VERSION}: "
          + ("installed" if fetch() else "already present") + f" in {WEB_DIR}")
