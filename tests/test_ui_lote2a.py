# -*- coding: utf-8 -*-
"""Lote 2a del motor en un navegador real (Playwright + Chromium), con la app de verdad:

- arranca con unos ajustes raros escritos a mano (punto 5): la interfaz abre Hoy y Ajustes sin errores y el
  diagnóstico dice qué ajustes se corrigieron;
- Ajustes › Acerca de › «Buscar versión nueva» (punto 60): nada consulta GitHub hasta pulsarlo; al pulsarlo, una
  petición (a un GitHub falso en local) y el aviso con el enlace a su Release;
- un hilo vigía parado (punto 33): el aviso sale una vez y se queda;
- «Copiar diagnóstico» lleva el PC despierto, los hilos y los informes técnicos de las últimas reuniones (puntos 30, 33
  y 32), sin nombres.
Capturas en NOTAS_ARTEFACTOS (ui-smoke.yml las sube). Se salta sin Playwright."""
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from support import nuevo_contexto

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - sin Playwright el test se salta
    sync_playwright = None

RELEASE = {"tag_name": "v2099.01.02-abc1234", "html_url": "https://github.com/bastegman/tenca/releases/tag/"
           "v2099.01.02-abc1234", "published_at": "2099-01-02T10:00:00Z"}


class GitHubFalso(BaseHTTPRequestHandler):
    peticiones = []

    def do_GET(self):
        GitHubFalso.peticiones.append((self.path, self.headers.get("User-Agent")))
        cuerpo = json.dumps(RELEASE).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, *a):
        pass


@unittest.skipUnless(sync_playwright, "playwright no instalado")
class Lote2aUI(unittest.TestCase):
    # unos ajustes escritos a mano con valores que antes dejaban la app sin arrancar o «cargando»
    AJUSTES = {"links": 7, "cloud": {"provider": [], "parallel": "3"}, "preset": "turbo-que-no-existe",
               "worker_keepalive_min": ["x"], "language": 7}

    @classmethod
    def setUpClass(cls):
        from test_ui_hoy import Servidor
        cls.gh = ThreadingHTTPServer(("127.0.0.1", 0), GitHubFalso)
        threading.Thread(target=cls.gh.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{cls.gh.server_address[1]}/repos/bastegman/tenca/releases/latest"

        def preparar(root):
            os.makedirs(os.path.join(root, "Alfa"))
            # la versión de un zip (en la CI la copia de _app no la lleva: sería «sin versión»)
            with open(os.path.join(root, "_app", "version.txt"), "w", encoding="utf-8") as f:
                f.write("2026-11-15 · 1234567\n")
        cls.srv = Servidor(preparar, cls.AJUSTES, {"CHULETA_PRUEBAS_URL_RELEASES": url})
        cls.art = os.environ.get("NOTAS_ARTEFACTOS")
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        for cerrar in (lambda: cls.browser.close(), lambda: cls.pw.stop(), lambda: cls.srv.cerrar(),
                       lambda: cls.gh.shutdown()):
            try:
                cerrar()
            except Exception:
                pass

    def pagina(self):
        ctx = nuevo_contexto(self.browser, bypass_csp=True, viewport={"width": 1500, "height": 920})
        ctx.add_init_script("localStorage.setItem('procElegido', '1'); localStorage.setItem('avisoProbar', '1')")
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        self.errores, self.peticiones, self.red = [], [], []
        pg.on("pageerror", lambda e: self.errores.append(f"pageerror: {e}"))
        pg.on("response", lambda r: self.red.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        pg.on("console", lambda m: self.errores.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        pg.on("request", lambda r: self.peticiones.append(r.url))
        pg.goto(f"{self.srv.base}/?t={self.srv.token}", wait_until="networkidle")
        pg.wait_for_selector("#main h1:has-text('Hoy')", timeout=15000)
        return pg

    def sin_errores(self):
        """Como test_ui_smoke: cuentan todos menos los 4xx permitidos (sin soundcard, /api/rec/devices) y su eco."""
        from test_ui_smoke import PERMITIDAS
        red = [r for r in self.red if not any(p in r for p in PERMITIDAS)]
        err = [e for e in self.errores if not (not red and "Failed to load resource" in e)]
        self.assertEqual((err, red), ([], []))

    def captura(self, pg, nombre):
        if self.art:
            os.makedirs(self.art, exist_ok=True)
            pg.screenshot(path=os.path.join(self.art, f"lote2a_{nombre}.png"), full_page=True)

    def acerca_de(self, pg):
        pg.click("#btnSettings")
        pg.wait_for_selector("#modal:not(.hidden) h3:has-text('Ajustes')", timeout=5000)
        pg.click(".aj-nav [data-sec='ayuda']")  # Ayuda: Acerca de
        boton = pg.locator("#aVersion")
        boton.scroll_into_view_if_needed()
        return boton

    def test_1_con_ajustes_raros_abre_y_el_diagnostico_lo_dice(self):
        pg = self.pagina()
        self.acerca_de(pg)
        self.assertIn("Acerca de", pg.inner_text("#modal"))
        d = pg.evaluate("() => api('/api/diagnostico')")["texto"]
        linea = next(l for l in d.splitlines() if l.startswith("Ajustes corregidos al arrancar:"))
        for k in ("links", "cloud.provider", "cloud.parallel", "preset", "worker_keepalive_min", "language"):
            self.assertIn(k, linea)
        self.assertIn("Hilos vigía: todos en marcha", d)
        self.assertTrue(any(l.startswith("PC despierto:") for l in d.splitlines()))
        self.assertIn("Informes técnicos de las últimas reuniones: ninguno todavía", d)
        c = pg.evaluate("() => api('/api/config')")
        self.assertEqual((c["preset"], c["cloud"]["parallel"]), ("rapido", 3))
        self.sin_errores()

    def test_2_buscar_version_nueva_solo_al_pulsar(self):
        GitHubFalso.peticiones.clear()
        pg = self.pagina()
        boton = self.acerca_de(pg)
        self.assertIn("nunca la hace sola", pg.inner_text("#aVersionRes"))
        time.sleep(1.5)
        self.assertEqual(GitHubFalso.peticiones, [])  # abrir la app y Acerca de no consulta nada
        self.assertFalse(any("/api/version/buscar" in u for u in self.peticiones))
        boton.click()
        pg.wait_for_selector("#aVersionRes:has-text('Hay una versión más nueva')", timeout=15000)
        txt = pg.inner_text("#aVersionRes")
        self.assertIn("2099-01-02 · abc1234", txt)
        self.assertIn("publicada el 2/1/2099", txt)
        self.assertEqual(len(GitHubFalso.peticiones), 1)  # una sola
        self.assertEqual(GitHubFalso.peticiones[0], ("/repos/bastegman/tenca/releases/latest", "Tenca"))
        self.captura(pg, "version_nueva")
        # «Ver la versión» se abre en el navegador del sistema desde el servidor (aquí no se abre nada)
        abiertos = []
        pg.route("**/api/acerca/abrir", lambda r: (abiertos.append(r.request.post_data_json),
                                                   r.fulfill(json={"ok": True})))
        pg.click("#aVersionVer")
        pg.wait_for_function("() => true")
        fin = time.time() + 5
        while not abiertos and time.time() < fin:
            pg.wait_for_timeout(100)
        self.assertEqual(abiertos, [{"que": "release"}])
        # sin red: la frase en rojo y el botón otra vez disponible
        pg.route("**/api/version/buscar", lambda r: r.fulfill(
            status=400, json={"error": "No se ha podido consultar GitHub (ConnectionError): sin conexión"}))
        boton.click()
        pg.wait_for_selector("#aVersionRes:has-text('No se ha podido consultar GitHub')", timeout=5000)
        self.assertTrue(boton.is_enabled())
        self.assertEqual([e for e in self.errores if "pageerror" in e], [])

    def test_3_un_hilo_vigia_parado_se_avisa_una_vez(self):
        pg = self.pagina()
        caido = {"id": 1, "hilo": "worker-local", "que": "la cola de transcripción en este PC", "a_las": "10:32",
                 "error": "KeyError"}

        def jobs(route):
            route.fulfill(json={"jobs": [], "movimientos": [], "hilos_caidos": [caido]})
        pg.route("**/api/jobs", jobs)
        aviso = pg.locator(".toast", has_text="Se ha parado una parte de")
        aviso.first.wait_for(timeout=15000)
        time.sleep(9)  # dos sondeos más (cada 4 s): sigue siendo uno, y no se va solo
        self.assertEqual(aviso.count(), 1)
        self.assertIn("la cola de transcripción en este PC", aviso.first.inner_text())
        self.assertIn("Ajustes → Apagar", aviso.first.inner_text())
        self.captura(pg, "hilo_parado")
        self.assertEqual([e for e in self.errores if "pageerror" in e], [])

    def test_4_el_diagnostico_con_los_informes_sin_nombres(self):
        informe = {"formato": 1, "id": "a1b2c3d4", "actualizado": "2026-11-15 10:00", "app": "2026-11-15 · d3c6591",
                   "secciones": {"directo": {"estado": "done", "tramos": [
                       {"motor": "nube", "proveedor": "groq", "modelo": "whisper-large-v3-turbo", "desde_s": 0.0,
                        "hasta_s": 3600.0, "trozos": 600, "fallidos": 2, "perdidos_s": 8.5}],
                       "latencia_s": {"n": 598, "p50": 2.1, "p95": 5.3}, "nota": "Reunión con Ana Pérez"}}}
        with open(os.path.join(self.srv.datos, "informes.json"), "w", encoding="utf-8") as f:
            json.dump({"formato": 1, "informes": [informe]}, f)
        pg = self.pagina()
        d = pg.evaluate("() => api('/api/diagnostico')")["texto"]
        self.assertIn("Informes técnicos de las 1 últimas reuniones (solo números):", d)
        self.assertIn('"modelo":"whisper-large-v3-turbo"', d)
        self.assertNotIn("Ana", d)
        self.assertIn('"nota":"<texto>"', d)
        self.acerca_de(pg)
        self.assertIn("las 3 últimas reuniones en números", pg.inner_text("#modal"))
        self.sin_errores()


if __name__ == "__main__":
    unittest.main()
