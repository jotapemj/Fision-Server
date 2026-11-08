# Fision server image.
#
#   docker build --build-arg TORCH_INDEX=cu129 -t fision-server .
#
# TORCH_INDEX: cu129 (RTX 20xx and newer), cu126 (Pascal and older), cpu.
# The repository must be cloned with --recurse-submodules (BTC weights).
# Model weights are not in the image: they go to the /models volume
# (python -m fision_server.prefetch, or on first use).

ARG PYTHON_IMAGE=python:3.12.13-slim-trixie

# ---- build: compila madmom e instala todo en /opt/venv ----
FROM ${PYTHON_IMAGE} AS build
ARG TORCH_INDEX=cu129

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git \
 && rm -rf /var/lib/apt/lists/*

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# torch primero, del indice de la GPU: trae las librerias CUDA 12
RUN pip install torch==2.13.0 torchaudio==2.11.0 torchvision==0.28.0 \
      --index-url https://download.pytorch.org/whl/${TORCH_INDEX}

COPY requirements.txt constraints.txt /tmp/
RUN pip install -r /tmp/requirements.txt -c /tmp/constraints.txt

# madmom: solo su codigo (BSD). Sus modelos (CC BY-NC-SA) no se usan y no
# se redistribuyen; models/__init__.py se queda porque `import madmom` lo carga.
RUN find /opt/venv/lib/python3.12/site-packages/madmom/models \
      -mindepth 1 -maxdepth 1 ! -name __init__.py -exec rm -rf {} +

# ---- runtime: sin compilador ----
FROM ${PYTHON_IMAGE}
ARG FISION_UID=1000
ARG FISION_GID=1000

# tzdata: TZ en .env pone los logs en hora local (por defecto UTC)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv

RUN groupadd -g ${FISION_GID} fision \
 && useradd -u ${FISION_UID} -g ${FISION_GID} -m -d /home/fision fision \
 && mkdir -p /data /library /models \
 && chown fision:fision /data /library /models \
 && chmod 1777 /home/fision

# El codigo al final: cambia en cada version y no debe invalidar lo de arriba
WORKDIR /app
COPY . /app
# Sin el submodulo no hay acordes: mejor fallar al construir que en el primer job
RUN test -f BTC-ISMIR19/test/btc_model_large_voca.pt \
 || (echo "Missing BTC weights: clone with --recurse-submodules" >&2; exit 1)
# Cliente web fijado por esta version (sha256 en fision_server/web.py)
RUN /opt/venv/bin/python -m fision_server.web

# Rutas fijas dentro del contenedor; en el host son volumenes (compose.yaml).
# nvidia-smi (utility) mide la VRAM: sin el, todo iria a CPU.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HOME=/home/fision \
    NUMBA_CACHE_DIR=/tmp/numba \
    FISION_DATA=/data \
    FISION_LIBRARY=/library \
    FISION_MODELS_DIR=/models \
    FISION_HOST=0.0.0.0 \
    FISION_PORT=8000 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

USER fision
EXPOSE 8000
VOLUME ["/data", "/library", "/models"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["python", "-m", "fision_server.run"]
