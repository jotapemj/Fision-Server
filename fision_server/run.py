"""
Arranque del servidor:

    python -m fision_server.run

O directamente con uvicorn (recarga en desarrollo):

    uvicorn fision_server.app:app --host 0.0.0.0 --port 8000
"""
import uvicorn

from . import config

if __name__ == "__main__":
    uvicorn.run("fision_server.app:app", host=config.HOST, port=config.PORT)
