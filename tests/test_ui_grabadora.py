# -*- coding: utf-8 -*-
"""La grabación cortada y «Tenca no responde» en un navegador real (Playwright + Chromium), con la app de verdad y una
grabadora de mentira (tests/fake_recorder/recorder.py copiada encima de _app/recorder.py) que se muere a los 1,5 s y
vuelve a grabar en la continuación (review del 14/11/2026, punto 1; se vio matando el servidor con la app abierta):

- la grabadora se muere: franja roja «Grabación interrumpida a las hh:mm» con el motivo, un aviso que no se va solo
  (sigue a los 10 s; los de error se iban a los 9 s) y nunca «■ Grabación guardada»; sigue grabando en la
  «(continuación)» y lo dice; la reunión cortada lleva «Grabación interrumpida a las …»; ✕ lo cierra y no vuelve;
- el servidor no contesta (peticiones que fallan, que se cuelgan y, al final, el servidor muerto): tras 3 sondeos,
  franja «Tenca no responde: puede que no se esté grabando · Reabrir», que se quita sola al volver; con el tope de
  5 s una petición colgada ya no deja la página muda; y «Grabar» dice «Tenca no responde», no «Failed to fetch».
Capturas en NOTAS_ARTEFACTOS (ui-smoke.yml las sube). Se salta sin Playwright."""
import json
import os
import shutil
import time
import unittest

from support import REPO, nuevo_contexto

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - sin Playwright el test se salta
    sync_playwright = None

FAKE_REC = os.path.join(REPO, "tests", "fake_recorder", "recorder.py")


@unittest.skipUnless(sync_playwright, "playwright no instalado")
class GrabacionCortadaUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_ui_hoy import Servidor
        import tempfile
        cls.ctl = os.path.join(tempfile.mkdtemp(prefix="tenca-ui-grab-"), "ctl.json")
        cls.runs = cls.ctl + ".arranques"
        cls.poner(["vive"])

        def preparar(root):
            shutil.copy(FAKE_REC, os.path.join(root, "_app", "recorder.py"))
            os.makedirs(os.path.join(root, "Alfa"))
        cls.srv = Servidor(preparar, None, {"FAKE_REC_CTL": cls.ctl})
        cls.art = os.environ.get("NOTAS_ARTEFACTOS")
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        for cerrar in (lambda: cls.browser.close(), lambda: cls.pw.stop(), lambda: cls.srv.cerrar(),
                       lambda: shutil.rmtree(os.path.dirname(cls.ctl), ignore_errors=True)):
            try:
                cerrar()
            except Exception:
                pass

    @classmethod
    def poner(cls, modos, muere_s=1.5):
        with open(cls.ctl, "w", encoding="utf-8") as f:
            json.dump({"modos": modos, "muere_s": muere_s, "runs": cls.runs}, f)
        if os.path.exists(cls.runs):
            os.remove(cls.runs)

    def pagina(self):
        ctx = nuevo_contexto(self.browser, bypass_csp=True, viewport={"width": 1500, "height": 920})
        ctx.add_init_script("localStorage.setItem('procElegido', '1'); localStorage.setItem('avisoProbar', '1')")
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        self.errores = []
        pg.on("pageerror", lambda e: self.errores.append(f"pageerror: {e}"))
        pg.goto(f"{self.srv.base}/?t={self.srv.token}", wait_until="load")  # grabando, la red nunca para
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        # todos los avisos que salen, aunque se vayan
        pg.evaluate("""() => { window.__toasts = []; new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(
            n => window.__toasts.push(n.textContent)))).observe(document.querySelector('#toasts'),
            { childList: true }); }""")
        return pg

    def captura(self, pg, nombre):
        if self.art:
            os.makedirs(self.art, exist_ok=True)
            pg.screenshot(path=os.path.join(self.art, f"grabadora_{nombre}.png"), full_page=True)

    def grabar(self, pg):
        return pg.evaluate("() => api('/api/rec/start', { mic: 'default', sys: 'all', folder: 'Alfa', live: false, "
                           "name: 'Comité' })")

    def test_1_cortada_se_ve_no_caduca_y_sigue(self):
        self.poner(["muere", "vive"])
        pg = self.pagina()
        r = self.grabar(pg)
        franja = pg.locator("[data-franja^='corte-']")
        franja.wait_for(timeout=20000)
        txt = franja.inner_text()
        self.assertRegex(txt, r"Grabación interrumpida a las \d\d:\d\d")
        self.assertIn("Input/output error", txt)
        self.captura(pg, "cortada")
        # la continuación: graba otra vez y lo dice
        pg.wait_for_function("() => (window.__toasts || []).some(t => t.includes('Sigue grabando en «'))",
                             timeout=15000)
        pg.wait_for_function("() => S.rec.recording && S.rec.path.includes('(continuación)')", timeout=5000)
        time.sleep(10)  # los avisos de error se iban a los 9 s: este no
        pegado = pg.locator(".toast.keep", has_text="Grabación interrumpida")
        self.assertEqual(pegado.count(), 1)
        self.assertFalse(pg.evaluate("() => window.__toasts.some(t => t.includes('Grabación guardada'))"),
                         pg.evaluate("() => window.__toasts"))
        # la reunión cortada lo dice en su cabecera
        pg.click("[data-franja^='corte-'] [data-franja-act='ver']")
        pg.wait_for_selector("#aHead .interrumpida", timeout=10000)
        self.assertRegex(pg.inner_text("#aHead .interrumpida"), r"Grabación interrumpida a las \d\d:\d\d · ")
        self.assertEqual(pg.evaluate("() => S.route.path"), r["path"])
        self.captura(pg, "reunion_cortada")
        # ✕ lo cierra (franja y aviso) y al recargar no vuelve
        pg.click("[data-franja^='corte-'] [data-franja-act='cerrar']")
        self.assertEqual(pg.locator("[data-franja^='corte-']").count(), 0)
        self.assertEqual(pg.locator(".toast.keep").count(), 0)
        pg.reload(wait_until="load")
        time.sleep(2.5)
        self.assertEqual(pg.locator("[data-franja^='corte-']").count(), 0)
        pg.evaluate("() => api('/api/rec/stop', {})")
        self.assertEqual(self.errores, [])

    def test_1b_la_continuacion_que_no_arranca(self):
        # revision 3: el aviso fijo decía «Sigo grabando…» para siempre aunque la continuación no arrancase, y la
        # página acababa en «■ Grabación guardada»
        self.poner(["muere", "nace"])
        pg = self.pagina()
        self.grabar(pg)
        # (el corte de la prueba anterior también sale: el servidor los guarda 12 h y esta página no lo ha cerrado)
        pg.wait_for_function("() => [...document.querySelectorAll('.toast.keep')].some(t => "
                             "t.textContent.includes('No se siguió grabando'))", timeout=25000)
        self.assertFalse(pg.evaluate("() => [...document.querySelectorAll('.toast.keep')].some(t => "
                                     "t.textContent.includes('Intentando seguir'))"))
        self.assertIn("No se siguió grabando", pg.locator("[data-franja^='corte-']").last.inner_text())
        time.sleep(2)
        self.assertFalse(pg.evaluate("() => window.__toasts.some(t => t.includes('Grabación guardada'))"),
                         pg.evaluate("() => window.__toasts"))
        self.captura(pg, "continuacion_no_arranca")
        self.assertEqual(self.errores, [])

    def test_2_sin_respuesta_franja_que_se_quita_sola(self):
        self.poner(["vive"])
        pg = self.pagina()
        self.grabar(pg)
        pg.wait_for_function("() => S.rec.recording", timeout=10000)
        latido = pg.locator("[data-franja='latido']")
        # peticiones que fallan (como con el servidor caído): tras 3 sondeos, la franja
        pg.route("**/api/**", lambda route: route.abort())
        latido.wait_for(timeout=15000)
        self.assertEqual(latido.inner_text().replace("\n", " ").split("Reabrir")[0].strip(),
                         "Tenca no responde: puede que no se esté grabando")
        self.captura(pg, "no_responde")
        # «Grabar» con el servidor caído: «Tenca no responde», no «Failed to fetch»
        pg.evaluate("() => toggleRec()")
        pg.wait_for_function("() => window.__toasts.some(t => t.includes('Tenca no responde'))", timeout=5000)
        self.assertFalse(pg.evaluate("() => window.__toasts.some(t => /Failed to fetch/i.test(t))"))
        # vuelve: se quita sola
        pg.unroute("**/api/**")
        latido.wait_for(state="detached", timeout=10000)
        # una petición colgada (sin respuesta): el tope de 5 s la corta y la franja sale igual
        pg.route("**/api/rec/status*", lambda route: None)
        latido.wait_for(timeout=40000)
        pg.unroute("**/api/rec/status*")
        latido.wait_for(state="detached", timeout=15000)
        pg.evaluate("() => api('/api/rec/stop', {})")
        # el servidor muerto de verdad (lo último: ya no vuelve)
        self.srv.proc.terminate()
        self.srv.proc.wait(15)
        latido.wait_for(timeout=15000)
        self.assertIn("no responde", latido.inner_text())
        self.assertEqual(self.errores, [])


if __name__ == "__main__":
    unittest.main()
