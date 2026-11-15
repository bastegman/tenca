# -*- coding: utf-8 -*-
"""«Mis pendientes» (review del 14/11/2026, punto 28): las tareas sin hacer de todas las reuniones, en Hoy.

- Funciones puras de pendientes.py: leer las tareas de un resumen (formatos raros incluidos), quién es el responsable,
  el primer minuto, si es tuya, la clave normalizada, marcar y conservar lo hecho al regenerar.
- El servidor: GET /api/pendientes (todas las reuniones, sin la que se graba ni los resúmenes «solo en línea» de
  OneDrive) y POST /api/pendientes/hecha (la línea del .resumen.md, también si se ha movido o el audio ya no está).
- Regenerar un resumen (Jobs._run_summary) no pierde lo hecho, y un clic durante la regeneración tampoco (el candado).
- La vista del resumen (notas.js, sumHtml) y la tarjeta de Hoy (hoy.js), con node.
La prueba en un navegador de verdad está en test_ui_pendientes."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from support import REPO, TempTree, bare_jobs, server  # noqa: F401  (support prepara sys.path)
import pendientes  # noqa: E402
import plan  # noqa: E402
import trabajos  # noqa: E402
from test_enganches import ServidorDePrueba  # noqa: E402
from test_frontend import js_defs, read  # noqa: E402
from test_hoy import JS_STUBS  # noqa: E402

STATIC = os.path.join(REPO, "_app", "static")

RESUMEN = """---
modelo: prueba
generado: 2026-11-10 11:00
---

## Resumen
De qué trató.

## Próximos pasos
- [ ] Ana: enviar el presupuesto — viernes [12:34]
- [x] Luis: reservar la sala [03:10]
* [ ] Yo: preparar la demo [1:02:10]
1. [ ] Revisar el contrato
2) [X] **Carmen**: cerrar el acta
  - [ ] Tú: llamar al proveedor [105:20]
- [ ]
- [] mal escrita
Texto - [ ] en medio
+ [ ] Equipo: plan de pruebas

## Participantes
- Ana (dirección)
"""


# ----------------------------------------------------------------------------- 1. funciones puras

class ExtraerTests(unittest.TestCase):
    def test_formatos(self):
        ts = pendientes.extraer(RESUMEN)
        lineas = RESUMEN.split("\n")
        self.assertEqual([(t["i"], t["hecha"]) for t in ts],
                         [(9, False), (10, True), (11, False), (12, False), (13, True), (14, False), (18, False)])
        for t in ts:
            self.assertIn(t["texto"], lineas[t["i"]])
            self.assertIn(lineas[t["i"]][t["pos"]], " xX")
        por_i = {t["i"]: t for t in ts}
        self.assertEqual((por_i[9]["responsable"], por_i[9]["tarea"], por_i[9]["segundo"]),
                         ("Ana", "enviar el presupuesto — viernes", 754))
        self.assertEqual((por_i[11]["responsable"], por_i[11]["segundo"]), ("Yo", 3730))
        self.assertEqual((por_i[12]["responsable"], por_i[12]["tarea"], por_i[12]["segundo"]),
                         ("", "Revisar el contrato", None))
        self.assertEqual((por_i[13]["responsable"], por_i[13]["tarea"]), ("Carmen", "cerrar el acta"))  # [X]
        self.assertEqual((por_i[14]["responsable"], por_i[14]["segundo"]), ("Tú", 6320))  # [105:20]
        self.assertEqual(por_i[18]["responsable"], "Equipo")

    def test_cuenta_lo_mismo_que_la_ultima_vez(self):
        """«La última vez» (plan.tareas_abiertas) y «Mis pendientes» cuentan las mismas tareas abiertas."""
        otro = ("## Próximos pasos\n- [ ] Ana: presupuesto [12:00]\n- [x] Luis: hecho\n* [ ] Yo: acta\n1. [ ] Otra\n"
                "  - [ ] sangrada\n- [ ]\n- [] mal escrita\nTexto - [ ] en medio\n- [ ] con\r\n")
        for t in (RESUMEN, otro, "", "## Próximos pasos\nNinguno\n"):
            self.assertEqual(pendientes.abiertas(t), plan.tareas_abiertas(t), t)

    def test_responsable(self):
        p = pendientes.partir
        self.assertEqual(p("**Ana**: enviar — viernes [12:34]"), ("Ana", "enviar — viernes"))
        self.assertEqual(p("Ana y Luis: revisar [1:00–2:00]"), ("Ana y Luis", "revisar"))
        self.assertEqual(p("Revisar esto: con calma"), ("Revisar esto", "con calma"))
        self.assertEqual(p("Una frase larga de verdad que no es nadie: y sigue"),
                         ("", "Una frase larga de verdad que no es nadie: y sigue"))
        self.assertEqual(p("Ver https: algo"), ("", "Ver https: algo"))
        self.assertEqual(p("sin responsable [00:10]"), ("", "sin responsable"))

    def test_normalizar_y_minutos(self):
        n = pendientes.normalizar
        self.assertEqual(n("Ana: enviar el **acta** [12:34]."), n("ana  enviar el ACTA"))
        self.assertEqual(n("Revisión [1:12, 3:40] del índice"), "revision del indice")
        self.assertNotEqual(n("Ana: enviar el presupuesto"), n("Ana: enviar el presupuesto revisado"))
        self.assertEqual(n("[12:34]"), "")
        s = pendientes.primer_segundo
        self.assertEqual((s("[0:05]"), s("x [12:34–13:10]"), s("[1:02:10]"), s("[105:20]"), s("10:30"), s(None)),
                         (5, 754, 3730, 6320, None, None))

    def test_es_tuya(self):
        t = pendientes.es_tuya
        for r in ("Yo", "yo", "Tú", "Tu", "Yo y Luis", "Luis, Yo", "¿Yo?"):
            self.assertTrue(t(r), r)
        # ni «Me» ni «I»: «I+D» o «Dirección e I+D» no son tuyas (revisión 1)
        for r in ("Luis", "Yolanda", "Todos", "", None, "Ana García", "I+D", "Dirección e I+D", "Me", "I"):
            self.assertFalse(t(r), r)
        self.assertTrue(t("Ana García", "ana  garcía"))  # la persona «Soy yo» de las voces
        self.assertTrue(t("¿Ana García?", "Ana García"))  # la provisional
        self.assertTrue(t("Luis y Ana García", "Ana García"))
        self.assertFalse(t("Ana", "Ana García"))  # exacto: otra Ana no es tuya
        self.assertFalse(t("Ana García", None))


class MarcarTests(unittest.TestCase):
    T = "## Próximos pasos\n- [ ] Ana: enviar [00:10]\n- [X] Luis: sala\n- [ ] Ana: enviar [00:10]\n"

    def test_marcar_y_desmarcar(self):
        nuevo, i, cambiado = pendientes.marcar(self.T, 1, "Ana: enviar [00:10]", True)
        self.assertEqual((i, cambiado), (1, True))
        self.assertEqual(nuevo.split("\n")[1], "- [x] Ana: enviar [00:10]")
        self.assertEqual(nuevo.split("\n")[3], "- [ ] Ana: enviar [00:10]")  # la otra igual, en su línea, no
        nuevo, i, cambiado = pendientes.marcar(self.T, 2, "Luis: sala", False)
        self.assertEqual((nuevo.split("\n")[2], i, cambiado), ("- [ ] Luis: sala", 2, True))
        self.assertEqual(pendientes.marcar(self.T, 2, "luis sala", True), (self.T, 2, False))  # ya estaba

    def test_la_linea_se_ha_movido(self):
        movido = "Una línea nueva arriba\n" + self.T
        nuevo, i, cambiado = pendientes.marcar(movido, 2, "Luis: sala", False)  # estaba en la 2, ahora en la 3
        self.assertEqual((i, cambiado, nuevo.split("\n")[3]), (3, True, "- [ ] Luis: sala"))
        # dos iguales: la más cercana a la que se pidió que aún no esté así
        nuevo, i, _ = pendientes.marcar(movido, 4, "Ana: enviar", True)
        self.assertEqual(i, 4)
        nuevo, i, _ = pendientes.marcar(nuevo, 2, "Ana: enviar", True)
        self.assertEqual(i, 2)

    def test_una_tarea_sin_letras_tambien_se_marca(self):
        """«- [ ] ✅» o una tarea que es solo un minuto: su clave es el texto tal cual (revisión 1: daba siempre 409)."""
        t = "- [ ] ✅\n- [ ] [12:34]\n- [ ] 🎯\n"
        nuevo, i, cambiado = pendientes.marcar(t, 0, "✅", True)
        self.assertEqual((nuevo.split("\n")[0], i, cambiado), ("- [x] ✅", 0, True))
        nuevo, i, _ = pendientes.marcar(nuevo, 1, "[12:34]", True)
        self.assertEqual(nuevo.split("\n")[1:3], ["- [x] [12:34]", "- [ ] 🎯"])
        self.assertEqual(pendientes.marcar(t, 2, "✅", True)[1], 0)  # la de la línea 0, no la 🎯 de la 2
        with self.assertRaises(pendientes.NoEsta):
            pendientes.marcar(t, 2, "⭐", True)

    def test_el_texto_ha_cambiado(self):
        with self.assertRaises(pendientes.NoEsta):
            pendientes.marcar(self.T, 1, "Ana: enviar otra cosa", True)
        with self.assertRaises(pendientes.NoEsta):
            pendientes.marcar(self.T, 1, "[00:10]", True)  # sin texto, nada
        with self.assertRaises(pendientes.NoEsta):
            pendientes.marcar("Ana: enviar\n- Ana: enviar\n", 0, "Ana: enviar", True)  # no son tareas


class ConservarHechasTests(unittest.TestCase):
    VIEJO = ("---\nmodelo: x\n---\n\n## Próximos pasos\n- [x] Ana: enviar el presupuesto [12:34]\n"
             "- [ ] Luis: reservar la sala\n- [x] Carmen: cerrar el acta\n- [x] Carmen: cerrar el acta\n")

    def test_la_misma_sale_hecha_y_la_que_ya_no_esta_se_anade(self):
        nuevo = ("## Resumen\nTexto.\n\n## Próximos pasos\n- [ ] ana: Enviar el presupuesto. [14:02]\n"
                 "- [ ] Luis: reservar la sala\n- [ ] Yo: preparar la demo\n\n## Participantes\n- Ana\n")
        out = pendientes.conservar_hechas(self.VIEJO, nuevo).split("\n")
        self.assertEqual(out[4], "- [x] ana: Enviar el presupuesto. [14:02]")  # la misma, con otro minuto
        self.assertEqual(out[5], "- [ ] Luis: reservar la sala")  # abierta en el viejo: abierta
        self.assertEqual(out[6], "- [ ] Yo: preparar la demo")
        self.assertEqual(out[7], "- [x] Carmen: cerrar el acta")  # ya no está en el nuevo: hecha, tras la última
        self.assertEqual(out.count("- [x] Carmen: cerrar el acta"), 1)  # repetida en el viejo: una vez
        self.assertEqual(out[8:], ["", "## Participantes", "- Ana", ""])

    def test_nunca_por_parecido(self):
        nuevo = "## Próximos pasos\n- [ ] Ana: enviar el presupuesto revisado [12:34]\n"
        out = pendientes.conservar_hechas(self.VIEJO, nuevo).split("\n")
        self.assertEqual(out[1], "- [ ] Ana: enviar el presupuesto revisado [12:34]")
        self.assertEqual(out[2:4], ["- [x] Ana: enviar el presupuesto [12:34]", "- [x] Carmen: cerrar el acta"])

    def test_sin_tareas_en_el_nuevo(self):
        for cab, ninguno in (("## Próximos pasos", "Ninguno"), ("## Next steps", "- None"),
                             ("### PRÓXIMOS PASOS", "- Ninguno.")):
            nuevo = f"## Resumen\nOk.\n\n{cab}\n{ninguno}\n\n## Participantes\n- Ana\n"
            out = pendientes.conservar_hechas(self.VIEJO, nuevo).split("\n")
            self.assertEqual(out[3:8], [cab, "- [x] Ana: enviar el presupuesto [12:34]",
                                        "- [x] Carmen: cerrar el acta", "", "## Participantes"], cab)
            self.assertNotIn(ninguno, out)
        out = pendientes.conservar_hechas(self.VIEJO, "## Resumen\nOk.")
        self.assertEqual(out, "## Resumen\nOk.\n\n## Próximos pasos\n- [x] Ana: enviar el presupuesto [12:34]\n"
                              "- [x] Carmen: cerrar el acta")

    def test_lo_sangrado_de_la_ultima_es_suyo(self):
        nuevo = "## Próximos pasos\n- [ ] Yo: demo\n  con el equipo de pruebas\n\nFin\n"
        out = pendientes.conservar_hechas(self.VIEJO, nuevo).split("\n")
        self.assertEqual(out[1:5], ["- [ ] Yo: demo", "  con el equipo de pruebas",
                                    "- [x] Ana: enviar el presupuesto [12:34]", "- [x] Carmen: cerrar el acta"])

    def test_repetidas_tantas_como_habia_hechas(self):
        """Dos tareas iguales salvo el minuto, una hecha y otra no: siguen una y una (revisión 1: salían las dos
        hechas y la abierta desaparecía de «Mis pendientes»)."""
        viejo = "## Próximos pasos\n- [x] Yo: llamar a Ana [10:00]\n- [ ] Yo: llamar a Ana [50:00]\n"
        nuevo = "## Próximos pasos\n- [ ] Yo: llamar a Ana [10:05]\n- [ ] Yo: llamar a Ana [50:10]\n"
        out = pendientes.conservar_hechas(viejo, nuevo)
        self.assertEqual(out.split("\n")[1:3], ["- [x] Yo: llamar a Ana [10:05]", "- [ ] Yo: llamar a Ana [50:10]"])
        # si el nuevo ya la trae hecha, cuenta: no se marca otra
        nuevo = "## Próximos pasos\n- [ ] Yo: llamar a Ana [10:05]\n- [x] Yo: llamar a Ana [50:10]\n"
        self.assertEqual(pendientes.abiertas(pendientes.conservar_hechas(viejo, nuevo)), 1)

    def test_en_su_seccion_aunque_haya_tareas_en_otra(self):
        """Las hechas que se arrastran van a los próximos pasos, no tras una tarea de «Riesgos» (revisión 1)."""
        nuevo = ("## Próximos pasos\n- [ ] Yo: demo\n\n## Riesgos\n- [ ] Vigilar el plazo\n\n## Participantes\n- Ana\n")
        out = pendientes.conservar_hechas(self.VIEJO, nuevo).split("\n")
        self.assertEqual(out[:5], ["## Próximos pasos", "- [ ] Yo: demo", "- [x] Ana: enviar el presupuesto [12:34]",
                                   "- [x] Carmen: cerrar el acta", ""])
        self.assertEqual(out[5:7], ["## Riesgos", "- [ ] Vigilar el plazo"])
        # sin cabecera de próximos pasos y en inglés: «## Next steps»
        out = pendientes.conservar_hechas(self.VIEJO, "## Summary\nOk.\n\n## Participants\n- Ana")
        self.assertIn("\n\n## Next steps\n- [x] Ana: enviar el presupuesto [12:34]", out)

    def test_sin_nada_hecho_no_cambia(self):
        nuevo = "## Próximos pasos\n- [ ] Yo: demo\n"
        self.assertIs(pendientes.conservar_hechas("- [ ] Yo: demo\n", nuevo), nuevo)
        self.assertIs(pendientes.conservar_hechas("", nuevo), nuevo)


class CabeceraTests(unittest.TestCase):
    def test_desde_es_la_linea_del_fichero(self):
        for texto in (RESUMEN, "## Sin cabecera\n- [ ] Yo: x\n", "---\nmodelo: x\n---\n\n\n  - [ ] Yo: x\n",
                      "---\nmodelo: x\n- [ ] Yo: x\n"):
            meta, cuerpo, desde = pendientes.separar_cabecera(texto)
            lineas = texto.split("\n")
            for t in pendientes.extraer(cuerpo):
                self.assertEqual(pendientes.extraer(lineas[t["i"] + desde])[0]["clave"], t["clave"], texto)
        self.assertEqual(pendientes.separar_cabecera(RESUMEN)[0], {"modelo": "prueba", "generado": "2026-11-10 11:00"})


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="tenca-pend-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.p = os.path.join(self.d, "r.resumen.md")
        with open(self.p, "w", encoding="utf-8") as f:
            f.write("- [ ] Yo: x\n")

    def test_relee_solo_si_cambia(self):
        c = pendientes.Cache()
        with mock.patch.object(pendientes, "read_text", wraps=pendientes.read_text) as rt:
            self.assertEqual(len(c.tareas(self.p)), 1)
            self.assertEqual(len(c.tareas(self.p)), 1)
            self.assertEqual(rt.call_count, 1)
            with open(self.p, "a", encoding="utf-8") as f:
                f.write("- [ ] Yo: y\n")
            self.assertEqual(len(c.tareas(self.p)), 2)
            self.assertEqual(rt.call_count, 2)
            c.olvidar(self.p)
            c.tareas(self.p)
            self.assertEqual(rt.call_count, 3)
        c.podar(set())
        self.assertEqual(c.datos, {})
        self.assertIsNone(c.tareas(os.path.join(self.d, "no-esta.resumen.md")))

    def test_solo_en_linea_no_se_abre(self):
        real = os.stat
        c = pendientes.Cache(stat=lambda p: types.SimpleNamespace(st_file_attributes=0x400000, st_mtime_ns=1,
                                                                  st_size=real(p).st_size))
        with mock.patch.object(pendientes, "read_text") as rt:
            self.assertIsNone(c.tareas(self.p))
        rt.assert_not_called()


# ----------------------------------------------------------------------------- 2. el servidor

class ServidorTests(TempTree):
    def setUp(self):
        super().setUp()
        self.salidas = []

    def reunion(self, rel, resumen, date="2026-11-10T10:00:00", **kw):
        if resumen is not None:
            self.write(os.path.splitext(rel)[0] + ".resumen.md", resumen)
        p = os.path.join(self.root, os.path.splitext(rel)[0] + ".resumen.md")
        return dict({"path": rel, "date": date, "label": "", "props": {}, "_sum": p if resumen is not None else None},
                    **kw)

    def app(self, flat, yo=None, voces_on=None):
        a = server.App.__new__(server.App)
        a.rec = None
        a.library = types.SimpleNamespace(at=1, flat=flat)
        on = (lambda: yo is not None) if voces_on is None else voces_on
        a.jobs = types.SimpleNamespace(voices_on=on, voices=types.SimpleNamespace(user=lambda: {"nombre": yo}),
                                       schedule_outputs=self.salidas.append)
        return a

    def flat(self):
        return [
            self.reunion("Alfa/vieja.wav", "## Próximos pasos\n- [ ] Luis: informe\n", date="2026-11-01T10:00:00",
                         label="Seguimiento"),
            self.reunion("Alfa/nueva.wav", "## Próximos pasos\n- [ ] Ana: presupuesto [12:34]\n- [x] Luis: sala\n"
                                           "- [ ] Yo: acta\n- [ ] Ana García: llamar\n",
                         date="2026-11-10T10:00:00", props={"titulo": "Comité"}),
            self.reunion("Alfa/sin-resumen.wav", None),
            self.reunion("Alfa/grabando.wav", "- [ ] Yo: no sale\n", date="2026-11-11T10:00:00", recording=True),
            self.reunion("Alfa/sin-fecha.wav", "- [ ] Tú: la de fecha por mtime\n", date=None, mtime=0),
        ]

    def test_get_todas_de_la_mas_reciente_a_la_mas_antigua(self):
        a = self.app(self.flat(), yo="Ana García")
        r = a.pendientes_lista({})
        self.assertEqual([(t["path"], t["i"], t["tuya"]) for t in r["tareas"]],
                         [("Alfa/nueva.wav", 1, False), ("Alfa/nueva.wav", 3, True), ("Alfa/nueva.wav", 4, True),
                          ("Alfa/vieja.wav", 1, False), ("Alfa/sin-fecha.wav", 0, True)])
        self.assertEqual((r["total"], r["tuyas"]), (5, 3))
        t = r["tareas"][0]
        self.assertEqual({k: t[k] for k in ("titulo", "fecha", "fecha_txt", "texto", "responsable", "tarea",
                                            "segundo", "sin_audio")},
                         {"titulo": "Comité", "fecha": "2026-11-10T10:00:00", "fecha_txt": "mar 10 nov",
                          "texto": "Ana: presupuesto [12:34]", "responsable": "Ana", "tarea": "presupuesto",
                          "segundo": 754, "sin_audio": False})
        self.assertEqual(r["tareas"][3]["titulo"], "Seguimiento")  # sin título, el del archivo
        self.assertNotIn("pos", t)
        self.assertNotIn("clave", t)

    def test_sin_la_que_se_graba_con_la_grabadora_de_la_app(self):
        flat = self.flat()
        a = self.app(flat)
        a.rec = types.SimpleNamespace(alive=lambda: True, path=os.path.join(self.root, "Alfa", "nueva.wav"))
        self.assertNotIn("Alfa/nueva.wav", {t["path"] for t in a.pendientes_lista({})["tareas"]})

    def test_quien_eres_sin_voces_o_si_fallan(self):
        a = self.app(self.flat(), yo="Ana García", voces_on=lambda: False)
        self.assertEqual(a.pendientes_lista({})["tuyas"], 2)  # «Yo» y «Tú»; Ana García no: las voces están apagadas

        def falla():
            raise RuntimeError("voces.json roto")
        a = self.app(self.flat(), yo="Ana García", voces_on=falla)
        with mock.patch.object(server, "log_exc"):
            self.assertEqual(a.pendientes_lista({})["tuyas"], 2)

    def test_onedrive_solo_en_linea_no_se_lee(self):
        flat = self.flat()
        nube = flat[1]["_sum"]
        real = os.stat

        def stat(p):
            st = real(p)
            return types.SimpleNamespace(st_file_attributes=0x400000 if p == nube else 0x20, st_mtime_ns=st.st_mtime_ns,
                                         st_size=st.st_size)
        leidos = []
        with mock.patch.object(pendientes, "CACHE", pendientes.Cache(stat=stat)), \
                mock.patch.object(pendientes, "read_text", side_effect=lambda p: leidos.append(p) or open(
                    p, encoding="utf-8").read()):
            r = self.app(flat).pendientes_lista({})
        self.assertNotIn("Alfa/nueva.wav", {t["path"] for t in r["tareas"]})
        self.assertNotIn(nube, leidos)
        self.assertIn(flat[0]["_sum"], leidos)

    def test_post_marca_la_linea_y_la_ultima_vez_lo_cuenta(self):
        flat = self.flat()
        a = self.app(flat)
        p = flat[1]["_sum"]
        antes = a._ultima_vez("Comité")["tareas"]
        r = a.pendiente_hecha({"path": "Alfa/nueva.wav", "i": 3, "texto": "Yo: acta", "hecha": True})
        self.assertEqual(r, {"ok": True, "path": "Alfa/nueva.wav", "i": 3, "hecha": True, "cambiado": True})
        self.assertEqual(server.read_text(p).split("\n")[3], "- [x] Yo: acta")
        self.assertEqual((a.library.at, self.salidas), (0, ["Alfa/nueva.wav"]))
        self.assertEqual(a._ultima_vez("Comité")["tareas"], antes - 1)
        self.assertNotIn(("Alfa/nueva.wav", 3), {(t["path"], t["i"]) for t in a.pendientes_lista({})["tareas"]})
        # deshacer
        a.pendiente_hecha({"path": "Alfa/nueva.wav", "i": 3, "texto": "Yo: acta", "hecha": False})
        self.assertEqual(server.read_text(p).split("\n")[3], "- [ ] Yo: acta")
        self.assertIn(("Alfa/nueva.wav", 3), {(t["path"], t["i"]) for t in a.pendientes_lista({})["tareas"]})
        # otra vez lo mismo: nada que escribir
        self.salidas.clear()
        r = a.pendiente_hecha({"path": "Alfa/nueva.wav", "i": 3, "texto": "Yo: acta", "hecha": False})
        self.assertEqual((r["cambiado"], self.salidas), (False, []))

    def test_post_con_la_linea_movida_o_ya_sin_esa_tarea(self):
        flat = self.flat()
        a = self.app(flat)
        p = flat[1]["_sum"]
        with open(p, "w", encoding="utf-8") as f:  # editado fuera: una línea más arriba, y otra tarea reescrita
            f.write("Arriba\n## Próximos pasos\n- [ ] Ana: presupuesto [12:34]\n- [x] Luis: sala\n- [ ] Yo: acta\n"
                    "- [ ] Ana García: llamar a otra persona\n")
        r = a.pendiente_hecha({"path": "Alfa/nueva.wav", "i": 3, "texto": "Yo: acta", "hecha": True})
        self.assertEqual(r["i"], 4)
        self.assertEqual(server.read_text(p).split("\n")[4], "- [x] Yo: acta")
        with self.assertRaises(server.ApiError) as e:
            a.pendiente_hecha({"path": "Alfa/nueva.wav", "i": 4, "texto": "Ana García: llamar", "hecha": True})
        self.assertEqual(e.exception.code, 409)
        self.assertIn("ya no está en el resumen", e.exception.msg)
        self.assertEqual(server.read_text(p).split("\n")[5], "- [ ] Ana García: llamar a otra persona")

    def test_post_peticiones_no_validas(self):
        a = self.app(self.flat())
        for b in ({}, {"path": "Alfa/nueva.wav", "i": "3", "texto": "Yo: acta", "hecha": True},
                  {"path": "Alfa/nueva.wav", "i": 3, "texto": "", "hecha": True},
                  {"path": "Alfa/nueva.wav", "i": 3, "texto": "Yo: acta", "hecha": "sí"},
                  {"path": "Alfa/nueva.wav", "i": True, "texto": "Yo: acta", "hecha": True}):
            with self.assertRaises(server.ApiError, msg=b):
                a.pendiente_hecha(b)
        with self.assertRaises(server.ApiError) as e:
            a.pendiente_hecha({"path": "Alfa/sin-resumen.wav", "i": 0, "texto": "x", "hecha": True})
        self.assertEqual(e.exception.code, 404)
        with self.assertRaises(server.ApiError) as e:
            a.pendiente_hecha({"path": "../fuera.wav", "i": 0, "texto": "x", "hecha": True})
        self.assertEqual(e.exception.code, 403)

    def test_post_sin_audio_y_movida_en_esta_sesion(self):
        """El resumen manda aunque el audio ya no esté (se borró al terminar, punto 17); y una reunión movida en esta
        sesión se busca en su ruta nueva."""
        flat = self.flat()
        a = self.app(flat)
        self.assertFalse(os.path.exists(os.path.join(self.root, "Alfa", "nueva.wav")))  # (no hay audio en la prueba)
        a.pendiente_hecha({"path": "Alfa/nueva.wav", "i": 1, "texto": "Ana: presupuesto [12:34]", "hecha": True})
        self.assertEqual(server.read_text(flat[1]["_sum"]).split("\n")[1], "- [x] Ana: presupuesto [12:34]")
        os.makedirs(os.path.join(self.root, "Beta"))
        os.rename(flat[0]["_sum"], os.path.join(self.root, "Beta", "vieja.resumen.md"))
        with server.ALIASES_LOCK:
            server.ALIASES["Alfa/vieja.wav"] = "Beta/vieja.wav"
        r = a.pendiente_hecha({"path": "Alfa/vieja.wav", "i": 1, "texto": "Luis: informe", "hecha": True})
        self.assertEqual(r["path"], "Beta/vieja.wav")
        self.assertIn("- [x] Luis: informe", server.read_text(os.path.join(self.root, "Beta", "vieja.resumen.md")))
        self.assertFalse(os.path.exists(flat[0]["_sum"]))  # nada escrito en la ruta vieja
        self.assertEqual(self.salidas[-1], "Beta/vieja.wav")

    def test_la_vista_del_resumen_marca_por_su_linea(self):
        """Lo que hace notas.js: la línea del cuerpo que da /api/summary + desde es la del fichero."""
        flat = [self.reunion("r.wav", RESUMEN)]
        self.wav("r.wav")
        a = self.app(flat)
        a.jobs.summary_ready = lambda: True
        s = a.summary_get({"path": "r.wav"})
        k = s["text"].split("\n").index("* [ ] Yo: preparar la demo [1:02:10]")
        r = a.pendiente_hecha({"path": "r.wav", "i": k + s["desde"], "texto": "Yo: preparar la demo [1:02:10]",
                               "hecha": True})
        self.assertEqual(r["i"], k + s["desde"])
        self.assertIn("* [x] Yo: preparar la demo [1:02:10]", a.summary_get({"path": "r.wav"})["text"])

    def bytes_de(self, p):
        with open(p, "rb") as f:
            return f.read()

    def test_post_conserva_bom_y_saltos_y_no_toca_otra_codificacion(self):
        """Marcar cambia solo el carácter de la casilla: el BOM y los \\r\\n se quedan; un resumen guardado a mano en
        otra codificación no se reescribe (revisión 1, P1: todas sus tildes acababan en U+FFFD)."""
        flat = self.flat()
        a = self.app(flat)
        p = flat[1]["_sum"]
        crlf = "## Próximos pasos\r\n- [ ] Ana: presupuesto [12:34]\r\n- [ ] Yo: acta — revisión\r\n".encode("utf-8")
        with open(p, "wb") as f:
            f.write(b"\xef\xbb\xbf" + crlf)
        r = a.pendiente_hecha({"path": "Alfa/nueva.wav", "i": 2, "texto": "Yo: acta — revisión", "hecha": True})
        self.assertTrue(r["cambiado"])
        self.assertEqual(self.bytes_de(p), b"\xef\xbb\xbf" + crlf.replace(b"- [ ] Yo", b"- [x] Yo"))
        ansi = "## Próximos pasos\r\n- [ ] José: revisión del año\r\n- [ ] Yo: acta\r\n".encode("cp1252")
        with open(p, "wb") as f:
            f.write(ansi)
        with self.assertRaises(server.ApiError) as e:
            a.pendiente_hecha({"path": "Alfa/nueva.wav", "i": 2, "texto": "Yo: acta", "hecha": True})
        self.assertEqual(e.exception.code, 409)
        self.assertIn("UTF-8", e.exception.msg)
        self.assertEqual(self.bytes_de(p), ansi)

    def test_post_solo_en_el_resumen_de_una_reunion(self):
        """«.», una carpeta o la raíz de una vinculada no son una reunión: antes «.» marcaba <padre>/Tenca.resumen.md,
        fuera de la carpeta de la app (revisión 1)."""
        fuera = os.path.join(self.tmp, os.path.basename(self.root) + ".resumen.md")
        with open(fuera, "w", encoding="utf-8") as f:
            f.write("- [ ] Yo: fuera\n")
        self.write("Alfa.resumen.md", "- [ ] Yo: carpeta\n")
        a = self.app(self.flat())
        for path, texto in ((".", "Yo: fuera"), ("Alfa", "Yo: carpeta"), ("Alfa/..", "Yo: fuera"),
                            ("Alfa/nueva.md", "Yo: acta")):
            with self.assertRaises(server.ApiError, msg=path):
                a.pendiente_hecha({"path": path, "i": 0, "texto": texto, "hecha": True})
        self.assertEqual(server.read_text(fuera), "- [ ] Yo: fuera\n")
        self.assertEqual(server.read_text(os.path.join(self.root, "Alfa.resumen.md")), "- [ ] Yo: carpeta\n")

    def test_post_con_el_resumen_que_da_la_biblioteca(self):
        """Una reunión de la biblioteca cuyo resumen no va junto a su ruta (la que ya no tiene audio, punto 17, si su
        ruta fuera la del propio resumen): el de la biblioteca (revisión 1: daba 404)."""
        p = self.write("Alfa/solo-texto.resumen.md", "- [ ] Yo: acta\n")
        a = self.app([{"path": "Alfa/solo-texto.resumen.md", "date": "2026-11-10T10:00:00", "props": {}, "_sum": p,
                       "sin_audio": True}])
        self.assertEqual(a.pendientes_lista({})["tareas"][0]["sin_audio"], True)
        r = a.pendiente_hecha({"path": "Alfa/solo-texto.resumen.md", "i": 0, "texto": "Yo: acta", "hecha": True})
        self.assertEqual((r["path"], r["cambiado"]), ("Alfa/solo-texto.resumen.md", True))
        self.assertEqual(server.read_text(p), "- [x] Yo: acta\n")

    def test_un_clic_que_espera_no_tiene_cogido_el_candado_de_mover(self):
        """Mientras se escribe un resumen regenerado (pendientes.LOCK cogido), el clic espera ese candado sin coger
        antes el de mover: mover, borrar o el directo no se frenan (revisión 1)."""
        flat = self.flat()
        a = self.app(flat)
        res = {}
        hilo = threading.Thread(target=lambda: res.update(r=a.pendiente_hecha(
            {"path": "Alfa/nueva.wav", "i": 3, "texto": "Yo: acta", "hecha": True})))
        libre = {}
        with pendientes.LOCK:
            hilo.start()
            time.sleep(0.3)

            def probar():
                lock = a._move_lock()
                libre["ok"] = lock.acquire(blocking=False)
                if libre["ok"]:
                    lock.release()
            otro = threading.Thread(target=probar)
            otro.start()
            otro.join(5)
        hilo.join(10)
        self.assertTrue(libre["ok"])
        self.assertTrue(res["r"]["cambiado"])

    def test_por_http(self):
        flat = self.flat()
        s = ServidorDePrueba(self.app(flat))
        self.addCleanup(s.cerrar)
        code, r = s.pedir("/api/pendientes")
        self.assertEqual((code, r["total"]), (200, 5))
        code, r = s.pedir("/api/pendientes/hecha", {"path": "Alfa/vieja.wav", "i": 1, "texto": "Luis: informe",
                                                    "hecha": True})
        self.assertEqual((code, r["cambiado"]), (200, True))
        code, r = s.pedir("/api/pendientes/hecha", {"path": "Alfa/vieja.wav", "i": 1, "texto": "Otra cosa",
                                                    "hecha": True})
        self.assertEqual(code, 409)
        self.assertEqual(s.pedir("/api/pendientes")[1]["total"], 4)


# ----------------------------------------------------------------------------- 3. regenerar el resumen

class RegenerarTests(TempTree):
    VIEJO = ("---\nmodelo: viejo\ngenerado: 2026-11-10 11:00\n---\n\n## Próximos pasos\n"
             "- [x] Ana: enviar el acta [01:00]\n- [ ] Yo: preparar la demo\n- [x] Luis: reservar la sala\n")
    NUEVO = ("## Resumen\nOtra versión.\n\n## Próximos pasos\n- [ ] Ana: enviar el acta [02:10]\n"
             "- [ ] Yo: preparar la demo\n\n## Participantes\n- Ana\n")

    def jobs(self):
        j = bare_jobs([])
        j.settings = server.Store(os.path.join(self.data_dir, "settings.json"), server.DEFAULT_SETTINGS)
        j.settings.update(lambda d: d.update(procesado="api"))
        j.library = types.SimpleNamespace(at=1, find=lambda rel: {"duration": 60, "date": "2026-11-15T10:00:00"})
        j.summary_missing = lambda: None
        j._with_speakers = lambda abs_path, segs, meta: segs
        j.write_outputs = lambda rel: None
        j._summarizer = lambda st: types.SimpleNamespace(summarize=lambda *a: (self.NUEVO, "modelo-nuevo"))
        return j

    def preparar(self):
        self.wav("r.wav")
        self.write("r.srt", "1\n00:00:00,000 --> 00:00:02,000\nhola\n")
        return self.write("r.resumen.md", self.VIEJO)

    def resumir(self, jobs):
        job = server.Job("r.wav", "r.wav", "resumen", None)
        with mock.patch.object(server, "log"):
            jobs._run_summary(job)
        self.assertEqual(job.status, "done", job.error)

    def test_no_se_pierde_lo_hecho(self):
        p = self.preparar()
        self.resumir(self.jobs())
        texto = server.read_text(p)
        self.assertIn("modelo: modelo-nuevo", texto)
        self.assertIn("- [x] Ana: enviar el acta [02:10]", texto)  # la misma: hecha
        self.assertIn("- [ ] Yo: preparar la demo", texto)
        self.assertIn("- [ ] Yo: preparar la demo\n- [x] Luis: reservar la sala\n\n## Participantes", texto)
        self.assertEqual(plan.tareas_abiertas(texto), 1)

    def test_sin_resumen_anterior_tal_cual(self):
        self.wav("r.wav")
        self.write("r.srt", "1\n00:00:00,000 --> 00:00:02,000\nhola\n")
        self.resumir(self.jobs())
        self.assertTrue(server.read_text(os.path.join(self.root, "r.resumen.md")).endswith(self.NUEVO))

    def test_un_fallo_al_conservar_no_tumba_el_resumen(self):
        p = self.preparar()
        with mock.patch.object(pendientes, "conservar_hechas", side_effect=ValueError("raro")), \
                mock.patch.object(trabajos, "log_exc"):
            self.resumir(self.jobs())
        self.assertTrue(server.read_text(p).endswith(self.NUEVO))

    def test_un_clic_durante_la_regeneracion_no_se_pierde(self):
        """Entre leer el resumen anterior y escribir el nuevo, alguien marca «Yo: preparar la demo» en Hoy: con el
        candado, el clic espera y se aplica al nuevo; sin él, el nuevo lo pisaba."""
        p = self.preparar()
        jobs = self.jobs()
        app = server.App.__new__(server.App)
        app.library, app.jobs = types.SimpleNamespace(at=1), types.SimpleNamespace(schedule_outputs=lambda rel: None)
        res, original = {}, jobs._con_lo_hecho

        def clic():
            try:
                res["r"] = app.pendiente_hecha({"path": "r.wav", "i": 7, "texto": "Yo: preparar la demo",
                                                "hecha": True})
            except Exception as e:  # pragma: no cover - lo dice el assert
                res["e"] = e

        hilo = threading.Thread(target=clic)

        def con_lo_hecho(path, text):
            out = original(path, text)
            hilo.start()
            time.sleep(0.4)  # el clic llega ahora: con el candado espera; sin él, escribiría en el anterior
            return out
        jobs._con_lo_hecho = con_lo_hecho
        with mock.patch.object(server, "log"):
            self.resumir(jobs)
            hilo.join(10)
        self.assertNotIn("e", res)
        self.assertTrue(res["r"]["cambiado"])
        texto = server.read_text(p)
        self.assertIn("modelo: modelo-nuevo", texto)
        self.assertIn("- [x] Yo: preparar la demo", texto)
        self.assertEqual(plan.tareas_abiertas(texto), 0)


# ----------------------------------------------------------------------------- 4. la interfaz, con node

@unittest.skipUnless(shutil.which("node"), "node no instalado")
class SumHtmlTests(unittest.TestCase):
    def node(self, code):
        defs = (js_defs(read("app.js"), ["esc", "md"]) + "\n"
                + js_defs(read("notas.js"), ["linkStamps", "SUM_TAREA", "sumHtml"]))
        d = tempfile.mkdtemp(prefix="tenca-sumhtml-")
        self.addCleanup(shutil.rmtree, d, True)
        js = os.path.join(d, "prueba.js")
        with open(js, "w", encoding="utf-8") as f:
            f.write(defs + "\n" + code)
        r = subprocess.run(["node", js], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_cada_casilla_es_la_de_su_linea(self):
        cuerpo = pendientes.separar_cabecera(RESUMEN)[1]
        out = self.node(f"""const h = sumHtml({json.dumps(cuerpo)});
          const cajas = [...h.matchAll(/<li class="todo( done)?"><button[^>]*data-l="(\\d+)"[^>]*>(.)<\\/button>/g)]
            .map(m => [+m[2], !!m[1], m[3]]);
          console.log(JSON.stringify({{cajas, marcas: /\\uE000|\\uE001/.test(h), minuto: h.includes('data-ts="12:34"'),
            mas: h.includes('+ [ ] Equipo')}}));""")
        lineas = cuerpo.split("\n")
        # las de «-», «*» y «1.»/«2)», cada una con su línea del cuerpo; «+ [ ]» (md() no la pinta como lista), no
        esperadas = [(t["i"], t["hecha"]) for t in pendientes.extraer(cuerpo)
                     if not lineas[t["i"]].lstrip().startswith("+")]
        self.assertEqual([(k, h) for k, h, _ in out["cajas"]], esperadas)
        self.assertEqual({c for _, h, c in out["cajas"] if h}, {"☑"})
        self.assertEqual({c for _, h, c in out["cajas"] if not h}, {"☐"})
        self.assertFalse(out["marcas"])
        self.assertTrue(out["minuto"])  # los minutos siguen siendo enlaces
        self.assertTrue(out["mas"])

    def test_sangrada_con_espacio_duro_sin_casilla(self):
        """El servidor solo cuenta la sangría con espacios o tabuladores: una tarea sangrada con un espacio duro no
        lleva casilla que pulsar (daba siempre 409; revisión 1)."""
        out = self.node(r"""const h = sumHtml('\u00a0- [ ] Yo: x\n\t- [ ] Yo: y\n\u3000* [x] Yo: z');
          console.log(JSON.stringify([...h.matchAll(/data-l="(\d+)"/g)].map(m => +m[1])));""")
        self.assertEqual(out, [1])
        self.assertEqual([t["i"] for t in pendientes.extraer("\u00a0- [ ] Yo: x\n\t- [ ] Yo: y\n\u3000* [x] Yo: z")],
                         [1])


@unittest.skipUnless(shutil.which("node"), "node no instalado")
class PendientesHoyJsTests(unittest.TestCase):
    def node(self, code):
        with open(os.path.join(STATIC, "hoy.js"), encoding="utf-8") as f:
            src = f.read()
        d = tempfile.mkdtemp(prefix="tenca-pendjs-")
        self.addCleanup(shutil.rmtree, d, True)
        js = os.path.join(d, "prueba.js")
        with open(js, "w", encoding="utf-8") as f:
            f.write(JS_STUBS + src + "\n" + code)
        r = subprocess.run(["node", js], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_la_segunda_marcada_sigue_en_su_sitio_con_deshacer(self):
        """Con 14 tuyas, se marcan la 1 y la 4; al releer (sin ellas), la 4 sigue tras la 3 y dentro de las 12
        primeras, con su «Deshacer» (revisión 1: iba al final y lo perdía). Y «La última vez» se pide al momento."""
        out = self.node("""
          const t = n => ({path: 'p', i: n, texto: 'x' + n, responsable: 'Yo', tarea: 'tarea ' + n, segundo: null,
            tuya: true, titulo: 'R', fecha_txt: 'lun 9 nov'});
          let respuesta = {tareas: Array.from({length: 14}, (_, n) => t(n))};
          const llamadas = [];
          globalThis.api = (url, body) => {
            llamadas.push([url, body]);
            const r = url === '/api/pendientes' ? JSON.parse(JSON.stringify(respuesta)) : {ok: true, i: body.i};
            return Promise.resolve(r);
          };
          globalThis.toast = () => {};
          const cargas = [];
          hoyCargar = (...a) => { cargas.push(a); };
          HOY.datos = {ultima: {path: 'p'}};
          (async () => {
            await hoyPendCargar(true);
            const de = n => PEND.datos.tareas.find(x => x.texto === 'x' + n);
            await hoyPendMarcar(de(1), true);
            await hoyPendMarcar(de(4), true);
            respuesta = {tareas: Array.from({length: 14}, (_, n) => t(n)).filter(x => x.i !== 1 && x.i !== 4)};
            clearTimeout(PEND.hechas.get(de(1)._k)); PEND.hechas.delete(de(1)._k);  // vence el «Deshacer» de la 1
            PEND.datos.tareas = PEND.datos.tareas.filter(x => x.texto !== 'x1');
            await hoyPendCargar(true);
            const orden = PEND.datos.tareas.map(x => x.texto);
            const h = hoyPendHtml();
            PEND.hechas.forEach(clearTimeout);
            console.log(JSON.stringify({orden, deshacer: (h.match(/hoy-pend-deshacer/g) || []).length,
              cargas: cargas.length}));
          })();""")
        self.assertEqual(out["orden"][:5], ["x0", "x2", "x3", "x4", "x5"])
        self.assertEqual(out["deshacer"], 1)
        self.assertEqual(out["cargas"], 2)  # una por cada marcada: «La última vez» de esa reunión

    def test_tuyas_todas_y_ver_todas(self):
        out = self.node("""
          const t = (n, tuya) => ({path: 'p' + (n % 3), i: n, texto: 'x' + n, responsable: tuya ? 'Yo' : 'Ana',
            tarea: 'tarea ' + n, segundo: n === 1 ? 754 : null, tuya, titulo: 'R', fecha_txt: 'lun 9 nov'});
          PEND.datos = {tareas: Array.from({length: 15}, (_, n) => t(n, n < 3))};
          PEND.datos.tareas.forEach((x, n) => { x._k = hoyPendClave(x); x._id = n + 1; });
          const h1 = hoyPendHtml();
          PEND.filtro = 'todas';
          const h2 = hoyPendHtml();
          PEND.todo = true;
          const h3 = hoyPendHtml();
          const filas = h => (h.match(/class="mp-fila/g) || []).length;
          PEND.hechas.set(PEND.datos.tareas[0]._k, 1);
          PEND.filtro = null; PEND.todo = false;
          const h4 = hoyPendHtml();
          PEND.datos = {tareas: [t(0, false)]}; PEND.datos.tareas[0]._k = 'k'; PEND.hechas.clear();
          const h5 = hoyPendHtml();
          PEND.datos = {tareas: []};
          const h6 = hoyPendHtml();
          console.log(JSON.stringify({f1: filas(h1), tuyas1: h1.includes('Tuyas (3)'),
            todas1: h1.includes('Todas (15)'), min: h1.includes('[12:34]'), f2: filas(h2),
            mas2: h2.includes('Ver todas (15)'), f3: filas(h3), mas3: h3.includes('Ver todas'),
            deshacer: h4.includes('Deshacer'), tuyas4: h4.includes('Tuyas (2)'), todas4: h4.includes('Todas (14)'),
            sinfiltro: !h5.includes('Tuyas'), nada: h6.includes('ninguna tarea abierta'),
            nadaCard: h6.includes('<section')}));""")
        self.assertEqual((out["f1"], out["tuyas1"], out["todas1"], out["min"]), (3, True, True, True))
        self.assertEqual((out["f2"], out["mas2"]), (12, True))
        self.assertEqual((out["f3"], out["mas3"]), (15, False))
        self.assertEqual((out["deshacer"], out["tuyas4"], out["todas4"]), (True, True, True))
        self.assertTrue(out["sinfiltro"])
        self.assertEqual((out["nada"], out["nadaCard"]), (True, False))


if __name__ == "__main__":
    unittest.main()
