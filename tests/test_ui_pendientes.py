# -*- coding: utf-8 -*-
"""«Mis pendientes» (review del 14/11/2026, punto 28) en un navegador real (Playwright + Chromium), con la app de
verdad.

- Hoy: la tarjeta «Mis pendientes» con «Tuyas» por defecto y «Todas», el minuto que abre la reunión en ese punto, y la
  casilla, que marca la línea del .resumen.md; la fila se queda un momento con «Deshacer», que la devuelve.
- La vista del resumen: sus casillas también se pulsan y cambian el .resumen.md (y se vuelven a pintar).
Usa el servidor y la página de test_ui_hoy. Capturas en NOTAS_ARTEFACTOS (ui-smoke.yml las sube). Se salta sin
Playwright."""
import unittest
from datetime import datetime, timedelta

from test_ui_hoy import Base, reunion

RESUMEN_1 = ("---\nmodelo: prueba\ngenerado: 2026-11-15 10:00\n---\n\n## Resumen\nTodo bien.\n\n"
             "## Próximos pasos\n- [ ] Ana: preparar el presupuesto [12:34]\n- [ ] Yo: escribir el acta [0:05]\n"
             "- [x] Luis: reservar sala\n")
RESUMEN_2 = "## Resumen\nOk.\n\n## Próximos pasos\n- [ ] Tú: revisar el contrato\n- [ ] Carmen: enviar las cifras\n"


class PendientesTests(Base):
    @classmethod
    def preparar(cls, root):
        now = datetime.now().replace(second=0, microsecond=0)
        cls.comite = reunion(root, "Alfa", now - timedelta(days=1), "Comité de seguimiento", 30,
                             {"titulo": "Comité de seguimiento"}, txt=True, resumen=RESUMEN_1) + ".resumen.md"
        cls.revision = reunion(root, "Beta", now - timedelta(days=3), "Revisión del contrato", 20,
                               {"titulo": "Revisión del contrato"}, txt=True, resumen=RESUMEN_2) + ".resumen.md"

    def leer(self, p):
        with open(p, encoding="utf-8") as f:
            return f.read()

    def esperar(self, pg):
        pg.wait_for_selector(".hoy-pendientes h2:has-text('Mis pendientes')", timeout=30000)

    def filas(self, pg):
        return [t.split("\n")[0] for t in pg.locator(".hoy-pendientes .mp-fila").all_inner_texts()]

    def test_hoy_marcar_y_deshacer(self):
        pg = self.pagina()
        self.esperar(pg)
        caja = pg.inner_text(".hoy-pendientes")
        self.assertIn("Tuyas (2)", caja)
        self.assertIn("Todas (4)", caja)
        self.assertEqual(self.filas(pg), ["Yo: escribir el acta", "Tú: revisar el contrato"])  # primero las tuyas
        pg.click("[data-act='hoy-pend-filtro'][data-f='todas']")
        self.assertEqual(self.filas(pg), ["Ana: preparar el presupuesto", "Yo: escribir el acta",
                                          "Tú: revisar el contrato", "Carmen: enviar las cifras"])  # la más reciente
        self.assertIn("Comité de seguimiento · ", pg.inner_text(".hoy-pendientes .mp-fila >> nth=0"))
        self.captura(pg, "pendientes_1500x920")
        # la casilla: la línea del .resumen.md pasa a «- [x]» y la fila se queda con «Deshacer»
        fila = pg.locator(".hoy-pendientes .mp-fila", has_text="escribir el acta")
        with pg.expect_response(lambda r: "/api/pendientes/hecha" in r.url and r.status == 200):
            fila.locator("[data-act='hoy-pend-hecha']").click()
        pg.wait_for_selector(".hoy-pendientes .mp-fila.hecha:has-text('escribir el acta') "
                             "[data-act='hoy-pend-deshacer']", timeout=5000)
        self.assertIn("- [x] Yo: escribir el acta [0:05]", self.leer(self.comite))
        self.assertIn("Todas (3)", pg.inner_text(".hoy-pendientes"))
        self.captura(pg, "pendientes_hecha")
        with pg.expect_response(lambda r: "/api/pendientes/hecha" in r.url and r.status == 200):
            pg.click(".hoy-pendientes [data-act='hoy-pend-deshacer']")
        pg.wait_for_selector(".hoy-pendientes .mp-fila:not(.hecha):has-text('escribir el acta') .mp-box", timeout=5000)
        self.assertIn("- [ ] Yo: escribir el acta [0:05]", self.leer(self.comite))
        self.assertIn("Todas (4)", pg.inner_text(".hoy-pendientes"))
        # el minuto abre la reunión en ese punto
        pg.click(".hoy-pendientes .mp-fila:has-text('presupuesto') [data-act='hoy-pend-min']")
        pg.wait_for_function("location.hash.startsWith('#/a/') && location.hash.includes('Comit') "
                             "&& location.hash.includes('t=754')", timeout=5000)
        self.sin_errores()

    def test_marcar_desde_la_vista_del_resumen(self):
        pg = self.pagina()
        self.esperar(pg)
        pg.click(".hoy-pendientes .mp-fila:has-text('escribir el acta') [data-act='hoy-pend-ir']")
        pg.wait_for_selector("#aSum .sumview li.todo button.todo-box", timeout=10000)
        li = "#aSum .sumview li.todo:has-text('preparar el presupuesto')"
        self.assertEqual(pg.inner_text(li + " button"), "☐")
        with pg.expect_response(lambda r: "/api/pendientes/hecha" in r.url and r.status == 200):
            pg.click(li + " button")
        pg.wait_for_selector(li + ".done button:has-text('☑')", timeout=5000)
        self.assertIn("- [x] Ana: preparar el presupuesto [12:34]", self.leer(self.comite))
        self.assertIn("- [x] Luis: reservar sala", self.leer(self.comite))  # lo demás, igual
        self.captura(pg, "pendientes_resumen")
        # la que ya estaba hecha se desmarca igual
        with pg.expect_response(lambda r: "/api/pendientes/hecha" in r.url and r.status == 200):
            pg.click("#aSum .sumview li.todo:has-text('reservar sala') button")
        pg.wait_for_selector("#aSum .sumview li.todo:not(.done):has-text('reservar sala')", timeout=5000)
        self.assertIn("- [ ] Luis: reservar sala", self.leer(self.comite))
        # y vuelta a como estaba (las dos)
        for t in ("preparar el presupuesto", "reservar sala"):
            with pg.expect_response(lambda r: "/api/pendientes/hecha" in r.url and r.status == 200):
                pg.click(f"#aSum .sumview li.todo:has-text('{t}') button")
            pg.wait_for_function(f"[...document.querySelectorAll('#aSum li.todo')].some(l => l.textContent.includes("
                                 f"'{t}') && l.querySelector('button')?.textContent !== "
                                 f"'{'☐' if t == 'reservar sala' else '☑'}')", timeout=5000)
        self.assertIn("- [ ] Ana: preparar el presupuesto [12:34]", self.leer(self.comite))
        self.assertIn("- [x] Luis: reservar sala", self.leer(self.comite))
        self.sin_errores()


if __name__ == "__main__":
    unittest.main()
