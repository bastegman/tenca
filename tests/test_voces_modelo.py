# -*- coding: utf-8 -*-
"""Cambio del modelo de huellas de «Quién habla» (review del 14/11/2026, punto 36): de TitaNet small a CAM++ zh_en. Las
huellas guardadas con el anterior no se comparan con las nuevas (las dos tienen 192 valores: compararlas daría
parecidos sin sentido); cada persona conserva id, nombre, «Soy yo» y consentimiento, y sus huellas se rehacen desde el
audio de sus reuniones.

tests/voces_titanet/voces.json lo escribió el código de antes (70a3e0c, MODEL_ID de TitaNet small) con
Voices.create/remember: es el estado que deja una instalación de antes del cambio.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

import numpy as np

from support import APP, HERE, server  # noqa: F401  (pone _app en sys.path)
import voices  # noqa: E402

VIEJO = os.path.join(HERE, "voces_titanet", "voces.json")
ANTERIOR = "pyannote-seg-3.0 + nemo_en_titanet_small"


def vec(seed):
    x = np.random.default_rng(seed).standard_normal(voices.DIM)
    return (x / np.linalg.norm(x)).tolist()


class MigracionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tenca-voces-modelo-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "voces.json")
        shutil.copy(VIEJO, self.path)
        with open(VIEJO, encoding="utf-8") as f:
            self.antes = json.loads(voices.unprotect(f.read().strip()).decode("utf-8"))

    def test_el_fichero_es_del_modelo_de_antes(self):
        self.assertEqual(self.antes["modelo"], ANTERIOR)
        self.assertNotEqual(voices.MODEL_ID, ANTERIOR)
        self.assertEqual(sum(len(p["huellas"]) for p in self.antes["personas"]), 4)

    def test_se_conservan_las_personas_y_las_huellas_pasan_a_rehacer(self):
        v = voices.Voices(self.path)
        self.assertIsNone(v.error)
        antes = {p["id"]: p for p in self.antes["personas"]}
        ahora = {p["id"]: p for p in v.personas()}
        self.assertEqual(set(ahora), set(antes))
        for pid, p in antes.items():
            q = ahora[pid]
            for k in ("nombre", "es_usuario", "consentimiento", "creada"):
                self.assertEqual(q.get(k), p.get(k), (pid, k))
            self.assertEqual(q["huellas"], [])  # ningún vector del modelo de antes
            self.assertEqual([(x["reunion"], x["fecha"], x["segundos"]) for x in q.get("rehacer") or []],
                             [(h["reunion"], h["fecha"], h["segundos"]) for h in p["huellas"]])
        self.assertEqual((v.data["modelo"], v.data["modelo_anterior"]), (voices.MODEL_ID, ANTERIOR))
        self.assertEqual(v.data["contador"], self.antes["contador"])  # «Persona N» sigue contando desde donde iba
        self.assertEqual(len(v.pendientes()), 4)
        pub = {p["nombre"]: p for p in v.public()}
        self.assertEqual((pub["Persona Alfa"]["huellas"], pub["Persona Alfa"]["por_rehacer"]), (0, 2))

    def test_queda_escrito_y_la_copia_es_la_de_antes(self):
        voices.Voices(self.path)
        with open(self.path, encoding="utf-8") as f:
            escrito = json.loads(voices.unprotect(f.read().strip()).decode("utf-8"))
        self.assertEqual(escrito["modelo"], voices.MODEL_ID)
        self.assertFalse(any(p["huellas"] for p in escrito["personas"]))
        with open(self.path + ".bak", encoding="utf-8") as f:
            self.assertEqual(json.loads(voices.unprotect(f.read().strip()).decode("utf-8")), self.antes)
        # al volver a abrirlo no se migra otra vez (ni se pierden las por rehacer)
        with open(self.path, encoding="utf-8") as f:
            ya = f.read()
        v = voices.Voices(self.path)
        self.assertEqual(len(v.pendientes()), 4)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), ya)  # no se ha vuelto a escribir

    def test_una_voz_de_antes_ya_no_reconoce_a_nadie(self):
        viejo = next(p for p in self.antes["personas"] if p["nombre"] == "Persona Alfa")["huellas"][0]["vector"]
        self.assertEqual(voices.match(viejo, self.antes["personas"])["persona"] is not None, True)  # antes, sí
        v = voices.Voices(self.path)
        self.assertIsNone(voices.match(viejo, v.personas())["persona"])

    def test_rehacer_desde_el_audio_de_sus_reuniones(self):
        v = voices.Voices(self.path)
        alfa = next(p for p in v.personas() if p["nombre"] == "Persona Alfa")
        yo = v.user()
        # una reunión de Alfa sigue en disco (con su sidecar); la otra ya no; la del usuario falla al calcular
        audio = os.path.join(self.tmp, "Proyecto", "2026-11-05 Proyecto.m4a")
        os.makedirs(os.path.dirname(audio))
        open(audio, "wb").close()
        manual = {"origen": "manual", "provisional": False}
        voices.write_sidecar(voices.sidecar_path(audio), {"tramos": [{"inicio": 0, "fin": 30, "h": "h2"}],
                                                          "hablantes": {"h1": dict(manual, persona=yo["id"]),
                                                                        "h2": dict(manual, persona=alfa["id"])}})
        audio_yo = os.path.join(self.tmp, "2026-11-01 Semanal.wav")
        open(audio_yo, "wb").close()
        voices.write_sidecar(voices.sidecar_path(audio_yo), {"tramos": [],
                                                             "hablantes": {"h1": dict(manual, persona=yo["id"])}})
        calculadas = []

        def buscar(nombre):
            return [p for p in (audio, audio_yo) if os.path.basename(p) == nombre]

        def calcular(a, h, sidecar):
            calculadas.append((os.path.basename(a), h, sorted(sidecar["hablantes"])))
            if a == audio_yo:
                raise MemoryError("bad allocation")
            return vec(5), 58.0
        out = voices.rehacer_huellas(v, buscar, calcular)
        self.assertEqual(out, {"rehechas": 1, "sin_audio": 2, "fallidas": 1})
        self.assertIn(("2026-11-05 Proyecto.m4a", "h2", ["h1", "h2"]), calculadas)
        alfa = v.find(alfa["id"])
        fecha = next(h["fecha"] for p in self.antes["personas"] for h in p["huellas"]
                     if h["reunion"] == "2026-11-05 Proyecto.m4a")
        self.assertEqual([(h["reunion"], h["fecha"], h["segundos"]) for h in alfa["huellas"]],
                         [("2026-11-05 Proyecto.m4a", fecha, 58.0)])  # con la fecha de cuando se guardó
        # la que no tiene audio y la que falló se quedan por rehacer (se vuelven a buscar la próxima vez)
        self.assertEqual(sorted(x["reunion"] for _, x in v.pendientes()),
                         ["2026-11-01 Semanal.wav", "2026-11-02 Proyecto.wav", "2026-11-03 Cliente.wav"])
        self.assertEqual(alfa["huellas"][0]["modelo"], voices.MODEL_ID)
        self.assertEqual(voices.match(vec(5), v.personas())["persona"], alfa["id"])  # y ya reconoce a Alfa
        self.assertEqual(sorted(p["nombre"] for p in v.personas()),
                         ["Persona Alfa", "Persona Beta", "Persona Gamma", "Yo"])

    def test_dos_a_la_vez_no_pierden_el_fichero(self):
        # revisión 2, P1-1: con un .tmp compartido y la copia moviendo el fichero, 402 de 2000 dejaban solo el .bak
        for i in range(40):
            shutil.copy(VIEJO, self.path)
            barrera, errores = threading.Barrier(2), []

            def abrir():
                try:
                    barrera.wait()
                    voices.Voices(self.path)
                except Exception as e:  # pragma: no cover
                    errores.append(e)
            hilos = [threading.Thread(target=abrir) for _ in range(2)]
            for t in hilos:
                t.start()
            for t in hilos:
                t.join(10)
            self.assertEqual(errores, [])
            self.assertTrue(os.path.isfile(self.path), i)
            self.assertEqual(len(voices.Voices(self.path).personas()), 4, i)
            self.assertEqual(len(voices.Voices(self.path).pendientes()), 4, i)  # una sola migración

    def test_sin_el_fichero_se_recupera_de_la_copia(self):
        os.replace(self.path, self.path + ".bak")  # una escritura de antes cortada entre las dos operaciones
        v = voices.Voices(self.path)
        self.assertEqual(len(v.personas()), 4)
        self.assertTrue(os.path.isfile(self.path))

    def test_una_huella_que_guarda_la_version_de_antes_tampoco_se_usa(self):
        # revisión 2, P1-2: instalar la versión de antes sobre un voces.json ya migrado y volver a esta
        v = voices.Voices(self.path)
        alfa = next(p for p in v.personas() if p["nombre"] == "Persona Alfa")["id"]
        with open(self.path, encoding="utf-8") as f:
            data = json.loads(voices.unprotect(f.read().strip()).decode("utf-8"))
        viejo = vec(11)
        for p in data["personas"]:  # lo que añade remember() en 70a3e0c: sin «modelo»
            if p["id"] == alfa:
                p["huellas"].append({"vector": viejo, "fecha": "2026-11-20T10:00:00", "reunion": "r.wav",
                                     "segundos": 40.0})
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(voices.protect(json.dumps(data).encode("utf-8")))
        w = voices.Voices(self.path)
        self.assertEqual(w.find(alfa)["huellas"], [])
        self.assertIn("r.wav", [x["reunion"] for pid, x in w.pendientes() if pid == alfa])
        self.assertIsNone(voices.match(viejo, w.personas())["persona"])

    def test_el_hablante_es_el_que_se_puso_a_mano(self):
        # revisión 2, P1-3: no uno que la app solo propuso (por voz, por el micro o provisional)
        data = {"hablantes": {"h1": {"persona": "p1", "origen": "huella", "provisional": True, "segundos": 60},
                              "h2": {"persona": "p1", "origen": "huella", "provisional": False, "segundos": 61},
                              "h3": {"persona": "p1", "origen": "manual", "provisional": False, "segundos": 30},
                              "h4": {"persona": "p1", "origen": "manual", "provisional": False, "segundos": 58},
                              "h5": {"persona": "p2", "origen": "manual"}}}
        self.assertEqual(voices.hablante_de(data, "p1", 57.0), "h4")  # el de segundos más cercanos
        self.assertEqual(voices.hablante_de(data, "p1", 31.0), "h3")
        self.assertIsNone(voices.hablante_de({"hablantes": {"h1": {"persona": "p1", "origen": "micro"}}}, "p1"))
        self.assertIsNone(voices.hablante_de({"hablantes": ["raro"]}, "p1"))
        self.assertIsNone(voices.hablante_de(None, "p1"))

    def test_rehacer_no_lanza_y_no_duplica(self):
        v = voices.Voices(self.path)
        alfa = next(p for p in v.personas() if p["nombre"] == "Persona Alfa")["id"]
        v.find(alfa)["rehacer"].append(dict(v.find(alfa)["rehacer"][0]))  # la misma reunión dos veces
        audio = os.path.join(self.tmp, "2026-11-02 Proyecto.wav")
        open(audio, "wb").close()
        voices.write_sidecar(voices.sidecar_path(audio), {"hablantes": {"h1": {"persona": alfa, "origen": "manual"}}})
        out = voices.rehacer_huellas(v, lambda n: [audio] if n == os.path.basename(audio) else [],
                                     lambda a, h, d: (vec(3), 40.0))
        self.assertEqual(out["rehechas"], 1)
        self.assertEqual(len(v.find(alfa)["huellas"]), 1)  # una, no dos iguales
        # una huella de otro tamaño o un sidecar raro no lanzan ni quitan la pendiente
        out = voices.rehacer_huellas(v, lambda n: [audio], lambda a, h, d: ([1.0, 2.0], 1.0))
        self.assertGreaterEqual(out["fallidas"], 1)
        self.assertIn("2026-11-05 Proyecto.m4a", [x["reunion"] for pid, x in v.pendientes() if pid == alfa])
        with open(voices.sidecar_path(audio), "w", encoding="utf-8") as f:
            f.write('{"hablantes": [1, 2]}')
        voices.rehacer_huellas(v, lambda n: [audio], lambda a, h, d: (vec(3), 40.0))

    def test_buscar_los_audios_por_su_nombre(self):
        raiz = os.path.join(self.tmp, "Reuniones")
        for rel in ("Alfa/2026-11-02 Proyecto.wav", "Beta/2026-11-02 Proyecto.wav", ".oculta/2026-11-03 Cliente.wav",
                    "Alfa/2026-11-02 Proyecto.srt", "Papelera/nota de voz.waptt", "_app/2026-11-03 Cliente.wav"):
            os.makedirs(os.path.dirname(os.path.join(raiz, rel)), exist_ok=True)
            open(os.path.join(raiz, rel), "wb").close()
        buscar = voices.buscar_por_nombre([raiz])
        self.assertEqual(sorted(os.path.relpath(p, raiz) for p in buscar("2026-11-02 Proyecto.wav")),
                         [os.path.join(x, "2026-11-02 Proyecto.wav") for x in ("Alfa", "Beta")])
        self.assertEqual(buscar("2026-11-03 Cliente.wav"), [])  # en una carpeta oculta o en _app: no
        self.assertEqual(buscar("no.wav"), [])
        # las extensiones de la app (server.AUDIO_EXT), también en una carpeta suya que se llame «Papelera»
        self.assertEqual(len(buscar("nota de voz.waptt")), 1)
        self.assertEqual(set(voices.AUDIO_EXT), set(server.AUDIO_EXT))

    def test_fusionar_no_pierde_las_por_rehacer(self):
        v = voices.Voices(self.path)
        ids = {p["nombre"]: p["id"] for p in v.personas()}
        v.merge(ids["Persona Alfa"], ids["Persona Beta"])
        self.assertEqual(sorted(x["reunion"] for pid, x in v.pendientes() if pid == ids["Persona Alfa"]),
                         ["2026-11-02 Proyecto.wav", "2026-11-03 Cliente.wav", "2026-11-05 Proyecto.m4a"])

    def test_un_fichero_del_modelo_de_ahora_no_se_toca(self):
        v = voices.Voices(os.path.join(self.tmp, "nuevo.json"))
        p = v.create("Persona Delta", consentimiento="2026-11-15T10:00:00")
        v.remember(p["id"], vec(9), 30.0, "r.wav", "2026-11-15T10:00:00")
        w = voices.Voices(os.path.join(self.tmp, "nuevo.json"))
        self.assertEqual(len(w.find(p["id"])["huellas"]), 1)
        self.assertEqual(w.pendientes(), [])
        self.assertNotIn("modelo_anterior", w.data)


class RecordarTrasActualizarTests(unittest.TestCase):
    def test_el_subproceso_baja_el_modelo_si_falta(self):
        # revisión 2, P1-4: «Recordar» (voices.py --fingerprint) tras actualizar, antes de ninguna identificación
        tmp = tempfile.mkdtemp(prefix="tenca-voces-recordar-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import voices\n"
                "def ensure(d, progress=None, cancelled=None):\n"
                "    raise voices.VoicesError('BAJANDO ' + d)\n"
                "voices.ensure_models = ensure\n"
                "sys.exit(voices.main(['--diarize', 'r.wav', '--out', 'r.hablantes.json', '--fingerprint', 'h1',"
                " '--data', sys.argv[2]]))")
        r = subprocess.run([sys.executable, "-c", code, APP, tmp], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("BAJANDO " + tmp, r.stdout)


class ModeloFijadoTests(unittest.TestCase):
    def test_modelo_de_huellas_y_su_sha256(self):
        m = voices.MODELS[1]
        self.assertEqual(m["name"], "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx")
        self.assertEqual(m["size"], 28281164)
        # el del checksum.txt del release speaker-recongition-models de sherpa-onnx (15/11/2026)
        self.assertEqual(m["sha256"], "aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2")
        self.assertTrue(m["url"].endswith("/speaker-recongition-models/" + m["name"]))
        self.assertIn("campplus", voices.MODEL_ID)

    def test_una_instalacion_de_antes_baja_el_modelo_nuevo(self):
        tmp = tempfile.mkdtemp(prefix="tenca-voces-modelos-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        d = voices.models_dir(tmp)
        os.makedirs(d)
        for name in ("segmentation.onnx", "nemo_en_titanet_small.onnx"):  # lo que dejó la app de antes
            with open(os.path.join(d, name), "wb") as f:
                f.write(b"x")
        self.assertFalse(voices.models_ready(tmp))  # el servidor llama a ensure_models al diarizar (server.py:4275)


if __name__ == "__main__":
    unittest.main()
