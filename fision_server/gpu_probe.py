"""
Sonda de GPU: ¿torch y CTranslate2 (Whisper) pueden usar CUDA de verdad?
Con torch para ROCm (AMD) la tarjeta tambien sale como "cuda"; la
arquitectura es un gfx (gfx1100) en vez de un sm. CTranslate2 no tiene
ROCm: en AMD las letras van siempre a CPU (whisper_no_cuda).

nvidia-smi solo dice que hay una tarjeta. Con un driver viejo, un torch de CPU
o una GPU fuera de las arquitecturas del wheel, nvidia-smi la ve pero el
pipeline acaba en CPU sin avisar. Esto lo comprueba.

Se lanza en un proceso aparte (`python -m fision_server.gpu_probe`, imprime
JSON): importar torch en el server costaria RAM y segundos de arranque.
doctor la llama directamente.
"""
import os
import json
import re

# Motivos estables (codes): los traducen el panel y el cliente.
PROBLEMS = ("torch_error", "torch_no_cuda", "arch_unsupported",
            "whisper_no_cuda", "smi_missing")


def run():
    """Estado de CUDA visto por torch y CTranslate2."""
    out = {"torch": "", "torch_cuda": "", "torch_hip": "", "cuda_available": False,
           "device_name": "", "capability": "", "arch_ok": None, "cudnn": 0,
           "ct2_devices": 0, "error": ""}
    errors = []
    try:
        import torch
        out["torch"] = torch.__version__
        out["torch_cuda"] = torch.version.cuda or ""
        out["torch_hip"] = getattr(torch.version, "hip", None) or ""
        out["cuda_available"] = torch.cuda.is_available()
        if out["cuda_available"]:
            out["device_name"] = torch.cuda.get_device_name(0)
            if out["torch_hip"]:
                # "gfx1100:sramecc-:xnack-" -> gfx1100
                gfx = (_gfx_override()
                       or torch.cuda.get_device_properties(0).gcnArchName.split(":")[0])
                out["capability"] = gfx
                out["arch_ok"] = gfx in {a.split(":")[0] for a in torch.cuda.get_arch_list()}
            else:
                major, minor = torch.cuda.get_device_capability(0)
                out["capability"] = f"{major}.{minor}"
                out["arch_ok"] = arch_supported(major, minor, torch.cuda.get_arch_list())
                out["cudnn"] = torch.backends.cudnn.version() or 0
    except Exception as e:
        errors.append(f"torch: {e}")
    try:
        import ctranslate2
        out["ct2_devices"] = ctranslate2.get_cuda_device_count()
    except Exception as e:
        errors.append(f"ctranslate2: {e}")
    out["error"] = "; ".join(errors)
    return out


def _gfx_override():
    """HSA_OVERRIDE_GFX_VERSION=10.3.0 -> gfx1030: ROCm usa esos nucleos aunque
    la tarjeta sea otra (RX 6600 = gfx1032). Stepping en hex (9.0.10 -> gfx90a)."""
    v = os.environ.get("HSA_OVERRIDE_GFX_VERSION", "").strip()
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", v)
    return f"gfx{m.group(1)}{m.group(2)}{int(m.group(3)):x}" if m else ""


def arch_supported(major, minor, arch_list):
    """sm_XY corre en la misma generacion X con minor >= Y (sm_60 vale para
    una GTX 10xx, sm_61)."""
    for a in arch_list:
        m = re.fullmatch(r"sm_(\d+)(\d)", a)
        if m and int(m.group(1)) == major and int(m.group(2)) <= minor:
            return True
    return False


def assess(smi_gpu, probe):
    """(usable, problema) a partir de nvidia-smi y la sonda.

    usable: la separacion puede ir en GPU. Un problema solo de Whisper no la
    quita (las letras caen a CPU aparte)."""
    cuda = probe.get("cuda_available")
    if smi_gpu:
        if not probe.get("torch"):
            return False, "torch_error"
        if not cuda:
            return False, "torch_no_cuda"          # driver viejo o torch de CPU
        if probe.get("arch_ok") is False:
            return False, "arch_unsupported"       # Pascal con cu129, gfx fuera del wheel
        if not probe.get("ct2_devices"):
            return True, "whisper_no_cuda"         # siempre en AMD
        return True, ""
    if cuda:
        return False, "smi_missing"                # contenedor sin 'utility'
    return False, ""                               # sin GPU: CPU normal


if __name__ == "__main__":
    print(json.dumps(run()))
