import os
import sys
import re
import time
import queue
import shutil
import threading
import traceback
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

# True: preguntar carpeta de salida | False: crear carpeta junto al audio
ASK_OUTPUT_DIR_DEFAULT = False

BG        = "#0f1419"   # fondo profundo
PANEL     = "#171d24"   # tarjetas / paneles
FIELD     = "#212a33"   # campos e inputs
BORDER    = "#2b3742"   # bordes sutiles
TXT       = "#e6edf3"   # texto principal
MUTED     = "#8b97a4"   # texto secundario
TEAL      = "#2dd4bf"   # acento (anillos)
TEAL_DK   = "#14b8a6"   # acento presionado
RED       = "#ff5454"   # nucleo
LOG_BG    = "#0b0f14"   # consola
LOG_FG    = "#cdd6e0"
DIM       = "#454f59"   # texto/controles atenuados (mientras procesa)

FONT      = "Segoe UI"
MONO      = "Consolas"

# ---------------------------------------------------------------------------
#  Fases del pipeline -> texto detectado en el log real, etiqueta legible.
#  SOLO sirven para poner un nombre a la fase actual. NO inventamos porcentajes:
#  el pipeline no emite progreso global, asi que la barra es indeterminada.
# ---------------------------------------------------------------------------
STAGES = [
    ("=== Obteining metadata",  "Leyendo metadatos"),
    ("=== Audio-Separator",     "Separando pistas"),
    ("=== MADMOM",              "Detectando beats y tempo"),
    ("Generating metronomes",   "Generando metronomos"),
    ("=== CHORD EXTRACTION",    "Extrayendo acordes"),
    ("Process completed",       "Listo"),
]

# Marcadores que imprime audio_separator_script.py -> nombre del modelo activo.
# Sirven para etiquetar la sub-barra de progreso real de cada modelo.
AS_STAGES = [
    ("[BASE]",  "RoFormer · voces/instrumental"),
    ("[VOCES]", "Karaoke · principal/coros"),
    ("[ft]",    "HTDemucs ft · bajo/bateria"),
    ("[6s]",    "HTDemucs 6s · guitarra/piano"),
    ("[drums]", "MDX23C · piezas de bateria"),
    ("[brass]", "Vientos · UVR"),
    ("[other]", "Calculando other (resta)"),
]
# tqdm escupe "...: 26%|##6   | 8/30 [..]"; cogemos el ultimo porcentaje del trozo.
_PCT_RE = re.compile(r"(\d{1,3})%\|")


# ===========================================================================
#  Redireccion de salida: captura prints de Python (live) y de subprocesos
# ===========================================================================
class QueueWriter:
    """Reemplaza sys.stdout/err. Empuja texto a una cola al instante (sin buffer)."""
    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(("PY", s))   # prints en-proceso: beat_this/BTC/Fision
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


class OutputCapture:
    """
    Captura TODA la salida en la cola:
      - prints de Python -> via sys.stdout/err (instantaneo).
      - subprocesos hijos -> via redireccion de descriptores 1/2 (y handles en Windows).
    Degrada con elegancia si algo falla: al menos veras los prints de Python.
    """
    def __init__(self, q):
        self.q = q
        self._active = False
        self._fd_ok = False
        self._py_out = sys.stdout
        self._py_err = sys.stderr

    def start(self):
        if self._active:
            return
        self._active = True
        # 1) Python -> cola (siempre funciona, siempre live)
        sys.stdout = QueueWriter(self.q)
        sys.stderr = QueueWriter(self.q)
        # 2) Descriptores del SO -> tuberia (captura subprocesos)
        try:
            self._r, self._w = os.pipe()
            self._saved_out = os.dup(1)
            self._saved_err = os.dup(2)
            os.dup2(self._w, 1)
            os.dup2(self._w, 2)
            self._win_redirect()
            self._fd_ok = True
            self._reader = threading.Thread(target=self._pump, daemon=True)
            self._reader.start()
        except Exception:
            self._fd_ok = False  # sin captura de subprocesos, pero seguimos

    def _win_redirect(self):
        if os.name != "nt":
            return
        import ctypes, msvcrt
        self._k = ctypes.windll.kernel32
        self._STD_OUT, self._STD_ERR = -11, -12
        self._saved_h_out = self._k.GetStdHandle(self._STD_OUT)
        self._saved_h_err = self._k.GetStdHandle(self._STD_ERR)
        os.set_inheritable(self._w, True)
        h = msvcrt.get_osfhandle(self._w)
        self._k.SetStdHandle(self._STD_OUT, h)
        self._k.SetStdHandle(self._STD_ERR, h)

    def _pump(self):
        while True:
            try:
                data = os.read(self._r, 4096)
            except OSError:
                break
            if not data:
                break
            self.q.put(("AS", data.decode("utf-8", errors="replace")))  # subproceso audio-separator
        try:
            os.close(self._r)
        except OSError:
            pass

    def stop(self):
        if not self._active:
            return
        self._active = False
        try:
            self._py_out.flush()
        except Exception:
            pass
        sys.stdout = self._py_out
        sys.stderr = self._py_err
        if self._fd_ok:
            try:
                if os.name == "nt":
                    self._k.SetStdHandle(self._STD_OUT, self._saved_h_out)
                    self._k.SetStdHandle(self._STD_ERR, self._saved_h_err)
            except Exception:
                pass
            try:
                os.dup2(self._saved_out, 1)
                os.dup2(self._saved_err, 2)
                os.close(self._saved_out)
                os.close(self._saved_err)
            except OSError:
                pass
            try:
                os.close(self._w)  # cierra el ultimo extremo de escritura -> EOF
            except OSError:
                pass


# ===========================================================================
#  Aplicacion
# ===========================================================================
class FisionApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Fision AI")
        self.configure(bg=BG)
        self.minsize(1024, 640)
        try:
            self.tk.call("tk", "scaling", 1.25)
        except Exception:
            pass

        # estado
        self.input_file = tk.StringVar(value="")
        self.device     = tk.StringVar(value="cpu")
        self.ask_out    = tk.BooleanVar(value=ASK_OUTPUT_DIR_DEFAULT)
        # Guardar en la biblioteca por hash, igual que el servidor (cache compartida).
        self.save_server = tk.BooleanVar(value=False)
        self._server_hash = None

        # --- Estado de separacion -------------------------------------------
        # Grupo activo: "classic" | "personalized". Excluyentes.
        self.sep_group    = tk.StringVar(value="classic")
        # Classic: "4stem" (voz+bateria+bajo+other) | "2stem" (voz+instrumental)
        self.classic_pre  = tk.StringVar(value="4stem")
        # Personalized: cada pista on/off. Lo no marcado -> se mergea a other.wav.
        # Voces SIEMPRE presentes (nunca al other): no es casilla, solo variante.
        self.p_vocals_var = tk.StringVar(value="single")  # "single" | "split" (main+coros)
        self.p_guitar     = tk.BooleanVar(value=False)
        self.p_bass       = tk.BooleanVar(value=False)
        self.p_drums      = tk.BooleanVar(value=True)
        self.p_drums_var  = tk.StringVar(value="full")    # "full" | "split" (kick/snare/...)
        self.p_piano      = tk.BooleanVar(value=False)
        self.p_brass      = tk.BooleanVar(value=False)

        self.log_q = queue.Queue()
        self.capture = None
        self.worker = None
        self.running = False
        self.start_ts = 0.0
        self.last_out_dir = None
        self._pulse = 0.0
        self.parallel_mode = False   # se activa al detectar separacion+beats en paralelo
        # Log multi-fuente: cada fuente (AS=subproceso, PY=en-proceso) tiene su
        # propia linea "viva" (barra de progreso) para que no se pisen entre si.
        self._log_committed = []  # lineas terminadas (texto, tag); acotadas
        self._log_cur = {}        # fuente -> texto de su linea viva en construccion
        self._log_live = {}       # fuente -> texto a pintar como linea viva (o None)
        self._LOG_CAP = 1000      # tope de lineas fijas en pantalla
        # Sub-barra de progreso real por modelo (leida del % de tqdm del AS).
        self._bar_determinate = False  # True cuando mostramos % real
        self._as_stage = ""            # etiqueta del modelo AS en curso
        self._as_active = False        # True mientras el separador trabaja
        self._proc = None              # Popen del separador (para poder cancelar)
        self.cancel_requested = False  # True si el usuario pidio cancelar
        self._dim_store = {}           # fg original de labels atenuados

        self._build_styles()
        self._build_ui()

        self.after(60, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.update_idletasks()
        self._maximize()

    # ---- estilos ttk -----------------------------------------------------
    def _build_styles(self):
        st = ttk.Style(self)
        st.theme_use("clam")

        st.configure(".", background=PANEL, foreground=TXT,
                     font=(FONT, 10), borderwidth=0, focuscolor=PANEL)

        # Boton principal "Procesar"
        st.configure("Accent.TButton", background=TEAL, foreground="#08221f",
                     font=(FONT, 11, "bold"), padding=(14, 11), borderwidth=0)
        st.map("Accent.TButton",
               background=[("disabled", "#1f3d39"), ("pressed", TEAL_DK), ("active", "#3fe6d2")],
               foreground=[("disabled", "#5b6b69")])

        # Boton secundario
        st.configure("Ghost.TButton", background=FIELD, foreground=TXT,
                     font=(FONT, 10), padding=(12, 9), borderwidth=1)
        st.configure("Ghost.TButton", bordercolor=BORDER)
        st.map("Ghost.TButton",
               background=[("disabled", PANEL), ("pressed", "#2c3742"),
                           ("active", "#283139")],
               foreground=[("disabled", DIM)],
               bordercolor=[("active", TEAL)])

        # Boton de cancelar (rojo) que sustituye a Procesar mientras corre
        st.configure("Danger.TButton", background=RED, foreground="#1a0e0d",
                     font=(FONT, 11, "bold"), padding=(14, 11), borderwidth=0)
        st.map("Danger.TButton",
               background=[("pressed", "#a23a31"), ("active", "#d6584d")])

        # Control segmentado (radios como botones)
        st.configure("Seg.TButton", background=FIELD, foreground=MUTED,
                     font=(FONT, 10), padding=(12, 8), borderwidth=1,
                     bordercolor=BORDER, anchor="center")
        st.map("Seg.TButton",
               background=[("disabled", "selected", "#1f3d39"),  # elegido + atenuado
                           ("disabled", PANEL),
                           ("selected", TEAL), ("active", "#283139")],
               foreground=[("disabled", "selected", "#6f8f8a"),
                           ("disabled", DIM),
                           ("selected", "#08221f"), ("active", TXT)],
               bordercolor=[("selected", TEAL), ("active", BORDER)])

        # Checkbutton: el fondo SIEMPRE el de la UI (arregla hover blanco/blanco)
        st.configure("TCheckbutton", background=PANEL, foreground=MUTED,
                     font=(FONT, 9), focuscolor=PANEL)
        st.map("TCheckbutton",
               background=[("active", PANEL), ("disabled", PANEL)],
               foreground=[("disabled", DIM), ("active", TXT)],
               indicatorcolor=[("disabled", "#2a333b"),
                               ("selected", TEAL), ("!selected", FIELD)])

        # Barra de progreso
        st.configure("Fision.Horizontal.TProgressbar", troughcolor=FIELD,
                     background=TEAL, bordercolor=PANEL, lightcolor=TEAL,
                     darkcolor=TEAL, thickness=10)

    # ---- construccion de la interfaz ------------------------------------
    def _build_ui(self):
        pad = 22
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True, padx=pad, pady=pad)
        wrap.columnconfigure(0, weight=0)   # columna izquierda: controles (fija)
        wrap.columnconfigure(1, weight=1)   # columna derecha: salida (crece)
        wrap.rowconfigure(1, weight=1)      # el cuerpo crece en vertical

        # ===================== CABECERA (ancho completo) =====================
        head = tk.Frame(wrap, bg=BG)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 18))
        head.columnconfigure(1, weight=1)

        self.nucleus = tk.Canvas(head, width=46, height=46, bg=BG,
                                 highlightthickness=0)
        self.nucleus.grid(row=0, column=0, rowspan=2, padx=(0, 14))
        self._draw_nucleus(running=False)

        tk.Label(head, text="FISION  AI", bg=BG, fg=TXT,
                 font=(FONT, 22, "bold")).grid(row=0, column=1, sticky="w")
        tk.Label(head, text="Separa pistas. Detecta beats. Saca acordes.",
                 bg=BG, fg=MUTED, font=(FONT, 10)).grid(row=1, column=1, sticky="w")

        # ===================== COLUMNA IZQUIERDA: CONTROLES ==================
        left = tk.Frame(wrap, bg=BG, width=560)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 18))
        left.grid_propagate(False)          # respeta el ancho de 560
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)       # la tarjeta puede crecer

        card = self._card(left, row=0)
        self.controls_card = card        # para atenuar/bloquear al procesar

        # archivo
        self._field_label(card, "Archivo de audio").grid(row=0, column=0, sticky="w")
        filerow = tk.Frame(card, bg=PANEL)
        filerow.grid(row=1, column=0, sticky="ew", pady=(4, 16))
        filerow.columnconfigure(0, weight=1)
        self.file_lbl = tk.Label(filerow, text="Ningun archivo elegido", bg=FIELD,
                                 fg=MUTED, font=(FONT, 10), anchor="w",
                                 padx=12, pady=10)
        self.file_lbl.grid(row=0, column=0, sticky="ew")
        ttk.Button(filerow, text="Elegir…", style="Ghost.TButton",
                   command=self._pick_file).grid(row=0, column=1, padx=(8, 0))

        # procesador
        self._field_label(card, "Procesador").grid(row=2, column=0, sticky="w")
        self._segmented(card, row=3, var=self.device,
                        options=[("CPU", "cpu"), ("GPU · CUDA", "cuda")])

        # separacion (grupos excluyentes: Classic / Personalizada)
        self._build_sep_section(card, row=4)

        # acordes: siempre BTC (chordino deprecado). Sin selector.

        ttk.Checkbutton(card, text="Preguntar carpeta de salida",
                        variable=self.ask_out, style="TCheckbutton",
                        takefocus=False).grid(row=9, column=0, sticky="w",
                                              pady=(16, 0))

        ttk.Checkbutton(card, text="Guardar como el servidor (biblioteca por hash)",
                        variable=self.save_server, style="TCheckbutton",
                        takefocus=False).grid(row=10, column=0, sticky="w",
                                              pady=(6, 0))

        # boton procesar (debajo de la tarjeta, ancho completo de la columna)
        self.go_btn = ttk.Button(left, text="▶   Procesar", style="Accent.TButton",
                                 command=self._on_go)
        self.go_btn.grid(row=1, column=0, sticky="ew", pady=(16, 0))

        # ===================== COLUMNA DERECHA: SALIDA =======================
        right = tk.Frame(wrap, bg=BG)
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)      # el log llena el resto

        # --- panel de progreso ---
        prog = self._card(right, row=0)
        toprow = tk.Frame(prog, bg=PANEL)
        toprow.grid(row=0, column=0, sticky="ew")
        toprow.columnconfigure(0, weight=1)
        self.stage_lbl = tk.Label(toprow, text="En espera", bg=PANEL, fg=TXT,
                                  font=(FONT, 11, "bold"), anchor="w")
        self.stage_lbl.grid(row=0, column=0, sticky="w")
        self.pct_lbl = tk.Label(toprow, text="", bg=PANEL, fg=TEAL,
                                font=(FONT, 11, "bold"))
        self.pct_lbl.grid(row=0, column=1, sticky="e")

        self.bar = ttk.Progressbar(prog, style="Fision.Horizontal.TProgressbar",
                                   mode="indeterminate", maximum=100, value=0)
        self.bar.grid(row=1, column=0, sticky="ew", pady=(10, 8))

        self.time_lbl = tk.Label(prog, text="Tiempo  00:00", bg=PANEL, fg=MUTED,
                                 font=(MONO, 10), anchor="w")
        self.time_lbl.grid(row=2, column=0, sticky="w")
        self.open_btn = ttk.Button(prog, text="Abrir carpeta", style="Ghost.TButton",
                                   command=self._open_output, state="disabled")
        self.open_btn.grid(row=2, column=0, sticky="e")

        # --- log (siempre visible) ---
        loghead = tk.Frame(right, bg=BG)
        loghead.grid(row=1, column=0, sticky="ew", pady=(16, 6))
        loghead.columnconfigure(0, weight=1)
        tk.Label(loghead, text="LOG DEL TERMINAL", bg=BG, fg=MUTED,
                 font=(FONT, 8, "bold")).grid(row=0, column=0, sticky="w")
        clr = tk.Label(loghead, text="limpiar", bg=BG, fg=MUTED, font=(FONT, 8),
                       cursor="hand2")
        clr.grid(row=0, column=1, sticky="e")
        clr.bind("<Button-1>", lambda e: self._clear_log())

        self.log_frame = tk.Frame(right, bg=BORDER, highlightthickness=0)
        self.log_frame.grid(row=2, column=0, sticky="nsew")
        self.log_frame.columnconfigure(0, weight=1)
        self.log_frame.rowconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)       # el marco del log es lo que crece
        self.log_text = tk.Text(self.log_frame, bg=LOG_BG, fg=LOG_FG,
                                insertbackground=TEAL, font=(MONO, 10),
                                relief="flat", padx=12, pady=10, wrap="char",
                                borderwidth=0, state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        sb = ttk.Scrollbar(self.log_frame, command=self.log_text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.tag_config("stage", foreground=TEAL)
        self.log_text.tag_config("err", foreground=RED)
        self.log_text.tag_config("src_as", foreground="#5fb3a3")  # audio-separator

    # ---- helpers de UI ---------------------------------------------------
    def _card(self, parent, row, pady=(0, 0)):
        outer = tk.Frame(parent, bg=BORDER)
        outer.grid(row=row, column=0, sticky="ew", pady=pady)
        outer.columnconfigure(0, weight=1)
        inner = tk.Frame(outer, bg=PANEL, padx=16, pady=16)
        inner.grid(row=0, column=0, sticky="ew", padx=1, pady=1)
        inner.columnconfigure(0, weight=1)
        return inner

    def _field_label(self, parent, text):
        return tk.Label(parent, text=text.upper(), bg=PANEL, fg=MUTED,
                        font=(FONT, 8, "bold"))

    def _segmented(self, parent, row, var, options, command=None):
        bar = tk.Frame(parent, bg=PANEL)
        bar.grid(row=row, column=0, sticky="ew", pady=(4, 0))
        for i, (label, value) in enumerate(options):
            bar.columnconfigure(i, weight=1)
            rb = ttk.Radiobutton(bar, text=label, value=value, variable=var,
                                 style="Seg.TButton", takefocus=False,
                                 command=command)
            rb.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0))
        return bar

    # ---- seccion de separacion (Classic / Personalizada) -----------------
    def _build_sep_section(self, card, row):
        """Selector de grupo + dos paneles intercambiables en el mismo hueco."""
        self._field_label(card, "Separacion").grid(row=row, column=0, sticky="w",
                                                    pady=(12, 0))
        self._segmented(card, row=row + 1, var=self.sep_group,
                        options=[("Classic", "classic"),
                                 ("Personalizada", "personalized")],
                        command=self._refresh_sep_panel)

        # Hueco unico donde vive el panel activo.
        holder = tk.Frame(card, bg=PANEL)
        holder.grid(row=row + 2, column=0, sticky="ew", pady=(8, 0))
        holder.columnconfigure(0, weight=1)

        # --- Panel Classic: una sola eleccion (radios) ---
        self.classic_panel = tk.Frame(holder, bg=PANEL)
        self.classic_panel.grid(row=0, column=0, sticky="ew")
        self.classic_panel.columnconfigure(0, weight=1)
        self._segmented(self.classic_panel, row=0, var=self.classic_pre,
                        options=[("Voz + Bateria + Bajo + Other", "4stem"),
                                 ("Voz + Instrumental", "2stem")])

        # --- Panel Personalizada: pistas marcables, con variante donde aplica ---
        self.personalized_panel = self._build_personalized(holder)

        self._refresh_sep_panel()  # muestra el panel correcto al arrancar

    def _build_personalized(self, parent):
        panel = tk.Frame(parent, bg=PANEL)
        panel.grid(row=0, column=0, sticky="ew")
        panel.columnconfigure(0, weight=1)

        # Voces SIEMPRE salen. Fija, no casilla. Solo se elige la variante.
        tk.Label(panel, text="Voces (siempre)", bg=PANEL, fg=TXT,
                 font=(FONT, 9, "bold"), anchor="w").grid(row=0, column=0,
                                                          sticky="w", pady=(2, 0))
        vbar = tk.Frame(panel, bg=PANEL)
        vbar.grid(row=1, column=0, sticky="ew", padx=(22, 0), pady=(2, 0))
        vbar.columnconfigure(0, weight=1)
        self._segmented(vbar, row=0, var=self.p_vocals_var,
                        options=[("Voz unica", "single"),
                                 ("Voz principal + coros", "split")])

        # Pistas extra (solo on/off). Lo no marcado -> other.wav.
        self._p_stem_row(panel, 2, "Guitarra", self.p_guitar)
        self._p_stem_row(panel, 3, "Bajo", self.p_bass)
        # Drums con variante
        self._p_stem_row(panel, 4, "Bateria", self.p_drums,
                         var=self.p_drums_var,
                         options=[("Bateria completa", "full"),
                                  ("Kick/Snare/Toms/Hats/Crash/Ride/Perc", "split")])
        self._p_stem_row(panel, 6, "Piano", self.p_piano)
        self._p_stem_row(panel, 7, "Vientos / Metales", self.p_brass)

        hint = tk.Label(panel,
                        text="Lo que no marques se junta en other.wav",
                        bg=PANEL, fg=MUTED, font=(FONT, 8, "italic"),
                        anchor="w")
        hint.grid(row=8, column=0, sticky="w", pady=(10, 0))
        return panel

    def _p_stem_row(self, parent, row, label, enabled_var, var=None, options=None):
        """Fila de pista. Si trae options, despliega la variante al marcarla."""
        sub = None
        if var is not None and options:
            sub = tk.Frame(parent, bg=PANEL)
            sub.columnconfigure(0, weight=1)
            self._segmented(sub, row=0, var=var, options=options)

        def _toggle():
            if sub is None:
                return
            if enabled_var.get():
                sub.grid(row=row + 1, column=0, sticky="ew",
                         padx=(22, 0), pady=(2, 0))
            else:
                sub.grid_remove()

        ttk.Checkbutton(parent, text=label, variable=enabled_var,
                        style="TCheckbutton", takefocus=False,
                        command=_toggle).grid(row=row, column=0, sticky="w",
                                              pady=(6, 0))
        _toggle()  # estado inicial coherente con el default de la casilla

    def _refresh_sep_panel(self):
        """Muestra el panel del grupo activo, oculta el otro."""
        if self.sep_group.get() == "classic":
            self.personalized_panel.grid_remove()
            self.classic_panel.grid()
        else:
            self.classic_panel.grid_remove()
            self.personalized_panel.grid()

    def _collect_sep_config(self):
        """Empaqueta la eleccion de la GUI en un dict que comera la pipeline."""
        if self.sep_group.get() == "classic":
            preset = self.classic_pre.get()                 # "4stem" | "2stem"
            return {
                "mode": "classic",
                "preset": preset,
                # Puente con la pipeline actual (sep_mode 2/4):
                "sep_mode": 4 if preset == "4stem" else 2,
            }
        # Personalizada: voces SIEMPRE (fuera del dict opcional), arriba.
        return {
            "mode": "personalized",
            "vocals": self.p_vocals_var.get(),     # "single" | "split"
            "stems": {
                "guitar": {"enabled": self.p_guitar.get()},
                "bass":   {"enabled": self.p_bass.get()},
                "drums":  {"enabled": self.p_drums.get(),
                           "variant": self.p_drums_var.get()},
                "piano":  {"enabled": self.p_piano.get()},
                "brass":  {"enabled": self.p_brass.get()},
            },
        }

    def _draw_nucleus(self, running):
        c = self.nucleus
        c.delete("all")
        cx, cy = 23, 23
        ring = TEAL if running else BORDER
        # anillos
        c.create_oval(cx-19, cy-9, cx+19, cy+9, outline=ring, width=2)
        c.create_oval(cx-9, cy-19, cx+9, cy+19, outline=ring, width=2)
        # nucleo (late si esta trabajando)
        if running:
            import math
            r = 6 + 2.2 * (0.5 + 0.5 * math.sin(self._pulse))
            glow = "#ff7a7a"
        else:
            r, glow = 6, "#7a2f2f"
        c.create_oval(cx-r, cy-r, cx+r, cy+r, fill=RED if running else "#5a2626",
                      outline=glow, width=2)

    # ---- acciones --------------------------------------------------------
    def _pick_file(self):
        path = filedialog.askopenfilename(
            title="Elegir archivo de audio",
            filetypes=[("Audio", "*.mp3 *.wav"), ("Todos", "*.*")])
        if path:
            self.input_file.set(path)
            self.file_lbl.config(text=os.path.basename(path), fg=TXT)

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        self._log_committed = []
        self._log_cur = {}
        self._log_live = {}

    def _on_go(self):
        if self.running:
            return
        path = self.input_file.get()
        if not path or not os.path.isfile(path):
            messagebox.showwarning("Falta el audio",
                                   "Elige un archivo de audio valido.")
            return

        # Config de separacion. Voces siempre salen -> no hace falta validar pistas.
        sep_config = self._collect_sep_config()

        # carpeta de salida
        if self.save_server.get():
            # Modo servidor: biblioteca por hash (fuente unica en library.py).
            from fision_server import library as _lib
            h, tmp_stems = _lib.prepare_entry(path)
            out = str(tmp_stems)
            self._server_hash = h
        else:
            self._server_hash = None
            if self.ask_out.get():
                out = filedialog.askdirectory(title="Elegir carpeta de salida")
                if not out:
                    return
            else:
                p = Path(path)
                out = str(p.parent / p.stem)
            os.makedirs(out, exist_ok=True)
        self.last_out_dir = out

        # bloquear UI y arrancar
        self.running = True
        self.start_ts = time.time()
        self.parallel_mode = False
        self._as_active = False
        self._as_stage = ""
        self._bar_determinate = False
        self.cancel_requested = False
        self._proc = None
        self.stage_lbl.config(text="Preparando…", fg=TXT)
        self.pct_lbl.config(text="●", fg=TEAL)
        # barra honesta: arranca indeterminada. Pasa a % real cuando un modelo lo da.
        self.bar.config(mode="indeterminate")
        self.bar.start(18)
        self._set_running_ui(True)
        self._clear_log()

        self.capture = OutputCapture(self.log_q)
        self.capture.start()

        args = (path, out, self.device.get(), sep_config,
                bool(self.save_server.get()), self._server_hash)
        self.worker = threading.Thread(target=self._run_pipeline_thread,
                                       args=args, daemon=True)
        self.worker.start()

    def _run_pipeline_thread(self, path, out, device, sep_config,
                             save_server=False, server_hash=None):
        ok, err = True, ""
        try:
            print("Inicializando motor (la primera carga puede tardar)...")
            from headless_fisionAI import run_pipeline
            run_pipeline(path, out, device=device,
                         sep_config=sep_config,
                         on_proc=self._register_proc)

            # Modo servidor: commit atomico + meta.json (fuente unica en library.py).
            if save_server and server_hash and not self.cancel_requested:
                from fision_server import library as _lib
                _lib.finalize_entry(server_hash, Path(out), sep_config,
                                    os.path.basename(path))
                self.last_out_dir = str(_lib.stems_dir(server_hash))  # abrir carpeta final
                print(f"\n[biblioteca] guardado como {server_hash}")
        except Exception:
            ok = False
            err = traceback.format_exc()
            print("\n" + err)
            # Si fallo en modo servidor, tira el temporal (lo vigente queda intacto).
            if save_server and server_hash:
                try:
                    from fision_server import library as _lib
                    _lib.discard_tmp(server_hash)
                except Exception:
                    pass
        finally:
            self.log_q.put({"__event__": "done", "ok": ok, "error": err})

    def _register_proc(self, proc):
        """El worker registra aqui el subproceso del separador (para cancelar)."""
        self._proc = proc

    def _on_cancel(self):
        if not self.running:
            return
        if not messagebox.askyesno(
                "Cancelar",
                "¿Seguro que quieres cancelar el proceso en marcha?"):
            return
        self.cancel_requested = True
        self.stage_lbl.config(text="Cancelando…", fg=RED)
        self.go_btn.config(state="disabled")   # evita doble pulsacion
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()               # mata el separador
            except Exception:
                pass
        # El worker terminara y disparara 'done'. El dialogo de borrar carpeta
        # se muestra en _on_done, cuando todo se ha detenido de verdad.

    def _after_cancel_cleanup(self):
        """Tras cancelar: preguntar si borrar la carpeta creada o conservarla."""
        d = self.last_out_dir
        if not d or not os.path.isdir(d):
            return
        borrar = messagebox.askyesno(
            "Carpeta de salida",
            "¿Borrar la carpeta creada durante el proceso?\n\n"
            f"{d}\n\n"
            "Si: se elimina todo lo generado.\n"
            "No: se conserva lo que se haya creado.")
        if borrar:
            try:
                shutil.rmtree(d)
                self.last_out_dir = None
                self.open_btn.config(state="disabled")
                self.stage_lbl.config(text="Cancelado · carpeta borrada", fg=MUTED)
            except Exception as e:
                messagebox.showwarning("Fision AI",
                                       f"No pude borrar la carpeta: {e}")
        else:
            self.stage_lbl.config(text="Cancelado · carpeta conservada", fg=MUTED)

    def _set_running_ui(self, running):
        if running:
            self.go_btn.config(text="■   Cancelar", style="Danger.TButton",
                               command=self._on_cancel, state="normal")
        else:
            self.go_btn.config(text="▶   Procesar", style="Accent.TButton",
                               command=self._on_go, state="normal")
        self.open_btn.config(state="disabled" if running else
                             ("normal" if self.last_out_dir else "disabled"))
        self._set_controls_enabled(not running)   # atenuar mientras procesa
        self._draw_nucleus(running=running)

    def _set_controls_enabled(self, enabled):
        """Bloquea y atenua toda la zona de selecciones mientras se procesa."""
        st = "!disabled" if enabled else "disabled"

        def walk(w):
            for c in w.winfo_children():
                cls = c.winfo_class()
                if cls in ("TButton", "TRadiobutton", "TCheckbutton"):
                    try:
                        c.state([st])
                    except Exception:
                        pass
                elif isinstance(c, tk.Label):
                    if not enabled:
                        self._dim_store.setdefault(c, c.cget("fg"))
                        c.config(fg=DIM)
                    elif c in self._dim_store:
                        c.config(fg=self._dim_store.pop(c))
                walk(c)

        walk(self.controls_card)

    # ---- bucle periodico: drena log, anima, mueve barra ------------------
    def _tick(self):
        # drenar cola
        dirty = False
        try:
            while True:
                item = self.log_q.get_nowait()
                if isinstance(item, dict) and item.get("__event__") == "done":
                    self._render_log()        # vuelca lo que quede antes de cerrar
                    self._on_done(item)
                elif isinstance(item, tuple):
                    source, text = item
                    self._feed(source, text)
                    self._parse_progress(source, text)
                    dirty = True
                else:                          # retrocompat: string suelto -> PY
                    self._feed("PY", item)
                    self._parse_progress("PY", item)
                    dirty = True
        except queue.Empty:
            pass

        if dirty:
            self._render_log()

        # Mientras corre: solo cosas REALES. El reloj cuenta tiempo de verdad
        # y el nucleo late. La barra indeterminada se anima sola (.start()).
        # No tocamos ningun porcentaje porque no existe progreso global real.
        if self.running:
            el = int(time.time() - self.start_ts)
            self.time_lbl.config(text=f"Tiempo  {el // 60:02d}:{el % 60:02d}")
            self._pulse += 0.25
            self._draw_nucleus(running=True)

        self.after(60, self._tick)

    def _parse_progress(self, source, text):
        """Nombra la fase y, para el separador, mueve una sub-barra REAL por modelo.

        El audio-separator (AS) manda los marcadores [BASE]/[ft]/... y las barras
        tqdm con su %. Mientras el separador trabaja tiene PRIORIDAD sobre la barra
        (es la tarea pesada); MADMOM/acordes solo nombran la fase.
        """
        if source == "AS":
            # 1) cambio de modelo -> reetiquetar y volver a indeterminada hasta el 1er %
            for key, label in AS_STAGES:
                if key in text:
                    self._as_stage = label
                    self._as_active = True
                    self._set_stage(label)
                    self._bar_indeterminate()
                    break
            # 2) fin del separador
            if "[OK]" in text or "completada" in text:
                self._as_active = False
            # 3) % real de tqdm -> sub-barra determinada
            matches = _PCT_RE.findall(text)
            if matches and self._as_active:
                pct = int(matches[-1])
                self._bar_set_pct(pct)
                if self._as_stage:
                    self._set_stage(self._as_stage)
            return

        # ---- fuente PY (orquestador / MADMOM / acordes) ----
        # Mientras el separador esta activo, PY no toca la barra (evita saltos).
        if "paralelo" in text or "+ MADMOM" in text:
            self.parallel_mode = True
            if not self._as_active:
                self._set_stage("Separando pistas + beats")
            return

        for key, label in STAGES:
            if key in text:
                if not self._as_active:
                    self._set_stage(label)
                    self._bar_indeterminate()   # MADMOM/acordes: sin % fiable
                break

    def _set_stage(self, label):
        """Solo fija la etiqueta de la fase actual, leida del log real."""
        self.stage_lbl.config(text=label, fg=TXT)

    def _bar_indeterminate(self):
        """Vuelve a la barra pulsante (modelo cargando, sin % todavia)."""
        if self._bar_determinate:
            self.bar.config(mode="indeterminate")
            self.bar.start(18)
            self._bar_determinate = False
        self.pct_lbl.config(text="●", fg=TEAL)

    def _bar_set_pct(self, pct):
        """Sub-barra determinada con el % REAL del modelo en curso."""
        pct = max(0, min(100, pct))
        if not self._bar_determinate:
            self.bar.stop()
            self.bar.config(mode="determinate")
            self._bar_determinate = True
        self.bar["value"] = pct
        self.pct_lbl.config(text=f"{pct}%", fg=TEAL)

    def _set_bar(self, pct):
        pct = max(0, min(100, pct))
        self.bar["value"] = pct
        self.pct_lbl.config(text=f"{int(round(pct))}%")

    # ---- log multi-fuente: una linea viva por fuente, sin colisiones -----
    _SRC_LABEL = {"AS": "[AUDIO-SEPARATOR] ", "PY": ""}

    def _feed(self, source, text):
        """Convierte el stream crudo de una fuente en lineas terminadas + linea viva.

        Procesa caracter a caracter para sobrevivir a trozos partidos (un \\r
        cortado entre dos lecturas de 4096 bytes ya no rompe nada): el estado
        de la linea viva se conserva entre llamadas en self._log_cur[source].
        """
        cur = self._log_cur.get(source, "")
        for ch in text:
            if ch == "\r":
                cur = ""                       # retorno de carro: reescribe la linea
            elif ch == "\n":
                self._commit(source, cur)      # linea terminada -> log fijo
                cur = ""
            else:
                cur += ch
        self._log_cur[source] = cur
        # Linea viva: nada si esta vacia o solo espacios (el modelo manda varias).
        self._log_live[source] = cur if cur.strip() else None

    def _commit(self, source, line):
        # El audio-separator escupe varias lineas vacias: las filtramos.
        if source == "AS" and not line.strip():
            return
        prefix = self._SRC_LABEL.get(source, "")
        self._log_committed.append((prefix + line, self._line_tag(source, line)))
        if len(self._log_committed) > self._LOG_CAP:   # acotar historico
            del self._log_committed[:-self._LOG_CAP]

    def _line_tag(self, source, line):
        if ("Error" in line or "ERROR" in line or "Traceback" in line
                or "error" in line):
            return "err"
        if line.startswith("==="):
            return "stage"
        return "src_as" if source == "AS" else ""

    def _render_log(self):
        """Reconstruye el log entero: lineas fijas + UNA linea viva por fuente.

        Patron de consola estandar (delete-all + append en 'end'): inmune al
        salto de linea implicito de tkinter, sin colisiones entre fuentes.
        """
        t = self.log_text
        t.config(state="normal")
        t.delete("1.0", "end")
        for text, tag in self._log_committed:
            t.insert("end", text + "\n", tag)
        for source in ("AS", "PY"):                  # orden estable
            live = self._log_live.get(source)
            if live:
                prefix = self._SRC_LABEL.get(source, "")
                tag = "src_as" if source == "AS" else ""
                t.insert("end", prefix + live + "\n", tag)
        t.see("end")
        t.config(state="disabled")

    def _on_done(self, ev):
        self.running = False
        self.bar.stop()  # corta la animacion indeterminada
        if self.capture:
            self.capture.stop()
            self.capture = None
        self._as_active = False
        self._bar_determinate = True
        self._proc = None
        cancelled = self.cancel_requested

        if cancelled:
            self.stage_lbl.config(text="Cancelado", fg=RED)
            self.bar.config(mode="determinate")
            self.bar["value"] = 0
            self.pct_lbl.config(text="")
        elif ev.get("ok"):
            self.stage_lbl.config(text="Listo", fg=TEAL)
            self.bar.config(mode="determinate")
            self._set_bar(100)  # terminado de verdad -> 100% real, no fingido
        else:
            self.stage_lbl.config(text="Error en el proceso", fg=RED)
            self.bar.config(mode="determinate")
            self.bar["value"] = 0
            self.pct_lbl.config(text="")

        self._set_running_ui(False)   # reactiva controles, restaura "Procesar"
        self._draw_nucleus(running=False)

        if cancelled:
            self._after_cancel_cleanup()      # preguntar borrar/conservar carpeta
        elif ev.get("ok"):
            messagebox.showinfo("Fision AI", "Proceso completado.")
        else:
            messagebox.showerror("Fision AI",
                                 "Algo fallo. Revisa el log del terminal.")
        self.cancel_requested = False

    def _open_output(self):
        if not self.last_out_dir or not os.path.isdir(self.last_out_dir):
            return
        try:
            if os.name == "nt":
                os.startfile(self.last_out_dir)  # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", self.last_out_dir])
            else:
                subprocess.Popen(["xdg-open", self.last_out_dir])
        except Exception as e:
            messagebox.showwarning("Fision AI", f"No pude abrir la carpeta: {e}")

    def _maximize(self):
        """Arranca maximizado (ventana grande, no fullscreen sin bordes)."""
        try:
            self.state("zoomed")            # Windows / la mayoria de Linux
            return
        except Exception:
            pass
        try:
            self.attributes("-zoomed", True)  # algunos gestores de ventanas Linux
            return
        except Exception:
            pass
        # ultimo recurso: ocupar la pantalla por geometria
        w = self.winfo_screenwidth()
        h = self.winfo_screenheight()
        self.geometry(f"{w}x{h}+0+0")

    def _on_close(self):
        if self.running:
            if not messagebox.askyesno(
                    "Fision AI",
                    "Hay un proceso en marcha. ¿Salir igualmente?"):
                return
            if self.capture:
                self.capture.stop()
        self.destroy()


def main():
    app = FisionApp()
    app.mainloop()


if __name__ == "__main__":
    main()