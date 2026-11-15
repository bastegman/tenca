# -*- coding: utf-8 -*-
"""Humo de la interfaz en un navegador real (Playwright + Chromium): la app arranca, se abre con su token y lo que
Bernardo pulsa primero (⚙ Ajustes, Reuniones, Calendario, Inicio, ● Grabar) responde sin errores de JavaScript.

Hasta el 02/11/2026 ningún test abría la interfaz en un navegador: una pantalla con los botones muertos pasaba la
suite entera. Se salta si Playwright no está instalado (la CI lo instala en `ui-smoke.yml`):

    pip install playwright && python -m playwright install chromium
"""
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from support import REPO

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - sin Playwright el test se salta
    sync_playwright = None

SERVER = os.path.join(REPO, "_app", "server.py")
# sin soundcard (fuera de Windows, y en ui-smoke.yml también en Windows: no lo instala) la grabadora no lista los
# dispositivos y /api/rec/devices contesta 400: no es un fallo de la interfaz. Hasta el 08/11/2026 se decidía por el
# sistema y en Windows no se permitía: pasaba porque nadie esperaba esa respuesta (el diálogo de grabar se cerraba
# antes de que llegara); Ajustes › Al grabar la pide al abrirse y espera lo bastante (f5366aa, run 42421762235)
PERMITIDAS = ("/api/rec/devices",) if importlib.util.find_spec("soundcard") is None else ()


def puerto_libre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@unittest.skipUnless(sync_playwright, "playwright no instalado")
class InterfazEnNavegadorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="chuleta-ui-")
        cls.root = os.path.join(cls.tmp, "Chuleta")
        shutil.copytree(os.path.join(REPO, "_app"), os.path.join(cls.root, "_app"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        os.makedirs(os.path.join(cls.root, "Proyecto", "Vocabulario"))
        with open(os.path.join(cls.root, "Proyecto", "Vocabulario", "General.txt"), "w", encoding="utf-8") as f:
            f.write("término uno, término dos\n")
        # una reunión ya grabada, para el autocompletado de la tarjeta de grabar (su proyecto sale de su cabecera)
        base = os.path.join(cls.root, "Proyecto", "2026-10-30 10-00-00 - Comité de prueba")
        with open(base + ".wav", "wb") as f:
            f.write(b"RIFF" + (36 + 32000).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little")
                    + (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + (16000).to_bytes(4, "little")
                    + (32000).to_bytes(4, "little") + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
                    + b"data" + (32000).to_bytes(4, "little") + b"\0" * 32000)
        with open(base + ".md", "w", encoding="utf-8") as f:
            f.write("---\nproyecto: Proyecto\nworkstream: General\n---\n")
        cls.datos = os.path.join(cls.tmp, "datos")
        cls.port = puerto_libre()
        cls.log = open(os.path.join(cls.tmp, "salida.log"), "w", encoding="utf-8")
        # elegir un modo que transcribe en el PC empieza a bajar Whisper medium (1,5 GB): aquí, contra un puerto
        # cerrado (falla al instante); la descarga de verdad la prueba e2e-procesado.yml
        env = dict(os.environ, PYTHONIOENCODING="utf-8",
                   CHULETA_PRUEBAS_URL_MODELOS="http://127.0.0.1:9/{repo}/resolve/{rev}/{file}")
        cls.proc = subprocess.Popen([sys.executable, os.path.join(cls.root, "_app", "server.py"), "--port",
                                     str(cls.port), "--data", cls.datos, "--no-browser"],
                                    cwd=cls.root, stdout=cls.log, stderr=subprocess.STDOUT, env=env)
        cls.base = f"http://127.0.0.1:{cls.port}"
        token_file = os.path.join(cls.datos, "sesion.token")
        fin = time.time() + 60
        cls.token = None
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

    def pagina(self):
        # cookies propias (la sesion de un test no vale para el siguiente) y sin la CSP de la pagina, que bloquea el
        # eval con el que Playwright evalua las expresiones de wait_for_function (la CSP la prueba test_seguridad)
        ctx = self.browser.new_context(bypass_csp=True)
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        errores, red = [], []
        pg.on("console", lambda m: errores.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        pg.on("pageerror", lambda e: errores.append(f"pageerror: {e}"))
        pg.on("response", lambda r: red.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        pg.on("requestfailed", lambda r: red.append(f"fallo {r.url}"))
        self.addCleanup(pg.close)
        return pg, errores, red

    def limpiar(self, errores, red):
        """Errores que cuentan: todo menos los 4xx permitidos (y su eco en la consola)."""
        red = [r for r in red if not any(p in r for p in PERMITIDAS)]
        err = [e for e in errores if not (not red and "Failed to load resource" in e)]
        return err, red

    def test_arranque_y_lo_primero_que_se_pulsa(self):
        pg, errores, red = self.pagina()
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        self.assertEqual(pg.url, self.base + "/")  # ?t= entrega la cookie y redirige
        self.assertEqual(pg.title(), "Tenca")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.click("#btnSettings")
        pg.wait_for_selector("#modal:not(.hidden) h3:has-text('Ajustes')", timeout=5000)
        # «Arrancar con Windows» (bandeja): sin la app instalada (aquí, y fuera de Windows) la casilla sale desactivada
        # con su nota; la API de verdad contesta (GET /api/inicio-windows)
        pg.wait_for_selector("#modal #sInicioWin", state="attached", timeout=5000)
        nota = "document.querySelector('#sInicioWinNota').textContent.includes('solo con la app instalada')"
        pg.wait_for_function(nota, timeout=10000)
        self.assertTrue(pg.evaluate("document.querySelector('#sInicioWin').disabled"))
        # Ajustes abre en «Al grabar», con el plan del servidor arriba (rediseño B2, 07/11/2026)
        pg.wait_for_selector(".aj-nav [data-sec='algrabar'].on", timeout=5000)
        pg.wait_for_selector("#agFilas .gr-fila[data-fila='carpeta']", timeout=10000)
        self.assertIn("Así se graba ahora", pg.text_content("[data-secc='algrabar']"))
        pg.click(".aj-nav [data-sec='procesado']")
        # «Dónde se procesan tus reuniones» (punto 37 del review): la tarjeta con lo que hay ahora, «Probar» y
        # «Cambiar…»; las tres opciones y sus avisos, en el asistente
        pg.wait_for_selector("#modal .cfg-estado:has-text('Solo en este PC')", timeout=5000)
        self.assertEqual(pg.locator("#modal input[name='proc']").count(), 0)
        self.assertNotIn("se envía a Anthropic", pg.inner_text("#modal"))
        self.assertTrue(pg.is_visible("#lPreset"))  # los motores, en Procesado
        pg.keyboard.press("Escape")
        pg.wait_for_function("document.querySelector('#modal').classList.contains('hidden')", timeout=5000)
        pg.click("[data-nav='r']")
        pg.wait_for_function("location.hash === '#/r/'", timeout=5000)
        pg.wait_for_selector("#main .page", timeout=5000)
        pg.click("[data-nav='c']")
        pg.wait_for_function("location.hash === '#/c/'", timeout=5000)
        pg.click("[data-nav='home']")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=5000)
        pg.click("#btnRec")
        pg.wait_for_selector("#modal:not(.hidden) .dialog.gr", timeout=5000)
        self.assertIn("Nueva grabación", pg.text_content("#modal"))
        pg.wait_for_selector(".dialog.gr [data-fila='carpeta'] .gr-v code", timeout=15000)  # el plan del servidor
        pg.keyboard.press("Escape")
        pg.wait_for_function("document.querySelector('#modal').classList.contains('hidden')", timeout=5000)
        self.assertTrue(pg.evaluate("document.querySelector('#meetChip').classList.contains('hidden')"))
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [], red)

    def test_asistente_configurar_tres_pasos_y_probar_sin_guardar(self):
        """«Configurar Chuleta» (07/11/2026): tres pasos, «Probar» de verdad (aquí falla en la descarga del modelo,
        contra un puerto cerrado, con su frase y su botón) y nada se guarda hasta terminar."""
        pg, errores, red = self.pagina()
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        antes = pg.evaluate("api('/api/config')")["procesado"]
        pg.evaluate("abrirConfigurar()")
        pg.wait_for_selector(".dialog.wz h2:has-text('¿Dónde se procesan tus reuniones?')", timeout=5000)
        # el botón de radio, a la izquierda y en la línea del título de su opción, no solo encima (punto 45 del review)
        cajas = pg.evaluate("""() => [...document.querySelectorAll('.dialog.wz .wz-opt')].map(o => {
          const r = o.querySelector('input').getBoundingClientRect(), t = o.querySelector('.t').getBoundingClientRect();
          return [r.right <= t.left, r.top + r.height / 2 > t.top && r.top + r.height / 2 < t.bottom]; })""")
        self.assertEqual(cajas, [[True, True]] * 3, cajas)
        pg.check("input[name='wzModo'][value='agente']")
        self.assertIn("se envía a Anthropic", pg.inner_text(".dialog.wz"))
        pg.click("[data-wz='sig']")
        pg.wait_for_selector(".dialog.wz h2:has-text('Conecta Claude Code')", timeout=5000)
        self.assertIn("irm https://claude.ai/install.ps1 | iex", pg.inner_text(".dialog.wz"))
        pg.click("[data-wz='atras']")
        pg.check("input[name='wzModo'][value='local']")
        pg.click("[data-wz='sig']")
        pg.wait_for_selector(".dialog.wz h2:has-text('de transcripción')", timeout=5000)  # el modelo o los modelos
        pg.click("[data-wz='sig']")
        pg.wait_for_selector(".dialog.wz h2:has-text('Prueba que todo funciona')", timeout=5000)
        pg.click(".wz-probar [data-wz='probar']")
        # en la CI falla la descarga (puerto cerrado: «Reintentar»); en un PC con los modelos ya bajados y sin Whisper
        # instalado, la transcripción («Copiar diagnóstico»). En los dos casos: una frase y su botón
        pg.wait_for_selector(".wz-res.err [data-wz-accion]", timeout=90000)
        self.assertTrue(pg.inner_text(".wz-res.err").strip())
        self.assertTrue(pg.is_disabled("[data-wz='terminar']"))
        self.assertEqual(pg.evaluate("api('/api/config')")["procesado"], antes)  # una prueba no guarda nada
        pg.keyboard.press("Escape")
        pg.wait_for_function("document.querySelector('#modal').classList.contains('hidden')", timeout=5000)
        # Ajustes por secciones: cambiar de opción se hace en el asistente («Cambiar…», punto 37 del review), y lo demás
        # se guarda al cambiarlo, también en una sección oculta al abrir (el 07/11/2026 una variable «api» tapó la
        # función api() en todo Ajustes y solo lo vio la e2e de Windows)
        pg.click("#btnSettings")
        pg.wait_for_selector("#modal:not(.hidden) .aj-nav", timeout=5000)
        pg.click(".aj-nav [data-sec='procesado']")
        self.assertEqual(pg.locator("#modal input[name='proc']").count(), 0)
        pg.click("#modal [data-cfg='cambiar']")
        pg.wait_for_selector(".dialog.wz h2:has-text('¿Dónde se procesan tus reuniones?')", timeout=5000)
        pg.keyboard.press("Escape")
        pg.wait_for_selector("#modal:not(.hidden) .aj-nav [data-sec='procesado'].on", timeout=5000)
        hilos = pg.evaluate("api('/api/config')")["threads"]
        pg.click(".aj-nav [data-sec='avanzado']")
        pg.fill("#sThreads", "2")
        pg.press("#sThreads", "Tab")
        self.guardado_junto_a(pg, "#sThreads")
        self.assertEqual(pg.evaluate("api('/api/config')")["threads"], 2)  # guardado desde una sección oculta al abrir
        # (con setConfig: si no, el siguiente Ajustes ve otro config_rev y se repinta solo, como con otra ventana)
        pg.evaluate("n => api('/api/settings', {threads: n}).then(setConfig)", hilos)
        pg.keyboard.press("Escape")
        # ⚙ abre en «Al grabar» (rediseño B2); volver de un subdiálogo (openSettings sin sección), en la última vista
        pg.click("#btnSettings")
        pg.wait_for_selector("#modal:not(.hidden) .aj-nav [data-sec='algrabar'].on", timeout=5000)
        pg.click(".aj-nav [data-sec='avanzado']")
        pg.evaluate("openSettings()")
        pg.wait_for_selector("#modal:not(.hidden) .aj-nav [data-sec='avanzado'].on", timeout=5000)
        # un enlace de dentro («Proyectos» en la ayuda, el origen de una línea…) lleva el foco a su sección del menú
        pg.click(".aj-nav [data-sec='algrabar']")
        pg.click(".ag-como-btn")
        pg.focus("#agComo [data-sec-ir='proyectos']")
        pg.keyboard.press("Enter")
        self.assertTrue(pg.evaluate("document.activeElement.matches(\".aj-nav [data-sec='proyectos'].on\")"))
        pg.keyboard.press("Escape")
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [])

    def test_tarjeta_de_grabar(self):
        """La tarjeta «Así se va a grabar» (rediseño B1, 07/11/2026): cabe en 900×600, cada línea dice de dónde sale,
        la reunión que llega tarde no pisa lo que has escrito (P2-30, D4), «Usarlo en» guarda para el proyecto y
        «Empezar a grabar» manda a /api/rec/start exactamente el plan que se ve."""
        pg, errores, red = self.pagina()
        pg.set_viewport_size({"width": 900, "height": 600})
        devs = {"mics": [{"id": "m1", "name": "Micro de prueba", "default": True}], "mic": "default", "sys": "all",
                "outputs": [{"id": "o1", "name": "Altavoces de prueba", "default": True}]}
        pg.route("**/api/rec/devices*", lambda r: r.fulfill(json=devs))
        pg.route("**/api/rec/monitor", lambda r: r.fulfill(json={"ok": True}))  # sin grabadora de verdad
        retenidas, enviados = [], []
        pg.route("**/api/reunion/actual", lambda r: retenidas.append(r))  # la reunión «tarda» (Outlook)

        def start(route):
            enviados.append(json.loads(route.request.post_data))
            route.fulfill(json={"error": "grabadora de prueba"}, status=400)  # no se graba: solo se mira el cuerpo
        pg.route("**/api/rec/start", start)
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.click("#btnRec")
        pg.wait_for_selector(".dialog.gr .gr-cargando", timeout=5000)  # «Buscando la reunión de Teams…»
        # no espera a la reunión más de ~2 s: se pinta el plan igualmente
        pg.wait_for_selector(".dialog.gr [data-fila='carpeta'] .gr-v code", timeout=8000)
        pg.wait_for_function("!document.querySelector('#rGo').disabled", timeout=5000)
        vis = pg.evaluate("[...document.querySelectorAll('.gr-fila:not(.hidden)')].map(f => f.dataset.fila)")
        self.assertEqual(vis, ["carpeta", "idioma", "vocab", "transcribe", "resume", "audio"])
        for f in vis:
            self.assertTrue(pg.text_content(f".gr-fila[data-fila='{f}'] .gr-origen").strip(), f)
        b = pg.evaluate("(() => { const r = document.querySelector('#rGo').getBoundingClientRect();"
                        " return [r.top, r.bottom, innerHeight]; })()")
        self.assertTrue(0 <= b[0] and b[1] <= b[2], b)  # «Empezar a grabar» a la vista en 900×600
        # el autocompletado: Intro elige la reunión y trae su proyecto, pero no empieza a grabar (revisión de B1)
        pg.fill("#grNombre", "")
        # «Comité de pr»: con «Comit», otra reunión de la suite (la de «Copiar para correo», más reciente) iba primero
        pg.type("#grNombre", "Comité de pr")
        pg.wait_for_selector("#grSug:not(.hidden) [data-sug-titulo]", timeout=5000)
        pg.keyboard.press("ArrowDown")
        pg.keyboard.press("Enter")
        pg.wait_for_function("document.querySelector('#grMotivo').textContent.includes('como la última')", timeout=5000)
        self.assertEqual(pg.input_value("#grNombre"), "Comité de prueba")
        self.assertEqual(pg.input_value("#grProy"), "Proyecto/Vocabulario/General.txt")
        self.assertTrue(pg.is_visible("#modal .dialog.gr"))
        self.assertEqual(enviados, [])
        # escribes un nombre; luego llega la reunión de Teams: se propone, no se pisa
        pg.fill("#grNombre", "Mi reunión")
        sug = {"name": "Comité", "source": "teams", "vocab": "", "participants": []}
        retenidas[0].fulfill(json={"teams": {"title": "Comité"}, "outlook": None, "reason": None, "sugerencia": sug})
        pg.wait_for_selector("#grPropuesta:not(.hidden)", timeout=5000)
        self.assertIn("Teams dice «Comité»", pg.inner_text("#grPropuesta"))
        self.assertEqual(pg.input_value("#grNombre"), "Mi reunión")
        # proyecto y, con «Usarlo en», el idioma para todas sus reuniones
        pg.select_option("#grProy", "Proyecto/Vocabulario/General.txt")
        pg.wait_for_function("document.querySelector('[data-fila=vocab] .gr-origen').textContent === 'del workstream'",
                             timeout=5000)
        pg.click("[data-gr-cambiar='idioma']")
        pg.select_option("[data-panel='idioma'] select", "pt")
        pg.wait_for_function("document.querySelector('[data-fila=idioma] .gr-origen').classList.contains('cambiado')",
                             timeout=5000)
        # con un idioma fijo, la nota de que lo que se diga en otro se traduce a este (msg 1150, ítem 12)
        pg.wait_for_selector("[data-fila='idioma'] .gr-nota", timeout=5000)
        self.assertIn("lo que se diga en otro se traduce a este", pg.inner_text("[data-fila='idioma'] .gr-nota"))
        pg.check("input[name='grAlc_idioma'][value='proyecto']")
        pg.click("[data-gr-listo='idioma']")
        pg.wait_for_function("document.querySelector('[data-fila=idioma] .gr-origen').textContent === 'de Proyecto'",
                             timeout=5000)
        self.assertEqual(pg.evaluate("api('/api/config')")["project_langs"], {"Proyecto": "pt"})
        # lo que se ve es lo que se usa: el cuerpo de /api/rec/start es el plan pintado
        plan = pg.evaluate("G.plan")
        pg.click("#rGo")
        pg.wait_for_function("document.querySelector('#toasts').innerText.includes('grabadora de prueba')",
                             timeout=5000)
        self.assertEqual(enviados[0], dict(plan["rec"], resumen=plan["resumen"]))
        self.assertEqual((enviados[0]["name"], enviados[0]["language"], enviados[0]["vocab"]),
                         ("Mi reunión", "pt", "Proyecto/Vocabulario/General.txt"))
        pg.evaluate("api('/api/grabar/recordar', {clave: 'idioma', valor: null, alcance: 'proyecto',"
                    " vocab: 'Proyecto/Vocabulario/General.txt'})")  # deja los ajustes como estaban
        pg.keyboard.press("Escape")
        err, red = self.limpiar(errores, [r for r in red if "/api/rec/start" not in r])
        self.assertEqual([e for e in err if "grabadora de prueba" not in e and "400" not in e], [])
        self.assertEqual(red, [])

    def test_ajustes_ocultar_al_compartir(self):
        """«Al compartir pantalla» en Ajustes › Calendario y Teams (ítem 18, msg 1242). Aquí no hay ventana propia (el
        servidor va con --no-browser): el interruptor sale apagado y dice por qué. Con un servidor que sí puede
        (respuesta simulada), marcarlo lo aplica al momento y dice «Guardado ✓» a su lado."""
        pg, errores, red = self.pagina()
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.click("#btnSettings")
        pg.click(".aj-nav [data-sec='calendario']")
        pg.wait_for_selector("#agOcultar", state="visible", timeout=10000)
        self.assertTrue(pg.is_disabled("#agOcultar"))
        nota = pg.inner_text("#agOcultarNota")
        self.assertTrue("Solo en Windows" in nota or "ventana propia" in nota, nota)
        st = pg.evaluate("api('/api/ocultar-captura')")
        self.assertEqual((st["activado"], st["soportado"]), (True, False))
        pg.keyboard.press("Escape")
        # un Windows con la ventana propia: el servidor contesta como lo haría (aplicada / quitada)
        enviados = []

        def ocultar(route):
            b = json.loads(route.request.post_data)
            enviados.append(b)
            route.fulfill(json={"activado": b["activado"], "soportado": True, "motivo": None,
                                "aplicado": b["activado"], "error": None})
        pg.route("**/api/ocultar-captura", ocultar)
        pg.evaluate("S.config.ocultar_captura = {activado: true, soportado: true, motivo: null, aplicado: true}")
        pg.click("#btnSettings")
        pg.click(".aj-nav [data-sec='calendario']")
        pg.wait_for_selector("#agOcultar:not([disabled])", state="visible", timeout=10000)
        self.assertTrue(pg.is_checked("#agOcultar"))
        self.assertIn("Tú la sigues viendo", pg.inner_text("#agOcultarNota"))
        pg.click("#agOcultar")
        self.guardado_junto_a(pg, "#agOcultar")  # como todo Ajustes (punto 12 del review): «Guardado ✓» a su lado
        self.assertEqual(enviados, [{"activado": False}])
        self.assertFalse(pg.is_checked("#agOcultar"))
        pg.click("#agOcultar")
        pg.wait_for_function("document.querySelector('#agOcultar').checked", timeout=5000)
        self.guardado_junto_a(pg, "#agOcultar")
        self.assertEqual(enviados[-1], {"activado": True})
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [])
        self.assertEqual(red, [])

    def test_ajustes_al_grabar_y_proyectos(self):
        """Ajustes (rediseño B2, 07/11/2026): abre en «Al grabar» con el plan del servidor arriba, que cambia con lo
        que se elige (y se guarda al momento, punto 12 del review); «Proyectos» guarda el idioma de un proyecto en su
        fila y lo dice como suyo;
        «Calendario y Teams» dice el estado de Teams en una frase."""
        pg, errores, red = self.pagina()
        # con la rama fix-calendario: el Outlook clásico instalado y apagado (aquí no lo hay), y «Activar» que responde
        apagado = {"nivel": 1, "estado": "apagado", "frase": "Tienes el Outlook clásico: activa el calendario.",
                   "accion": {"id": "activar", "texto": "Activar"}, "activado": False}
        activados = []

        def activar(route):
            b = json.loads(route.request.post_data)
            activados.append(b)
            route.fulfill(json=dict(apagado, estado="conectado", frase="Outlook clásico: conectado", accion=None,
                                    activado=True) if b.get("activar") else apagado)
        pg.route("**/api/calendario/estado*", lambda r: r.fulfill(json=apagado))
        pg.route("**/api/calendario/activar", activar)
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.click("#btnSettings")
        pg.wait_for_selector("#agFilas .gr-fila[data-fila='idioma']", timeout=10000)
        if PERMITIDAS:  # sin grabadora la lista de dispositivos no llega: una frase, no el error de Python
            pg.wait_for_selector("#agDevsAviso.gr-aviso", timeout=30000)
            aviso = pg.inner_text("#agDevsAviso")
            self.assertIn("No se ha podido leer la lista de micrófonos", aviso)
            self.assertNotIn("Traceback", aviso)
            self.assertNotIn("soundcard", aviso)
        antes = pg.evaluate("api('/api/config')")["language"]
        otro = "pt" if antes != "pt" else "de"
        pg.select_option("#sLang", otro)
        nombre = {"pt": "Portugués", "de": "Alemán"}[otro]
        fila = "document.querySelector('#agFilas [data-fila=idioma] .gr-v').textContent"
        pg.wait_for_function(f"{fila}.includes('{nombre}')", timeout=5000)
        self.guardado_junto_a(pg, "#sLang")  # guardado al momento (punto 12 del review)
        self.assertEqual(pg.evaluate("api('/api/config')")["language"], otro)
        pg.select_option("#sLang", antes)  # se deja como estaba
        self.guardado_junto_a(pg, "#sLang")
        # el plan vuelve a lo de antes antes de seguir: nada de lo de abajo depende de una petición que llegue tarde
        pg.wait_for_function(f"!{fila}.includes('{nombre}')", timeout=5000)
        pg.click(".aj-nav [data-sec='proyectos']")
        pg.wait_for_selector("#pjTabla [data-pj-editar='Proyecto']", timeout=10000)
        # la carpeta, del mismo cálculo que la tarjeta: sin asignar, la suya del árbol (11a′), en gris
        celda = "document.querySelector('#pjTabla tr[data-pj=\"Proyecto\"] td:nth-child(2)')?.innerText || ''"
        pg.wait_for_function(f"({celda}).includes('su carpeta')", timeout=10000)
        self.assertIn("Proyecto\\", pg.evaluate(celda))
        self.assertNotIn("la última usada", pg.evaluate(celda))
        pg.click("#pjTabla [data-pj-editar='Proyecto']")
        self.assertTrue(pg.evaluate("document.activeElement.matches('#pjTabla tr.pj-ed select')"))  # foco en la fila
        self.assertIn("Sin asignar (su carpeta", pg.inner_text("#pjTabla [data-pj-f]"))
        pg.select_option("#pjTabla [data-pj-l]", "en")
        # se guarda al momento, sin ningún «Guardar» (revisión de B2: el de abajo lo perdía)
        pg.wait_for_function("document.querySelector('#pjTabla .pj-estado')?.textContent === 'Idioma guardado'",
                             timeout=5000)
        self.assertEqual(pg.evaluate("api('/api/config')")["project_langs"], {"Proyecto": "en"})
        pg.click("#pjTabla [data-pj-no]")  # «Listo»
        fila_p = "document.querySelector('#pjTabla tr[data-pj=\"Proyecto\"]')?.innerText"
        pg.wait_for_function(f"{fila_p}.includes('Solo inglés')", timeout=5000)
        self.assertTrue(pg.evaluate("document.activeElement.matches(\"[data-pj-editar='Proyecto']\")"))
        pg.click(".aj-nav [data-sec='algrabar']")
        origen = "document.querySelector('#agFilas [data-fila=idioma] .gr-origen').textContent"
        pg.wait_for_function(f"{origen} === 'de Proyecto'", timeout=5000)
        pg.click(".aj-nav [data-sec='calendario']")
        self.assertTrue(pg.inner_text("#ctTeamsEstado").strip())
        # Outlook una sola vez, en su fila: la casilla de reunion.js no se enseña y solo hay un «Activar» o un
        # «Desactivar» a la vista (revisión 2, P1-5). Con fix-calendario la frase es su #rnEstado y guardan al momento
        # (aquí, con su estado simulado: en este PC no hay Outlook clásico)
        self.assertFalse(pg.is_visible("#rnOutlook"))
        self.assertTrue(pg.is_visible("#ctOutFila"))
        botones = ("[...document.querySelectorAll('#ctOutAcc button, #rnEstado button, label:has(#rnOutlook)')]"
                   ".filter(b => b.offsetParent && /Activar|Desactivar|Leer mi calendario/.test(b.textContent))"
                   ".map(b => b.textContent.trim())")
        acc = "document.querySelector('#ctOutAcc').innerText"
        if pg.evaluate("!!document.querySelector('#rnEstado')"):
            pg.wait_for_function(f"{acc}.includes('Activar')", timeout=5000)
            self.assertEqual(pg.evaluate(botones), ["Activar"])
            self.assertEqual(pg.inner_text("#ctOutEstado"), "Apagado")
            self.assertTrue(pg.is_visible("#ctOutFila #rnEstado"))
            self.assertEqual(pg.get_attribute("#ctOutAcc [data-ct-outlook]", "aria-label"),
                             "Activar el calendario de Outlook")
            # con el teclado: el foco sigue en el botón de la fila, también tras activar (revisión 3, P1-c)
            pg.focus("#ctOutAcc [data-ct-outlook='1']")
            pg.keyboard.press("Enter")
            pg.wait_for_function(f"{acc}.includes('Desactivar')", timeout=5000)
            pg.wait_for_function("document.activeElement.matches(\"#ctOutAcc [data-ct-outlook='0']\")", timeout=5000)
            self.assertEqual(pg.evaluate(botones), ["Desactivar"])
            self.assertTrue(pg.is_checked("#rnOutlook"))
            self.assertEqual(pg.inner_text("#ctOutEstado"), "Activado")
            pg.click("#ctOutAcc [data-ct-outlook='0']")
            pg.wait_for_function(f"{acc}.includes('Activar')", timeout=5000)
            self.assertFalse(pg.is_checked("#rnOutlook"))
            self.assertEqual([c["activar"] for c in activados], [True, False])
            # la casilla de Outlook no se guarda por su cuenta (lo hacen «Activar» y «Desactivar»): no está en AJ_GUARDA
            self.assertFalse(pg.evaluate("'rnOutlook' in AJ_GUARDA"))
        ayuda = "[data-secc='calendario'] .ct-filas + details.ag-como .help"
        self.assertTrue(pg.evaluate(f"!!document.querySelector(\"{ayuda}\")"))  # la ayuda larga existe, plegada
        self.assertFalse(pg.is_visible(ayuda))
        # el aviso flotante depende de «Llamada detectada» (Al grabar): apagada, no se puede marcar
        self.assertFalse(pg.is_disabled("#rnAviso"))
        pg.click(".aj-nav [data-sec='algrabar']")
        pg.uncheck("#lMeet")
        self.guardado_junto_a(pg, "#lMeet")
        pg.click(".aj-nav [data-sec='calendario']")
        self.assertTrue(pg.is_disabled("#rnAviso"))
        pg.click(".aj-nav [data-sec='algrabar']")
        pg.check("#lMeet")
        self.guardado_junto_a(pg, "#lMeet")
        pg.evaluate("api('/api/grabar/recordar', {clave: 'idioma', valor: null, alcance: 'proyecto',"
                    " vocab: 'Proyecto/Vocabulario/General.txt'})")
        pg.keyboard.press("Escape")
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [])

    def test_proyectos_lo_de_una_fila_no_se_pierde(self):
        """Revisión de B2 (09/11/2026, P0-1): una fila editada y Ajustes cerrado justo después: lo de la fila se guarda
        igual (al momento). Y la carpeta de un workstream también se elige en su fila."""
        pg, errores, red = self.pagina()
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.click("#btnSettings")
        pg.click(".aj-nav [data-sec='proyectos']")
        pg.wait_for_selector("#pjTabla [data-pj-editar='Proyecto']", timeout=10000)
        pg.click("#pjTabla [data-pj-editar='Proyecto']")
        pg.select_option("#pjTabla [data-pj-l]", "pt")
        pg.click("#modal [data-m='cerrar']")  # «Cerrar» enseguida: lo de la fila se guarda igual
        pg.wait_for_function("document.querySelector('#modal').classList.contains('hidden')", timeout=5000)
        pg.wait_for_function("api('/api/config').then(c => c.project_langs?.Proyecto === 'pt')", timeout=5000)
        # la carpeta de un workstream
        ws = "Proyecto/Vocabulario/General.txt"
        pg.click("#btnSettings")
        pg.click(".aj-nav [data-sec='proyectos']")
        pg.wait_for_selector(f"#pjTabla [data-pjw-editar='{ws}']", timeout=10000)
        pg.click(f"#pjTabla [data-pjw-editar='{ws}']")
        self.assertIn("La del proyecto", pg.inner_text("#pjTabla tr.pj-ed [data-pj-f] option"))
        pg.select_option("#pjTabla tr.pj-ed [data-pj-f]", "Proyecto")
        pg.wait_for_function("document.querySelector('#pjTabla .pj-estado')?.textContent === 'Carpeta guardada'",
                             timeout=5000)
        self.assertEqual(pg.evaluate("api('/api/config')")["workstream_folders"], {ws: "Proyecto"})
        pg.click("#pjTabla [data-pj-no]")
        celda = f"document.querySelector('#pjTabla tr[data-pjw=\"{ws}\"] td:nth-child(2)').innerText"
        pg.wait_for_function(f"!{celda}.includes('la del proyecto')", timeout=5000)
        # dos cambios seguidos en la misma fila y enseguida otra fila: no se pierde ninguno (revisión 2, P1-1)
        pg.click("#pjTabla [data-pj-editar='Proyecto']")
        pg.select_option("#pjTabla tr.pj-ed [data-pj-f]", "Proyecto")
        pg.select_option("#pjTabla tr.pj-ed [data-pj-l]", "de")  # enseguida, el otro desplegable
        pg.click(f"#pjTabla [data-pjw-editar='{ws}']")  # y enseguida otra fila
        # la fila nueva, en cuanto se abre (la vieja se cierra tras guardar lo pendiente: no se elige en ella)
        fila_ws = f"#pjTabla tr.pj-ed[data-alcance='workstream'][data-vocab='{ws}']"
        pg.wait_for_selector(fila_ws, timeout=5000)
        pg.select_option(f"{fila_ws} [data-pj-l]", "fr")
        pg.click("#pjTabla [data-pj-no]")
        todo = ("api('/api/config').then(c => (c.project_folders_asignadas || c.project_folders)?.Proyecto"
                " === 'Proyecto'"
                f" && c.project_langs?.Proyecto === 'de' && c.workstream_langs?.['{ws}'] === 'fr')")
        pg.wait_for_function(todo, timeout=5000)
        for body in ({"clave": "idioma", "valor": None, "alcance": "proyecto", "vocab": ws},
                     {"clave": "idioma", "valor": None, "alcance": "workstream", "vocab": ws},
                     {"clave": "carpeta", "valor": None, "alcance": "proyecto", "vocab": ws},
                     {"clave": "carpeta", "valor": None, "alcance": "workstream", "vocab": ws}):
            pg.evaluate("b => api('/api/grabar/recordar', b)", body)  # deja los ajustes como estaban
        cfg = pg.evaluate("api('/api/config')")
        self.assertEqual((cfg["workstream_folders"], cfg["workstream_langs"], cfg["project_langs"]), ({}, {}, {}))
        pg.keyboard.press("Escape")
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [], red)

    def test_ajustes_plan_solo_cambian_las_filas_que_cambian(self):
        """Revisión 2 de B2 (P2-4): al recalcular el plan de Al grabar, solo cambian las filas que cambian: un lector de
        pantalla no vuelve a leer el plan entero. (La opción de Procesado ya no se elige en Ajustes: punto 37.)"""
        pg, errores, red = self.pagina()
        self.abrir_ajustes(pg)
        pg.wait_for_selector("#agFilas .gr-fila[data-fila='idioma']", timeout=10000)
        self.assertIsNone(pg.get_attribute(".ag-plan", "aria-live"))
        self.assertEqual(pg.get_attribute("#agFilas", "aria-live"), "polite")
        antes = pg.evaluate("api('/api/config')")["language"]
        otro = "pt" if antes != "pt" else "de"
        idioma = "document.querySelector('#agFilas [data-fila=idioma] .gr-v').textContent"
        i0 = pg.evaluate(idioma)
        pg.evaluate("window.__audio = document.querySelector('#agFilas [data-fila=audio]')")
        pg.select_option("#sLang", otro)
        pg.wait_for_function(f"{idioma} !== {json.dumps(i0)}", timeout=5000)
        self.assertTrue(pg.evaluate("window.__audio === document.querySelector('#agFilas [data-fila=audio]')"))
        self.guardado_junto_a(pg, "#sLang")
        pg.select_option("#sLang", antes)
        pg.wait_for_function(f"{idioma} === {json.dumps(i0)}", timeout=5000)
        self.guardado_junto_a(pg, "#sLang")
        pg.keyboard.press("Escape")
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [], red)

    def test_ajustes_lo_que_se_escribe_se_guarda_antes_de_salir(self):
        """Punto 12 del review: sin borrador. Lo que se está escribiendo (aún en la pausa de 0,7 s) se guarda antes de
        salir a un subdiálogo (editar una lista) y al cerrar Ajustes; al volver, Ajustes enseña lo guardado."""
        pg, errores, red = self.pagina()
        cfg0 = None
        try:
            self.abrir_ajustes(pg)
            cfg0 = pg.evaluate("api('/api/config')")
            pg.fill("#pfContext", "Antes de salir r12")
            # sin salir de la casilla (un clic de verdad la dejaría): «editar» una lista desde Vocabulario
            pg.evaluate("document.querySelector('.aj-nav [data-sec=\"vocabulario\"]').click()")
            pg.wait_for_selector("#sVocab [data-vedit]", state="attached", timeout=10000)
            pg.evaluate("document.querySelector('#sVocab [data-vedit]').click()")
            pg.wait_for_selector("#modal .dialog:not(.aj)", timeout=5000)
            self.assertEqual(pg.evaluate("api('/api/config')")["perfil"]["context"], "Antes de salir r12")
            pg.keyboard.press("Escape")
            # y al cerrar Ajustes con «Cerrar», también lo que está a medio escribir
            self.abrir_ajustes(pg, "resumen")
            pg.fill("#sExtra", "Al cerrar r12")
            pg.evaluate("document.querySelector('#modal [data-m=\"cerrar\"]').click()")
            pg.wait_for_function("api('/api/config').then(c => c.summary.extra === 'Al cerrar r12')", timeout=5000)
            # el servidor ya lo tiene, pero su respuesta puede seguir en camino: recargar ahora la cortaría
            pg.wait_for_function("!S.ajPendiente", timeout=5000)
            self.abrir_ajustes(pg)
            self.assertEqual(pg.input_value("#pfContext"), "Antes de salir r12")
            pg.keyboard.press("Escape")
            # (el editor de la lista se cierra con su comprobación aún en el aire: se cancela, no es un fallo)
            err, red = self.limpiar(errores, [r for r in red if "/api/vocab/check" not in r])
            self.assertEqual(err, [], (err, red))
            self.assertEqual(red, [], red)
        finally:
            if cfg0:
                pg.evaluate("c => api('/api/settings', {perfil: {context: c.perfil.context || ''}, "
                            "summary: {extra: c.summary.extra || ''}})", cfg0)

    def test_ajustes_confirmaciones_encima(self):
        """Revisión 10 de B2: lo que se hace dentro de Ajustes no lo cierra. Las confirmaciones van encima («Apagar» ›
        «Cancelar» o Escape; «Quitar» una carpeta vinculada; «Activar» las voces) y «Probar otra vez» de las listas e
        «Importar listas…» se hacen ahí mismo: lo escrito sigue en el formulario (el mismo, sin repintar Ajustes) y lo
        que cambian (las carpetas vinculadas, «Reuniones sin proyecto», las listas, las voces) se pone al día."""
        import re
        pg, errores, red = self.pagina()
        self.addCleanup(lambda: pg.unroute_all(behavior="ignoreErrors"))
        apagar = []
        pg.route("**/api/apagar", lambda r: (apagar.append(r.request.url), r.abort()))
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        encima = "#modalSobre:not(.hidden) .dialog"

        def abrir(texto):
            pg.click("#btnSettings")
            pg.wait_for_selector("#modal .aj-nav", timeout=5000)
            pg.fill("#pfContext", texto)
            pg.evaluate("document.querySelector('#modal .dialog.aj').__marca = 1")

        def mismo(texto):  # el mismo Ajustes (no repintado), con lo escrito
            pg.wait_for_function(f"!document.querySelector('{encima}')", timeout=5000)
            self.assertEqual(pg.evaluate("document.querySelector('#modal .dialog.aj')?.__marca"), 1)
            self.assertEqual(pg.input_value("#pfContext"), texto)
        contexto0 = pg.evaluate("api('/api/config')")["perfil"].get("context") or ""
        self.addCleanup(pg.evaluate, "t => api('/api/settings', {perfil: {context: t}})", contexto0)
        # «Apagar» › «Cancelar» y › Escape: la confirmación, encima; Ajustes sigue
        abrir("escrito y apagando")
        for salir in ("cancelar", "escape"):
            pg.evaluate("document.querySelector('#sOff').click()")
            pg.wait_for_selector(encima, timeout=5000)
            self.assertIn("Apagar", pg.inner_text(encima))
            if salir == "cancelar":
                pg.click(f"{encima} [data-m='no']")
            else:
                pg.keyboard.press("Escape")
            mismo("escrito y apagando")
        self.assertEqual(apagar, [])
        # y si otro diálogo ocupa el sitio con la confirmación abierta (Ctrl+Alt+R, por ejemplo), no se queda encima
        pg.evaluate("document.querySelector('#sOff').click()")
        pg.wait_for_selector(encima, timeout=5000)
        pg.evaluate("() => { openModal('<h3>Otro</h3>'); }")  # (sin esperar su promesa: se resuelve al cerrarlo)
        self.assertFalse(pg.evaluate(f"!!document.querySelector('{encima}')"))
        pg.keyboard.press("Escape")
        self.assertEqual(apagar, [])
        pg.click("#btnSettings")
        pg.wait_for_selector("#modal .aj-nav", timeout=5000)
        pg.keyboard.press("Escape")
        # «Otra carpeta…» y «quitar» esa carpeta: confirmación encima; la carpeta quitada deja lo guardado
        carpeta = os.path.join(self.tmp, "fuera-r10")
        os.makedirs(carpeta, exist_ok=True)
        pg.route("**/api/link/pick", lambda r: r.fulfill(json={"path": carpeta}))
        abrir("escrito con una carpeta")
        pg.select_option("#pfFolder", "__pick")
        pg.wait_for_function("!['__auto', '__pick'].includes(document.querySelector('#pfFolder').value)", timeout=5000)
        rel = pg.input_value("#pfFolder")
        self.addCleanup(pg.evaluate, "p => api('/api/link/remove', { path: p }).catch(() => null)", rel)
        self.guardado_junto_a(pg, "#pfFolder")  # elegida y guardada al momento (punto 12 del review)
        self.assertEqual(pg.evaluate("api('/api/config')")["perfil"]["folder"], rel)
        # «quitar», en su sección y con un clic de verdad (el foco, en el botón)
        seccion = pg.evaluate("document.querySelector('#sLinks').closest('section').dataset.secc")
        pg.click(f".aj-nav [data-sec='{seccion}']")
        pg.click(f"[data-unlink='{rel}']")
        pg.wait_for_selector(encima, timeout=5000)
        pg.click(f"{encima} [data-m='ok']")
        pg.wait_for_function("p => !document.querySelector(`[data-unlink='${p}']`)", arg=rel, timeout=5000)
        mismo("escrito con una carpeta")
        self.assertEqual(pg.evaluate("document.activeElement.id"), "lnAdd")  # su «quitar» ya no está
        self.assertEqual(pg.input_value("#pfFolder"), "__auto")
        self.assertNotIn(rel, pg.evaluate("[...document.querySelectorAll('#pfFolder option')].map(o => o.value)"))
        self.assertIsNone(pg.evaluate("api('/api/config')")["perfil"]["folder"])  # el servidor la deja automática
        pg.keyboard.press("Escape")
        # «Importar listas de otra carpeta…»: las listas, al día aquí mismo
        origen = os.path.join(self.tmp, "imp-r10", "Cliente Imp", "Vocabulario")
        os.makedirs(origen, exist_ok=True)
        with open(os.path.join(origen, "Lista.txt"), "w", encoding="utf-8") as f:
            f.write("uno, dos")
        self.addCleanup(pg.evaluate, "() => api('/api/vocab/delete', { id: 'Cliente Imp/Vocabulario/Lista.txt' })"
                        ".catch(() => null)")
        pg.unroute("**/api/link/pick")
        pg.route("**/api/link/pick", lambda r: r.fulfill(json={"path": os.path.join(self.tmp, "imp-r10")}))
        abrir("escrito e importando")
        pg.wait_for_selector("#sVocabImport", state="attached", timeout=10000)
        pg.evaluate("document.querySelector('#sVocabImport').click()")
        pg.wait_for_function("document.querySelector('#sVocab')?.innerText.includes('Cliente Imp')", timeout=10000)
        mismo("escrito e importando")
        pg.wait_for_function("[...document.querySelectorAll('#pfVocab option')]"
                             ".some(o => o.value === 'Cliente Imp/Vocabulario/Lista.txt')", timeout=5000)
        pg.keyboard.press("Escape")
        # «Probar otra vez» cuando no se han podido leer las listas: aquí mismo
        vocab_rx = re.compile(r"/api/vocab(\?|$)")
        pg.route(vocab_rx, lambda r: r.fulfill(status=500, json={"error": "OneDrive no responde"}))
        pg.evaluate("S.vocab = []")  # (como si nunca se hubieran podido leer)
        abrir("escrito sin listas")
        pg.wait_for_selector("[data-secc='algrabar'] .ag-sin-listas", timeout=10000)
        pg.unroute(vocab_rx)
        pg.click("[data-secc='algrabar'] [data-aj-listas-otra]")
        pg.wait_for_function("!document.querySelector('#modal .ag-sin-listas')", timeout=10000)
        mismo("escrito sin listas")
        pg.keyboard.press("Escape")
        # «Activar» las voces (simulado: aquí no hay modelos): encima, y la casilla y el panel, al día
        pg.route("**/api/voces/activar", lambda r: r.fulfill(json={"enabled": True, "models": {"ready": True}}))
        voces = {"enabled": True, "available": True, "models": {"ready": True}, "personas": []}
        pg.route("**/api/voces", lambda r: r.fulfill(json=voces))
        abrir("escrito y con voces")
        pg.evaluate("document.querySelector('#vzOn').click()")
        pg.wait_for_selector(encima, timeout=5000)
        pg.click(f"{encima} [data-m='ok']")
        pg.wait_for_function("document.querySelector('#vzOn').checked", timeout=5000)
        mismo("escrito y con voces")
        pg.wait_for_function("document.querySelector('#vzModels')?.innerText.includes('Modelos listos')", timeout=5000)
        pg.keyboard.press("Escape")
        err, red = self.limpiar(errores, [r for r in red if "/api/vocab" not in r and "/api/apagar" not in r])
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [], red)

    def test_ajustes_lista_renombrada_o_borrada_en_su_editor(self):
        """Revisión 11 de B2: renombrar o borrar en su editor la lista guardada como «Proyecto al grabar» cambia ese
        ajuste en el servidor; al volver, Ajustes lo enseña así (con su nombre nuevo, o «Automático» si se borró). Antes
        volvía con la configuración vieja: «(no se encuentra)» y «Guardar» daba 400. (Desde el punto 12 del review, sin
        «Guardar»: se guarda al elegir.)"""
        pg, errores, red = self.pagina()
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        uno = pg.evaluate("api('/api/vocab/save', { project: 'Cliente R11', name: 'Legal', terms: 'x' })")["id"]
        self.addCleanup(pg.evaluate, "() => Promise.allSettled(['Legal', 'Legal 2'].map(n =>"
                        " api('/api/vocab/delete', { id: `Cliente R11/Vocabulario/${n}.txt` })))")
        self.addCleanup(pg.evaluate, "() => api('/api/perfil', { vocab: null })")
        pg.evaluate("loadVocab(S.config.language ?? 'es-en')")

        def ajustes():
            pg.click("#btnSettings")
            pg.wait_for_selector("#modal .aj-nav", timeout=5000)
            pg.wait_for_selector("#sVocab [data-vedit]", state="attached", timeout=10000)

        def guardado():
            pg.click("#modal .aj-pie [data-m='cerrar']")
            pg.wait_for_function("!document.querySelector('#modal .aj-nav')", timeout=5000)
            return pg.evaluate("api('/api/config')")["perfil"]["vocab"]
        ajustes()
        pg.select_option("#pfVocab", uno)
        self.guardado_junto_a(pg, "#pfVocab")
        self.assertEqual(guardado(), uno)
        dos = "Cliente R11/Vocabulario/Legal 2.txt"
        for accion, queda in (("renombrar", dos), ("borrar", None)):
            ajustes()
            lista = uno if accion == "renombrar" else dos
            pg.evaluate("id => document.querySelector(`#sVocab [data-vedit='${id}']`).click()", lista)
            pg.wait_for_selector("#modal #veName", timeout=5000)  # sin nada escrito: sin preguntar
            if accion == "renombrar":
                pg.fill("#veName", "Legal 2")
                pg.click("#modal [data-m='ok']")
            else:
                pg.click("#modal [data-m='del']")
            pg.wait_for_selector("#modal .aj-nav", timeout=5000)
            pg.wait_for_function("v => document.querySelector('#pfVocab').value === v", arg=queda or "__auto",
                                 timeout=5000)
            self.assertNotIn("no se encuentra", pg.inner_text("#pfVocab"), accion)
            self.assertEqual(guardado(), queda, accion)
            self.assertFalse(pg.evaluate("document.querySelector('#toasts').innerText.includes('ya no existe')"))
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [], red)

    def test_ajustes_revisar_desde_el_asistente(self):
        """Revisión 6 de B2: «Revisar en Ajustes» del asistente abierto desde Ajustes, con «Con tu cuenta de Claude»
        elegida y sin guardar, lleva a Procesado con su selector de modelo a la vista y dice que falta aplicarla."""
        pg, errores, red = self.pagina()
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.click("#btnSettings")
        pg.click(".aj-nav [data-sec='procesado']")
        pg.click("#modal [data-cfg='cambiar']")
        pg.wait_for_selector("#modal .dialog.wz", timeout=5000)
        pg.evaluate("W.modo = 'agente'; wzAccion('procesado')")
        pg.wait_for_selector("#modal .aj-nav [data-sec='procesado'].on", timeout=5000)
        # (punto 37) sin las tres opciones en Ajustes: dice que aún no está aplicada y enseña su selector de modelo
        self.assertIn("aún no está aplicada", pg.inner_text("#ajProcPedido"))
        self.assertTrue(pg.is_visible("#agModelo"))
        # «Cambiar…» vuelve al asistente con esa opción
        pg.click("#modal [data-cfg='cambiar']")
        pg.wait_for_selector("#modal .dialog.wz input[name='wzModo'][value='agente']:checked", timeout=5000)
        pg.keyboard.press("Escape")
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [], red)

    def test_tarjeta_dice_los_avisos_del_plan(self):
        """Revisión 4 de B2: los avisos del plan (aquí, «Falta la API key» con «API externa» sin clave) salen en la
        tarjeta de grabar, en su línea, como en Ajustes: es al grabar cuando importan."""
        pg, errores, red = self.pagina()
        nada = {"teams": None, "outlook": None, "reason": None, "sugerencia": {}}
        pg.route("**/api/reunion/actual", lambda r: r.fulfill(json=nada))
        pg.route("**/api/rec/monitor", lambda r: r.fulfill(json={"ok": True}))
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        antes = pg.evaluate("api('/api/config')")["procesado"]
        self.addCleanup(pg.evaluate, "m => api('/api/settings', {procesado: m}).then(setConfig)", antes)
        pg.evaluate("api('/api/settings', {procesado: 'api'}).then(setConfig)")
        pg.click("#btnRec")
        pg.wait_for_selector(".dialog.gr [data-fila='resume'] .gr-aviso", timeout=10000)
        self.assertEqual(pg.inner_text(".dialog.gr [data-fila='resume'] .gr-aviso"),
                         "Falta la API key: ponla en Ajustes › Procesado.")
        pg.keyboard.press("Escape")
        # y el de la carpeta: la de las reuniones sin proyecto, guardada y que ya no se encuentra (revisión 5)
        fuera = os.path.join(self.root, "Fuera")
        os.makedirs(fuera, exist_ok=True)
        self.addCleanup(shutil.rmtree, fuera, True)
        pg.evaluate("api('/api/settings', {perfil: {folder: 'Fuera'}}).then(setConfig)")
        self.addCleanup(pg.evaluate, "api('/api/settings', {perfil: {folder: null}}).then(setConfig)")
        os.rmdir(fuera)
        pg.click("#btnRec")
        pg.wait_for_selector(".dialog.gr [data-fila='carpeta'] .gr-v code", timeout=10000)
        pg.select_option("#grProy", "")  # sin proyecto: se graba en la de Ajustes, que no está
        pg.wait_for_selector(".dialog.gr [data-fila='carpeta'] .gr-aviso", timeout=10000)
        # la ruta, con la raíz por su nombre en el disco: esta carpeta se llama «Chuleta» (test_rutas_con_el_nombre_…)
        self.assertIn("No se encuentra la carpeta de las reuniones sin proyecto (Chuleta\\Fuera\\)",
                      pg.inner_text(".dialog.gr [data-fila='carpeta'] .gr-aviso"))
        pg.keyboard.press("Escape")
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], (err, red))

    def test_reunion_de_la_raiz_con_un_nombre_anterior_sigue_con_su_lista(self):
        """Revisión de b7b9bea (P1): el proyecto de las listas de la raíz se llama como su carpeta («Chuleta» aquí);
        una reunión de la raíz guardada con otro de sus nombres («Tenca», con 4553b9c, o «Notas») sigue con su lista en
        la página de la reunión aunque otro proyecto tenga una lista que se llame igual (antes: «no se encuentra el
        archivo»). Con la función real de notas.js y las listas de /api/vocab sustituidas en la página."""
        pg, errores, red = self.pagina()
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        self.assertEqual(sorted(pg.evaluate("S.config.root_names")), ["Chuleta", "Notas", "Tenca"])
        r = pg.evaluate("""() => {
          const antes = S.vocab;
          S.vocab = [{ id: 'Vocabulario/General.txt', project: 'Chuleta', folder: '', name: 'General' },
                     { id: 'Alfa/Vocabulario/General.txt', project: 'Alfa', folder: 'Alfa', name: 'General' },
                     { id: ':vx/Vocabulario/General.txt', project: 'Legal', folder: ':vx', name: 'General',
                       conectada: ':vx' }];
          const f = (proyecto, vocabulario) =>
            (vocabOf({ props: { proyecto, workstream: 'General', vocabulario } }) || {}).id || null;
          const out = [f('Tenca'), f('Chuleta'), f('notas'), f('Alfa'), f('Tenca', 'Vocabulario/General.txt'),
                       f('Legal'), f('Otro')];
          // con su id, la de la raíz aunque una carpeta conectada se llame «Tenca»
          S.vocab.push({ id: ':vy/General.txt', project: 'Tenca', folder: ':vy', name: 'General', conectada: ':vy' });
          out.push(f('Tenca', 'Vocabulario/General.txt'));
          // sin lista en la raíz, un nombre de la raíz no vale para la de otro proyecto
          S.vocab = S.vocab.filter(v => v.folder !== '' && v.folder !== ':vy');
          out.push(f('Notas'));
          S.vocab = antes;
          return out;
        }""")
        raiz = "Vocabulario/General.txt"
        self.assertEqual(r, [raiz, raiz, raiz, "Alfa/Vocabulario/General.txt", raiz, ":vx/Vocabulario/General.txt",
                             None, raiz, None])
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], (err, red))

    def test_rutas_con_el_nombre_de_la_carpeta_en_el_disco(self):
        """P2 del renombrado (09/11/2026): la carpeta de esta suite se llama «Chuleta», como la de una instalación de
        Tenca encima de Chuleta, que no la renombra. La tarjeta de grabar, su selector de carpeta y Ajustes › Proyectos
        enseñan la ruta de verdad («Chuleta\\…», «Chuleta (raíz)») y nunca «Tenca\\…», que no está en el Explorador; el
        nombre de la app sigue siendo Tenca."""
        pg, errores, red = self.pagina()
        nada = {"teams": None, "outlook": None, "reason": None, "sugerencia": {}}
        pg.route("**/api/reunion/actual", lambda r: r.fulfill(json=nada))
        pg.route("**/api/rec/monitor", lambda r: r.fulfill(json={"ok": True}))
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        self.assertEqual(pg.evaluate("[APP_NAME, rootName(), folderName('')]"), ["Tenca", "Chuleta", "Chuleta (raíz)"])
        self.assertEqual(pg.title(), "Tenca")
        pg.click("#btnRec")
        pg.wait_for_selector(".dialog.gr [data-fila='carpeta'] .gr-v code", timeout=10000)
        pg.select_option("#grProy", "")  # sin proyecto: la de Ajustes, que es la raíz
        pg.wait_for_function("document.querySelector(\".dialog.gr [data-fila='carpeta'] .gr-v code\")"
                             ".textContent.startsWith('Chuleta')", timeout=10000)
        ruta = pg.inner_text(".dialog.gr [data-fila='carpeta'] .gr-v code")
        self.assertRegex(ruta, r"^Chuleta\\[^\\]+\\$")  # la carpeta de la reunión, en la raíz
        pg.select_option("#grProy", "Proyecto/Vocabulario/General.txt")
        pg.wait_for_function("document.querySelector(\".dialog.gr [data-fila='carpeta'] .gr-v code\")"
                             ".textContent.startsWith('Chuleta\\\\Proyecto')", timeout=10000)
        pg.click(".dialog.gr [data-gr-cambiar='carpeta']")
        pg.wait_for_selector("#grSelCarpeta", timeout=5000)
        opciones = pg.evaluate("[...document.querySelectorAll('#grSelCarpeta option')].map(o => o.textContent)")
        self.assertIn("Chuleta (raíz)", opciones)
        self.assertFalse(any("Tenca" in o and "fuera de" not in o for o in opciones), opciones)
        self.assertNotIn("Tenca\\", pg.inner_text(".dialog.gr"))
        pg.keyboard.press("Escape")
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], (err, red))

    def test_ajustes_sin_poder_leer_las_listas(self):
        """Revisión 2 de B2, P1-4: si /api/vocab falla, Proyectos no dice «Todavía no hay proyectos» (invitaba a
        crearlos otra vez): una frase con «Probar otra vez», también en las listas y en Al grabar."""
        pg, errores, red = self.pagina()
        pg.route("**/api/vocab*", lambda r: r.fulfill(status=500, json={"error": "OneDrive no responde"}))
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.click("#btnSettings")
        pg.wait_for_selector("[data-secc='algrabar'] .ag-sin-listas", timeout=10000)
        self.assertIn("solo se puede comprobar «Sin proyecto»", pg.inner_text("[data-secc='algrabar'] .ag-sin-listas"))
        pg.click(".aj-nav [data-sec='proyectos']")
        pg.wait_for_selector("#pjTabla .ag-sin-listas", timeout=10000)
        self.assertIn("No se han podido leer las listas de vocabulario", pg.inner_text("#pjTabla"))
        self.assertNotIn("Todavía no hay", pg.inner_text("#pjTabla"))
        pg.click(".aj-nav [data-sec='vocabulario']")  # las listas, en su sección (punto 37 del review)
        pg.wait_for_selector("#sVocab .ag-sin-listas", timeout=10000)
        self.assertNotIn("Todavía no hay listas", pg.inner_text("#sVocab"))
        self.assertNotIn("OneDrive no responde", pg.inner_text("#modal"))  # sin el texto técnico
        pg.click(".aj-nav [data-sec='proyectos']")
        pg.unroute("**/api/vocab*")
        pg.click("#pjTabla [data-aj-listas-otra]")  # otra vez, aquí mismo
        pg.wait_for_selector("#modal [data-secc='proyectos']:not([hidden]) #pjTabla [data-pj-editar='Proyecto']",
                             timeout=10000)
        self.assertEqual(pg.locator(".ag-sin-listas").count(), 0)
        pg.keyboard.press("Escape")
        err, red = self.limpiar(errores, [r for r in red if "/api/vocab" not in r])
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [], red)

    def test_ajustes_lo_guardado_que_ahora_no_se_encuentra(self):
        """Revisión 2 de B2, P1-3: la carpeta de las reuniones sin proyecto y el proyecto al grabar guardados, si ahora
        no se encuentran (una vinculada desconectada, una lista que no se lee), se ven tal cual y cualquier «Guardar»
        los conserva. Antes el desplegable enseñaba «Automática» y el «Guardar» de otra sección los borraba; ahora cada
        cambio manda solo lo suyo."""
        pg, errores, red = self.pagina()
        omega, fuera = os.path.join(self.root, "Omega"), os.path.join(self.tmp, "Omega fuera")
        os.makedirs(os.path.join(omega, "Vocabulario"))
        os.makedirs(os.path.join(omega, "Clientes"))
        with open(os.path.join(omega, "Vocabulario", "Gen.txt"), "w", encoding="utf-8") as f:
            f.write("alfa, beta\n")
        self.addCleanup(shutil.rmtree, omega, True)
        self.addCleanup(shutil.rmtree, fuera, True)
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        perfil = {"folder": "Omega/Clientes", "vocab": "Omega/Vocabulario/Gen.txt"}
        pg.evaluate("p => api('/api/settings', {perfil: p})", perfil)
        # al terminar, como estaba (antes de cerrar la página: las limpiezas van al revés)
        self.addCleanup(pg.evaluate, "api('/api/settings', {perfil: {folder: null, vocab: null},"
                                     " summary: {extra: ''}})")
        os.rename(omega, fuera)  # ahora no se encuentran
        pg.reload(wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        # el árbol del servidor se lee como mucho cada segundo: hasta que ya no tenga la carpeta
        pg.wait_for_function("refreshTree(true).then(() => !S.folders.has('Omega/Clientes'))", polling=500,
                             timeout=10000)
        pg.click("#btnSettings")
        pg.wait_for_selector("#agFilas .gr-fila", timeout=10000)
        elegido = "(id => { const s = document.querySelector(id); return s.options[s.selectedIndex].textContent; })"
        pg.wait_for_function(f"{elegido}('#pfVocab').includes('no se encuentra')", timeout=5000)
        self.assertEqual(pg.evaluate(f"{elegido}('#pfVocab')"), "Omega › Gen (no se encuentra)")
        self.assertEqual(pg.evaluate(f"{elegido}('#pfFolder')"), "Omega\\Clientes (no se encuentra)")
        pg.click(".aj-nav [data-sec='resumen']")
        pg.fill("#sExtra", "Céntrate en fechas")
        self.guardado_junto_a(pg, "#sExtra")  # se guarda solo lo cambiado (punto 12): lo demás no se toca
        c = pg.evaluate("api('/api/config')")
        self.assertEqual(({k: c["perfil"][k] for k in perfil}, c["summary"]["extra"]), (perfil, "Céntrate en fechas"))
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [], red)

    def test_ajustes_lo_no_guardado_errores_y_foco(self):
        """Revisión de B2 (09/11/2026): al abrir, el foco va a la sección y el plan no se desplaza fuera de la vista
        (900×600); el plan cambia con el motor del directo elegido en Procesado, y «Transcribe» dice que sale de allí;
        sin micrófono y sin audio del PC no se guarda, vuelve a lo guardado y lo dice; si el plan falla, una frase con
        «Probar otra vez» (en Al grabar y en Proyectos), sin el texto técnico."""
        pg, errores, red = self.pagina()
        # un PC con micrófono y altavoces (sin grabadora aquí, la lista no llega y su aviso ocupa una línea más)
        devs = {"mics": [{"id": "m1", "name": "Micrófono (Realtek)", "default": True}], "mic": "default", "sys": "all",
                "outputs": [{"id": "o1", "name": "Altavoces (Realtek)", "default": True}]}
        pg.route("**/api/rec/devices*", lambda r: r.fulfill(json=devs))
        pg.set_viewport_size({"width": 900, "height": 600})
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.click("#btnSettings")
        pg.wait_for_selector("#agFilas .gr-fila[data-fila='transcribe']", timeout=10000)
        self.assertTrue(pg.evaluate("document.activeElement.matches('.aj-nav [data-sec].on')"))
        self.assertEqual(pg.evaluate("document.querySelector('.aj-main').scrollTop"), 0)
        cfg = pg.evaluate("api('/api/config')")
        actual = cfg["live_preset"]
        otro = next(p for p in cfg["presets"] if p["id"] != actual and p["engine"] == "local")
        pg.click(".aj-nav [data-sec='procesado']")
        pg.select_option("#lPreset", otro["id"])
        pg.click(".aj-nav [data-sec='algrabar']")
        t = "document.querySelector('#agFilas [data-fila=transcribe]').innerText"
        pg.wait_for_function(f"{t}.includes({json.dumps(otro['live_label'].split(' · ')[0])})", timeout=5000)
        self.assertIn("Ajustes › Procesado", pg.inner_text("#agFilas [data-fila='transcribe']"))
        pg.click(".aj-nav [data-sec='procesado']")
        pg.select_option("#lPreset", actual)
        self.guardado_junto_a(pg, "#lPreset")
        pg.click(".aj-nav [data-sec='algrabar']")
        # sin micrófono y sin el audio del PC: el segundo no se guarda, vuelve a lo guardado y lo dice a su lado
        pg.select_option("#agMic", "none")
        self.guardado_junto_a(pg, "#agMic")
        pg.select_option("#agSys", "none")
        self.guardado_junto_a(pg, "#agSys", "no se graba nada")
        self.assertEqual(pg.input_value("#agSys"), cfg["rec_sys"])
        audio = "document.querySelector('#agFilas [data-fila=audio]').innerText"
        pg.wait_for_function(f"!{audio}.includes('Así no se graba nada')", timeout=5000)  # el plan, con lo que se ve
        c2 = pg.evaluate("api('/api/config')")
        self.assertEqual((c2["rec_mic"], c2["rec_sys"]), ("none", cfg["rec_sys"]))
        pg.select_option("#agMic", cfg["rec_mic"])
        self.guardado_junto_a(pg, "#agMic")
        # el plan falla: una frase y «Probar otra vez», sin el error de Python
        tecnico = {"error": "'NoneType' object has no attribute 'get'"}
        pg.route("**/api/grabar/plan", lambda r: r.fulfill(status=500, json=tecnico))
        self.addCleanup(pg.evaluate, "l => api('/api/settings', {language: l})", cfg["language"])
        pg.select_option("#sLang", "de" if cfg["language"] != "de" else "pt")
        pg.wait_for_selector("#agFilas [data-ag-reintentar]", timeout=5000)
        self.assertIn("No se ha podido calcular", pg.inner_text("#agFilas"))
        self.assertNotIn("NoneType", pg.inner_text("#agFilas"))
        pg.click(".aj-nav [data-sec='proyectos']")
        pg.wait_for_selector("#pjTabla [data-pj-reintentar]", timeout=5000)
        self.assertNotIn("sin asignar", pg.inner_text("#pjTabla"))  # no una tabla que parezca correcta
        pg.unroute("**/api/grabar/plan")
        pg.click("#pjTabla [data-pj-reintentar]")
        celda = "document.querySelector('#pjTabla tr[data-pj=\"Proyecto\"] td:nth-child(2)')?.innerText || ''"
        pg.wait_for_function(f"({celda}).includes('su carpeta')", timeout=10000)
        # «Al grabar»: las reglas, en una pantalla (§4.0 del rediseño) a 1500×920, sin desplazar
        pg.set_viewport_size({"width": 1500, "height": 920})
        pg.click(".aj-nav [data-sec='algrabar']")
        pg.wait_for_selector("#agFilas .gr-fila[data-fila='audio']", timeout=5000)
        alto = pg.evaluate("(() => { const m = document.querySelector('.aj-main');"
                           " return [m.scrollHeight, m.clientHeight]; })()")
        self.assertLessEqual(alto[0], alto[1], alto)
        pg.click(".ag-como-btn")  # «¿Cómo funciona?», plegado en la línea de arriba
        self.assertTrue(pg.is_visible("#agComo"))
        self.assertEqual(pg.get_attribute(".ag-como-btn", "aria-expanded"), "true")
        pg.keyboard.press("Escape")
        err, red = self.limpiar(errores, [r for r in red if "/api/grabar/plan" not in r and "/api/settings" not in r])
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [], red)

    def test_ajustes_micro_sin_guardar_y_listas_que_cambian(self):
        """Revisiones de B2 (09/11/2026): un micrófono elegido no se pierde por abrir el editor de una lista (se guarda
        al elegirlo, punto 12 del review); y si cambian las listas de
        vocabulario (p. ej. al conectar una carpeta, rama vocab-carpeta), la tabla de Proyectos y el selector de «Así se
        graba ahora» se rehacen."""
        pg, errores, red = self.pagina()
        devs = {"mics": [{"id": "m1", "name": "Micro uno", "default": True},
                         {"id": "m2", "name": "Micro dos", "default": False}],
                "outputs": [{"id": "o1", "name": "Altavoces", "default": True}], "mic": "default", "sys": "all"}
        pendientes = []
        pg.route("**/api/rec/devices*", lambda r: pendientes.append(r))
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.click("#btnSettings")
        pg.wait_for_function("document.querySelector('#agFilas .gr-fila')", timeout=10000)
        pendientes.pop(0).fulfill(json=devs)
        pg.wait_for_selector("#agMic option[value='m2']", state="attached", timeout=5000)
        self.addCleanup(pg.evaluate, "() => api('/api/settings', {rec_mic: 'default'})")
        pg.select_option("#agMic", "m2")
        self.guardado_junto_a(pg, "#agMic")  # guardado al elegirlo (punto 12 del review)
        ws = "Proyecto/Vocabulario/General.txt"
        pg.click(".aj-nav [data-sec='proyectos']")
        pg.click(f"#pjTabla [data-pjw-editar='{ws}']")
        pg.click("#pjTabla [data-pj-lista]")  # el editor de la lista ocupa el sitio de Ajustes: sin preguntar nada
        pg.wait_for_selector("#modal textarea#veTerms", timeout=5000)
        pg.click("#modal [data-m='no']")  # vuelve a Ajustes, con lo guardado
        pg.wait_for_selector("#modal .aj-nav", timeout=5000)
        pg.click(".aj-nav [data-sec='algrabar']")
        self.assertEqual(pg.input_value("#agMic"), "m2")
        self.assertEqual(pg.evaluate("api('/api/config')")["rec_mic"], "m2")
        pg.click(".aj-nav [data-sec='proyectos']")
        pg.wait_for_selector("#pjTabla [data-pj-editar='Proyecto']", timeout=5000)
        # las listas cambian (otro proyecto) y el bloque de listas se repinta: la tabla y el selector, también
        pg.evaluate("api('/api/vocab/save', {project: 'Otro cliente', name: 'General', terms: 'uno, dos'})")
        pg.evaluate("loadVocab(S.config.language ?? 'es-en').then(() => { const b = document.querySelector('#sVocab');"
                    " b.innerHTML = b.innerHTML; })")
        pg.wait_for_selector("#pjTabla [data-pj-editar='Otro cliente']", timeout=5000)
        pg.wait_for_selector("#agProy option[value='Otro cliente/Vocabulario/General.txt']", state="attached",
                             timeout=5000)
        pg.evaluate("api('/api/vocab/delete', {id: 'Otro cliente/Vocabulario/General.txt'})")
        pg.keyboard.press("Escape")
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [], red)

    def test_claude_de_empresa_en_ajustes_y_en_el_asistente(self):
        """Rama claude-empresa (08/11/2026): «Comprobar» dice qué copia de Claude Code respondió (la de VS Code) y,
        si la cuenta no deja el modelo elegido, el aviso; el paso 2 del asistente dice dónde lo encontró. Las respuestas
        del servidor se simulan con la forma de esa rama (en esta no existen esos campos)."""
        pg, errores, red = self.pagina()
        aviso = ("El modelo elegido (sonnet) no está disponible con esta cuenta: se ha usado el predeterminado de tu "
                 "Claude Code (empresa-modelo).")
        pg.route("**/api/agente/comprobar", lambda r: r.fulfill(json={
            "ok": True, "version": "2.1.319", "seconds": 1.4, "ruta": "C:/x/claude.exe",
            "origen": "Claude Code de VS Code", "modelo_usado": "empresa-modelo", "aviso": aviso}))
        pg.route("**/api/agente/estado", lambda r: r.fulfill(json={
            "encontrado": True, "label": "Claude Code", "origen": "Claude Code de VS Code"}))
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        # Procesado con la opción de Claude (pedida y sin aplicar): su modelo y «Comprobar», junto a él
        pg.evaluate("() => openSettings({sec: 'procesado', proc: 'agente'})")
        pg.wait_for_selector("#agModelo", timeout=5000)
        pg.click("#agCheck")
        pg.wait_for_function("document.querySelector('#agRes').innerText.includes('responde')", timeout=5000)
        res = pg.inner_text("#agRes")
        self.assertIn("Claude Code de VS Code 2.1.319 responde (1,4 s)", res)
        self.assertIn(aviso, res)
        self.assertNotIn("· modelo", res)  # con el aviso, el modelo ya va dicho en él
        self.assertIn("Claude Code de VS Code 2.1.319", pg.inner_text("#agEstado"))
        pg.click("#modal [data-m='cerrar']")
        pg.wait_for_function("document.querySelector('#modal').classList.contains('hidden')", timeout=5000)
        pg.evaluate("abrirConfigurar()")
        pg.check("input[name='wzModo'][value='agente']")
        pg.click("[data-wz='sig']")
        pg.wait_for_selector(".dialog.wz h2:has-text('Conecta Claude Code')", timeout=5000)
        pg.wait_for_function("document.querySelector('[data-wz-encontrado]').innerText.includes('VS Code')",
                             timeout=10000)
        self.assertEqual(pg.inner_text("[data-wz-encontrado]"), "Instalado en este PC (Claude Code de VS Code)")
        paso = pg.inner_text(".dialog.wz")
        for trozo in ("Entra con tu cuenta", "Login successful", "pulsa c para copiar el enlace",
                      "La app de escritorio de Claude no basta"):
            self.assertIn(trozo, paso.replace("\n", " "))
        self.assertTrue(pg.is_visible("[data-wz='powershell']"))
        pg.keyboard.press("Escape")
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [], red)

    def test_tarjeta_con_listas_de_una_carpeta_conectada(self):
        """Rama vocab-carpeta (msg 1148): si existe vocabConectar (perfil.js de esa rama), la tarjeta ofrece «Usar las
        listas de una carpeta…» en Proyecto y en el panel del vocabulario; elegirla no cambia el proyecto y, al volver,
        la tarjeta sigue con lo escrito. Aquí vocabConectar es de prueba, con su firma: vocabConectar(after)."""
        pg, errores, red = self.pagina()
        devs = {"mics": [], "outputs": [], "mic": "default", "sys": "all"}
        pg.route("**/api/rec/devices*", lambda r: r.fulfill(json=devs))
        pg.route("**/api/rec/monitor", lambda r: r.fulfill(json={"ok": True}))
        nada = {"teams": None, "outlook": None, "reason": None, "sugerencia": {}}
        pg.route("**/api/reunion/actual", lambda r: r.fulfill(json=nada))
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        # (dentro de una función: evaluate llama a la función si la expresión acaba en una)
        pg.evaluate("() => { window.vocabLlamadas = 0; window.vocabConectar = after => { window.vocabLlamadas++;"
                    " setTimeout(after, 50); }; }")
        pg.click("#btnRec")
        pg.wait_for_selector(".dialog.gr [data-fila='carpeta'] .gr-v code", timeout=15000)
        # un nombre con signos que no admite un archivo: se dice cómo se guardará (y se graba así)
        pg.fill("#grNombre", "Comité: Q4")
        pg.wait_for_function("document.querySelector('#grNombreTag').textContent.includes('se guarda como')",
                             timeout=5000)
        self.assertEqual(pg.inner_text("#grNombreTag"), "se guarda como «Comité Q4»")
        pg.fill("#grNombre", "Mi reunión")
        pg.wait_for_function("!document.querySelector('#grNombreTag').textContent.includes('se guarda como')",
                             timeout=5000)
        antes = pg.input_value("#grProy")
        self.assertIn("Usar las listas de una carpeta…", pg.inner_text("#grProy"))
        pg.select_option("#grProy", "__conectar")
        pg.wait_for_function("window.vocabLlamadas === 1", timeout=5000)
        pg.wait_for_selector(".dialog.gr [data-fila='carpeta'] .gr-v code", timeout=15000)
        self.assertEqual(pg.input_value("#grNombre"), "Mi reunión")  # se vuelve a la tarjeta con lo escrito
        self.assertEqual(pg.input_value("#grProy"), antes)  # «Usar las listas…» no es un proyecto
        pg.click("[data-gr-cambiar='vocab']")
        pg.wait_for_selector("[data-panel='vocab'] [data-gr-ir='conectar']", timeout=5000)
        pg.keyboard.press("Escape")
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [], red)

    def test_sin_token_no_hay_sesion(self):
        """La pagina se sirve, pero sin la cookie la API contesta 403 y la pagina lo dice (no una pantalla muerta)."""
        pg, _, _ = self.pagina()
        r = pg.goto(self.base + "/", wait_until="load")
        self.assertEqual(r.status, 200)
        pg.wait_for_selector("#main:has-text('Sesión no válida')", timeout=10000)
        self.assertIn("acceso directo", pg.inner_text("#main"))

    def test_version_anterior_abierta_se_dice(self):
        """La pagina de esta version con la API de una anterior (sin app_name ni perfil): un aviso claro, no botones
        muertos."""
        pg, errores, red = self.pagina()

        def vieja(route):
            r = route.fetch()
            d = r.json()
            d.pop("app_name", None)
            d.pop("perfil", None)
            d["version"] = "30/10/2026 18:37"
            route.fulfill(response=r, json=d)
        pg.route("**/api/config", vieja)
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main:has-text('versión anterior')", timeout=10000)
        self.assertIn("30/10/2026", pg.inner_text("#main"))
        pg.click("[data-nav='r']")  # los clics no revientan: la ruta espera a la configuracion
        time.sleep(0.5)
        self.assertEqual(self.limpiar(errores, red)[0], [])

    def test_servidor_lento_se_dice_y_los_botones_no_revientan(self):
        """/api/config tarda (02/11/2026: 8 minutos por una importacion): la pagina dice que espera, con los segundos,
        y pulsar ⚙ o Reuniones da un aviso o nada, nunca un error de JavaScript. La peticion se retiene en Playwright
        (sin dormir en el manejador, que bloquearia su bucle) y se suelta al final: se pinta la ruta elegida."""
        pg, errores, red = self.pagina()
        retenidas = []
        pg.route("**/api/config", lambda route: retenidas.append(route))  # retenida hasta soltarla
        pg.goto(f"{self.base}/?t={self.token}", wait_until="commit")
        pg.wait_for_selector("#main:has-text('tarda en contestar')", timeout=10000)
        self.assertEqual(len(retenidas), 1)
        pg.click("#btnSettings")
        pg.wait_for_function("document.querySelector('#toasts').innerText.includes('arrancando')", timeout=5000)
        pg.click("[data-nav='r']")
        time.sleep(0.5)
        self.assertIn("tarda en contestar", pg.inner_text("#main"))  # sigue esperando, sin reventar
        retenidas[0].continue_()
        pg.wait_for_selector("#main .page:has-text('Reuniones')", timeout=15000)  # al llegar la configuracion,
        self.assertEqual(pg.evaluate("location.hash"), "#/r/")                     # se pinta la ruta elegida
        self.assertEqual(self.limpiar(errores, red)[0], [])

    def api_post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"X-Chuleta-Token": self.token, "X-Audios": "1",
                                              "Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=10))

    def test_vocabulario_desde_una_carpeta_conectada(self):
        """Ajustes › Proyectos, Listas de vocabulario (msg 1148): la carpeta conectada sale con sus listas y lo que se
        salta; sus workstreams, con «carpeta conectada»; «quitar» la quita sin borrar nada. La conexión va por la API:
        el selector de carpetas de Windows no se puede pulsar desde aquí."""
        listas = os.path.join(self.tmp, "Listas de prueba")
        os.makedirs(listas)
        with open(os.path.join(listas, "Infra.txt"), "w", encoding="utf-8") as f:
            f.write("Kubernetes\nVPN\n")
        with open(os.path.join(listas, "Glosario.txt"), "w", encoding="utf-8") as f:
            f.write("x" * 70000)
        r = self.api_post("/api/vocab/link/add", {"path": listas})
        self.assertEqual((r["listas"], [a["motivo"] for a in r["avisos"]]), (1, ["grande"]))
        pg, errores, red = self.pagina()
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.click("#btnSettings")
        pg.wait_for_selector("#modal:not(.hidden) h3:has-text('Ajustes')", timeout=5000)
        pg.click(".aj-nav [data-sec='vocabulario']")
        pg.wait_for_selector("#sVocabCarpetas:has-text('Listas de prueba')", timeout=10000)
        caja = pg.inner_text("#sVocabCarpetas")
        self.assertIn("1 lista", caja)
        self.assertIn("pesa 69 KB", caja)
        self.assertIn("carpeta conectada", pg.inner_text("#sVocab"))
        self.assertIn("Infra", pg.inner_text("#sVocab"))
        for boton in ("Usar las listas de una carpeta…", "Copiar listas de otra carpeta…"):
            self.assertTrue(pg.is_visible(f"#sVocab button:has-text('{boton}')"), boton)
        pg.click("#sVocabCarpetas [data-vunlink]")
        pg.wait_for_selector("#sVocabCarpetas:has-text('Ninguna')", timeout=10000)
        self.assertNotIn("Infra", pg.inner_text("#sVocab"))
        self.assertEqual(sorted(os.listdir(listas)), ["Glosario.txt", "Infra.txt"])  # nada borrado
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], err)
        self.assertEqual(red, [], red)

    def test_quitar_la_carpeta_del_tema_del_perfil_deja_guardar(self):
        """Quitar la carpeta de listas de la que sale el tema de «Perfil de reunión»: el servidor pone el tema en
        automático y Ajustes también, también con algo a medio escribir y un subdiálogo que reabre Ajustes: lo escrito
        se guarda y el tema queda en automático (revisión de la fusión con ux-grabar, 09/11/2026: el selector se
        quedaba con la lista quitada y «Guardar» daba 400 «Ese tema de vocabulario ya no existe»; con un borrador, lo
        dejaba en «Sin vocabulario»)."""
        listas = os.path.join(self.tmp, "Listas del perfil")
        os.makedirs(listas)
        for n in ("Uno", "Dos"):
            with open(os.path.join(listas, n + ".txt"), "w", encoding="utf-8") as f:
                f.write("alfa, beta\n")
        rel = self.api_post("/api/vocab/link/add", {"path": listas})["rel"]
        self.addCleanup(self.quitar_carpeta_de_listas, rel)
        self.addCleanup(self.api_post, "/api/settings", {"perfil": {"vocab": None, "context": ""}})
        vid = next(v["id"] for v in self.api_get("/api/vocab")["items"] if v.get("conectada") and v["name"] == "Dos")
        self.api_post("/api/settings", {"perfil": {"vocab": vid}})
        pg, errores, red = self.pagina()
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.click("#btnSettings")
        pg.wait_for_selector("#modal:not(.hidden) h3:has-text('Ajustes')", timeout=5000)
        self.assertEqual(pg.input_value("#pfVocab"), vid)
        pg.click(".aj-nav [data-sec='algrabar']")
        pg.fill("#pfContext", "Comité semanal")  # a medio escribir (se guarda en la pausa o al salir)
        pg.click(".aj-nav [data-sec='vocabulario']")
        pg.wait_for_selector("#sVocabCarpetas:has-text('Listas del perfil')", timeout=10000)
        pg.click("#sVocabCarpetas [data-vunlink]")
        pg.wait_for_selector("#sVocabCarpetas:has-text('Ninguna')", timeout=10000)
        pg.wait_for_function("document.querySelector('#pfVocab').value === '__auto'", timeout=5000)
        pg.click("#sVocab [data-vedit]")  # un subdiálogo que reabre Ajustes (editar el tema que queda) y Cancelar
        pg.wait_for_selector("#modal #veTerms", timeout=5000)
        pg.click("#modal [data-m='no']")
        pg.wait_for_selector("#modal:not(.hidden) h3:has-text('Ajustes')", timeout=5000)
        self.assertEqual((pg.input_value("#pfVocab"), pg.input_value("#pfContext")), ("__auto", "Comité semanal"))
        pg.click("#modal [data-m='cerrar']")
        pg.wait_for_selector("#modal.hidden", state="attached", timeout=5000)
        perfil = self.api_get("/api/config")["perfil"]
        self.assertEqual((perfil["vocab"], perfil["context"]), (None, "Comité semanal"))
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], err)
        self.assertEqual(red, [], red)

    def api_get(self, path):
        return json.load(urllib.request.urlopen(urllib.request.Request(
            self.base + path, headers={"X-Chuleta-Token": self.token}), timeout=10))

    def quitar_carpeta_de_listas(self, rel):
        try:
            self.api_post("/api/vocab/link/remove", {"rel": rel})
        except urllib.error.HTTPError:
            pass  # ya quitada

    def test_copiar_para_correo_con_formato(self):
        """«Copiar para correo» (mock 08): al portapapeles van text/html y text/plain, sin Markdown ni minutos y sin
        firma de IA. En WebView2 lo prueba test_portapapeles_webview2 (runner de Windows)."""
        import wave
        stem = os.path.join(self.root, "Reuniones", "2026-11-06 10-00-00 - Comite")
        os.makedirs(os.path.dirname(stem), exist_ok=True)
        with wave.open(stem + ".wav", "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\0\0" * 16000)
        with open(stem + ".txt", "w", encoding="utf-8") as f:
            f.write("hola\n")
        with open(stem + ".md", "w", encoding="utf-8") as f:
            f.write("---\ntitulo: Comité de seguimiento\nparticipantes: Ana, Luis\n---\n")
        with open(stem + ".resumen.md", "w", encoding="utf-8") as f:
            f.write("---\nmodelo: prueba\n---\n\n## Resumen\nLa migración va **en plazo** [1:12].\n\n"
                    "## Próximos pasos\n- [ ] Luis: escalar los permisos [2:05]\n\n## Participantes\n- Ana\n- Luis\n")
        ctx = self.browser.new_context(bypass_csp=True)
        self.addCleanup(ctx.close)
        ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=self.base)
        pg = ctx.new_page()
        errores = []
        pg.on("pageerror", lambda e: errores.append(f"pageerror: {e}"))
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.evaluate("location.hash = '#/a/' + encodeURIComponent('Reuniones/2026-11-06 10-00-00 - Comite.wav')")
        pg.wait_for_selector("[data-act='sum-copy']:has-text('Copiar para correo')", timeout=15000)
        pg.click("[data-act='sum-copy']")
        pg.wait_for_function("document.querySelector('#toasts').innerText.includes('Copiado para correo')",
                             timeout=5000)
        self.assertNotIn("sin formato", pg.inner_text("#toasts"))
        r = pg.evaluate("""async () => {
          const items = await navigator.clipboard.read(), out = {types: items.flatMap(i => i.types)};
          out.html = await (await items[0].getType('text/html')).text();
          out.text = await (await items[0].getType('text/plain')).text();
          return out; }""")
        self.assertIn("text/html", r["types"])
        self.assertIn("text/plain", r["types"])
        self.assertIn("<b>Luis</b>", r["html"])
        self.assertIn("Comité de seguimiento", r["html"])
        self.assertTrue(r["text"].startswith("Comité de seguimiento · "), r["text"])
        self.assertIn("• Luis: escalar los permisos", r["text"])
        for marca in ("##", "[ ]", "**", "[1:12]", "IA"):
            self.assertNotIn(marca, r["text"], marca)
        self.assertEqual(errores, [])

    def test_fallo_de_las_voces_no_sale_como_de_la_transcripcion(self):
        """fix-subir-memoria (09/11/2026, msg 1378): con la reunión ya transcrita, un fallo de «Quién habla» salía
        también como «No se pudo transcribir» con «Reintentar» (lastJobFor no miraba el tipo) y con la traza de ONNX
        en crudo. Ahora: la transcripción sigue «Transcrito», el error va solo en la barra de voces y en su aviso, y
        en una frase."""
        import wave
        rel = "Reuniones/2026-11-09 19-38-00 - Audio viejo.wav"
        stem = os.path.join(self.root, *os.path.splitext(rel)[0].split("/"))
        os.makedirs(os.path.dirname(stem), exist_ok=True)
        with wave.open(stem + ".wav", "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\0\0" * 16000)
        with open(stem + ".srt", "w", encoding="utf-8") as f:
            f.write("1\n00:00:00,000 --> 00:00:01,000\nHola a todos\n")
        onnx = ("Non-zero status code returned while running Concat node. Name:'/decoder/_pooling/Concat_4' Status "
                "Message: bad allocation")
        sys.path.insert(0, os.path.join(REPO, "_app"))
        import explicar
        tx = {"id": "t1", "path": rel, "name": "a.wav", "preset": "cloud", "engine": "cloud", "status": "done",
              "progress": 1, "stage": "Listo", "error": None, "kind": "transcribe", "created": 1, "elapsed": 40}
        voz = {"id": "v1", "path": rel, "name": "a.wav", "preset": "voces", "engine": None, "status": "running",
               "progress": 0.4, "stage": "Identificando voces… (bloque 1/2)", "error": None, "kind": "voces",
               "created": 2, "elapsed": 10}
        estado = {"voz": voz}

        def jobs(route):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"jobs": [tx, estado["voz"]], "movimientos": []}))

        def config(route):
            r = route.fetch()
            d = r.json()
            d["voices"] = {"enabled": True, "models_ready": True}
            route.fulfill(response=r, body=json.dumps(d))

        pg, errores, red = self.pagina()
        pg.route("**/api/jobs", jobs)
        pg.route("**/api/config", config)
        # antes de cerrar la página: un route.fetch que siga en vuelo al cerrarla daba TargetClosedError en el
        # new_page del test siguiente (ui-smoke de Windows, run 42686380142)
        self.addCleanup(pg.unroute_all, behavior="ignoreErrors")
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.evaluate("p => location.hash = '#/a/' + encodeURIComponent(p)", rel)
        pg.wait_for_function("document.querySelector('#aJob') && document.querySelector('#aJob').innerText"
                             ".includes('Identificando voces')", timeout=15000)
        # mientras, en la lista no dice «Transcribiendo 40 %» ni sale de «Transcritas»: ya está transcrita
        self.assertIn("Voces 40 %", pg.evaluate("p => statusPill(S.audios.get(p))", rel))
        self.assertEqual(pg.evaluate("p => statusOf(S.audios.get(p))", rel), "done")
        # termina en error: lo que manda el servidor (Job.public) es el error técnico y su aviso en una frase
        estado["voz"] = dict(voz, status="error", stage="Error", error=onnx, progress=0.4,
                             aviso=explicar.explicar(onnx))
        pg.wait_for_function("document.querySelector('#aVoz').innerText.includes('No se pudieron identificar')",
                             timeout=15000)
        pg.wait_for_function("document.querySelector('#toasts').innerText.includes('No se pudieron identificar')",
                             timeout=15000)
        self.assertNotIn("No se pudo transcribir", pg.inner_text("#aJob"))
        self.assertEqual(pg.locator("#aJob [data-act='tx-toggle']").count(), 0)  # ni su «Reintentar»
        for donde in ("#aVoz", "#toasts"):
            texto = pg.inner_text(donde)
            self.assertIn("le ha faltado memoria", texto, donde)
            self.assertNotIn("Concat", texto, donde)
            self.assertNotIn("No se pudo transcribir", texto, donde)
        # en la lista sigue «Transcrito», no «Error»
        self.assertEqual(pg.evaluate("p => statusOf(S.audios.get(p))", rel), "done")
        self.assertIn("Transcrito", pg.evaluate("p => statusPill(S.audios.get(p))", rel))
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [])
        self.assertEqual(red, [])

    # ---------------------------------------------------------------- Ajustes: se guarda al cambiarlo (punto 12)

    def abrir_ajustes(self, pg, sec="algrabar"):
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.click("#btnSettings")
        pg.wait_for_selector("#modal .dialog.aj", timeout=5000)
        pg.click(f".aj-nav [data-sec='{sec}']")

    def guardado_junto_a(self, pg, sel, txt="Guardado ✓"):
        """Espera el «Guardado ✓» (o el error) junto al control."""
        pg.wait_for_function("""([s, t]) => { const el = document.querySelector(s); if (!el) return false;
            const m = el._ajMarca;  // la marca de ese control (junto a él, fuera de su <label>)
            return !!m && m.isConnected && m.classList.contains('aj-ok') && m.textContent.includes(t); }""",
                             arg=[sel, txt], timeout=8000)

    def test_ajustes_se_guarda_al_cambiar_cada_seccion(self):
        """Punto 12 del review (14/11/2026): una sola forma de guardar. Cada control se guarda al cambiarlo, con
        «Guardado ✓» a su lado, y al volver a abrir la app sigue así. El pie solo tiene «Cerrar»."""
        pg, errores, red = self.pagina()
        cfg0 = None
        try:
            self.abrir_ajustes(pg)
            cfg0 = pg.evaluate("api('/api/config')")
            pie = pg.inner_text("#modal .aj-pie")
            self.assertIn("Cerrar", pie)
            self.assertNotIn("Guardar", pie)
            self.assertNotIn("Cancelar", pie)
            self.assertEqual(pg.locator("#modal .aj-pend").count(), 0)
            otro = "pt" if cfg0["language"] != "pt" else "de"
            directo = not (cfg0.get("rec_directo") is not False)
            hilos = "1" if str(cfg0["threads"]) != "1" else "2"
            cambios = [  # (sección, selector, cómo, valor esperado al volver)
                ("algrabar", "#sLang", lambda: pg.select_option("#sLang", otro), otro),
                ("algrabar", "#agDirecto", lambda: pg.set_checked("#agDirecto", directo), directo),
                ("algrabar", "#pfContext", lambda: pg.fill("#pfContext", "Comités r12"), "Comités r12"),
                ("vocabulario", "#sPrompt", lambda: pg.fill("#sPrompt", "Alfa, Beta, r12"), "Alfa, Beta, r12"),
                ("procesado", "#pfPreset", lambda: pg.select_option("#pfPreset", "turbo"), "turbo"),
                ("resumen", "#sSumLang", lambda: pg.select_option("#sSumLang", "en"), "en"),
                ("resumen", "#sExtra", lambda: pg.fill("#sExtra", "Céntrate en fechas r12"), "Céntrate en fechas r12"),
                ("calendario", "#rnAviso", lambda: pg.set_checked("#rnAviso", False), False),
                ("avanzado", "#lAuto", lambda: pg.set_checked("#lAuto", not cfg0["live_auto"]), not cfg0["live_auto"]),
                ("avanzado", "#sThreads", lambda: (pg.fill("#sThreads", hilos), pg.press("#sThreads", "Tab")), hilos),
            ]
            for sec, sel, hacer, _ in cambios:
                pg.click(f".aj-nav [data-sec='{sec}']")
                if sel == "#rnAviso":  # depende de «Llamada detectada» (Al grabar)
                    pg.wait_for_function("!document.querySelector('#rnAviso').disabled", timeout=5000)
                hacer()
                self.guardado_junto_a(pg, sel)
            pg.click("#modal [data-m='cerrar']")
            pg.wait_for_selector("#modal .dialog.aj", state="detached", timeout=5000)
            # recargar: lo que se ve es lo guardado
            self.abrir_ajustes(pg)
            for sec, sel, _, esperado in cambios:
                pg.click(f".aj-nav [data-sec='{sec}']")
                if isinstance(esperado, bool):
                    self.assertEqual(pg.is_checked(sel), esperado, sel)
                else:
                    self.assertEqual(pg.input_value(sel), esperado, sel)
            c = pg.evaluate("api('/api/config')")
            self.assertEqual((c["language"], c["preset"], c["summary"]["lang"], c["perfil"]["context"]),
                             (otro, "turbo", "en", "Comités r12"))
            pg.keyboard.press("Escape")
            err, red = self.limpiar(errores, red)
            self.assertEqual(err, [], (err, red))
            self.assertEqual(red, [], red)
        finally:
            if cfg0:  # como estaba, para los demás tests
                pg.evaluate("""c => api('/api/settings', {language: c.language, rec_directo: c.rec_directo !== false,
                    perfil: {context: c.perfil.context || ''}, prompt: c.prompt || '', preset: c.preset,
                    summary: {lang: c.summary.lang || 'es', extra: c.summary.extra || ''},
                    rec_aviso: c.rec_aviso !== false, live_auto: c.live_auto, threads: c.threads})""", cfg0)

    def test_ajustes_dos_cambios_seguidos_y_un_error(self):
        """Punto 12: dos cambios rápidos seguidos (el mismo control dos veces y otro a la vez) se guardan los dos, en
        orden y sin pisarse: una petición a la vez, cada una con el config_rev que deja la anterior. Si una no se puede
        guardar, el control vuelve a lo guardado y lo dice a su lado."""
        pg, errores, red = self.pagina()
        enviados, en_vuelo, maximo = [], [0], [0]
        lentas = []

        def settings(route):
            b = json.loads(route.request.post_data or "{}")
            enviados.append(b)
            en_vuelo[0] += 1
            maximo[0] = max(maximo[0], en_vuelo[0])
            lentas.append(route)
        cfg0 = None
        try:
            self.abrir_ajustes(pg)
            cfg0 = pg.evaluate("api('/api/config')")
            pg.route("**/api/settings", settings)
            a, b = ("pt", "de") if cfg0["language"] not in ("pt", "de") else ("fr", "it")
            pg.select_option("#sLang", a)
            pg.wait_for_function("document.querySelector('#sLang')._ajMarca?.textContent.includes('Guardando')",
                                 timeout=5000)
            pg.select_option("#sLang", b)  # mientras la primera sigue en el aire
            pg.set_checked("#agDirecto", not (cfg0.get("rec_directo") is not False))
            pg.wait_for_timeout(300)
            self.assertEqual(len(lentas), 1)  # la segunda espera a la primera
            # se sueltan de una en una, contra el servidor de verdad
            while lentas:
                r = lentas.pop(0)
                en_vuelo[0] -= 1
                r.continue_()
                pg.wait_for_timeout(400)
            self.guardado_junto_a(pg, "#sLang")
            self.guardado_junto_a(pg, "#agDirecto")
            self.assertEqual(maximo[0], 1)
            self.assertEqual([x.get("language") for x in enviados], [a, b])
            self.assertIn("rec_directo", enviados[1])  # los dos últimos cambios, juntos
            self.assertNotEqual(enviados[0]["rev"], enviados[1]["rev"])
            c = pg.evaluate("api('/api/config')")
            self.assertEqual(c["language"], b)
            self.assertEqual(c["rec_directo"], not (cfg0.get("rec_directo") is not False))
            # un fallo: vuelve a lo guardado y lo dice junto al control
            pg.unroute("**/api/settings")
            pg.route("**/api/settings", lambda r: r.fulfill(status=400, json={"error": "fallo de prueba r12"}))
            pg.select_option("#sLang", a)
            self.guardado_junto_a(pg, "#sLang", "No se ha guardado: fallo de prueba r12")
            self.assertEqual(pg.input_value("#sLang"), b)
            pg.unroute("**/api/settings")
            pg.keyboard.press("Escape")
        finally:
            if cfg0:
                pg.evaluate("c => api('/api/settings', {language: c.language, rec_directo: c.rec_directo !== false})",
                            cfg0)

    def test_ajustes_otra_ventana_no_se_pisa(self):
        """Punto 12 y config_rev: lo que cambia otra ventana no se pisa. Un cambio suelto se guarda y Ajustes se repinta
        con lo de la otra ventana; una lista entera (las copias en otras carpetas) con un config_rev viejo no se guarda
        (409) y se dice."""
        pg, errores, red = self.pagina()
        cfg0 = None
        try:
            self.abrir_ajustes(pg)
            cfg0 = pg.evaluate("api('/api/config')")
            viejo = cfg0["config_rev"]
            # la API, directamente: un rev viejo con una lista entera no se guarda
            r = pg.evaluate("""async rev => { await api('/api/settings', {summary: {lang: 'same'}});
                try { await api('/api/settings', {perfil: {outputs: []}, rev}); return 'guardado'; }
                catch (e) { return e.status + ' ' + e.message; } }""", viejo)
            self.assertTrue(r.startswith("409 Los ajustes han cambiado desde que abriste Ajustes"), r)
            # y un cambio suelto sí, diciendo que había otro
            r = pg.evaluate("rev => api('/api/settings', {summary: {lang: 'es'}, rev})", viejo)
            self.assertTrue(r["otro_cambio"])
            r = pg.evaluate("rev => api('/api/settings', {summary: {lang: 'en'}, rev})", r["config_rev"])
            self.assertFalse(r["otro_cambio"])
            # en la interfaz: otra ventana cambia el idioma; al cambiar aquí otra cosa, se ve el suyo
            pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
            pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
            pg.click("#btnSettings")
            pg.wait_for_selector("#modal .dialog.aj", timeout=5000)
            pg.wait_for_timeout(500)
            otro = "pt" if cfg0["language"] != "pt" else "de"
            with urllib.request.urlopen(urllib.request.Request(  # otra ventana (sin pasar por esta página)
                    self.base + "/api/settings", data=json.dumps({"language": otro}).encode(), method="POST",
                    headers={"Content-Type": "application/json", "X-Audios": "1", "X-Chuleta-Token": self.token,
                             "Cookie": "; ".join(f"{k['name']}={k['value']}" for k in pg.context.cookies())}),
                    timeout=10) as resp:
                self.assertEqual(resp.status, 200)
            self.assertNotEqual(pg.input_value("#sLang"), otro)
            pg.set_checked("#agDirecto", not (cfg0.get("rec_directo") is not False))
            pg.wait_for_function(f"document.querySelector('#modal #sLang')?.value === '{otro}'", timeout=8000)
            pg.wait_for_selector(".toast:has-text('se ha puesto al día')", timeout=5000)
            directo = not (cfg0.get("rec_directo") is not False)
            self.assertEqual(pg.evaluate("api('/api/config')")["rec_directo"], directo)
            pg.keyboard.press("Escape")
        finally:
            if cfg0:
                pg.evaluate("c => api('/api/settings', {language: c.language, rec_directo: c.rec_directo !== false, "
                            "summary: {lang: c.summary.lang || 'es'}})", cfg0)

    def test_ajustes_proyectos_y_procesado_mas_cortos(self):
        """Punto 37: las listas de vocabulario van en su sección; la primera columna de Proyectos se recorta con «…» en
        vez de montarse sobre la de al lado; Procesado es la tarjeta con «Probar» y «Cambiar…», sin las tres opciones
        (viven en el asistente)."""
        pg, errores, red = self.pagina()
        self.abrir_ajustes(pg, "proyectos")
        pg.wait_for_selector("#pjTabla [data-pjw-editar]", timeout=10000)
        self.assertEqual(pg.locator("[data-secc='proyectos'] #sVocab").count(), 0)
        estilo = pg.evaluate("""() => { const td = document.querySelector('#pjTabla tr.pj-w td');
            const cs = getComputedStyle(td);
            return [cs.overflow, cs.textOverflow, cs.whiteSpace, !!td.title]; }""")
        self.assertEqual(estilo, ["hidden", "ellipsis", "nowrap", True])
        pg.click(".aj-nav [data-sec='vocabulario']")
        pg.wait_for_selector("[data-secc='vocabulario'] #sVocab [data-vnew]", timeout=10000)
        pg.click(".aj-nav [data-sec='procesado']")
        self.assertEqual(pg.locator("#modal input[name='proc']").count(), 0)
        self.assertEqual(pg.locator("#procAplicar").count(), 0)
        self.assertTrue(pg.is_visible("#modal [data-cfg='probar']"))
        pg.click("#modal [data-cfg='cambiar']")
        pg.wait_for_selector("#modal .dialog.wz input[name='wzModo']", timeout=5000)
        pg.keyboard.press("Escape")
        pg.wait_for_selector("#modal .aj-nav [data-sec='procesado'].on", timeout=5000)  # vuelve a Procesado
        pg.keyboard.press("Escape")
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [], red)

    def test_ajustes_revision_1(self):
        """Revisión 1 de ajustes-uno (Fable 5.2): la tarjeta de Procesado se pone al día al guardar un motor (P1-1); un
        campo vaciado no vuelve solo mientras se escribe y, al salir vacío, vuelve y lo dice (P1-3); el valor que ajusta
        el servidor se dice (P2-5); la marca va fuera del <label> (P2-6) y al final de la fila de micro y audio (P2-10);
        un texto que no se ha podido guardar no se borra (P2-4)."""
        pg, errores, red = self.pagina()
        cfg0 = None
        try:
            self.abrir_ajustes(pg, "procesado")
            cfg0 = pg.evaluate("api('/api/config')")
            locales = [p for p in cfg0["presets"] if p["engine"] == "local"]
            otro = next(p for p in locales if p["id"] != cfg0["live_preset"] and p["model"] != next(
                q["model"] for q in locales if q["id"] == cfg0["live_preset"]))
            tarjeta0 = pg.inner_text("#modal .cfg-estado")
            pg.select_option("#lPreset", otro["id"])
            self.guardado_junto_a(pg, "#lPreset")
            pg.wait_for_function("t => document.querySelector('#modal .cfg-estado').innerText !== t", arg=tarjeta0,
                                 timeout=5000)
            # vaciar los hilos para escribir otro número: no vuelve solo; al salir vacío, sí, y lo dice
            pg.click(".aj-nav [data-sec='avanzado']")
            hilos = str(cfg0["threads"])
            pg.fill("#sThreads", "")
            pg.wait_for_timeout(1200)
            self.assertEqual(pg.input_value("#sThreads"), "")
            pg.press("#sThreads", "Tab")
            self.guardado_junto_a(pg, "#sThreads", "no puede quedar vacío")
            self.assertEqual(pg.input_value("#sThreads"), hilos)
            # un número que el servidor ajusta, con el foco aún en la casilla: «Guardado como «N» ✓»
            pg.fill("#sThreads", "999")
            self.guardado_junto_a(pg, "#sThreads", "Guardado como «")
            # la marca de una casilla, fuera de su <label> y como «status»
            pg.click(".aj-nav [data-sec='avanzado']")
            pg.set_checked("#lAuto", not cfg0["live_auto"])
            self.guardado_junto_a(pg, "#lAuto")
            marca = pg.evaluate("(() => { const el = document.querySelector('#lAuto'); return"
                                " [el.closest('label').contains(el._ajMarca), el._ajMarca.getAttribute('role')]; })()")
            self.assertEqual(marca, [False, "status"])
            # micro y audio del PC: la marca al final de la fila, no entre los dos desplegables
            pg.click(".aj-nav [data-sec='algrabar']")
            pg.select_option("#agMic", "none")
            self.guardado_junto_a(pg, "#agMic")
            self.assertTrue(pg.evaluate("(() => { const m = document.querySelector('#agMic')._ajMarca; return"
                                        " m.parentElement.classList.contains('ag-dos')"
                                        " && m.previousElementSibling.id !== 'agMic'; })()"))
            pg.select_option("#agMic", cfg0["rec_mic"])
            self.guardado_junto_a(pg, "#agMic")
            # un texto que no se ha podido guardar se queda escrito, con su error
            pg.route("**/api/settings", lambda r: r.fulfill(status=500, json={"error": "fallo pasajero r1"}))
            pg.click(".aj-nav [data-sec='vocabulario']")
            pg.fill("#sPrompt", "Texto largo que no se pierde r1")
            pg.press("#sPrompt", "Tab")
            # (un 500: la frase de siempre y no el texto técnico, punto 11 del review)
            self.guardado_junto_a(pg, "#sPrompt", "Algo ha fallado dentro de")
            self.assertEqual(pg.input_value("#sPrompt"), "Texto largo que no se pierde r1")
            pg.unroute("**/api/settings")
            pg.keyboard.press("Escape")
            err, red = self.limpiar(errores, [r for r in red if "/api/settings" not in r])
            self.assertEqual(err, [], (err, red))
            self.assertEqual(red, [], red)
        finally:
            if cfg0:
                pg.unroute_all(behavior="ignoreErrors")
                pg.evaluate("c => api('/api/settings', {live_preset: c.live_preset, threads: c.threads, "
                            "live_auto: c.live_auto, rec_mic: c.rec_mic, prompt: c.prompt || ''})", cfg0)

    def test_ajustes_revision_1_claves_repintado_y_rev(self):
        """Revisión 1: guardar una clave adicional no repinta Ajustes (el clic con el que se sale de la casilla llega a
        su botón, P1-2) y quitar una ya guardada se pregunta antes (P2-7); el repintado por un cambio de otra ventana
        conserva la opción pedida y el foco (P2-1, P2-2); y el config_rev es el de lo que enseña Ajustes, no el de
        S.config: una lista vieja da 409 aunque otra cosa haya puesto S.config al día (P2-3)."""
        pg, errores, red = self.pagina()
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        self.addCleanup(self.api_post, "/api/settings", {"cloud": {"extra_keys_by": {"groq": []}},
                                                         "perfil": {"outputs": []}})
        # con la API (solo en esta página): Avanzado enseña las claves adicionales
        pg.evaluate("S.config.procesado = 'api'")
        pg.click("#btnSettings")
        pg.click(".aj-nav [data-sec='avanzado']")
        pg.click("#dkAdd")
        pg.fill("#dkRows [data-xkey]", "gsk_adicional_r1_000000000000")
        pg.click("#dkAdd")  # sale de la casilla (se guarda) y añade otra fila
        pg.wait_for_function("document.querySelector('#dkAdd').parentElement.querySelector('.aj-ok')"
                             "?.textContent.includes('Guardado')", timeout=8000)
        self.assertEqual(pg.locator("#dkRows .dk-row").count(), 2)
        self.assertEqual(len(pg.evaluate("api('/api/config')")["cloud"]["extra_keys_by"]["groq"]), 1)
        self.assertIsNotNone(pg.get_attribute("#dkRows .dk-row:first-child [data-xkey]", "data-mask"))  # enmascarada
        # quitarla: se pregunta (encima); «Cancelar» la deja
        pg.click("#dkRows .dk-row:first-child [data-xkey-del]")
        pg.wait_for_selector("#modalSobre:not(.hidden) .dialog", timeout=5000)
        pg.click("#modalSobre [data-m='no']")
        self.assertEqual(pg.locator("#dkRows [data-mask]").count(), 1)
        pg.click("#dkRows .dk-row:first-child [data-xkey-del]")
        pg.click("#modalSobre:not(.hidden) [data-m='ok']")
        pg.wait_for_function("api('/api/config').then(c => !(c.cloud.extra_keys_by?.groq || []).length)", timeout=8000)
        pg.keyboard.press("Escape")
        # repintado por otra ventana: con la opción pedida y el foco donde estaba
        pg.evaluate("() => openSettings({sec: 'procesado', proc: 'agente'})")
        pg.wait_for_selector("#agModelo", timeout=5000)
        pg.focus("#agModelo")
        self.api_post("/api/settings", {"summary": {"lang": "same"}})  # otra ventana
        pg.evaluate("window.dispatchEvent(new Event('focus'))")
        pg.wait_for_selector(".toast:has-text('se ha puesto al día')", timeout=5000)
        self.assertTrue(pg.is_visible("#ajProcPedido"))
        pg.wait_for_function("document.activeElement?.id === 'agModelo'", timeout=3000)
        pg.keyboard.press("Escape")
        # el rev de lo que se ve: otra ventana deja una copia; aquí S.config se pone al día sin repintar Ajustes
        # (como al vincular una carpeta) y la lista de aquí, vieja, no la pisa
        destino = os.path.join(self.tmp, "copias r1")
        os.makedirs(destino, exist_ok=True)
        pg.click("#btnSettings")
        pg.click(".aj-nav [data-sec='proyectos']")
        pg.wait_for_timeout(1500)  # que acabe lo que mira Ajustes al abrir (si no, se repinta con el rev nuevo)
        self.api_post("/api/settings", {"summary": {"lang": "es"}})
        pg.evaluate("api('/api/config').then(setConfig)")
        pg.evaluate("p => { perfilOuts.push({path: p, format: 'md', by_project: false});"
                    " document.querySelector('#modal .dialog.aj').ajOutsCambiadas(); }", destino)
        pg.wait_for_selector(".toast:has-text('desde que abriste Ajustes')", timeout=8000)
        self.assertEqual(pg.evaluate("api('/api/config')")["perfil"]["outputs"], [])
        pg.keyboard.press("Escape")
        err, red = self.limpiar(errores, [r for r in red if "/api/settings" not in r])
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [], red)

    def test_ajustes_revision_2(self):
        """Revisión 2 de ajustes-uno: un texto que acaba en espacio dice «Guardado ✓», no «Guardado como «todo el
        texto»» (P1-A); quitar una clave guardada justo después de escribir otra la quita (las filas se ponen al día en
        su sitio, P1-B); un texto que no se pudo guardar se reintenta al cerrar (P2-3)."""
        pg, errores, red = self.pagina()
        self.addCleanup(self.api_post, "/api/settings", {"cloud": {"extra_keys_by": {"groq": []}}, "prompt": "",
                                                         "summary": {"extra": ""}})
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.evaluate("S.config.procesado = 'api'")
        pg.click("#btnSettings")
        pg.click(".aj-nav [data-sec='vocabulario']")
        pg.fill("#sPrompt", "Acme, Contoso, ")
        self.guardado_junto_a(pg, "#sPrompt")
        self.assertNotIn("como", pg.evaluate("document.querySelector('#sPrompt')._ajMarca.textContent"))
        # dos claves: la primera guardada; escribir otra y, enseguida, quitar la primera
        pg.click(".aj-nav [data-sec='avanzado']")
        pg.click("#dkAdd")
        pg.fill("#dkRows [data-xkey]", "gsk_primera_r2_00000000000000")
        pg.press("#dkRows [data-xkey]", "Tab")
        pg.wait_for_selector("#dkRows [data-mask]", state="attached", timeout=8000)
        pg.click("#dkAdd")
        pg.fill("#dkRows .dk-row:nth-child(2) [data-xkey]", "gsk_segunda_r2_00000000000000")
        pg.click("#dkRows .dk-row:first-child [data-xkey-del]")  # sale de la casilla (se guarda) y quita la primera
        pg.click("#modalSobre:not(.hidden) [data-m='ok']")
        pg.wait_for_function("api('/api/config').then(c => (c.cloud.extra_keys_by?.groq || []).length === 1"
                             " && c.cloud.extra_keys_by.groq[0].includes('…') && !c.cloud.extra_keys_by.groq[0]"
                             ".startsWith('gsk_pri'))", timeout=8000)
        self.assertEqual(pg.locator("#dkRows .dk-row").count(), 1)
        # un texto que falla una vez: se queda escrito y se vuelve a intentar al cerrar
        pg.click(".aj-nav [data-sec='resumen']")
        pg.route("**/api/settings", lambda r: r.fulfill(status=500, json={"error": "fallo r2"}))
        pg.fill("#sExtra", "Texto que falla r2")
        self.guardado_junto_a(pg, "#sExtra", "Algo ha fallado dentro de")  # (un 500, punto 11)
        pg.unroute("**/api/settings")
        pg.keyboard.press("Escape")
        pg.wait_for_function("api('/api/config').then(c => c.summary.extra === 'Texto que falla r2')", timeout=8000)
        err, red = self.limpiar(errores, [r for r in red if "/api/settings" not in r])
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [], red)

    def test_reuniones_por_proyecto(self):
        """Reuniones › Tabla (punto 43 del review): columna y filtro «Proyecto» en vez de «Carpeta»; fuera las columnas
        sin datos y cada desplegable con su nombre accesible. Otros tests de la clase añaden reuniones al mismo
        servidor: se mira la del montaje («Comité de prueba», de Proyecto › General)."""
        pg, errores, red = self.pagina()
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        # lo de antes, en este navegador: una carpeta que no la contiene y un proyecto que ya no existe
        guardado = "{folder: 'Otra carpeta', proy: 'Ya no existe', group: 'folder', estado: 'Revisada'}"
        pg.evaluate(f"localStorage.setItem('tbl', JSON.stringify({guardado}))")
        pg.reload(wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.click("[data-nav='r']")
        fila = "#main .db-table tbody tr[data-path*='Comité de prueba']"
        pg.wait_for_selector(fila, timeout=10000)
        cab = pg.eval_on_selector_all("#main .db-table thead th", "ths => ths.map(t => t.textContent)")
        datos = pg.evaluate("""() => [allAudios().some(a => ((a.props || {}).etiquetas || []).length),
                                     allAudios().some(a => (a.props || {}).estado)]""")
        esperadas = ["Nombre", "Fecha", "Duración", "Proyecto"] + (["Etiquetas"] if datos[0] else []) \
            + (["Estado"] if datos[1] else []) + ["Transcripción"]
        self.assertEqual(cab, esperadas)
        self.assertEqual(pg.text_content(fila + " td:nth-child(4)").strip(), "Proyecto › General")
        js = "els => els.map(e => [e.dataset.tbl, e.getAttribute('aria-label') || ''])"
        nombres = dict(pg.eval_on_selector_all("#main .db-tools select, #main .db-tools input", js))
        self.assertEqual(nombres["q"], "Filtrar reuniones")
        self.assertEqual(nombres["proy"], "Proyecto")
        self.assertNotIn("folder", nombres)  # con proyectos, el filtro de carpeta se va
        self.assertTrue(all(nombres.values()), nombres)
        # lo guardado de antes (carpeta, proyecto que no existe) ya no filtra, y «por carpeta» pasa a «por proyecto»
        self.assertEqual(pg.input_value("[data-tbl='proy']"), "*")
        self.assertEqual(pg.input_value("[data-tbl='group']"), "proy")
        js = "tds => tds.map(t => t.firstChild.textContent.trim())"
        grupos = pg.eval_on_selector_all("#main .db-table tr.grp td", js)
        self.assertIn("Proyecto", grupos)
        pg.select_option("[data-tbl='proy']", "")
        pg.wait_for_selector(fila, state="detached", timeout=5000)
        pg.select_option("[data-tbl='proy']", "Proyecto")
        pg.wait_for_selector(fila, timeout=5000)
        celdas = pg.eval_on_selector_all("#main .db-table tbody tr[data-path] td:nth-child(4)",
                                         "tds => tds.map(t => t.textContent.trim())")
        self.assertTrue(celdas and all(c.startswith("Proyecto ›") for c in celdas), celdas)
        err, red = self.limpiar(errores, red)
        self.assertEqual(err, [], (err, red))
        self.assertEqual(red, [], red)

    # ---------------------------------------------------------------- errores que se leen (punto 11)

    def test_avisos_de_error_no_caducan_y_se_releen(self):
        """Punto 11 del review: un aviso de error ya no se va a los 9 s; se cierra con ✕ y lleva el botón de
        explicar.py, que hace lo suyo sin abrir la reunión. Un 500 dice «Algo ha fallado dentro de Tenca», con el texto
        de Python plegado en «Detalle». La Cola tiene «Avisos» para releerlos (también tras recargar), y el botón de
        un error de la Cola funciona (el clic se paraba en la Cola y no llegaba a configurar.js)."""
        rel = "Proyecto/2026-10-30 10-00-00 - Comité de prueba.wav"
        aviso = {"frase": "Claude Code no tiene la sesión iniciada: entra con tu cuenta y vuelve a probar.",
                 "accion": {"tipo": "procesado", "etiqueta": "Revisar en Ajustes"}, "tecnico": "Please run /login"}
        job = {"id": "t9", "path": rel, "name": "a.wav", "preset": "rapido", "engine": "local", "status": "running",
               "progress": 0.3, "stage": "Transcribiendo", "error": None, "kind": "transcribe", "created": 1,
               "elapsed": 5}
        estado = {"job": job}

        def jobs(route):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"jobs": [estado["job"]], "movimientos": []}))

        def roto(route):
            route.fulfill(status=500, content_type="application/json",
                          body=json.dumps({"error": "'NoneType' object has no attribute 'get'"}))
        pg, errores, red = self.pagina()
        pg.route("**/api/jobs", jobs)
        pg.route("**/api/acerca/abrir", roto)
        self.addCleanup(pg.unroute_all, behavior="ignoreErrors")
        pg.goto(f"{self.base}/?t={self.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.evaluate("sessionStorage.removeItem('tenca.avisos')")
        estado["job"] = dict(job, status="error", stage="Error", error="Please run /login", aviso=aviso)
        t = pg.locator("#toasts .toast.err", has_text="No se pudo transcribir")
        t.wait_for(timeout=15000)
        self.assertIn("Claude Code no tiene la sesión iniciada", t.inner_text())
        self.assertEqual(t.locator("button.toast-x[aria-label='Cerrar este aviso']").count(), 1)
        self.assertEqual(t.locator("[data-cfg-accion='procesado']").inner_text(), "Revisar en Ajustes")
        time.sleep(9.5)  # se iba a los 9 s
        self.assertEqual(t.count(), 1)
        # su botón abre lo suyo (Ajustes › Procesado) y no la reunión; el aviso sigue
        antes = pg.evaluate("location.hash")
        t.locator("[data-cfg-accion]").click()
        pg.wait_for_selector("#modal .dialog.aj section[data-secc='procesado']:not([hidden])", timeout=5000)
        self.assertEqual(pg.evaluate("location.hash"), antes)
        self.assertEqual(t.count(), 1)
        pg.keyboard.press("Escape")
        pg.wait_for_selector("#modal.hidden", state="attached", timeout=5000)
        # ✕ lo cierra, sin abrir la reunión
        t.locator(".toast-x").click()
        self.assertEqual(t.count(), 0)
        self.assertEqual(pg.evaluate("location.hash"), antes)
        # un 500: la frase, y el texto técnico solo al desplegar «Detalle»; el mismo dos veces no se apila
        llamar = "() => api('/api/acerca/abrir', { que: 'datos' }).catch(e => toast(esc(e.message), 'err'))"
        pg.evaluate(llamar)
        pg.evaluate(llamar)
        f = pg.locator("#toasts .toast.err", has_text="Algo ha fallado dentro de")
        f.wait_for(timeout=5000)
        self.assertEqual(f.count(), 1)
        self.assertNotIn("NoneType", f.inner_text())
        f.locator("summary:has-text('Detalle')").click()
        self.assertIn("'NoneType' object has no attribute 'get'", f.locator(".toast-tec").inner_text())
        self.assertEqual(f.count(), 1)  # desplegar «Detalle» no lo cierra
        # «Avisos» en la Cola: los dos, el último primero; el 500 dos veces
        pg.click("#btnJobs")
        pg.wait_for_selector("#jobsPanel .ph-avisos", timeout=5000)
        items = pg.eval_on_selector_all("#jobsPanel .aviso-item", "xs => xs.map(x => x.innerText)")
        self.assertEqual(len(items), 2, items)
        self.assertIn("Algo ha fallado dentro de", items[0])
        self.assertIn("2 veces", items[0])
        self.assertIn("No se pudo transcribir", items[1])
        # el botón del error en la Cola (su fila y su aviso) también llega a configurar.js
        pg.click("#jobsPanel .jitem [data-cfg-accion='procesado']")
        pg.wait_for_selector("#modal .dialog.aj section[data-secc='procesado']:not([hidden])", timeout=5000)
        pg.keyboard.press("Escape")
        pg.wait_for_selector("#modal.hidden", state="attached", timeout=5000)
        # tras recargar, siguen en la Cola; «Borrar avisos» los quita
        pg.reload(wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        pg.click("#btnJobs")
        pg.wait_for_selector("#jobsPanel .aviso-item", timeout=5000)
        self.assertEqual(pg.locator("#jobsPanel .aviso-item").count(), 2)
        pg.click("#jobsPanel [data-jact='avisos-borrar']")
        self.assertEqual(pg.locator("#jobsPanel .aviso-item, #jobsPanel .ph-avisos").count(), 0)
        # los 500 de la prueba (y su eco en la consola) y el sondeo de la Cola cortado al recargar
        red = [r for r in red if "/api/acerca/abrir" not in r and r != f"fallo {self.base}/api/jobs"]
        err, red = self.limpiar([e for e in errores if "status of 500" not in e], red)
        self.assertEqual((err, red), ([], []))


if __name__ == "__main__":
    unittest.main()
