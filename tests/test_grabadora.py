# -*- coding: utf-8 -*-
"""Que una grabación no se pierda ni se corte sin decirlo (review del 14/11/2026, punto 1), y el nivelador del micro
que subía el eco de los altavoces (punto 6).

1. La grabadora de verdad (recorder.py con tests/fake_soundcard y fallos de disco de mentira,
   fake_soundcard/grabadora_falla.py): un fallo pasajero al escribir no pierde audio; un disco lleno que no se libera
   la para con el motivo y la cabecera cerrada; con poco disco lo dice.
2. El servidor con una grabadora de mentira que se muere (tests/fake_recorder/recorder.py en el _app temporal):
   rec_status «cortada» con el motivo, la reunión marcada «Grabación interrumpida a las hh:mm», la línea de tiempo del
   micro guardada (también cada 30 s mientras graba), la reserva suelta y la «(continuación)» en la misma carpeta a
   los ~2 s, como mucho 3 veces. Parar a mano no es un corte; la que no llega a arrancar, tampoco. El disco al empezar.
3. Al arrancar: las grabaciones propias que quedaron abiertas y ya no crecen se cierran y se dice; las de otro
   programa, o las que siguen creciendo, no se tocan.
4. MicLeveler: el eco ya no sube hasta la voz directa y su voz no baja (los casos del refutador del 07/11/2026)."""
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

from support import APP, REPO, TempTree, server, wav_bytes

import live  # noqa: E402
import recorder  # noqa: E402
import recording  # noqa: E402
import voices  # noqa: E402

FAKE_SC = os.path.join(REPO, "tests", "fake_soundcard")
FAKE_REC = os.path.join(REPO, "tests", "fake_recorder", "recorder.py")


def _vozsint():
    """vozsint.py sin poner tests/fake_soundcard en sys.path: ahi esta el soundcard falso, y con el importable
    test_ui_smoke cree que hay tarjeta de sonido y no permite el 400 de /api/rec/devices."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("vozsint", os.path.join(FAKE_SC, "vozsint.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.voz


voz = _vozsint()


def esperar(cond, tope=15.0, paso=0.05):
    fin = time.time() + tope
    while time.time() < fin:
        if cond():
            return True
        time.sleep(paso)
    return False


# ----------------------------------------------------------------------------- 1. la grabadora de verdad

class GrabadoraFallosTests(unittest.TestCase):
    """recorder.py con el soundcard falso: los tres casos a la vez (cada uno es un proceso de unos segundos)."""

    CASOS = {
        "pasajero": dict(env={"FAKE_ESCRITURA": "1-3"}, stop_s=9),  # segundos desde que empieza a escribir audio
        "lleno": dict(env={"FAKE_ESCRITURA": "1.5-", "FAKE_RETRY_S": "1.5"}, stop_s=None),
        "poco": dict(env={"FAKE_DISCO_MB": "100"}, stop_s=3),
        # parar mientras falla la escritura: no se tira lo que espera en memoria (revision 1, P2-1)
        "parar": dict(env={"FAKE_ESCRITURA": "0.5-4"}, stop_s=4),
        # y si no se arregla en los 8 s de gracia: se dice (revision 2, P2-B)
        "parar_lleno": dict(env={"FAKE_ESCRITURA": "0.5-"}, stop_s=4),
    }

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="tenca-grabadora-")
        cls.res = {}
        hilos = [threading.Thread(target=cls._uno, args=(k, c)) for k, c in cls.CASOS.items()]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(90)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _uno(cls, k, c):
        wav = os.path.join(cls.tmp, f"{k}.wav")
        env = dict(os.environ, PYTHONPATH=FAKE_SC, PYTHONIOENCODING="utf-8", FAKE_APP=APP, **c["env"])
        p = subprocess.Popen([sys.executable, "-u", os.path.join(FAKE_SC, "grabadora_falla.py"), wav, "--mic",
                              "default", "--sys", "all"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env, cwd=APP)
        out, err = [], []  # se lee mientras graba (en Windows la tuberia se llena en ~1,5 s: test_grabadora_eco)
        lectores = [threading.Thread(target=lambda f=f, b=b: b.extend(f), daemon=True)
                    for f, b in ((p.stdout, out), (p.stderr, err))]
        for t in lectores:
            t.start()
        t0 = time.time()
        if c["stop_s"]:
            time.sleep(c["stop_s"])
            try:
                p.stdin.write(b"stop\n")
                p.stdin.flush()
            except OSError:
                pass
        try:
            p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(10)
        for t in lectores:
            t.join(10)
        lines = [json.loads(x) for x in b"".join(out).decode("utf-8", "replace").splitlines() if x.strip()]
        cls.res[k] = {"rc": p.returncode, "s": time.time() - t0, "lines": lines, "wav": wav,
                      "log": b"".join(err).decode("utf-8", "replace")}

    def test_fallo_pasajero_no_pierde_audio(self):
        r = self.res["pasajero"]
        self.assertEqual(r["rc"], 0, r["log"])
        self.assertIn("se guarda en memoria", r["log"])
        self.assertIn("se vuelve a escribir la grabacion", r["log"])
        # mientras fallaba, el estado lo dice (la franja del directo: «N s de audio esperando en memoria»)
        self.assertGreater(max(x.get("pendiente_s") or 0 for x in r["lines"]), 1.0)
        done = [x for x in r["lines"] if x.get("done")][-1]
        info = live.wav_info(r["wav"])
        self.assertIsNotNone(info["data_size"])  # cabecera cerrada
        # todo lo que se dio por escrito esta en el fichero: nada se perdio durante los 2 s del fallo
        self.assertAlmostEqual(info["data_size"] / recorder.WAV_BYTES_S, done["t"], delta=0.01)
        self.assertGreater(done["t"], 4.0)

    def test_disco_lleno_para_con_el_motivo_y_la_cabecera_cerrada(self):
        r = self.res["lleno"]
        self.assertNotEqual(r["rc"], 0)
        self.assertLess(r["s"], 30)  # se para sola
        self.assertIn({"fatal": "el disco está lleno"}, r["lines"])
        self.assertIn("✖ la grabadora se para: el disco está lleno", r["log"])
        info = live.wav_info(r["wav"])
        self.assertIsNotNone(info["data_size"])  # reescribir la cabecera no necesita sitio nuevo
        # lo escrito antes del fallo (empieza a escribir ~1 s despues de arrancar: LAT, y el arranque del proceso)
        self.assertGreater(info["data_size"] / recorder.WAV_BYTES_S, 0.5)

    def test_parar_mientras_falla_no_tira_lo_pendiente(self):
        r = self.res["parar"]
        self.assertEqual(r["rc"], 0, r["log"])
        done = [x for x in r["lines"] if x.get("done")][-1]
        info = live.wav_info(r["wav"])
        self.assertAlmostEqual(info["data_size"] / recorder.WAV_BYTES_S, done["t"], delta=0.01)
        self.assertNotIn("no se han podido escribir", r["log"])

    def test_parar_con_el_disco_lleno_lo_dice(self):
        r = self.res["parar_lleno"]
        fatal = [x["fatal"] for x in r["lines"] if x.get("fatal")]
        self.assertEqual(len(fatal), 1, r["log"])
        self.assertRegex(fatal[0],
                         r"^no se han podido escribir los últimos \d+ s de la grabación \(el disco está lleno\)$")
        self.assertIsNotNone(live.wav_info(r["wav"])["data_size"])

    def test_poco_disco_se_dice(self):
        r = self.res["poco"]
        self.assertEqual(r["rc"], 0, r["log"])
        mins = [x.get("disco_min") for x in r["lines"] if x.get("disco_min") is not None]
        self.assertEqual(mins[-1], int(100e6 / recorder.WAV_BYTES_S / 60))  # 8 min
        self.assertIn("queda poco disco: unos 8 min", r["log"])


class EscritorTests(unittest.TestCase):
    """Escritor sin procesos: escrituras a medias y el tope de reintento."""

    class F:
        def __init__(self, fallos=0, trozo=None):
            self.data, self.fallos, self.trozo = bytearray(), fallos, trozo

        def write(self, b):
            if self.fallos:
                self.fallos -= 1
                raise PermissionError(13, "Permission denied")
            b = bytes(b[:self.trozo] if self.trozo else b)
            self.data += b
            return len(b)

    def test_escrituras_a_medias_no_pierden_ni_duplican(self):
        f = self.F(trozo=3)
        w = recorder.Escritor(f)
        w.write(b"abcdefgh")
        w.write(b"ij")
        self.assertEqual(bytes(f.data), b"abcdefghij")
        self.assertEqual(w.pend_n, 0)

    def test_fallo_guarda_en_memoria_y_luego_escribe_en_orden(self):
        f, logs = self.F(fallos=2), []
        w = recorder.Escritor(f, log=logs.append)
        self.assertFalse(w.write(b"uno"))
        self.assertFalse(w.write(b"dos"))
        self.assertEqual(w.pend_n, 6)
        self.assertTrue(w.write(b"tres"))
        self.assertEqual(bytes(f.data), b"unodostres")
        self.assertEqual(len([x for x in logs if "se guarda en memoria" in x]), 1)
        self.assertTrue(any("se vuelve a escribir" in x for x in logs))

    def test_tras_el_tope_se_rinde(self):
        f = self.F(fallos=10 ** 6)
        w = recorder.Escritor(f)
        with mock.patch.object(recorder, "WRITE_RETRY_S", 0.2):
            self.assertFalse(w.write(b"x"))
            time.sleep(0.3)
            with self.assertRaises(OSError):
                w.write(b"y")

    def test_motivos(self):
        import errno
        self.assertEqual(recorder.motivo_de(OSError(errno.ENOSPC, "No space left on device")), "el disco está lleno")
        e = OSError(22, "Invalid argument")
        e.winerror = 112  # ERROR_DISK_FULL en Windows
        self.assertEqual(recorder.motivo_de(e), "el disco está lleno")
        self.assertIn("no se puede escribir la grabación (PermissionError",
                      recorder.motivo_de(PermissionError(13, "x")))
        self.assertIn("error en la grabadora (ValueError", recorder.motivo_de(ValueError("y")))


# ----------------------------------------------------------------------------- 2. el servidor

class CorteServidorTests(TempTree):
    """La grabadora de mentira se copia como _app/recorder.py del arbol temporal: RecorderProcess la lanza de verdad."""

    def setUp(self):
        super().setUp()
        shutil.copy(FAKE_REC, os.path.join(self.app_dir, "recorder.py"))
        os.makedirs(os.path.join(self.root, "Alfa"), exist_ok=True)
        self.ctl = os.path.join(self.tmp, "ctl.json")
        self.runs = os.path.join(self.tmp, "arranques.txt")
        self.avisos, self.sonidos = [], []
        for p in (mock.patch.dict(os.environ, FAKE_REC_CTL=self.ctl),
                  mock.patch.object(server, "notify", lambda t, b: self.avisos.append((t, b))),
                  mock.patch.object(recording, "notify", lambda t, b: self.avisos.append((t, b))),
                  mock.patch.object(server, "cue", self.sonidos.append),
                  mock.patch.object(server, "log"), mock.patch.object(recording, "log"),
                  mock.patch.object(recording.Cortes, "REINTENTO_S", 0.3)):
            p.start()
            self.addCleanup(p.stop)
        self.a = self.app()
        self.addCleanup(self.parar)

    def ctl_(self, **kw):
        with open(self.ctl, "w", encoding="utf-8") as f:
            json.dump(dict({"runs": self.runs, "muere_s": 0.8}, **kw), f)

    def app(self):
        from test_carpetas import make_app
        import directo_avisos
        a = make_app(self)
        a.toggle_lock, a.meeting, a.parada_auto = threading.Lock(), None, None
        a.fin_llamada, a.siguiente, a._siguiente_cache = directo_avisos.FinDeLlamada(), None, (0.0, None)
        return a

    def parar(self):
        r = self.a.rec
        if r and r.alive():
            r.stop(timeout=5)

    def start(self, **b):
        body = dict({"mic": "default", "sys": "all", "folder": "Alfa", "live": False, "preset": "medium",
                     "name": "Comité", "vocab": ""}, **b)
        return self.a.rec_start(body)

    def arranques(self):
        try:
            with open(self.runs, encoding="utf-8") as f:
                return f.read().splitlines()
        except OSError:
            return []

    def abiertas(self):
        return [os.path.normcase(os.path.normpath(p)) for p in self.a.cortes._leer()]

    def ruta(self, rel):  # como abiertas(): en Windows, rel lleva «/» y la ruta absoluta «\\»
        return os.path.normcase(os.path.normpath(os.path.join(self.root, rel)))

    def test_se_corta_se_avisa_y_sigue_en_una_continuacion(self):
        self.ctl_(modos=["muere", "vive"])
        r = self.start()
        cut_abs = os.path.join(self.root, r["path"])
        self.assertEqual(self.abiertas(), [self.ruta(r["path"])])  # apuntada antes de grabar: si Tenca se cae
        self.assertTrue(esperar(lambda: any(c.get("continuacion") for c in self.a.cortes.estado())
                                and self.a.rec.path.endswith("(continuación).wav") and self.a.rec.status),
                        self.a.cortes.estado())
        c = self.a.cortes.estado()[-1]
        self.assertEqual((c["tipo"], c["path"], c["sigue"]), ("cortada", r["path"], True))
        self.assertIn("OSError: [Errno 5] Input/output error", c["motivo"])  # la ultima linea de recorder.log
        # la continuacion: misma carpeta, mismo nombre con «(continuación)», grabando y reservada
        cont = c["continuacion"]
        self.assertEqual(os.path.dirname(cont), os.path.dirname(r["path"]))
        self.assertTrue(cont.endswith(" - Comité (continuación).wav"), cont)
        self.assertTrue(self.a.rec.alive())
        self.assertEqual(server.rel_of(self.a.rec.path), cont)
        self.assertIn(cont, self.a.jobs.claimed)
        self.assertNotIn(r["path"], self.a.jobs.claimed)  # la reserva de la cortada, suelta
        self.assertEqual(self.abiertas(), [self.ruta(cont)])
        # la cortada: cabecera cerrada, «Tú» guardado y la reunion marcada
        self.assertIsNotNone(live.wav_info(cut_abs)["data_size"])
        side = voices.read_sidecar(voices.sidecar_path(cut_abs))
        self.assertTrue(side and side.get("micro") and "1" in side["micro"]["activo"], side)
        props, _ = server.parse_notes(server.read_text(os.path.splitext(cut_abs)[0] + ".md"))
        self.assertRegex(props["interrumpida"], r"^\d\d:\d\d · .*Input/output error")
        st = self.a.rec_status({})
        self.assertTrue(st["recording"])
        self.assertNotIn("cortada", st)  # ya graba otra vez: «cortada» es para cuando no se graba
        self.assertLess(st["sin_estado_s"], 2.0)  # manda estado cada 0,25 s
        self.assertEqual(st["cortes"][-1]["continuacion"], cont)
        self.assertIn("error", self.sonidos)
        self.assertTrue(any(t == "✖ Grabación interrumpida" and "Sigo grabando" in b for t, b in self.avisos),
                        self.avisos)
        # parar a mano la continuacion no es otro corte
        self.a.rec_stop({})
        time.sleep(0.3)
        self.assertEqual(len(self.a.cortes.estado()), 1)
        self.assertEqual(self.abiertas(), [])

    def test_como_mucho_tres_continuaciones(self):
        self.ctl_(modos=["muere"])
        r = self.start()
        self.assertTrue(esperar(lambda: len(self.a.cortes.estado()) == 4 and not self.a.cortes.estado()[-1]["sigue"],
                                tope=30), self.a.cortes.estado())
        time.sleep(1.0)
        self.assertEqual(len(self.arranques()), 4)  # la original y 3 continuaciones, ni una mas
        nombres = [os.path.basename(c["path"]) for c in self.a.cortes.estado()]
        self.assertEqual(nombres[0], os.path.basename(r["path"]))
        for n, sufijo in zip(nombres[1:], ("(continuación)", "(continuación 2)", "(continuación 3)")):
            self.assertTrue(n.endswith(f"Comité {sufijo}.wav"), n)
        st = self.a.rec_status({})
        self.assertFalse(st["recording"])
        self.assertEqual(st["cortada"]["path"], self.a.cortes.estado()[-1]["path"])
        self.assertFalse(st["cortada"]["sigue"])
        self.assertTrue(any("Ya no se graba" in b for t, b in self.avisos), self.avisos)
        self.assertEqual(self.abiertas(), [])
        self.assertEqual(self.a.jobs.claimed, set())

    def test_motivo_dicho_por_la_grabadora_y_sin_continuacion(self):
        self.ctl_(modos=["fatal"])
        with mock.patch.object(recording.Cortes, "MAX_REINTENTOS", 0):
            self.start()
            self.assertTrue(esperar(lambda: self.a.rec_status({}).get("cortada")))
        st = self.a.rec_status({})
        self.assertEqual((st["recording"], st["cortada"]["motivo"], st["cortada"]["sigue"]),
                         (False, "el disco está lleno", False))
        time.sleep(0.6)
        self.assertEqual(len(self.arranques()), 1)

    def test_el_micro_silenciado_sigue_silenciado_en_la_continuacion(self):
        ordenes = os.path.join(self.tmp, "ordenes.txt")
        self.ctl_(modos=["muere", "vive"], muere_s=1.5, ordenes=ordenes)
        self.start()
        time.sleep(0.5)
        self.a.rec_mic({"mute": True})
        self.assertTrue(esperar(lambda: self.a.rec.path.endswith("(continuación).wav") and self.a.rec.alive()
                                and self.a.rec.status.get("mic_muted")))

        def leidas():
            return open(ordenes, encoding="utf-8").read() if os.path.isfile(ordenes) else ""
        self.assertTrue(esperar(lambda: "(continuación).wav\tmute" in leidas()), leidas())

    def test_parar_en_los_2_s_antes_de_la_continuacion(self):
        self.ctl_(modos=["muere", "vive"])
        with mock.patch.object(recording.Cortes, "REINTENTO_S", 1.5):
            r = self.start()
            self.assertTrue(esperar(lambda: self.a.rec_status({}).get("cortada", {}).get("sigue")))
            res = self.a.rec_stop({})  # antes: «No hay ninguna grabación en marcha» y seguía grabando igual
            self.assertEqual(res["path"], r["path"])
            time.sleep(2.0)
        self.assertEqual(len(self.arranques()), 1)
        c = self.a.cortes.estado()[-1]
        self.assertEqual((c["sigue"], c["error"], c["continuacion"]), (False, "parada a mano", None))
        self.assertFalse(self.a.rec_status({})["recording"])

    def test_parar_justo_al_morir_no_sigue(self):
        # revision 2, P3: «Parar» cuando el proceso ya no esta pero el corte aun no se ha apuntado
        self.ctl_(modos=["muere", "vive"])
        motivo = recording.RecorderProcess.motivo

        def lento(rp):
            time.sleep(1.0)
            return motivo(rp)
        with mock.patch.object(recording.RecorderProcess, "motivo", lento):
            r = self.start()
            self.assertTrue(esperar(lambda: self.a.rec_status({}).get("cortada", {}).get("pendiente")))
            self.assertEqual(self.a.rec_stop({})["path"], r["path"])
            time.sleep(3.0)
        self.assertEqual(len(self.arranques()), 1)
        c = self.a.cortes.estado()[-1]
        self.assertEqual((c["sigue"], c["error"]), (False, "parada a mano"))

    def test_parada_a_medias_se_dice(self):
        self.ctl_(modos=["pierde"])
        self.start()
        time.sleep(0.6)
        self.a.rec_stop({})
        self.assertTrue(any(t == "✖ Grabación guardada a medias" and "últimos 3 s" in b for t, b in self.avisos),
                        self.avisos)
        self.assertEqual(self.a.cortes.estado(), [])  # no es un corte: se paró a mano

    def test_la_continuacion_no_se_pide_desde_fuera(self):
        # «_continuacion» en el cuerpo de /api/rec/start ya no vale: graba donde dice la carpeta, no en otro sitio
        self.ctl_(modos=["vive"])
        fuera = os.path.join(self.tmp, "fuera")
        os.makedirs(fuera)
        r = self.start(_continuacion={"dir": fuera, "intentos": 0, "nombre": "x"})
        self.assertTrue(r["path"].startswith("Alfa/"), r)
        self.assertEqual(os.listdir(fuera), [])

    def test_parar_a_mano_no_es_un_corte(self):
        self.ctl_(modos=["vive"])
        self.start()
        time.sleep(0.6)
        self.a.rec_stop({})
        time.sleep(0.5)
        self.assertEqual(self.a.cortes.estado(), [])
        self.assertNotIn("cortada", self.a.rec_status({}))
        self.assertEqual(self.abiertas(), [])
        self.assertEqual(self.avisos, [])

    def test_la_que_no_arranca_lo_dice_y_no_sigue(self):
        self.ctl_(modos=["nace"])
        with self.assertRaises(server.ApiError) as cm:
            self.start()
        self.assertIn("no ha podido arrancar: RuntimeError: no hay dispositivos", cm.exception.msg)
        time.sleep(0.8)
        self.assertEqual(len(self.arranques()), 1)
        self.assertEqual(self.a.cortes.estado(), [])
        self.assertEqual(self.abiertas(), [])

    def test_linea_de_tiempo_del_micro_mientras_graba(self):
        self.ctl_(modos=["vive"])
        with mock.patch.object(recording.RecorderProcess, "TIMELINE_S", 0.5):
            r = self.start()
            side = voices.sidecar_path(os.path.join(self.root, r["path"]))
            self.assertTrue(esperar(lambda: (voices.read_sidecar(side) or {}).get("micro"), tope=8))

    def test_disco_al_empezar(self):
        self.ctl_(modos=["vive"])
        with mock.patch.object(recording, "disco_libre", lambda d: 100e6):
            with self.assertRaises(server.ApiError) as cm:
                self.start()
        self.assertIn("No queda sitio en el disco para grabar: hay 100 MB libres", cm.exception.msg)
        self.assertEqual(self.arranques(), [])
        self.assertEqual(self.abiertas(), [])
        self.assertEqual(os.listdir(os.path.join(self.root, "Alfa")), [])  # ni la carpeta de la reunion
        with mock.patch.object(recording, "disco_libre", lambda d: 1e9):
            r = self.start()
        self.assertEqual(r["aviso"], "queda poco disco: caben unos 86 min de grabación")
        self.assertEqual(recording.disco_al_empezar("x", libre=20e9), (None, None))
        self.assertEqual(recording.disco_al_empezar("x", libre=None), (None, None))

    def test_la_continuacion_sin_disco_lo_dice(self):
        self.ctl_(modos=["muere"])
        libre = iter([1e10, 100e6, 100e6])
        with mock.patch.object(recording, "disco_libre", lambda d: next(libre)):
            self.start()
            self.assertTrue(esperar(lambda: self.a.cortes.estado() and self.a.cortes.estado()[-1].get("error")))
        c = self.a.cortes.estado()[-1]
        self.assertFalse(c["sigue"])
        self.assertIn("No queda sitio en el disco", c["error"])
        self.assertEqual(len(self.arranques()), 1)
        self.assertTrue(any(t == "✖ No se ha podido seguir grabando" for t, b in self.avisos), self.avisos)


class CortadaPendienteTests(unittest.TestCase):
    def test_muerta_sin_apuntar_ya_es_cortada(self):
        # el proceso ya no esta y el hilo de lectura aun no ha apuntado el corte: rec_status ya no dice «guardada»
        import types
        import directo_avisos
        a = server.App.__new__(server.App)
        a.monitor, a.meeting, a.parada_auto, a.siguiente = None, None, None, None
        a._siguiente_pub = lambda: None
        a.fin_llamada = directo_avisos.FinDeLlamada()
        a.__dict__["_cortes"] = recording.Cortes(tempfile.mkdtemp(prefix="tenca-cortes-"))
        self.addCleanup(shutil.rmtree, a.cortes.file.rsplit(os.sep, 1)[0], True)
        a.rec = types.SimpleNamespace(path=os.path.join(server.ROOT, "x.wav"), alive=lambda: False,
                                      on_end=lambda r: None, stopping=False, apuntado=False)
        st = a.rec_status({})
        self.assertEqual((st["recording"], st["cortada"]["pendiente"]), (False, True))
        a.rec.stopping = True  # parada pedida: no es un corte
        self.assertNotIn("cortada", a.rec_status({}))
        a.rec.stopping, a.rec.apuntado = False, True  # ya apuntado (y quiza ya caducado): nada provisional
        self.assertNotIn("cortada", a.rec_status({}))


class UltimaLineaTests(unittest.TestCase):
    def test_solo_lo_de_esta_grabadora_y_lo_que_dice_un_fallo(self):
        d = tempfile.mkdtemp(prefix="tenca-log-")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "recorder.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write("10:00:00 ✖ eco: viejo: ValueError\n")
        desde = os.path.getsize(p)
        self.assertIsNone(recording.ultima_linea(p, desde))
        with open(p, "a", encoding="utf-8") as f:
            f.write("10:01:00 salida duplicada: spk2 = spk1 con 80 ms\nTraceback (most recent call last):\n"
                    "  File \"recorder.py\", line 1\nMemoryError\n10:01:02 eco al terminar: micro {}\n")
        self.assertEqual(recording.ultima_linea(p, desde), "MemoryError")
        self.assertIsNone(recording.ultima_linea(os.path.join(d, "no-existe.log")))


# ----------------------------------------------------------------------------- 3. al arrancar

class RecuperarTests(TempTree):
    def abierto(self, rel, seconds=1.0):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(wav_bytes(seconds, open_header=True))
        old = time.time() - 120
        os.utime(p, (old, old))
        return p

    def test_cierra_las_propias_quietas_y_no_toca_las_demas(self):
        propia = self.abierto("Alfa/propia.wav")
        obs = self.abierto("Alfa/obs.wav")  # abierta, pero no es de Tenca: no se toca
        crece = self.abierto("Alfa/crece.wav")
        cerrada = os.path.join(self.root, "Alfa/cerrada.wav")
        with open(cerrada, "wb") as f:
            f.write(wav_bytes(1.0))
        cortes = recording.Cortes(self.data_dir)
        for p in (propia, crece, cerrada, os.path.join(self.root, "Alfa/no-existe.wav")):
            cortes.abierta(p)

        def escribe():
            time.sleep(0.2)
            with open(crece, "ab") as f:
                f.write(b"\0" * 4800)
        threading.Thread(target=escribe).start()
        vistas = []
        with mock.patch.object(recording, "log"):
            hechas = cortes.recuperar(lambda p, mt: vistas.append((p, mt)), esperar=0.6)
        # la cerrada tambien se dice: la grabadora la cerro al cerrarse Tenca, pero el corte nadie lo vio
        self.assertEqual(hechas, [cerrada, propia])
        self.assertEqual([v[0] for v in vistas], [cerrada, propia])
        self.assertEqual(live.wav_info(cerrada)["data_size"], 192000)  # no se toca
        self.assertIsNotNone(live.wav_info(propia)["data_size"])
        self.assertAlmostEqual(live.wav_info(propia)["data_size"] / recorder.WAV_BYTES_S, 1.0, delta=0.01)
        self.assertIsNone(live.wav_info(obs)["data_size"])
        self.assertIsNone(live.wav_info(crece)["data_size"])  # sigue creciendo: la escribe alguien
        self.assertEqual(cortes._leer(), [crece])  # se vuelve a mirar al arrancar otra vez

    def test_el_servidor_lo_dice_y_marca_la_reunion(self):
        from test_carpetas import make_app
        p = self.abierto("Alfa/2026-11-14 10-00-00 - Comité.wav")
        a = make_app(self)
        a.cortes.abierta(p)
        with mock.patch.object(recording.Cortes, "QUIETO_S", 0.2), mock.patch.object(server, "log"), \
                mock.patch.object(recording, "log"):
            a._recuperar_grabaciones()
        c = a.cortes.estado()[-1]
        self.assertEqual((c["tipo"], c["path"], c["nombre"]),
                         ("recuperada", "Alfa/2026-11-14 10-00-00 - Comité.wav", "2026-11-14 10-00-00 - Comité.wav"))
        self.assertRegex(c["a_las"], r"^\d\d:\d\d$")
        props, _ = server.parse_notes(server.read_text(os.path.splitext(p)[0] + ".md"))
        self.assertTrue(props["interrumpida"].startswith(c["a_las"] + " · "), props)
        self.assertIn("se cerró sin terminar la grabación", props["interrumpida"])


# ----------------------------------------------------------------------------- 4. el nivelador del micro

SR, B = recorder.SR, 2400  # bloques de 50 ms, como el escritor


def _db(x):
    return 20 * math.log10(float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) + 1e-12)


def _norm(x, nivel):
    act = x[np.abs(x) > 1e-6]
    return (x * (10 ** (nivel / 20) / (np.sqrt(np.mean(act * act)) + 1e-12))).astype(np.float32)


def _simular(T, otros, yo, eco, nuevo):
    """La mezcla bloque a bloque con el nivelador nuevo (MicLeveler, con la salida en crudo y la puerta de eco ya
    llamada, como en recorder.main) o el de antes (Leveler): la ganancia del micro y de la salida en cada muestra."""
    import eco as eco_mod
    n = T * SR
    mic = eco + yo + _norm(np.random.default_rng(9).standard_normal(n).astype(np.float32), -70.0)
    lm = (recorder.MicLeveler if nuevo else recorder.Leveler)(*recorder.GAIN_RANGE["mic"])
    ls = recorder.Leveler(*recorder.GAIN_RANGE["sys"])
    puerta = eco_mod.Gate("eco de los altavoces en el micro", 0.25, 0.003, act=False)
    gm, gs = np.zeros(n, np.float32), np.zeros(n, np.float32)
    for i in range(0, n - B + 1, B):
        m, s = np.repeat(mic[i:i + B, None], 2, 1), np.repeat(otros[i:i + B, None], 2, 1)
        a0, b0 = lm.gain, ls.gain
        if nuevo:
            puerta(m, s)
            lm.eco = puerta
            lm(m, ref=s)
        else:
            lm(m)
        ls(s)
        gm[i:i + B], gs[i:i + B] = np.linspace(a0, lm.gain, B), np.linspace(b0, ls.gain, B)
    return gm, gs


def _caso(T, otros_on, yo_on, eco_db, yo_db, musica=False, extra=None):
    """{nuevo, viejo}: su voz en el fichero (dBFS) y el eco frente a la voz directa cuando solo hablan los demas."""
    n = T * SR
    t = np.arange(n) / SR
    ruido = np.random.default_rng(4).standard_normal(n)
    o = voz(T, 120, 1) if not musica else np.convolve(ruido, np.ones(12) / 12, "same").astype(np.float32)
    otros = _norm(o * otros_on(t), -20.0)
    yo = _norm(voz(T, 180, 2) * yo_on(t), yo_db)
    if extra is not None:
        yo = yo + extra(t)
    d = int(0.030 * SR)  # eco de la sala a 30 ms
    eco = np.zeros(n, np.float32)
    if eco_db is not None:
        eco[d:] = otros[:-d] * 10 ** ((eco_db + 20.0) / 20)
    act = (yo_on(t) > 0) & (np.abs(yo) > 1e-5)
    first = np.argmax(act) / SR
    suya = act & (t > first + 2)  # tras sus dos primeros segundos (la ganancia prudente)
    solo = (otros_on(t) > 0) & (yo_on(t) == 0) & (t > first + 2)
    out = {}
    for k in ("nuevo", "viejo"):
        gm, gs = _simular(T, otros, yo, eco, k == "nuevo")
        out[k] = {"voz": _db((yo * gm)[suya]),
                  "eco": _db((eco * gm)[solo]) - _db((otros * gs)[solo]) if eco_db is not None else None}
    return out


def _turnos(t):
    return ((t % 6) < 4.5).astype(float)


def _suyos(t):
    return ((t % 6) >= 4.6).astype(float)


class MicLevelerTests(unittest.TestCase):
    def test_arranca_con_ganancia_prudente(self):
        lev = recorder.MicLeveler(*recorder.GAIN_RANGE["mic"])
        self.assertAlmostEqual(20 * math.log10(lev.gain), recorder.MIC_PRUDENTE_DB, places=3)
        eco = np.repeat(_norm(voz(5, 120, 1), -45.0)[:, None], 2, 1)
        sale = np.repeat(_norm(voz(5, 120, 1), -20.0)[:, None], 2, 1)
        for i in range(0, len(eco) - B, B):  # solo suena la reunion: su eco no se toma por su voz
            lev(eco[i:i + B], ref=sale[i:i + B])
        self.assertIsNone(lev.level)
        self.assertAlmostEqual(20 * math.log10(lev.gain), recorder.MIC_PRUDENTE_DB, places=3)

    def test_el_eco_ya_no_llega_a_la_voz_directa(self):
        # el caso del review: los demas por el altavoz a -20, el eco en el micro a -40, el usuario a -30
        r = _caso(60, _turnos, lambda t: (((t > 20) & (t < 26)) | ((t > 45) & (t < 50))).astype(float), -40.0, -30.0)
        self.assertGreater(r["viejo"]["eco"], -2.0)  # antes: casi igual de fuerte (las «voces dobles»)
        self.assertLess(r["nuevo"]["eco"], -8.0)
        self.assertGreater(r["nuevo"]["voz"], r["viejo"]["voz"] - 2.0)

    def test_su_voz_no_baja(self):
        casos = {
            "auriculares": dict(T=40, otros_on=_turnos, yo_on=_suyos, eco_db=None, yo_db=-30.0),
            "voz floja": dict(T=40, otros_on=_turnos, yo_on=_suyos, eco_db=-60.0, yo_db=-47.0),
            "a la vez, eco mas alto que su voz": dict(T=40, otros_on=lambda t: np.ones_like(t),
                                                      yo_on=lambda t: ((t % 4) < 2).astype(float), eco_db=-30.0,
                                                      yo_db=-35.0),
            "musica continua": dict(T=40, otros_on=lambda t: np.ones_like(t),
                                    yo_on=lambda t: ((t % 8) < 3).astype(float), eco_db=-40.0, yo_db=-30.0,
                                    musica=True),
        }
        for nombre, c in casos.items():
            with self.subTest(nombre):
                r = _caso(**c)
                # ni mas de 2 dB por debajo de antes ni del objetivo (antes, el eco inflaba la ganancia y su voz
                # quedaba por encima del objetivo: eso no cuenta como que baje)
                self.assertGreater(r["nuevo"]["voz"], min(r["viejo"]["voz"], recorder.TARGET_DB) - 2.0, r)

    def test_la_reunion_que_sube_de_golpe_no_se_toma_por_su_voz(self):
        # revision 1, P1-1: el usuario callado, un remoto flojo 20 s y luego otro 12 dB mas fuerte (o un aviso)
        T = 40
        n = T * SR
        t = np.arange(n) / SR
        v = voz(T, 120, 1) * _turnos(t)
        otros = np.where(t < 20, _norm(v, -32.0), _norm(v, -20.0)).astype(np.float32)
        d = int(0.03 * SR)
        eco = np.zeros(n, np.float32)
        eco[d:] = otros[:-d] * 10 ** (-22 / 20)
        gm, _ = _simular(T, otros, np.zeros(n, np.float32), eco, True)
        self.assertAlmostEqual(20 * math.log10(gm[-1]), recorder.MIC_PRUDENTE_DB, places=3)  # antes: +18 dB

    def test_la_reunion_que_no_calla_y_el_habla_casi_siempre(self):
        # revision 1, P1-2: musica continua (o el ruido de fondo de alguien) y el habla el 85 % desde el principio
        r = _caso(40, lambda t: np.ones_like(t), lambda t: ((t % 4) < 3.4).astype(float), -50.0, -42.0, musica=True)
        self.assertGreater(r["nuevo"]["voz"], min(r["viejo"]["voz"], recorder.TARGET_DB) - 2.0, r)

    def test_un_ponente_sin_pausas_con_el_callado(self):
        # revision 2, P1-A: un ponente que habla seguido (sin silencios) y el usuario callado: el eco no sube
        T = 40
        t = np.arange(T * SR) / SR
        otros = _norm(voz(T, 120, 1, on=lambda t: np.ones_like(t)), -20.0)
        eco = np.zeros(len(t), np.float32)
        d = int(0.03 * SR)
        eco[d:] = otros[:-d] * 10 ** (-22 / 20)
        gm, _ = _simular(T, otros, np.zeros(len(t), np.float32), eco, True)
        self.assertAlmostEqual(20 * math.log10(gm[-1]), recorder.MIC_PRUDENTE_DB, places=3)

    def test_su_voz_que_vuelve_por_la_salida(self):
        # revision 2, P1-B: con cascos, su voz vuelve por la salida (otro participante con altavoz, o «Escuchar este
        # dispositivo»): la salida suena justo cuando el habla; su voz no puede quedarse en la ganancia prudente
        T = 40
        n = T * SR
        t = np.arange(n) / SR
        on = lambda t: ((t % 8) < 4.5).astype(float)  # noqa: E731
        yo = _norm(voz(T, 180, 2) * on(t), -36.0)
        otros = np.zeros(n, np.float32)
        d = int(0.25 * SR)
        otros[d:] = yo[:-d] * 10 ** (6 / 20)  # vuelve a 250 ms, mas fuerte que en el micro
        res = {}
        for k in ("nuevo", "viejo"):
            gm, _ = _simular(T, otros, yo, np.zeros(n, np.float32), k == "nuevo")
            sel = (on(t) > 0) & (t > 10)
            res[k] = _db((yo * gm)[sel])
        self.assertGreater(res["nuevo"], min(res["viejo"], recorder.TARGET_DB) - 2.0, res)

    def test_tras_hablar_un_ponente_largo_no_sube_el_eco(self):
        # revision 2, P2-A: habla 10 s y luego escucha a un ponente sin pausas: la ganancia no vuelve a +18 dB
        T = 70
        n = T * SR
        t = np.arange(n) / SR
        otros = _norm(voz(T, 120, 1, on=lambda t: np.ones_like(t)), -20.0) * (t >= 10)
        yo = _norm(voz(T, 180, 2), -30.0) * (t < 10)
        eco = np.zeros(n, np.float32)
        d = int(0.03 * SR)
        eco[d:] = otros[:-d] * 10 ** (-22 / 20)
        gm, _ = _simular(T, otros.astype(np.float32), yo.astype(np.float32), eco, True)
        self.assertLess(20 * math.log10(gm[-1]), 10.0)  # antes: +18 a los 30 s

    def test_un_nan_no_rompe_la_mezcla(self):
        # revision 1, P2-3: con una muestra NaN del micro, la ganancia quedaba en NaN y la mezcla entera tambien
        lev = recorder.MicLeveler(*recorder.GAIN_RANGE["mic"])
        x = np.repeat(_norm(voz(6, 180, 2), -30.0)[:, None], 2, 1)
        x[5 * SR, 0] = np.nan
        for i in range(0, len(x) - B + 1, B):
            lev(x[i:i + B].copy())
        self.assertTrue(math.isfinite(lev.gain) and math.isfinite(lev.level))

    def test_una_tos_no_la_hunde(self):
        tos = lambda t: (0.5 * np.sin(2 * np.pi * 300 * t) * ((t >= 20) & (t < 20.3))).astype(np.float32)  # noqa
        r = _caso(40, _turnos, _suyos, -45.0, -30.0, extra=tos)
        self.assertGreater(r["nuevo"]["voz"], recorder.TARGET_DB - 2.0, r)  # antes: -24,6 y el 10 % peor a -36

    def test_mezclar_usa_la_salida_en_crudo(self):
        parts = {"mic": np.full((2400, 2), 0.01, np.float32), "sys": np.full((2400, 2), 0.1, np.float32)}
        levs = {"mic": recorder.MicLeveler(*recorder.GAIN_RANGE["mic"]), "sys": recorder.Leveler(-9.0, 9.0)}
        vistos = []
        orig = recorder.MicLeveler.__call__

        def espia(self, x, update=True, ref=None):
            vistos.append(ref)
            return orig(self, x, update, ref)
        with mock.patch.object(recorder.MicLeveler, "__call__", espia):
            out = recorder.mezclar(parts, levs)
        self.assertIs(vistos[0], parts["sys"])
        self.assertEqual(out.shape, (2400, 2))
        self.assertTrue(np.all(np.abs(recorder.mezclar(parts, levs, mic_muted=True)) <= 1.0))


# ----------------------------------------------------------------------------- 5. lo que dice la interfaz (node)

@unittest.skipUnless(shutil.which("node"), "node no instalado")
class TextosInterfazTests(unittest.TestCase):
    def run_js(self, fichero, nombres, code, pre=""):
        from test_frontend import js_defs, read
        defs = pre + js_defs(read(fichero), nombres)
        r = subprocess.run(["node", "-e", defs + "\nconsole.log(JSON.stringify(" + code + "))"], capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_disco_y_escritura_en_la_franja_del_directo(self):
        def avisos(rec, mine=True):
            return self.run_js("directo.js", ["directoMinTxt", "directoAvisos"],
                               f"directoAvisos({json.dumps(rec)}, null, {json.dumps(mine)})")
        self.assertEqual(avisos({"disco_min": 500, "pendiente_s": 0}), [])
        a = avisos({"disco_min": 90})
        self.assertEqual((a[0]["id"], a[0]["texto"]), ("disco", "Caben unos 90 min más de grabación en el disco"))
        a = avisos({"disco_min": 12, "pendiente_s": 3.4})
        self.assertEqual([x["id"] for x in a], ["disco", "escritura"])
        self.assertEqual(a[0]["texto"], "Quedan unos 12 min de disco para grabar")
        self.assertEqual(a[0]["nota"], "Libera espacio ya o la grabación se cortará.")
        self.assertEqual(a[1]["texto"], "No se puede escribir en el disco: 3 s de audio esperando en memoria")
        self.assertEqual(avisos({"disco_min": 12, "pendiente_s": 3.4}, mine=False), [])  # otra grabación
        a = avisos({"sin_estado_s": 7.2})  # la grabadora atascada (una escritura bloqueada no lanza error)
        self.assertEqual((a[0]["id"], a[0]["texto"]), ("atascada", "La grabadora no contesta desde hace 7 s"))
        self.assertEqual(avisos({"sin_estado_s": 1.0}), [])

    def test_aviso_de_la_grabacion_cortada(self):
        pre = "const esc = s => String(s), nfc = s => s;\n"

        def texto(c):
            return self.run_js("app.js", ["corteTexto"], f"corteTexto({json.dumps(c)})", pre)
        base = {"tipo": "cortada", "nombre": "2026-11-14 10-00-00 - Comité.wav", "a_las": "10:37",
                "motivo": "el disco está lleno"}
        self.assertEqual(texto(dict(base, sigue=True)),
                         "<b>Grabación interrumpida a las 10:37</b> («2026-11-14 10-00-00 - Comité.wav»): el disco "
                         "está lleno. Sigo grabando en la misma carpeta…")
        self.assertTrue(texto(dict(base, sigue=True, continuacion="Alfa/2026-11-14 10-37-02 - Comité (continuación)"
                                   ".wav")).endswith(" Sigue grabando en «2026-11-14 10-37-02 - Comité (continuación)"
                                                     ".wav», en la misma carpeta."))
        self.assertTrue(texto(dict(base, sigue=False, error="No queda sitio")).endswith(
            " <b>Ya no se graba</b>: No queda sitio."))
        self.assertEqual(texto(dict(base, tipo="recuperada", motivo="Tenca se cerró")),
                         "<b>Se recuperó la grabación «2026-11-14 10-00-00 - Comité.wav»</b>, cortada a las 10:37: "
                         "Tenca se cerró.")


if __name__ == "__main__":
    unittest.main()
