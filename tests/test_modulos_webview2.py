# -*- coding: utf-8 -*-
"""Los módulos de la interfaz (punto 52) en la ventana de verdad: server.py con pywebview y WebView2, como la abre el
acceso directo. En Chromium (y en Edge) ya lo prueba test_ui_smoke; aquí, el motor de la ventana propia: que el único
<script type="module"> carga, que la interfaz arranca (Hoy a la vista), que window.py encuentra onWindowClosing por
su nombre y que nada más de la interfaz queda en window. Lo que contesta la página queda en los artefactos.

Solo en Windows con pywebview y NOTAS_VENTANA_REAL=1 (workflow windows-real)."""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from support import APP

WIN = sys.platform == "win32"
ART = os.environ.get("NOTAS_ARTEFACTOS") or os.path.join(tempfile.gettempdir(), "notas-artefactos")
# lo que se pregunta a la página: una expresión que devuelve un JSON
SONDA = ("(() => { const h = document.querySelector('#main h1'); return JSON.stringify({"
         "hoy: !!(h && h.textContent.includes('Hoy')), "
         "scripts: [...document.scripts].map(s => (s.type || 'clásico') + ' ' + s.getAttribute('src')), "
         "cierre: typeof window.onWindowClosing, S: typeof window.S, api: typeof window.api, "
         "velocidades: document.querySelectorAll('#pSpeed button').length, ua: navigator.userAgent}); })()")
# la app de verdad; al cargar la página, un hilo le pregunta SONDA hasta que Hoy está a la vista (como mucho 60 s) y
# lo apunta en server.log. evaluate_js envuelve el código en un eval() que la CSP puede bloquear; run_js no (igual que
# window.py con onWindowClosing)
PRUEBA = ("import os, runpy, sys, threading, time\n"
          f"sys.path.insert(0, {APP!r})\n"
          "import common, window\n"
          f"SONDA = {SONDA!r}\n"
          "wv = window.import_webview()\n"
          "_crear = wv.create_window\n"
          "def crear(*a, **k):\n"
          "    w = _crear(*a, **k)\n"
          "    def sonda():\n"
          "        r = None\n"
          "        for _ in range(60):\n"
          "            time.sleep(1)\n"
          "            try:\n"
          "                r = w.evaluate_js(SONDA)\n"
          "            except Exception:\n"
          "                try:\n"
          "                    r = w.run_js(SONDA)\n"
          "                except Exception as e:\n"
          "                    r = repr(e)\n"
          "            if '\"hoy\": true' in str(r) or '\"hoy\":true' in str(r):\n"
          "                break\n"
          "        common.log('(prueba) modulos: ' + str(r))\n"
          "    w.events.loaded += lambda *a: threading.Thread(target=sonda, daemon=True).start()\n"
          "    return w\n"
          "wv.create_window = crear\n"
          f"runpy.run_path({os.path.join(APP, 'server.py')!r}, run_name='__main__')\n")


def puerto_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(WIN and os.environ.get("NOTAS_VENTANA_REAL"), "solo Windows con NOTAS_VENTANA_REAL=1")
class ModulosEnWebView2Tests(unittest.TestCase):
    def setUp(self):
        try:
            import webview  # noqa: F401
        except ImportError:
            self.skipTest("sin pywebview")
        self.data = tempfile.mkdtemp(prefix="notas-modulos-")
        self.port = puerto_libre()
        self.log = os.path.join(self.data, "server.log")
        self.out = open(os.path.join(self.data, "consola.txt"), "wb")
        self.p = subprocess.Popen([sys.executable, "-c", PRUEBA, "--port", str(self.port), "--data", self.data],
                                  cwd=APP, stdout=self.out, stderr=subprocess.STDOUT)

    def tearDown(self):
        if self.p.poll() is None:
            self.p.kill()
            self.p.wait(15)
        self.out.close()
        os.makedirs(ART, exist_ok=True)
        for name in ("server.log", "consola.txt"):
            src = os.path.join(self.data, name)
            if os.path.isfile(src):
                shutil.copyfile(src, os.path.join(ART, f"modulos_webview2.{name}"))
        shutil.rmtree(self.data, ignore_errors=True)

    def leer_log(self):
        try:
            with open(self.log, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def esperar_log(self, patron, tope=180):
        fin = time.time() + tope
        while time.time() < fin:
            m = re.search(patron, self.leer_log(), re.M)
            if m:
                return m
            if self.p.poll() is not None:
                break
            time.sleep(0.5)
        self.fail(f"sin «{patron}» en server.log:\n{self.leer_log()[-3000:]}")

    def test_la_interfaz_arranca_en_webview2(self):
        self.esperar_log(r"ventana abierta \(WebView2; hwnd \d+\)")
        m = self.esperar_log(r"\(prueba\) modulos: (.*)$", tope=120)
        try:
            r = json.loads(m.group(1))
        except ValueError:
            self.fail(f"la página no contestó un JSON: {m.group(1)[:500]}")
        os.makedirs(ART, exist_ok=True)
        with open(os.path.join(ART, "modulos_webview2.json"), "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1)
        self.assertEqual(r["scripts"], ["module /main.js"])
        self.assertTrue(r["hoy"], r)  # la interfaz ha arrancado: app.js, la cadena entera y el init
        self.assertGreater(r["velocidades"], 0)  # lo que pinta app.js al cargarse
        self.assertEqual(r["cierre"], "function")  # window.py la llama por su nombre al cerrar con la X
        self.assertEqual((r["S"], r["api"]), ("undefined", "undefined"))
        self.assertIn("Edg/", r["ua"])  # WebView2 es Edge
        # el servidor de la app sirve los módulos con un tipo que vale (los de un tipo distinto no se ejecutan) y sin
        # caché, también en Windows (donde el tipo de .js del registro puede ser text/plain)
        for nombre in ("main.js", "app.js", "pistas.js"):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/{nombre}", timeout=10) as resp:
                tipo = resp.headers["Content-Type"]
                self.assertTrue(tipo.startswith("text/javascript"), tipo)
                self.assertEqual(resp.headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
