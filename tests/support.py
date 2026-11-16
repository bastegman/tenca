# -*- coding: utf-8 -*-
"""Utilidades comunes de los tests. `_app/` no es un paquete: se añade a sys.path y se importan los módulos
tal como los importa el servidor."""
import contextlib
import json
import os
import re
import struct
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
APP = os.path.join(REPO, "_app")
if APP not in sys.path:
    sys.path.insert(0, APP)

os.environ.setdefault("LOCALAPPDATA", tempfile.mkdtemp(prefix="notas-localappdata-"))  # DATA_DIR de los tests

import server  # noqa: E402  (después de preparar sys.path)
import carga  # noqa: E402
import engines  # noqa: E402
import live  # noqa: E402

PATH_NAMES = ("ROOT", "APP_DIR", "STATIC_DIR", "EXPORT_DIR", "DATA_DIR")
# server.py se partio en modulos el 15/11/2026: server.py (la fachada, con main() y App) y los que salieron de el.
# Los tests que miran el codigo («la ruta X esta registrada», «main() hace Y») leen todos juntos.
PARTES_DEL_SERVIDOR = ("server.py",) + tuple(n + ".py" for n in server.PARTES)


def codigo_del_servidor():
    """El texto de server.py y de los modulos que salieron de el, uno detras de otro."""
    out = []
    for n in PARTES_DEL_SERVIDOR:
        with open(os.path.join(APP, n), encoding="utf-8") as f:
            out.append(f.read())
    return "\n".join(out)


def app_modules():
    """Módulos de _app ya importados (cuando server.py se reparta en varios, todos comparten rutas y LINKS)."""
    return [m for name, m in list(sys.modules.items())
            if m is not None and getattr(m, "__file__", None) and os.path.dirname(os.path.abspath(m.__file__)) == APP]


@contextlib.contextmanager
def patched_paths(**values):
    """Sustituye ROOT, APP_DIR… (y vacía LINKS, VOCAB_LINKS y ALIASES) en todos los módulos de _app mientras dura el
    bloque. ALIASES (las rutas nuevas de las reuniones movidas, de carpetas) tambien: un test que mueve «r.wav» dejaba
    su alias y el Handler llevaba el «r.wav» de otro test a esa ruta (revision de la integracion del 08/11/2026)."""
    saved = []
    for m in app_modules():
        for k, v in values.items():
            if hasattr(m, k):
                saved.append((m, k, getattr(m, k)))
                setattr(m, k, v)
    links_saved, vlinks_saved = dict(server.LINKS), dict(server.VOCAB_LINKS)
    with server.ALIASES_LOCK:
        aliases_saved = dict(server.ALIASES)
        server.ALIASES.clear()
    sugeridos_saved = set(server.TEXTOS_SUGERIDOS)  # los textos de participantes que «escribio la app» (la pista)
    server.TEXTOS_SUGERIDOS.clear()
    server.LINKS.clear()
    server.VOCAB_LINKS.clear()
    try:
        yield
    finally:
        for m, k, v in reversed(saved):
            setattr(m, k, v)
        server.LINKS.clear()
        server.LINKS.update(links_saved)
        server.VOCAB_LINKS.clear()
        server.VOCAB_LINKS.update(vlinks_saved)
        with server.ALIASES_LOCK:
            server.ALIASES.clear()
            server.ALIASES.update(aliases_saved)
        server.TEXTOS_SUGERIDOS.clear()
        server.TEXTOS_SUGERIDOS.update(sugeridos_saved)


class TempTree(unittest.TestCase):
    """Un ROOT temporal con su _app/ y un DATA_DIR aparte; COMPLETE apunta a ese DATA_DIR."""
    # el nombre de la carpeta: el de una instalacion nueva (server.root_name() == APP_NAME); la de una instalada encima
    # de Chuleta, que conserva su nombre, la prueban test_raiz_real (ROOT_NAME = "Chuleta") y test_ui_smoke
    ROOT_NAME = "Tenca"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="notas-test-")
        self.root = os.path.join(self.tmp, self.ROOT_NAME)
        self.app_dir = os.path.join(self.root, "_app")
        self.data_dir = os.path.join(self.tmp, "datos")
        os.makedirs(os.path.join(self.app_dir, "static"))
        os.makedirs(self.data_dir)
        self._ctx = patched_paths(ROOT=self.root, APP_DIR=self.app_dir, DATA_DIR=self.data_dir,
                                  STATIC_DIR=os.path.join(self.app_dir, "static"))
        self._ctx.__enter__()
        self._complete = server.COMPLETE
        server.COMPLETE = server.Complete(os.path.join(self.data_dir, "completas.json"))
        # lo ocupado que va el PC (carga.py) solo se mide en Windows: en la CI, con el runner cargado por los propios
        # tests, el trabajo de fondo esperaria. Los tests que lo prueban lo parchean ellos (test_arranque_suave)
        self._carga = (carga.ocupado, carga.poca_memoria, carga.memoria)
        carga.ocupado, carga.poca_memoria = (lambda *a, **k: None), (lambda *a, **k: False)
        carga.memoria = lambda: None

    def tearDown(self):
        server.COMPLETE = self._complete
        carga.ocupado, carga.poca_memoria, carga.memoria = self._carga
        self._ctx.__exit__(None, None, None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- ficheros de prueba
    def write(self, rel, text="", encoding="utf-8"):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding=encoding, newline="") as f:
            f.write(text)
        return p

    def wav(self, rel, seconds=2.0, open_header=False, rate=48000, channels=2):
        """WAV 16 bit con cabecera cerrada (normal) o abierta (0xFFFFFFFF: grabación en curso)."""
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(wav_bytes(seconds, open_header, rate, channels))
        return p


def wav_bytes(seconds=2.0, open_header=False, rate=48000, channels=2, data=None):
    block = channels * 2
    if data is None:
        data = b"\0" * (int(seconds * rate) * block)
    n = 0xFFFFFFFF if open_header else len(data)
    riff = 0xFFFFFFFF if open_header else 36 + len(data)
    head = (b"RIFF" + struct.pack("<I", riff) + b"WAVE" + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block, block, 16)
            + b"data" + struct.pack("<I", n))
    return head + data


def srt_text(segs):
    return "\n".join(server.srt_block(i, a, b, t) for i, (a, b, t) in enumerate(segs, 1))


class FakeJob:
    """Lo mínimo que leen live.run / choose_cut y Jobs.active_for de un trabajo."""

    def __init__(self, path="x.wav", kind="transcribe", status="running", live=False, preset="rapido"):
        self.path, self.kind, self.status, self.live, self.preset = path, kind, status, live, preset
        self.cancel = self.restart = False
        self.recording, self.recorded, self.transcribed, self.rev, self.progress = False, 0.0, 0.0, 0, 0.0
        self.stage, self.id, self.duration, self.edits = "", "job1", None, {}
        self.final_pass = False


def bare_jobs(jobs):
    """Un Jobs sin hilos ni motores: solo la lista y el candado (para probar active_for y compañía)."""
    j = server.Jobs.__new__(server.Jobs)
    j.list, j.lock, j.claimed = list(jobs), threading.Lock(), set()
    return j


# ----------------------------------------------------------------------------- la interfaz son módulos ES (punto 52)
STATIC = os.path.join(APP, "static")
# los que no son de la página de escritorio: el punto de entrada (no exporta nada), la página del móvil y el
# AudioWorklet (importarlos desde la página de escritorio los ejecutaría ahí)
NO_ESCRITORIO = ("main.js", "movil.js", "pistas-worklet.js")


def modulos_de_escritorio():
    return sorted(n for n in os.listdir(STATIC) if n.endswith(".js") and n not in NO_ESCRITORIO)


# Los tests de navegador miran y llaman cosas de la interfaz con page.evaluate («S.config», «api(…)», «toast(…)»).
# Con módulos nada de eso es global. Este guion, en cada página de escritorio (la que carga /main.js), vuelve a
# importar los módulos ya cargados (import() de la misma URL da el mismo módulo, no otra copia) y deja en window un
# acceso de solo lectura a lo que exportan, con su valor vivo (un `let` que cambia se ve cambiado). Es solo de los
# tests: la app no lo lleva (test_modulos.py comprueba que en window no hay nada más que la lista de main.js).
# Reasignar uno desde fuera (window.api = …) no cambiaría el del módulo: lanza un error, para que un test no crea que
# lo ha sustituido. window.__modulos es la promesa: un test que mira algo justo al cargar la página la espera.
EXPONER_MODULOS = r"""(() => {
  if (window.top !== window) return;
  addEventListener('DOMContentLoaded', () => {
    if (!document.querySelector('script[type="module"][src="/main.js"]')) return;
    window.__modulos = Promise.all(%s.map(m => import('/' + m))).then(mods => {
      for (const m of mods) for (const k of Object.keys(m)) {
        if (k in window) continue;
        Object.defineProperty(window, k, { configurable: true, get: () => m[k], set: () => {
          throw new Error(`«${k}» es de un módulo de la interfaz: no se cambia desde window (tests/support.py)`); } });
      }
      return true;
    });
  });
})();"""


def nuevo_contexto(navegador, **kw):
    """browser.new_context(**kw) con el guion de arriba en todas sus páginas."""
    ctx = navegador.new_context(**kw)
    ctx.add_init_script(EXPONER_MODULOS % json.dumps(modulos_de_escritorio()))
    return ctx


def orden_de_arranque():
    """La cadena de main.js («app.js → notas.js → …»): el orden en que se evalúan los módulos de escritorio, el de los
    <script> de antes."""
    with open(os.path.join(STATIC, "main.js"), encoding="utf-8") as f:
        m = re.search(r"((?:[\w-]+\.js\s*→\s*)+[\w-]+\.js)", f.read())
    return re.findall(r"[\w-]+\.js", m.group(1)) if m else []


def importa(src, nombre, desde):
    """¿El módulo (su texto) importa `nombre` de `desde` (p. ej. './directo.js')?"""
    for m in re.finditer(r"^import\s+\{([^}]*)\}\s+from\s+'([^']+)';", src, re.M):
        if m.group(2) == desde and nombre in [x.strip() for x in m.group(1).split(",")]:
            return True
    return False


def js_sin_modulo(src):
    """El código de un módulo de static/ sin sus import ni su export, para ejecutarlo con node como un script, con
    dobles de lo que importa (las funciones puras se prueban así)."""
    src = re.sub(r"^import\b[^;]*;[^\n]*\n", "", src, flags=re.M)
    return re.sub(r"^export \{[^}]*\};\n?", "", src, flags=re.M)


__all__ = ["server", "engines", "live", "TempTree", "patched_paths", "wav_bytes", "srt_text", "FakeJob",
           "bare_jobs", "REPO", "APP", "PARTES_DEL_SERVIDOR", "codigo_del_servidor", "STATIC", "nuevo_contexto",
           "js_sin_modulo", "modulos_de_escritorio", "orden_de_arranque", "importa"]
