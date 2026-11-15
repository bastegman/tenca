# -*- coding: utf-8 -*-
"""El fallo del msg 1170 en un navegador real (Playwright + Chromium): una reunion en la que el micro marco como «Tú»
a otra persona; clic en «Tú», escribir «Ana», Guardar → la etiqueta dice «Ana» y la persona del usuario no cambia.
Tambien «No soy yo» y «Copiar texto» con hablantes. Se salta sin Playwright (la CI lo instala en `ui-smoke.yml`)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from support import REPO, srt_text, wav_bytes
from test_ui_smoke import PERMITIDAS, puerto_libre

import voices  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - sin Playwright el test se salta
    sync_playwright = None

TRAMOS = [{"inicio": 0, "fin": 10, "h": "h1"}, {"inicio": 10, "fin": 20, "h": "h2"},
          {"inicio": 20, "fin": 30, "h": "h1"}]


@unittest.skipUnless(sync_playwright, "playwright no instalado")
class VocesEnNavegadorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="chuleta-ui-voces-")
        cls.root = os.path.join(cls.tmp, "Chuleta")
        shutil.copytree(os.path.join(REPO, "_app"), os.path.join(cls.root, "_app"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        cls.datos = os.path.join(cls.tmp, "datos")
        os.makedirs(cls.datos)
        with open(os.path.join(cls.datos, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"voices_enabled": True}, f)
        v = voices.Voices(os.path.join(cls.datos, "voces.json"))  # se cifra igual que en el servidor (mismo usuario)
        cls.yo = v.create("Bernardo", es_usuario=True)["id"]
        cls.port = puerto_libre()
        cls.log = open(os.path.join(cls.tmp, "salida.log"), "w", encoding="utf-8")
        cls.proc = subprocess.Popen([sys.executable, os.path.join(cls.root, "_app", "server.py"), "--port",
                                     str(cls.port), "--data", cls.datos, "--no-browser"], cwd=cls.root,
                                    stdout=cls.log, stderr=subprocess.STDOUT,
                                    env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        cls.base = f"http://127.0.0.1:{cls.port}"
        token_file, fin, cls.token = os.path.join(cls.datos, "sesion.token"), time.time() + 60, None
        while time.time() < fin and cls.proc.poll() is None:
            if os.path.isfile(token_file):
                with open(token_file, encoding="utf-8") as f:
                    cls.token = f.read().strip()
                try:
                    req = urllib.request.Request(cls.base + "/api/ping", headers={"X-Chuleta-Token": cls.token})
                    if json.load(urllib.request.urlopen(req, timeout=2)).get("app") == "audios":
                        break
                except Exception:
                    pass
            time.sleep(0.3)
        else:
            cls.tearDownClass()
            raise unittest.SkipTest("el servidor de prueba no arrancó (ver salida.log)")
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        for attr in ("browser", "pw"):
            try:
                getattr(cls, attr).stop() if attr == "pw" else getattr(cls, attr).close()
            except Exception:
                pass
        if getattr(cls, "proc", None) and cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(10)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
        try:
            cls.log.close()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def reunion(self, nombre):
        """Reunion con tres frases; el micro marco a h2 (otra persona) como «Tú», y h2 es del usuario por el micro."""
        base = os.path.join(self.root, nombre)
        with open(base + ".wav", "wb") as f:
            f.write(wav_bytes(1.0))
        with open(base + ".srt", "w", encoding="utf-8") as f:
            f.write(srt_text([(0.5, 9.5, "Hola a todos."), (10.5, 19.5, "Yo no soy Bernardo."),
                              (20.5, 29.5, "Seguimos.")]))
        voices.write_sidecar(base + voices.SIDECAR_EXT, {
            "version": 1, "tramos": TRAMOS, "tu": "h2", "micro": {"paso": 0.25, "activo": "0" * 40 + "1" * 40,
                                                                  "desfase": 0},
            "hablantes": {"h1": {"segundos": 20.0, "persona": None},
                          "h2": {"segundos": 10.0, "persona": self.yo, "origen": "micro"}}})
        return nombre + ".wav"

    def api(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Chuleta-Token": self.token, "X-Audios": "1",
                                              "Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=10))

    def abrir(self, rel):
        ctx = self.browser.new_context(bypass_csp=True)
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        errores, red = [], []
        pg.on("console", lambda m: errores.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        pg.on("pageerror", lambda e: errores.append(f"pageerror: {e}"))
        pg.on("response", lambda r: red.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1", timeout=15000)
        pg.evaluate("p => { location.hash = '#/a/' + encodeURIComponent(p); }", rel)
        pg.wait_for_selector("#txBody .spk[data-spk='h2']", timeout=15000)
        return pg, errores, red

    def limpio(self, errores, red):
        red = [r for r in red if not any(p in r for p in PERMITIDAS)]
        return [e for e in errores if not (not red and "Failed to load resource" in e)], red

    def chip(self, pg, h):
        return pg.inner_text(f"#txBody .spk[data-spk='{h}']")

    def test_nombrar_al_tu_equivocado(self):
        rel = self.reunion("R nombre")
        pg, errores, red = self.abrir(rel)
        self.assertEqual(self.chip(pg, "h2"), "Tú")
        pg.click("#txBody .spk[data-spk='h2']")
        pg.wait_for_selector(".spk-pop", timeout=5000)
        pg.fill("#spName", "Ana")
        pg.click(".spk-pop [data-sa='name']")
        time.sleep(1.5)  # la peticion y el repintado
        st = self.api("/api/voces")
        names = {p["id"]: (p["nombre"], p["es_usuario"]) for p in st["personas"]}
        self.assertEqual(names[self.yo], ("Bernardo", True))  # el usuario sigue siendo el y con su nombre
        pg.wait_for_function("document.querySelector(\"#txBody .spk[data-spk='h2']\").innerText === 'Ana'",
                             timeout=10000)
        self.assertIn(("Ana", False), names.values())
        texto = pg.evaluate("transcriptText()")  # lo que deja «Copiar texto» en el portapapeles
        self.assertEqual(texto, "Persona 1: Hola a todos.\n\nAna: Yo no soy Bernardo.\n\nPersona 1: Seguimos.")
        self.assertEqual(self.limpio(errores, red), ([], []))

    def test_no_soy_yo(self):
        rel = self.reunion("R no soy yo")
        pg, errores, red = self.abrir(rel)
        pg.click("#txBody .spk[data-spk='h2']")
        pg.wait_for_selector(".spk-pop [data-sa='notme']", timeout=5000)
        self.assertIn("No soy yo", pg.inner_text(".spk-pop [data-sa='notme']"))
        pg.click(".spk-pop [data-sa='notme']")
        pg.wait_for_function("document.querySelector(\"#txBody .spk[data-spk='h2']\").innerText === 'Persona 2'",
                             timeout=10000)
        self.assertIsNone(self.api("/api/hablantes?path=" + urllib.request.quote(rel))["tu"])
        self.assertEqual(self.limpio(errores, red), ([], []))

    def test_es_el_mismo_que(self):
        """«Es el mismo que…» (msg 1242): la app partio una voz en dos hablantes y se juntan desde la etiqueta (la
        revision de la integracion del 08/11/2026 vio que ningun test pulsaba el desplegable)."""
        nombre = "R unir"
        base = os.path.join(self.root, nombre)
        with open(base + ".wav", "wb") as f:
            f.write(wav_bytes(1.0))
        with open(base + ".srt", "w", encoding="utf-8") as f:
            f.write(srt_text([(0.5, 19.5, "Hola a todos."), (20.5, 39.5, "Empiezo yo."), (40.5, 59.5, "Seguimos.")]))
        voices.write_sidecar(base + voices.SIDECAR_EXT, {
            "version": 1, "tu": None, "micro": None, "creado": "2026-11-08T23:00:00",
            "tramos": [{"inicio": 0, "fin": 20, "h": "h1"}, {"inicio": 20, "fin": 40, "h": "h2"},
                       {"inicio": 40, "fin": 60, "h": "h3"}],
            "hablantes": {h: {"segundos": 20.0, "persona": None} for h in ("h1", "h2", "h3")}})
        rel = nombre + ".wav"
        pg, errores, red = self.abrir(rel)
        pg.click("#txBody .spk[data-spk='h3']")
        pg.wait_for_selector(".spk-pop #spJoin", timeout=5000)
        self.assertEqual(pg.eval_on_selector_all(".spk-pop #spJoin option", "os => os.map(o => o.value)"),
                         ["", "h1", "h2"])
        pg.select_option(".spk-pop #spJoin", "h1")
        pg.wait_for_function("!document.querySelector(\"#txBody .spk[data-spk='h3']\")", timeout=10000)
        hs = self.api("/api/hablantes?path=" + urllib.request.quote(rel))["hablantes"]
        self.assertEqual(sorted(hs), ["h1", "h2"])
        self.assertEqual(hs["h1"]["segundos"], 40.0)
        self.assertEqual(self.limpio(errores, red), ([], []))

    def test_fusionar_que_falla_repinta(self):
        """«Fusionar con…» que falla (una reunion abierta en otro programa): aviso de error y el cuadro se repinta con
        los hablantes releidos (parte de las reuniones puede haber cambiado; revision de la integracion del 08/11)."""
        rel = self.reunion("R fusionar")
        ana = self.api("/api/voces/persona", {"nombre": "Ana Fusión"})["id"]
        luis = self.api("/api/voces/persona", {"nombre": "Luis Fusión"})["id"]
        self.api("/api/voces/reasignar", {"path": rel, "h": "h1", "persona": luis})
        pg, errores, red = self.abrir(rel)
        pedidas = []

        def falla(route):  # la fusion cambia parte de las reuniones (esta) y falla en otra
            pedidas.append(route.request.post_data)
            self.api("/api/voces/reasignar", {"path": rel, "h": "h1", "persona": ana})
            route.fulfill(status=409, content_type="application/json",
                          body=json.dumps({"error": "1 reunión no se ha podido actualizar"}))
        pg.route("**/api/voces/fusionar", falla)
        pg.click("#txBody .spk[data-spk='h1']")
        pg.wait_for_selector(".spk-pop #spMerge", timeout=5000)
        self.assertIn("Fusionar con", pg.inner_text(".spk-pop #spMerge option:first-child"))
        pg.select_option(".spk-pop #spMerge", ana)
        pg.wait_for_function("!document.querySelector('.spk-pop')", timeout=5000)  # el cuadro se cierra
        # y los hablantes se vuelven a leer: la etiqueta ya dice lo que hay en el disco
        pg.wait_for_function("document.querySelector(\"#txBody .spk[data-spk='h1']\").innerText === 'Ana Fusión'",
                             timeout=5000)
        self.assertEqual(len(pedidas), 1)
        pg.wait_for_function("document.querySelector('#toasts').innerText.includes('no se ha podido actualizar')",
                             timeout=5000)
        errores, red = self.limpio(errores, [r for r in red if "/api/voces/fusionar" not in r])
        self.assertEqual((errores, red), ([], []))

    def nueva(self, nombre, segundos, md=None, resumen=None):
        """Reunión con una frase de 9 s por hablante (en el orden de `segundos`) y sus voces sin nombre."""
        base = os.path.join(self.root, nombre)
        with open(base + ".wav", "wb") as f:
            f.write(wav_bytes(1.0))
        hs = list(segundos)
        with open(base + ".srt", "w", encoding="utf-8") as f:
            f.write(srt_text([(k * 10 + 0.5, k * 10 + 9.5, f"Frase {k}.") for k in range(len(hs))]))
        voices.write_sidecar(base + voices.SIDECAR_EXT, {
            "version": 1, "tu": None, "micro": None, "creado": "2026-11-15T10:00:00",
            "tramos": [{"inicio": k * 10, "fin": k * 10 + 10, "h": h} for k, h in enumerate(hs)],
            "hablantes": {h: {"segundos": float(x), "persona": None} for h, x in segundos.items()}})
        if md:
            with open(base + ".md", "w", encoding="utf-8") as f:
                f.write("---\n" + "".join(f"{k}: {v}\n" for k, v in md.items()) + "---\n")
        if resumen:
            with open(base + ".resumen.md", "w", encoding="utf-8") as f:
                f.write(resumen)
        return nombre + ".wav"

    def test_quien_habla_embebido(self):
        """B8 (punto 21 del review: embebido, sin abrir otra ventana): «Ponles nombre» lleva al bloque «Quién habla»
        de la página, con todas las voces por tiempo hablado, los asistentes de la cita como sugerencia, «Recordar»
        solo con consentimiento y «Son la misma persona» con varias a la vez. Escape
        cierra el globo de una etiqueta."""
        rel = self.nueva("R quien", {"h1": 8.0, "h2": 30.0, "h3": 12.0, "h4": 5.0},
                         md={"participantes": "Ana Prueba, Luis Prueba", "participantes_origen": "outlook"})
        ctx = self.browser.new_context(bypass_csp=True, viewport={"width": 1280, "height": 800})
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        errores, red = [], []
        pg.on("pageerror", lambda e: errores.append(f"pageerror: {e}"))
        pg.on("response", lambda r: red.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1", timeout=15000)
        pg.wait_for_function("p => S.audios && S.audios.get(p)", arg=rel, timeout=10000)
        pg.evaluate("p => hoyAccion('voces', S.audios.get(p))", rel)  # lo que hace «Ponles nombre» en Hoy
        pg.wait_for_selector("#qhBloque .qh-fila", timeout=15000)
        self.assertTrue(pg.is_visible("#qhBloque"))
        sin_dialogo = "document.querySelector('#modal').classList.contains('hidden')"
        self.assertTrue(pg.evaluate(sin_dialogo))  # no es un diálogo
        filas = pg.eval_on_selector_all("#qhBloque .qh-fila", "fs => fs.map(f => f.dataset.h)")
        self.assertEqual(filas, ["h2", "h3", "h1", "h4"])  # de la que más habla a la que menos
        self.assertEqual(pg.evaluate("document.activeElement.closest('.qh-fila')?.dataset.h"), "h2")
        sug = pg.eval_on_selector_all("#qhBloque [data-qh-sug]", "bs => bs.map(b => b.textContent)")
        self.assertEqual(sug, ["Ana Prueba", "Luis Prueba"])
        # oír: la frase más larga de esa voz
        self.assertEqual(pg.evaluate("qhMuestra('h3').t"), 20.5)
        # «Recordar» sin consentimiento no guarda nada y enseña la casilla
        fila = "#qhBloque .qh-fila[data-h='h2']"
        pg.click("#qhBloque [data-qh-sug='Ana Prueba']")
        self.assertEqual(pg.input_value(fila + " [data-qh-nombre]"), "Ana Prueba")
        pg.check(fila + " [data-qh-rec]")
        pg.click(fila + " [data-qh-guardar]")
        pg.wait_for_selector("#qhBloque .qh-consent:not(.hidden)", timeout=5000)
        hs = self.api("/api/hablantes?path=" + urllib.request.quote(rel))["hablantes"]
        self.assertIsNone(hs["h2"]["persona"])
        pg.uncheck(fila + " [data-qh-rec]")
        pg.click(fila + " [data-qh-guardar]")
        pg.wait_for_function("document.querySelector(\"#txBody .spk[data-spk='h2']\").textContent === 'Ana Prueba'",
                             timeout=10000)
        self.assertTrue(pg.is_visible("#qhBloque"))  # sigue abierto tras guardar
        # «Son la misma persona»: dos voces de una vez
        pg.check("#qhBloque .qh-fila[data-h='h1'] [data-qh-sel]")
        self.assertTrue(pg.is_disabled("#qhBloque [data-qh-unir]"))
        pg.check("#qhBloque .qh-fila[data-h='h4'] [data-qh-sel]")
        pg.click("#qhBloque [data-qh-unir]")
        pg.wait_for_function("!document.querySelector(\"#qhBloque .qh-fila[data-h='h4']\")", timeout=10000)
        hs = self.api("/api/hablantes?path=" + urllib.request.quote(rel))["hablantes"]
        self.assertEqual(sorted(hs), ["h1", "h2", "h3"])
        self.assertEqual(hs["h1"]["segundos"], 20.0)  # sus tramos y los de h4
        # Escape cierra el globo de una etiqueta
        pg.click("#txBody .spk[data-spk='h3']")
        pg.wait_for_selector(".spk-pop", timeout=5000)
        pg.keyboard.press("Escape")
        pg.wait_for_selector(".spk-pop", state="detached", timeout=3000)
        self.assertEqual(self.limpio(errores, red), ([], []))

    def test_pagina_de_la_reunion_compacta(self):
        """B5 (puntos 13 y 22 del review): el resumen se ve sin bajar a 1500×920 y a 900×600 (antes, y = 805 y 932);
        lo poco usado en «⋯»; «Detalles» plegado; resumen y transcripción lado a lado con ancho; el aviso dice quién
        hizo el resumen y cuándo; el lateral se pliega solo por debajo de 1.000 px y con Ctrl+\\."""
        resumen = ("---\nmodelo: claude-sonnet-4-5\ngenerado: 2026-11-12 16:09\n---\n\n## Resumen\nPrimera línea del "
                   "resumen de prueba.\n\n## Decisiones\n- Una decisión\n")
        rel = self.nueva("R b5", {"h1": 10.0, "h2": 9.0}, md={"participantes": "Ana Prueba"}, resumen=resumen)
        for w, h in ((1500, 920), (900, 600)):
            ctx = self.browser.new_context(bypass_csp=True, viewport={"width": w, "height": h})
            self.addCleanup(ctx.close)
            pg = ctx.new_page()
            errores = []
            pg.on("pageerror", lambda e: errores.append(f"pageerror: {e}"))
            pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
            pg.wait_for_selector("#main h1", timeout=15000)
            pg.evaluate("p => { location.hash = '#/a/' + encodeURIComponent(p); }", rel)
            pg.wait_for_selector("#aSum .sumview p", timeout=15000)
            y = pg.evaluate("Math.round(document.querySelector('#aSum .sumview p').getBoundingClientRect().top)")
            self.assertLess(y, h - 60, (w, y))
            autor = pg.inner_text("#aSum .sum-autor")
            self.assertIn("por Claude (claude-sonnet-4-5) el 12 nov 2026 a las 16:09", autor)
            self.assertNotIn("no carga tu portátil", pg.inner_text("#aTabs"))
            self.assertFalse(pg.evaluate("document.querySelector('#aHead .pdet').open"))  # «Detalles», plegado
            self.assertFalse(pg.is_visible("#aHead [data-prop='participantes']"))
            # lo poco usado, en «⋯»; Escape lo cierra y devuelve el foco
            self.assertFalse(pg.is_visible("#aHead [data-act='delete']"))
            pg.click("#aHead [data-act='mas']")
            self.assertTrue(pg.is_visible("#aHead .pmenu [data-act='delete']"))
            pg.keyboard.press("Escape")
            self.assertFalse(pg.is_visible("#aHead .pmenu"))
            self.assertEqual(pg.evaluate("document.activeElement.dataset.act"), "mas")
            lado = pg.evaluate("""() => { const s = document.querySelector('#aSum').getBoundingClientRect(),
                t = document.querySelector('#aTx').getBoundingClientRect();
                return [t.width > 0 && t.left > s.right, t.height > 0]; }""")
            lateral = pg.evaluate("getComputedStyle(document.querySelector('.sidebar')).display")
            if w == 1500:
                self.assertEqual(lado, [True, True])  # lado a lado
                self.assertNotEqual(lateral, "none")
            else:
                self.assertEqual(lado, [False, False])  # una columna, con pestañas
                self.assertEqual(lateral, "none")  # plegado solo por debajo de 1.000 px
            pg.keyboard.press("Control+Backslash")
            lateral2 = pg.evaluate("getComputedStyle(document.querySelector('.sidebar')).display")
            self.assertEqual(lateral2 == "none", w == 1500, (w, lateral, lateral2))
            if w == 900:  # abierto encima del contenido; Escape lo cierra
                self.assertIn("lat-sobre", pg.evaluate("document.body.className"))
                pg.keyboard.press("Escape")
                self.assertEqual(pg.evaluate("getComputedStyle(document.querySelector('.sidebar')).display"), "none")
            pg.click("#aHead .pdet summary")
            self.assertTrue(pg.is_visible("#aHead [data-prop='participantes']"))
            self.assertEqual(errores, [])
            ctx.close()


if __name__ == "__main__":
    unittest.main()
