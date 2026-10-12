"""
Acordes sincronizados al beat sobre las probabilidades por frame de BTC.

BTC decide cada ~0.09 s sin saber de beats: en mezclas densas (metales,
montuno) parpadea entre acordes y entre X / X7. Aqui:

1. Prob media de los frames de cada beat (beats.txt) -> un voto por beat.
2. Viterbi sobre los beats: cambiar de acorde cuesta `change`; dentro de la
   misma familia (raiz + triada: C, C7, Cmaj7, C6...) solo `family`. Fuera
   del downbeat o del medio compas el coste se multiplica por `off_mult`.
3. Tramos consecutivos de la misma familia se funden; la etiqueta es el
   miembro con mas probabilidad acumulada en el tramo.

Salida: lineas .lab (start end chord) con el vocabulario de BTC, igual que
el camino sin suavizar. Solo numpy.
"""
import os
import numpy as np

_ROOTS = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
_QUALS = ['min', 'maj', 'dim', 'aug', 'min6', 'maj6', 'min7', 'minmaj7',
          'maj7', '7', 'dim7', 'hdim7', 'sus2', 'sus4']
# Triada de cada calidad (mismo criterio que _simplify_to_triad: dim -> menor)
_TRIAD = {'min': 'm', 'min6': 'm', 'min7': 'm', 'minmaj7': 'm', 'dim': 'm',
          'dim7': 'm', 'hdim7': 'm', 'maj': '', 'maj6': '', 'maj7': '',
          '7': '', 'aug': '', 'sus2': '', 'sus4': ''}


def settings():
    """Parametros por env. enabled=False -> camino crudo de BTC."""
    return {
        "enabled": os.environ.get("FISION_CHORDS_SMOOTH", "1").strip()
                   not in ("0", "false", "False"),
        "change": float(os.environ.get("FISION_CHORDS_CHANGE_COST", "1.0")),
        "family": float(os.environ.get("FISION_CHORDS_FAMILY_COST", "0.2")),
        "off_mult": float(os.environ.get("FISION_CHORDS_OFFBEAT_MULT", "2.0")),
    }


def _label(i):
    if i == 169:
        return 'N'
    if i == 168:
        return 'X'
    r, q = _ROOTS[i // 14], _QUALS[i % 14]
    return r if q == 'maj' else f"{r}:{q}"


def _family(i):
    if i >= 168:
        return _label(i)
    return _ROOTS[i // 14] + _TRIAD[_QUALS[i % 14]]


def read_beats(stems_dir):
    """(beats, downbeats) de beats.txt / downbeats.txt, o None si faltan."""
    bp = os.path.join(stems_dir, "beats.txt")
    dp = os.path.join(stems_dir, "downbeats.txt")
    if not (os.path.isfile(bp) and os.path.isfile(dp)):
        return None
    with open(bp) as f:
        beats = [float(x) for x in f.read().split()[1:]]   # 1a linea = BPM
    with open(dp) as f:
        downs = [float(x) for x in f.read().split()]
    if len(beats) < 2:
        return None
    return np.asarray(beats), np.asarray(downs)


def smooth(probs, frame_sec, length, beats, downs,
           change=1.0, family=0.2, off_mult=2.0):
    """probs (frames x 170) -> lista de (start, end, chord) por beats."""
    edges = np.unique(np.concatenate(
        ([0.0], beats[(beats > 0) & (beats < length)], [length])))
    starts = edges[:-1]

    # Posicion del tramo en el compas: beats desde el ultimo downbeat
    bi = np.searchsorted(beats, starts - 0.05)            # beats ya pasados
    di = np.searchsorted(downs, starts + 0.05) - 1
    pos = np.full(len(starts), 1)
    ok = di >= 0
    pos[ok] = bi[ok] - np.searchsorted(beats, downs[di[ok]] - 0.05)
    strong = (pos == 0) | (pos == 2)                      # downbeat / medio compas

    # Emision: log de la prob media de los frames de cada tramo
    nf = len(probs)
    E = np.empty((len(starts), probs.shape[1]))
    for k, (a, b) in enumerate(zip(edges[:-1], edges[1:])):
        i0 = min(int(a / frame_sec), nf - 1)
        i1 = min(max(i0 + 1, int(np.ceil(b / frame_sec))), nf)
        E[k] = np.log(probs[i0:i1].mean(0) + 1e-9)

    S = probs.shape[1]
    fam = np.array([_family(i) for i in range(S)])
    same = fam[:, None] == fam[None, :]
    cost = np.where(same, family, change)
    np.fill_diagonal(cost, 0.0)

    n = len(E)
    score = E[0].copy()
    back = np.zeros((n, S), dtype=np.int32)
    for k in range(1, n):
        cand = score[:, None] - cost * (1.0 if strong[k] else off_mult)
        back[k] = cand.argmax(0)
        score = cand.max(0) + E[k]
    path = [int(score.argmax())]
    for k in range(n - 1, 0, -1):
        path.append(int(back[k][path[-1]]))
    path.reverse()

    # Funde familias; etiqueta = miembro con mas prob acumulada
    out = []
    k = 0
    while k < n:
        j = k
        while j + 1 < n and fam[path[j + 1]] == fam[path[k]]:
            j += 1
        members = np.flatnonzero(fam == fam[path[k]])
        w = np.exp(E[k:j + 1][:, members]).sum(0)
        out.append((float(edges[k]), float(edges[j + 1]),
                    _label(int(members[w.argmax()]))))
        k = j + 1
    return out
