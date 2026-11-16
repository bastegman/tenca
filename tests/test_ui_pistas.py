# -*- coding: utf-8 -*-
"""El reproductor con una grabación en dos pistas, en un navegador real (Playwright + Chromium) con la app de verdad
(punto 20 del review del 14/11/2026, pistas.js y pistas-worklet.js):

- al darle a Reproducir a una grabación en dos pistas sale «Pistas» en el reproductor y el audio pasa por Web Audio:
  lo que sale es la mezcla en mono (tu micro a la izquierda y el audio del PC a la derecha, sumados);
- «Tú» a 0 deja solo a los demás, y «Los demás» a 0, solo tu micro; el ajuste se guarda;
- un audio de antes (estéreo con la mezcla) suena tal cual y sin «Pistas»;
- «Exportar audio mezclado» manda lo que se oye y dice dónde lo ha guardado.
Capturas en NOTAS_ARTEFACTOS (ui-smoke.yml las sube). Se salta sin Playwright."""
import json
import os
import struct
import unittest
import wave

import numpy as np

from support import APP  # noqa: F401  (prepara sys.path)
import pistas  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - sin Playwright el test se salta
    sync_playwright = None

SR = 48000


def tono(f, segundos=30.0, amp=0.2):
    t = np.arange(int(segundos * SR)) / SR
    return (np.sin(2 * np.pi * f * t) * amp).astype(np.float32)


def escribir_doble(path, izq, der, eco=b"1"):
    pcm = (np.stack([izq, der], axis=1) * 32767).astype("<i2").tobytes()
    head = bytearray(pistas.cabecera(SR))
    head[pistas.eco_off(SR)] = eco[0]  # lo que escribe la grabadora al cerrar: vio eco (1) o no (0)
    struct.pack_into("<I", head, 4, len(head) - 8 + len(pcm))
    struct.pack_into("<I", head, len(head) - 4, len(pcm))
    with open(path, "wb") as f:
        f.write(bytes(head) + pcm)


def escribir_antes(path, izq, der):
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.stack([izq, der], axis=1) * 32767).astype("<i2").tobytes())


@unittest.skipUnless(sync_playwright, "playwright no instalado")
class ReproductorPistasUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_ui_hoy import Servidor

        def preparar(root):
            os.makedirs(os.path.join(root, "Alfa"))
            # izquierda (tu micro): 0,2 de pico; derecha (los demás): 0,1. RMS 0,141 y 0,071; la suma, 0,158
            escribir_doble(os.path.join(root, "Alfa", "Dos pistas.wav"), tono(300), tono(900, amp=0.1))
            escribir_doble(os.path.join(root, "Alfa", "Sin eco.wav"), tono(300), tono(900, amp=0.1), eco=b"0")
            escribir_doble(os.path.join(root, "Alfa", "En duda.wav"), tono(300), tono(900, amp=0.1), eco=b"?")
            # un estéreo de antes con los dos canales distintos: tiene que seguir sonando así (no mezclado)
            escribir_antes(os.path.join(root, "Alfa", "De antes.wav"), tono(600), tono(600) * 0)
        cls.srv = Servidor(preparar)
        cls.art = os.environ.get("NOTAS_ARTEFACTOS")
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        # como Chrome y WebView2 de serie: sin un gesto del usuario, el audio no arranca (Playwright lo deja pasar)
        cls.browser_gesto = cls.pw.chromium.launch(args=["--autoplay-policy=document-user-activation-required"])

    @classmethod
    def tearDownClass(cls):
        for cerrar in (lambda: cls.browser.close(), lambda: cls.browser_gesto.close(), lambda: cls.pw.stop(),
                       lambda: cls.srv.cerrar()):
            try:
                cerrar()
            except Exception:
                pass

    def pagina(self, navegador=None):
        ctx = (navegador or self.browser).new_context(bypass_csp=False, viewport={"width": 1400, "height": 900})
        ctx.add_init_script("localStorage.setItem('procElegido', '1'); localStorage.setItem('avisoProbar', '1')")
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        self.errores = []
        pg.on("pageerror", lambda e: self.errores.append(f"pageerror: {e}"))
        pg.on("console", lambda m: self.errores.append(f"console: {m.text}") if m.type == "error" else None)
        pg.goto(f"{self.srv.base}/?t={self.srv.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1", timeout=15000)
        return pg

    def captura(self, pg, nombre):
        if self.art:
            os.makedirs(self.art, exist_ok=True)
            pg.screenshot(path=os.path.join(self.art, f"pistas-{nombre}.png"))

    def reproducir(self, pg, rel):
        pg.evaluate("p => { location.hash = '#/a/' + encodeURIComponent(p); }", rel)
        boton = pg.wait_for_selector(f"[data-act='play'][data-path='{rel}']", timeout=15000)
        boton.click()
        pg.wait_for_function("p => S.playing === p && !audio.paused && audio.currentTime > 0.3", arg=rel,
                             timeout=15000)

    def estado(self, pg, espera_s=0.6):
        pg.wait_for_timeout(int(espera_s * 1000))
        pg.evaluate("() => Pistas.estado()")  # vacía los niveles: lo que se mide es lo de después
        pg.wait_for_timeout(int(espera_s * 1000))
        return pg.evaluate("() => Pistas.estado()")

    def test_dos_pistas_se_oyen_mezcladas(self):
        pg = self.pagina()
        self.reproducir(pg, "Alfa/Dos pistas.wav")
        pg.wait_for_selector("#player.pistas #pPistas", state="visible", timeout=10000)
        st = self.estado(pg)
        self.assertTrue(st["montado"] and st["doble"], st)
        self.assertEqual((st["modo"], st["ctx"], st["eco"]), ("doble", "running", True), st)
        self.assertAlmostEqual(st["rms"]["izq"], 0.141, delta=0.01, msg=st)
        self.assertAlmostEqual(st["rms"]["der"], 0.0707, delta=0.008, msg=st)
        self.assertAlmostEqual(st["rms"]["sale"], 0.158, delta=0.012, msg=st)  # la suma, en mono
        self.captura(pg, "reproductor")
        # el panel: «Tú» a 0 → solo los demás; «Los demás» a 0 → solo tu micro
        pg.click("#pPistas")
        pg.wait_for_selector("#pistasPanel:not(.hidden) #ppTu", timeout=5000)
        self.captura(pg, "panel")
        pg.evaluate("() => { const r = document.querySelector('#ppTu'); r.value = 0; "
                    "r.dispatchEvent(new Event('input')); }")
        st = self.estado(pg)
        self.assertAlmostEqual(st["rms"]["sale"], 0.0707, delta=0.008, msg=st)
        self.assertEqual(json.loads(pg.evaluate("() => localStorage.getItem('pistas')"))["tu"], 0)
        pg.evaluate("""() => { for (const [id, v] of [['#ppTu', 1], ['#ppDemas', 0]]) {
            const r = document.querySelector(id); r.value = v; r.dispatchEvent(new Event('input')); } }""")
        st = self.estado(pg)
        self.assertAlmostEqual(st["rms"]["sale"], 0.141, delta=0.012, msg=st)
        self.assertEqual(pg.text_content("#ppDemasV").strip(), "0 %")
        # exportar: lo que se oye, a /api/audio/mezclado (la escritura del archivo la prueban test_dos_pistas)
        pedidos = []

        def falso(route, request):
            pedidos.append(json.loads(request.post_data))
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"destino": "/x/Dos pistas (mezclado).wav",
                                           "nombre": "Dos pistas (mezclado).wav", "segundos": 30.0}))
        pg.route("**/api/audio/mezclado", falso)
        pg.click("#ppExportar")
        pg.wait_for_selector("#toasts :text('Dos pistas (mezclado).wav')", timeout=5000)
        self.assertEqual(pedidos, [{"path": "Alfa/Dos pistas.wav", "tu": 1, "demas": 0, "eco": True}])
        pg.keyboard.press("Escape")
        pg.wait_for_selector("#pistasPanel", state="hidden", timeout=3000)
        self.assertTrue(pg.evaluate("() => document.querySelector('#pPistas').classList.contains('ajustado')"))
        # un audio de antes: tal cual, sin «Pistas»; su estéreo sigue siendo estéreo (izquierda sí, derecha no)
        self.reproducir(pg, "Alfa/De antes.wav")
        pg.wait_for_function("() => !document.querySelector('#player').classList.contains('pistas')", timeout=5000)
        self.assertFalse(pg.is_visible("#pPistas"))
        st = self.estado(pg)
        self.assertEqual(st["modo"], "normal", st)
        self.assertAlmostEqual(st["rms"]["sale"], 0.141, delta=0.012, msg=st)
        self.assertLess(st["rms"]["sale_der"], 0.005, st)
        # una grabada con cascos (eco=0): no hay eco que quitar, la casilla no se puede marcar
        pg.evaluate("() => { const p = Pistas.cfg; p.tu = 1; p.demas = 1; "
                    "localStorage.setItem('pistas', JSON.stringify(p)); }")
        self.reproducir(pg, "Alfa/Sin eco.wav")
        pg.wait_for_selector("#player.pistas #pPistas", state="visible", timeout=10000)
        pg.click("#pPistas")
        pg.wait_for_selector("#pistasPanel:not(.hidden) #ppEco", timeout=5000)
        self.assertFalse(pg.is_checked("#ppEco"))
        self.assertIn("no se vio eco", pg.text_content("#ppEcoT"))
        st = self.estado(pg)
        self.assertIs(st["eco"], False, st)
        self.assertIs(st["quita_eco"], False, st)  # lo que recibe el nodo, no solo la casilla
        self.captura(pg, "sin-eco")
        pedidos = []

        def falso(route, request):
            pedidos.append(json.loads(request.post_data))
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"destino": "/x/m.wav", "nombre": "m.wav", "segundos": 30.0}))
        pg.route("**/api/audio/mezclado", falso)
        pg.click("#ppExportar")
        pg.wait_for_selector("#toasts :text('m.wav')", timeout=5000)
        self.assertIs(pedidos[-1]["eco"], False)
        # se puede marcar igual (la puerta pudo no llegar a verlo): vale para este audio
        pg.click("#ppEco")
        self.assertIs(self.estado(pg)["quita_eco"], True)
        # una en duda («?»: grabándose o cortada sin ver eco): no se quita por defecto, y el panel abierto se repinta
        pg.evaluate("p => loadAudio(S.audios.get(p))", "Alfa/En duda.wav")
        pg.wait_for_function("() => !audio.paused && S.playing === 'Alfa/En duda.wav'", timeout=10000)
        pg.wait_for_function("() => document.querySelector('#ppEcoT').textContent.includes('aún no se sabe')",
                             timeout=5000)
        self.assertFalse(pg.is_checked("#ppEco"))
        st = self.estado(pg)
        self.assertIs(st["quita_eco"], False, st)
        self.assertIsNone(st["eco"], st)
        self.assertEqual(self.errores, [])

    def test_sin_gesto_se_reanuda_al_darle(self):
        # Web Audio montado antes de ningún gesto del usuario (aquí, el audio se carga solo al abrir, sin reproducir: un
        # evaluate de Playwright ya cuenta como gesto): el contexto nace suspendido; al darle a Reproducir se reanuda y
        # suena (sin eso, una grabación en dos pistas quedaría en silencio; revisión 1, mutación M31)
        rel = "Alfa/Dos pistas.wav"
        ctx = self.browser_gesto.new_context(viewport={"width": 1400, "height": 900})
        self.addCleanup(ctx.close)
        ctx.add_init_script("""localStorage.setItem('procElegido', '1'); localStorage.setItem('avisoProbar', '1');
            (function espera() {
              const p = %s;
              if (typeof S !== 'undefined' && S.audios && S.audios.get(p) && typeof Pistas !== 'undefined') {
                location.hash = '#/a/' + encodeURIComponent(p);
                loadAudio(S.audios.get(p), null, false);
                window.__cargado = true;
                return;
              }
              setTimeout(espera, 100);
            })();""" % json.dumps(rel))
        pg = ctx.new_page()
        self.errores = []
        pg.on("pageerror", lambda e: self.errores.append(f"pageerror: {e}"))
        pg.goto(f"{self.srv.base}/?t={self.srv.token}", wait_until="networkidle")
        pg.wait_for_function("() => window.__cargado === true", timeout=15000)
        antes = None
        for _ in range(50):
            antes = pg.evaluate("() => Pistas.estado()")
            if antes["montado"]:
                break
            pg.wait_for_timeout(200)
        self.assertEqual(antes["ctx"], "suspended", antes)
        pg.wait_for_selector(f"[data-act='play'][data-path='{rel}']", timeout=15000)
        pg.click(f"[data-act='play'][data-path='{rel}']")
        pg.wait_for_function("() => !audio.paused && audio.currentTime > 0.3", timeout=15000)
        st = self.estado(pg)
        self.assertEqual(st["ctx"], "running", st)
        self.assertGreater(st["rms"]["sale"], 0.1, st)
        self.assertEqual(self.errores, [])

    def test_un_audio_de_antes_no_monta_web_audio(self):
        pg = self.pagina()
        self.reproducir(pg, "Alfa/De antes.wav")
        pg.wait_for_timeout(500)
        st = pg.evaluate("() => Pistas.estado()")
        self.assertEqual((st["montado"], st["doble"]), (False, False), st)
        self.assertFalse(pg.is_visible("#pPistas"))
        self.assertEqual(self.errores, [])


if __name__ == "__main__":
    unittest.main()
