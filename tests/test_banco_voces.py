# -*- coding: utf-8 -*-
"""scripts/banco_voces.py (review del 14/11/2026, punto 36): el banco con el que se mide «Quién habla» con
scripts/eval_voces.py. Que la serie encadenada desplaza bien los tiempos, que la reunión montada marca solo la voz y que
el paso por Opus deja el audio alineado con su RTTM."""
import importlib.util
import os
import shutil
import tempfile
import unittest

import numpy as np

from support import REPO

_spec = importlib.util.spec_from_file_location("banco_voces", os.path.join(REPO, "scripts", "banco_voces.py"))
banco = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(banco)
SR = banco.SR

try:
    import av  # noqa: F401
    HAY_AV = True
except ImportError:
    HAY_AV = False


def tono(seg, f=220.0, amp=8000):
    t = np.arange(int(seg * SR)) / SR
    return (amp * np.sin(2 * np.pi * f * t)).astype(np.int16)


def silencio(seg):
    return np.zeros(int(seg * SR), np.int16)


class BancoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tenca-banco-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def p(self, *a):
        return os.path.join(self.tmp, *a)

    def test_voz_marca_solo_lo_que_suena(self):
        x = np.concatenate([silencio(1.0), tono(2.0), silencio(1.0), tono(0.5), silencio(0.1), tono(0.5)])
        tr = banco.voz(x)
        self.assertEqual(len(tr), 2)  # el hueco de 0,1 s se une; el de 1 s no
        self.assertAlmostEqual(tr[0][0], 1.0, delta=0.02)
        self.assertAlmostEqual(tr[0][1], 3.0, delta=0.02)
        self.assertAlmostEqual(tr[1][1] - tr[1][0], 1.1, delta=0.03)
        self.assertEqual(banco.voz(silencio(1.0)), [])

    def test_serie_desplaza_los_tiempos_de_cada_reunion(self):
        for k, (dur, quien) in enumerate(((10.0, "A"), (6.0, "B"))):
            banco.write_wav(self.p(f"r{k}.wav"), tono(dur))
            banco.write_rttm(self.p(f"r{k}.rttm"), f"r{k}", [(1.0, 2.0, quien), (4.0, 1.5, "C")])
        d = banco.serie([(self.p("r0.wav"), self.p("r0.rttm")), (self.p("r1.wav"), self.p("r1.rttm"))],
                        self.p("s.wav"), hueco_s=2.0)
        self.assertAlmostEqual(d, 18.0, places=2)
        self.assertEqual(len(banco.read_wav(self.p("s.wav"))), int(18.0 * SR))
        spans = sorted(banco.read_rttm(self.p("s.rttm")))
        self.assertEqual(spans, [(1.0, 2.0, "A"), (4.0, 1.5, "C"), (13.0, 2.0, "B"), (16.0, 1.5, "C")])

    def montar(self, n=3, semilla=1):
        dirs = []
        for k in range(n):
            d = self.p(f"voz{k}")
            os.makedirs(d, exist_ok=True)
            for j in range(6):
                banco.write_wav(os.path.join(d, f"f{j}.wav"),
                                np.concatenate([silencio(0.3), tono(1.5, 150.0 + 60 * k), silencio(0.4)]))
            dirs.append(d)
        return banco.montar(dirs, self.p("m.wav"), minutos=0.5, semilla=semilla)

    def test_reunion_montada_con_su_rttm(self):
        dur, hablado = self.montar()
        x = banco.read_wav(self.p("m.wav"))
        self.assertAlmostEqual(len(x) / SR, dur, places=2)
        spans = banco.read_rttm(self.p("m.rttm"))
        self.assertEqual({q for _, _, q in spans}, {"voz0", "voz1", "voz2"})
        self.assertTrue(all(a >= 0 and a + d <= dur + 0.01 for a, d, _ in spans))
        # solo la voz: cada frase (1,5 s de tono, sin sus silencios) con ~1,5 s marcados
        self.assertTrue(all(1.3 <= d <= 1.8 for _, d, _ in spans), spans)
        # donde la RTTM dice voz, suena algo
        for a, d, _ in spans[:5]:
            seg = x[int((a + 0.2) * SR):int((a + d - 0.2) * SR)].astype(np.float32)
            self.assertGreater(float(np.sqrt(np.mean(seg * seg))), 1000.0)
        with open(self.p("m.rttm"), encoding="utf-8") as f:
            antes = f.read()
        self.assertEqual(self.montar(semilla=1)[1], hablado)  # la misma semilla, la misma reunión
        with open(self.p("m.rttm"), encoding="utf-8") as f:
            self.assertEqual(f.read(), antes)

    @unittest.skipUnless(HAY_AV, "sin PyAV")
    def test_teams_deja_el_audio_alineado(self):
        x = np.concatenate([silencio(1.0), tono(2.0), silencio(1.0)])
        banco.write_wav(self.p("a.wav"), x)
        banco.write_rttm(self.p("a.rttm"), "a", [(1.0, 2.0, "A")])
        kbps = banco.teams(self.p("a.wav"), self.p("b.wav"), 24)
        y = banco.read_wav(self.p("b.wav"))
        self.assertEqual(len(y), len(x))
        self.assertLess(kbps, 40)
        tr = banco.voz(y)
        self.assertAlmostEqual(tr[0][0], 1.0, delta=0.05)  # el codec no mueve la voz
        self.assertEqual(banco.read_rttm(self.p("b.rttm")), [(1.0, 2.0, "A")])


if __name__ == "__main__":
    unittest.main()
