"""
Alta de usuario desde la caja (no hay registro publico):

    python -m fision_server.useradd valeria

Pide password (Enter -> genera una aleatoria y la muestra una vez).
La password se reparte por fuera (en persona, Signal...), no por la app.
"""
import sys
import getpass
import secrets

from . import users, log


def main():
    if len(sys.argv) != 2:
        print("uso: python -m fision_server.useradd <username>")
        sys.exit(1)
    name = sys.argv[1]

    if not users.valid_username(name):
        print(f"error: username invalido '{name}' (usa [a-z0-9_], 2-32 chars)")
        sys.exit(1)
    if users.exists(name):
        print(f"error: el usuario '{name}' ya existe")
        sys.exit(1)

    pw = getpass.getpass("Password (Enter para generar una): ").strip()
    generated = False
    if not pw:
        pw = secrets.token_urlsafe(12)
        generated = True

    try:
        users.create(name, pw)
    except ValueError as e:
        print(f"error: {e}")
        sys.exit(1)

    log.event("user.create", user=name)
    print(f"OK: usuario '{name}' creado.")
    if generated:
        print(f"Password generada (guardala, no se vuelve a mostrar): {pw}")


if __name__ == "__main__":
    main()
