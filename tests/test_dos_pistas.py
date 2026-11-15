# -*- coding: utf-8 -*-
"""Grabación en dos pistas (punto 20 del review del 14/11/2026, pistas.py): el micro a la izquierda y lo que suena a
la derecha, con la marca en la cabecera. Lo que se comprueba:

- el formato: la cabecera la leen el directo, la duración, `wave` y el reproductor (la misma marca); los WAV de antes
  no la llevan;
- el directo no cambia: la suma de las dos pistas es la mezcla de antes (también con el limitador) y un directo de
  verdad (live.run con reloj falso) manda a la nube los mismos trozos con el mismo audio;
- la grabadora real (tarjeta falsa) con --pistas: canales, cabecera cerrada en su sitio y la línea de «Tú» alineada;
- RecorderProcess guarda esa línea y la da como la de «Tú» (desfase 0);
- el motor lee la suma o un canal (PyAV), y voices sin PyAV;
- «Quién habla» en dos pistas (con una diarización de mentira): los demás aparte, «Tú» del micro, el eco y la voz que
  vuelve quitados, la reunión presencial por la suma, y la huella de cada uno de su canal;
- exportar la mezcla y el reproductor (pistas-worklet.js en node) hacen lo mismo.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import wave
from unittest import mock

import numpy as np

from support import APP, REPO, live, wav_bytes  # noqa: F401  (support prepara sys.path)
import common  # noqa: E402
import engines  # noqa: E402
import pistas  # noqa: E402
import recorder  # noqa: E402
import recording  # noqa: E402
import voices  # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_soundcard")
try:
    import av  # noqa: F401
    HAY_AV = True
except ImportError:
    HAY_AV = False


def dual_wav(path, left, right, rate=48000, open_header=False):
    """Un WAV de dos pistas como el de la grabadora (left/right: float en [-1, 1])."""
    pcm = (np.clip(np.stack([left, right], axis=1), -1, 1) * 32767).astype("<i2").tobytes()
    head = bytearray(pistas.cabecera(rate))
    if not open_header:
        struct.pack_into("<I", head, 4, len(head) - 8 + len(pcm))
        struct.pack_into("<I", head, len(head) - 4, len(pcm))
    with open(path, "wb") as f:
        f.write(bytes(head) + pcm)
    return path


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tenca-pistas-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def p(self, name):
        return os.path.join(self.tmp, name)


# ----------------------------------------------------------------------------- formato

class FormatoTests(Tmp):
    def test_la_cabecera_la_leen_todos(self):
        x = np.sin(np.arange(48000) / 10).astype(np.float32) * 0.3
        path = dual_wav(self.p("r.wav"), x, -x)
        info = live.wav_info(path)
        self.assertTrue(info["pistas"])
        self.assertEqual((info["channels"], info["rate"], info["bits"]), (2, 48000, 16))
        self.assertEqual(info["data_off"], len(pistas.cabecera()))
        self.assertEqual(info["data_size"], 48000 * 4)
        self.assertAlmostEqual(common.wav_duration(path, os.path.getsize(path)), 1.0, places=3)
        with wave.open(path, "rb") as w:  # cualquier programa: un WAV normal
            self.assertEqual((w.getnchannels(), w.getframerate(), w.getnframes()), (2, 48000, 48000))
        self.assertTrue(pistas.es_doble(path))
        self.assertEqual(len(pistas.cabecera()) % 2, 0)

    def test_abierta_mientras_graba(self):
        path = dual_wav(self.p("r.wav"), np.zeros(4800), np.zeros(4800), open_header=True)
        info = live.wav_info(path)
        self.assertTrue(info["pistas"])
        self.assertIsNone(info["data_size"])
        self.assertTrue(pistas.es_doble(path))
        recording.close_wav(path)  # la grabadora murio sin cerrarla: se cierra en su sitio, no en el byte 40
        info = live.wav_info(path)
        self.assertEqual(info["data_size"], 4800 * 4)
        with open(path, "rb") as f:
            self.assertEqual(struct.unpack("<I", f.read(8)[4:])[0], os.path.getsize(path) - 8)
        self.assertTrue(pistas.es_doble(path))

    def test_los_de_antes_y_otros_no(self):
        cases = {
            "antes.wav": wav_bytes(1.0),  # la grabadora de antes: estereo sin marca
            "mono.wav": pistas.cabecera().replace(struct.pack("<HH", 1, 2), struct.pack("<HH", 1, 1), 1),
            "marca_en_el_audio.wav": wav_bytes(0.1, data=pistas.MARCA * 10),
            "corta.wav": pistas.cabecera()[:30],
        }
        for name, data in cases.items():
            with open(self.p(name), "wb") as f:
                f.write(data)
            self.assertFalse(pistas.es_doble(self.p(name)), name)
            self.assertFalse((live.wav_info(self.p(name)) or {}).get("pistas"), name)
        self.assertFalse(pistas.es_doble(self.p("no-existe.wav")))
        with open(self.p("r.flac"), "wb") as f:
            f.write(pistas.cabecera())
        self.assertFalse(pistas.es_doble(self.p("r.flac")))  # solo WAV

    def test_la_grabadora_de_antes_sigue_igual(self):
        self.assertEqual(len(recorder.wav_header()), 44)


# ----------------------------------------------------------------------------- el directo no cambia

def bloques(seed, n_blocks=200, loud=False):
    """Partes de la grabadora al azar: voz del micro, reunion y eco, a veces muy fuerte (el limitador actua)."""
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n_blocks):
        n = int(rng.integers(400, 3000))
        t = np.arange(n) / 48000 + i
        mic = (np.sin(2 * np.pi * 180 * t) * (0.02 + 0.03 * (i % 7 < 3))).astype(np.float32)
        sysm = (np.sin(2 * np.pi * 120 * t) * (0.2 if i % 5 < 3 else 0.0)).astype(np.float32)
        if loud and i % 11 == 0:
            mic *= 40
        elif loud and i % 13 == 0:  # los dos a la vez por encima del fondo de escala, no (ver test_limite)
            sysm *= 6
        mic = mic + 0.05 * np.roll(sysm, 30)
        out.append({"mic": np.repeat(mic[:, None], 2, axis=1),
                    "sys": np.stack([sysm, sysm * 0.9], axis=1).astype(np.float32)})
    return out


class LaSumaEsLaMezclaTests(unittest.TestCase):
    def comparar(self, seed, loud=False, mute_every=None):
        mk = lambda: {"mic": recorder.MicLeveler(*recorder.GAIN_RANGE["mic"]),  # noqa: E731
                      "sys": recorder.Leveler(*recorder.GAIN_RANGE["sys"])}
        old, new = mk(), mk()
        peor = 0.0
        for i, parts in enumerate(bloques(seed, loud=loud)):
            muted = bool(mute_every and i % mute_every == 0)
            a = recorder.mezclar({k: v.copy() for k, v in parts.items()}, old, muted)
            b, mine = recorder.mezclar_pistas({k: v.copy() for k, v in parts.items()}, new, muted)
            # el archivo guarda int16: se compara lo que leera el directo de cada uno
            qa = (a * 32767).astype("<i2").astype(np.float32) / 32768.0
            qb = (b * 32767).astype("<i2").astype(np.float32) / 32768.0
            viejo = qa.mean(axis=1)
            nuevo = pistas.a_mono(qb)
            peor = max(peor, float(np.abs(viejo - nuevo).max()))
            self.assertEqual(mine, False if muted else new["mic"].mine)
        return peor

    def test_igual_que_antes(self):
        self.assertLess(self.comparar(1), 2e-4)

    def test_igual_con_el_limitador_actuando(self):
        # un grito con +18 dB o un aviso muy fuerte: antes, el limitador sobre la suma; ahora se deshace el de cada
        # canal antes de sumar (sin eso, hasta 0,048 de diferencia)
        self.assertLess(self.comparar(2, loud=True), 2e-3)

    def test_igual_con_el_micro_silenciado(self):
        self.assertLess(self.comparar(3, mute_every=4), 2e-4)

    def test_limite(self):
        """Lo que no se puede: los dos canales por encima del fondo de escala a la vez y con signo contrario (un grito
        con +18 dB mientras suena algo saturado). Cada uno se guardó ya en 1 y la suma de antes no se recupera; son
        milisegundos y solo en lo que lee el directo."""
        q = np.array([[0.99997, -0.99997]], np.float32)
        self.assertLess(abs(float(pistas.a_mono(q)[0])), 0.01)

    def test_canales(self):
        lev = {"mic": recorder.MicLeveler(*recorder.GAIN_RANGE["mic"]),
               "sys": recorder.Leveler(*recorder.GAIN_RANGE["sys"])}
        n = 4800
        parts = {"mic": np.zeros((n, 2), np.float32), "sys": np.full((n, 2), 0.1, np.float32)}
        parts["sys"][:, 1] = 0.3
        b, _ = recorder.mezclar_pistas(parts, lev)
        self.assertEqual(float(np.abs(b[:, 0]).max()), 0.0)  # el micro callado: la izquierda, en silencio
        self.assertGreater(float(np.abs(b[:, 1]).max()), 0.0)  # lo que suena, en mono a la derecha
        sin_micro = recorder.mezclar_pistas({"sys": parts["sys"]}, {"sys": recorder.Leveler(-9, 9)})[0]
        self.assertEqual(float(np.abs(sin_micro[:, 0]).max()), 0.0)

    def test_soft_unlimit_deshace_el_limitador(self):
        x = np.linspace(-1.6, 1.6, 2001).astype(np.float32)
        y = pistas.soft_limit(x.copy())
        np.testing.assert_allclose(pistas.soft_unlimit(y), x, atol=2e-4)


class TuTests(unittest.TestCase):
    def test_pasos_alineados(self):
        tu = recorder.PasosTu()
        paso = tu.paso
        # 0,25 s de voz, 0,25 s callado, 0,1 s de voz (< 30 %) y el resto callado, en bloques de tamaño raro
        pat = [(paso, True), (paso, False), (int(0.1 * paso * 4), True), (paso - int(0.1 * paso * 4), False)]
        for n, mine in pat:
            done = 0
            while done < n:
                k = min(777, n - done)
                tu.add(k, mine)
                done += k
        i, s = tu.take()
        self.assertEqual((i, s), (0, "1010"[:2] + "1"[:0] + ("1" if 0.4 >= tu.UMBRAL else "0") + "0"[:0]))
        tu.add(paso // 2, True)
        self.assertEqual(tu.take(), (3, ""))  # medio paso: aún no
        tu.cerrar()
        self.assertEqual(tu.take(), (3, "1"))  # al parar, el que quedaba a medias
        tu.add(paso, False)
        self.assertEqual(tu.take(), (4, "0"))

    def test_umbral(self):
        tu = recorder.PasosTu()
        tu.add(int(tu.paso * 0.2), True)
        tu.add(tu.paso - int(tu.paso * 0.2), False)
        tu.add(int(tu.paso * 0.35), True)
        tu.add(tu.paso - int(tu.paso * 0.35), False)
        self.assertEqual(tu.take()[1], "01")

    def test_tramos_de_bits(self):
        self.assertEqual(pistas.tramos_de_bits(""), [])
        self.assertEqual(pistas.tramos_de_bits("0000"), [])
        # un paso suelto (0,25 s) no llega a min_s 0,3; dos seguidos sí; un hueco de 0,5 s se une
        self.assertEqual(pistas.tramos_de_bits("0100"), [])
        self.assertEqual(pistas.tramos_de_bits("0110"), [(0.15, 0.85)])
        self.assertEqual(pistas.tramos_de_bits("11001100000011"), [(0.0, 1.6), (2.9, 3.6)])


class RecorderProcessTests(unittest.TestCase):
    def rp(self):
        r = recording.RecorderProcess.__new__(recording.RecorderProcess)
        r.path, r.levels, r.tu = "x.wav", [], bytearray()
        return r

    def test_la_linea_de_la_grabadora(self):
        r = self.rp()
        r.note_levels({"t": 0.5, "mic": {"db": -20}, "sys": {"db": -60}, "tu": "01", "tu_i": 0})
        r.note_levels({"t": 0.75, "mic": {"db": -20}, "sys": {"db": -60}, "tu": "1", "tu_i": 2})
        r.note_levels({"t": 1.5, "mic": {"db": -20}, "sys": {"db": -60}, "tu": "11", "tu_i": 5})  # se perdio una
        tl = r.timeline()
        self.assertEqual(tl, {"paso": 0.25, "activo": "0110011", "desfase": 0.0, "fuente": "grabadora"})
        self.assertTrue(voices.micro_de_grabadora(tl))
        self.assertEqual(len(r.levels), 3)  # los niveles siguen (los usa «Probar el micro»)

    def test_lo_raro_no_se_apunta(self):
        r = self.rp()
        for st in ({"t": 1, "tu": "12", "tu_i": 0}, {"t": 1, "tu": "1", "tu_i": -1}, {"t": 1, "tu": "1", "tu_i": "0"},
                   {"t": 1, "tu": 1, "tu_i": 0}, {"t": 1, "tu": "1", "tu_i": 10 ** 8}):
            r.note_levels(st)
        self.assertEqual(bytes(r.tu), b"")

    def test_grabadora_de_antes_niveles(self):
        r = self.rp()
        for k in range(8):
            r.note_levels({"t": k * 0.25, "mic": {"db": -20 if k < 4 else -70}, "sys": {"db": -60}})
        tl = r.timeline()
        self.assertNotIn("fuente", tl)
        self.assertFalse(voices.micro_de_grabadora(tl))
        self.assertEqual(tl["activo"], "11110000")

    def test_pide_dos_pistas_al_grabar_y_no_al_medir(self):
        seen = []

        class P:
            stdout = iter(())

            def __init__(self, args, **k):
                seen.append(args)

            def poll(self):
                return 0
        with tempfile.TemporaryDirectory() as d, mock.patch.object(recording.subprocess, "Popen", P):
            recording.RecorderProcess(os.path.join(d, "r.wav"), "default", "all", APP, d)
            recording.RecorderProcess(None, "default", "all", APP, d)
        self.assertIn("--pistas", seen[0])
        self.assertNotIn("--pistas", seen[1])
        self.assertIn("--monitor", seen[1])


# ----------------------------------------------------------------------------- la grabadora de verdad

class GrabadoraRealTests(unittest.TestCase):
    """recorder.py --pistas con la tarjeta falsa: la reunión (MEET) suena 3 s de cada 5 y el usuario (MINE) habla de
    3,2 a 4,8 s de cada 5; el micro lleva su voz y el eco de la reunión (0,1 a 30 ms)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="tenca-pistas-rec-")
        cls.wav = os.path.join(cls.tmp, "r.wav")
        env = dict(os.environ, PYTHONPATH=FAKE, PYTHONIOENCODING="utf-8")
        p = subprocess.Popen([sys.executable, "-u", os.path.join(APP, "recorder.py"), cls.wav, "--mic", "default",
                              "--sys", "all", "--pistas"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env, cwd=APP)
        out, err = [], []
        readers = [threading.Thread(target=lambda f=f, b=b: b.extend(f), daemon=True)
                   for f, b in ((p.stdout, out), (p.stderr, err))]
        for t in readers:
            t.start()
        time.sleep(16)
        p.stdin.write(b"stop\n")
        p.stdin.flush()
        p.wait(timeout=60)
        for t in readers:
            t.join(timeout=10)
        cls.rc = p.returncode
        cls.log = b"".join(err).decode("utf-8", "replace")
        cls.lines = [json.loads(line) for line in b"".join(out).decode("utf-8", "replace").splitlines() if line.strip()]
        r = recording.RecorderProcess.__new__(recording.RecorderProcess)
        r.path, r.levels, r.tu = cls.wav, [], bytearray()
        for st in cls.lines:
            r.note_levels(st)
        cls.tl = r.timeline()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_archivo(self):
        self.assertEqual(self.rc, 0, self.log)
        info = live.wav_info(self.wav)
        self.assertTrue(info["pistas"])
        self.assertEqual(info["data_size"], os.path.getsize(self.wav) - info["data_off"])  # cerrada en su sitio
        self.assertEqual(info["data_size"] % 4, 0)
        self.assertGreater(info["data_size"] / info["byte_rate"], 12)

    def canales(self):
        info = live.wav_info(self.wav)
        with open(self.wav, "rb") as f:
            f.seek(info["data_off"])
            x = np.frombuffer(f.read(info["data_size"]), "<i2").reshape(-1, 2).astype(np.float32) / 32768
        return x

    def rms(self, x, t, a, b):
        sel = np.zeros(len(x), bool)
        for k in range(int(t / 5) + 1):
            sel[int((5 * k + a) * 48000):int((5 * k + b) * 48000)] = True
        sel[:int(6 * 48000)] = False  # los primeros segundos: la puerta de eco aun no lo sabe
        sel[len(x) - 48000:] = False
        return float(np.sqrt(np.mean(x[sel[:len(x)]] ** 2)))

    def test_cada_uno_en_su_canal(self):
        x = self.canales()
        dur = len(x) / 48000
        # el tiempo del archivo va 1 s (LAT) por detras del reloj de la tarjeta falsa, mas lo que tarda en arrancar:
        # se mide por niveles, no por instantes exactos
        izq, der = x[:, 0], x[:, 1]
        self.assertGreater(float(np.abs(izq).max()), 0.05)
        self.assertGreater(float(np.abs(der).max()), 0.05)
        self.assertFalse(np.allclose(izq, der))  # ya no es la misma mezcla en los dos
        # la reunion suena por la derecha; por la izquierda solo su eco, muy por debajo
        r_meet = np.sqrt(np.mean(der[int(6 * 48000):] ** 2))
        self.assertGreater(r_meet, 0.01)
        self.assertGreater(dur, 12)

    def test_la_linea_de_tu(self):
        self.assertEqual(self.rc, 0, self.log)
        self.assertEqual(self.tl["fuente"], "grabadora")
        bits = self.tl["activo"]
        st = [s for s in self.lines if "tu" in s]
        self.assertTrue(st)
        # indices seguidos: cada estado continua donde acabo el anterior
        pos = 0
        for s in st:
            self.assertEqual(s["tu_i"], pos)
            pos += len(s["tu"])
        info = live.wav_info(self.wav)
        self.assertAlmostEqual(len(bits) * 0.25, info["data_size"] / info["byte_rate"], delta=0.3)
        # su voz en la izquierda coincide con los «1»: energia de la izquierda en los pasos 1 frente a los 0
        x = self.canales()[:, 0]
        n = 12000
        e = np.array([np.sqrt(np.mean(x[i * n:(i + 1) * n] ** 2)) for i in range(min(len(bits), len(x) // n))])
        b = np.array([c == "1" for c in bits[:len(e)]])
        tarde = np.arange(len(e)) >= 32  # desde los 8 s, con la puerta de eco ya convencida
        self.assertGreater(b[tarde].sum(), 8, bits)
        self.assertGreater((~b[tarde]).sum(), 8, bits)
        self.assertGreater(np.median(e[tarde & b]), 3 * np.median(e[tarde & ~b]), (bits, e.round(3).tolist()))
        # con la reunion sonando y él callado (solo eco en el micro), casi nunca «1»
        x_r = self.canales()[:, 1]
        er = np.array([np.sqrt(np.mean(x_r[i * n:(i + 1) * n] ** 2)) for i in range(len(e))])
        eco = tarde & (er > 0.02) & (e < er / 4)
        if eco.sum() >= 4:
            self.assertLessEqual(b[eco].mean(), 0.25, bits)


# ----------------------------------------------------------------------------- un directo de verdad (reloj falso)

class DirectoIgualTests(unittest.TestCase):
    """live.run sobre una grabacion que crece, una vez con la mezcla de antes y otra con las dos pistas de la misma
    reunion: los mismos trozos, a la misma hora y con el mismo audio."""

    def run_live(self, dos_pistas):
        import test_directo as td
        tmp = tempfile.mkdtemp(prefix="tenca-pistas-dir-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "reunion.wav")
        sr = td.SR
        spans_tu, spans_ellos = [(0.0, 3.4), (8.2, 11.3)], [(4.0, 7.6), (11.9, 21.9), (22.6, 24.0)]
        tu, ellos = td.synth(spans_tu, 26.0) * 0.5, td.synth(spans_ellos, 26.0) * 0.5
        rec = td.FakeRecorder(path, tu + ellos)
        if dos_pistas:  # misma reunion, cada uno en su canal, con la cabecera de la grabadora
            pcm = (np.clip(np.stack([tu, ellos], axis=1), -1, 1) * 32767).astype("<i2").tobytes()
            head = pistas.cabecera(sr)
            rec.pcm = pcm
            with open(path, "wb") as f:
                f.write(head)

            def sleep(dt, rec=rec, off=len(head)):
                with rec.lock:
                    rec.t += dt
                    want = min(len(rec.pcm), int((rec.t - rec.t0) * sr) * 4)
                    if want > rec.written:
                        with open(rec.path, "ab") as f:
                            f.write(rec.pcm[rec.written:want])
                        rec.written = want
                    if rec.written >= len(rec.pcm) and not rec.closed:
                        rec.closed = True
                        with open(rec.path, "r+b") as f:
                            f.seek(4)
                            f.write(struct.pack("<I", off - 8 + len(rec.pcm)))
                            f.seek(off - 4)
                            f.write(struct.pack("<I", len(rec.pcm)))
            rec.sleep = sleep
        else:  # la de antes: la mezcla en los dos canales
            mix = np.clip(tu + ellos, -1, 1)
            rec.pcm = (np.stack([mix, mix], axis=1) * 32767).astype("<i2").tobytes()
            with open(path, "wb") as f:
                f.write(wav_bytes(0, open_header=True, rate=sr, channels=2, data=b""))

            def sleep(dt, rec=rec):
                with rec.lock:
                    rec.t += dt
                    want = min(len(rec.pcm), int((rec.t - rec.t0) * sr) * 4)
                    if want > rec.written:
                        with open(rec.path, "ab") as f:
                            f.write(rec.pcm[rec.written:want])
                        rec.written = want
                    if rec.written >= len(rec.pcm) and not rec.closed:
                        rec.closed = True
                        with open(rec.path, "r+b") as f:
                            f.seek(4)
                            f.write(struct.pack("<I", 36 + len(rec.pcm)))
                            f.seek(40)
                            f.write(struct.pack("<I", len(rec.pcm)))
            rec.sleep = sleep
        cloud = td.FakeCloud(rec, 0.8)
        audios = []
        orig = cloud.transcribe_array

        def grabar_audio(cfg, audio, *a, **k):
            audios.append(np.array(audio, np.float32))
            return orig(cfg, audio, *a, **k)
        cloud.transcribe_array = grabar_audio
        job = td.DirectoJob(preset="nube")
        opts = {"preset": "nube", "language": None, "prompt": None, "threads": 1, "worker": td.FakeWorker(rec),
                "cloud": cloud, "cloud_cfg": {"provider": "groq", "api_key": "k1"}, "tmp_dir": tmp, "resume": [],
                "summarizer": td.FakeSummarizer()}
        fake_time = types.SimpleNamespace(time=rec.time, sleep=rec.sleep)
        with mock.patch.object(live, "time", fake_time), mock.patch.object(live, "speech", td.energy_vad):
            rec.sleep(0.6)
            segs, lang = live.run(job, path, opts, lambda s: None, lambda m: None)
        return segs, cloud.calls, audios

    def test_mismos_trozos_mismo_audio(self):
        segs_a, calls_a, aud_a = self.run_live(False)
        segs_b, calls_b, aud_b = self.run_live(True)
        self.assertGreaterEqual(len(calls_a), 4)
        self.assertEqual([(round(c["secs"], 3), round(c["at"], 3)) for c in calls_a],
                         [(round(c["secs"], 3), round(c["at"], 3)) for c in calls_b])
        for a, b in zip(aud_a, aud_b):
            self.assertEqual(len(a), len(b))
            self.assertLess(float(np.abs(a - b).max()), 2e-4)
        self.assertEqual(segs_a, segs_b)


# ----------------------------------------------------------------------------- el motor y voices: la suma o un canal

class LecturaTests(Tmp):
    def wavs(self):
        rng = np.random.default_rng(4)
        left = (rng.standard_normal(48000 * 3) * 0.05).astype(np.float32)
        right = (np.sin(np.arange(48000 * 3) * 2 * np.pi * 300 / 48000) * 0.2).astype(np.float32)
        d = dual_wav(self.p("d.wav"), left, right)
        mix = np.clip(left + right, -1, 1)
        pcm = (np.stack([mix, mix], axis=1) * 32767).astype("<i2").tobytes()
        with open(self.p("antes.wav"), "wb") as f:
            f.write(wav_bytes(data=pcm))
        return d, self.p("antes.wav")

    @unittest.skipUnless(HAY_AV, "sin PyAV")
    def test_decode_16k(self):
        d, antes = self.wavs()
        suma, viejo = engines.decode_16k(d), engines.decode_16k(antes)
        self.assertEqual(suma.shape, viejo.shape)
        self.assertLessEqual(int(np.abs(suma.astype(int) - viejo.astype(int)).max()), 2)  # la mezcla de siempre
        izq, der = engines.decode_16k(d, canal=0), engines.decode_16k(d, canal=1)
        ambos = engines.decode_16k(d, canal="ambos")
        self.assertEqual(ambos.shape, (len(suma), 2))
        np.testing.assert_array_equal(ambos[:, 0], izq)
        np.testing.assert_array_equal(ambos[:, 1], der)
        self.assertLessEqual(int(np.abs(izq.astype(int) + der.astype(int) - suma.astype(int)).max()), 2)
        self.assertLess(np.std(izq) / 32768, 0.08)  # el ruido a la izquierda, el tono a la derecha
        self.assertGreater(np.abs(np.fft.rfft(der[:16000]))[300], 10 * np.abs(np.fft.rfft(izq[:16000]))[300])
        # cualquier otro audio: canal no cambia nada
        np.testing.assert_array_equal(engines.decode_16k(antes, canal=1), viejo)
        with open(self.p("dest.pcm"), "wb") as f:
            n = engines.decode_16k(d, destino=f, canal="ambos")
        self.assertEqual(n, len(suma))
        self.assertEqual(os.path.getsize(self.p("dest.pcm")), n * 4)

    @unittest.skipUnless(HAY_AV, "sin PyAV")
    def test_decode_rangos(self):
        d, _ = self.wavs()
        dur = len(engines.decode_16k(d)) / 16000
        for canal in (None, 0, 1):
            entera = engines.decode_16k(d, canal=canal)
            [tramo] = engines.decode_rangos_16k(d, [(1.0, 2.0)], dur, canal=canal)
            self.assertLessEqual(int(np.abs(tramo[100:-100].astype(int) - entera[16100:31900].astype(int)).max()), 2)

    def test_voices_sin_pyav(self):
        d, antes = self.wavs()
        with mock.patch.dict(sys.modules, {"engines": None}):
            suma = voices.load_audio_16k(d)
            viejo = voices.load_audio_16k(antes)
            ambos = voices.load_audio_16k(d, canal="ambos")
            izq = voices.load_audio_16k(d, canal=0)
        self.assertEqual(ambos.shape, (len(suma), 2))
        np.testing.assert_array_equal(ambos[:, 0], izq)
        self.assertLessEqual(int(np.abs(suma.astype(int) - viejo.astype(int)).max()), 2)

    @unittest.skipUnless(HAY_AV, "sin PyAV")
    def test_voices_en_disco(self):
        d, _ = self.wavs()
        m = voices.load_audio_16k(d, en_disco=self.p("v.pcm"), canal="ambos")
        self.assertEqual(m.shape[1], 2)
        s = voices._suma_en_disco(m, self.p("s.pcm"), trozo=5000)
        np.testing.assert_array_equal(np.asarray(s), pistas.a_mono_i16(np.asarray(m)))
        del m, s


# ----------------------------------------------------------------------------- «Quién habla» en dos pistas

def micro(bits):
    return {"paso": 0.25, "activo": bits, "desfase": 0.0, "fuente": "grabadora"}


def bits_de(tramos, total_s):
    b = ["0"] * int(total_s / 0.25)
    for a, z in tramos:
        for i in range(int(a / 0.25), int(z / 0.25)):
            b[i] = "1"
    return "".join(b)


class DiarizarPistasTests(unittest.TestCase):
    """diarize_pistas con una diarización de mentira: cada llamada recibe un canal (izquierda compactada o derecha) y
    devuelve los grupos que el test decide."""

    TOTAL = 200.0

    def estereo(self, voz_der=True):
        n = int(self.TOTAL * voices.SR)
        x = np.zeros((n, 2), np.int16)
        if voz_der:  # 100 s de voz de los demás a -26 dB
            x[:int(100 * voices.SR), 1] = 1600
        x[:, 0] = 3
        return x

    def correr(self, x, mic, salidas):
        llamadas = []

        def falso(samples16, *a, **k):
            llamadas.append(len(samples16))
            return salidas[len(llamadas) - 1](samples16)
        with mock.patch.object(voices, "diarize", falso):
            res = voices.diarize_pistas(x, mic, "datos")
        return res, llamadas

    def test_los_demas_aparte_y_tu_del_micro(self):
        mios = [(110.0, 130.0), (150.0, 160.0)]
        x = self.estereo()

        def der(s):
            return {"tramos": [{"inicio": 0.0, "fin": 60.0, "h": "h1"}, {"inicio": 60.0, "fin": 100.0, "h": "h2"}],
                    "hablantes": {}, "huellas": {"h1": [1.0] + [0.0] * 191, "h2": [0.0, 1.0] + [0.0] * 190}}

        def izq(s):
            # lo compactado: 20 + 0,3 + 10 s (los tramos con su margen)
            self.assertLess(len(s) / voices.SR, 31.5)
            return {"tramos": [{"inicio": 0.0, "fin": len(s) / voices.SR, "h": "h1"}], "hablantes": {},
                    "huellas": {"h1": [0.0, 0.0, 1.0] + [0.0] * 189}}
        mic = micro(bits_de(mios, self.TOTAL))
        res, llamadas = self.correr(x, mic, [der, izq])
        self.assertEqual(llamadas[0], len(x))  # la derecha entera
        self.assertTrue(res["pistas"])
        por = {h: i for h, i in res["hablantes"].items()}
        self.assertEqual(sorted(i["pista"] for i in por.values()), ["micro", "sistema", "sistema"])
        tu_h = [h for h, i in por.items() if i["pista"] == "micro"][0]
        tramos_tu = [(t["inicio"], t["fin"]) for t in res["tramos"] if t["h"] == tu_h]
        self.assertEqual(tramos_tu, [(109.9, 130.1), (149.9, 160.1)])  # de vuelta a sus tiempos (el hueco no cuenta)
        self.assertEqual(voices.mark_user(res["tramos"], mic), tu_h)

    def test_una_sala_no_da_tu(self):
        mios = [(110.0, 140.0), (150.0, 180.0)]

        def der(s):
            return {"tramos": [{"inicio": 0.0, "fin": 100.0, "h": "h1"}], "hablantes": {},
                    "huellas": {"h1": [1.0] + [0.0] * 191}}

        def izq(s):  # en el micro, dos voces: tú y quien está a tu lado
            m = len(s) / voices.SR / 2
            return {"tramos": [{"inicio": 0.0, "fin": m, "h": "h1"}, {"inicio": m, "fin": 2 * m, "h": "h2"}],
                    "hablantes": {}, "huellas": {"h1": [0.0, 1.0] + [0.0] * 190, "h2": [0.0, 0.0, 1.0] + [0.0] * 189}}
        mic = micro(bits_de(mios, self.TOTAL))
        res, _ = self.correr(self.estereo(), mic, [der, izq])
        self.assertIsNone(voices.mark_user(res["tramos"], mic, skip=set()))  # como antes: sin su huella, nadie

    def test_el_eco_del_micro_se_quita(self):
        # el micro dio «1» con la reunión sonando (la puerta aún no lo sabía) y sale un grupo con la voz de h1
        mios = [(10.0, 40.0), (120.0, 140.0)]

        def der(s):
            return {"tramos": [{"inicio": 0.0, "fin": 100.0, "h": "h1"}], "hablantes": {},
                    "huellas": {"h1": [1.0] + [0.0] * 191}}

        def izq(s):
            corte = 30.2 / 1  # lo compactado: primero 10-40 (con margen, 30,2 s), luego el hueco y 120-140
            return {"tramos": [{"inicio": 0.0, "fin": corte, "h": "h1"},
                               {"inicio": corte + 0.3, "fin": len(s) / voices.SR, "h": "h2"}],
                    "hablantes": {}, "huellas": {"h1": [0.99, 0.14] + [0.0] * 190, "h2": [0.0, 0.0, 1.0] + [0.0] * 189}}
        mic = micro(bits_de(mios, self.TOTAL))
        with mock.patch.object(voices, "log") as lg:
            res, _ = self.correr(self.estereo(), mic, [der, izq])
        micro_h = [h for h, i in res["hablantes"].items() if i["pista"] == "micro"]
        self.assertEqual(len(micro_h), 1)
        tr = [(t["inicio"], t["fin"]) for t in res["tramos"] if t["h"] == micro_h[0]]
        self.assertEqual(tr, [(119.9, 140.1)])  # el de 10-40 era eco
        self.assertTrue(any("eco de los demas" in str(c) for c in lg.call_args_list))
        self.assertEqual(voices.mark_user(res["tramos"], mic), micro_h[0])

    def test_tu_voz_que_vuelve_se_quita(self):
        mios = [(100.0, 160.0)]
        x = self.estereo()
        x[int(100 * voices.SR):int(160 * voices.SR), 0] = 1600  # tu voz en el micro
        x[int(100 * voices.SR):int(160 * voices.SR), 1] = 400  # y más floja (-12 dB), por la llamada

        def der(s):
            return {"tramos": [{"inicio": 0.0, "fin": 100.0, "h": "h1"}, {"inicio": 100.2, "fin": 160.0, "h": "h2"}],
                    "hablantes": {}, "huellas": {"h1": [1.0] + [0.0] * 191, "h2": [0.0, 0.98, 0.2] + [0.0] * 189}}

        def izq(s):
            return {"tramos": [{"inicio": 0.0, "fin": len(s) / voices.SR, "h": "h1"}], "hablantes": {},
                    "huellas": {"h1": [0.0, 1.0] + [0.0] * 190}}
        mic = micro(bits_de(mios, self.TOTAL))
        res, _ = self.correr(x, mic, [der, izq])
        pistas_ = sorted(i["pista"] for i in res["hablantes"].values())
        self.assertEqual(pistas_, ["micro", "sistema"])  # sin el «sistema» con tu voz
        tu = voices.mark_user(res["tramos"], mic)
        self.assertEqual(res["hablantes"][tu]["pista"], "micro")

    def test_hablar_a_la_vez_no_es_eco(self):
        # tú, al nivel de ellos, mientras hablan (el micro va a su nivel): ni se quita tu grupo ni el de ellos, aunque
        # la huella se parezca (una huella de pocos segundos puede parecerse)
        mios = [(20.0, 50.0)]
        x = self.estereo()
        x[int(20 * voices.SR):int(50 * voices.SR), 0] = 1600

        def der(s):
            return {"tramos": [{"inicio": 0.0, "fin": 100.0, "h": "h1"}], "hablantes": {},
                    "huellas": {"h1": [1.0] + [0.0] * 191}}

        def izq(s):
            return {"tramos": [{"inicio": 0.0, "fin": len(s) / voices.SR, "h": "h1"}], "hablantes": {},
                    "huellas": {"h1": [0.9, 0.43] + [0.0] * 190}}
        mic = micro(bits_de(mios, self.TOTAL))
        res, _ = self.correr(x, mic, [der, izq])
        self.assertEqual(sorted(i["pista"] for i in res["hablantes"].values()), ["micro", "sistema"])

    def test_presencial_y_sin_linea_por_la_suma(self):
        mic = micro(bits_de([(10.0, 50.0)], self.TOTAL))
        res, llamadas = self.correr(self.estereo(voz_der=False), mic, [])
        self.assertIsNone(res)
        self.assertEqual(llamadas, [])
        viejo = {"paso": 0.25, "activo": "0110"}  # la de niveles (grabadora de antes): no vale
        self.assertIsNone(self.correr(self.estereo(), viejo, [])[0])
        self.assertIsNone(self.correr(self.estereo()[:, 0], mic, [])[0])  # un solo canal

    def test_sin_voz_en_el_micro(self):
        def der(s):
            return {"tramos": [{"inicio": 0.0, "fin": 100.0, "h": "h1"}], "hablantes": {},
                    "huellas": {"h1": [1.0] + [0.0] * 191}}
        res, llamadas = self.correr(self.estereo(), micro(bits_de([(120.0, 121.0)], self.TOTAL)), [der])
        self.assertEqual(len(llamadas), 1)  # 1,2 s de micro: no se diariza
        self.assertEqual([i["pista"] for i in res["hablantes"].values()], ["sistema"])

    def test_compactar(self):
        x = np.arange(100 * voices.SR, dtype=np.int64).astype(np.int16)
        comp, mapa = voices.compactar(x, [(1.0, 2.0), (5.0, 5.5), (99.9, 120.0)])
        with tempfile.TemporaryDirectory() as d:
            m, mapa2 = voices.compactar(x, [(1.0, 2.0), (5.0, 5.5), (99.9, 120.0)], en_disco=os.path.join(d, "c.pcm"))
            self.assertIsInstance(m, np.memmap)
            np.testing.assert_array_equal(np.asarray(m), comp)
            self.assertEqual(mapa2, mapa)
            del m
        self.assertEqual(len(comp), int((1.0 + 0.3 + 0.5 + 0.3 + 0.1) * voices.SR))
        self.assertEqual([(round(a, 2), round(b, 2), round(d, 2)) for a, b, d in mapa],
                         [(0.0, 1.0, 1.0), (1.3, 5.0, 0.5), (2.1, 99.9, 0.1)])
        back = voices.descompactar([{"inicio": 0.5, "fin": 1.6, "h": "h1"}, {"inicio": 2.1, "fin": 9.0, "h": "h2"}],
                                   mapa)
        self.assertEqual(back, [{"inicio": 1.5, "fin": 2.0, "h": "h1"}, {"inicio": 5.0, "fin": 5.3, "h": "h1"},
                                {"inicio": 99.9, "fin": 100.0, "h": "h2"}])


class MainPistasTests(Tmp):
    """voices.main sobre un WAV en dos pistas: el sidecar sale con los hablantes de cada canal y «Tú», y «Recordar»
    calcula la huella del canal de ese hablante."""

    def setUp(self):
        super().setUp()
        self.wav = dual_wav(self.p("r.wav"), np.zeros(48000 * 4), np.zeros(48000 * 4))
        self.side = voices.sidecar_path(self.wav)
        self.mic = micro(bits_de([(1.0, 2.0)], 4.0))
        voices.write_micro(self.side, self.mic)

    def main(self, *args):
        out = []
        with mock.patch.object(voices, "models_ready", return_value=True), \
                mock.patch.object(os, "dup2"), mock.patch.object(os, "fdopen") as fd:
            fd.return_value.write = lambda b: out.append(b)
            rc = voices.main(["--diarize", self.wav, "--out", self.side, "--data", self.tmp, *args])
        return rc, [json.loads(b.decode("utf-8")) for b in out]

    def test_diariza_las_dos_pistas(self):
        visto = {}

        def dp(stereo, mic, *a, **k):
            visto["shape"], visto["mic"] = stereo.shape, mic
            return {"tramos": [{"inicio": 1.0, "fin": 2.0, "h": "h1"}, {"inicio": 2.0, "fin": 3.5, "h": "h2"}],
                    "hablantes": {"h1": {"segundos": 1.0, "breve": True, "pista": "micro"},
                                  "h2": {"segundos": 1.5, "breve": True, "pista": "sistema"}},
                    "huellas": {}, "duracion_s": 4.0, "pistas": True}
        with mock.patch.object(voices, "diarize_pistas", dp), mock.patch.object(voices, "diarize") as d:
            rc, msgs = self.main()
        self.assertEqual(rc, 0, msgs)
        d.assert_not_called()
        self.assertEqual(visto["shape"][1], 2)
        self.assertEqual(visto["mic"], self.mic)
        data = voices.read_sidecar(self.side)
        self.assertTrue(data["pistas"])
        self.assertEqual(data["hablantes"]["h1"]["pista"], "micro")
        self.assertEqual(data["micro"], self.mic)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "tmp")), [])  # sin temporales

    def test_presencial_diariza_la_suma(self):
        def d(samples, *a, **k):
            self.assertEqual(np.asarray(samples).ndim, 1)
            return {"tramos": [], "hablantes": {}, "huellas": {}, "duracion_s": 4.0}
        with mock.patch.object(voices, "diarize_pistas", return_value=None), mock.patch.object(voices, "diarize", d):
            rc, msgs = self.main()
        self.assertEqual(rc, 0, msgs)
        self.assertNotIn("pistas", voices.read_sidecar(self.side))
        self.assertEqual(os.listdir(os.path.join(self.tmp, "tmp")), [])

    def test_recordar_usa_su_canal(self):
        data = voices.read_sidecar(self.side)
        data.update(tramos=[{"inicio": 0.5, "fin": 3.0, "h": "h1"}, {"inicio": 0.5, "fin": 3.0, "h": "h2"},
                            {"inicio": 0.5, "fin": 3.0, "h": "h3"}],
                    hablantes={"h1": {"pista": "micro"}, "h2": {"pista": "sistema"}, "h3": {}}, duracion_s=4.0)
        voices.write_sidecar(self.side, data)
        canales = []

        def ah(path, tramos, dur, max_s=90, en_disco=None, canal=None):
            canales.append(canal)
            return np.ones(16000, np.int16), True
        so = types.SimpleNamespace()
        with mock.patch.object(voices, "audio_huella", ah), mock.patch.object(voices, "_sherpa", return_value=so), \
                mock.patch.object(voices, "_extractor"), mock.patch.object(voices, "embed", return_value=np.ones(192)):
            for h in ("h1", "h2", "h3"):
                rc, msgs = self.main("--fingerprint", h)
                self.assertEqual(rc, 0, msgs)
        self.assertEqual(canales, [0, 1, None])

    def test_unir_dos_de_canales_distintos(self):
        data = {"tramos": [{"inicio": 0.0, "fin": 1.0, "h": "h1"}, {"inicio": 2.0, "fin": 3.0, "h": "h2"},
                           {"inicio": 4.0, "fin": 5.0, "h": "h3"}],
                "hablantes": {"h1": {"pista": "micro"}, "h2": {"pista": "sistema"}, "h3": {"pista": "sistema"}}}
        voices.join_speakers(data, "h3", "h2")
        self.assertEqual(data["hablantes"]["h2"]["pista"], "sistema")
        voices.join_speakers(data, "h2", "h1")
        self.assertNotIn("pista", data["hablantes"]["h1"])  # de los dos: su huella, de la suma
        self.assertIsNone(voices.canal_de(data, "h1"))


# ----------------------------------------------------------------------------- exportar y el reproductor

def eco_prueba(sr=48000, segundos=6.0):
    """Izquierda: tu voz de 0,5 a 2 s y el eco de los demás (16 dB por debajo, 30 ms) de 2,5 a 5,5 s; derecha: los
    demás de 2,5 a 5,5 s. De 4 a 4,5 s hablas a la vez que ellos."""
    n = int(segundos * sr)
    t = np.arange(n) / sr
    rng = np.random.default_rng(9)
    ellos = (rng.standard_normal(n) * 0.08 * ((t >= 2.5) & (t < 5.5))).astype(np.float32)
    tu = (np.sin(2 * np.pi * 200 * t) * 0.1 * (((t >= 0.5) & (t < 2)) | ((t >= 4) & (t < 4.5)))).astype(np.float32)
    eco = np.roll(ellos, int(0.03 * sr)) * 10 ** (-16 / 20)
    return np.stack([tu + eco, ellos], axis=1).astype(np.float32)


class ExportarTests(Tmp):
    def test_sin_tocar_es_la_suma(self):
        x = eco_prueba()
        path = dual_wav(self.p("r.wav"), x[:, 0], x[:, 1])
        dst = self.p("m.wav")
        segs = pistas.exportar_mezcla(path, dst, eco=False, bloque_s=0.7)
        self.assertAlmostEqual(segs, 6.0, places=3)
        with wave.open(dst, "rb") as w:
            self.assertEqual((w.getnchannels(), w.getframerate(), w.getnframes()), (1, 48000, len(x)))
            y = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
        q = (np.clip(x, -1, 1) * 32767).astype("<i2").astype(np.float32) / 32768
        np.testing.assert_allclose(y, pistas.a_mono(q), atol=1e-4)
        self.assertFalse(os.path.exists(dst + ".part"))

    def test_quita_el_eco_y_no_tu_voz(self):
        x = eco_prueba()
        g = pistas.ganancias_eco(x)
        sr = 48000
        tramo = lambda a, b: g[int(a * sr):int(b * sr)]  # noqa: E731
        self.assertTrue(np.all(tramo(0.6, 1.9) > 0.99))       # tú sola
        self.assertLess(float(np.median(tramo(2.7, 3.9))), 0.2)  # solo el eco: -18 dB
        self.assertTrue(np.all(tramo(4.1, 4.45) > 0.9))       # hablando a la vez, pasa
        self.assertTrue(np.all(tramo(5.62, 5.9) < 0.2))       # la cola del eco, tras callarse ellos
        self.assertTrue(np.all(tramo(0.0, 0.4) == 1.0))
        # por bloques da lo mismo que de una vez (exportar_mezcla va en bloques de 10 s: ventanas enteras); con
        # bloques que parten ventanas, como mucho una ventana distinta en cada corte
        for paso, tol in ((960 * 7, 1e-6), (1234, 1.0)):
            est, partes = {}, []
            for i in range(0, len(x), paso):
                partes.append(pistas.ganancias_eco(x[i:i + paso], estado=est))
            y = np.concatenate(partes)
            self.assertEqual(len(y), len(g))
            dif = np.abs(y - g) > 1e-4
            self.assertLessEqual(dif.sum(), (len(x) // paso + 1) * 960, paso)
            if tol < 1:
                np.testing.assert_allclose(y, g, atol=tol)

    def test_peticion(self):
        x = eco_prueba()
        path = dual_wav(self.p("r.wav"), x[:, 0], x[:, 1])
        esc = os.path.join(self.tmp, "Escritorio")
        os.makedirs(esc)
        r = pistas.exportar_peticion(path, esc, {"tu": "0.5", "demas": 9, "eco": False})
        self.assertEqual(r["nombre"], "r (mezclado).wav")
        r2 = pistas.exportar_peticion(path, esc, {})
        self.assertEqual(r2["nombre"], "r (mezclado 2).wav")
        with wave.open(r["destino"], "rb") as w:
            y = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
        q = (np.clip(x, -1, 1) * 32767).astype("<i2").astype(np.float32) / 32768
        np.testing.assert_allclose(y, pistas.mezclar_bloque(q, 0.5, 2.0, False), atol=1e-4)  # 9 → 2 (el tope)
        with open(self.p("antes.wav"), "wb") as f:
            f.write(wav_bytes(1.0))
        with self.assertRaisesRegex(ValueError, "no está grabado en dos pistas"):
            pistas.exportar_peticion(self.p("antes.wav"), esc, {})
        with self.assertRaisesRegex(ValueError, "se está grabando"):
            pistas.exportar_peticion(path, esc, {}, grabando=path)
        with self.assertRaisesRegex(ValueError, "No encuentro la carpeta"):
            pistas.exportar_peticion(path, os.path.join(self.tmp, "no"), {})

    def test_grabando_y_cancelar(self):
        x = eco_prueba()
        path = dual_wav(self.p("r.wav"), x[:, 0], x[:, 1], open_header=True)
        with open(path, "ab") as f:
            f.write(b"\0\0")  # a medias de una muestra
        dst = self.p("m.wav")
        self.assertAlmostEqual(pistas.exportar_mezcla(path, dst), 6.0, places=3)
        with self.assertRaises(InterruptedError):
            pistas.exportar_mezcla(path, self.p("c.wav"), cancelled=lambda: True)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["m.wav", "r.wav"])


class RutaExportarTests(Tmp):
    """POST /api/audio/mezclado (app_grabar.Grabar.audio_mezclado) por el Handler de verdad: al Escritorio, y no la
    que se esta grabando ni un audio de antes."""

    def test_ruta(self):
        import app_grabar
        import server
        from support import patched_paths
        root = self.p("Tenca")
        os.makedirs(os.path.join(root, "Alfa"))
        esc = self.p("Escritorio")
        os.makedirs(esc)
        x = eco_prueba()
        wav = dual_wav(os.path.join(root, "Alfa", "r.wav"), x[:, 0], x[:, 1])
        with open(os.path.join(root, "Alfa", "antes.wav"), "wb") as f:
            f.write(wav_bytes(1.0))
        app = server.App.__new__(server.App)
        app.rec = None
        with patched_paths(ROOT=root), mock.patch.object(app_grabar, "desktop_dir", return_value=esc):
            r = app.audio_mezclado({"path": "Alfa/r.wav", "tu": 1, "demas": 1, "eco": False})
            self.assertEqual(r["nombre"], "r (mezclado).wav")
            self.assertTrue(os.path.isfile(os.path.join(esc, "r (mezclado).wav")))
            with self.assertRaisesRegex(server.ApiError, "no está grabado en dos pistas"):
                app.audio_mezclado({"path": "Alfa/antes.wav"})
            app.rec = types.SimpleNamespace(path=wav, alive=lambda: True)
            with self.assertRaisesRegex(server.ApiError, "se está grabando"):
                app.audio_mezclado({"path": "Alfa/r.wav"})
            with self.assertRaises(server.ApiError):
                app.audio_mezclado({"path": "../fuera.wav"})
        with open(os.path.join(APP, "api.py"), encoding="utf-8") as f:
            self.assertIn('"/api/audio/mezclado": a.audio_mezclado', f.read())


NODE = shutil.which("node")


@unittest.skipUnless(NODE, "sin node")
class ReproductorTests(Tmp):
    """pistas-worklet.js (el reproductor) en node, con un AudioWorkletProcessor de mentira: lo que sale es
    pistas.mezclar_bloque (lo que exporta) con una ventana de retraso, con y sin quitar el eco."""

    def worklet(self, x, msgs, sr=48000):
        np.save(self.p("x.npy"), x.astype(np.float32))
        raw = self.p("x.f32")
        x.astype("<f4").tofile(raw)
        js = r"""
const fs = require('fs'), vm = require('vm');
let Proc = null;
const ctx = { sampleRate: %d, console, Math, Float32Array,
  AudioWorkletProcessor: class { constructor() { this.port = { postMessage() {}, onmessage: null }; } },
  registerProcessor: (n, c) => { Proc = c; } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(%s, 'utf8'), ctx);
const p = new Proc();
for (const m of %s) p.port.onmessage({ data: m });
const buf = new Float32Array(fs.readFileSync(%s).buffer.slice(0));
const n = buf.length / 2, out = new Float32Array(n * 2);
for (let i = 0; i < n; i += 128) {
  const k = Math.min(128, n - i), L = new Float32Array(k), R = new Float32Array(k);
  for (let j = 0; j < k; j++) { L[j] = buf[2 * (i + j)]; R[j] = buf[2 * (i + j) + 1]; }
  const o = [new Float32Array(k), new Float32Array(k)];
  p.process([[L, R]], [o]);
  for (let j = 0; j < k; j++) { out[2 * (i + j)] = o[0][j]; out[2 * (i + j) + 1] = o[1][j]; }
}
fs.writeFileSync(%s, Buffer.from(out.buffer));
"""
        js = js % (sr, json.dumps(os.path.join(APP, "static", "pistas-worklet.js")), json.dumps(msgs), json.dumps(raw),
                   json.dumps(self.p("y.f32")))
        with open(self.p("w.js"), "w", encoding="utf-8") as f:
            f.write(js)
        subprocess.run([NODE, self.p("w.js")], check=True, timeout=120)
        return np.fromfile(self.p("y.f32"), "<f4").reshape(-1, 2)

    def test_como_al_exportar(self):
        x = eco_prueba()
        w = int(0.02 * 48000)
        for msg, args in (({"modo": "doble"}, (1.0, 1.0, True)),
                          ({"modo": "doble", "eco": False, "tu": 0.5, "demas": 1.5}, (0.5, 1.5, False)),
                          ({"modo": "doble", "tu": 0}, (0.0, 1.0, True))):
            y = self.worklet(x, [msg])
            np.testing.assert_allclose(y[:, 0], y[:, 1])  # mono en los dos oídos
            ref = pistas.mezclar_bloque(x, *args)
            np.testing.assert_allclose(y[w:, 0], ref[:len(x) - w], atol=2e-3, err_msg=str(msg))

    def test_normal_pasa_tal_cual(self):
        x = eco_prueba()
        y = self.worklet(x, [{"modo": "normal"}])
        np.testing.assert_allclose(y, x, atol=1e-7)


if __name__ == "__main__":
    unittest.main()
