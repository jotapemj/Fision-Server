"""
Baja de usuario desde la caja:

    python -m fision_server.userdel bob

Borra la cuenta (login). NO borra su carpeta library/<user>/ por seguridad:
eso lo decides tu a mano. Asi una baja por error no destruye datos.
"""
import sys

from . import users, log


def main():
    if len(sys.argv) != 2:
        print("uso: python -m fision_server.userdel <username>")
        sys.exit(1)
    name = sys.argv[1]

    if not users.exists(name):
        print(f"error: el usuario '{name}' no existe")
        sys.exit(1)

    ans = input(f"Borrar la cuenta '{name}'? (su library/ NO se toca) [s/N]: ")
    if ans.strip().lower() not in ("s", "si", "y", "yes"):
        print("cancelado.")
        return

    try:
        users.delete(name)
    except ValueError as e:                     # p.ej. ultimo administrador
        print(f"error: {e}")
        sys.exit(1)
    log.event("user.delete", user=name, by="cli")
    print(f"OK: usuario '{name}' borrado. Recuerda: library/{name}/ sigue en disco.")


if __name__ == "__main__":
    main()
