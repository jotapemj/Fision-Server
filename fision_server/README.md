# Fision AI · Servidor

Servidor self-hosted que recibe audio desde la app movil, lo procesa con el
pipeline de Fision y guarda los resultados en una "biblioteca" en tu PC, para
volver a descargarlos sin reprocesar.

## Idea

- La peticion HTTP **no espera** el proceso largo (evita el corte de ~100s de
  Cloudflare): encola y devuelve un `job_id`. El movil pregunta el estado.
- **Un worker** procesa de una en una (la GPU no hace dos a la vez).
- **Biblioteca por hash**: `library/<sha256>/`. Un audio = un hash = un resultado.
  Reprocesar con otra config hace **override** (la ultima manda).
- **Atomico**: se procesa en `stems_tmp/` y solo si sale bien se hace el cambio.
  Un reproceso fallido o cancelado **no** destruye lo que ya tenias.

## Estructura en disco

```
library/<hash>/
    input.<ext>     audio original (semilla para reprocesar sin reenviar)
    meta.json       nombre real, config vigente, estado, fecha
    stems/          separacion vigente
    result.zip      zip de la separacion vigente
```

## Arrancar

```bash
pip install -r requirements.txt

export FISION_TOKEN="pon-un-token-largo-aqui"     # Bearer que pide la app
export FISION_LIBRARY="/ruta/a/library"           # donde guardar (def: ./library)
export FISION_CLI="python fision_cli.py"          # tu orquestador CLI

python -m fision_server.run
# o: uvicorn fision_server.app:app --host 0.0.0.0 --port 8000
```

### Contrato del CLI (lo que debe cumplir tu orquestador)

El servidor lo invoca como subproceso:

```
<FISION_CLI> <input_audio> <output_stems_dir> <config.json>
```

Debe dejar los `.wav` en `output_stems_dir` y devolver codigo 0 si todo fue bien.
(`config.json` lleva el mismo dict que ya produce la GUI.)

## Endpoints

| Metodo | Ruta                          | Que hace                                   |
|--------|-------------------------------|--------------------------------------------|
| POST   | `/jobs`                       | sube audio + `config_json` -> encola o cache |
| POST   | `/library/{hash}/reprocess`   | reprocesa el input guardado con nueva config |
| GET    | `/jobs/{job_id}`              | estado del trabajo                          |
| DELETE | `/jobs/{job_id}`              | cancela (en cola o en marcha)               |
| GET    | `/library`                    | lista de canciones ("mi nube")              |
| GET    | `/library/{hash}`             | meta de una cancion                         |
| GET    | `/library/{hash}/zip`         | descarga el `result.zip` vigente            |
| DELETE | `/library/{hash}`             | borra la entrada entera                     |

Todas piden `Authorization: Bearer <FISION_TOKEN>` (si el token esta vacio,
modo abierto solo para pruebas en local).

## Exponerlo (elige UNA via, documentada en el repo)

- **Solo LAN**: nada que abrir. Uso en casa.
- **Tailscale** (recomendado, sencillo): `http://tupc:8000`. Cero exposicion publica.
- **Cloudflare Tunnel**: URL publica. Anade Cloudflare Access + el token Bearer.
- **WireGuard**: control total, abres un puerto UDP (mudo a desconocidos).

El token Bearer va **siempre**, este detras de lo que este. Defensa en profundidad.

## Pendiente / ideas v2

- Limpieza de biblioteca (tope de tamano, borrar viejos).
- Persistir la cola en disco (ahora los jobs en vuelo viven en memoria).
- Hash del audio decodificado (mismo audio en mp3/wav = mismo hash).
