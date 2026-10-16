"""
Alta de usuario desde la caja (no hay registro publico):

    python -m fision_server.useradd valeria
    python -m fision_server.useradd jp --admin

Pide password (Enter -> genera una aleatoria y la muestra una vez).
La password se reparte por fuera (en persona, Signal...), no por la app.
El primer usuario de una instalacion nueva sale admin aunque no se pida:
asi el panel /admin nunca queda sin dueño.
"""
import sys
import getpass
import secrets

from . import users, log


def main():
    args = [a for a in sys.argv[1:] if a != "--admin"]
    want_admin = "--admin" in sys.argv[1:]
    if len(args) != 1:
        print("uso: python -m fision_server.useradd <username> [--admin]")
        sys.exit(1)
    name = args[0]

    if not users.valid_username(name):
        print(f"error: username invalido '{name}' (usa [a-z0-9_], 2-32 chars)")
        sys.exit(1)
    if users.exists(name):
        print(f"error: el usuario '{name}' ya existe")
        sys.exit(1)

    first = not users.list_users()
    admin = want_admin or first

    pw = getpass.getpass("Password (Enter para generar una): ").strip()
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
    print(f"OK: usuario '{name}' creado{' como administrador' if admin else ''}.")
    if first and not want_admin:
        print("    (primer usuario de la instalacion: se le da el rol admin)")
    if generated:
        print(f"Password generada (guardala, no se vuelve a mostrar): {pw}")


if __name__ == "__main__":
    main()
