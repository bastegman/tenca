# -*- coding: utf-8 -*-
"""Reunión en curso (calendario.py): título de la ventana de Teams, cita de Outlook (con un objeto COM falso),
sugerencia de nombre/tema/participantes, decisiones del vigilante (aviso flotante) y la ruta /api/reunion/actual.
Nada de aquí necesita Windows ni pywin32."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from support import orden_de_arranque, server  # noqa: F401  (prepara sys.path con _app)
import aviso  # noqa: E402  (solo sus funciones puras: tkinter se importa dentro de la clase)
import calendario  # noqa: E402


class ParseTitleTests(unittest.TestCase):
    def test_reunion_con_sufijo(self):
        self.assertEqual(calendario.parse_title("Reunión semanal | Microsoft Teams"), "Reunión semanal")
        self.assertEqual(calendario.parse_title("  Kick-off   Alfa - Microsoft Teams "), "Kick-off Alfa")
        self.assertEqual(calendario.parse_title("Daily – Microsoft Teams"), "Daily")
        self.assertEqual(calendario.parse_title("Comité | Microsoft Teams (trabajo o escuela)"), "Comité")
        self.assertEqual(calendario.parse_title("Comité | Microsoft Teams classic"), "Comité")
        self.assertEqual(calendario.parse_title("(2) Comité | Microsoft Teams"), "Comité")
        self.assertEqual(calendario.parse_title("Daily - Equipo A | Microsoft Teams"), "Daily - Equipo A")

    def test_ventanas_principales_descartadas(self):
        for t in ("Microsoft Teams", "(3) Microsoft Teams", "Chat | Ana García | Microsoft Teams",
                  "Calendario | Microsoft Teams", "Calendar | Microsoft Teams", "Actividad | Microsoft Teams",
                  "Activity | Microsoft Teams", "Equipos | Microsoft Teams", "Teams | Microsoft Teams",
                  "Llamadas | Microsoft Teams", "Calls | Microsoft Teams", "Atividade | Microsoft Teams",
                  "Chamadas | Microsoft Teams", "Calendário | Microsoft Teams", "Equipes | Microsoft Teams",
                  "Archivos | Microsoft Teams", "Más | Microsoft Teams", "Copilot | Microsoft Teams"):
            self.assertIsNone(calendario.parse_title(t), t)

    def test_sin_sufijo_no_es_reunion(self):
        for t in ("", None, "Reunión semanal", "Teams sync", "Bloc de notas", "Microsoft Teams Notification"):
            self.assertIsNone(calendario.parse_title(t), repr(t))

    def test_una_reunion_que_se_llama_como_una_pestana_pero_con_mas_texto(self):
        self.assertEqual(calendario.parse_title("Teams sync | Microsoft Teams"), "Teams sync")
        self.assertEqual(calendario.parse_title("Chat con cliente | Microsoft Teams"), "Chat con cliente")


class PickWindowTests(unittest.TestCase):
    def test_primera_en_orden_z_y_solo_procesos_de_teams(self):
        wins = [("Notas", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", 1),
                ("Reunión semanal | Microsoft Teams", r"C:\x\notepad.exe", 2),  # otro proceso con ese título: no
                ("Chat | Microsoft Teams", r"C:\Program Files\WindowsApps\MSTeams_8wekyb3d8bbwe\ms-teams.exe", 3),
                ("Comité de dirección | Microsoft Teams", r"C:\Program Files\WindowsApps\x\MS-TEAMS.EXE", 4),
                ("Otra reunión | Microsoft Teams", r"C:\Users\u\AppData\Local\Microsoft\Teams\current\Teams.exe", 5)]
        self.assertEqual(calendario.pick_meeting_window(wins), "Comité de dirección")
        self.assertEqual(calendario.pick_meeting_window(wins, exe_names=("notepad.exe",)), "Reunión semanal")
        self.assertIsNone(calendario.pick_meeting_window(wins[:3]))
        self.assertIsNone(calendario.pick_meeting_window([]))

    def test_fuera_de_windows_devuelve_none(self):
        if calendario.WINDOWS:
            self.skipTest("en Windows enumera ventanas reales (tests/test_reunion_windows.py)")
        self.assertIsNone(calendario.teams_meeting_title())


# --- doble del modelo de objetos de Outlook (lo justo que usa calendario._consulta)

class FakeRecipient:
    def __init__(self, name, type_):
        self.Name, self.Type = name, type_


class FakeRecipients:
    def __init__(self, recs):
        self._recs = recs

    @property
    def Count(self):
        return len(self._recs)

    def Item(self, i):
        return self._recs[i - 1]


class FakeItem:
    def __init__(self, subject, start, end, organizer="Ana Organizadora", required=(), optional=(), categories="",
                 location="Reunión de Microsoft Teams", body="", all_day=False, status=1, recurring=False,
                 blocked=()):
        self.Subject, self.Start, self.End, self.Organizer = subject, start, end, organizer
        self.RequiredAttendees, self.OptionalAttendees = "; ".join(required), "; ".join(optional)
        self.Recipients = FakeRecipients([FakeRecipient(organizer, 0)] + [FakeRecipient(r, 1) for r in required]
                                         + [FakeRecipient(o, 2) for o in optional] + [FakeRecipient("Sala 3", 3)])
        self.Categories, self.Location, self.Body = categories, location, body
        self.AllDayEvent, self.MeetingStatus, self.IsRecurring = all_day, status, recurring
        self._blocked = set(blocked)

    def __getattribute__(self, name):
        if not name.startswith("_") and name in object.__getattribute__(self, "_blocked"):
            raise RuntimeError(f"Outlook bloquea {name} (aviso de seguridad)")
        return object.__getattribute__(self, name)


class FakeItems:
    def __init__(self, items, log):
        self._items, self._log, self._i = items, log, 0
        self._include = False

    def Sort(self, prop, desc=False):
        self._log.append(("Sort", prop))
        self._items.sort(key=lambda it: it.Start)

    @property
    def IncludeRecurrences(self):
        return self._include

    @IncludeRecurrences.setter
    def IncludeRecurrences(self, v):
        self._log.append(("IncludeRecurrences", v))
        self._include = v

    def Restrict(self, flt):
        self._log.append(("Restrict", flt))
        return FakeItems(list(self._items), self._log)

    def GetFirst(self):
        self._i = 0
        return self.GetNext()

    def GetNext(self):
        if self._i >= len(self._items):
            return None
        it = self._items[self._i]
        self._i += 1
        return it

    @property
    def Count(self):
        raise AssertionError("con IncludeRecurrences el Count no es fiable: no se usa")


class FakeFolder:
    def __init__(self, items):
        self.Items = items


class FakeNamespace:
    def __init__(self, items, log):
        self._items, self._log = items, log

    def GetDefaultFolder(self, n):
        self._log.append(("GetDefaultFolder", n))
        return FakeFolder(FakeItems(list(self._items), self._log))


class FakeApp:
    def __init__(self, items, log):
        self._items, self._log = items, log

    def GetNamespace(self, name):
        self._log.append(("GetNamespace", name))
        return FakeNamespace(self._items, self._log)


class ComError(Exception):
    def __init__(self, hresult, text="com"):
        super().__init__(text)
        self.hresult, self.strerror = hresult, text


NOW = datetime(2026, 10, 31, 10, 0)
FMT = "%d/%m/%Y %H:%M"


def reset_calendario():
    """Lo que calendario recuerda entre consultas (si Outlook deja leer los asistentes, la directiva, el estado)."""
    calendario.ASISTENTES["estado"] = None
    calendario._ESTADO_CACHE.clear()
    calendario._POLITICA[:] = []


def consulta(items, now=NOW, **kw):
    log = []
    fake = FakeApp(items, log)
    out = calendario.outlook_candidates(now=now, session=lambda: fake, fmt=lambda d: d.strftime(FMT), **kw)
    return out, log


class OutlookTests(unittest.TestCase):
    def setUp(self):
        reset_calendario()
        calendario.ASISTENTES["estado"] = "si"  # Outlook deja leer los asistentes (antivirus aceptado, sin directiva)

    def test_orden_de_llamadas_y_filtro_sin_segundos(self):
        (cands, reason), log = consulta([FakeItem("Semanal", NOW - timedelta(minutes=20), NOW + timedelta(minutes=40))])
        self.assertIsNone(reason)
        self.assertEqual([k for k, _ in log], ["GetNamespace", "GetDefaultFolder", "Sort", "IncludeRecurrences",
                                               "Restrict"])
        self.assertEqual(log[1], ("GetDefaultFolder", 9))
        self.assertEqual(log[2], ("Sort", "[Start]"))
        self.assertEqual(log[3], ("IncludeRecurrences", True))
        # inicio <= ahora+10 min y fin >= ahora-5 min
        self.assertEqual(log[4][1], "[Start] <= '31/10/2026 10:10' AND [End] >= '31/10/2026 09:55'")
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["subject"], "Semanal")

    def test_fecha_jet_sin_segundos(self):
        s = calendario.fecha_jet(datetime(2026, 10, 31, 9, 5, 42))
        self.assertNotIn(":42", s)
        self.assertRegex(s, r"\d")

    def test_campos_de_la_cita(self):
        it = FakeItem("Kick-off   Alfa ", NOW - timedelta(minutes=5), NOW + timedelta(minutes=55),
                      organizer="Ana Organizadora", required=("Luis Pérez", "externo@proveedor.com", "Luis Pérez"),
                      optional=("Marta Ruiz <marta@x.com>",), categories="Proyecto Alfa; Cliente", recurring=True,
                      location="Sala 2; Microsoft Teams Meeting")
        (cands, _), _ = consulta([it])
        m = cands[0]
        self.assertEqual(m["subject"], "Kick-off Alfa")
        self.assertEqual(m["organizer"], "Ana Organizadora")
        self.assertEqual(m["attendees"], ["Luis Pérez", "Marta Ruiz"])  # sin correos, sin repetidos, sin recursos
        self.assertEqual(m["categories"], ["Proyecto Alfa", "Cliente"])
        self.assertEqual((m["start"], m["end"]), ("2026-10-31T09:55", "2026-10-31T10:55"))
        self.assertTrue(m["is_teams"] and m["is_now"] and m["recurring"])
        self.assertEqual(m["blocked"], [])
        self.assertFalse(any(k.startswith("_") for k in calendario.outlook_current_meeting(
            now=NOW, session=lambda: FakeApp([it], []), fmt=lambda d: d.strftime(FMT))[0]))

    def test_teams_por_el_cuerpo_y_sin_teams(self):
        # el cuerpo está protegido (Object Model Guard): solo se lee de la cita elegida, y solo si el lugar no lo dice
        a = FakeItem("A", NOW, NOW + timedelta(minutes=30), location="Sala 4",
                     body="Únete: https://teams.microsoft.com/l/meetup-join/19%3ameeting_x")
        b = FakeItem("B", NOW, NOW + timedelta(minutes=30), location="Sala 4", body="sin enlace")
        (cands, _), _ = consulta([a, b])
        self.assertEqual([m["is_teams"] for m in cands], [True, False])
        (cands, _), _ = consulta([b, a])  # la elegida es B (la primera en empate): A no se mira
        self.assertEqual([m["is_teams"] for m in cands], [False, False])

    def test_fuera_quedan_todo_el_dia_canceladas_y_fuera_de_hora(self):
        items = [FakeItem("Todo el día", NOW.replace(hour=0), NOW.replace(hour=23, minute=59), all_day=True),
                 FakeItem("Cancelada", NOW, NOW + timedelta(minutes=30), status=5),
                 FakeItem("Pasada", NOW - timedelta(hours=2), NOW - timedelta(hours=1)),
                 FakeItem("Futura", NOW + timedelta(minutes=11), NOW + timedelta(hours=1)),
                 FakeItem("Ahora", NOW - timedelta(minutes=10), NOW + timedelta(minutes=20))]
        (cands, _), _ = consulta(items)
        self.assertEqual([m["subject"] for m in cands], ["Ahora"])

    def test_propiedades_bloqueadas_no_rompen(self):
        it = FakeItem("Privada", NOW, NOW + timedelta(minutes=30), blocked=("Recipients", "RequiredAttendees",
                                                                            "OptionalAttendees", "Body"),
                      location="Despacho")
        (cands, reason), _ = consulta([it])
        self.assertIsNone(reason)
        self.assertEqual(cands[0]["attendees"], [])
        self.assertEqual(cands[0]["organizer"], "Ana Organizadora")
        # al primer fallo se para (cada intento podría ser otro aviso de Outlook): el cuerpo ni se toca
        self.assertEqual(cands[0]["blocked"], ["asistentes"])
        self.assertEqual(calendario.ASISTENTES["estado"], "deniega")

    def test_sin_pywin32(self):
        def session():
            raise ImportError("No module named win32com")
        cands, reason = calendario.outlook_candidates(now=NOW, session=session, fmt=str)
        self.assertIsNone(cands)
        self.assertIn("pywin32", reason)

    def test_motivos_com(self):
        for hr, frag in ((-2147221021, "está cerrado"), (-2147221005, "no tiene el Outlook clásico"),
                         (-2147418111, "no responde"), (-2147417846, "no responde"),
                         (-2147024891, "no deja conectarse"), (-2147467260, "no deja conectarse"),
                         (-2147352567, "0x80020009"), (-2147319779, "repara Office")):
            def session(hr=hr):
                raise ComError(hr, "texto de Outlook")
            cands, reason = calendario.outlook_candidates(now=NOW, session=session, fmt=str)
            self.assertIsNone(cands)
            self.assertIn(frag, reason, reason)
        cands, reason = calendario.outlook_candidates(now=NOW, session=lambda: 1 / 0, fmt=str)
        self.assertIn("ZeroDivisionError", reason)

    def test_tope_de_tiempo_y_consulta_ocupada(self):
        gate = threading.Event()

        def session():
            gate.wait(5)
            return FakeApp([], [])
        t0 = time.time()
        cands, reason = calendario.outlook_candidates(now=NOW, session=session, fmt=str, timeout=0.3)
        self.assertIsNone(cands)
        self.assertIn("no ha respondido", reason)
        self.assertLess(time.time() - t0, 2)
        cands, reason = calendario.outlook_candidates(now=NOW, session=lambda: FakeApp([], []), fmt=str)
        self.assertIn("consulta anterior", reason)  # la colgada sigue viva: no se amontonan hilos
        gate.set()
        time.sleep(0.2)
        cands, reason = calendario.outlook_candidates(now=NOW, session=lambda: FakeApp([], []), fmt=str)
        self.assertEqual((cands, reason), ([], None))

    def test_fuera_de_windows_sin_doble(self):
        if calendario.WINDOWS:
            self.skipTest("en Windows intenta el COM real")
        cands, reason = calendario.outlook_candidates(now=NOW)
        self.assertIsNone(cands)
        self.assertIn("Windows", reason)

    def test_ninguna_a_esta_hora(self):
        m, reason = calendario.outlook_current_meeting(now=NOW, session=lambda: FakeApp([], []), fmt=str)
        self.assertIsNone(m)
        self.assertIn("ninguna reunión", reason)


class ElegirCitaTests(unittest.TestCase):
    def setUp(self):
        reset_calendario()

    def cands(self, now=NOW):
        items = [FakeItem("Termina ya", now - timedelta(minutes=55), now + timedelta(minutes=3)),
                 FakeItem("Empieza luego", now + timedelta(minutes=8), now + timedelta(minutes=60)),
                 FakeItem("Sala sin Teams", now - timedelta(minutes=55), now + timedelta(minutes=3), location="Sala")]
        (cands, _), _ = consulta(items, now)
        return cands

    def test_prefiere_la_que_esta_en_curso_y_con_teams(self):
        self.assertEqual(calendario.elegir_cita(self.cands())["subject"], "Termina ya")

    def test_si_no_hay_ninguna_en_curso_la_proxima(self):
        items = [FakeItem("Próxima", NOW + timedelta(minutes=5), NOW + timedelta(minutes=35)),
                 FakeItem("Más tarde", NOW + timedelta(minutes=9), NOW + timedelta(minutes=35))]
        (cands, _), _ = consulta(items)
        self.assertEqual(calendario.elegir_cita(cands)["subject"], "Próxima")

    def test_el_titulo_de_teams_decide(self):
        self.assertEqual(calendario.elegir_cita(self.cands(), "Empieza luego")["subject"], "Empieza luego")
        self.assertEqual(calendario.elegir_cita(self.cands(), "EMPIEZA LUEGO (Alfa)")["subject"], "Empieza luego")
        self.assertEqual(calendario.elegir_cita(self.cands(), "Otra cosa")["subject"], "Termina ya")
        self.assertIsNone(calendario.elegir_cita([], "x"))


VOCABS = [{"id": "Alfa/Vocabulario/General.txt", "project": "Alfa", "name": "General",
           "terms": ["Alfa", "SAP", "Luis Pérez", "comité"]},
          {"id": "Alfa/Vocabulario/Migración.txt", "project": "Alfa", "name": "Migración",
           "terms": ["Migración", "cutover", "SAP", "Luis Pérez", "Marta Ruiz"]},
          {"id": "Beta/Vocabulario/General.txt", "project": "Beta", "name": "General",
           "terms": ["Beta", "Azure", "API"]}]


class SuggestTests(unittest.TestCase):
    def test_tema_por_terminos_y_participantes(self):
        s = calendario.suggest({"subject": "Cutover plan SAP", "organizer": "Marta Ruiz",
                                "attendees": ["Luis Pérez", "marta ruiz", "Eva"], "categories": []}, VOCABS)
        self.assertEqual(s["vocab"], "Alfa/Vocabulario/Migración.txt")
        self.assertEqual(s["vocab_label"], "Alfa · Migración")
        self.assertEqual(s["name"], "Cutover plan SAP")
        self.assertEqual(s["participants"], ["Marta Ruiz", "Luis Pérez", "Eva"])  # organizador primero, sin repetir

    def test_nombre_de_proyecto_en_categoria(self):
        s = calendario.suggest({"subject": "Seguimiento", "categories": ["Beta"]}, VOCABS)
        self.assertEqual(s["vocab"], "Beta/Vocabulario/General.txt")

    def test_un_termino_suelto_no_basta(self):
        s = calendario.suggest({"subject": "Revisión de la API"}, VOCABS)
        self.assertEqual(s["vocab"], "")
        self.assertEqual(s["score"], 0)
        self.assertEqual(s["name"], "Revisión de la API")
        self.assertEqual(calendario.suggest({"subject": "Reunión general"}, VOCABS)["vocab"], "")  # «General» no cuenta

    def test_prefiere_el_tema_concreto_frente_a_general_en_empate(self):
        s = calendario.suggest({"subject": "Comité SAP"}, VOCABS)  # General: SAP+comité = 2; Migración: SAP = 1
        self.assertEqual(s["vocab"], "Alfa/Vocabulario/General.txt")
        s = calendario.suggest({"subject": "SAP con Luis Pérez"}, VOCABS)  # empate a 3: gana el tema concreto
        self.assertEqual(s["vocab"], "Alfa/Vocabulario/Migración.txt")

    # un proyecto con muchas listas en su carpeta, como las de un cliente con un workstream por área (16/11/2026)
    GAMMA = [{"id": f"Gamma/Vocabulario/{n}.txt", "project": "Gamma", "name": n, "terms": t} for n, t in (
        ("Business processes", ["order to cash", "maestro de materiales"]),
        ("HR and people", ["Workday", "nómina"]),
        ("Legacy (AS400)", ["AS400", "RPG", "iSeries"]),
        ("TSA y legal", ["TSA", "LTSA", "exit plan"]),
        ("General", ["Steerco", "PMO"]))]

    def test_el_nombre_de_la_lista_cuenta_por_palabras(self):
        """El nombre de la lista ya no tiene que salir entero en el asunto: sus palabras cuentan sueltas (y con un
        término de la lista llegan al umbral). Antes «AS400 decommissioning» y «TSA review» no daban nada."""
        def tema(asunto):
            return calendario.suggest({"subject": asunto}, self.GAMMA)["vocab"].split("/")[-1]
        self.assertEqual(tema("AS400 decommissioning"), "Legacy (AS400).txt")
        self.assertEqual(tema("TSA review"), "TSA y legal.txt")
        self.assertEqual(tema("Legal y TSA: exit plan"), "TSA y legal.txt")  # todas sus palabras, en otro orden
        self.assertEqual(tema("HR and people weekly"), "HR and people.txt")
        self.assertEqual(tema("Business processes"), "Business processes.txt")
        self.assertEqual(tema("HR sync"), "")  # una palabra del nombre sola no basta (sigue el umbral)
        self.assertEqual(tema("People HR review"), "HR and people.txt")  # todas sus palabras
        self.assertEqual(tema("people hr review"), "")  # una sigla de dos letras cuenta solo en mayúsculas
        self.assertEqual(calendario.suggest({"subject": "Review it, BI"}, [  # «it» no es «IT»; «BI» sí
            {"id": "a", "project": "Delta", "name": "IT", "terms": []},
            {"id": "b", "project": "Delta", "name": "BI", "terms": []}])["vocab"], "b")
        self.assertEqual(tema("Revisión de procesos"), "")  # «de», «y», «and»… no cuentan
        self.assertEqual(calendario._palabras_nombre("TSA y legal"), ["tsa", "legal"])
        self.assertEqual(calendario._palabras_nombre("HR and people"), ["hr", "people"])
        self.assertEqual(calendario._palabras_nombre("General"), [])

    def test_solo_el_proyecto_en_el_asunto_va_a_su_general(self):
        """El nombre del proyecto suma a todas sus listas a la vez: si es lo único que coincide, la General del
        proyecto, y no la primera por orden alfabético (antes, «Business processes» para cualquier reunión de Gamma)."""
        s = calendario.suggest({"subject": "Gamma - comité de dirección"}, self.GAMMA)
        self.assertEqual(s["vocab"], "Gamma/Vocabulario/General.txt")
        # si una lista coincide por lo suyo, gana la concreta
        s = calendario.suggest({"subject": "Gamma - plan de RPG"}, self.GAMMA)
        self.assertEqual(s["vocab"], "Gamma/Vocabulario/Legacy (AS400).txt")
        # sin General en el proyecto, la primera de sus listas, como antes
        sin_general = [v for v in self.GAMMA if v["name"] != "General"]
        s = calendario.suggest({"subject": "Gamma - comité de dirección"}, sin_general)
        self.assertEqual(s["vocab"], "Gamma/Vocabulario/Business processes.txt")

    def test_el_proyecto_que_nombra_el_asunto_manda_en_el_empate(self):
        """Revisión 1, P1-1: con las palabras del nombre de la lista sumando, una lista de OTRO proyecto podía empatar
        por lo suyo y ganar al proyecto que nombra el asunto («Alfa - plan de cutover» se iba a Gamma)."""
        gamma = self.GAMMA + [{"id": "Gamma/Vocabulario/Cutover plan.txt", "project": "Gamma", "name": "Cutover plan",
                               "terms": ["cutover", "rollback"]}]
        s = calendario.suggest({"subject": "Alfa - plan de cutover"}, VOCABS + gamma)
        self.assertEqual(s["vocab"], "Alfa/Vocabulario/Migración.txt")
        s = calendario.suggest({"subject": "Cutover plan: rollback"}, VOCABS + gamma)  # sin proyecto: la de Gamma
        self.assertEqual(s["vocab"], "Gamma/Vocabulario/Cutover plan.txt")

    def test_el_nombre_entero_gana_a_uno_con_una_palabra_generica(self):
        """Revisión 1, P2-3: «Network team» suma 2 con «network» («team» no cuenta), lo mismo que «Network» entero; a
        igual puntuación, el nombre que sale tal cual."""
        listas = [{"id": "Beta/Vocabulario/Network team.txt", "project": "Beta", "name": "Network team", "terms": []},
                  {"id": "Gamma/Vocabulario/Network.txt", "project": "Gamma", "name": "Network", "terms": []}]
        self.assertEqual(calendario.suggest({"subject": "Network review"}, listas)["vocab"],
                         "Gamma/Vocabulario/Network.txt")
        self.assertEqual(calendario.suggest({"subject": "Network team weekly"}, listas)["vocab"],
                         "Beta/Vocabulario/Network team.txt")

    def test_las_cifras_del_nombre_cuentan(self):
        """Revisión 2, P2-1: «Wave 1» quedaba en «wave» (las cifras no contaban) y esa palabra sola valía por el nombre
        entero: «Wave 3 planning» iba a «Wave 1». Ahora pide también la cifra, como palabra entera («1» no es «10»)."""
        olas = [{"id": f"Gamma/Vocabulario/{n}.txt", "project": "Gamma", "name": n, "terms": []}
                for n in ("Wave 1", "Wave 2", "General")]
        self.assertEqual(calendario._palabras_nombre("Wave 1"), ["wave", "1"])
        self.assertEqual(calendario.suggest({"subject": "Wave 3 planning"}, olas)["vocab"], "")
        self.assertEqual(calendario.suggest({"subject": "Wave readiness"}, olas)["vocab"], "")
        self.assertEqual(calendario.suggest({"subject": "Wave 10 kickoff"}, olas)["vocab"], "")
        self.assertEqual(calendario.suggest({"subject": "Wave 2 go/no-go"}, olas)["vocab"],
                         "Gamma/Vocabulario/Wave 2.txt")
        self.assertEqual(calendario.suggest({"subject": "Go-live wave 1"}, olas)["vocab"],
                         "Gamma/Vocabulario/Wave 1.txt")

    def test_sin_reunion_ni_listas(self):
        self.assertEqual(calendario.suggest({}, [])["name"], "")
        self.assertEqual(calendario.suggest(None, None)["participants"], [])

    def test_nombre_de_archivo_valido(self):
        self.assertEqual(calendario.nombre_archivo('Sync: Alfa / Beta  "Q4" <v2>?'), "Sync Alfa Beta Q4 v2")
        self.assertEqual(calendario.nombre_archivo("..."), "")
        self.assertLessEqual(len(calendario.nombre_archivo("palabra " * 40)), 80)
        server.clean_name(calendario.nombre_archivo('Sync: Alfa / Beta  "Q4" <v2>?'))  # no lanza


class ReunionActualTests(unittest.TestCase):
    def test_solo_teams(self):
        r = calendario.reunion_actual({"outlook_on": False}, VOCABS, teams=lambda: "Cutover SAP")
        self.assertEqual(r["teams"], {"title": "Cutover SAP"})
        self.assertIsNone(r["outlook"])
        self.assertIsNone(r["reason"])
        self.assertEqual(r["sugerencia"]["source"], "teams")
        self.assertEqual(r["sugerencia"]["name"], "Cutover SAP")

    def test_outlook_apagado_no_se_consulta_y_probar_lo_fuerza(self):
        calls = []

        def fn(now=None, timeout=8.0, teams_title=None):
            calls.append(teams_title)
            return {"subject": "Comité SAP", "organizer": "Ana", "attendees": ["Luis Pérez"], "categories": []}, None
        r = calendario.reunion_actual({"outlook_on": False}, VOCABS, teams=lambda: None, outlook_fn=fn)
        self.assertEqual((calls, r["sugerencia"], r["outlook"]), ([], {}, None))
        r = calendario.reunion_actual({"outlook_on": False}, VOCABS, outlook=True, teams=lambda: "Comité",
                                      outlook_fn=fn)
        self.assertEqual(calls, ["Comité"])
        self.assertEqual(r["sugerencia"]["source"], "outlook")
        self.assertEqual(r["sugerencia"]["participants"], ["Ana", "Luis Pérez"])
        self.assertEqual(r["sugerencia"]["vocab_label"], "Alfa · General")

    def test_motivo_de_outlook_y_teams_de_respaldo(self):
        r = calendario.reunion_actual({"outlook_on": True}, VOCABS, teams=lambda: "Semanal",
                                      outlook_fn=lambda **k: (None, "Outlook clásico no está abierto."))
        self.assertEqual(r["reason"], "Outlook clásico no está abierto.")
        self.assertEqual(r["sugerencia"]["source"], "teams")

    def test_nada(self):
        r = calendario.reunion_actual({}, VOCABS, teams=lambda: None)
        self.assertEqual(r, {"teams": None, "outlook": None, "sugerencia": {}, "reason": None})


class DecidirAvisoTests(unittest.TestCase):
    def test_una_vez_por_llamada(self):
        abrir, avisado = calendario.decidir_aviso("Teams", False, True, False, None)
        self.assertEqual((abrir, avisado), (True, "Teams"))
        self.assertEqual(calendario.decidir_aviso("Teams", False, True, False, "Teams"), (False, "Teams"))
        self.assertEqual(calendario.decidir_aviso(None, False, True, False, "Teams"), (False, None))  # fin: se olvida
        self.assertEqual(calendario.decidir_aviso("Teams", False, True, False, None), (True, "Teams"))

    def test_no_grabando_ni_apagado_ni_abierto(self):
        self.assertEqual(calendario.decidir_aviso("Teams", True, True, False, None), (False, None))
        self.assertEqual(calendario.decidir_aviso("Teams", False, False, False, None), (False, None))
        self.assertEqual(calendario.decidir_aviso("Teams", False, True, True, None), (False, None))
        self.assertEqual(calendario.decidir_aviso("Zoom", False, True, False, "Teams"), (True, "Zoom"))


class _AvisoHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.server.posts.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
        body = json.dumps(getattr(self.server, "respuesta", {"ok": True})).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AvisoCerrarTests(unittest.TestCase):
    """La ✕ del aviso flotante (punto 38 del review del 14/11/2026): en «Ya ha empezado «X»» es «Ahora no», como el
    botón de la app (POST /api/rec/siguiente {descartar: true}); en el de la llamada solo cierra. Sin Tk: los métodos
    de Aviso sobre un doble con lo que usan."""

    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _AvisoHandler)
        self.srv.posts = []
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        datos = tempfile.mkdtemp(prefix="tenca-aviso-")
        self.addCleanup(shutil.rmtree, datos, True)
        env = unittest.mock.patch.dict(os.environ, {"CHULETA_DATOS": datos})
        env.start()
        self.addCleanup(env.stop)

    def doble(self, siguiente):
        class Root:
            oculta = destruida = False

            def withdraw(self):
                self.oculta = True

            def after(self, ms, fn):
                fn()

            def destroy(self):
                self.destruida = True
        a = type("A", (), {})()
        a.args = type("Args", (), {"siguiente": siguiente})()
        a.url, a.root = f"http://127.0.0.1:{self.srv.server_address[1]}", Root()
        a.resultado, a.enviando, a.datos = None, False, {}
        for m in ("cerrar", "_pedir", "_descartar"):
            setattr(a, m, getattr(aviso.Aviso, m).__get__(a))
        return a

    def test_en_la_siguiente_reunion_la_x_es_ahora_no(self):
        a = self.doble("Revisión de presupuesto")
        aviso.Aviso.cerrar_x(a)
        self.assertTrue(a.root.oculta)  # se va al momento; la petición, en segundo plano
        fin = time.time() + 10
        while a.resultado is None and time.time() < fin:
            time.sleep(0.05)
        self.assertEqual(self.srv.posts, [("/api/rec/siguiente", {"descartar": True, "origen": "aviso"})])
        self.assertEqual((a.resultado, a.datos, a.root.destruida), ("cerrar", {"descartada": True}, True))

    def test_con_el_descarte_en_camino_ni_los_20_s_lo_cortan(self):
        a = self.doble("Revisión de presupuesto")
        a.descartando = True
        aviso.Aviso.cerrar(a, "tiempo")
        aviso.Aviso.cerrar(a, "sin-servidor")
        self.assertEqual((a.resultado, a.root.destruida), (None, False))

    def test_en_el_aviso_de_la_llamada_solo_cierra(self):
        a = self.doble("")
        aviso.Aviso.cerrar_x(a)
        self.assertEqual((a.resultado, a.root.destruida, self.srv.posts), ("cerrar", True, []))

    def test_cambiar_pide_la_tarjeta_y_cierra(self):
        """«Cambiar», junto al proyecto (16/11/2026): POST /api/rec/elegir {origen: aviso} y el aviso se
        cierra con «elegir»; la app abre la tarjeta de Grabar. Con la petición en camino, ni Grabar ni otro clic."""
        a = self.doble("")
        a.subs = []
        a.sub = lambda texto, color=None: a.subs.append(texto)
        for m in ("_sin_cambiar", "_elegir", "_error", "grabar"):
            setattr(a, m, getattr(aviso.Aviso, m).__get__(a))
        aviso.Aviso.elegir(a)
        aviso.Aviso.elegir(a)  # doble clic: una sola petición
        fin = time.time() + 10
        while a.resultado is None and time.time() < fin:
            time.sleep(0.05)
        self.assertEqual(self.srv.posts, [("/api/rec/elegir", {"origen": "aviso"})])
        self.assertEqual((a.resultado, a.root.destruida), ("elegir", True))
        self.assertEqual(a.subs, ["Abriendo la tarjeta de grabar…"])

    def test_cambiar_sin_ventana_propia_lo_dice(self):
        """Revisión 1, P2-2: sin ventana propia (Edge) la app no puede ponerse delante sola: el aviso dice dónde está
        la tarjeta antes de cerrarse."""
        self.srv.respuesta = {"ok": True, "abierta": True, "tarjeta": 1}
        a = self.doble("")
        a.subs = []
        a.sub = lambda texto, color=None: a.subs.append(texto)
        for m in ("_sin_cambiar", "_elegir", "_error"):
            setattr(a, m, getattr(aviso.Aviso, m).__get__(a))
        aviso.Aviso.elegir(a)
        fin = time.time() + 10
        while a.resultado is None and time.time() < fin:
            time.sleep(0.05)
        self.assertEqual(a.resultado, "elegir")
        self.assertEqual(a.subs, ["Abriendo la tarjeta de grabar…", f"La tarjeta está abierta en {aviso.APP_NAME}"])


class FakeProc:
    def __init__(self, cmd, **kw):
        self.cmd, self.kw, self.alive = cmd, kw, True
        FakeProc.spawned.append(self)

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False


class VigiaTests(unittest.TestCase):
    def setUp(self):
        FakeProc.spawned = []
        self._popen = calendario.subprocess.Popen
        calendario.subprocess.Popen = FakeProc

    def tearDown(self):
        calendario.subprocess.Popen = self._popen

    def lectura(self, title="Cutover SAP"):
        return calendario.reunion_actual({}, VOCABS, teams=lambda: title)

    def test_tic_abre_una_vez_cierra_al_grabar_y_al_terminar(self):
        v = calendario.Vigia()
        st = {"rec_aviso": True}
        self.assertFalse(v.tic(None, False, st, self.lectura, 8765, "/app"))
        self.assertTrue(v.tic("Teams", False, st, self.lectura, 8765, "/app", creationflags=7))
        self.assertEqual(len(FakeProc.spawned), 1)
        cmd = FakeProc.spawned[0].cmd
        self.assertTrue(cmd[1].endswith("aviso.py"))
        self.assertEqual(cmd[cmd.index("--puerto") + 1], "8765")
        self.assertEqual(cmd[cmd.index("--titulo") + 1], "Cutover SAP")
        self.assertEqual(cmd[cmd.index("--vocab") + 1], "Alfa/Vocabulario/Migración.txt")
        self.assertEqual(cmd[cmd.index("--tema") + 1], "Alfa · Migración")
        self.assertEqual(FakeProc.spawned[0].kw["creationflags"], 7)
        self.assertFalse(v.tic("Teams", False, st, self.lectura, 8765, "/app"))  # ya abierto / ya avisado
        self.assertEqual(len(FakeProc.spawned), 1)
        self.assertFalse(v.tic("Teams", True, st, self.lectura, 8765, "/app"))  # empieza a grabar: se cierra
        self.assertFalse(FakeProc.spawned[0].alive)
        self.assertFalse(v.tic("Teams", False, st, self.lectura, 8765, "/app"))  # misma llamada: no se repite
        self.assertFalse(v.tic(None, False, st, self.lectura, 8765, "/app"))  # fin de la llamada
        self.assertIsNone(v.ultimo)
        self.assertTrue(v.tic("Teams", False, st, self.lectura, 8765, "/app"))  # llamada nueva: otra vez
        self.assertEqual(len(FakeProc.spawned), 2)

    def test_el_aviso_dice_el_proyecto_con_que_grabara(self):
        """16/11/2026: el aviso decía el tema de la sugerencia por título, pero su «Grabar» graba con el plan
        (la misma reunión grabada antes, el proyecto fijo de Ajustes, la última grabación…). Con tema_plan, el del
        plan; si falla, el de la sugerencia, como antes; "" (sin proyecto) no se tapa con la sugerencia."""
        def tema_de(tema_plan):
            FakeProc.spawned = []
            calendario.Vigia().tic("Teams", False, {}, self.lectura, 8765, "/app", tema_plan=tema_plan)
            cmd = FakeProc.spawned[0].cmd
            return cmd[cmd.index("--tema") + 1], cmd[cmd.index("--vocab") + 1]
        self.assertEqual(tema_de(lambda: "Beta › General"), ("Beta › General", "Alfa/Vocabulario/Migración.txt"))
        self.assertEqual(tema_de(lambda: ""), ("", "Alfa/Vocabulario/Migración.txt"))
        self.assertEqual(tema_de(lambda: 1 / 0), ("Alfa · Migración", "Alfa/Vocabulario/Migración.txt"))
        self.assertEqual(tema_de(None), ("Alfa · Migración", "Alfa/Vocabulario/Migración.txt"))

    def test_ajuste_apagado_y_lectura_que_falla(self):
        v = calendario.Vigia()
        self.assertFalse(v.tic("Teams", False, {"rec_aviso": False}, self.lectura, 8765, "/app"))
        self.assertEqual(FakeProc.spawned, [])

        def rota():
            raise RuntimeError("COM")
        v2 = calendario.Vigia()
        self.assertTrue(v2.tic("Teams", False, {}, rota, 8765, "/app"))  # sin lectura el aviso sale igual
        self.assertEqual(FakeProc.spawned[0].cmd[FakeProc.spawned[0].cmd.index("--titulo") + 1], "")

    def test_sugerencia_usa_la_lectura_reciente(self):
        v = calendario.Vigia()
        n = []

        def leer():
            n.append(1)
            return self.lectura()
        self.assertEqual(v.sugerencia(leer)["name"], "Cutover SAP")
        self.assertEqual(v.sugerencia(leer)["name"], "Cutover SAP")
        self.assertEqual(len(n), 1)
        v.ultimo_at -= 1000
        v.sugerencia(leer)
        self.assertEqual(len(n), 2)
        self.assertEqual(calendario.Vigia().sugerencia(lambda: 1 / 0), {})


class ElegirAlEmpezarTests(unittest.TestCase):
    """16/11/2026: el aviso flotante dice el proyecto › lista del plan (con el que grabará) y su «Cambiar»
    pide la tarjeta de Grabar (POST /api/rec/elegir → `tarjeta` en /api/rec/status, que la interfaz abre una vez)."""

    def setUp(self):
        FakeProc.spawned = []
        self.tmp = tempfile.mkdtemp(prefix="tenca-elegir-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        a = self.a = server.App.__new__(server.App)
        a.settings = server.Store(os.path.join(self.tmp, "settings.json"), server.DEFAULT_SETTINGS)
        a.rec, a.monitor, a.meeting, a.meeting_prev, a.vigia = None, None, None, None, calendario.Vigia()
        a.parada_auto, a._vocab_terms = None, lambda: []
        a.__dict__["_cortes"] = type("Cortes", (), {"estado": lambda self: []})()
        a._siguiente_pub = lambda: None
        self.mostradas = []
        a.mostrar = lambda b: (self.mostradas.append(b), {"ok": True})[1]
        for p in (unittest.mock.patch.object(calendario.subprocess, "Popen", FakeProc),
                  unittest.mock.patch.object(server, "mic_meeting_app", lambda: "Teams"),
                  unittest.mock.patch.object(server, "notify", lambda t, b: None),
                  unittest.mock.patch.object(calendario, "reunion_actual", lambda *x, **k: {"sugerencia": {
                      "name": "Comité", "vocab": "Alfa/Vocabulario/General.txt", "vocab_label": "Alfa · General"}})):
            p.start()
            self.addCleanup(p.stop)

    def test_el_aviso_lleva_el_proyecto_del_plan(self):
        planes = []
        self.a.plan_grabar = lambda pedido, st=None, **k: (planes.append(pedido), {"proyecto": {
            "vocab": "Beta/Vocabulario/Redes.txt", "texto": "Beta › Redes"}})[1]
        n = self.a._meeting_step(None)
        self.a._meeting_step(n)  # dos lecturas seguidas: se abre
        self.assertEqual(len(FakeProc.spawned), 1)
        cmd = FakeProc.spawned[0].cmd
        self.assertEqual(cmd[cmd.index("--tema") + 1], "Beta › Redes")
        self.assertEqual(cmd[cmd.index("--vocab") + 1], "Alfa/Vocabulario/General.txt")  # lo que manda «Grabar»
        self.assertEqual(planes, [{}])  # el plan de Ctrl+Alt+R: sin nada elegido
        self.a.plan_grabar = lambda pedido, st=None, **k: {"proyecto": {"vocab": "", "texto": "Sin proyecto"}}
        self.assertEqual(self.a.tema_del_plan(), "")  # sin proyecto: «Pulsa Grabar para tomar notas»

    def test_cambiar_pide_la_tarjeta_una_vez(self):
        self.assertNotIn("tarjeta", self.a.rec_status({}))
        r = self.a.rec_elegir({"origen": "aviso"})
        self.assertEqual((r["tarjeta"], self.mostradas), (1, [{}]))  # sin interfaz abierta: se trae la ventana
        self.assertEqual(self.a.rec_status({})["tarjeta"], 1)
        self.a.rec_status({"ui": "1"})  # la interfaz sondea: sin ventana propia (Edge), no se abre otro Edge
        with unittest.mock.patch("app_grabar.WINDOW", None):
            self.assertEqual(self.a.rec_elegir({"origen": "aviso"})["abierta"], True)
        self.assertEqual((self.a.rec_status({})["tarjeta"], len(self.mostradas)), (2, 1))
        with unittest.mock.patch("app_grabar.WINDOW", object()):  # con ventana propia, siempre se trae delante
            self.assertEqual(self.a.rec_elegir({})["tarjeta"], 3)
        self.assertEqual(len(self.mostradas), 2)
        self.a.rec_elegir({"vista": 3})  # la atendió la interfaz: ninguna otra página la vuelve a abrir
        self.assertNotIn("tarjeta", self.a.rec_status({}))
        self.a.rec_elegir({"vista": 99})  # una que no es la de ahora no borra nada
        self.a.rec_elegir({})
        self.assertEqual(self.a.rec_status({})["tarjeta"], 4)
        self.a.tarjeta_pedida["at"] -= self.a.TARJETA_S + 1  # pasados 30 s, tampoco
        self.assertNotIn("tarjeta", self.a.rec_status({}))
        # revisión 1, P3-4: si se empieza a grabar antes de que la interfaz la abra, no reaparece al parar
        self.a.rec_elegir({})
        self.a.fin_llamada = type("Fin", (), {"quedan": lambda self, t: None})()
        self.a.rec = type("Rec", (), {"alive": lambda self: True, "path": os.path.join(self.tmp, "x.wav"),
                                      "started": 0, "status": {}, "sin_estado_s": lambda self: None})()
        self.assertNotIn("tarjeta", self.a.rec_status({}))
        self.a.rec = None
        self.assertNotIn("tarjeta", self.a.rec_status({}))
        self.a.rec = type("Rec", (), {"alive": lambda self: True})()
        self.assertEqual(self.a.rec_elegir({}), {"ok": False, "recording": True})  # grabando: nada que elegir


class StubApp:
    """Lo mínimo que el Handler necesita: cualquier ruta existe; /api/reunion/actual responde de verdad."""

    def __getattr__(self, name):
        return lambda *a, **k: {"stub": name}

    def reunion_actual(self, q):
        return calendario.reunion_actual({"outlook_on": False}, VOCABS, outlook=True if q.get("outlook") else None,
                                         teams=lambda: "Cutover SAP",
                                         outlook_fn=lambda **k: (None, "Outlook clásico no está abierto."))


class RutaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = server.Server(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        cls._app, cls._port, cls._token = server.Handler.app, server.PORT, server.Handler.token
        server.Handler.app, server.PORT = StubApp(), cls.port
        server.Handler.token = "prueba-reunion"  # el token de sesion que exige la API (P1-02)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        server.Handler.app, server.PORT, server.Handler.token = cls._app, cls._port, cls._token

    def get(self, path, host=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     headers={server.TOKEN_HEADER: server.Handler.token})
        if host:
            req.add_header("Host", host)
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)

    def test_reunion_actual(self):
        r = self.get("/api/reunion/actual")
        self.assertEqual(set(r), {"teams", "outlook", "sugerencia", "reason"})
        self.assertEqual(r["teams"]["title"], "Cutover SAP")
        self.assertEqual(r["sugerencia"]["source"], "teams")
        self.assertIsNone(r["reason"])
        r = self.get("/api/reunion/actual?outlook=1")
        self.assertEqual(r["reason"], "Outlook clásico no está abierto.")

    def test_host_ajeno_prohibido(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/api/reunion/actual", host="evil.example:80")
        self.assertEqual(cm.exception.code, 403)


class AjustesTests(unittest.TestCase):
    def test_claves_nuevas_en_los_ajustes(self):
        self.assertIs(server.DEFAULT_SETTINGS["outlook_on"], False)
        self.assertIs(server.DEFAULT_SETTINGS["rec_aviso"], True)
        tmp = tempfile.mkdtemp(prefix="notas-ajustes-")
        a = server.App.__new__(server.App)
        a.settings = server.Store(os.path.join(tmp, "settings.json"), server.DEFAULT_SETTINGS)
        a.config = lambda q: {}  # save_settings devuelve config(): aquí solo importa lo guardado
        a.save_settings({"outlook_on": True, "rec_aviso": False, "meeting_detect": True})
        st = a.settings.get()
        self.assertEqual((st["outlook_on"], st["rec_aviso"]), (True, False))
        a.save_settings({"outlook_on": 0})
        self.assertEqual(a.settings.get()["outlook_on"], False)


STATIC = os.path.join(os.path.dirname(calendario.__file__), "static")


def static(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read()


class AvisoTextoTests(unittest.TestCase):
    def test_limpiar_texto_para_tk(self):
        self.assertEqual(aviso.limpiar_texto("Reunión \U0001F680 Q4\n  (Alfa)\t"), "Reunión Q4 (Alfa)")
        self.assertEqual(aviso.limpiar_texto(None), "")
        self.assertEqual(len(aviso.limpiar_texto("x" * 500)), 200)
        self.assertEqual(aviso.limpiar_texto("Comité · Alfa"), "Comité · Alfa")  # el BMP se queda tal cual


class ReunionFrontendTests(unittest.TestCase):
    def test_ganchos_en_app_js_e_index(self):
        # (B2: el bloque va en «Calendario y Teams», que pinta ajustes.js)
        app = static("app.js") + static("ajustes.js")
        self.assertIn("reunion.js", orden_de_arranque())  # (main.js: antes, un <script> en index.html)
        # (07/11/2026: la sugerencia al grabar la aplica el servidor en el plan: sin reunionPrefill ni reunionExtra)
        # (14/11/2026, punto 12 del review) sin reunionCollect: «Aviso flotante» se guarda al cambiarlo (AJ_GUARDA)
        # (16/11/2026, punto 52) por import de reunion.js: con módulos, window.reunionX ya no existiría
        for s in ("${reunionFieldsetHtml()}", "    bindReunionSettings(d);\n",
                  "rnAviso: el => ({ rec_aviso: el.checked })"):
            self.assertEqual(app.count(s), 1, s)
        js = static("reunion.js")
        for s in ("function reunionFieldsetHtml", "function bindReunionSettings",
                  "/api/reunion/actual?outlook=1", 'id="rnOutlook"', 'id="rnAviso"', 'id="rnProbar"'):
            self.assertIn(s, js, s)
        self.assertNotRegex(js, r"[\U0001F300-\U0001FAFF\u2600-\u26FF]")  # sin emojis (iconos I.*)

    @unittest.skipUnless(shutil.which("node"), "node no instalado")
    def test_linea_de_probar(self):
        src = static("reunion.js")
        defs = ("const esc = s => String(s ?? '').replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"
                "'\"':'&quot;',\"'\":'&#39;'}[c]));\n")
        defs += "\n".join(m.group(0) for m in re.finditer(r"^function reunion(Hora|Linea)\(.*?^}", src, re.S | re.M))
        code = defs + """
const o = {subject: 'Comité <Q4>', start: '2026-10-31T10:00', end: '2026-10-31T11:00', organizer: 'Ana',
  attendees: ['Luis', 'Eva'], blocked: []};
console.log(reunionLinea({outlook: o, teams: {title: 'Comité'}, sugerencia: {vocab_label: 'Alfa · General'},
  reason: null}));
console.log(reunionLinea({outlook: null, teams: {title: 'Semanal'}, sugerencia: {},
  reason: 'Outlook clásico no está abierto.'}));
console.log(reunionLinea({outlook: null, teams: null, sugerencia: {}, reason: null}));
"""
        r = subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(lines[0], "Ahora: <b>Comité &lt;Q4&gt;</b> · 10:00–11:00 · organiza Ana · 2 asistentes · "
                                   "proyecto sugerido: <b>Alfa · General</b>")
        self.assertIn("Outlook clásico no está abierto.", lines[1])
        self.assertIn("Teams: <b>Semanal</b>", lines[1])
        self.assertEqual(lines[2], "Sin ventana de reunión de Teams abierta")


if __name__ == "__main__":
    unittest.main()
