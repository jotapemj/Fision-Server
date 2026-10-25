"""
Regenera SOLO los acordes de canciones ya procesadas (sin volver a separar):

    python -m fision_server.rechords <user> --all
    python -m fision_server.rechords <user> <hash> [<hash> ...]

Corre BTC sobre input.* con los beats ya guardados, reescribe chords.txt /
chords_basic.txt y rehace result.zip. Util tras cambiar el suavizado
(FISION_CHORDS_*). Ejecutar desde la raiz del repo, con las mismas envs que
el servicio (FISION_LIBRARY, FISION_MODELS_DIR...). Unos segundos por cancion.
"""
import os
import sys

from . import library, log


def rechord(lib, h):
    from headless_fisionAI import run_btc, analyze_chords_file   # import pesado

    meta = lib.read_meta(h)
    inp = lib.input_path(h)
    stems = lib.stems_dir(h)
    if not meta or meta.get("state") != "ready" or inp is None:
        return "no ready result"
    if not (stems / "beats.txt").exists():
        return "no beats.txt"

    run_btc(str(inp), str(stems))
    analyze_chords_file(str(stems))
    tmp = lib.entry_dir(h) / "result.zip.tmp"
    library.build_zip(stems, tmp)
    os.replace(tmp, lib.zip_path(h))
    meta["updated_at"] = library._now()          # el cliente ve que cambio
    lib.write_meta(h, meta)
    return None


def main():
    if len(sys.argv) < 3:
        print("usage: python -m fision_server.rechords <user> --all | <hash> ...")
        sys.exit(1)
    user, targets = sys.argv[1], sys.argv[2:]
    try:
        lib = library.for_user(user)
    except ValueError as e:
        print(f"error: {e}")
        sys.exit(1)

    if targets == ["--all"]:
        targets = [m["hash"] for m in lib.list_entries() if m.get("hash")]

    ok = fail = 0
    for i, h in enumerate(targets, 1):
        if not library.valid_hash(h) or not lib.exists(h):
            print(f"[{i}/{len(targets)}] {h[:12]}: not found")
            fail += 1
            continue
        name = (lib.read_meta(h) or {}).get("filename", h)
        print(f"[{i}/{len(targets)}] {name}")
        try:
            err = rechord(lib, h)
        except Exception as e:                   # una cancion rota no para el resto
            err = str(e)
        if err:
            print(f"    skipped: {err}")
            fail += 1
        else:
            ok += 1
    log.event("chords.regenerate", user=user, ok=ok, failed=fail)
    print(f"OK: {ok} regenerated, {fail} skipped.")


if __name__ == "__main__":
    main()
