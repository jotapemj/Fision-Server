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
        print("usage: python -m fision_server.userpasswd <username>")
        sys.exit(1)
    name = sys.argv[1]

    if not users.exists(name):
        print(f"error: user '{name}' does not exist")
        sys.exit(1)

    pw = getpass.getpass("New password (Enter to generate one): ").strip()
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
    print(f"OK: password of '{name}' changed. Its open sessions are closed.")
    if generated:
        print(f"Generated password: {pw}")


if __name__ == "__main__":
    main()
