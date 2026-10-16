"""
Cambia el rol de un usuario existente desde la caja:

    python -m fision_server.usermod jp --admin       # da acceso al panel /admin
    python -m fision_server.usermod jp --no-admin    # lo quita

Nunca deja el servidor sin administrador.
"""
import sys

from . import users, log


def main():
    if len(sys.argv) != 3 or sys.argv[2] not in ("--admin", "--no-admin"):
        print("uso: python -m fision_server.usermod <username> --admin|--no-admin")
        sys.exit(1)
    name, flag = sys.argv[1], sys.argv[2] == "--admin"

    try:
        users.set_admin(name, flag)
    except ValueError as e:
        print(f"error: {e}")
        sys.exit(1)

    log.event("user.role", user=name, role="admin" if flag else "user", by="cli")
    print(f"OK: '{name}' {'ahora es administrador' if flag else 'ya no es administrador'}.")


if __name__ == "__main__":
    main()
