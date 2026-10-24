"""
Baja de usuario desde la caja:

    python -m fision_server.userdel bob           # solo la cuenta (login)
    python -m fision_server.userdel bob --purge   # y su biblioteca, playlists y avatar

Sin --purge NO borra sus datos por seguridad: una baja por error no destruye
nada. Pero quedan en disco y una cuenta nueva con el mismo nombre los
heredaria (useradd avisa).

Un CLI no puede cancelar jobs del server: si la cuenta tiene trabajos vivos,
--purge se niega (cancelalos desde el panel o espera a que acaben).
"""
import sys

from . import users, log, accounts


def main():
    args = [a for a in sys.argv[1:] if a != "--purge"]
    purge = "--purge" in sys.argv[1:]
    if len(args) != 1:
        print("uso: python -m fision_server.userdel <username> [--purge]")
        sys.exit(1)
    name = args[0]

    if not users.exists(name):
        print(f"error: el usuario '{name}' no existe")
        sys.exit(1)
    if purge and accounts.queued_jobs(name):
        print(f"error: '{name}' tiene trabajos en marcha o en cola; "
              "cancelalos desde el panel antes de purgar")
        sys.exit(1)

    # El CLI lee las mismas envs que el servicio: sin ellas miraria otra
    # biblioteca. Se muestra para que se vea.
    print(f"biblioteca: {accounts.config.shown_path(accounts.config.LIBRARY_DIR)}   "
          f"datos: {accounts.config.shown_path(accounts.config.DATA_DIR)}")
    what = "y TODOS sus datos (biblioteca, playlists, avatar)" if purge         else "(sus datos NO se tocan)"
    ans = input(f"Borrar la cuenta '{name}' {what}? [s/N]: ")
    if ans.strip().lower() not in ("s", "si", "y", "yes"):
        print("cancelado.")
        return

    try:
        users.delete(name)
    except ValueError as e:                     # p.ej. ultimo administrador
        print(f"error: {e}")
        sys.exit(1)
    if purge:
        accounts.purge(name)
    log.event("user.delete", user=name, by="cli", purge=purge)
    print(f"OK: usuario '{name}' borrado.")
    left = accounts.leftovers(name)
    if left:
        print("Siguen en disco (una cuenta nueva con este nombre los heredaria):")
        for p in left:
            print(f"    {p}")


if __name__ == "__main__":
    main()
