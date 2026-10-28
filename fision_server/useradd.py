"""
Alta de usuario desde la caja (no hay registro publico):

    python -m fision_server.useradd valeria
    python -m fision_server.useradd jp --admin
    python -m fision_server.useradd ana --no-demo

Pide password (Enter -> genera una aleatoria y la muestra una vez).
La password se reparte por fuera (en persona, Signal...), no por la app.
El primer usuario de una instalacion nueva sale admin aunque no se pida:
asi el panel /admin nunca queda sin dueño.
La cuenta nueva recibe la cancion demo (library/.demo/) salvo --no-demo.
"""
import sys
import getpass
import secrets

from . import users, log, accounts, demo


def main():
    flags = {"--admin", "--no-demo"}
    args = [a for a in sys.argv[1:] if a not in flags]
    want_admin = "--admin" in sys.argv[1:]
    want_demo = "--no-demo" not in sys.argv[1:]
    if len(args) != 1:
        print("usage: python -m fision_server.useradd <username> [--admin] [--no-demo]")
        sys.exit(1)
    name = args[0]

    if not users.valid_username(name):
        print(f"error: invalid username '{name}' (use [a-z0-9_], 2-32 characters)")
        sys.exit(1)
    if users.exists(name):
        print(f"error: user '{name}' already exists")
        sys.exit(1)

    print(f"library: {accounts.config.shown_path(accounts.config.LIBRARY_DIR)}   "
          f"data: {accounts.config.shown_path(accounts.config.DATA_DIR)}")
    left = accounts.leftovers(name)
    if left:
        # Restos de una cuenta borrada sin purgar: la nueva los heredaria
        print(f"warning: data from a previous account '{name}' is still on disk:")
        for p in left:
            print(f"    {p}")
        ans = input("The new account will own it. Continue? [y/N]: ")
        if ans.strip().lower() not in ("y", "yes"):
            print("cancelled. If that data is not needed, delete the paths above.")
            sys.exit(1)

    first = not users.list_users()
    admin = want_admin or first

    pw = getpass.getpass("Password (Enter to generate one): ").strip()
    generated = False
    if not pw:
        pw = secrets.token_urlsafe(12)
        generated = True

    try:
        users.create(name, pw, admin=admin)
    except ValueError as e:
        print(f"error: {e}")
        sys.exit(1)

    log.event("user.create", user=name, role="admin" if admin else None, by="cli")
    print(f"OK: user '{name}' created{' as administrator' if admin else ''}.")
    if first and not want_admin:
        print("    (first user of this installation: given the admin role)")
    if want_demo:
        try:
            n = demo.give(name)
        except Exception as e:
            print(f"warning: the demo song could not be copied: {e}")
        else:
            if n:
                log.event("user.demo", user=name, songs=n, by="cli")
                print(f"    demo song{'s' if n > 1 else ''} added: {n}")
    if generated:
        print(f"Generated password (save it, it is not shown again): {pw}")


if __name__ == "__main__":
    main()
