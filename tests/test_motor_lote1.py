# -*- coding: utf-8 -*-
"""Lote 1 del motor (review del 14/11/2026): mezcla de tres idiomas (54), avisos por el globo de la bandeja (57) y el
repaso en la nube cortado por idioma (35). «Quién habla» (36) se mide con scripts/eval_voces.py y scripts/banco_voces.py
(test_banco_voces.py).
"""
import base64
import os
import shutil
import tempfile
import unittest
from unittest import mock

import numpy as np

from support import APP  # noqa: F401  (pone _app en sys.path)
import bandeja  # noqa: E402
import directo_avisos  # noqa: E402
import engines  # noqa: E402
import plan  # noqa: E402
import recording  # noqa: E402
import test_bandeja  # noqa: E402
import test_idioma_directo as tid  # noqa: E402  (como módulo: sus tests no se repiten aquí)


# ----------------------------------------------------------------------------- 54 · español + inglés + portugués

class MezclaDeTresTests(unittest.TestCase):
    def test_es_una_mezcla_de_los_tres(self):
        self.assertEqual(engines.lang_pair("es-en-pt"), ("es", "en", "pt"))
        self.assertEqual(engines.mix_code({"pt", "en", "es"}), "es-en-pt")
        self.assertEqual(engines.mix_code(("pt", "es")), "es-pt")  # las de dos, como antes
        self.assertIsNone(engines.mix_code(("es",)))
        self.assertIsNone(engines.mix_code(("es", "fr")))

    def test_frase_de_estilo_con_los_tres_idiomas(self):
        frase = engines.style_for("es-en-pt")
        self.assertTrue(frase)
        for palabra in ("¿no?", "let's", "vamos"):  # una frase en cada idioma
            self.assertIn(palabra, frase)
        self.assertNotEqual(frase, engines.style_for("es-en"))
        # cabe en el prompt de la nube junto a un vocabulario normal (Groq: <= 896 bytes)
        self.assertLess(len(frase.encode("utf-8")), engines.CLOUD_PROMPT_BYTES // 3)

    def test_la_nube_detecta_con_la_frase_mezclada(self):
        lang, prompt = engines.CloudEngine.prompt_for("es-en-pt", "Alfa, Beta")
        self.assertIsNone(lang)  # la API no tiene mezclas: detecta
        self.assertTrue(prompt.startswith(engines.style_for("es-en-pt")))

    def test_en_local_se_detecta_en_cada_bloque_solo_entre_los_tres(self):
        # el mismo reparto que worker.lang_mode: una mezcla -> (sin idioma fijo, multilingual, permitidos)
        with open(f"{APP}/worker.py", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("pair = engines.lang_pair(value)", src)
        self.assertIn("return None, True, pair", src)
        inner = mock.Mock()
        inner.detect_language.return_value = [[("<|gl|>", 0.5), ("<|pt|>", 0.3), ("<|en|>", 0.1), ("<|es|>", 0.1)]]
        import worker
        out = worker._OnlyLanguages(inner, engines.lang_pair("es-en-pt")).detect_language(None)
        self.assertEqual(out[0][0][0], "<|pt|>")  # el gallego no gana: solo entre es, en y pt

    def test_ajustes_y_la_reunion_la_aceptan(self):
        for code in ("es-en-pt", "es-en", "es", "auto"):
            self.assertTrue(engines.IDIOMA_RE.match(code), code)
            self.assertTrue(plan.IDIOMA_RE.match(code), code)
        for code in ("es-en-pt-fr", "es-", "español", "xx-yy-zz", "es-fr-it"):
            self.assertFalse(engines.IDIOMA_RE.match(code), code)
            self.assertFalse(plan.IDIOMA_RE.match(code), code)
        self.assertEqual(plan.idioma_txt("es-en-pt"), "Español + inglés + portugués")
        self.assertFalse(plan.idioma_fijo("es-en-pt"))

    def test_el_directo_no_propone_otro_idioma_con_la_mezcla_de_tres(self):
        p = directo_avisos.Propuesta()
        for _ in range(6):
            p.oir("es-en-pt", [("pt", 0.9, 30.0)])
        self.assertIsNone(p.publica())
        self.assertEqual(directo_avisos._mezcla("es", "pt"), "es-pt")
        self.assertIsNone(directo_avisos._mezcla("es", "es"))


# ----------------------------------------------------------------------------- 57 · avisos con el icono de la app

class AvisoPorElGloboTests(unittest.TestCase):
    def setUp(self):
        test_bandeja.Icon.falla = False
        self.popen = []
        p1 = mock.patch.object(recording, "WINDOWS", True)
        p2 = mock.patch.object(recording.subprocess, "Popen", lambda *a, **k: self.popen.append((a, k)))
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def bandeja(self, form=None):
        b = bandeja.Bandeja("tenca.ico", {"abrir": lambda: None, "grabar": lambda: None, "salir": lambda: None},
                            lambda: {}, wf=test_bandeja.wf())
        self.addCleanup(b.close)
        self.assertTrue(b.create(form or test_bandeja.Form()))
        return b

    def test_con_el_icono_puesto_el_aviso_sale_por_su_globo(self):
        b = self.bandeja()
        recording.notify("● Grabando en Tenca", "Alfa · General")
        self.assertEqual(b.icon.balloons, [("● Grabando en Tenca", "Alfa · General")])
        self.assertEqual(self.popen, [])  # sin PowerShell

    def test_sin_icono_por_powershell_como_antes(self):
        recording.notify("● Grabando", "Alfa")
        self.assertEqual(len(self.popen), 1)
        (argv,), _ = self.popen[0]
        self.assertEqual(argv[0], "powershell")

    def test_quitado_el_icono_vuelve_a_powershell(self):
        b = self.bandeja()
        b.close()
        recording.notify("■ Grabación guardada", "Alfa")
        self.assertEqual(b.icon, None)
        self.assertEqual(len(self.popen), 1)

    def test_si_el_globo_falla_al_pedirlo_va_por_powershell(self):
        with mock.patch.object(bandeja, "globo", side_effect=RuntimeError("x")):
            recording.notify("✖ Grabación interrumpida", "motivo")
        self.assertEqual(len(self.popen), 1)
        script = base64.b64decode(self.popen[0][0][0][-1]).decode("utf-16-le")
        self.assertIn("ToastNotificationManager", script)

    def test_globo_recortado_a_lo_que_cabe_y_nunca_sin_texto(self):
        b = self.bandeja()
        recording.notify("T" * 300, "x" * 1000)
        recording.notify("Solo título", "   ")
        (t1, x1), (t2, x2) = b.icon.balloons
        self.assertEqual((len(t1), len(x1)), (bandeja.GLOBO_TITULO_MAX, bandeja.GLOBO_TEXTO_MAX))
        self.assertEqual((t2, x2), ("Solo título", "Solo título"))  # WinForms lanza con el texto vacío

    def test_el_icono_que_no_se_pudo_poner_no_recibe_avisos(self):
        test_bandeja.Icon.falla = True
        b = bandeja.Bandeja("tenca.ico", {}, lambda: {}, wf=test_bandeja.wf())
        self.addCleanup(b.close)
        self.assertFalse(b.create(test_bandeja.Form()))
        test_bandeja.Icon.falla = False
        recording.notify("● Grabando", "Alfa")
        self.assertEqual(len(self.popen), 1)


# ----------------------------------------------------------------------------- 35 · repaso en la nube por idioma

class TramosDeIdiomaTests(unittest.TestCase):
    def test_sin_cambios_un_tramo(self):
        self.assertEqual(engines.tramos_idioma(None, 600.0, language="es"), [(0.0, 600.0, "es")])
        self.assertEqual(engines.tramos_idioma([(0.0, None)], 600.0), [(0.0, 600.0, None)])

    def test_cambios_del_directo(self):
        tr = engines.tramos_idioma([(0.0, "en"), (1800.0, "pt"), (3000.0, "en")], 3600.0)
        self.assertEqual(tr, [(0.0, 1800.0, "en"), (1800.0, 3000.0, "pt"), (3000.0, 3600.0, "en")])

    def test_dos_cambios_seguidos_valen_como_uno_y_el_mismo_idioma_no_parte(self):
        tr = engines.tramos_idioma([(0.0, "en"), (600.0, "es"), (604.0, "pt"), (900.0, "pt")], 1200.0)
        self.assertEqual(tr, [(0.0, 600.0, "en"), (600.0, 1200.0, "pt")])

    def test_continuando_a_medias_cuenta_desde_donde_empieza(self):
        tr = engines.tramos_idioma([(0.0, "en"), (1800.0, "pt")], 1200.0, start=1200.0)
        self.assertEqual(tr, [(0.0, 600.0, "en"), (600.0, 1200.0, "pt")])
        self.assertEqual(engines.tramos_idioma([(0.0, "en"), (1800.0, "pt")], 300.0, start=2000.0),
                         [(0.0, 300.0, "pt")])

    def test_un_ultimo_tramo_de_segundos_va_con_el_anterior(self):
        self.assertEqual(engines.tramos_idioma([(0.0, "en"), (595.0, "pt")], 600.0), [(0.0, 600.0, "en")])


class PartesPorIdiomaTests(unittest.TestCase):
    plan = staticmethod(engines.CloudEngine.plan_parts)

    def test_sin_cambios_ni_mezcla_como_antes(self):
        for lang in ("es", None):
            parts = self.plan(1000.0, [(290.0, 292.0), (610.0, 612.0)], 300, lang)
            self.assertEqual([(a, b) for a, b, _ in parts],
                             engines.CloudEngine._cut_points(1000.0, [(290.0, 292.0), (610.0, 612.0)], 300))
            self.assertEqual({x for _, _, x in parts}, {lang})

    def test_con_mezcla_partes_de_minuto_y_medio(self):
        parts = self.plan(1200.0, [], 600, "es-en-pt")
        largos = [b - a for a, b, _ in parts]
        self.assertTrue(all(60 <= d <= 150 for d in largos), largos)
        self.assertEqual({x for _, _, x in parts}, {"es-en-pt"})
        self.assertEqual((parts[0][0], parts[-1][1]), (0.0, 1200.0))

    def test_el_cambio_de_idioma_corta_en_el_silencio_mas_cercano(self):
        sil = [(296.0, 298.0), (610.0, 612.0)]
        parts = self.plan(900.0, sil, 600, "en", tramos=[(0.0, "en"), (300.0, "pt")])
        self.assertEqual(parts, [(0.0, 297.0, "en"), (297.0, 900.0, "pt")])
        for (_, b, _), (a, _, _) in zip(parts, parts[1:]):
            self.assertEqual(b, a)  # sin huecos ni solapes

    def test_detectar_en_una_reunion_que_cambio_va_en_partes_cortas(self):
        parts = self.plan(1200.0, [], 600, None, tramos=[(0.0, "en"), (600.0, None)])
        self.assertEqual(parts[0], (0.0, 600.0, "en"))
        self.assertTrue(all(b - a <= 150 and x is None for a, b, x in parts[1:]), parts)


class RepasoPorIdiomaTests(unittest.TestCase):
    """CloudEngine.transcribe con el Groq de los tests (el _post falso): cada parte, con el idioma de su tramo."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tenca-lote1-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        y = np.zeros(int(900 * 16000), np.int16)
        rnd = np.random.default_rng(1)
        for a in range(0, 900, 10):  # voz cada 10 s con 2 s de silencio entre medias
            y[a * 16000:(a + 8) * 16000] = (rnd.standard_normal(8 * 16000) * 3000).astype(np.int16)
        self.y, self.wav = y, os.path.join(self.tmp, "r.wav")  # sin PyAV: decode_16k y write_flac, dobles
        self.calls, self.durs = [], {}

    def write_flac(self, path, y, sr=16000):
        self.durs[path] = len(y) / sr
        with open(path, "wb") as f:
            f.write(b"fLaC")

    def post(self, cfg, chunk, language, prompt, report, cancelled, context=None, model=None, timeout=300,
             about=None, **kw):
        api_lang, full = engines.CloudEngine.prompt_for(language, prompt, context, about)
        self.calls.append((language, api_lang, full))
        dur = self.durs[chunk]
        return {"language": api_lang or "english",
                "segments": [{"start": 0.5, "end": min(dur, 5.0), "text": f"[{language}] parte"}]}

    def run_cloud(self, language, tramos):
        cloud = engines.CloudEngine(self.tmp)
        with mock.patch.object(cloud, "_post", self.post), mock.patch.object(cloud, "endpoint", lambda *a, **k: None), \
                mock.patch.object(engines, "decode_16k", lambda *a, **k: self.y.copy()), \
                mock.patch.object(engines, "write_flac", self.write_flac):
            return cloud.transcribe(self.wav, 900.0, {"chunk_minutes": 10, "parallel": 2}, language, "Alfa", "j1",
                                    lambda **k: None, lambda: False, tramos_idioma=tramos)

    def test_cada_tramo_con_su_idioma(self):
        segs, _, dur = self.run_cloud("en-pt", [(0.0, "en"), (400.0, "pt")])
        self.assertAlmostEqual(dur, 900.0, places=0)
        langs = [c[0] for c in self.calls]
        self.assertEqual(sorted(set(langs)), ["en", "pt"])
        self.assertEqual([c[1] for c in self.calls if c[0] == "pt"][0], "pt")  # a la API, con el idioma fijado
        # en orden: lo de antes del cambio, en inglés; lo de después, en portugués
        en = [a for a, _, t in segs if t.startswith("[en]")]
        pt = [a for a, _, t in segs if t.startswith("[pt]")]
        self.assertTrue(en and pt and max(en) < 400.0 <= min(pt) + 10, segs)

    def test_mezcla_sin_cambios_partes_cortas_y_detectar(self):
        segs, _, _ = self.run_cloud("es-en-pt", None)
        self.assertGreaterEqual(len(self.calls), 6)  # 15 min en partes de ~1,5 min
        self.assertTrue(all(c[1] is None for c in self.calls))  # la API detecta en cada parte
        self.assertTrue(all(c[2].startswith(engines.style_for("es-en-pt")) for c in self.calls))
        starts = [a for a, _, _ in segs]
        self.assertEqual(starts, sorted(starts))

    def test_un_idioma_como_antes(self):
        self.run_cloud("es", None)
        self.assertEqual([c[0] for c in self.calls], ["es", "es"])  # 15 min en partes de 10


class DirectoApuntaLosTramosTests(unittest.TestCase):
    def session(self, *a, **k):
        return tid.LiveSwitchTests.session(self, *a, **k)

    def test_sin_cambios_un_tramo(self):
        r = self.session(tid.SPANS, 34.0)
        self.assertEqual(r.job.idiomas_tramos, [(0.0, None)])

    def test_cada_cambio_desde_donde_se_rehace(self):
        r = self.session(tid.SPANS, 34.0, events=[(10.0, "en"), (26.0, "pt")])
        tr = r.job.idiomas_tramos
        self.assertEqual(tr[-1][1], "pt")
        ts = [t for t, _ in tr]
        self.assertEqual(ts, sorted(set(ts)))
        self.assertLessEqual(tr[-1][0], 26.0)  # desde lo que se rehizo en portugués, no desde el clic
        # el repaso de esa reunión: lo de antes del último cambio, con lo de antes; lo de después, en portugués
        parts = engines.CloudEngine.plan_parts(34.0, [], 600, "pt", tramos=tr)
        self.assertEqual(parts[-1][2], "pt")


if __name__ == "__main__":
    unittest.main()
