# -*- coding: utf-8 -*-
"""La transcripción en la nube sin multiplicar peticiones ni hacer esperar al usuario (rama nube-trozos, 16/11/2026):

1. Con una mezcla de idiomas (o detectando), partes de cloud.chunk_minutes, cortadas solo en los cambios de idioma que
   apuntó el directo. Hasta ahora eran de 90 s: 48 peticiones por hora de audio en vez de 6 contra el cupo por minuto
   del proveedor.
2. El cupo: un 429 marca el ritmo de las siguientes peticiones de esa clave, también de las que ya esperaban su turno
   en paralelo, y uno sin Retry-After también.
3. Un repaso final retomado al arrancar va en su propia cola («fondo») y se aparta mientras haya algo del usuario en la
   nube: lo que se pide no espera detrás de él.
El banco de punta a punta (proveedor simulado con 20 peticiones/min) es scripts/nube_trozos_bench.py.
"""
import json
import os
import queue
import threading
import time
import types
import unittest
from unittest import mock

import numpy as np

from support import TempTree, server
from test_directo_auditoria import LiveJobs

import engines  # noqa: E402
import trabajos  # noqa: E402


def silencios(dur, cada=10.0):
    return [(t - 2.0, t) for t in range(int(cada), int(dur), int(cada))]


# ----------------------------------------------------------------------------- 1: partes

class PartesTests(unittest.TestCase):
    plan = staticmethod(engines.CloudEngine.plan_parts)

    def test_una_hora_mezclada_seis_partes(self):
        for lang in ("es-en", "es-en-pt", "en-pt"):
            with self.subTest(lang=lang):
                parts = self.plan(3600.0, silencios(3600.0), 600, lang)
                self.assertEqual(len(parts), 6)  # con partes de 90 s eran 40 o más
                self.assertTrue(all(550 <= b - a <= 650 for a, b, _ in parts), parts)
                self.assertEqual({x for _, _, x in parts}, {lang})
                self.assertEqual((parts[0][0], parts[-1][1]), (0.0, 3600.0))

    def test_corta_solo_en_los_cambios_que_apunto_el_directo(self):
        parts = self.plan(3600.0, silencios(3600.0), 600, "es-en", tramos=[(0.0, "es"), (1505.0, "en")])
        cortes = [b for _, b, _ in parts[:-1]]
        self.assertIn(1509.0, cortes)  # el cambio, en el silencio mas cercano (a 4 s)
        self.assertEqual([x for _, b, x in parts if b <= 1509.0], ["es", "es", "es"])
        self.assertEqual({x for a, _, x in parts if a >= 1509.0}, {"en"})
        self.assertEqual(len(parts), 7)  # 6 partes de 10 min y la del cambio
        for (_, b, _), (a, _, _) in zip(parts, parts[1:]):
            self.assertEqual(b, a)  # sin huecos ni solapes

    def test_detectar_en_partes_de_siempre(self):
        parts = self.plan(1800.0, silencios(1800.0), 600, None, tramos=[(0.0, "en"), (900.0, None)])
        self.assertEqual(len(parts), 4)
        self.assertEqual([x for _, _, x in parts], ["en", "en", None, None])

    def test_el_tamano_lo_pone_chunk_minutes(self):
        parts = self.plan(3600.0, silencios(3600.0), 300, "es-en")
        self.assertEqual(len(parts), 12)


# ----------------------------------------------------------------------------- 2: cupo

class Resp:
    def __init__(self, code, headers=None):
        self.status_code, self.headers = code, dict(headers or {})
        self.text = "{}"

    def json(self):
        if self.status_code == 200:
            return {"text": "hola", "segments": [{"start": 0.0, "end": 1.0, "text": "hola"}]}
        return {"error": {"message": "Rate limit reached for requests"}}


class CupoTests(TempTree):
    CFG = {"provider": "groq", "api_key": "gsk_pruebas_00000000000000"}

    def setUp(self):
        super().setUp()
        self.parte = os.path.join(self.data_dir, "parte.flac")
        with open(self.parte, "wb") as f:
            f.write(b"fLaC" + b"\0" * 64)

    def test_un_429_sin_retry_after_frena_a_todas(self):
        pool = engines.KeyPool()
        reloj = [1000.0]
        pool.clock = lambda: reloj[0]
        k = "gsk_pruebas_00000000000000"
        pool.reserve(k, "whisper")
        pool.note(k, "whisper", 429, {})
        self.assertGreaterEqual(pool.reserve(k, "whisper"), engines.RETRY_SIN_PLAZO - 0.01)
        self.assertAlmostEqual(pool.wait_left(k, "whisper"), engines.RETRY_SIN_PLAZO)

    def test_la_que_esperaba_su_turno_no_sale_antes_del_retry_after(self):
        """Dos partes en paralelo: la primera recibe un 429 con Retry-After cuando la segunda ya tenia su hueco
        reservado. La segunda no sale antes de lo que pidio el proveedor (antes salia y se llevaba otro 429)."""
        cloud = engines.CloudEngine(os.path.join(self.data_dir, "tmp"))
        envios, lock = [], threading.Lock()
        primera, el_429 = [True], []

        def post(url, timeout, headers=None, data=None, files=None):
            with lock:
                envios.append(time.time())
                es_primera, primera[0] = primera[0], False
            if es_primera:
                time.sleep(0.1)  # la segunda ya ha reservado su hueco
                el_429.append(time.time())
                return Resp(429, {"retry-after": "1"})
            return Resp(200)
        res = []
        with mock.patch.object(engines.HTTP, "post", post), \
                mock.patch.dict(engines.RATE_SPACING, {"whisper": 0.3}):
            a = threading.Thread(target=lambda: res.append(cloud._post(self.CFG, self.parte, "es", "", lambda **k: None,
                                                                       lambda: False)))
            a.start()
            time.sleep(0.03)
            b = threading.Thread(target=lambda: res.append(cloud._post(self.CFG, self.parte, "es", "", lambda **k: None,
                                                                       lambda: False)))
            b.start()
            a.join(10)
            b.join(10)
        self.assertEqual(len(res), 2)
        # tras el 429 con Retry-After 1, ninguna otra sale antes de ese segundo (la segunda ya esperaba su hueco)
        self.assertEqual(len(envios), 3)  # la primera, la segunda y el reintento de la primera
        self.assertTrue(all(t >= el_429[0] + 0.95 for t in envios[1:]), [t - el_429[0] for t in envios[1:]])

    def test_mirar_el_hueco_no_se_lo_queda(self):
        pool = engines.KeyPool()
        pool.clock = lambda: 1000.0
        k = "gsk_pruebas_00000000000000"
        pool.reserve(k, "whisper")
        espera = pool.reserve(k, "whisper", max_wait=0.0)  # pasa de max_wait: no se lo queda
        self.assertGreater(espera, 0)
        self.assertEqual(pool.reserve(k, "whisper", max_wait=0.0), espera)
        self.assertEqual(pool.reserve(k, "whisper", max_wait=espera), espera)  # cabe: se lo queda
        self.assertGreater(pool.reserve(k, "whisper"), espera)

    def test_la_que_sale_sin_esperar_no_se_queda_un_hueco(self):
        """Revision 2, P3-3: tras un 429 de otra peticion mientras esta esperaba, con mas de max_wait por delante (el
        directo) sale ya; antes se quedaba ademas un hueco detras del plazo y retrasaba la siguiente de la cola."""
        cloud = engines.CloudEngine(os.path.join(self.data_dir, "tmp"))
        k = self.CFG["api_key"]
        st = {}

        def wait(segundos, cancelled):
            if not st:  # durante la espera de su hueco llega el 429 de otra: 100 s
                st.update(cloud.keys._st(k, "whisper", "m"))
                cloud.keys._st(k, "whisper", "m")["not_before"] = cloud.keys.clock() + 100
                st["last"] = cloud.keys._st(k, "whisper", "m")["last_request"]

        with mock.patch.object(engines.HTTP, "post", lambda *a, **kw: Resp(200)), \
                mock.patch.object(cloud, "_wait", wait), \
                mock.patch.object(cloud.keys, "pick", lambda *a, **kw: k), \
                mock.patch.object(cloud, "endpoint", lambda cfg, key=None, model=None: ("http://x", {}, "m")):
            cloud._post(self.CFG, self.parte, "es", "", lambda **kw: None, lambda: False, max_wait=5.0)
        self.assertEqual(cloud.keys._st(k, "whisper", "m")["last_request"], st["last"])

    def test_las_que_esperaban_no_salen_todas_a_la_vez(self):
        """Revision 1, P1-1: al vencer el Retry-After, las que esperaban (y el reintento de la del 429) salen con el
        ritmo de siempre, no todas en el mismo instante (otra vez 429 cada una)."""
        cloud = engines.CloudEngine(os.path.join(self.data_dir, "tmp"))
        envios, lock = [], threading.Lock()
        el_429 = []

        def post(url, timeout, headers=None, data=None, files=None):
            with lock:
                envios.append(time.time())
                primera = len(envios) == 1
            if primera:
                time.sleep(0.1)  # las otras ya han reservado su hueco
                el_429.append(time.time())
                return Resp(429, {"retry-after": "1"})
            return Resp(200)
        hilos = []
        with mock.patch.object(engines.HTTP, "post", post), \
                mock.patch.dict(engines.RATE_SPACING, {"whisper": 0.3}):
            for k in range(3):
                h = threading.Thread(target=cloud._post, args=(self.CFG, self.parte, "es", "", lambda **k: None,
                                                               lambda: False))
                h.start()
                hilos.append(h)
                time.sleep(0.03)
            for h in hilos:
                h.join(15)
        tras = sorted(t for t in envios[1:])
        self.assertEqual(len(tras), 3)  # las dos que esperaban y el reintento
        self.assertTrue(all(t >= el_429[0] + 0.95 for t in tras), [t - el_429[0] for t in tras])
        huecos = [b - a for a, b in zip(tras, tras[1:])]
        self.assertTrue(all(g >= 0.25 for g in huecos), huecos)  # con el ritmo de siempre (0,3 s), no en rafaga


# ----------------------------------------------------------------------------- 3: el repaso retomado no hace esperar

class FondoTests(LiveJobs):
    """El repaso retomado al arrancar va en la cola «fondo» y se aparta mientras haya algo del usuario en la nube."""

    def jobs(self):
        j = super().jobs()
        j.queues["fondo"] = queue.Queue()
        return j

    def pendiente(self, rel="r.wav"):
        with open(os.path.join(self.data_dir, "repasos_pendientes.json"), "w", encoding="utf-8") as f:
            json.dump({rel: {"local_part": False}}, f)

    def test_el_retomado_va_a_la_cola_fondo(self):
        self.pendiente()
        jobs = self.jobs()
        jobs._resume_final_passes(delay=0)
        fp = [j for j in jobs.list if j.final_pass]
        self.assertEqual(len(fp), 1)
        self.assertTrue(fp[0].fondo)
        self.assertEqual((jobs.queues["fondo"].qsize(), jobs.queues["cloud"].qsize()), (1, 0))
        # el de la reunion que acaba de terminar, no: va con lo del usuario
        jobs._final_pass(self.live_job())
        nuevo = [j for j in jobs.list if j.final_pass and not j.fondo]
        self.assertEqual(len(nuevo), 1)
        self.assertEqual(jobs.queues["cloud"].qsize(), 1)

    def test_nube_delante(self):
        jobs = self.jobs()
        self.assertFalse(jobs._nube_delante())
        fondo = server.Job("a.wav", "a.wav", "nube", None)
        fondo.fondo, fondo.final_pass = True, True
        resumen = server.Job("b.wav", "b.wav", "nube", None)
        resumen.kind = "summary"
        local = server.Job("c.wav", "c.wav", "rapido", None)
        jobs.list += [fondo, resumen, local]
        self.assertFalse(jobs._nube_delante())  # ni el propio repaso, ni un resumen, ni una transcripcion en local
        directo = self.live_job()
        directo.status = "running"
        jobs.list.append(directo)
        self.assertEqual(jobs._nube_delante(), "directo")  # un directo en la nube
        nube = server.Job("d.wav", "d.wav", "nube", None)
        jobs.list.append(nube)
        self.assertEqual(jobs._nube_delante(), "usuario")  # una transcripcion en la nube manda
        directo.status = nube.status = "done"
        self.assertIsNone(jobs._nube_delante())

    def test_ante_un_directo_cede_como_mucho_un_rato(self):
        """Revision 1, P1-3: un directo en la nube podia dejar el repaso de la reunion anterior parado toda la reunion
        (bloqueada, sin destinos ni resumen). Ante un directo, como mucho CEDER_DIRECTO_S; ante lo del usuario, todo."""
        jobs = self.jobs()
        fondo = server.Job("a.wav", "a.wav", "nube", None)
        fondo.fondo = True
        directo = self.live_job()
        directo.status = "running"
        jobs.list += [fondo, directo]
        with mock.patch.object(trabajos, "CEDER_DIRECTO_S", 0.3):
            self.assertEqual(jobs._ceder_fondo(fondo), "directo")
            time.sleep(0.35)
            self.assertIsNone(jobs._ceder_fondo(fondo))
            nube = server.Job("d.wav", "d.wav", "nube", None)
            jobs.list.append(nube)
            self.assertEqual(jobs._ceder_fondo(fondo), "usuario")  # lo del usuario, sin tope

    def test_el_tope_ante_el_directo_cuenta_solo_lo_cedido(self):
        """Revision 2, P3-2: contaba el reloj desde la primera vez que vio un directo; si el directo iba y venia,
        cedia menos de CEDER_DIRECTO_S."""
        jobs = self.jobs()
        fondo = server.Job("a.wav", "a.wav", "nube", None)
        fondo.fondo = True
        directo = self.live_job()
        directo.status = "running"
        jobs.list += [fondo, directo]
        reloj = [100.0]
        falso = types.SimpleNamespace(monotonic=lambda: reloj[0], time=time.time, sleep=time.sleep)
        with mock.patch.object(trabajos, "CEDER_DIRECTO_S", 0.3), mock.patch.object(trabajos, "time", falso):
            self.assertEqual(jobs._ceder_fondo(fondo), "directo")
            reloj[0] += 0.1
            self.assertEqual(jobs._ceder_fondo(fondo), "directo")  # 0,1 s cedidos
            directo.status = "done"
            self.assertIsNone(jobs._ceder_fondo(fondo))
            reloj[0] += 0.5  # sin directo: no cuenta
            directo.status = "running"
            self.assertEqual(jobs._ceder_fondo(fondo), "directo")
            reloj[0] += 0.1
            self.assertEqual(jobs._ceder_fondo(fondo), "directo")  # 0,2 s
            reloj[0] += 0.15
            self.assertIsNone(jobs._ceder_fondo(fondo))  # 0,35 s: se acabo

    def retomado(self, jobs):
        self.pendiente()
        jobs._resume_final_passes(delay=0)
        return jobs.queues["fondo"].get_nowait()

    def test_run_le_pasa_ceder_con_su_tope_y_los_ajustes_de_ahora(self):
        """Revision 2, P2-2: _run tiene que pasar al motor el ceder con el tope (_ceder_fondo) y cfg_fn; antes solo se
        probaban sueltos. Y la pausa: la de antes de empezar no se descuenta, la de a mitad si; el ETA, nunca < 0."""
        jobs = self.jobs()
        job = self.retomado(jobs)
        visto = {}

        def transcribe(path, duration, cfg, language, prompt, job_id, report, cancelled, start=0.0, about=None,
                       tramos_idioma=None, ceder=None, cfg_fn=None):
            visto["ceder"] = ceder()
            jobs.settings.update(lambda d: d["cloud"].update(api_key="gsk_nueva_00000000"))
            visto["cfg"] = cfg_fn()["api_key"]
            report(pausa=50.0)  # antes de empezar: fuera de lo que se mide
            report(stage="Transcribiendo en la nube (0/6 partes)…", frac=0.12, transcribing=True)
            time.sleep(0.3)
            report(pausa=0.2)
            report(frac=0.5)
            visto["pausa_s"], visto["eta"] = job.pausa_s, job.eta
            return [(0.0, 1.0, "hola")], "es", 2.0
        jobs.cloud.transcribe = transcribe
        with mock.patch.object(jobs, "_ceder_fondo", mock.Mock(return_value="usuario")) as cf, \
                mock.patch.object(trabajos, "write_outputs", lambda *a, **k: None), \
                mock.patch.object(trabajos, "meeting_setup", lambda *a, **k: {
                    "prompt": "", "about": "", "language": "es"}):
            jobs._run(job)
        self.assertEqual((job.status, job.error), ("done", None))
        cf.assert_called_with(job)
        self.assertEqual(visto["ceder"], "usuario")
        self.assertEqual(visto["cfg"], "gsk_nueva_00000000")
        self.assertAlmostEqual(visto["pausa_s"], 0.2)
        self.assertGreaterEqual(visto["eta"], 0)

    def test_el_retomado_cancelado_al_apagar_la_nube_lo_dice(self):
        """Revision 2, P3-4: los de la cola decian «Cancelado: la nube se ha desactivado en Ajustes»; el retomado en
        marcha, «Cancelado» a secas."""
        jobs = self.jobs()
        job = self.retomado(jobs)

        def transcribe(path, duration, cfg, language, prompt, job_id, report, cancelled, start=0.0, about=None,
                       tramos_idioma=None, ceder=None, cfg_fn=None):
            jobs.nube_apagada()
            if cancelled():
                raise engines.Cancelled()
            return [(0.0, 1.0, "hola")], "es", 2.0
        jobs.cloud.transcribe = transcribe
        with mock.patch.object(trabajos, "write_outputs", lambda *a, **k: None), \
                mock.patch.object(trabajos, "meeting_setup", lambda *a, **k: {
                    "prompt": "", "about": "", "language": "es"}):
            jobs._run(job)
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(job.stage, "Cancelado: la nube se ha desactivado en Ajustes")
        self.assertIn("r.wav", jobs.after)  # con el texto del directo: destinos y resumen

    def test_sin_cola_fondo_no_cede(self):
        """Revision 1, P3-1: un Jobs sin la cola «fondo» lo pone en la de siempre y entonces no debe ceder (lo que pide
        el usuario iria detras de el en la misma cola: bloqueo mutuo)."""
        self.pendiente()
        jobs = super().jobs()  # sin "fondo"
        jobs._resume_final_passes(delay=0)
        fp = [j for j in jobs.list if j.final_pass]
        self.assertEqual(len(fp), 1)
        self.assertFalse(fp[0].fondo)
        self.assertEqual(jobs.queues["cloud"].qsize(), 1)

    def test_apagar_la_nube_cancela_el_repaso_retomado_en_marcha(self):
        """Revision 1, P1-2: en pausa el repaso esta «running»; antes nube_apagada solo cancelaba lo que estaba en cola
        y, al soltarse la pausa, subia la reunion entera con la nube ya apagada."""
        jobs = self.jobs()
        fondo = server.Job("a.wav", "a.wav", "nube", None)
        fondo.fondo, fondo.final_pass, fondo.status = True, True, "running"
        otro = server.Job("b.wav", "b.wav", "nube", None)
        otro.status = "running"
        jobs.list += [fondo, otro]
        jobs.nube_apagada()
        self.assertTrue(fondo.cancel)
        self.assertFalse(otro.cancel)  # lo que el usuario puso en marcha, como antes

    def test_lo_que_pide_el_usuario_no_espera_al_repaso_retomado(self):
        """De punta a punta con los hilos de las colas: el repaso retomado (A, 6 partes de 0,3 s) ya va por la
        primera parte cuando el usuario pide B (2 partes). B termina antes que A, y A se aparta mientras tanto."""
        self.wav("b.wav", seconds=2.0)
        self.pendiente()
        jobs = self.jobs()
        orden, lock = [], threading.Lock()

        def transcribe(path, duration, cfg, language, prompt, job_id, report, cancelled, start=0.0, about=None,
                       tramos_idioma=None, ceder=None, **kw):
            nombre = os.path.basename(path)
            for k in range(6 if nombre == "r.wav" else 2):
                while ceder is not None and ceder():
                    with lock:
                        if not orden or orden[-1] != (nombre, "pausa"):
                            orden.append((nombre, "pausa"))
                    time.sleep(0.05)
                time.sleep(0.3)
                with lock:
                    orden.append((nombre, k))
            return [(0.0, 1.0, "hola")], "es", 2.0
        jobs.cloud.transcribe = transcribe
        with mock.patch.object(trabajos, "write_outputs", lambda *a, **k: None), \
                mock.patch.object(trabajos, "meeting_setup", lambda *a, **k: {
                    "prompt": "", "about": "", "language": "es"}):
            for kind in ("cloud", "fondo"):
                threading.Thread(target=jobs._worker, args=(kind,), daemon=True).start()
            jobs._resume_final_passes(delay=0)
            fin = time.time() + 10
            while time.time() < fin and not orden:
                time.sleep(0.02)
            jobs.submit(["b.wav"], "nube", "es")
            fin = time.time() + 20
            while time.time() < fin and not all(j.status in ("done", "error", "cancelled") for j in jobs.list):
                time.sleep(0.05)
        estados = {j.path: (j.status, j.error) for j in jobs.list}
        self.assertEqual(estados, {"r.wav": ("done", None), "b.wav": ("done", None)})
        fin_b = max(i for i, x in enumerate(orden) if x[0] == "b.wav")
        fin_a = max(i for i, x in enumerate(orden) if x[0] == "r.wav")
        self.assertLess(fin_b, fin_a, orden)
        self.assertIn(("r.wav", "pausa"), orden)


class CederEnCloudEngineTests(TempTree):
    def test_ninguna_parte_sale_mientras_hay_que_ceder(self):
        cloud = engines.CloudEngine(os.path.join(self.data_dir, "tmp"))
        cede, enviadas, etapas = [True], [], []

        def post(cfg, chunk, lang, prompt, report, stop, about=None):
            enviadas.append(time.time())
            return {"text": "hola", "segments": [{"start": 0.0, "end": 1.0, "text": "hola"}]}

        def soltar():
            time.sleep(0.5)
            cede[0] = False
        y = np.zeros(16000 * 1200, np.int16)
        with mock.patch.object(cloud, "_post", post), mock.patch.object(cloud, "endpoint", lambda *a, **k: None), \
                mock.patch.object(engines, "decode_16k", lambda *a, **k: y.copy()), \
                mock.patch.object(engines, "write_flac", lambda path, data, sr=16000: open(path, "wb").close()):
            threading.Thread(target=soltar, daemon=True).start()
            t0 = time.time()
            cloud.transcribe("x.wav", 1200.0, {"chunk_minutes": 10, "parallel": 2}, "es", "", "j1",
                             lambda **k: etapas.append(k.get("stage")), lambda: False, ceder=lambda: cede[0])
        self.assertEqual(len(enviadas), 2)
        self.assertTrue(all(t - t0 >= 0.45 for t in enviadas), [t - t0 for t in enviadas])
        self.assertIn("En pausa: primero lo que acabas de pedir…", etapas)

    def test_en_pausa_no_codifica_y_al_volver_relee_los_ajustes(self):
        """Revision 1, P1-2 y P2-2: la parte no se codifica (ni queda en disco) mientras se cede; al volver se usan los
        ajustes de ahora (cfg_fn) y se dice cuanto se ha esperado (report pausa=…)."""
        cloud = engines.CloudEngine(os.path.join(self.data_dir, "tmp"))
        cede, flacs, cfgs, pausas = [True], [], [], []

        def post(cfg, chunk, lang, prompt, report, stop, about=None):
            cfgs.append(cfg.get("api_key"))
            return {"text": "hola", "segments": [{"start": 0.0, "end": 1.0, "text": "hola"}]}

        def write_flac(path, data, sr=16000):
            flacs.append(time.time())
            open(path, "wb").close()

        def report(**k):
            if k.get("pausa"):
                pausas.append(k["pausa"])

        def soltar():
            time.sleep(0.5)
            cede[0] = False
        y = np.zeros(16000 * 1200, np.int16)
        with mock.patch.object(cloud, "_post", post), mock.patch.object(cloud, "endpoint", lambda *a, **k: None), \
                mock.patch.object(engines, "decode_16k", lambda *a, **k: y.copy()), \
                mock.patch.object(engines, "write_flac", write_flac):
            threading.Thread(target=soltar, daemon=True).start()
            t0 = time.time()
            cloud.transcribe("x.wav", 1200.0, {"chunk_minutes": 10, "parallel": 2, "api_key": "vieja"}, "es", "",
                             "j3", report, lambda: False, ceder=lambda: "usuario" if cede[0] else None,
                             cfg_fn=lambda: {"chunk_minutes": 10, "parallel": 2, "api_key": "nueva"})
        self.assertTrue(flacs and all(t - t0 >= 0.45 for t in flacs), [t - t0 for t in flacs])
        self.assertEqual(cfgs, ["nueva", "nueva"])
        self.assertTrue(pausas and max(pausas) >= 0.4, pausas)

    def test_pausa_a_mitad_no_codifica_y_sigue_con_la_clave_nueva(self):
        """Revision 2, P2-2: la pausa dentro de una parte (no solo al principio): esa parte no se codifica mientras
        espera y, al volver, las que quedan usan los ajustes de ahora."""
        cloud = engines.CloudEngine(os.path.join(self.data_dir, "tmp"))
        cede, flacs, claves, pausas, lock = [False], [], [], [], threading.Lock()
        soltado = []

        def post(cfg, chunk, lang, prompt, report, stop, about=None):
            with lock:
                claves.append(cfg.get("api_key"))
                primera = len(claves) == 1
            if primera:  # tras la primera parte llega algo del usuario: 0,5 s
                cede[0] = True

                def soltar():
                    time.sleep(0.5)
                    soltado.append(time.time())
                    cede[0] = False
                threading.Thread(target=soltar, daemon=True).start()
            return {"text": "hola", "segments": [{"start": 0.0, "end": 1.0, "text": "hola"}]}

        def write_flac(path, data, sr=16000):
            flacs.append(time.time())
            open(path, "wb").close()

        def report(**k):
            if k.get("pausa"):
                pausas.append(k["pausa"])
        y = np.zeros(16000 * 1800, np.int16)
        with mock.patch.object(cloud, "_post", post), mock.patch.object(cloud, "endpoint", lambda *a, **k: None), \
                mock.patch.object(engines, "decode_16k", lambda *a, **k: y.copy()), \
                mock.patch.object(engines, "write_flac", write_flac):
            cloud.transcribe("x.wav", 1800.0, {"chunk_minutes": 10, "parallel": 1, "api_key": "vieja"}, "es", "",
                             "j4", report, lambda: False, ceder=lambda: "usuario" if cede[0] else None,
                             cfg_fn=lambda: {"chunk_minutes": 10, "parallel": 1, "api_key": "nueva"})
        self.assertEqual(len(flacs), 3)
        self.assertGreaterEqual(flacs[1], soltado[0] - 0.01)  # la segunda, codificada al soltarse, no antes
        self.assertEqual(claves, ["vieja", "nueva", "nueva"])
        self.assertEqual(len(pausas), 1)

    def test_una_pausa_se_cuenta_una_vez_con_partes_en_paralelo(self):
        """Revision 2, P2-1 y P3-1: con 3 partes a la vez en pausa, cada una decia su espera y se descontaba el triple
        (ETA negativo, velocidad guardada inflada); y la etapa «En pausa…» la pisaba «Transcribiendo (k/n)»."""
        cloud = engines.CloudEngine(os.path.join(self.data_dir, "tmp"))
        cede, ev, n, lock = [False], [], [0], threading.Lock()

        def post(cfg, chunk, lang, prompt, report, stop, about=None):
            with lock:
                n[0] += 1
                primera = n[0] == 1
            if primera:  # la primera trae algo del usuario (1 s); las demas tardan 0,4 s: terminan en plena pausa
                cede[0] = True

                def soltar():
                    time.sleep(1.0)
                    cede[0] = False
                threading.Thread(target=soltar, daemon=True).start()
            time.sleep(0.05 if primera else 0.4)
            return {"text": "hola", "segments": [{"start": 0.0, "end": 1.0, "text": "hola"}]}

        def report(**k):
            with lock:
                if k.get("pausa"):
                    ev.append(("pausa", k["pausa"]))
                if k.get("stage"):
                    ev.append(("etapa", k["stage"]))
        y = np.zeros(16000 * 3600, np.int16)
        with mock.patch.object(cloud, "_post", post), mock.patch.object(cloud, "endpoint", lambda *a, **k: None), \
                mock.patch.object(engines, "decode_16k", lambda *a, **k: y.copy()), \
                mock.patch.object(engines, "write_flac", lambda path, data, sr=16000: open(path, "wb").close()):
            t0 = time.time()
            cloud.transcribe("x.wav", 3600.0, {"chunk_minutes": 10, "parallel": 3}, "es", "", "j5", report,
                             lambda: False, ceder=lambda: "usuario" if cede[0] else None)
            total = time.time() - t0
        pausas = [v for k, v in ev if k == "pausa"]
        self.assertTrue(pausas, ev)
        self.assertLess(sum(pausas), 2.0, pausas)  # una pausa de 1-1,5 s, una vez (antes, una por parte: 3-4,5 s)
        self.assertLess(sum(pausas), total, (pausas, total))
        # entre «En pausa…» y la vuelta, ningun «Transcribiendo (k/n)» de las partes que terminan entretanto
        i = ev.index(("etapa", engines.CloudEngine.PAUSAS["usuario"]))
        j = next(k for k in range(i, len(ev)) if ev[k][0] == "pausa")
        self.assertEqual([e for e in ev[i:j] if e[1].startswith("Transcribiendo")], [], ev)
        self.assertTrue(ev[j + 1][1].startswith("Transcribiendo"), ev)  # y la vuelta, con lo hecho

    def test_cancelar_mientras_cede(self):
        cloud = engines.CloudEngine(os.path.join(self.data_dir, "tmp"))
        with mock.patch.object(cloud, "endpoint", lambda *a, **k: None), self.assertRaises(engines.Cancelled):
            cloud.transcribe("x.wav", 10.0, {}, "es", "", "j2", lambda **k: None, lambda: True, ceder=lambda: True)


if __name__ == "__main__":
    unittest.main()
