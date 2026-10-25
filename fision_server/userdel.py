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
        print("usage: python -m fision_server.userdel <username> [--purge]")
        sys.exit(1)
    name = args[0]

    if not users.exists(name):
        print(f"error: user '{name}' does not exist")
        sys.exit(1)
    if purge and accounts.queued_jobs(name):
        print(f"error: '{name}' has running or queued jobs; "
              "cancel them from the admin panel before purging")
        sys.exit(1)

    # El CLI lee las mismas envs que el servicio: sin ellas miraria otra
    # biblioteca. Se muestra para que se vea.
    print(f"library: {accounts.config.shown_path(accounts.config.LIBRARY_DIR)}   "
          f"data: {accounts.config.shown_path(accounts.config.DATA_DIR)}")
    what = ("and ALL its data (library, playlists, avatar)" if purge
            else "(its data is NOT touched)")
    ans = input(f"Delete the account '{name}' {what}? [y/N]: ")
    if ans.strip().lower() not in ("y", "yes"):
        print("cancelled.")
        return

    try:
        users.delete(name)
    except ValueError as e:                     # p.ej. ultimo administrador
        print(f"error: {e}")
        sys.exit(1)
    if purge:
        accounts.purge(name)
    log.event("user.delete", user=name, by="cli", purge=purge)
    print(f"OK: user '{name}' deleted.")
    left = accounts.leftovers(name)
    if left:
        print("Still on disk (a new account with this name would inherit it):")
        for p in left:
            print(f"    {p}")


if __name__ == "__main__":
    main()
