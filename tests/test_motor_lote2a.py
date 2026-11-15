# -*- coding: utf-8 -*-
"""Lote 2a del motor (review del 14/11/2026): ajustes a prueba de valores raros (5), el PC despierto mientras graba o
transcribe (30), el informe técnico por reunión (32), los errores de los hilos en el log (33) y «Buscar versión nueva»
a mano (60).
"""
import copy
import faulthandler
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import support
from support import APP, FakeJob, live, server
import ajustes  # noqa: E402
import app_grabar  # noqa: E402
import despierto  # noqa: E402
import fallos  # noqa: E402
import informe  # noqa: E402
import paquete  # noqa: E402
import test_directo as td  # noqa: E402  (como módulo: sus tests no se repiten aquí)

WINDOWS = sys.platform == "win32"


# ----------------------------------------------------------------------------- 5 · ajustes a prueba de valores raros

MALOS = [None, "x", 7, -1, [], {}, True, 1e308, ["x"], {"x": 1}]
# lo que lee la interfaz al abrir y lo que pide al momento (las lecturas del fuzz del review)
LECTURAS = ("config", "tree", "rec_status", "jobs_list", "hoy", "voices_state", "modelos_state", "calendario_estado",
            "perfil_get", "diagnostico", "aprender_get", "tags", "vocab", "plan_grabar")


def casos_de_tipo():
    """Por cada clave de DEFAULT_SETTINGS (y un nivel por debajo), cada valor de MALOS que no es de su tipo (los 583
    casos del review del 14/11/2026)."""
    for k, v in server.DEFAULT_SETTINGS.items():
        for m in MALOS:
            if not (type(m) is type(v) and not isinstance(v, (dict, list))):
                yield (k,), m
        if isinstance(v, dict):
            for k2, v2 in v.items():
                for m in MALOS:
                    if not (type(m) is type(v2) and not isinstance(v2, (dict, list))):
                        yield (k, k2), m


# del tipo bueno pero con un valor que no vale, o listas y mapas con algún elemento raro
CASOS_DE_VALOR = [
    (("preset",), "turbo-que-no-existe"), (("live_preset",), ""), (("summary_lang",), "klingon"),
    (("procesado",), "nube"), (("outlook_asistentes",), "quizá"), (("threads",), 0), (("threads",), -4),
    (("worker_keepalive_min",), 10 ** 9), (("rec_mic",), "  "), (("rec_sys",), ""),
    (("links",), [{"path": 7}, "x", None, {"name": "a"}]), (("vocab_links",), [{"path": ""}, 3]),
    (("favorites",), [7, {"kind": "a"}, {"kind": "a", "path": None}]),
    (("project_folders",), {"A": 7, "B": None}), (("workstream_folders",), {"x": ["y"]}),
    (("project_langs",), {"A": {}}), (("folder_icons",), {"f": 1}), (("speed",), {"rapido": "x", "nube": -1}),
    (("speed",), {"medium": float("inf")}), (("cloud", "chunk_minutes"), 0), (("cloud", "parallel"), 99),
    (("cloud", "extra_keys"), [7, None]), (("cloud", "extra_keys_openai"), [{}]), (("perfil", "outputs"), [7, "x"]),
    (("aprendido", "reglas"), [7, "x", {"de": 1}]), (("aprendido", "no"), [None]), (("agente",), {"tipo": 7}),
]


class AjustesRarosTests(unittest.TestCase):
    """Un valor raro en settings.json ya no deja la app sin arrancar ni rompe lo que pide la interfaz al abrir."""

    def probar(self, keys, malo):
        tmp = tempfile.mkdtemp(prefix="ajustes-raros-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root, data = os.path.join(tmp, "Tenca"), os.path.join(tmp, "datos")
        os.makedirs(os.path.join(root, "_app", "static"))
        os.makedirs(data)
        if len(keys) == 1:
            st = {keys[0]: malo}
        else:
            st = {keys[0]: dict(copy.deepcopy(server.DEFAULT_SETTINGS[keys[0]]), **{keys[1]: malo})}
        with open(os.path.join(data, "settings.json"), "w", encoding="utf-8") as f:
            json.dump(st, f)
        fallos = []
        # sin hilos: cada App() deja los suyos vivos (el vigilante, la agenda…) y a los ~200 casos no quedan
        with support.patched_paths(ROOT=root, APP_DIR=os.path.join(root, "_app"), DATA_DIR=data,
                                   STATIC_DIR=os.path.join(root, "_app", "static")), \
                mock.patch.object(threading.Thread, "start", lambda self: None), \
                mock.patch("common.log"), mock.patch.object(ajustes, "log"):
            try:
                a = server.App()
            except Exception as e:  # noqa: BLE001
                return [("App()", e)], None
            for nombre in LECTURAS:
                try:
                    json.dumps(getattr(a, nombre)({}), ensure_ascii=False, default=str)
                except server.ApiError:
                    pass
                except Exception as e:  # noqa: BLE001
                    fallos.append((nombre, e))
        return fallos, a.settings.get()

    def test_ningun_valor_de_otro_tipo_impide_arrancar_ni_rompe_la_interfaz(self):
        """El fuzz del review (583 casos): antes, 21 sin arrancar y 58 con alguna lectura rota."""
        casos = list(casos_de_tipo())
        self.assertEqual(len(casos), 583)  # si cambia DEFAULT_SETTINGS, el fuzz crece con él
        malos = []
        for keys, malo in casos:
            fallos, _ = self.probar(keys, malo)
            malos += [(".".join(keys), repr(malo)[:12], donde, f"{e.__class__.__name__}: {e}"[:80])
                      for donde, e in fallos]
        self.assertEqual(malos, [])

    def test_valores_que_no_valen_y_elementos_raros(self):
        malos = []
        for keys, malo in CASOS_DE_VALOR:
            fallos, st = self.probar(keys, malo)
            malos += [(".".join(keys), donde, f"{e.__class__.__name__}: {e}"[:80]) for donde, e in fallos]
        self.assertEqual(malos, [])

    def test_lo_raro_vuelve_al_valor_por_defecto(self):
        d = {"preset": "zzz", "threads": True, "live_auto": 1, "language": None, "summary_lang": "xx",
             "procesado": "nube", "worker_keepalive_min": 30.0,
             "cloud": {"parallel": "3", "chunk_minutes": 99, "provider": 7, "api_key": ["a"]},
             "perfil": {"folder": 7, "vocab": None, "context": 3}, "aviso_resumen_local": "si"}
        defaults = copy.deepcopy(server.DEFAULT_SETTINGS)
        st = copy.deepcopy(defaults)
        for k, v in d.items():
            st[k] = dict(st[k], **v) if isinstance(v, dict) else v
        with mock.patch.object(ajustes, "log") as log:
            out = ajustes.normalizar_ajustes(st, defaults)
        self.assertEqual(sorted(out), sorted(["preset", "threads", "live_auto", "language", "summary_lang",
                                              "procesado", "cloud.parallel", "cloud.chunk_minutes", "cloud.provider",
                                              "cloud.api_key", "perfil.folder", "perfil.context",
                                              "aviso_resumen_local"]))
        for k in ("preset", "threads", "live_auto", "language", "summary_lang", "procesado", "aviso_resumen_local"):
            self.assertEqual(st[k], defaults[k], k)
        for k in ("parallel", "chunk_minutes", "provider", "api_key"):
            self.assertEqual(st["cloud"][k], defaults["cloud"][k], k)
        self.assertEqual(st["worker_keepalive_min"], 30)  # 30.0 escrito a mano vale
        self.assertIsInstance(st["worker_keepalive_min"], int)
        self.assertIsNone(st["perfil"]["vocab"])  # None es «automático»: vale
        self.assertEqual(log.call_count, len(out))  # una línea por clave
        self.assertIn("   ajuste cloud.parallel no válido: vuelve al valor por defecto",
                      [c.args[0] for c in log.call_args_list])

    def test_de_las_listas_y_los_mapas_solo_se_quita_lo_que_no_vale(self):
        defaults = server.DEFAULT_SETTINGS
        st = copy.deepcopy(defaults)
        st["links"] = [{"name": "Proyecto", "path": "C:\\Proyecto"}, {"path": 7}, "x"]
        st["project_folders"] = {"A": "a/b", "B": 7}
        st["speed"] = {"rapido": 7.7, "nube": "x"}
        st["cloud"]["extra_keys"] = ["gsk_buena", 7]
        with mock.patch.object(ajustes, "log") as log:
            out = ajustes.normalizar_ajustes(st, defaults)
        self.assertEqual(st["links"], [{"name": "Proyecto", "path": "C:\\Proyecto"}])
        self.assertEqual(st["project_folders"], {"A": "a/b"})
        self.assertEqual(st["speed"], {"rapido": 7.7})
        self.assertEqual(st["cloud"]["extra_keys"], ["gsk_buena"])
        self.assertEqual(sorted(out), ["cloud.extra_keys", "links", "project_folders", "speed"])
        lineas = [c.args[0] for c in log.call_args_list]
        self.assertIn("   ajuste links: 2 elementos no válidos, fuera", lineas)
        self.assertFalse(any("Proyecto" in l or "gsk_" in l for l in lineas))  # ni rutas ni claves en el log

    def test_unos_ajustes_buenos_no_cambian(self):
        """Lo que guarda la propia app (también lo de versiones anteriores, que lleva claves que ya no están en
        DEFAULT_SETTINGS) se queda tal cual."""
        defaults = server.DEFAULT_SETTINGS
        st = copy.deepcopy(defaults)
        st.update(preset="nube", live_preset="rapido", procesado="api", threads=4, carpetas_v=1, nombre_v="Tenca",
                  motores_api={"preset": "nube"}, agente={"tipo": "claude", "modelo": "sonnet"},
                  links=[{"name": "P", "path": "D:\\P"}], favorites=[{"kind": "a", "path": "x.wav"}],
                  speed={"rapido": 7.7}, outlook_asistentes="si", aviso_resumen_local=False,
                  perfil=dict(defaults["perfil"], folder="", vocab="P/Vocabulario/x.txt"))
        st["cloud"] = dict(defaults["cloud"], provider="azure", api_key="dpapi:AAAA", extra_keys=["k1"], otra="x")
        antes = copy.deepcopy(st)
        with mock.patch.object(ajustes, "log") as log:
            self.assertEqual(ajustes.normalizar_ajustes(st, defaults), [])
        self.assertEqual(st, antes)
        log.assert_not_called()

    def test_el_proveedor_raro_lo_sigue_arreglando_fix_groq(self):
        """El incidente del 10/11/2026 (un proveedor de una versión anterior): el tipo lo mira normalizar_ajustes, el
        valor, reparar_proveedor, que dice a qué pasa (y Ajustes lo enseña elegido)."""
        for viejo in ("", "groq-antiguo"):
            fallos, st = self.probar(("cloud", "provider"), viejo)
            self.assertEqual(fallos, [])
            self.assertEqual(st["cloud"]["provider"], "groq")

    def test_store_lo_hace_al_cargar_y_lo_guarda_corregido(self):
        tmp = tempfile.mkdtemp(prefix="ajustes-raros-")
        self.addCleanup(shutil.rmtree, tmp, True)
        p = os.path.join(tmp, "settings.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"cloud": {"provider": [], "parallel": 2}, "links": 7, "preset": "medium"}, f)
        with mock.patch.object(ajustes, "log"):
            st = server.Store(p, server.DEFAULT_SETTINGS)
        self.assertEqual(sorted(st.corregidos), ["cloud.provider", "links"])
        self.assertIsNone(st.broken)  # no es un fichero roto: lo demás se queda
        self.assertEqual((st.get()["preset"], st.get()["cloud"]["parallel"]), ("medium", 2))
        st.update(lambda d: None)
        with open(p, encoding="utf-8") as f:
            guardado = json.load(f)
        self.assertEqual((guardado["links"], guardado["cloud"]["provider"]), ([], "groq"))


# ----------------------------------------------------------------------------- 30 · el PC despierto


class FakeExecState:
    """SetThreadExecutionState de mentira: apunta las llamadas y el hilo de cada una; `falla` devuelve 0 (NULL)."""

    def __init__(self, falla=False):
        self.llamadas, self.falla = [], falla

    def __call__(self, flags):
        self.llamadas.append((flags, threading.get_ident()))
        return 0 if self.falla else 0x80000000


class DespiertoTests(unittest.TestCase):
    def nuevo(self, f):
        return despierto.Despierto(llamar=lambda: f)

    def test_se_pide_al_empezar_y_se_suelta_al_acabar_una_vez_cada_cosa(self):
        f = FakeExecState()
        d = self.nuevo(f)
        with mock.patch.object(despierto, "log") as log:
            for m in ("", "grabando", "grabando", "grabando y transcribiendo", "transcribiendo", "", ""):
                d.al_dia(m)
        flags = [x for x, _ in f.llamadas]
        self.assertEqual(flags, [despierto.ES_CONTINUOUS | despierto.ES_SYSTEM_REQUIRED, despierto.ES_CONTINUOUS])
        self.assertFalse(d.pedido)
        self.assertEqual(d.veces, 1)
        self.assertEqual([c.args[0] for c in log.call_args_list],
                         ["   PC despierto: pedido a Windows (grabando)",
                          "   PC despierto: ya no hace falta, se suelta"])
        # ni la pantalla ni el modo ausente: solo que no se suspenda solo
        self.assertFalse(flags[0] & 0x2 or flags[0] & 0x40)

    def test_si_windows_no_lo_acepta_se_dice_una_vez_y_se_reintenta(self):
        f = FakeExecState(falla=True)
        d = self.nuevo(f)
        with mock.patch.object(despierto, "log") as log:
            for _ in range(3):
                self.assertFalse(d.al_dia("grabando"))
        self.assertEqual(len(f.llamadas), 3)  # cada vuelta lo vuelve a intentar
        self.assertEqual(log.call_count, 1)
        self.assertIn("Windows no lo acepta", d.estado())
        f.falla = False
        self.assertTrue(d.al_dia("grabando"))
        self.assertIn("pedido a Windows (grabando)", d.estado())

    def test_un_motivo_raro_o_un_fallo_no_lanza(self):
        d = self.nuevo(mock.Mock(side_effect=OSError("x")))
        self.assertFalse(d.al_dia("grabando"))  # el vigilante que lo llama no puede morir por esto
        f = FakeExecState()
        d = self.nuevo(f)
        d.al_dia("algo que no es un motivo")
        self.assertEqual(f.llamadas, [])

    def test_fuera_de_windows_no_hace_nada(self):
        d = despierto.Despierto(llamar=lambda: None)
        self.assertFalse(d.disponible)
        self.assertFalse(d.al_dia("grabando"))
        self.assertEqual(d.estado(), "PC despierto: no disponible (solo en Windows)")

    def test_motivo(self):
        self.assertEqual([despierto.motivo(g, t) for g, t in ((0, 0), (1, 0), (0, 1), (1, 1))],
                         ["", "grabando", "transcribiendo", "grabando y transcribiendo"])

    def app(self, rec_vivo=False, estados=()):
        a = server.App.__new__(server.App)
        a.rec = types.SimpleNamespace(alive=lambda: rec_vivo) if rec_vivo is not None else None
        a.monitor, a.monitor_seen = None, 0.0
        a.jobs = support.bare_jobs([FakeJob(status=s) for s in estados])
        return a

    def test_lo_pide_el_vigilante_de_grabar_desde_su_hilo(self):
        """Grabando o con trabajos en cola o en marcha, el vigilante (_monitor_watchdog, que vive lo que la app) lo
        pide; con todo terminado, lo suelta. Siempre desde ese mismo hilo: la petición a Windows es del hilo que la
        hace."""
        casos = [(None, ()), (True, ()), (False, ("queued",)), (False, ("running", "done")), (False, ("done", "error")),
                 (True, ("running",))]
        esperado = ["", "grabando", "transcribiendo", "transcribiendo", "", "grabando y transcribiendo"]
        self.assertEqual([self.app(r, e)._motivo_despierto() for r, e in casos], esperado)

        class Para(Exception):
            pass
        a, f = self.app(True), FakeExecState()
        a.__dict__["_despierto"] = self.nuevo(f)
        vueltas = iter([None, None, Para()])

        def sleep(s):
            v = next(vueltas)
            if v is not None:
                raise v
        hilo = []

        def vigilante():
            hilo.append(threading.get_ident())
            reloj = types.SimpleNamespace(sleep=sleep, time=time.time)  # solo el de app_grabar, no el de todos
            with mock.patch.object(app_grabar, "time", reloj), mock.patch.object(despierto, "log"):
                try:
                    a._monitor_watchdog()
                except Para:
                    pass
        t = threading.Thread(target=vigilante)
        t.start()
        t.join(10)
        self.assertEqual(f.llamadas, [(despierto.ES_CONTINUOUS | despierto.ES_SYSTEM_REQUIRED, hilo[0])])

    def test_un_fallo_al_mirar_no_para_el_vigilante(self):
        class Para(Exception):
            pass
        a = self.app(True)
        a.jobs = None  # _motivo_despierto: sin cola
        a.rec = types.SimpleNamespace(alive=mock.Mock(side_effect=RuntimeError("x")))
        vueltas = iter([None, None, Para()])

        def sleep(s):
            v = next(vueltas)
            if v is not None:
                raise v
        with mock.patch.object(app_grabar, "time", types.SimpleNamespace(sleep=sleep, time=time.time)):
            with self.assertRaises(Para):  # dio dos vueltas sin morir
                a._monitor_watchdog()

    def test_en_el_diagnostico(self):
        a = self.app(True)
        a.__dict__["_despierto"] = self.nuevo(FakeExecState())
        with mock.patch.object(despierto, "log"):
            a.despierto.al_dia("grabando")
        a.settings = server.Store(os.path.join(tempfile.mkdtemp(prefix="desp-"), "settings.json"),
                                  server.DEFAULT_SETTINGS)
        with mock.patch.object(server, "log"):
            texto = a.diagnostico({})["texto"]
        self.assertIn("PC despierto: pedido a Windows (grabando) desde hace 0 min", texto)

    @unittest.skipUnless(WINDOWS, "SetThreadExecutionState y powercfg solo existen en Windows")
    def test_windows_de_verdad_lo_ve_en_powercfg(self):
        """En Windows (CI): la petición sale en `powercfg /requests` (SYSTEM) mientras dura y se va al soltarla."""
        d, hecho, soltar = despierto.Despierto(), threading.Event(), threading.Event()

        def hilo():
            d.al_dia("grabando")
            hecho.set()
            soltar.wait(30)
            d.al_dia("")
            hecho.set()
        with mock.patch.object(despierto, "log"):
            t = threading.Thread(target=hilo)
            t.start()
            self.assertTrue(hecho.wait(10))
            self.assertTrue(d.pedido)
            r = subprocess.run(["powercfg", "/requests"], capture_output=True, timeout=60)
            hecho.clear()
            soltar.set()
            self.assertTrue(hecho.wait(10))
            t.join(10)
        salida = r.stdout.decode("mbcs", "replace") if hasattr(r.stdout, "decode") else r.stdout
        if r.returncode != 0:
            self.skipTest(f"powercfg /requests no se puede leer aquí (rc {r.returncode}: ¿sin permisos?)")
        bloques = re.split(r"\r?\n\s*\r?\n", salida)
        sistema = next((b for b in bloques if b.strip().upper().startswith("SYSTEM")), "")
        self.assertIn(os.path.basename(sys.executable).lower(), sistema.lower(), salida)
        r2 = subprocess.run(["powercfg", "/requests"], capture_output=True, timeout=60)
        bloques = re.split(r"\r?\n\s*\r?\n", r2.stdout.decode("mbcs", "replace"))
        sistema = next((b for b in bloques if b.strip().upper().startswith("SYSTEM")), "")
        self.assertNotIn(os.path.basename(sys.executable).lower(), sistema.lower())
        self.assertFalse(d.pedido)


# ----------------------------------------------------------------------------- 33 · los errores de los hilos


class FallosTests(unittest.TestCase):
    def setUp(self):
        self.antes = list(fallos.CAIDOS)
        self.addCleanup(lambda: fallos.CAIDOS.__setitem__(slice(None), self.antes))
        fallos.CAIDOS.clear()

    def morir(self, nombre, exc):
        def f():
            raise exc
        t = threading.Thread(target=f, name=nombre)
        with mock.patch.object(threading, "excepthook", fallos.hilo):
            t.start()
            t.join(10)

    def test_un_vigia_que_muere_queda_en_el_log_y_se_avisa(self):
        with mock.patch.object(fallos, "LOGGER") as logger, mock.patch.object(fallos, "log") as log:
            self.morir("worker-local", KeyError("clave"))
        msg = logger.error.call_args.args[0] % logger.error.call_args.args[1:]
        self.assertTrue(msg.startswith("✖ hilo worker-local: se ha parado por un error (KeyError)\nTraceback"), msg)
        self.assertIn("KeyError: 'clave'", msg)
        self.assertEqual([(c["hilo"], c["que"], c["error"]) for c in fallos.caidos()],
                         [("worker-local", "la cola de transcripción en este PC", "KeyError")])
        self.assertIn("hasta reiniciar no funcionará", log.call_args.args[0])

    def test_por_el_nombre_de_su_funcion_y_los_que_no_son_vigias(self):
        self.assertEqual(fallos.vigia_de("Thread-7 (_monitor_watchdog)"), "_monitor_watchdog")
        self.assertIsNone(fallos.vigia_de("Thread-7 (_otra)"))
        self.assertIsNone(fallos.vigia_de("descarga-medium"))
        with mock.patch.object(fallos, "LOGGER") as logger, mock.patch.object(fallos, "log"):
            self.morir("descarga-medium", ValueError("x"))  # al log, pero no es un vigía
            self.morir("worker-voces", SystemExit())  # SystemExit, como en Python: nada
        self.assertEqual(logger.error.call_count, 1)
        self.assertEqual(fallos.caidos(), [])

    def test_el_hilo_principal(self):
        with mock.patch.object(fallos, "LOGGER") as logger, mock.patch.object(sys, "__excepthook__") as orig:
            try:
                raise ZeroDivisionError("x")
            except ZeroDivisionError:
                fallos.programa(*sys.exc_info())
            self.assertIn("✖ error sin atender (ZeroDivisionError)", logger.error.call_args.args[0] %
                          logger.error.call_args.args[1:])
            try:
                raise KeyboardInterrupt()
            except KeyboardInterrupt:
                fallos.programa(*sys.exc_info())  # Ctrl+C, como siempre
            self.assertEqual(logger.error.call_count, 1)
            self.assertEqual(orig.call_count, 2)  # con consola, también ahí

    def test_instalar_pone_los_ganchos_y_faulthandler_y_guarda_el_de_antes(self):
        tmp = tempfile.mkdtemp(prefix="fallos-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, fallos.FALLOS_GRAVES), "w", encoding="utf-8") as f:
            f.write('Fatal Python error: Segmentation fault\n\nCurrent thread 0x0001 (most recent call first):\n'
                    '  File "C:\\Users\\omega\\Tenca\\_app\\live.py", line 12 in run\n')
        antes = (threading.excepthook, sys.excepthook, faulthandler.is_enabled())

        def restaurar():
            threading.excepthook, sys.excepthook = antes[0], antes[1]
            faulthandler.disable()
            if antes[2]:
                faulthandler.enable()
            if fallos._ESTADO["fichero"]:
                fallos._ESTADO["fichero"].close()
        self.addCleanup(restaurar)
        open(os.path.join(tmp, "settings.json"), "w").close()
        open(os.path.join(tmp, "sesion.token"), "w").close()  # no lo borró: terminó sin apagar
        with mock.patch.object(fallos, "log") as log:
            self.assertTrue(fallos.instalar(tmp))
        self.assertIs(threading.excepthook, fallos.hilo)
        self.assertIs(sys.excepthook, fallos.programa)
        self.assertTrue(faulthandler.is_enabled())
        self.assertEqual(os.path.getsize(os.path.join(tmp, fallos.FALLOS_GRAVES)), 0)
        self.assertEqual([c.args[0] for c in log.call_args_list],
                         ["   la sesión anterior terminó sin apagar",
                          "   la sesión anterior dejó un volcado de faulthandler (3 líneas): "
                          "fallos-graves.anterior.log"])
        lineas = fallos.diagnostico(tmp)
        self.assertIn("Sesión anterior: terminó sin apagar (se cerró la consola o el proceso, corte de luz o fallo "
                      "grave)", lineas)
        self.assertIn("Volcado de faulthandler (sesión anterior; terminó sin apagar):", lineas)
        self.assertIn("  live.py:12 run", lineas)
        self.assertNotIn("omega", "\n".join(lineas))

    def restaurar_faulthandler(self):
        antes = (threading.excepthook, sys.excepthook, faulthandler.is_enabled(), dict(fallos._ESTADO))

        def restaurar():
            threading.excepthook, sys.excepthook = antes[0], antes[1]
            faulthandler.disable()
            if antes[2]:
                faulthandler.enable()
            if fallos._ESTADO["fichero"]:
                fallos._ESTADO["fichero"].close()
            fallos._ESTADO.update(antes[3])
        self.addCleanup(restaurar)

    @staticmethod
    def fin_de_sesion():
        """Como al terminar el proceso: faulthandler suelta su fichero (en Windows, abierto no se puede apartar; en la
        app instalar va una vez por proceso)."""
        faulthandler.disable()
        if fallos._ESTADO["fichero"]:
            fallos._ESTADO["fichero"].close()

    def test_un_volcado_viejo_no_se_dice_como_de_la_sesion_anterior(self):
        """Revisión 1 (P2): tras arranques limpios, el .anterior de hace semanas no sale en el diagnóstico; y el de
        una sesión que terminó bien se dice como lo que es (un error recuperado), no como una caída."""
        tmp = tempfile.mkdtemp(prefix="fallos-viejo-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.restaurar_faulthandler()  # despues: se cierra el fichero antes de borrar la carpeta (Windows)
        open(os.path.join(tmp, "settings.json"), "w").close()
        with open(os.path.join(tmp, fallos.FALLOS_GRAVES_ANTERIOR), "w", encoding="utf-8") as f:
            f.write("Windows fatal exception: access violation\n")
        with mock.patch.object(fallos, "log"):
            fallos.instalar(tmp)  # sin volcado nuevo y sin sesion.token: terminó bien
        lineas = fallos.diagnostico(tmp)
        self.assertIn("Sesión anterior: terminó bien", lineas)
        self.assertFalse(any("Volcado" in l for l in lineas), lineas)
        self.fin_de_sesion()
        with open(os.path.join(tmp, fallos.FALLOS_GRAVES), "w", encoding="utf-8") as f:
            f.write("Windows fatal exception: access violation\n")  # la sesión que termina bien con un volcado
        with mock.patch.object(fallos, "log"):
            fallos.instalar(tmp)
        lineas = fallos.diagnostico(tmp)
        self.assertIn("Volcado de faulthandler (sesión anterior; terminó bien: un error recuperado, no una caída):",
                      lineas)

    def test_la_primera_vez_no_hay_sesion_anterior(self):
        tmp = tempfile.mkdtemp(prefix="fallos-primera-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.restaurar_faulthandler()  # despues: se cierra el fichero antes de borrar la carpeta (Windows)
        with mock.patch.object(fallos, "log") as log:
            fallos.instalar(tmp)
        log.assert_not_called()
        self.assertFalse(any(l.startswith("Sesión anterior") for l in fallos.diagnostico(tmp)))

    def test_si_no_se_puede_apartar_no_se_vacia(self):
        """Revisión 1 (P3): sin poder apartar el volcado (bloqueado), se sigue escribiendo detrás, sin vaciarlo."""
        tmp = tempfile.mkdtemp(prefix="fallos-bloqueado-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.restaurar_faulthandler()  # despues: se cierra el fichero antes de borrar la carpeta (Windows)
        with open(os.path.join(tmp, fallos.FALLOS_GRAVES), "w", encoding="utf-8") as f:
            f.write("Fatal Python error: Aborted\n")
        with mock.patch.object(fallos.os, "replace", side_effect=PermissionError("en uso")), \
                mock.patch.object(fallos, "log"):
            self.assertTrue(fallos.instalar(tmp))
        with open(os.path.join(tmp, fallos.FALLOS_GRAVES), encoding="utf-8") as f:
            self.assertIn("Fatal Python error: Aborted", f.read())
        # revisión 2 (P3): el diagnóstico no lo da por un error de esta sesión
        self.assertIn("Volcado de faulthandler (esta sesión; con el de la sesión anterior, que no se pudo apartar):",
                      fallos.diagnostico(tmp))

    def test_un_fallo_grave_de_verdad_deja_su_volcado(self):
        """Un proceso que cae de verdad (faulthandler._sigsegv) con fallos.instalar: el volcado queda en el fichero,
        con la llamada que lo provocó, y el diagnóstico lo enseña sin rutas."""
        tmp = tempfile.mkdtemp(prefix="fallos-grave-")
        self.addCleanup(shutil.rmtree, tmp, True)
        codigo = ("import sys, ctypes\nsys.path.insert(0, sys.argv[1])\nimport fallos\n"
                  "if sys.platform == 'win32':\n    ctypes.windll.kernel32.SetErrorMode(0x0002)  # sin WER\n"
                  "fallos.instalar(sys.argv[2])\nimport faulthandler\n"
                  "def caer():\n    faulthandler._sigsegv()\ncaer()\n")
        r = subprocess.run([sys.executable, "-c", codigo, APP, tmp], capture_output=True, timeout=120)
        self.assertNotEqual(r.returncode, 0)
        v = fallos.volcado(os.path.join(tmp, fallos.FALLOS_GRAVES))
        self.assertTrue(v and v[0].startswith(("Fatal Python error:", "Windows fatal exception:")), v)
        self.assertIn("  <string>:9 caer", v)
        self.assertNotIn(tmp, "\n".join(v))

    def test_la_interfaz_lo_recibe_con_los_trabajos(self):
        a = server.App.__new__(server.App)
        a.jobs = support.bare_jobs([])
        a.movimientos_publicos = lambda: []
        a.jobs.snapshot = lambda: []
        self.assertEqual(a.jobs_list({})["hilos_caidos"], [])
        with mock.patch.object(fallos, "LOGGER"), mock.patch.object(fallos, "log"):
            self.morir("live-watch", RuntimeError("x"))
        caidos = a.jobs_list({})["hilos_caidos"]
        self.assertEqual([(c["hilo"], c["error"]) for c in caidos], [("live-watch", "RuntimeError")])
        self.assertEqual(set(caidos[0]), {"id", "hilo", "que", "a_las", "error"})  # ni el mensaje ni la traza

    def test_main_los_ganchos_tras_el_log_y_el_volcado_solo_siendo_la_app(self):
        """Los ganchos, en cuanto hay server.log; faulthandler, solo cuando esta copia ya es la app (despues de la
        instancia única y del puerto): una segunda copia que se abre y se cierra no toca el fichero de la que corre."""
        with open(os.path.join(APP, "server.py"), encoding="utf-8") as f:
            src = f.read()
        i = src.index("def main():")
        orden = [src.index(x, i) for x in ("setup_logging(DATA_DIR)", "fallos.ganchos()",
                                           "instance_lock(DATA_DIR, PORT)", "show_running(no_browser)",
                                           "fallos.instalar(DATA_DIR)", "start_session(DATA_DIR)")]
        self.assertEqual(orden, sorted(orden))

    def test_una_segunda_copia_no_toca_el_volcado_de_la_que_corre(self):
        """Si dos llamaran a instalar sobre la misma carpeta, la segunda apartaría el de la primera: por eso main solo
        lo llama siendo la app. Aquí, que instalar sí aparta uno con contenido y deja intacto uno vacío."""
        tmp = tempfile.mkdtemp(prefix="fallos-dos-")
        self.addCleanup(shutil.rmtree, tmp, True)
        open(os.path.join(tmp, fallos.FALLOS_GRAVES), "w").close()
        antes = (threading.excepthook, sys.excepthook, faulthandler.is_enabled())

        def restaurar():
            threading.excepthook, sys.excepthook = antes[0], antes[1]
            faulthandler.disable()
            if antes[2]:
                faulthandler.enable()
            if fallos._ESTADO["fichero"]:
                fallos._ESTADO["fichero"].close()
        self.addCleanup(restaurar)
        with mock.patch.object(fallos, "log") as log:
            fallos.instalar(tmp)
        log.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(tmp, fallos.FALLOS_GRAVES_ANTERIOR)))

    def test_el_aviso_en_la_interfaz(self):
        with open(os.path.join(APP, "static", "app.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn("onHilosCaidos(r.hilos_caidos);", js[js.index("async function pollJobs"):][:600])
        self.assertIn("Se ha parado una parte de", js)


# ----------------------------------------------------------------------------- 32 · informe técnico por reunión


class InformeTests(support.TempTree):
    def setUp(self):
        super().setUp()
        self.audio = self.wav("Proyecto Omega/2026-11-15 Reunión con Ana Pérez.wav")

    def test_apuntes_del_directo_por_tramos(self):
        a = informe.ApuntesDirecto()
        a.trozo("nube", "groq", "whisper-large-v3-turbo", 0.0, 5.0)
        a.trozo("nube", "groq", "whisper-large-v3-turbo", 5.0, 4.0, ok=False)
        a.trozo("pc", None, "medium", 9.0, 20.0)
        a.trozo("nube", "groq", "whisper-large-v3-turbo", 29.0, 5.0)
        a.pausa(0.5)
        a.pausa(0.5)
        t = a.tramos_publicos()
        self.assertEqual([(x["motor"], x["desde_s"], x["hasta_s"], x["trozos"], x["fallidos"], x["perdidos_s"])
                          for x in t], [("nube", 0.0, 9.0, 2, 1, 4.0), ("pc", 9.0, 29.0, 1, 0, 0.0),
                                        ("nube", 29.0, 34.0, 1, 0, 0.0)])
        self.assertEqual(a.pausa_s, 1.0)

    def run_live(self, spans, total, fail_on=(), remaining=None):
        tmp = tempfile.mkdtemp(prefix="informe-run-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "reunion.wav")
        rec = td.FakeRecorder(path, td.synth(spans, total))
        cloud = td.FakeCloud(rec, 0.8, remaining, fail_on)
        job = td.DirectoJob(preset="nube")
        job.informe = informe.ApuntesDirecto()
        opts = {"preset": "nube", "language": None, "prompt": None, "threads": 1, "worker": td.FakeWorker(rec),
                "cloud": cloud, "cloud_cfg": {"provider": "groq", "api_key": "k1"}, "tmp_dir": tmp, "resume": [],
                "summarizer": td.FakeSummarizer()}
        fake_time = types.SimpleNamespace(time=rec.time, sleep=rec.sleep)
        with mock.patch.object(live, "time", fake_time), mock.patch.object(live, "speech", td.energy_vad):
            rec.sleep(0.6)
            live.run(job, path, opts, lambda s: None, lambda m: None)
        return job, cloud

    def test_el_directo_apunta_cada_trozo_los_fallidos_y_la_espera_por_cupo(self):
        job, cloud = self.run_live([(0.0, 3.4), (4.0, 7.6), (8.2, 11.3)], 13.0, fail_on=(2,))
        t = job.informe.tramos_publicos()
        self.assertEqual([(x["motor"], x["proveedor"], x["modelo"]) for x in t],
                         [("nube", "groq", "whisper-large-v3-turbo")])
        self.assertEqual((t[0]["trozos"], t[0]["fallidos"]), (len(cloud.calls), 1))
        self.assertAlmostEqual(t[0]["perdidos_s"], cloud.calls[1]["secs"], places=1)
        job, cloud = self.run_live([(0.0, 3.4), (4.0, 7.6), (8.2, 11.3)], 13.0, remaining=40)
        self.assertGreater(job.informe.pausa_s, 5)  # en pausa por cupo hasta que se cerró la grabación

    def test_seccion_del_directo_solo_numeros(self):
        job, _ = self.run_live([(0.0, 3.4), (4.0, 7.6), (8.2, 11.3)], 13.0, fail_on=(2,))
        job.status = "done"
        informe.caida_a_local(job)
        d = informe.directo(job, self.audio)
        sec = d["secciones"]["directo"]
        self.assertEqual((sec["estado"], sec["fallidos"], sec["caidas_a_local"]), ("done", 1, 1))
        self.assertGreater(sec["latencia_s"]["n"], 0)
        self.assertEqual(sec["cupo"]["keys"], 1)
        self.assertIn("app", sec["memoria_mb"])
        guardado = informe.leer(self.audio)
        self.assertEqual(guardado["secciones"]["directo"], sec)
        self.sin_nada_personal(json.dumps(guardado, ensure_ascii=False))

    def sin_nada_personal(self, texto):
        for x in ("Omega", "Ana", "Pérez", "Reunión", self.tmp, "k1", "Auriculares"):
            self.assertNotIn(x, texto)

    def test_grabacion_sin_nombres_de_dispositivos(self):
        st = {"t": 63.5, "mic": {"name": "Auriculares de Ana", "db": -20, "lost": 0.25, "note": "x"},
              "sys": {"name": "Altavoces Omega", "db": -30, "lost": 0.0},
              "eco": {"micro": {"activa": True, "retardo_ms": 31, "atenuacion_db": 22.04, "bajado_s": 3.21},
                      "salida": {"activa": False, "retardo_ms": None, "atenuacion_db": 0.0, "bajado_s": 0.0}},
              "pendiente_s": 0, "disco_min": 900}
        d = informe.grabacion(self.audio, st, 63.4)
        g = d["secciones"]["grabacion"]
        self.assertEqual(g["segundos"], 63.4)
        self.assertEqual(g["perdido_s"], {"mic": 0.25, "sys": 0.0})
        self.assertEqual(g["eco"]["micro"], {"activa": True, "retardo_ms": 31, "atenuacion_db": 22.0,
                                             "atenuado_s": 3.2})
        self.sin_nada_personal(json.dumps(d, ensure_ascii=False))
        self.assertIsNotNone(informe.grabacion(self.audio, None))  # sin estado (se cortó al empezar): no rompe

    def test_trabajo_de_la_cola_y_repaso(self):
        j = FakeJob(path="x.wav", status="done", preset="nube")
        j.started, j.finished, j.duration, j.final_pass = 100.0, 160.0, 3600.0, True
        d = informe.trabajo(j, self.audio, cfg={"provider": "groq", "model": "whisper-large-v3"})
        self.assertEqual(d["secciones"]["repaso"]["modelo"], "whisper-large-v3")
        self.assertEqual(d["secciones"]["repaso"]["x_tiempo_real"], 60.0)
        j2 = FakeJob(path="x.wav", status="error", preset="rapido")
        j2.started, j2.finished, j2.duration, j2.final_pass = 1.0, 2.0, None, False
        d = informe.trabajo(j2, self.audio, cfg={"provider": "azure", "model": "mi-despliegue-omega"})
        self.assertEqual((d["secciones"]["transcripcion"]["motor"], d["secciones"]["transcripcion"]["modelo"]),
                         ("pc", "small"))
        self.assertEqual(set(d["secciones"]), {"repaso", "transcripcion"})  # las dos secciones, en el mismo
        d = informe.trabajo(FakeJob(path="x.wav", status="done", preset="nube"), self.audio,
                            cfg={"provider": "azure", "model": "mi-despliegue-omega"})
        self.assertEqual(d["secciones"]["transcripcion"]["modelo"], "otro")  # uno escrito a mano no sale
        self.assertIsNone(informe.trabajo(FakeJob(kind="summary", status="done"), self.audio))

    def test_va_con_la_reunion(self):
        self.assertIn(informe.SUFIJO, server.SIDE_EXTS)
        self.assertIn(informe.SUFIJO, server.salidas.SIDECAR_TAILS)
        self.assertEqual(informe.ruta_de(self.audio), os.path.splitext(self.audio)[0] + ".informe.json")

    def test_indice_con_los_ultimos_y_el_diagnostico(self):
        audios = [self.wav(f"Omega/r{i}.wav") for i in range(informe.GUARDADOS + 3)]
        for i, a in enumerate(audios):
            with mock.patch.object(informe.time, "strftime", return_value=f"2026-11-{i + 1:02d} 10:00"):
                informe.grabacion(a, {"t": i}, i)
        with open(os.path.join(self.data_dir, informe.INDICE), encoding="utf-8") as f:
            lista = json.load(f)["informes"]
        self.assertEqual(len(lista), informe.GUARDADOS)
        self.assertEqual([x["secciones"]["grabacion"]["segundos"] for x in informe.recientes()], [12.0, 11.0, 10.0])
        lineas = informe.diagnostico()
        self.assertEqual(lineas[0], "Informes técnicos de las 3 últimas reuniones (solo números):")
        self.assertEqual(len(lineas), 4)
        self.assertNotIn("Omega", "\n".join(lineas))

    def test_el_diagnostico_no_deja_pasar_texto_aunque_lo_escriba_otro(self):
        malo = {"formato": 1, "id": "abc", "actualizado": "2026-11-15 10:00", "app": "Reunión con Ana",
                "secciones": {"directo": {"motor": "nube", "modelo": "Proyecto Omega", "nota": "Ana Pérez dijo…",
                                          "Clave con espacios": 1, "tramos": [{"proveedor": "Omega SA"}]}}}
        with open(os.path.join(self.data_dir, informe.INDICE), "w", encoding="utf-8") as f:
            json.dump({"formato": 1, "informes": [malo]}, f)
        texto = "\n".join(informe.diagnostico())
        self.sin_nada_personal(texto)
        self.assertIn('"motor":"nube"', texto)
        self.assertIn('"app":"<texto>"', texto)

    def test_un_informe_que_no_se_deja_leer_no_se_pisa(self):
        """Revisión 1 (P2): OneDrive o el antivirus con el .informe.json abierto: se reintenta y, si sigue sin poder,
        no se anota esa sección (antes se escribía un informe nuevo encima y se perdían las demás)."""
        informe.grabacion(self.audio, {"t": 1}, 1)
        with open(informe.ruta_de(self.audio), encoding="utf-8") as f:
            antes = f.read()
        reloj = types.SimpleNamespace(sleep=mock.Mock(), strftime=time.strftime)
        with mock.patch.object(informe, "time", reloj), \
                mock.patch.object(informe.json, "load", side_effect=PermissionError("en uso")):
            self.assertIsNone(informe.trabajo(FakeJob(status="done"), self.audio))
        self.assertEqual(reloj.sleep.call_count, 19)
        with open(informe.ruta_de(self.audio), encoding="utf-8") as f:
            self.assertEqual(f.read(), antes)
        n = []  # y si a la tercera se deja, se anota con lo que había
        cargar = json.load

        def a_la_tercera(f):
            n.append(1)
            if len(n) < 3:
                raise PermissionError("en uso")
            return cargar(f)
        with mock.patch.object(informe, "time", reloj), mock.patch.object(informe.json, "load", a_la_tercera):
            d = informe.trabajo(FakeJob(status="done"), self.audio)
        self.assertEqual(set(d["secciones"]), {"grabacion", "transcripcion"})

    def test_sin_el_audio_no_deja_un_informe_suelto(self):
        os.remove(self.audio)
        self.assertIsNone(informe.grabacion(self.audio, {"t": 1}, 1))
        self.assertFalse(os.path.exists(informe.ruta_de(self.audio)))

    def test_una_seccion_que_falla_al_montarse_no_lanza(self):
        malo = types.SimpleNamespace(status="done", recorded=1.0)  # sin preset ni informe
        with mock.patch.object(informe, "memoria", side_effect=RuntimeError("x")):
            self.assertIsNone(informe.directo(malo, self.audio))
            self.assertIsNone(informe.trabajo(FakeJob(status="done"), self.audio))
        with mock.patch.object(informe, "_num", side_effect=RuntimeError("x")):
            self.assertIsNone(informe.grabacion(self.audio, {"t": 1}))

    def test_nada_de_esto_rompe_una_grabacion(self):
        with mock.patch.object(informe, "write_bytes", side_effect=PermissionError("OneDrive")):
            self.assertIsNone(informe.grabacion(self.audio, {"t": 1}))
        job = FakeJob()
        informe.trozo(job, True, {}, 0, 1)  # sin apuntes (un trabajo de antes): nada
        job.informe = informe.ApuntesDirecto()
        with mock.patch.object(informe.engines.CloudEngine, "live_model", side_effect=KeyError("x")):
            informe.trozo(job, True, {}, 0, 1)
        self.assertEqual(job.informe.tramos, [])

    def test_memoria_pico(self):
        m = informe.memoria(types.SimpleNamespace(live_worker=None, local=None))
        self.assertEqual(set(m), {"app", "whisper_directo", "whisper_cola"})
        self.assertGreater(m["app"], 10)  # este proceso, en MB (Windows, macOS y Linux)
        # bytes llenos (no bytearray, que en Linux reserva sin tocar las páginas y no cuenta)
        p = subprocess.Popen([sys.executable, "-c", "import time; x = bytes([1]) * (60 * 2**20); time.sleep(30)"])
        self.addCleanup(p.wait, 10)
        self.addCleanup(p.kill)
        time.sleep(1.5)
        m = informe.memoria(types.SimpleNamespace(live_worker=types.SimpleNamespace(p=p), local=None))
        if WINDOWS or sys.platform.startswith("linux"):
            self.assertGreater(m["whisper_directo"], 55)
        else:
            self.assertIsNone(m["whisper_directo"])  # en macOS no se sabe de otro proceso (la app es de Windows)

    def test_jobs_lo_engancha_al_directo_y_cuenta_las_caidas_a_local(self):
        jobs = support.bare_jobs([])
        jobs.settings = types.SimpleNamespace(get=lambda: dict(server.DEFAULT_SETTINGS, procesado="api",
                                                               cloud=dict(server.DEFAULT_SETTINGS["cloud"],
                                                                          api_key="")))
        jobs._cloud_text = lambda: types.SimpleNamespace(set=lambda *a: None, __contains__=lambda *a: False)
        jobs._hold = lambda rel: None
        rel = server.rel_of(self.audio)
        with mock.patch.object(server.threading.Thread, "start", lambda self: None), \
                mock.patch.object(server.Jobs, "active_for", return_value=None), \
                mock.patch("trabajos.log"), mock.patch.object(server.COMPLETE, "set"):
            jobs.start_live(rel, "nube")
        job = jobs.list[-1]
        self.assertIsInstance(job.informe, informe.ApuntesDirecto)
        self.assertEqual((job.preset, job.informe.caidas_a_local), ("medium", 1))  # sin clave: a este PC
        job.preset = "nube"
        with mock.patch("trabajos.log"):
            jobs.nube_apagada()
        self.assertEqual(job.informe.caidas_a_local, 2)

    def test_en_el_diagnostico_de_la_app(self):
        informe.grabacion(self.audio, {"t": 5}, 5)
        a = server.App.__new__(server.App)
        a.settings = server.Store(os.path.join(self.data_dir, "settings.json"), server.DEFAULT_SETTINGS)
        with mock.patch.object(server, "log"):
            texto = a.diagnostico({})["texto"]
        self.assertIn("Informes técnicos de las 1 últimas reuniones (solo números):", texto)
        self.assertIn('"grabacion":{"segundos":5', texto)
        self.sin_nada_personal(texto)


# ----------------------------------------------------------------------------- 60 · «Buscar versión nueva»


class Respuesta:
    def __init__(self, code, data=None):
        self.status_code, self.data = code, data

    def json(self):
        if self.data is None:
            raise ValueError("no es JSON")
        return self.data


RELEASE = {"tag_name": "v2026.11.20-abc1234", "html_url": "https://github.com/bastegman/tenca/releases/tag/"
           "v2026.11.20-abc1234", "published_at": "2026-11-20T10:00:00Z", "name": "Tenca 2026-11-20 · abc1234"}


class VersionNuevaTests(unittest.TestCase):
    def test_comparar(self):
        c = paquete.comparar_version
        tag = "v2026.11.15-d3c6591"
        self.assertEqual(c("2026-11-15 · d3c6591", tag), "al_dia")
        self.assertEqual(c("2026-11-14 · 62f93ea", tag), "nueva")
        self.assertEqual(c("2026-11-16 · 0d5169b", tag), "posterior")
        self.assertEqual(c("2026-11-15 · 1234567", tag), "mismo_dia")
        self.assertEqual(c("2026-11-15 21:40", tag), "sin_version")  # copia de trabajo: fecha y hora del código
        self.assertEqual(c("", tag), "sin_version")
        with self.assertRaises(ValueError):
            c("2026-11-15 · d3c6591", "latest")

    def test_una_sola_peticion_a_la_api_de_releases(self):
        get = mock.Mock(return_value=Respuesta(200, RELEASE))
        u = paquete.ultima_version(get)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.args, ("https://api.github.com/repos/bastegman/tenca/releases/latest",))
        h = get.call_args.kwargs["headers"]
        self.assertEqual(h["User-Agent"], server.APP_NAME)  # ni la versión ni nada del usuario
        self.assertEqual(set(get.call_args.kwargs) - {"headers", "timeout", "allow_redirects"}, set())
        self.assertEqual(u, {"tag": "v2026.11.20-abc1234", "version": "2026-11-20 · abc1234",
                             "url": RELEASE["html_url"], "fecha": "2026-11-20"})

    def test_lo_que_puede_responder_github(self):
        casos = [(Respuesta(404), "Todavía no hay"), (Respuesta(403), "no deja consultar"),
                 (Respuesta(429), "no deja consultar"), (Respuesta(500), "HTTP 500"), (Respuesta(200), "no es una"),
                 (Respuesta(200, {"tag_name": "nightly"}), "no es una"), (Respuesta(200, []), "no es una")]
        for r, frase in casos:
            with self.assertRaises(ValueError) as e:
                paquete.ultima_version(lambda *a, **k: r)
            self.assertIn(frase, str(e.exception))
        with self.assertRaises(ValueError) as e:
            paquete.ultima_version(mock.Mock(side_effect=OSError("sin red")))
        self.assertIn("No se ha podido consultar GitHub (OSError)", str(e.exception))

    def test_un_enlace_de_fuera_del_repo_no_se_abre(self):
        r = dict(RELEASE, html_url="https://ejemplo.invalid/malo")
        u = paquete.ultima_version(lambda *a, **k: Respuesta(200, r))
        self.assertEqual(u["url"], "https://github.com/bastegman/tenca/releases/tag/v2026.11.20-abc1234")

    def app(self):
        a = server.App.__new__(server.App)
        return a

    def test_la_app_lo_dice_y_abre_su_release(self):
        a = self.app()
        u = paquete.ultima_version(lambda *x, **k: Respuesta(200, RELEASE))
        with mock.patch("app_reuniones.ultima_version", return_value=u), \
                mock.patch("app_reuniones.VERSION", "2026-11-15 · d3c6591"), mock.patch("app_reuniones.log") as log:
            r = a.version_buscar({})
        self.assertEqual((r["estado"], r["ultima"], r["url"]), ("nueva", "2026-11-20 · abc1234", RELEASE["html_url"]))
        self.assertEqual(log.call_args.args[0], "   versión nueva: consultada (nueva)")
        with mock.patch("app_reuniones.webbrowser.open") as abrir:
            a.acerca_abrir({"que": "release"})
        abrir.assert_called_once_with(RELEASE["html_url"])
        b = self.app()
        with mock.patch("app_reuniones.webbrowser.open") as abrir:
            b.acerca_abrir({"que": "release"})  # sin haber buscado: la lista de versiones
        abrir.assert_called_once_with("https://github.com/bastegman/tenca/releases")
        with mock.patch("app_reuniones.ultima_version", side_effect=ValueError("No se ha podido consultar GitHub")), \
                mock.patch("app_reuniones.log"):
            with self.assertRaises(server.ApiError):
                b.version_buscar({})

    def test_solo_a_mano(self):
        """Nada la llama sola: ni al arrancar ni en un sondeo. En el servidor, solo version_buscar (POST, el botón);
        en la interfaz, solo buscarVersion, que solo se llama desde el clic de «Buscar versión nueva»."""
        llamadas = []
        for n in sorted(os.listdir(APP)):
            if n.endswith(".py"):
                with open(os.path.join(APP, n), encoding="utf-8") as f:
                    src = f.read()
                llamadas += [n for _ in re.finditer(r"ultima_version\(", src)]
        self.assertEqual(llamadas, ["app_reuniones.py", "paquete.py"])  # la definición y version_buscar
        self.assertIn('"/api/version/buscar": a.version_buscar', support.codigo_del_servidor())
        js = ""
        for n in sorted(os.listdir(os.path.join(APP, "static"))):
            if n.endswith(".js"):
                with open(os.path.join(APP, "static", n), encoding="utf-8") as f:
                    js += f.read()
        self.assertEqual(js.count("/api/version/buscar"), 1)
        self.assertEqual(re.findall(r"buscarVersion\([^)]*\)", js), ["buscarVersion(d)", "buscarVersion(d)"])
        self.assertIn("$('#aVersion', d).onclick = () => buscarVersion(d);", js)


if __name__ == "__main__":
    unittest.main()
