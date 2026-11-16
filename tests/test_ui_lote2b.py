# -*- coding: utf-8 -*-
"""Rama motor-lote2b en un navegador real (Playwright + Chromium), con la app de verdad.

Punto 17 · qué pasa con el audio de cada reunión:
- la tarjeta de grabar tiene «El audio: Se guarda» y, con «Cambiar», las tres opciones; lo elegido va en el cuerpo
  de /api/rec/start (no se graba de verdad);
- en la página de una reunión transcrita y resumida con «Guardar 15 días», la cabecera dice cuándo irá a la papelera;
  al cambiarla a «Borrar al terminar» en Detalles, el .md lo apunta, el vigilante del servidor la manda a la papelera
  (DATA_DIR/papelera fuera de Windows; la de Windows en la CI de Windows) y la página pasa a «El audio está en la
  papelera», sin «Reproducir»; los textos siguen;
- una reunión sin audio (su marca .audio.json) se abre con sus textos y un minuto pulsado no pide el audio.
Capturas en NOTAS_ARTEFACTOS (ui-smoke.yml las sube). Se salta sin Playwright.
"""
import json
import os
import time
import unittest
from datetime import datetime, timedelta
from urllib.parse import quote

from support import srt_text
from test_ui_hoy import Base, reunion


def textos(base, segundos=60):
    with open(base + ".srt", "w", encoding="utf-8") as f:
        f.write(srt_text([(1.0, 20.0, "Hola a todos, empezamos."), (21.0, segundos - 1, "Repasamos el plan.")]))
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write("Hola a todos, empezamos. Repasamos el plan.\n")


RESUMEN = "---\nmodelo: x\ngenerado: 2026-11-15 10:00\n---\n\n## Resumen\nSe repasa el plan [0:21].\n"


class ConservarTests(Base):
    @classmethod
    def preparar(cls, root):
        now = datetime.now().replace(second=0, microsecond=0)
        cls.quince = reunion(root, "Alfa", now - timedelta(hours=3), "Comité", 1,
                             {"titulo": "Comité", "audio": "15-dias"}, resumen=RESUMEN)
        textos(cls.quince)
        cls.sin = reunion(root, "Alfa", now - timedelta(days=2), "Entrevista", 1,
                          {"titulo": "Entrevista"}, resumen=RESUMEN)
        textos(cls.sin)
        os.remove(cls.sin + ".wav")  # su audio ya está en la papelera: queda la marca
        with open(cls.sin + ".audio.json", "w", encoding="utf-8") as f:
            json.dump({"version": 1, "fase": "borrado", "ext": ".wav", "bytes": 600, "duracion_s": 60.0,
                       "fin": (now - timedelta(days=2)).isoformat(), "borrado": now.isoformat(timespec="seconds")}, f)

    def abrir(self, pg, base):
        rel = os.path.relpath(base + ".wav", self.srv.root).replace(os.sep, "/")
        pg.goto(f"{self.srv.base}/?t={self.srv.token}#/a/{quote(rel)}", wait_until="networkidle")
        pg.wait_for_selector("#aHead .ptitle", timeout=15000)
        return rel

    def test_1_la_tarjeta_manda_lo_elegido(self):
        pg = self.pagina()
        enviado = []

        def start(route):
            enviado.append(json.loads(route.request.post_data))
            route.fulfill(status=400, content_type="application/json",
                          body=json.dumps({"error": "Prueba: no se graba"}))
        pg.route("**/api/rec/start", start)
        pg.evaluate("openRec()")
        pg.wait_for_selector("#modal:not(.hidden) [data-fila='conservar']", timeout=10000)
        fila = pg.inner_text("[data-fila='conservar']")
        self.assertIn("El audio", fila)
        self.assertIn("Se guarda", fila)
        pg.click("[data-gr-cambiar='conservar']")
        pg.wait_for_selector("[data-panel='conservar'] .gr-conservar", timeout=5000)
        panel = pg.inner_text("[data-panel='conservar']")
        for t in ("Guardar", "Guardar 15 días", "Borrar al terminar", "papelera de Windows", "se quedan"):
            self.assertIn(t, panel)
        with pg.expect_response(lambda r: "/api/grabar/plan" in r.url):
            pg.click("[data-panel='conservar'] label:has-text('Borrar al terminar')")
        pg.click("[data-gr-listo='conservar']")
        pg.wait_for_function("document.querySelector('[data-fila=conservar]').textContent.includes('al terminar')",
                             timeout=5000)
        self.assertIn("cambiado para esta reunión", pg.inner_text("[data-fila='conservar'] .gr-origen"))
        self.captura(pg, "conservar_tarjeta")
        pg.click("#rGo")
        pg.wait_for_function("document.querySelector('#toasts').textContent.includes('Prueba: no se graba')",
                             timeout=5000)
        self.assertEqual(enviado[-1]["conservar"], "borrar")
        self.sin_errores(permitidas=("/api/rec/start",))

    def test_2_quince_dias_y_luego_borrar_al_terminar(self):
        pg = self.pagina()
        self.abrir(pg, self.quince)
        pg.wait_for_selector("[data-audio-destino]", timeout=5000)
        linea = pg.inner_text("[data-audio-destino]")
        self.assertIn("irá a la papelera el", linea)
        self.assertIn("15 días después de grabarla", linea)
        self.assertEqual(pg.locator("#aHead [data-act='play']").count(), 1)
        pg.click("[data-act='audio-cambiar']")
        sel = pg.locator("select[data-prop='audio']")
        self.assertEqual(sel.input_value(), "15-dias")
        self.captura(pg, "conservar_15dias")
        with pg.expect_response(lambda r: "/api/notes" in r.url and r.request.method == "POST"):
            sel.select_option("borrar")
        with open(self.quince + ".md", encoding="utf-8") as f:
            self.assertIn("audio: borrar", f.read())
        pg.wait_for_function("document.querySelector('[data-audio-destino]').textContent.includes('en unos minutos')",
                             timeout=5000)
        # el vigilante (despertado al cambiarla, con 20 s de respiro) la manda a la papelera; la página lo ve sola
        fin = time.time() + 90
        while time.time() < fin and os.path.exists(self.quince + ".wav"):
            time.sleep(1)
        self.assertFalse(os.path.exists(self.quince + ".wav"), "el audio no ha ido a la papelera")
        for e in (".srt", ".txt", ".md", ".resumen.md", ".audio.json"):
            self.assertTrue(os.path.isfile(self.quince + e), e)
        pg.wait_for_function("(document.querySelector('[data-audio-destino]')||{}).textContent?.includes('está en "
                             "la papelera')", timeout=30000)
        self.assertEqual(pg.locator("#aHead [data-act='play']").count(), 0)
        self.assertIn("Repasamos el plan", pg.inner_text("#aTx"))
        self.captura(pg, "conservar_borrado")
        self.sin_errores()

    def test_3_sin_audio_no_se_reproduce(self):
        pg = self.pagina()
        media = []
        pg.on("request", lambda r: media.append(r.url) if "/media" in r.url else None)
        self.abrir(pg, self.sin)
        pg.wait_for_selector("[data-audio-destino]", timeout=5000)
        self.assertIn("El audio está en la papelera de Windows desde el", pg.inner_text("[data-audio-destino]"))
        self.assertEqual(pg.locator("#aHead [data-act='play']").count(), 0)
        pg.wait_for_selector("#aTx :text('Repasamos el plan')", timeout=10000)
        pg.click("#aTx .ts >> nth=0")
        pg.wait_for_function("document.querySelector('#toasts').textContent.includes('recupéralo de la papelera')",
                             timeout=5000)
        self.assertEqual(media, [])
        pg.click("#aHead .pdet summary")
        self.assertIn("En la papelera de Windows desde el", pg.inner_text("#aHead .pdet"))
        self.sin_errores()


class QuienHablaParcialTests(Base):
    """Punto 56: «Quién habla» identificado hasta el min 30 mientras se graba con el directo en la nube (el sidecar
    parcial que escribe voces_directo.py): las frases de antes llevan quién habla, las de después no, y la línea de
    voces lo dice."""

    @classmethod
    def preparar(cls, root):
        now = datetime.now().replace(second=0, microsecond=0)
        cls.base = reunion(root, "Alfa", now - timedelta(minutes=40), "Seguimiento", 32, {"titulo": "Seguimiento"})
        with open(cls.base + ".srt", "w", encoding="utf-8") as f:
            f.write(srt_text([(10.0, 15.0, "Primera frase de Ana."), (20.0, 25.0, "Respuesta de Luis."),
                              (1850.0, 1855.0, "Esto ya es del segundo bloque.")]))
        with open(cls.base + ".hablantes.json", "w", encoding="utf-8") as f:
            json.dump({"version": 1, "creado": "2026-11-16T10:30:00", "tramos": [
                {"inicio": 9.5, "fin": 15.5, "h": "h1"}, {"inicio": 19.5, "fin": 25.5, "h": "h2"}],
                "hablantes": {"h1": {"segundos": 6.0}, "h2": {"segundos": 6.0}}, "parcial": {"hasta": 1800},
                "rejilla": 1800}, f)

    def test_las_frases_identificadas_llevan_quien_habla(self):
        pg = self.pagina()
        rel = os.path.relpath(self.base + ".wav", self.srv.root).replace(os.sep, "/")
        pg.goto(f"{self.srv.base}/?t={self.srv.token}#/a/{quote(rel)}", wait_until="networkidle")
        pg.wait_for_selector("#aTx :text('Esto ya es del segundo bloque')", timeout=15000)
        pg.wait_for_selector("#aVoz .vz-st", timeout=10000)
        self.assertIn("hasta el min 30 (el resto, al terminar)", pg.inner_text("#aVoz .vz-st"))
        pg.wait_for_selector("#aTx .spk", timeout=10000)
        chips = pg.locator("#aTx .spk").all_inner_texts()
        self.assertEqual(chips, ["Persona 1", "Persona 2"])
        # la frase de después de lo identificado, sin nadie: va en su párrafo, sin etiqueta
        ultimo = pg.locator("#aTx p").last.inner_text()
        self.assertIn("segundo bloque", ultimo)
        self.assertNotIn("Persona", ultimo)
        self.captura(pg, "quien_habla_parcial")
        self.sin_errores()


if __name__ == "__main__":
    unittest.main()
