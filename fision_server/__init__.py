"""Fision server package.

La version sigue SemVer (MAJOR.MINOR.PATCH):
  MAJOR - rompe el contrato con el cliente (endpoint eliminado, campo
          cambiado, formato incompatible).
  MINOR - feature compatible (endpoint nuevo, campo opcional, env nuevo).
  PATCH - bugfix, deps, refactor interno, sin cambios visibles.

Se expone en GET /server/info["version"] para que el cliente pueda
mostrarla y detectar incompatibilidades.
"""

__version__ = "1.11.1"
