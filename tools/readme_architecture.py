"""Architecture diagram for the README, in the style of the app's feature grid:
bordered cards, accent icons, text drawn as paths (Noto Sans) so it renders
the same everywhere. Needs fontTools.

Usage, from the repo root:
    python tools/readme_architecture.py docs/readme <noto_sans_regular.ttf>
Writes architecture-dark.svg and architecture-light.svg.
"""
import os
import re
import sys

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

OUT, FONT = sys.argv[1], sys.argv[2]

ACCENT = "#27B397"
# texto, texto secundario y flechas, borde de tarjeta (colores de GitHub)
THEMES = {
    "dark":  ("#E6EDF3", "#9198A1", "#3D444D"),
    "light": ("#1F2328", "#59636E", "#D1D9E0"),
}

W = 864
CARD_W, CARD_H = 224, 150
GAP_X = (W - 3 * CARD_W) // 2   # 96: hueco de las flechas
GAP_Y = 60
RADIUS = 12
ICON = 44
ICON_TOP = 22
TITLE_BASE, TITLE_SIZE = 96, 18
SUB_BASE, SUB_SIZE, SUB_LEAD = 120, 13, 18
LABEL_SIZE = 12
STROKE = 1.2                    # grosor en la caja de 24 del icono
ARROW_W = 1.5


def p(d, sw=STROKE):
    return (f'<path d="{d}" fill="none" stroke="{{c}}" stroke-width="{sw}" '
            f'stroke-linecap="round" stroke-linejoin="round"/>')


def dot(x, y, r):
    return f'<circle cx="{x}" cy="{y}" r="{r}" fill="{{c}}"/>'


ICONS = {}

# Movil con boton.
ICONS["phone"] = p("M8,2h8a2,2 0 0 1 2,2v16a2,2 0 0 1 -2,2h-8a2,2 0 0 1 -2,-2v-16a2,2 0 0 1 2,-2z"
                   "M10.5,18.5h3")

# Server: dos unidades con luces (igual que la rejilla de la app).
ICONS["server"] = (
    p("M4.5,3h15a1.5,1.5 0 0 1 1.5,1.5v4.5a1.5,1.5 0 0 1 -1.5,1.5h-15a1.5,1.5 0 0 1 -1.5,-1.5v-4.5a1.5,1.5 0 0 1 1.5,-1.5z"
      "M4.5,13.5h15a1.5,1.5 0 0 1 1.5,1.5v4.5a1.5,1.5 0 0 1 -1.5,1.5h-15a1.5,1.5 0 0 1 -1.5,-1.5v-4.5a1.5,1.5 0 0 1 1.5,-1.5z"
      "M13,6.75h5M13,17.25h5")
    + dot(6.75, 6.75, 1.1) + dot(6.75, 17.25, 1.1))


# Separacion: nota del logo, tronco en tres flechas, tres corcheas (app).
def logo_note(x, y, k):
    d = ("M207.5,287.5L207.5,227.4Q207.5,215.4 219.29,213.17L260.21,205.43"
         "Q272,203.2 272,215.2L272,275.8M207.5,242.8L272,231.5")
    sw = STROKE / k
    heads = (f'<circle cx="{207.5 - 18 + sw / 2}" cy="287.5" r="18" fill="{{c}}"/>'
             f'<circle cx="{272 - 18 + sw / 2}" cy="275.8" r="18" fill="{{c}}"/>')
    return (f'<g transform="translate({x} {y}) scale({k}) translate(-171.5 -203.2)">'
            f'{p(d, sw=sw)}{heads}</g>')


def eighth(x, y):
    return (f'<ellipse cx="{x}" cy="{y}" rx="1.75" ry="1.35" transform="rotate(-20 {x} {y})" fill="{{c}}"/>'
            + p(f"M{x + 1.15},{y - 0.1}V{y - 5.6}c0.35,1.6 2.6,2 2.4,4.2"))


def icon_arrow_head(x, y):
    return (f'<path d="M{x - 2.3},{y - 1.45}L{x},{y}L{x - 2.3},{y + 1.45}Z" fill="{{c}}" '
            f'stroke="{{c}}" stroke-width="0.4" stroke-linejoin="round"/>')


ICONS["split"] = (
    logo_note(0.3, 7.9, 0.08)
    + p("M10.8,12H13.5"
        "M13.5,12C16.6,12 16.6,4 19.8,4H21.5"
        "M13.5,12H21.5"
        "M13.5,12C16.6,12 16.6,20 19.8,20H21.5")
    + icon_arrow_head(23.6, 4) + icon_arrow_head(23.6, 12) + icon_arrow_head(23.6, 20)
    + eighth(26.8, 4.9) + eighth(26.8, 12.9) + eighth(26.8, 20.9))

# Resultados: forma de onda.
ICONS["wave"] = p("M3,10.5v3M6,7.5v9M9,4v16M12,8v8M15,5.5v13M18,9v6M21,11v2")

# Biblioteca: carpeta.
ICONS["folder"] = p("M3,6.5a1.5,1.5 0 0 1 1.5,-1.5h4.6l2,2.5h8.4a1.5,1.5 0 0 1 1.5,1.5v9.5"
                    "a1.5,1.5 0 0 1 -1.5,1.5h-15a1.5,1.5 0 0 1 -1.5,-1.5z")

ICON_W = {"split": 32}

# (columna, fila, icono, titulo, lineas de subtitulo)
CARDS = {
    "app":      (0, 0, "phone", "Fision app", ["Client"]),
    "server":   (1, 0, "server", "FisionServer", ["Accounts and job queue"]),
    "pipeline": (2, 0, "split", "Pipeline", ["Isolated process, GPU"]),
    "library":  (1, 1, "folder", "Library", ["Per user, by audio hash"]),
    "results":  (2, 1, "wave", "Results", ["Stems, beats, chords,", "metronomes, lyrics"]),
}

# --- texto como trazos ------------------------------------------------------
font = TTFont(FONT)
glyphs = font.getGlyphSet()
cmap = font.getBestCmap()
upm = font["head"].unitsPerEm
hmtx = font["hmtx"]


def text_width(s, size):
    return sum(hmtx[cmap[ord(ch)]][0] for ch in s) * size / upm


def text_path(s, size, x, y):
    scale = size / upm
    pen = SVGPathPen(glyphs)
    cursor = 0
    for ch in s:
        g = cmap[ord(ch)]
        glyphs[g].draw(TransformPen(pen, (scale, 0, 0, -scale, x + cursor * scale, y)))
        cursor += hmtx[g][0]
    return re.sub(r"-?\d+\.\d+", lambda m: f"{float(m.group()):.2f}".rstrip("0").rstrip("."),
                  pen.getCommands())


def text(s, size, cx, base, color, max_w=None):
    """Texto centrado en cx. Si no cabe en max_w, falla: mejor que recortado."""
    w = text_width(s, size)
    if max_w is not None and w > max_w:
        raise SystemExit(f"'{s}' mide {w:.0f} px, max {max_w}")
    return f'<path d="{text_path(s, size, cx - w / 2, base)}" fill="{color}"/>'


# --- piezas -----------------------------------------------------------------
def card_box(key):
    col, row, *_ = CARDS[key]
    return col * (CARD_W + GAP_X), row * (CARD_H + GAP_Y)


def card(key, fg, muted, border):
    x0, y0 = card_box(key)
    _, _, icon, title, subs = CARDS[key]
    cx = x0 + CARD_W / 2
    k = ICON / 24
    ix = cx - ICON_W.get(icon, 24) * k / 2
    out = [f'<rect x="{x0 + 0.5}" y="{y0 + 0.5}" width="{CARD_W - 1}" height="{CARD_H - 1}" '
           f'rx="{RADIUS}" fill="none" stroke="{border}"/>',
           f'<g transform="translate({ix} {y0 + ICON_TOP}) scale({k})">{ICONS[icon].replace("{c}", ACCENT)}</g>',
           text(title, TITLE_SIZE, cx, y0 + TITLE_BASE, fg, CARD_W - 24)]
    for i, s in enumerate(subs):
        out.append(text(s, SUB_SIZE, cx, y0 + SUB_BASE + i * SUB_LEAD, muted, CARD_W - 24))
    return out


def head(x, y, dx, dy, color):
    """Punta de flecha rellena en x,y apuntando en (dx, dy)."""
    L, H = 8, 4.5
    bx, by = x - dx * L, y - dy * L
    px, py = -dy * H, dx * H
    return (f'<path d="M{bx + px},{by + py}L{x},{y}L{bx - px},{by - py}Z" fill="{color}" '
            f'stroke="{color}" stroke-width="1" stroke-linejoin="round"/>')


def arrow(points, color):
    """Polilinea con punta en el ultimo punto."""
    d = "M" + "L".join(f"{x},{y}" for x, y in points)
    (x1, y1), (x2, y2) = points[-2], points[-1]
    n = max(abs(x2 - x1), abs(y2 - y1))
    dx, dy = (x2 - x1) / n, (y2 - y1) / n
    # la linea acaba donde empieza la punta
    trimmed = d.rsplit("L", 1)[0] + f"L{x2 - dx * 7},{y2 - dy * 7}"
    return [f'<path d="{trimmed}" fill="none" stroke="{color}" stroke-width="{ARROW_W}" '
            f'stroke-linecap="round" stroke-linejoin="round"/>', head(x2, y2, dx, dy, color)]


def h_arrow(a, b, top, bottom, line, muted, reverse=False):
    """Flecha horizontal entre dos tarjetas contiguas, con etiqueta arriba y abajo."""
    ax, ay = card_box(a)
    bx, _ = card_box(b)
    y = ay + CARD_H / 2
    x1, x2 = ax + CARD_W + 10, bx - 10
    if reverse:
        x1, x2 = x2, x1
    cx = (x1 + x2) / 2
    out = arrow([(x1, y), (x2, y)], line)
    out.append(text(top, LABEL_SIZE, cx, y - 9, muted, GAP_X - 6))
    out.append(text(bottom, LABEL_SIZE, cx, y + 20, muted, GAP_X - 6))
    return out


def build(theme):
    fg, muted, border = THEMES[theme]
    h = 2 * CARD_H + GAP_Y
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}" viewBox="0 0 {W} {h}" '
           'role="img" aria-label="Fision architecture: the client app uploads audio over HTTP '
           'to FisionServer, which runs the pipeline as one GPU process per job; the results go '
           'to the per-user library and the app downloads them as a zip.">']
    for key in CARDS:
        out += card(key, fg, muted, border)

    # app -> server -> pipeline
    out += h_arrow("app", "server", "HTTP + token", "Tailscale / LAN", muted, muted)
    out += h_arrow("server", "pipeline", "One job", "at a time", muted, muted)

    # pipeline -> resultados (vertical)
    px, py = card_box("pipeline")
    cx = px + CARD_W / 2
    out += arrow([(cx, py + CARD_H + 10), (cx, py + CARD_H + GAP_Y - 10)], muted)

    # resultados -> biblioteca
    out += h_arrow("library", "results", "Atomic", "swap", muted, muted, reverse=True)

    # biblioteca -> app: codo por debajo de la app
    lx, ly = card_box("library")
    ax, ay = card_box("app")
    y = ly + CARD_H / 2
    acx = ax + CARD_W / 2
    out += arrow([(lx - 10, y), (acx, y), (acx, ay + CARD_H + 10)], muted)
    out.append(text("result.zip", LABEL_SIZE, (lx - 10 + acx) / 2 + 20, y - 9, muted))
    out.append(text("download", LABEL_SIZE, (lx - 10 + acx) / 2 + 20, y + 20, muted))

    out.append("</svg>")
    return "\n".join(out)


for theme in THEMES:
    with open(os.path.join(OUT, f"architecture-{theme}.svg"), "w", encoding="utf-8") as f:
        f.write(build(theme))
print("ok")
