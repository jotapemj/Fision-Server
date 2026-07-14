"""
Reset de password por el admin (si alguien la olvida):

    python -m fision_server.userpasswd valeria

Sube el token_epoch: cualquier sesion abierta con la pass vieja se cae.
El cambio normal (el propio usuario) va por el endpoint /auth/change-password.
"""
import sys
import getpass
import secrets

from . import users, log


def main():
    if len(sys.argv) != 2:
        print("uso: python -m fision_server.userpasswd <username>")
        sys.exit(1)
    name = sys.argv[1]

    if not users.exists(name):
        print(f"error: el usuario '{name}' no existe")
        sys.exit(1)

    pw = getpass.getpass("Nueva password (Enter para generar): ").strip()
    generated = False
    if not pw:
        pw = secrets.token_urlsafe(12)
        generated = True

    try:
        users.set_password(name, pw)
    except ValueError as e:
        print(f"error: {e}")
        sys.exit(1)

    log.event("password_change.success", user=name, by="admin_cli")
    print(f"OK: password de '{name}' cambiada. Sus sesiones viejas caducan.")
    if generated:
        print(f"Password generada: {pw}")


if __name__ == "__main__":
    main()
