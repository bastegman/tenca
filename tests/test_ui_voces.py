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

from support import REPO, srt_text, wav_bytes, nuevo_contexto
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
        ctx = nuevo_contexto(self.browser, bypass_csp=True)
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
        ctx = nuevo_contexto(self.browser, bypass_csp=True, viewport={"width": 1280, "height": 800})
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        errores, red, personas = [], [], []
        pg.on("pageerror", lambda e: errores.append(f"pageerror: {e}"))
        pg.on("response", lambda r: red.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        pg.on("request", lambda r: personas.append(r.post_data) if "/api/voces/persona" in r.url else None)
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
        # oír: la frase más larga de esa voz (la transcripción llega por su lado: en la CI, a veces después del bloque)
        pg.wait_for_selector("#txBody .spk[data-spk='h3']", timeout=15000)
        self.assertEqual(pg.evaluate("qhMuestra('h3').t"), 20.5)
        # «Recordar» sin consentimiento no guarda nada y enseña la casilla
        fila = "#qhBloque .qh-fila[data-h='h2']"
        pg.click("#qhBloque [data-qh-sug='Ana Prueba']")
        self.assertEqual(pg.input_value(fila + " [data-qh-nombre]"), "Ana Prueba")
        pg.check(fila + " [data-qh-rec]")
        pg.click(fila + " [data-qh-guardar]")
        pg.wait_for_function("document.querySelector('#toasts').textContent.includes('consentimiento')", timeout=5000)
        self.assertEqual(personas, [])  # no ha salido ninguna petición
        hs = self.api("/api/hablantes?path=" + urllib.request.quote(rel))["hablantes"]
        self.assertIsNone(hs["h2"]["persona"])
        pg.uncheck(fila + " [data-qh-rec]")
        pg.click(fila + " [data-qh-guardar]")
        pg.wait_for_function("document.querySelector(\"#txBody .spk[data-spk='h2']\").textContent === 'Ana Prueba'",
                             timeout=10000)
        self.assertTrue(pg.is_visible("#qhBloque"))  # sigue abierto tras guardar
        self.assertEqual([json.loads(b)["consentimiento"] for b in personas], [False])
        # «Son la misma persona»: dos voces de una vez
        pg.check("#qhBloque .qh-fila[data-h='h1'] [data-qh-sel]")
        self.assertTrue(pg.is_disabled("#qhBloque [data-qh-unir]"))
        pg.check("#qhBloque .qh-fila[data-h='h4'] [data-qh-sel]")
        pg.click("#qhBloque [data-qh-unir]")
        pg.wait_for_function("!document.querySelector(\"#qhBloque .qh-fila[data-h='h4']\")", timeout=10000)
        hs = self.api("/api/hablantes?path=" + urllib.request.quote(rel))["hablantes"]
        self.assertEqual(sorted(hs), ["h1", "h2", "h3"])
        self.assertEqual(hs["h1"]["segundos"], 20.0)  # sus tramos y los de h4
        # lo escrito en una fila no se pierde al guardar otra, y el foco va a la siguiente sin nombre
        pg.fill("#qhBloque .qh-fila[data-h='h1'] [data-qh-nombre]", "Borrador")
        pg.fill("#qhBloque .qh-fila[data-h='h3'] [data-qh-nombre]", "Luis Prueba")
        pg.click("#qhBloque .qh-fila[data-h='h3'] [data-qh-guardar]")
        pg.wait_for_function("document.querySelector(\"#txBody .spk[data-spk='h3']\").textContent === 'Luis Prueba'",
                             timeout=10000)
        self.assertEqual(pg.input_value("#qhBloque .qh-fila[data-h='h1'] [data-qh-nombre]"), "Borrador")
        # si las voces se vuelven a identificar, el bloque se repinta con las nuevas (no guarda en la voz de antes)
        sc = os.path.join(self.root, "R quien" + voices.SIDECAR_EXT)
        d = voices.read_sidecar(sc)
        d["creado"] = "2026-11-15T11:00:00"
        voices.write_sidecar(sc, d)
        pg.evaluate("() => { S.spk = null; return loadSpeakers(curAudio()); }")
        nuevo = "document.querySelector('#qhBloque').dataset.creado === '2026-11-15T11:00:00'"
        pg.wait_for_function(nuevo, timeout=5000)
        # Escape cierra el globo de una etiqueta
        pg.click("#txBody .spk[data-spk='h3']")
        pg.wait_for_selector(".spk-pop", timeout=5000)
        pg.keyboard.press("Escape")
        pg.wait_for_selector(".spk-pop", state="detached", timeout=3000)
        self.assertEqual(self.limpio(errores, red), ([], []))

    def bloque(self, rel, w=1280, h=800, antes=None):
        """El bloque «Quién habla» de `rel`, abierto como desde «Ponles nombre» en Hoy."""
        ctx = nuevo_contexto(self.browser, bypass_csp=True, viewport={"width": w, "height": h})
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        errores, red = [], []
        pg.on("pageerror", lambda e: errores.append(f"pageerror: {e}"))
        pg.on("response", lambda r: red.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        if antes:
            antes(pg)
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1", timeout=15000)
        pg.wait_for_function("p => S.audios && S.audios.get(p)", arg=rel, timeout=10000)
        pg.evaluate("p => hoyAccion('voces', S.audios.get(p))", rel)
        pg.wait_for_selector("#qhBloque .qh-fila", timeout=15000)
        return pg, errores, red

    def test_juntar_no_deja_ninguna_marcada(self):
        """Revisión 2, N1: tras «Son la misma persona», la voz que queda seguía marcada y la siguiente unión se la
        llevaba también (sin deshacer)."""
        rel = self.nueva("R juntar", {"h1": 50.0, "h2": 40.0, "h3": 30.0, "h4": 20.0, "h5": 10.0})
        pg, errores, red = self.bloque(rel)
        fila = "#qhBloque .qh-fila[data-h='{}'] [data-qh-sel]"
        pg.check(fila.format("h1"))
        pg.check(fila.format("h4"))
        pg.click("#qhBloque [data-qh-unir]")
        pg.wait_for_function("!document.querySelector(\"#qhBloque .qh-fila[data-h='h4']\")", timeout=10000)
        self.assertEqual(pg.eval_on_selector_all("#qhBloque [data-qh-sel]:checked", "xs => xs.length"), 0)
        self.assertTrue(pg.is_disabled("#qhBloque [data-qh-unir]"))
        pg.check(fila.format("h2"))
        pg.check(fila.format("h3"))
        pg.click("#qhBloque [data-qh-unir]")
        pg.wait_for_function("document.querySelectorAll('#qhBloque .qh-fila').length === 3", timeout=10000)
        hs = self.api("/api/hablantes?path=" + urllib.request.quote(rel))["hablantes"]
        self.assertEqual(len(hs), 3, sorted(hs))
        self.assertIn("h5", hs)
        self.assertEqual(hs["h1"]["segundos"], 20.0)  # h1 y h4, nada más
        self.assertEqual(self.limpio(errores, red), ([], []))

    def test_consentimiento_y_borrador(self):
        """Revisión 2: el consentimiento no se queda marcado para la siguiente persona tras guardar con «Recordar»
        (N4), y un refresco de trabajos mientras se vuelven a leer las voces no quita el bloque ni lo escrito (N6)."""
        rel = self.nueva("R consiento", {"h1": 20.0, "h2": 10.0, "h3": 5.0})
        enviados = []

        def guarda(route):  # sin sherpa_onnx no se puede guardar una huella de verdad: la respuesta es simulada
            enviados.append(json.loads(route.request.post_data))
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "huellas": 1}))
        pg, errores, red = self.bloque(rel, antes=lambda p: p.route("**/api/voces/persona", guarda))
        fila = "#qhBloque .qh-fila[data-h='{}'] "
        pg.fill(fila.format("h1") + "[data-qh-nombre]", "Ana Prueba")
        pg.check(fila.format("h1") + "[data-qh-rec]")
        pg.check("#qhConsent")
        pg.click(fila.format("h1") + "[data-qh-guardar]")
        pg.wait_for_function("document.querySelector('#toasts').textContent.includes('huella guardada')", timeout=10000)
        pg.wait_for_function("!document.querySelector('#qhBloque').dataset.busy", timeout=5000)
        self.assertEqual([b["consentimiento"] for b in enviados], [True])
        self.assertFalse(pg.is_checked("#qhConsent"))
        pg.check(fila.format("h2") + "[data-qh-rec]")
        self.assertTrue(pg.is_visible("#qhConsent"))
        self.assertFalse(pg.is_checked("#qhConsent"))  # la siguiente persona lo pide otra vez
        pg.fill(fila.format("h2") + "[data-qh-nombre]", "Luis Prueba")
        pg.click(fila.format("h2") + "[data-qh-guardar]")
        pg.wait_for_function("document.querySelector('#toasts').textContent.includes('consentimiento')", timeout=5000)
        self.assertEqual(len(enviados), 1)
        # N6: S.spk a null (se están releyendo las voces) y llega un refresco de la barra
        pg.uncheck(fila.format("h2") + "[data-qh-rec]")
        pg.fill(fila.format("h3") + "[data-qh-nombre]", "Borrador")
        pg.evaluate("() => { document.activeElement.blur(); S.spk = null; renderVoicesBar(curAudio()); }")
        self.assertTrue(pg.is_visible("#qhBloque"))
        self.assertEqual(pg.input_value(fila.format("h3") + "[data-qh-nombre]"), "Borrador")
        # y al llegar las voces se repinta (sin `sucio`, como tras guardar) con lo escrito (revisión 3, N10)
        pg.evaluate("() => { const b = document.querySelector('#qhBloque'); b.dataset.viejo = '1'; "
                    "delete b.dataset.sucio; return loadSpeakers(curAudio()); }")
        pg.wait_for_function("(b => b && !b.dataset.viejo)(document.querySelector('#qhBloque'))", timeout=5000)
        self.assertEqual(pg.input_value(fila.format("h2") + "[data-qh-nombre]"), "Luis Prueba")
        self.assertEqual(pg.input_value(fila.format("h3") + "[data-qh-nombre]"), "Borrador")
        self.assertEqual(self.limpio(errores, red), ([], []))

    def test_oir_con_la_transcripcion_por_llegar_y_otra_frase(self):
        """«Oír»: la muestra la da el servidor (/api/hablantes/muestra, de los tramos del sidecar), también sin la
        transcripción. Sin él (una respuesta que no llega), la frase más larga de la transcripción: si aún no ha
        llegado lo dice (no que falte el .srt; era el fallo de ui-smoke en la CI). Pulsar otra frase mientras suena la
        muestra no la pausa (revisión 2, N5)."""
        nombre = "R oir"
        base = os.path.join(self.root, nombre)
        with open(base + ".wav", "wb") as f:
            f.write(wav_bytes(25.0, rate=8000, channels=1))
        with open(base + ".srt", "w", encoding="utf-8") as f:
            f.write(srt_text([(0.5, 9.5, "Hola a todos."), (10.5, 14.5, "Una frase corta."),
                              (15.0, 24.0, "Sigo yo.")]))
        voices.write_sidecar(base + voices.SIDECAR_EXT, {
            "version": 1, "tu": None, "micro": None, "creado": "2026-11-15T10:00:00",
            "tramos": [{"inicio": 0, "fin": 10, "h": "h1"}, {"inicio": 10, "fin": 14.8, "h": "h2"},
                       {"inicio": 14.8, "fin": 25, "h": "h1"}],
            "hablantes": {"h1": {"segundos": 20.2, "persona": None}, "h2": {"segundos": 4.8, "persona": None}}})
        rel = nombre + ".wav"
        retenidas = []  # la transcripción, retenida hasta después del primer «Oír»

        def retiene(route):
            if retenidas is None:
                route.continue_()
            else:
                retenidas.append(route)
        pg, errores, red = self.bloque(rel, antes=lambda p: p.route(lambda url: "/api/transcript?" in url, retiene))
        self.assertTrue(retenidas)
        # con el servidor: su muestra (10,0-14,8, de los tramos), aunque la transcripción no haya llegado
        pg.click("#qhBloque .qh-fila[data-h='h2'] [data-qh-oir]")
        pg.wait_for_function("qhOyendo && qhOyendo.t === 10 && Math.abs(qhOyendo.fin - 14.8) < 1e-6", timeout=5000)
        pg.evaluate("() => { audio.pause(); audio.currentTime = 0; }")  # un salto lejos: la app la olvida
        pg.wait_for_function("qhOyendo === null", timeout=5000)
        # sin él (la muestra no contesta: un servidor de antes, o colgado), la transcripción, que aún no ha llegado
        pg.route("**/api/hablantes/muestra*", lambda route: route.abort())
        pg.click("#qhBloque .qh-fila[data-h='h2'] [data-qh-oir]")
        pg.wait_for_function("document.querySelector('#toasts').textContent.includes('cargando la transcripción')",
                             timeout=5000)
        self.assertNotIn(".srt", pg.inner_text("#toasts"))
        sueltas, retenidas = retenidas, None
        for r in sueltas:
            r.continue_()
        pg.wait_for_selector("#txBody .spk[data-spk='h2']", timeout=15000)
        self.assertEqual(pg.evaluate("qhMuestra('h2')"), {"t": 10.5, "d": 4.0})
        pg.click("#qhBloque .qh-fila[data-h='h2'] [data-qh-oir]")
        pg.wait_for_function("audio.readyState >= 1 && audio.currentTime >= 10.2 && audio.currentTime < 14",
                             timeout=10000)
        self.assertEqual(pg.evaluate("qhOyendo && qhOyendo.t"), 10.5)  # la frase de la transcripción
        pg.unroute("**/api/hablantes/muestra*")
        # quieto (lo que tarde el clic no cuenta) y con la cuenta de pausas; la frase siguiente (15,0) cae dentro del
        # margen de la muestra (acaba en 14,5): antes, el salto contaba como «llegó al final» y la pausaba
        pg.evaluate("() => { audio.pause(); window.pausas = 0; const p = audio.pause.bind(audio); "
                    "audio.pause = () => { window.pausas++; p(); }; }")
        pg.click("#txBody .ts[data-i='2']")
        pg.wait_for_function("!audio.seeking && audio.currentTime >= 14.9 && audio.currentTime < 20", timeout=10000)
        pg.wait_for_timeout(400)  # el timeupdate del salto
        self.assertEqual(pg.evaluate("window.pausas"), 0)
        self.assertIsNone(pg.evaluate("qhOyendo"))
        self.assertEqual(self.limpio(errores, red), ([], []))

    def pagina(self, rel, w, h, ctx=None):
        if ctx is None:
            ctx = nuevo_contexto(self.browser, bypass_csp=True, viewport={"width": w, "height": h})
            self.addCleanup(ctx.close)
        pg = ctx.new_page()
        errores = []
        pg.on("pageerror", lambda e: errores.append(f"pageerror: {e}"))
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1", timeout=15000)
        self.ir(pg, rel)
        return pg, errores

    def ir(self, pg, rel):
        pg.evaluate("p => { location.hash = '#/a/' + encodeURIComponent(p); }", rel)
        pg.wait_for_function("p => curAudio() && curAudio().path === p && document.querySelector('#aHead .pdet')",
                             arg=rel, timeout=15000)

    def test_detalles_del_proyecto_no_se_quedan_para_todas(self):
        """Revisión 2, N2: abrir «Detalles» desde el proyecto de la cabecera lo abre en esa reunión; el primer repintado
        (Chromium lanza `toggle` al pintar un <details open>) lo guardaba para todas. Y plegarlo a mano después de
        ese clic se ignoraba."""
        una = self.nueva("R det una", {"h1": 10.0})
        otra = self.nueva("R det otra", {"h1": 10.0})
        pg, errores = self.pagina(una, 1280, 800)
        abierto = "document.querySelector('#aHead .pdet').open"
        guardado = "localStorage.getItem('detalles')"
        self.assertFalse(pg.evaluate(abierto))
        pg.click("#aHead [data-act='det-proy']")
        self.assertTrue(pg.evaluate(abierto))
        # un repintado normal de la cabecera (cualquier cambio de archivos o trabajos), con el foco fuera
        pg.evaluate("() => { document.activeElement.blur(); renderAudioHead(curAudio()); }")
        pg.wait_for_timeout(300)  # el `toggle` del <details open> recién pintado
        self.assertTrue(pg.evaluate(abierto))  # en esta reunión sigue abierto
        self.assertIsNone(pg.evaluate(guardado))
        self.ir(pg, otra)
        self.assertFalse(pg.evaluate(abierto))  # en las demás, no
        # abierto a mano (se recuerda), clic en el proyecto y plegado a mano: se recuerda plegado
        pg.click("#aHead .pdet summary")
        self.assertEqual(pg.evaluate(guardado), "1")
        pg.click("#aHead [data-act='det-proy']")
        pg.click("#aHead .pdet summary")
        self.assertEqual(pg.evaluate(guardado), "0")
        pg.evaluate("() => { document.activeElement.blur(); renderAudioHead(curAudio()); }")
        pg.wait_for_timeout(300)
        self.assertFalse(pg.evaluate(abierto))
        self.ir(pg, una)
        self.assertFalse(pg.evaluate(abierto))
        # con el teclado en su título también se recuerda; Espacio no pone a sonar la reunión (revisión 3, N9)
        pg.focus("#aHead .pdet summary")
        pg.keyboard.press("Enter")
        self.assertEqual(pg.evaluate(guardado), "1")
        pg.keyboard.press("Space")
        self.assertFalse(pg.evaluate(abierto))
        self.assertEqual(pg.evaluate(guardado), "0")
        self.assertIsNone(pg.evaluate("S.playing"))
        self.assertEqual(errores, [])

    def test_lateral_con_teclado_no_quita_el_foco(self):
        """Revisión 2, N3: en ancha, abrir el lateral con Ctrl+\\ llevaba el foco a «Hoy» y lo que escribías en Notas
        acababa navegando. En estrecha (se abre encima) el foco entra en el lateral y vuelve a donde estaba al
        cerrarlo, con Escape o con Ctrl+\\ (N8: se quedaba en <body>)."""
        for w, h in ((1280, 800), (900, 600)):
            rel = self.nueva(f"R lateral {w}", {"h1": 10.0})  # cada una con sus notas
            pg, errores = self.pagina(rel, w, h)
            pg.click("#aTabs [data-tab='notes']")
            pg.wait_for_selector("#aNotes .nt", timeout=10000)
            pg.click("#aNotes .nt")
            pg.keyboard.type("ab")
            en_notas = "!!document.activeElement.closest('#aNotes')"
            if w == 1280:
                pg.keyboard.press("Control+Backslash")  # lo pliega
                self.assertIn("lat-off", pg.evaluate("document.body.className"))
                pg.keyboard.type("cd")
                pg.keyboard.press("Control+Backslash")  # lo abre: el foco no se mueve
                self.assertNotIn("lat-off", pg.evaluate("document.body.className"))
                self.assertTrue(pg.evaluate(en_notas))
                pg.keyboard.type("ef")
                self.assertEqual(pg.evaluate("document.querySelector('#aNotes .nt').textContent"), "abcdef")
            else:
                for cerrar in ("Escape", "Control+Backslash"):
                    pg.keyboard.press("Control+Backslash")  # se abre encima: el foco, al lateral
                    self.assertIn("lat-sobre", pg.evaluate("document.body.className"))
                    self.assertTrue(pg.evaluate("!!document.activeElement.closest('#sidebar')"))
                    pg.keyboard.press(cerrar)
                    self.assertIn("lat-off", pg.evaluate("document.body.className"), cerrar)
                    self.assertTrue(pg.evaluate(en_notas), cerrar)  # y vuelve a las notas
                pg.keyboard.type("cd")
                self.assertEqual(pg.evaluate("document.querySelector('#aNotes .nt').textContent"), "abcd")
                # encima, un clic en la reunión en la que ya estás (no cambia la ruta) también lo cierra (N8)
                pg.click("#btnSide")
                self.assertIn("lat-sobre", pg.evaluate("document.body.className"))
                pg.click(f"#tree .trow[data-audio='{rel}']")
                self.assertIn("lat-off", pg.evaluate("document.body.className"))
            self.assertEqual(errores, [])

    def test_pagina_de_la_reunion_compacta(self):
        """B5 (puntos 13 y 22 del review): el resumen se ve sin bajar a 1500×920 y a 900×600 (antes, y = 805 y 932);
        lo poco usado en «⋯»; «Detalles» plegado; resumen y transcripción lado a lado con ancho; el aviso dice quién
        hizo el resumen y cuándo; el lateral se pliega solo por debajo de 1.000 px y con Ctrl+\\."""
        resumen = ("---\nmodelo: claude-sonnet-4-5\ngenerado: 2026-11-12 16:09\n---\n\n## Resumen\nPrimera línea del "
                   "resumen de prueba.\n\n## Decisiones\n- Una decisión\n")
        rel = self.nueva("R b5", {"h1": 10.0, "h2": 9.0}, md={"participantes": "Ana Prueba"}, resumen=resumen)
        for w, h in ((1500, 920), (900, 600)):
            ctx = nuevo_contexto(self.browser, bypass_csp=True, viewport={"width": w, "height": h})
            self.addCleanup(ctx.close)
            pg = ctx.new_page()
            errores = []
            pg.on("pageerror", lambda e: errores.append(f"pageerror: {e}"))
            pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
            pg.wait_for_selector("#main h1", timeout=15000)
            pg.evaluate("p => { location.hash = '#/a/' + encodeURIComponent(p); }", rel)
            pg.wait_for_selector("#aSum .sumview p", timeout=15000)
            y = pg.evaluate("Math.round(document.querySelector('#aSum .sumview p').getBoundingClientRect().top)")
            self.assertLess(y, h * 0.8, (w, y))  # antes: 805 y 932
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
