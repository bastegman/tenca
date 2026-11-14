# -*- coding: utf-8 -*-
"""Hoy (rediseño B7, msgs 1380 y 1390): la agenda de Outlook de un mes, la reunión de ahora que sale de ella, la
caché en segundo plano, /api/hoy y las funciones puras de hoy.js. Con el COM falso de test_reunion/test_calendario:
nada de aquí necesita Windows (lo de Windows de verdad, en test_calendario_windows).

El B2 del 09/11/2026 («conecta bien al parecer, pero no veo las siguientes reuniones», msg 1380): la lectura COM era
correcta (su «Probar» daba agenda, horas y asistentes bien), pero solo pedía HOY (agenda_hoy, 00:00-23:59) y de esa
solo enseñaba una frase («próxima reunión 12:00»): ninguna pantalla listaba las citas. AgendaB2Tests lo deja escrito."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

from support import REPO, TempTree, bare_jobs, server  # noqa: F401  (support prepara sys.path)
import calendario  # noqa: E402
import plan  # noqa: E402
from test_calendario import Base, SerieItems, pywin32_fecha  # noqa: E402
from test_perfil import settings_store  # noqa: E402
from test_reunion import FakeApp, FakeItem, FakeNamespace  # noqa: E402

NOW = datetime(2026, 11, 9, 10, 30)  # lunes
DIA = (lambda d: d.strftime("%d/%m/%Y %H:%M"))
PROTEGIDAS = {"Organizer", "RequiredAttendees", "OptionalAttendees", "Recipients", "Body", "RTFBody", "Resources",
              "PropertyAccessor"}


class Contada(FakeItem):
    """Apunta cada acceso a lo protegido (que abriría el aviso de seguridad de Outlook)."""
    accesos = []

    def __getattribute__(self, name):
        if name in PROTEGIDAS:
            Contada.accesos.append(name)
        return super().__getattribute__(name)


def cita(asunto, ini, minutos=60, **kw):
    return Contada(asunto, pywin32_fecha(ini), pywin32_fecha(ini + timedelta(minutes=minutos)), **kw)


def leer(items, now=NOW, **kw):
    log = []
    fake = FakeApp(items, log)
    out = calendario.agenda(now=now, session=lambda: fake, fmt=DIA, **kw)
    return out, log


# ----------------------------------------------------------------------------- 1. la agenda de un mes

class AgendaTests(Base):
    def setUp(self):
        super().setUp()
        Contada.accesos = []

    def test_un_mes_por_hora_sin_lo_protegido(self):
        items = [cita("Mañana", NOW + timedelta(days=1)), cita("Ahora", NOW - timedelta(minutes=20), recurring=True),
                 cita("Dentro de 30 días", datetime(2026, 12, 9, 9, 0)),
                 cita("Dentro de 31 días", datetime(2026, 12, 10, 9, 0)),
                 cita("Esta mañana, ya pasada", datetime(2026, 11, 9, 8, 0)), cita("Ayer", NOW - timedelta(days=1)),
                 cita("Cancelada", NOW + timedelta(days=2), status=5), cita("Todo el día", NOW, all_day=True),
                 cita("Mañana, sala", NOW + timedelta(days=1, hours=2), location="Sala 3", categories="Alfa; Beta")]
        (datos, fallo), log = leer(items)
        self.assertIsNone(fallo)
        # desde hoy a las 00:00 hasta el día 31 a las 23:59, con el orden de las periódicas
        self.assertEqual([k for k, _ in log][-3:], ["Sort", "IncludeRecurrences", "Restrict"])
        self.assertEqual(log[-1][1], "[Start] <= '09/12/2026 23:59' AND [End] >= '09/11/2026 00:00'")
        self.assertEqual([c["subject"] for c in datos["citas"]],
                         ["Esta mañana, ya pasada", "Ahora", "Mañana", "Mañana, sala", "Dentro de 30 días"])
        self.assertEqual(set(datos["citas"][0]), {"subject", "start", "end", "location", "is_teams", "recurring",
                                                  "categories"})
        sala = datos["citas"][3]
        self.assertEqual((sala["location"], sala["is_teams"], sala["categories"]), ("Sala 3", False, ["Alfa", "Beta"]))
        self.assertEqual((datos["citas"][1]["recurring"], datos["citas"][1]["is_teams"]), (True, True))
        self.assertEqual(datos["citas"][1]["start"], "2026-11-09T10:10")  # la hora de Outlook, sin convertir
        self.assertEqual((datos["formato"], datos["cortada"]), ("dado", False))
        self.assertEqual(Contada.accesos, [])  # ni organizador ni asistentes: no puede salir el aviso de Outlook

    def test_una_serie_sin_fin_da_sus_repeticiones_del_mes_y_acaba(self):
        log = []

        class NS(FakeNamespace):
            def GetDefaultFolder(s, n):
                return types.SimpleNamespace(Items=SerieItems(datetime(2026, 9, 28, 9, 30), log))

        app = types.SimpleNamespace(GetNamespace=lambda name: NS([], log))
        datos, fallo = calendario.agenda(now=NOW, session=lambda: app, fmt=DIA, max_citas=200)
        self.assertIsNone(fallo)
        self.assertEqual(len(datos["citas"]), 31)  # del 9-nov al 9-dic, una al día
        self.assertEqual((datos["citas"][0]["start"], datos["citas"][-1]["start"]),
                         ("2026-11-09T09:30", "2026-12-09T09:30"))
        self.assertTrue(all(c["recurring"] for c in datos["citas"]))
        self.assertTrue(datos["cortada"])  # el doble no se acaba nunca: se corta en el tope y se dice

    def test_errores_y_fuera_de_windows(self):
        with mock.patch.object(calendario, "WINDOWS", False):
            self.assertEqual(calendario.agenda(now=NOW)[1][0], "no_windows")
        e = Exception(calendario.CO_E_CLASSSTRING, "Invalid class string", None, None)
        e.hresult = calendario.CO_E_CLASSSTRING
        datos, fallo = calendario.agenda(now=NOW, session=lambda: (_ for _ in ()).throw(e), fmt=DIA)
        self.assertEqual((datos, fallo[0]), (None, "sin_outlook"))

    def test_cortada_solo_si_quedaban_mas(self):
        """Revisión 1: con exactamente el tope no está cortada; con una más, sí."""
        tres = [cita(f"C{i}", NOW + timedelta(hours=i)) for i in range(3)]
        (datos, _), _ = leer(tres, max_citas=3)
        self.assertEqual((len(datos["citas"]), datos["cortada"]), (3, False))
        (datos, _), _ = leer(tres + [cita("C3", NOW + timedelta(hours=4))], max_citas=3)
        self.assertEqual((len(datos["citas"]), datos["cortada"]), (3, True))

    def test_outlook_ocupado_con_otra_consulta(self):
        """El candado COM cogido (una consulta colgada): no se amontona otro hilo, se dice que no contesta."""
        self.assertTrue(calendario._COM_LOCK.acquire(timeout=1))
        try:
            datos, fallo = calendario.agenda(now=NOW, session=lambda: FakeApp([], []), fmt=DIA, espera_otra=0.05)
        finally:
            calendario._COM_LOCK.release()
        self.assertEqual((datos, fallo[0]), (None, "ocupado"))


class AgendaB2Tests(Base):
    def test_b2_probar_solo_miraba_hoy_y_la_agenda_mira_el_mes(self):
        """La causa del B2: lo que había (agenda_hoy, lo que usa «Probar») solo lee hoy y solo da cifras y la próxima;
        la agenda de Hoy lee el mes y da cada cita."""
        items = [FakeItem("Hoy", NOW + timedelta(hours=1), NOW + timedelta(hours=2)),
                 FakeItem("Mañana", NOW + timedelta(days=1), NOW + timedelta(days=1, hours=1)),
                 FakeItem("La semana que viene", NOW + timedelta(days=7), NOW + timedelta(days=7, hours=1))]
        hoy, fallo = calendario.agenda_hoy(now=NOW, session=lambda: FakeApp(list(items), []), fmt=DIA)
        self.assertIsNone(fallo)
        self.assertEqual((hoy["citas"], hoy["proxima"]["inicio"]), (1, "11:30"))
        self.assertNotIn("citas_lista", hoy)  # nada que listar: solo la cifra
        mes, _ = calendario.agenda(now=NOW, session=lambda: FakeApp(list(items), []), fmt=DIA)
        self.assertEqual([c["subject"] for c in mes["citas"]], ["Hoy", "Mañana", "La semana que viene"])


# ----------------------------------------------------------------------------- 2. la reunión de ahora

def publica(asunto, ini, minutos=60, **kw):
    return dict({"subject": asunto, "start": ini.isoformat(timespec="minutes"),
                 "end": (ini + timedelta(minutes=minutos)).isoformat(timespec="minutes"), "location": "",
                 "is_teams": False, "recurring": False, "categories": []}, **kw)


LISTAS = [{"id": "Alfa/Vocabulario/General.txt", "project": "Alfa", "name": "General", "terms": ["comité"]}]


class AhoraTests(unittest.TestCase):
    def test_la_ventana_de_ahora(self):
        citas = [publica("Ya acabó", NOW - timedelta(minutes=70)),
                 publica("Acabó hace 3 min", NOW - timedelta(minutes=63)),
                 publica("En 8 min", NOW + timedelta(minutes=8)), publica("En 20 min", NOW + timedelta(minutes=20))]
        self.assertEqual(calendario.cita_ahora(citas, NOW)["subject"], "En 8 min")  # la más cercana entre las dos
        self.assertEqual(calendario.cita_ahora(citas[:1] + citas[3:], NOW), None)
        en_curso = publica("En curso", NOW - timedelta(minutes=30))
        self.assertEqual(calendario.cita_ahora(citas + [en_curso], NOW)["subject"], "En curso")
        self.assertTrue(calendario.cita_ahora([en_curso], NOW)["is_now"])
        # el título de Teams manda sobre la que está en curso
        self.assertEqual(calendario.cita_ahora(citas + [en_curso], NOW, "En 8 min | Microsoft Teams")["subject"],
                         "En 8 min")
        self.assertIsNone(calendario.cita_ahora([{"subject": "rota", "start": "x", "end": None}], NOW))

    def test_sin_teams_la_cita_de_la_agenda_con_su_proyecto(self):
        pedidas = []

        def listas():
            pedidas.append(1)
            return LISTAS
        sug, info = calendario.reunion_de_ahora({}, [publica("Comité Alfa", NOW - timedelta(minutes=30),
                                                             recurring=True)], listas, NOW)
        self.assertEqual((sug["name"], sug["source"], sug["vocab"], sug["participants"]),
                         ("Comité Alfa", "outlook", "Alfa/Vocabulario/General.txt", []))
        self.assertEqual(info, {"titulo": "Comité Alfa", "fuente": "outlook", "teams": False,
                                "inicio": "2026-11-09T10:00", "fin": "2026-11-09T11:00", "en_curso": True,
                                "periodica": True, "asunto": "Comité Alfa"})
        self.assertEqual(pedidas, [1])

    def test_teams_manda_y_las_horas_solo_si_es_la_misma(self):
        lect = {"teams": {"title": "Comité Alfa"}, "sugerencia": {"name": "Comité Alfa", "source": "teams"}}
        citas = [publica("Comité Alfa", NOW - timedelta(minutes=30))]
        sug, info = calendario.reunion_de_ahora(lect, citas, lambda: self.fail("no hacen falta las listas"), NOW)
        self.assertEqual((sug["source"], info["teams"], info["inicio"], info["en_curso"]),
                         ("teams", True, "2026-11-09T10:00", True))
        _, info = calendario.reunion_de_ahora(lect, [publica("Otra cosa", NOW - timedelta(minutes=30))], [], NOW)
        self.assertEqual((info["titulo"], info["inicio"], info["fin"]), ("Comité Alfa", None, None))

    def test_la_cita_que_vio_el_vigilante_en_la_llamada(self):
        lect = {"teams": None, "outlook": publica("Comité", NOW - timedelta(minutes=5)),
                "sugerencia": {"name": "Comité", "source": "outlook", "participants": ["Ana"]}}
        sug, info = calendario.reunion_de_ahora(lect, [], [], NOW)
        self.assertEqual((sug["participants"], info["inicio"]), (["Ana"], "2026-11-09T10:25"))

    def test_a_punto_de_empezar_y_nada(self):
        _, info = calendario.reunion_de_ahora({}, [publica("Luego", NOW + timedelta(minutes=8))], [], NOW)
        self.assertFalse(info["en_curso"])
        self.assertEqual(calendario.reunion_de_ahora({}, [], [], NOW), ({}, None))
        self.assertEqual(calendario.reunion_de_ahora(None, [publica("  ", NOW)], [], NOW), ({}, None))

    def test_sin_leer(self):
        f = calendario.sin_leer
        base = {"windows": True, "pywin32": True, "instalado": True, "abierto": True}
        self.assertIsNone(f(base))
        self.assertEqual(f({"windows": False})[0], "no_windows")
        self.assertEqual(f(dict(base, pywin32=False))[0], "sin_pywin32")
        self.assertEqual(f(dict(base, instalado=False))[0], "sin_outlook")
        self.assertEqual(f(dict(base, instalado=False, nuevo_activado=True))[0], "outlook_nuevo")
        self.assertEqual(f(dict(base, abierto=False))[0], "cerrado")
        self.assertEqual(f(dict(base, abierto=False, nuevo_abierto=True))[0], "outlook_nuevo")
        self.assertIsNone(f(dict(base, abierto=None)))  # no se sabe: se prueba


# ----------------------------------------------------------------------------- 3. la caché en segundo plano

class Reloj:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        p = mock.patch.object(calendario, "log", self.logs.append)
        p.start()
        self.addCleanup(p.stop)

    def test_lectura_buena_luego_fallo_y_lo_que_se_dice(self):
        respuestas = [({"citas": [publica("A", NOW)], "formato": "regional", "cortada": False}, None),
                      (None, ("cerrado", calendario.FRASES["cerrado"]))]
        reloj = Reloj()
        ag = calendario.Agenda(leer=lambda now=None, **kw: respuestas.pop(0), reloj=reloj)
        self.assertEqual(ag.publica(True)["estado"], "leyendo")
        self.assertTrue(ag.toca())
        self.assertTrue(ag.refrescar())
        r = ag.publica(True)
        self.assertEqual((r["estado"], [c["subject"] for c in r["citas"]], r["dias"]), ("ok", ["A"], 31))
        self.assertFalse(ag.toca())
        reloj.t += calendario.AGENDA_CADA
        self.assertTrue(ag.toca())
        self.assertFalse(ag.refrescar())
        r = ag.publica(True)  # la de antes se queda, con su hora y por qué no se ha renovado
        self.assertEqual((r["estado"], len(r["citas"]), r["accion"]["id"]), ("cerrado", 1, "leer"))
        self.assertRegex(r["frase"], r"^Outlook clásico está cerrado: .* Lo de abajo es de las \d+:\d\d\.$")
        self.assertEqual(ag.citas(), r["citas"])
        r["citas"][0]["preparada"] = {"idioma": "pt"}  # /api/hoy anota las suyas: la caché no cambia
        ag.citas()[0]["otra"] = 1
        self.assertEqual(set(ag.publica(True)["citas"][0]), {"subject", "start", "end", "location", "is_teams",
                                                             "recurring", "categories"})
        # server.log: solo cifras y estados, y cada cosa una vez
        self.assertEqual([re.sub(r"\(\d+ ms\)", "(N ms)", x) for x in self.logs],
                         ["   calendario: agenda · 1 citas (N ms)", "   calendario: agenda · cerrado (N ms)"])
        for linea in self.logs:
            self.assertTrue(any(rx.fullmatch(linea) for rx in server._DIAG_RX), linea)
            self.assertNotIn("<texto>", server.diag_line(linea))

    def test_apagado_y_sin_outlook(self):
        ag = calendario.Agenda(leer=lambda **kw: self.fail("apagado no se lee"))
        r = ag.publica(False)
        self.assertEqual((r["estado"], r["accion"]["id"], r["citas"]), ("apagado", "ajustes", []))
        self.assertIn("próximo mes", r["frase"])
        r = ag.publica(False, ("sin_outlook", calendario.FRASES["sin_outlook"]))
        self.assertEqual((r["estado"], r["accion"]), ("sin_outlook", None))
        r = ag.publica(False, ("apagado", "lo de Ajustes"))
        self.assertEqual(r["frase"], calendario.FRASES_AGENDA["apagado"])

    def test_anotar_sin_leer_y_olvidar(self):
        ag = calendario.Agenda(leer=lambda **kw: ({"citas": [publica("A", NOW)], "cortada": True}, None))
        ag.anotar(("sin_pywin32", calendario.FRASES["sin_pywin32"]))
        r = ag.publica(True)
        self.assertEqual((r["estado"], r["accion"]), ("sin_pywin32", None))  # reinstalar no es «volver a leer»
        ag.anotar(("sin_pywin32", calendario.FRASES["sin_pywin32"]))
        self.assertEqual(len(self.logs), 1)  # lo mismo otra vez: no se repite en server.log
        ag.refrescar()
        self.assertTrue(ag.publica(True)["cortada"])
        self.assertIn("(cortada)", self.logs[-1])
        ag.olvidar()
        self.assertEqual((ag.citas(), ag.publica(True)["estado"]), ([], "leyendo"))

    def test_una_lectura_a_la_vez_y_un_fallo_raro_no_mata_el_hilo(self):
        ag = calendario.Agenda(leer=lambda **kw: 1 / 0)
        with mock.patch.object(calendario, "log_exc"):
            self.assertFalse(ag.refrescar())
        self.assertEqual(ag.publica(True)["estado"], "error")
        ag.leyendo = True
        self.assertFalse(ag.refrescar())
        self.assertFalse(ag.toca(0))

    def test_el_outlook_falso_de_las_pruebas_de_la_interfaz(self):
        datos, fallo = calendario.agenda(session=calendario.sesion_de_prueba(
            os.path.join(REPO, "tests", "fake_outlook", "agenda.json")))
        self.assertIsNone(fallo)
        asuntos = [c["subject"] for c in datos["citas"]]
        self.assertIn("Comité de seguimiento", asuntos)
        self.assertFalse([a for a in asuntos if "no sale" in a])


# ----------------------------------------------------------------------------- 4. tareas abiertas

class ConPreparadaTests(unittest.TestCase):
    def test_lo_de_la_tarjeta_manda(self):
        prep = {"vocab": "v1", "idioma": "pt", "contexto": "c"}
        self.assertEqual(plan.con_preparada({}, prep), {"vocab": "v1", "cambios": {"idioma": "pt", "contexto": "c"},
                                                        "preparada": ["vocab", "idioma", "contexto"]})
        p = plan.con_preparada({"vocab": "v2", "cambios": {"idioma": "en"}}, prep)
        self.assertEqual(p, {"vocab": "v2", "cambios": {"idioma": "en", "contexto": "c"}, "preparada": ["contexto"]})
        self.assertEqual(plan.con_preparada({"cambios": {}}, {}), {"cambios": {}})
        pedido = {"cambios": {}}
        plan.con_preparada(pedido, prep)
        self.assertEqual(pedido, {"cambios": {}})  # no toca el pedido que recibe


class TareasTests(unittest.TestCase):
    def test_tareas_abiertas(self):
        t = ("## Próximos pasos\n- [ ] Ana: presupuesto [12:00]\n- [x] Luis: hecho\n* [ ] Yo: acta\n1. [ ] Otra\n"
             "  - [ ] sangrada\n- [ ]\n- [] mal escrita\nTexto - [ ] en medio\n")
        self.assertEqual(plan.tareas_abiertas(t), 4)
        self.assertEqual(plan.tareas_abiertas("## Próximos pasos\nNinguno\n"), 0)
        self.assertEqual(plan.tareas_abiertas(None), 0)


# ----------------------------------------------------------------------------- 5. el servidor

class ServidorTests(TempTree):
    def setUp(self):
        super().setUp()
        self.write("Alfa/Vocabulario/General.txt", "comité, API\n")
        self.lect = {}

    def app(self, **st):
        a = server.App.__new__(server.App)
        a.settings = settings_store(self.tmp, **st)
        a.rec_lock, a.notes_lock, a.toggle_lock, a.rec, a.monitor = (threading.Lock(), threading.Lock(),
                                                                     threading.Lock(), None, None)
        a.library = types.SimpleNamespace(at=1, flat=[])
        a.jobs = bare_jobs([])
        a.jobs.settings = a.settings
        a._vocab_terms = lambda: [{"id": "Alfa/Vocabulario/General.txt", "project": "Alfa", "name": "General",
                                   "terms": ["comité", "API"]}]
        a.vigia = calendario.Vigia()
        a.vigia.lectura = lambda leer, max_age=90, recordar=True: dict(self.lect)
        a.agenda = calendario.Agenda(leer=lambda **kw: ({"citas": self.citas, "cortada": False}, None))
        a.agenda_sesion, a.meeting = None, None
        now = datetime.now().replace(second=0, microsecond=0)
        self.citas = [publica("Comité de seguimiento", now - timedelta(minutes=20)),
                      publica("Mañana", now + timedelta(days=1))]
        a.agenda.refrescar()
        return a

    def test_api_hoy_y_ctrl_alt_r_con_la_misma_reunion(self):
        a = self.app(outlook_on=True)
        a.library.flat = [
            {"path": "Alfa/vieja.wav", "label": "Comité de seguimiento", "date": "2026-10-30T10:00:00",
             "props": {"vocabulario": "Alfa/Vocabulario/General.txt", "proyecto": "Alfa", "workstream": "General"},
             "_sum": self.write("Alfa/vieja.resumen.md", "- [ ] Ana: x\n- [ ] Luis: y\n- [x] z\n")},
            {"path": "Alfa/mas-vieja.wav", "label": "Comité de seguimiento", "date": "2026-10-23T10:00:00",
             "props": {}, "_sum": None}]
        r = a.hoy({})
        self.assertEqual((r["ahora"]["titulo"], r["ahora"]["fuente"], r["ahora"]["en_curso"]),
                         ("Comité de seguimiento", "outlook", True))
        self.assertEqual(r["plan"]["nombre"], {"valor": "Comité de seguimiento", "origen": "outlook",
                                               "texto": "de Outlook"})
        self.assertEqual(r["plan"]["proyecto"]["origen"], "historial")
        self.assertEqual(r["ultima"], {"path": "Alfa/vieja.wav", "fecha": "2026-10-30T10:00:00",
                                       "fecha_txt": "vie 30 oct", "resumen": True, "tareas": 2})
        self.assertEqual([c["subject"] for c in r["agenda"]["citas"]], ["Comité de seguimiento", "Mañana"])
        # la tarjeta (grabar_plan_api sin nada) y Ctrl+Alt+R usan la misma reunión de ahora
        self.assertEqual(a.grabar_plan_api({})["rec"], r["plan"]["rec"])
        vistos = []
        a.rec_start = lambda b: vistos.append(b) or {"path": "x.wav", "workstream": None}
        with mock.patch.object(server, "cue"), mock.patch.object(server, "notify"):
            a.rec_toggle({"source": "hotkey"})
        self.assertEqual({k: v for k, v in vistos[0].items() if k != "resumen"}, r["plan"]["rec"])

    def test_la_reunion_de_ahora_y_la_lista_salen_de_la_misma_foto(self):
        """Si el hilo renueva la agenda en mitad de /api/hoy, la reunión de ahora no puede salir de otra lectura que la
        lista (en ui-smoke de Ubuntu salía «agenda ok» con ahora = None)."""
        a = self.app(outlook_on=True)
        a.agenda.citas = lambda: []  # lo que habría dado una lectura de la caché a destiempo
        r = a.hoy({})
        self.assertEqual(r["ahora"]["titulo"], "Comité de seguimiento")

    def test_con_el_calendario_apagado_no_se_usa_la_agenda(self):
        a = self.app(outlook_on=False)
        with mock.patch.object(calendario, "estado", lambda on: {"estado": "apagado", "frase": "x"}):
            r = a.hoy({})
        self.assertIsNone(r["ahora"])
        self.assertEqual((r["agenda"]["estado"], r["agenda"]["citas"]), ("apagado", []))
        self.assertEqual(r["plan"]["nombre"]["valor"], "")

    def test_teams_manda_y_hoy_no_guarda_su_lectura(self):
        a = self.app(outlook_on=True)
        self.lect = {"teams": {"title": "Otra reunión"}, "sugerencia": {"name": "Otra reunión", "source": "teams"}}
        r = a.hoy({})
        self.assertEqual((r["ahora"]["titulo"], r["ahora"]["teams"], r["ahora"]["inicio"]),
                         ("Otra reunión", True, None))
        a2 = self.app(outlook_on=True)
        a2.vigia = calendario.Vigia()
        with mock.patch.object(calendario, "reunion_actual", lambda *x, **k: {"sugerencia": {}, "teams": None}):
            a2.hoy({})
            self.assertIsNone(a2.vigia.ultimo)  # Hoy pregunta cada 30 s: no tapa la lectura con Outlook del vigilante
            a2.grabar_plan_api({})
            self.assertIsNotNone(a2.vigia.ultimo)  # la tarjeta sí, como antes

    def test_un_vigilante_que_solo_da_la_sugerencia(self):
        """Los dobles de otros tests (y de otras ramas) solo tienen `sugerencia`: siguen valiendo."""
        a = self.app(outlook_on=True)
        a.vigia = types.SimpleNamespace(sugerencia=lambda leer: {"name": "De Teams", "source": "teams"})
        self.assertEqual(a._reunion_ahora()[0]["name"], "De Teams")

    def test_la_ultima_vez_sin_la_que_se_graba(self):
        a = self.app(outlook_on=True)
        a.library.flat = [{"path": "Alfa/hoy.wav", "label": "Comité de seguimiento", "date": "2026-11-09T10:00:00",
                           "props": {}, "_sum": None, "recording": True},
                          {"path": "Alfa/x.wav", "label": "", "date": "2026-11-02T10:00:00",
                           "props": {"titulo": "comité  de SEGUIMIENTO"}, "_sum": None}]
        self.assertEqual(a._ultima_vez("Comité de seguimiento"),
                         {"path": "Alfa/x.wav", "fecha": "2026-11-02T10:00:00", "fecha_txt": "lun 2 nov",
                          "resumen": False, "tareas": None})
        a.library.flat[1]["_sum"] = os.path.join(self.tmp, "no-existe.resumen.md")
        self.assertIsNone(a._ultima_vez("Comité de seguimiento")["tareas"])  # el resumen se ha ido: sin cifra
        self.assertIsNone(a._ultima_vez(""))
        self.assertIsNone(a._ultima_vez("Otra"))

    def test_el_hilo_de_la_agenda(self):
        a = self.app(outlook_on=True)
        llamadas = []
        a.agenda = calendario.Agenda(leer=lambda **kw: llamadas.append(kw) or ({"citas": []}, None))
        env = {"windows": True, "pywin32": True, "instalado": True, "abierto": True}
        with mock.patch.object(calendario, "log"):
            a._agenda_paso(entorno=env)
            self.assertEqual(len(llamadas), 1)
            a._agenda_paso(entorno=env)  # no toca aún
            self.assertEqual(len(llamadas), 1)
            a._agenda_paso(forzado=True, entorno=env)  # Hoy la ha pedido
            self.assertEqual(len(llamadas), 2)
            a.meeting = "Teams"  # en una llamada, con agenda ya leída: no se relee
            a._agenda_paso(forzado=True, entorno=env)
            self.assertEqual(len(llamadas), 2)
            a.meeting = None
            a._agenda_paso(forzado=True, entorno=dict(env, abierto=False))  # cerrado: sin hablar con Outlook
            self.assertEqual((len(llamadas), a.agenda.publica(True)["estado"]), (2, "cerrado"))
            a.agenda_sesion = "sesion-de-prueba"
            a._agenda_paso(forzado=True)
            self.assertEqual(llamadas[-1]["session"], "sesion-de-prueba")
            a.settings.update(lambda d: d.update(outlook_on=False))  # apagado: se olvida lo leído
            a._agenda_paso(forzado=True)
            self.assertEqual(a.agenda.citas(), [])
            self.assertEqual(a.agenda.publica(True)["estado"], "leyendo")

    def test_hoy_pide_la_agenda_al_abrirla_si_es_vieja_y_el_sondeo_no(self):
        """Revisión 1: con el sondeo de 30 s pidiendo cada vez que tenía más de 2 min, Outlook se releía cada ~2 min con
        Hoy a la vista. Ahora solo al abrir Hoy (abrir=1) si es vieja, o con «Actualizar» (refrescar=1)."""
        a = self.app(outlook_on=True)
        a.agenda.despertar.clear()
        a.hoy({"abrir": "1"})
        self.assertFalse(a.agenda.despertar.is_set())  # recién leída
        a.hoy({"refrescar": "1"})
        self.assertTrue(a.agenda.despertar.is_set())
        a.agenda.despertar.clear()
        a.agenda.intento -= calendario.AGENDA_VIEJA
        a.hoy({})
        self.assertFalse(a.agenda.despertar.is_set())  # el sondeo no relee
        a.hoy({"abrir": "1"})
        self.assertTrue(a.agenda.despertar.is_set())
        self.assertTrue(a.hoy({})["agenda"]["comprobada"])

    def test_una_agenda_vieja_se_ensena_pero_no_pone_nombre(self):
        """Revisión 1: con Outlook cerrado horas, una cita movida o cancelada seguiría nombrando las grabaciones."""
        a = self.app(outlook_on=True)
        self.assertEqual(a.hoy({})["ahora"]["titulo"], "Comité de seguimiento")
        a.agenda.leida -= calendario.AGENDA_FRESCA + 1
        r = a.hoy({})
        self.assertIsNone(r["ahora"])
        self.assertEqual(len(r["agenda"]["citas"]), 2)
        self.assertEqual(a._reunion_ahora()[0], {})

    def test_preparada_con_signos_en_el_asunto(self):
        """Revisión 1 (P1): «Comité: Q4» de Outlook es «Comité Q4» en la reunión de ahora (nombre_archivo)."""
        a = self.app(outlook_on=True)
        ini = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=20)
        largo = "RE: Alfa | Sync / Q4 «presupuesto» " + "x" * 90
        for asunto in ("Comité: Q4", largo):
            self.citas[:] = [publica(asunto, ini)]
            a.agenda.refrescar()
            a.hoy_preparar({"asunto": asunto, "inicio": ini.isoformat(timespec="minutes"),
                            "fin": (ini + timedelta(minutes=60)).isoformat(timespec="minutes"), "idioma": "pt"})
            r = a.hoy({})
            self.assertEqual(r["agenda"]["citas"][0]["preparada"], {"idioma": "pt"}, asunto)
            self.assertEqual(r["ahora"]["asunto"], asunto)  # la agenda marca «Ahora» por el asunto de Outlook
            idioma = {x["clave"]: x for x in r["plan"]["lineas"]}["idioma"]
            self.assertEqual((idioma["code"], idioma["origen"]), ("pt", "preparada"), asunto)

    def test_preparadas_raras_en_settings_no_tumban_hoy(self):
        a = self.app(outlook_on=True)
        base = {"asunto": "X", "inicio": "2030-01-01T10:00", "fin": "2030-01-01T11:00", "idioma": "pt"}
        for raro in ("texto", ["lista"], {"k": "no es un dict"}, None):
            a.settings.update(lambda d: d.update(preparadas=raro))
            self.assertIn("agenda", a.hoy({}))
            a.hoy_preparar(base)
            self.assertEqual(list(a.settings.get()["preparadas"].values()), [base])

    def test_el_hilo_no_relee_grabando_ni_en_llamada(self):
        """Grabando, o en una llamada aunque «Avisarme si estoy en una llamada» esté apagado (meeting None, pero el
        registro de privacidad dice que hay llamada: meeting_prev), con agenda ya leída no se relee."""
        a = self.app(outlook_on=True)
        llamadas = []
        a.agenda.leer = lambda **kw: llamadas.append(1) or ({"citas": []}, None)
        env = {"windows": True, "pywin32": True, "instalado": True, "abierto": True}
        with mock.patch.object(calendario, "log"):
            a.rec = types.SimpleNamespace(alive=lambda: True)
            a._agenda_paso(forzado=True, entorno=env)
            a.rec, a.meeting_prev = None, "Teams"
            a._agenda_paso(forzado=True, entorno=env)
            self.assertEqual(llamadas, [])
            a.meeting_prev = None
            a._agenda_paso(forzado=True, entorno=env)
            self.assertEqual(llamadas, [1])

    def test_activar_prueba_antes_de_pedir_la_agenda(self):
        """Revisión 1: «Activar» lanza «Probar» con el usuario delante; si la agenda se pidiera antes, competirían por
        el candado de Outlook y «Probar» podría decir «ocupado»."""
        a = self.app(outlook_on=False)
        orden = []
        a.agenda.pedir = lambda: orden.append("agenda")
        with mock.patch.object(calendario, "estado", lambda on, probar=False: orden.append("probar") or {}), \
                mock.patch.object(server, "log"):
            a.calendario_activar({"activar": True})
        self.assertEqual(orden, ["probar", "agenda"])

    def test_preparar_una_cita_y_se_usa_al_grabarla(self):
        a = self.app(outlook_on=True)
        ini = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=20)  # la de ahora (self.citas)
        cita = {"asunto": "Comité de seguimiento", "inicio": ini.isoformat(timespec="minutes"),
                "fin": (ini + timedelta(minutes=60)).isoformat(timespec="minutes")}
        r = a.hoy_preparar(dict(cita, vocab="Alfa/Vocabulario/General.txt", idioma="pt",
                                contexto="  Presupuesto   Q4 "))
        self.assertEqual(r["preparada"], dict(cita, vocab="Alfa/Vocabulario/General.txt", idioma="pt",
                                              contexto="Presupuesto Q4"))
        h = a.hoy({})
        self.assertEqual(h["agenda"]["citas"][0]["preparada"], {"vocab": "Alfa/Vocabulario/General.txt",
                                                                "idioma": "pt", "contexto": "Presupuesto Q4"})
        self.assertNotIn("preparada", h["agenda"]["citas"][1])
        lineas = {x["clave"]: x for x in h["plan"]["lineas"]}
        self.assertEqual((h["plan"]["proyecto"]["origen"], h["plan"]["proyecto"]["motivo"]),
                         ("preparada", "preparado en Hoy"))
        self.assertEqual((lineas["idioma"]["code"], lineas["idioma"]["origen"], lineas["idioma"]["origen_txt"]),
                         ("pt", "preparada", "preparado en Hoy"))
        self.assertEqual((lineas["contexto"]["valor"], lineas["contexto"]["origen"]), ("Presupuesto Q4", "preparada"))
        # lo cambiado en los chips de Hoy o en la tarjeta manda sobre lo preparado
        otro = {x["clave"]: x for x in a.grabar_plan_api({"cambios": {"idioma": "en"}})["lineas"]}
        self.assertEqual((otro["idioma"]["code"], otro["idioma"]["origen"]), ("en", "reunion"))
        # Ctrl+Alt+R graba con lo preparado
        vistos = []
        a.rec_start = lambda b: vistos.append(b) or {"path": "x.wav", "workstream": None}
        with mock.patch.object(server, "cue"), mock.patch.object(server, "notify"):
            a.rec_toggle({"source": "hotkey"})
        self.assertEqual((vistos[0]["language"], vistos[0]["vocab"], vistos[0]["contexto"]),
                         ("pt", "Alfa/Vocabulario/General.txt", "Presupuesto Q4"))
        # de 10 min antes a 5 min después, y solo esa reunión
        self.assertIsNotNone(a._preparada("comité  de SEGUIMIENTO", now=ini - timedelta(minutes=10)))
        self.assertIsNone(a._preparada("Comité de seguimiento", now=ini - timedelta(minutes=11)))
        self.assertIsNone(a._preparada("Comité de seguimiento", now=ini + timedelta(minutes=66)))
        self.assertIsNone(a._preparada("Otra", now=ini))
        # quitar (o guardar sin nada que usar) la deja como siempre
        a.hoy_preparar(dict(cita, quitar=True, vocab="x"))
        self.assertEqual(a.settings.get()["preparadas"], {})
        a.hoy_preparar(dict(cita, contexto="   "))
        self.assertEqual(a.settings.get()["preparadas"], {})
        self.assertNotEqual(a.hoy({})["plan"]["proyecto"]["origen"], "preparada")

    def test_preparar_valida_y_olvida_lo_viejo(self):
        a = self.app(outlook_on=True)
        base = {"asunto": "X", "inicio": "2026-11-10T10:00", "fin": "2026-11-10T11:00"}
        for malo, texto in ((dict(base, asunto=" "), "asunto"), (dict(base, inicio="ayer"), "hora"),
                            (dict(base, idioma="portugués"), "Idioma"), (dict(base, vocab="Zeta/Vocabulario/No.txt"),
                                                                         "proyecto")):
            with self.assertRaises(server.ApiError) as cm:
                a.hoy_preparar(malo)
            self.assertIn(texto, cm.exception.msg)
        vieja = {"asunto": "V", "inicio": "2020-01-01T10:00", "fin": "2020-01-01T11:00", "idioma": "es"}
        a.settings.update(lambda d: d.update(preparadas={"vieja": vieja}))
        hoy = datetime.now() + timedelta(days=1)
        a.hoy_preparar(dict(base, inicio=hoy.isoformat(timespec="minutes"),
                            fin=(hoy + timedelta(hours=1)).isoformat(timespec="minutes"), idioma="auto", vocab=""))
        prep = a.settings.get()["preparadas"]
        self.assertEqual(len(prep), 1)
        self.assertEqual({k: v for k, v in list(prep.values())[0].items() if k in ("idioma", "vocab")},
                         {"idioma": "", "vocab": ""})

    def test_ruta_y_voces_sin_nombre(self):
        with open(server.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"/api/hoy": a.hoy', src)
        self.assertIn('threading.Thread(target=self._agenda_watch, daemon=True, name="agenda-outlook").start()', src)
        lib = server.Library.__new__(server.Library)
        lib.speakers = {}
        p = self.write("x.hablantes.json", json.dumps({"tramos": [[0, 1, "A"]], "tu": "C", "hablantes": {
            "A": {}, "B": {"breve": True}, "C": {}, "D": {"persona": "p1"},
            "E": {"persona": "p2", "provisional": True}}}))
        info = lib._speakers({"_spk": p, "spk_mtime": 1})
        self.assertEqual((info["n"], info["known"], info["doubt"], info["sin_nombre"]), (4, 1, 1, 1))


# ----------------------------------------------------------------------------- 6. hoy.js (node)

STATIC = os.path.join(REPO, "_app", "static")
JS_STUBS = r"""
const S = {jobs: [], vocab: [{id: 'v1', project: 'Alfa', name: 'General'}], audios: new Map(), route: {kind: 'x'}};
const main = {addEventListener() {}}, document = {hidden: false}, addEventListener = () => {};
const setInterval = () => 0;
const $ = () => null, $$ = () => [], esc = s => String(s ?? ''), I = new Proxy({}, {get: () => ''});
const pad = n => String(n).padStart(2, '0'), nfc = s => s;
const MES = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic'];
const aDate = a => a.date ? new Date(a.date) : null, titleOf = a => (a.props && a.props.titulo) || '';
const iconOf = () => '', isLive = a => !!a.live, jobFor = p => S.jobs.find(j => j.path === p && j.activo);
const statusOf = a => a.st, estDuration = a => a.dur || 60, fmtClock = s => `${Math.floor(s / 60)}:${pad(s % 60)}`;
const fmtDur = s => `${Math.round(s / 60)} min`;
"""


# ----------------------------------------------------------------------------- 6. «Ya ha empezado «X»» al parar

class SiguienteReglaTests(unittest.TestCase):
    """La siguiente reunión es la reunión de ahora de siempre (reunion_de_ahora) sin la que se acaba de grabar."""
    A = [{"asunto": "Comité Alfa", "inicio": "2026-11-09T09:30"}]

    def test_excluida(self):
        f = calendario.excluida
        self.assertTrue(f(self.A, "Comité Alfa", "2026-11-09T09:30"))
        self.assertTrue(f(self.A, "comité: alfa", "2026-11-09T09:30:00"))  # como el nombre del archivo y sin segundos
        self.assertTrue(f(self.A, "Comité Alfa | Microsoft Teams"))  # sin hora (Teams): basta el asunto
        self.assertFalse(f(self.A, "Comité Alfa", "2026-11-09T10:30"))  # la misma reunión, a otra hora: es otra
        self.assertFalse(f(self.A, "Revisión", "2026-11-09T09:30"))
        self.assertFalse(f([{"asunto": "", "inicio": None}, "rara"], "Comité Alfa"))
        self.assertTrue(f([{"asunto": "Comité Alfa", "inicio": None}], "Comité Alfa", "2026-11-09T10:30"))
        # sin hora en ninguno de los dos, el mismo nombre: «Daily» no tapa «Daily de producto» (revisión 2, P2)
        self.assertFalse(f([{"asunto": "Daily", "inicio": None}], "Daily de producto"))
        self.assertTrue(f([{"asunto": "Daily", "inicio": None}], "daily"))
        self.assertTrue(f([{"asunto": "Daily", "inicio": None}], "Daily de producto", "2026-11-09T10:30"))

    def test_sin_la_grabada_en_teams_en_la_lectura_y_en_la_agenda(self):
        citas = [publica("Comité Alfa", NOW - timedelta(minutes=60)),  # 9:30-10:30: la grabada
                 publica("Revisión", NOW - timedelta(minutes=2))]  # 10:28: ya ha empezado
        # Teams aún enseña la grabada y el vigilante la leyó con su cita: las dos fuera, queda la de la agenda
        lect = {"teams": {"title": "Comité Alfa"}, "outlook": citas[0],
                "sugerencia": {"name": "Comité Alfa", "source": "outlook", "participants": ["Ana"]}}
        sug, info = calendario.reunion_de_ahora(lect, citas, LISTAS, NOW, excluir=self.A)
        self.assertEqual((sug["name"], sug["source"], sug["participants"]), ("Revisión", "outlook", []))
        self.assertEqual((info["titulo"], info["teams"], info["inicio"], info["en_curso"]),
                         ("Revisión", False, "2026-11-09T10:28", True))
        # sin excluir, la de siempre (no cambia nada para la tarjeta ni Ctrl+Alt+R)
        self.assertEqual(calendario.reunion_de_ahora(lect, citas, LISTAS, NOW)[1]["titulo"], "Comité Alfa")
        # la siguiente ya en Teams: manda su ventana, como siempre
        lect = {"teams": {"title": "Revisión"}, "sugerencia": {"name": "Revisión", "source": "teams"}}
        _, info = calendario.reunion_de_ahora(lect, citas, LISTAS, NOW, excluir=self.A)
        self.assertEqual((info["titulo"], info["teams"], info["inicio"]), ("Revisión", True, "2026-11-09T10:28"))

    def test_dos_seguidas_con_el_mismo_asunto(self):
        a = [{"asunto": "Bloque", "inicio": "2026-11-09T09:30"}]
        citas = [publica("Bloque", NOW - timedelta(minutes=60)), publica("Bloque", NOW - timedelta(minutes=1))]
        _, info = calendario.reunion_de_ahora({}, citas, [], NOW, excluir=a)
        self.assertEqual(info["inicio"], "2026-11-09T10:29")
        # solo con Teams (sin hora) el mismo asunto no se puede distinguir: no se propone (del lado seguro)
        lect = {"teams": {"title": "Bloque"}, "sugerencia": {"name": "Bloque", "source": "teams"}}
        self.assertEqual(calendario.reunion_de_ahora(lect, [], [], NOW, excluir=[{"asunto": "Bloque"}]), ({}, None))


class SiguienteServidorTests(TempTree):
    """Al parar una grabación: la propuesta en /api/rec/status y /api/hoy, «Grabar», «Ahora no», cuándo se va y el aviso
    flotante. Con la agenda de ServidorTests y el reloj de verdad (las citas se ponen alrededor de ahora)."""
    app_base = ServidorTests.app

    def setUp(self):
        super().setUp()
        self.write("Alfa/Vocabulario/General.txt", "comité, API\n")
        self.lect = {}

    def app(self, **st):
        a = self.app_base(**st)
        now = datetime.now().replace(second=0, microsecond=0)
        self.citas = [publica("Comité de seguimiento", now - timedelta(minutes=58)),  # la grabada, acaba en 2 min
                      publica("Revisión de presupuesto", now - timedelta(minutes=3), 45)]
        a.agenda.refrescar()
        a.grabada, a.siguiente, a._siguiente_cache = [], None, (0.0, None)
        a.parada_auto, a.meeting_prev = None, None
        a.fin_llamada = types.SimpleNamespace(quedan=lambda t: None)
        self.lect = {"teams": {"title": "Comité de seguimiento"},
                     "sugerencia": {"name": "Comité de seguimiento", "source": "teams"}}
        self.logs = []
        p = mock.patch.object(server, "log", self.logs.append)
        p.start()
        self.addCleanup(p.stop)
        return a

    def grabar_y_parar(self, a):
        """Lo que hacen rec_start y rec_stop con la grabadora de mentira: se graba «Comité de seguimiento» desde su
        llamada de Teams (la lectura de ahora vuelve a ser la de antes al parar)."""
        antes, self.lect = self.lect, {"teams": {"title": "Comité de seguimiento"},
                                       "sugerencia": {"name": "Comité de seguimiento", "source": "teams"}}
        a.grabada = a._que_se_graba("Comité de seguimiento")
        self.lect = antes
        rec = types.SimpleNamespace(alive=lambda: True, path=os.path.join(self.tmp, "x.wav"), stop=lambda: 60.0,
                                    timeline=lambda: None)
        a.rec, a._movs = rec, lambda: {}
        a.rec_stop({})
        self.assertIsNone(a.rec)

    def test_propone_la_que_ya_ha_empezado(self):
        a = self.app(outlook_on=True)
        self.assertIsNone(a.rec_status({}).get("siguiente"))  # sin parar nada, nada
        self.grabar_y_parar(a)
        self.assertEqual(a.grabada[0]["asunto"], "Comité de seguimiento")
        self.lect = {}  # colgó: ya no hay ventana de Teams
        g = a.rec_status({})["siguiente"]
        self.assertEqual((g["titulo"], g["teams"]), ("Revisión de presupuesto", False))
        self.assertNotIn("tema", g)  # el proyecto lo dice el plan al grabar, no una sugerencia que puede no ser él
        self.assertEqual(g["inicio"], self.citas[1]["start"])
        self.assertEqual(a.hoy({})["siguiente"], g)  # también en Hoy
        self.assertIn("   siguiente reunión: propuesta (empezó hace 3 min)", self.logs)
        self.assertEqual(server.diag_line(self.logs[-1]), self.logs[-1])  # en «Copiar diagnóstico», entera
        # Teams todavía con la ventana de la grabada: no cambia nada
        self.lect = {"teams": {"title": "Comité de seguimiento"},
                     "sugerencia": {"name": "Comité de seguimiento", "source": "teams"}}
        self.assertEqual(a._siguiente_pub(fresco=True)["titulo"], "Revisión de presupuesto")

    def test_nada_si_no_hay_otra_o_sin_outlook(self):
        a = self.app(outlook_on=True)
        self.citas.pop()
        a.agenda.refrescar()
        self.grabar_y_parar(a)
        self.assertIsNone(a.rec_status({})["siguiente"])  # solo la grabada
        a = self.app(outlook_on=False)  # calendario apagado y sin Teams: nada, sin errores ni avisos
        self.grabar_y_parar(a)
        self.lect = {}
        self.assertIsNone(a.rec_status({})["siguiente"])
        self.assertFalse([x for x in self.logs if "siguiente" in x])
        # la que empieza dentro de 5 min aún no ha empezado: no se propone
        a = self.app(outlook_on=True)
        self.citas[1] = publica("Revisión de presupuesto", datetime.now() + timedelta(minutes=5))
        a.agenda.refrescar()
        self.grabar_y_parar(a)
        self.lect = {}
        self.assertIsNone(a.rec_status({})["siguiente"])

    def test_lo_grabado_con_otro_nombre_no_tapa_la_cita_en_curso(self):
        """Grabar «Llamada imprevista» (escrito a mano) con «Revisión de presupuesto» en curso en el calendario: al
        parar, se propone la cita (no era la grabada). Con el nombre de la cita, la cita y su hora."""
        a = self.app(outlook_on=True)
        self.lect = {}  # sin Teams: la reunión de ahora es la cita en curso
        a.siguiente = None
        self.assertEqual(a._que_se_graba("Llamada imprevista"), [{"asunto": "Llamada imprevista", "inicio": None}])
        quien = a._reunion_ahora()[1]
        self.assertEqual(a._que_se_graba(quien["titulo"]), [{"asunto": quien["titulo"], "inicio": quien["inicio"]}])
        self.assertEqual(a._que_se_graba(""), [{"asunto": quien["titulo"], "inicio": quien["inicio"]}])
        a.grabada = a._que_se_graba("Llamada imprevista")
        a.rec, a._movs = types.SimpleNamespace(alive=lambda: True, path=os.path.join(self.tmp, "x.wav"),
                                               stop=lambda: 60.0, timeline=lambda: None), lambda: {}
        a.rec_stop({})
        self.assertEqual(a.rec_status({})["siguiente"]["titulo"], quien["titulo"])

    def test_con_avisarme_si_estoy_en_una_llamada_apagado_nada(self):
        a = self.app(outlook_on=True, meeting_detect=False)
        self.grabar_y_parar(a)
        self.lect = {"teams": {"title": "Otra en Teams"}, "sugerencia": {"name": "Otra en Teams", "source": "teams"}}
        self.assertIsNone(a._siguiente_pub(fresco=True))

    def test_teams_sin_outlook(self):
        a = self.app(outlook_on=False)
        self.grabar_y_parar(a)
        self.assertEqual(a.grabada, [{"asunto": "Comité de seguimiento", "inicio": None}])
        self.assertIsNone(a.rec_status({})["siguiente"])  # Teams sigue en la grabada
        self.lect = {"teams": {"title": "Revisión"}, "sugerencia": {"name": "Revisión", "source": "teams"}}
        g = a._siguiente_pub(fresco=True)
        self.assertEqual((g["titulo"], g["teams"], g["inicio"]), ("Revisión", True, None))
        self.assertIn("   siguiente reunión: propuesta (en Teams)", self.logs)

    def test_ahora_no_otra_grabacion_y_cuando_acaba(self):
        a = self.app(outlook_on=True)
        self.grabar_y_parar(a)
        self.lect = {}
        a.vigia.tipo, cerrado = "siguiente", []
        a.vigia.cerrar_aviso = lambda: cerrado.append(1)
        self.assertTrue(a.rec_status({})["siguiente"])
        a.rec_siguiente({"descartar": True})
        self.assertIsNone(a.rec_status({})["siguiente"])
        self.assertEqual(cerrado, [1])  # y el flotante de la misma propuesta se cierra
        self.assertIsNone(a.hoy({})["siguiente"])
        # otra parada la vuelve a proponer; grabando, no; empezar otra grabación la quita
        self.grabar_y_parar(a)
        self.assertTrue(a._siguiente_pub(fresco=True))
        a.rec = types.SimpleNamespace(alive=lambda: True)
        self.assertIsNone(a._siguiente_pub(fresco=True))
        a.rec = None
        a.siguiente = None  # lo que hace rec_start al empezar
        self.assertIsNone(a._siguiente_pub(fresco=True))
        # cuando la cita acaba, se va; y pasados los 10 min sin nada que proponer, ya no se propone nada
        self.grabar_y_parar(a)
        fin = datetime.fromisoformat(self.citas[1]["end"])
        self.assertIsNone(a._siguiente(now=fin + timedelta(minutes=1)))
        self.grabar_y_parar(a)
        a.siguiente["hasta"] = time.time() - 1
        self.assertIsNone(a._siguiente_pub(fresco=True))
        # en su llamada de Teams pasada la hora de fin de la cita: también se va (la cita ha acabado)
        self.grabar_y_parar(a)
        a.siguiente["hasta"] = fin.timestamp() + 3600
        self.lect = {"teams": {"title": "Revisión de presupuesto"},
                     "sugerencia": {"name": "Revisión de presupuesto", "source": "teams"}}
        self.assertTrue(a._siguiente())
        with mock.patch.object(server.time, "time", lambda: fin.timestamp() + 60):
            self.assertIsNone(a._siguiente())
        self.lect = {}
        # vista una vez, se queda mientras dure la reunión (más allá de los 10 min)
        self.grabar_y_parar(a)
        self.assertTrue(a._siguiente_pub(fresco=True))
        self.assertGreaterEqual(a.siguiente["hasta"], fin.timestamp())

    def test_grabar_usa_el_plan_de_esa_reunion_con_lo_preparado(self):
        a = self.app(outlook_on=True)
        c = self.citas[1]
        a.hoy_preparar({"asunto": c["subject"], "inicio": c["start"], "fin": c["end"], "idioma": "pt",
                        "contexto": "Presupuesto anual"})
        self.grabar_y_parar(a)
        self.lect = {}
        vistos = []
        a.rec_start = lambda b: vistos.append(b) or {"path": "y.wav", "workstream": None}
        with self.assertRaises(server.ApiError):  # lo que se ve tiene que ser lo que se graba
            a.rec_siguiente({"grabar": "Otra reunión"})
        self.assertEqual(vistos, [])
        with mock.patch.object(server, "cue") as cue, mock.patch.object(server, "notify") as notify:
            r = a.rec_siguiente({"grabar": "Revisión de presupuesto"})
            self.assertFalse(cue.called or notify.called)  # desde la app no hay pitido ni aviso de Windows
        self.assertTrue(r["recording"])
        b = vistos[0]
        self.assertEqual((b["name"], b["language"], b["contexto"]), ("Revisión de presupuesto", "pt",
                                                                     "Presupuesto anual"))
        # el mismo plan que la tarjeta de Hoy daría para esa reunión
        sug = a._siguiente()[0]
        self.assertEqual({k: v for k, v in b.items() if k != "resumen"}, a.plan_grabar({}, sug=sug)["rec"])
        self.assertIn("   siguiente reunión: grabar desde la app", self.logs)
        # desde el aviso, con su pitido y su aviso; si ya se graba, no se toca
        with mock.patch.object(server, "cue") as cue, mock.patch.object(server, "notify"):
            a.rec_siguiente({"grabar": "Revisión de presupuesto", "origen": "aviso"})
            self.assertEqual(cue.call_args[0], ("start",))
        a.rec = types.SimpleNamespace(alive=lambda: True, path=os.path.join(self.tmp, "z.wav"))
        self.assertTrue(a.rec_siguiente({"grabar": "Revisión de presupuesto", "origen": "aviso"})["recording"])
        with self.assertRaises(server.ApiError):
            a.rec_siguiente({"grabar": "Revisión de presupuesto"})
        self.assertEqual(len(vistos), 2)

    def test_aviso_flotante(self):
        a = self.app(outlook_on=True)
        abiertos = []
        a.vigia.abierto = lambda: False
        a.vigia.abrir_aviso = lambda *x, **k: abiertos.append(k) or True
        st = a.settings.get()
        self.assertFalse(a._siguiente_aviso("Teams", st))  # sin parar nada
        self.grabar_y_parar(a)
        self.lect = {}
        self.assertFalse(a._siguiente_aviso("Teams", dict(st, rec_aviso=False)))
        self.assertFalse(a._siguiente_aviso("Teams", dict(st, meeting_detect=False)))
        self.assertTrue(a._siguiente_aviso("Teams", st))
        k = abiertos[0]
        self.assertEqual((k["siguiente"], k["titulo"]), ("Revisión de presupuesto", "Ya ha empezado «Revisión de "
                                                                                    "presupuesto»"))
        self.assertTrue(k["sub"].endswith(" · Pulsa Grabar para tomar notas"), k["sub"])
        self.assertEqual(a.vigia.avisado, "Teams")  # al cerrarse, no sale además el «● Grabar» de esa llamada
        self.assertFalse(a._siguiente_aviso("Teams", st))  # una vez por parada
        self.assertEqual(len(abiertos), 1)
        self.assertIn("   siguiente reunión: aviso flotante abierto", self.logs)

    def test_vigia_no_cierra_el_aviso_de_la_siguiente_sin_llamada(self):
        import test_reunion
        FakeProc = test_reunion.FakeProc
        FakeProc.spawned = []
        with mock.patch.object(calendario.subprocess, "Popen", FakeProc):
            v = calendario.Vigia()
            self.assertTrue(v.abrir_aviso(8765, "/app", siguiente="Revisión", titulo="Ya ha empezado «Revisión»",
                                          sub="10:00–11:00 · Pulsa Grabar para tomar notas", tema="Alfa · General"))
            cmd = FakeProc.spawned[0].cmd
            self.assertEqual(cmd[cmd.index("--siguiente") + 1], "Revisión")
            self.assertEqual(cmd[cmd.index("--sub") + 1], "10:00–11:00 · Pulsa Grabar para tomar notas")
            self.assertNotIn("--nombre", cmd)
            self.assertFalse(v.tic(None, False, {}, lambda: {}, 8765, "/app"))  # sin llamada: sigue abierto
            self.assertTrue(FakeProc.spawned[0].alive)
            self.assertFalse(v.tic(None, True, {}, lambda: {}, 8765, "/app"))  # al grabar, se cierra
            self.assertFalse(FakeProc.spawned[0].alive)
            self.assertTrue(v.tic("Teams", False, {}, lambda: {}, 8765, "/app"))  # el de la llamada, como antes
            self.assertEqual(v.tipo, "llamada")
            self.assertFalse(v.tic(None, False, {}, lambda: {}, 8765, "/app"))
            self.assertFalse(FakeProc.spawned[1].alive)

    def test_las_lineas_del_log_salen_en_el_diagnostico(self):
        """Las de la siguiente reunión y las del fin de la llamada (directo-avisos), con su texto y sin asuntos."""
        for linea in ("   siguiente reunión: propuesta (empezó hace 3 min)",
                      "   siguiente reunión: propuesta (en Teams)",
                      "   siguiente reunión: aviso flotante abierto", "   siguiente reunión: «Ahora no»",
                      "   siguiente reunión: grabar desde la app", "   siguiente reunión: grabar desde el aviso",
                      "   siguiente reunión: ya no está en curso",
                      "   la llamada de Teams ha terminado: se para la grabación en 30 s si no se pide seguir",
                      "   la llamada sigue (o no se puede saber): no se para la grabación",
                      "   «No terminar»: la grabación sigue aunque la llamada haya terminado"):
            self.assertEqual(server.diag_line(linea), linea)
        self.assertEqual(server.diag_line("■ Clientes Omega/Junta.wav: parada al terminar la llamada de Teams (sin "
                                          "«No terminar» en 30 s)"),
                         "■ <texto>: parada al terminar la llamada de Teams (sin «No terminar» en 30 s)")


@unittest.skipUnless(shutil.which("node"), "node no instalado")
class HoyJsTests(unittest.TestCase):
    def node(self, code):
        with open(os.path.join(STATIC, "hoy.js"), encoding="utf-8") as f:
            src = f.read()
        # en un fichero y no con `node -e`: en Windows la línea de órdenes no pasa de 32 767 caracteres y hoy.js ya no
        # cabía (WinError 206 en tests de Windows, run 42716635016 del 09/11/2026)
        d = tempfile.mkdtemp(prefix="tenca-hoyjs-")
        self.addCleanup(shutil.rmtree, d, True)
        js = os.path.join(d, "prueba.js")
        with open(js, "w", encoding="utf-8") as f:
            f.write(JS_STUBS + src + "\n" + code)
        r = subprocess.run(["node", js], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_dias(self):
        out = self.node("""const now = new Date(2026, 10, 9, 10, 30);  // lunes 9 nov
          const dia = d => hoyDiaTxt(new Date(2026, 10, 9 + d, 23, 59), now, d > 1);
          console.log(JSON.stringify([0, -1, 1, -2, 3, 9].map(dia)
            .concat([hoyFechaLarga(now), hoyFaltan(8), hoyFaltan(75), hoyFaltan(120), hoyFaltan(0.5)])));""")
        self.assertEqual(out, ["Hoy", "Ayer", "Mañana", "Sáb 7", "Jue 12 nov", "Mié 18 nov", "lunes 9 de noviembre",
                               "en 8 min", "en 1 h 15 min", "en 2 h", "ya"])

    def test_para_revisar(self):
        out = self.node("""const now = new Date(2026, 10, 9, 10, 30);
          S.jobs = [{path: 'c', activo: true}, {path: 'f', kind: 'summary', status: 'error'}];
          const A = [
            {path: 'a', date: '2026-11-09T09:00:00', st: 'pending', props: {workstream: 'General'}, dur: 120},
            {path: 'b', date: '2026-11-08T09:00:00', st: 'done', has_summary: true, props: {workstream: 'G'},
             speakers: {sin_nombre: 2}},
            {path: 'c', date: '2026-11-09T08:00:00', st: 'pending', props: {}},
            {path: 'd', date: '2026-11-07T09:00:00', st: 'done', props: {}},
            {path: 'e', date: '2026-11-06T09:00:00', st: 'error', props: {}},
            {path: 'f', date: '2026-11-05T09:00:00', st: 'done', props: {workstream: 'G'}},
            {path: 'g', date: '2026-11-04T09:00:00', st: 'done', props: {workstream: 'G'}, has_summary: true},
            {path: 'h', date: '2026-10-01T09:00:00', st: 'pending', props: {}},
            {path: 'i', date: '2026-11-09T07:00:00', st: 'partial', tx_end: 125, props: {workstream: 'G'}},
            {path: 'j', date: '2026-11-09T06:00:00', st: 'pending', props: {}, live: true}];
          const fila = r => [r.a.path, r.accion.k, r.motivos.map(m => m.texto).join(' · ')];
          console.log(JSON.stringify(hoyRevisar(A, now).map(fila)));""")
        self.assertEqual(out, [
            ["a", "transcribir", "Grabada · sin transcribir (2 min)"],
            ["i", "continuar", "La transcripción se cortó en 2:05"],
            ["b", "voces", "Resumen listo · 2 voces sin nombre"],
            ["d", "proyecto", "¿De qué proyecto es?"],
            ["e", "transcribir", "No se pudo transcribir · ¿De qué proyecto es?"],
            ["f", "resumir", "No se pudo resumir"],
        ])  # c (en la cola), g (nada que hacer), h (hace más de 30 días) y j (en directo) no salen

    def test_esta_semana_y_agenda(self):
        out = self.node("""const now = new Date(2026, 10, 11, 10, 30);  // miércoles
          const A = [{path: 'lun', date: '2026-11-09T09:00:00'}, {path: 'mie', date: '2026-11-11T09:00:00'},
            {path: 'mie2', date: '2026-11-11T08:00:00'}, {path: 'dom', date: '2026-11-08T09:00:00'},
            {path: 'fuera', date: '2026-11-10T09:00:00'}];
          const sem = hoySemana(A, now, new Set(['fuera'])).map(d => [d.dia, d.audios.map(a => a.path)]);
          const citas = [{subject: 'Ya pasó', start: '2026-11-11T08:00', end: '2026-11-11T09:00'},
            {subject: 'Ahora', start: '2026-11-11T10:00', end: '2026-11-11T11:00'},
            {subject: 'Mañana', start: '2026-11-12T09:00', end: '2026-11-12T10:00'},
            {subject: 'Rota', start: 'x', end: 'y'},
            {subject: 'Luego', start: '2026-11-20T09:00', end: '2026-11-20T10:00'}];
          const ag = hoyAgendaDias(citas, now, {inicio: '2026-11-11T10:00', titulo: 'Ahora'})
            .map(d => [d.dia, d.citas.map(c => c.subject + (c.ahora ? '*' : ''))]);
          console.log(JSON.stringify([sem, ag]));""")
        self.assertEqual(out, [[["Hoy", ["mie", "mie2"]], ["Lun 9", ["lun"]]],
                               [["Hoy", ["Ahora*"]], ["Mañana", ["Mañana"]], ["Vie 20 nov", ["Luego"]]]])

    def test_siguiente_texto(self):
        out = self.node("""console.log(JSON.stringify([
            hoySiguienteTxt({titulo: 'Revisión', inicio: '2026-11-09T10:00', fin: '2026-11-09T11:00', teams: false}),
            hoySiguienteTxt({titulo: 'Revisión', inicio: null, fin: null, teams: true}),
            hoySiguienteTxt({titulo: 'Revisión', inicio: null, fin: null, teams: false})]));""")
        self.assertEqual(out, ["Ya ha empezado «Revisión» · 10:00–11:00", "Ya ha empezado «Revisión» en Teams",
                               "Ya ha empezado «Revisión»"])

    def test_la_carga_y_los_ganchos(self):
        html = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read()
        app = open(os.path.join(STATIC, "app.js"), encoding="utf-8").read()
        self.assertLess(html.index('<script src="/app.js">'), html.index('<script src="/hoy.js">'))
        self.assertIn('<link rel="stylesheet" href="/hoy.css">', html)
        self.assertIn("if (typeof viewHoy === 'function') return viewHoy();", app)
        self.assertIn("</svg>Hoy</a>", html)
        self.assertIn("if (typeof renderSiguiente === 'function') renderSiguiente();", app)


if __name__ == "__main__":
    unittest.main()
