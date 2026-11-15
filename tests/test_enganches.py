# -*- coding: utf-8 -*-
"""Los enganches en el servidor ya partido de lo que dejaron preparado motor-lote1, interfaz-lote2 y reunion-b5-b8
(15/11/2026):

- 36: las huellas de voz del modelo anterior (tests/voces_titanet/voces.json, escrito por el codigo de antes del
  cambio a CAM++) se rehacen al arrancar desde el audio de sus reuniones, sin perder nombres, «Soy yo» ni
  consentimiento, y nunca con la grabadora en marcha; una persona que solo tiene huellas por rehacer no cuenta como
  «sin huellas»; las lineas nuevas del log salen en el diagnostico.
- 54: la mezcla es-en-pt en todo el servidor (IDIOMA_RE, idioma del repaso, frase de estilo, diagnostico).
- 35: el repaso final en la nube recibe los tramos de idioma del directo.
- 11: un 500 responde {error: frase, detalle}; quien ya se fue (el aviso flotante) no deja traza.
- 38: sin_proyecto en la cabecera del .md, agenda.ocupado en /api/hoy y la ✕ del aviso de la siguiente reunion.
- B8/B5: la muestra de una voz para «Oír» y el proveedor en la cabecera del resumen.
"""
import ast
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

import numpy as np

from support import APP, HERE, TempTree, bare_jobs, server, wav_bytes
from test_directo_auditoria import LiveJobs
from test_fix_voces import Reunion
from test_ui_smoke import puerto_libre

import api  # noqa: E402
import app_voces  # noqa: E402
import engines  # noqa: E402
import plan  # noqa: E402
import voices  # noqa: E402

VIEJO = os.path.join(HERE, "voces_titanet", "voces.json")


def leer_voces(path):
    with open(path, encoding="utf-8") as f:
        return json.loads(voices.unprotect(f.read().strip()).decode("utf-8"))


def vec(seed):
    x = np.random.default_rng(seed).standard_normal(voices.DIM)
    return (x / np.linalg.norm(x)).tolist()


# ----------------------------------------------------------------------------- 36: rehacer las huellas al arrancar

class RehacerAlArrancarTests(TempTree):
    """Una instalacion de antes del cambio de modelo: su voces.json (4 personas, 4 huellas de TitaNet) y las
    reuniones donde se pulso «Recordar», con su sidecar. Lo que hace el hilo que arranca main()."""

    def setUp(self):
        super().setUp()
        self.voces = os.path.join(self.data_dir, "voces.json")
        shutil.copy(VIEJO, self.voces)
        self.antes = leer_voces(VIEJO)
        self.ids = {p["nombre"]: p["id"] for p in self.antes["personas"]}
        # las reuniones de las huellas (los nombres que guardo «Recordar»): Yo en la semanal (h1, a mano), Alfa en la
        # del 2 (h2, a mano; h1 era otra voz). La del 5 (Alfa, m4a) ya no esta. La del 3 tiene a Beta solo propuesta
        # por la app (no a mano): de ahi no se rehace
        self.reunion("2026-11-01 Semanal.wav", {"h1": dict(self.manual("Yo"), segundos=95.0),
                                                "h2": {"segundos": 40.0, "persona": None}})
        self.reunion("Proyecto/2026-11-02 Proyecto.wav", {
            "h1": {"segundos": 30.0, "persona": None}, "h2": dict(self.manual("Persona Alfa"), segundos=42.5)})
        self.reunion("2026-11-03 Cliente.wav", {"h1": {"segundos": 30.0, "persona": self.ids["Persona Beta"],
                                                       "origen": "huella", "provisional": True}})
        self.app = self.nueva_app()
        self.calculadas = []
        self.app.jobs.speaker_vector = self.speaker_vector
        p1 = mock.patch.object(voices, "available", return_value=(True, None))
        p2 = mock.patch.object(voices, "models_ready", return_value=True)
        p3 = mock.patch.object(app_voces, "REHACER_PASO_S", 0.05)
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)

    def manual(self, nombre):
        return {"persona": self.ids[nombre], "origen": "manual", "provisional": False}

    def reunion(self, rel, hablantes, creado="2026-11-05T10:00:00"):
        self.wav(rel, seconds=0.5)
        tramos = [{"inicio": 10.0 * i, "fin": 10.0 * i + 9.0, "h": h} for i, h in enumerate(hablantes)]
        voices.write_sidecar(voices.sidecar_path(os.path.join(self.root, rel)), {
            "version": 1, "creado": creado, "tramos": tramos, "hablantes": hablantes})

    def nueva_app(self, **st):
        a = server.App.__new__(server.App)
        a.settings = server.Store(os.path.join(self.data_dir, "settings.json"), server.DEFAULT_SETTINGS)
        a.settings.update(lambda d: d.update(dict({"voices_enabled": True}, **st)))
        a.library = server.Library(server.Durations(os.path.join(self.data_dir, "durations.json")))
        jobs = bare_jobs([])
        jobs.settings, jobs.library, jobs._voices, jobs.voice_vectors = a.settings, a.library, None, {}
        a.jobs, a.voices_dl, a.rec, a.meeting = jobs, None, None, None
        return a

    def speaker_vector(self, rel, h, cancel=None):
        self.calculadas.append((rel, h))
        return vec(len(self.calculadas)), 33.0

    def persona(self, nombre):
        return next(p for p in leer_voces(self.voces)["personas"] if p["nombre"] == nombre)

    def test_se_rehacen_sin_perder_nombres_soy_yo_ni_consentimiento(self):
        res = self.app.rehacer_huellas()
        self.assertEqual(res, {"rehechas": 2, "sin_audio": 2, "fallidas": 0})
        self.assertEqual(self.calculadas, [("2026-11-01 Semanal.wav", "h1"),
                                           ("Proyecto/2026-11-02 Proyecto.wav", "h2")])
        ahora = {p["id"]: p for p in leer_voces(self.voces)["personas"]}  # lo escrito en disco
        self.assertEqual(set(ahora), {p["id"] for p in self.antes["personas"]})
        for p in self.antes["personas"]:
            q = ahora[p["id"]]
            for k in ("nombre", "es_usuario", "consentimiento", "creada"):
                self.assertEqual(q.get(k), p.get(k), (p["nombre"], k))
        yo, alfa = ahora[self.ids["Yo"]], ahora[self.ids["Persona Alfa"]]
        beta, gamma = ahora[self.ids["Persona Beta"]], ahora[self.ids["Persona Gamma"]]
        self.assertTrue(yo["es_usuario"])
        # la huella nueva: del modelo de ahora, con la fecha de la de antes y su reunion
        viejas = {(h["reunion"]): h for p in self.antes["personas"] for h in p["huellas"]}
        for p, reunion in ((yo, "2026-11-01 Semanal.wav"), (alfa, "2026-11-02 Proyecto.wav")):
            self.assertEqual(len(p["huellas"]), 1, p["nombre"])
            h = p["huellas"][0]
            self.assertEqual((h["modelo"], h["reunion"], h["fecha"]), (voices.MODEL_ID, reunion,
                                                                       viejas[reunion]["fecha"]))
            self.assertEqual(len(h["vector"]), voices.DIM)
        # lo que no se pudo rehacer sigue apuntado: la reunion que ya no esta y la de Beta (no puesta a mano)
        self.assertEqual([x["reunion"] for x in alfa["rehacer"]], ["2026-11-05 Proyecto.m4a"])
        self.assertEqual(([x["reunion"] for x in beta["rehacer"]], beta["huellas"]), (["2026-11-03 Cliente.wav"], []))
        self.assertEqual((gamma["huellas"], gamma.get("rehacer")), ([], None))
        # y ya se la reconoce por la voz (con el modelo de ahora)
        m = voices.match(yo["huellas"][0]["vector"], self.app.jobs.voices.personas())
        self.assertEqual((m["persona"], m["provisional"]), (self.ids["Yo"], False))

    def test_un_recordar_de_antes_del_7_de_noviembre(self):
        """Los sidecars de antes del 07/11/2026 no dicen qué se puso a mano (sin «origen»): ahí vale el único hablante
        de esa persona con los segundos de la huella (los de su voz al pulsar «Recordar»)."""
        beta = self.ids["Persona Beta"]  # su huella: «2026-11-03 Cliente.wav», 30,0 s
        side = voices.sidecar_path(os.path.join(self.root, "2026-11-03 Cliente.wav"))
        viejo = {"version": 1, "tramos": [{"inicio": 0, "fin": 30, "h": "h1"}, {"inicio": 30, "fin": 42, "h": "h2"}],
                 "hablantes": {"h1": {"segundos": 30.0, "persona": beta, "provisional": False},
                               "h2": {"segundos": 12.0, "persona": beta, "provisional": False}}}
        voices.write_sidecar(side, viejo)
        self.app.rehacer_huellas()
        self.assertIn(("2026-11-03 Cliente.wav", "h1"), self.calculadas)
        b = next(p for p in leer_voces(self.voces)["personas"] if p["id"] == beta)
        self.assertEqual((len(b["huellas"]), b.get("rehacer")), (1, None))

    def test_un_sidecar_de_antes_vuelto_a_identificar_no_vale(self):
        beta = self.ids["Persona Beta"]
        side = voices.sidecar_path(os.path.join(self.root, "2026-11-03 Cliente.wav"))
        for hablantes in ({"h1": {"segundos": 44.0, "persona": beta}},  # otros segundos: otra identificación
                          {"h1": {"segundos": 30.0, "persona": beta, "provisional": True}},  # solo propuesta
                          {"h1": {"segundos": 30.0, "persona": beta}, "h2": {"segundos": 30.2, "persona": beta}},
                          {"h1": {"segundos": 30.0, "persona": beta}, "h2": {"segundos": 9.0, "origen": "micro"}}):
            with self.subTest(hablantes):
                voices.write_sidecar(side, {"version": 1, "hablantes": hablantes,
                                            "tramos": [{"inicio": 0, "fin": 30, "h": "h1"}]})
                self.assertIsNone(voices.hablante_de(voices.read_sidecar(side), beta, 30.0))
        self.assertIsNone(voices.hablante_de({"hablantes": {"h1": {"segundos": 0.0, "persona": beta}}}, beta, 0))

    def test_otra_vez_no_duplica_ni_pierde(self):
        self.app.rehacer_huellas()
        n = len(self.calculadas)
        res = self.nueva_app().rehacer_huellas()  # el siguiente arranque, con el mismo voces.json
        self.assertEqual(res["rehechas"], 0)
        self.assertEqual(len(self.calculadas), n)
        ahora = {p["nombre"]: p for p in leer_voces(self.voces)["personas"]}
        self.assertEqual((len(ahora["Yo"]["huellas"]), len(ahora["Persona Alfa"]["huellas"])), (1, 1))

    def test_con_la_grabadora_en_marcha_espera(self):
        grabando = [True]
        self.app.rec = types.SimpleNamespace(alive=lambda: grabando[0])
        t = threading.Thread(target=self.app.rehacer_huellas, daemon=True)
        with mock.patch.object(app_voces, "log") as log:
            t.start()
            time.sleep(0.5)
            self.assertEqual(self.calculadas, [])  # nada mientras se graba
            self.assertTrue(t.is_alive())
            self.assertIn("(grabando)", " ".join(str(c.args[0]) for c in log.call_args_list))
            grabando[0] = False
            t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(self.calculadas), 2)

    def test_con_un_directo_o_identificando_voces_tambien(self):
        for kind, live in (("transcribe", True), ("voces", False)):
            with self.subTest(kind):
                job = types.SimpleNamespace(path="otra.wav", kind=kind, status="running", live=live)
                self.app.jobs.list = [job]
                self.assertEqual(self.app._rehacer_ocupado(), "directo" if live else "identificando_voces")
        self.app.jobs.list = []
        self.app.meeting = {"app": "Teams"}
        self.assertEqual(self.app._rehacer_ocupado(), "llamada")
        self.app.meeting = None
        self.assertIsNone(self.app._rehacer_ocupado())

    def test_lo_que_se_lee_es_el_sidecar_de_despues_de_esperar(self):
        """Se espera antes de leer el sidecar y elegir el hablante: si entretanto se volvió a identificar y h1 sigue
        siendo «Yo» puesto a mano (carry_manual lo conserva), se calcula sobre la identificación de ahora."""
        grabando = [True]
        self.app.rec = types.SimpleNamespace(alive=lambda: grabando[0])
        t = threading.Thread(target=self.app.rehacer_huellas, daemon=True)
        t.start()
        time.sleep(0.3)
        side = voices.sidecar_path(os.path.join(self.root, "2026-11-01 Semanal.wav"))
        data = voices.read_sidecar(side)
        data["creado"] = "2026-11-15T22:00:00"
        voices.write_sidecar(side, data)
        grabando[0] = False
        t.join(10)
        self.assertIn(("2026-11-01 Semanal.wav", "h1"), self.calculadas)
        self.assertEqual(len(self.persona("Yo")["huellas"]), 1)

    def test_si_se_vuelve_a_identificar_mientras_se_calcula_no_se_guarda(self):
        """h es el hablante de la identificación que se leyó: si se vuelve a identificar durante el cálculo, h ya
        puede ser otra voz. Se deja para la próxima vez."""
        side = voices.sidecar_path(os.path.join(self.root, "2026-11-01 Semanal.wav"))

        def lento(rel, h, cancel=None):
            if rel == "2026-11-01 Semanal.wav":
                data = voices.read_sidecar(side)
                data["creado"] = "2026-11-15T22:00:00"
                voices.write_sidecar(side, data)
            return self.speaker_vector(rel, h)
        self.app.jobs.speaker_vector = lento
        self.app.rehacer_huellas()
        yo = self.persona("Yo")
        self.assertEqual((yo["huellas"], [x["reunion"] for x in yo["rehacer"]]), ([], ["2026-11-01 Semanal.wav"]))

    def corregir_mientras_espera(self, rel, cambio):
        """Con la grabadora en marcha el hilo espera; mientras, el usuario corrige un hablante de esa reunión (sin
        volver a identificar: el «creado» no cambia). Al parar de grabar, se sigue."""
        grabando = [True]
        self.app.rec = types.SimpleNamespace(alive=lambda: grabando[0])
        t = threading.Thread(target=self.app.rehacer_huellas, daemon=True)
        t.start()
        time.sleep(0.4)
        self.assertEqual(self.calculadas, [])
        side = voices.sidecar_path(os.path.join(self.root, rel))
        d = voices.read_sidecar(side)
        cambio(d)
        voices.write_sidecar(side, d)
        grabando[0] = False
        t.join(10)
        self.assertFalse(t.is_alive())

    def test_un_hablante_corregido_mientras_espera_no_da_su_huella(self):
        """Revisión 1 (P1-1): h1 de la semanal era «Yo»; mientras se graba, el usuario dice que es Gamma. Su voz no
        puede acabar en «Yo»."""
        self.corregir_mientras_espera("2026-11-01 Semanal.wav",
                                      lambda d: voices.assign(d, "h1", self.ids["Persona Gamma"], self.ids["Yo"]))
        self.assertEqual(self.persona("Yo")["huellas"], [])
        self.assertNotIn(("2026-11-01 Semanal.wav", "h1"), self.calculadas)
        self.assertEqual(len(self.persona("Persona Alfa")["huellas"]), 1)  # lo demás, como siempre

    def test_uno_que_ya_no_es_nadie_tampoco(self):
        self.corregir_mientras_espera("2026-11-01 Semanal.wav", lambda d: voices.assign(d, "h1", None))
        self.assertEqual(self.persona("Yo")["huellas"], [])

    def test_corregido_mientras_se_calcula_tampoco(self):
        """El subproceso tarda unos segundos: si entretanto se corrige, lo calculado no se guarda."""
        side = voices.sidecar_path(os.path.join(self.root, "2026-11-01 Semanal.wav"))

        def lento(rel, h, cancel=None):
            if rel == "2026-11-01 Semanal.wav":
                d = voices.read_sidecar(side)
                voices.assign(d, "h1", self.ids["Persona Gamma"], self.ids["Yo"])
                voices.write_sidecar(side, d)
            return self.speaker_vector(rel, h)
        self.app.jobs.speaker_vector = lento
        self.app.rehacer_huellas()
        self.assertEqual(self.persona("Yo")["huellas"], [])
        self.assertEqual(len(self.persona("Persona Alfa")["huellas"]), 1)

    def test_una_grabacion_que_empieza_mientras_se_calcula_lo_corta(self):
        """Revisión 1 (P2-1): el cálculo en marcha se corta en cuanto empieza a grabarse (cancel) y se rehace al
        terminar, en la misma pasada."""
        grabando, cortes = [False], []

        def lento(rel, h, cancel=None):
            if not cortes:  # el primero: empieza una grabación mientras calcula
                grabando[0] = True
                fin = time.time() + 5
                while time.time() < fin:
                    if cancel and cancel():
                        cortes.append(rel)
                        threading.Timer(0.3, lambda: grabando.__setitem__(0, False)).start()
                        raise engines.Cancelled()
                    time.sleep(0.02)
                grabando[0] = False  # no se ha cortado: que el resto no espere para siempre
                cortes.append("sin cortar")
            return self.speaker_vector(rel, h)
        self.app.rec = types.SimpleNamespace(alive=lambda: grabando[0])
        self.app.jobs.speaker_vector = lento
        out = {}
        t = threading.Thread(target=lambda: out.update(res=self.app.rehacer_huellas()), daemon=True)
        with mock.patch.object(app_voces, "log"):
            t.start()
            t.join(30)
        self.assertFalse(t.is_alive())
        res = out["res"]
        self.assertEqual(cortes, ["2026-11-01 Semanal.wav"])
        self.assertEqual(res["rehechas"], 2)
        self.assertEqual((len(self.persona("Yo")["huellas"]), len(self.persona("Persona Alfa")["huellas"])), (1, 1))

    def test_con_una_transcripcion_en_marcha_tambien_espera(self):
        """Revisión 1 (P2-2): Whisper en el PC, y lo que encadena al acabar, también ocupan el PC. Un resumen no."""
        self.app.jobs.list = [types.SimpleNamespace(path="x.wav", kind="transcribe", status="running", live=False)]
        self.assertEqual(self.app._rehacer_ocupado(), "transcribiendo")
        self.app.jobs.list = [types.SimpleNamespace(path="x.wav", kind="summary", status="running", live=False)]
        self.assertIsNone(self.app._rehacer_ocupado())
        self.app.jobs.list, self.app.jobs.held = [], {"x.wav": 1}
        self.assertEqual(self.app._rehacer_ocupado(), "transcribiendo")

    def test_un_resumen_en_marcha_no_hace_esperar(self):
        """Revisión 2 (P2-A): el hilo de la cola retiene la reunión (held) también mientras resume."""
        jobs = self.app.jobs
        jobs.list = [types.SimpleNamespace(path="x.wav", kind="summary", status="running", live=False)]
        jobs._hold("x.wav")  # lo que hace Jobs._worker antes de _run_summary
        self.assertIsNone(self.app._rehacer_ocupado())
        jobs._hold("y.wav")  # otra reunión retenida sin resumen (lo que encadena una transcripción): sí
        self.assertEqual(self.app._rehacer_ocupado(), "transcribiendo")

    def test_la_misma_reunion_dos_veces_y_un_corte_da_una_huella(self):
        """Revisión 2 (P2-B): «Recordar» dos veces en la misma reunión (dos huellas de antes por rehacer) y la primera
        cortada por una grabación: una sola huella, también tras la vuelta extra."""
        d = leer_voces(self.voces)
        yo = next(p for p in d["personas"] if p["nombre"] == "Yo")
        yo["huellas"].append(dict(yo["huellas"][0], fecha="2026-11-04T10:00:00"))
        with open(self.voces, "w", encoding="utf-8") as f:
            f.write(voices.protect(json.dumps(d).encode("utf-8")))
        n = [0]

        def corta_la_primera(rel, h, cancel=None):
            n[0] += 1
            if n[0] == 1:
                raise engines.Cancelled()
            return self.speaker_vector(rel, h)
        self.app.jobs.speaker_vector = corta_la_primera
        out = {}
        t = threading.Thread(target=lambda: out.update(res=self.app.rehacer_huellas()), daemon=True)
        t.start()
        t.join(30)
        self.assertFalse(t.is_alive())
        yo = self.persona("Yo")
        self.assertEqual(([h["reunion"] for h in yo["huellas"]], yo.get("rehacer")), (["2026-11-01 Semanal.wav"], None))

    def test_un_cambio_en_como_se_puso_el_hablante_tampoco(self):
        """Revisión 2 (P2-D): la persona es la misma pero ya no es «a mano» (o es provisional): no vale."""
        side = voices.sidecar_path(os.path.join(self.root, "2026-11-01 Semanal.wav"))
        for cambio in ({"provisional": True}, {"origen": "huella"}):
            with self.subTest(cambio):
                def lento(rel, h, cancel=None, cambio=cambio):
                    if rel == "2026-11-01 Semanal.wav":
                        d = voices.read_sidecar(side)
                        d["hablantes"]["h1"].update(cambio)
                        voices.write_sidecar(side, d)
                    return self.speaker_vector(rel, h)
                self.app.jobs.speaker_vector = lento
                self.app.rehacer_huellas()
                self.assertEqual(self.persona("Yo")["huellas"], [])
                d = voices.read_sidecar(side)  # vuelta a como estaba, para el siguiente
                d["hablantes"]["h1"].update(origen="manual", provisional=False)
                voices.write_sidecar(side, d)

    def test_un_sidecar_tocado_a_mano_no_tumba_nada(self):
        """Revisión 2 (P2-E): «hablantes» que no es un objeto."""
        side = voices.sidecar_path(os.path.join(self.root, "2026-11-01 Semanal.wav"))
        self.assertFalse(app_voces.Voces._rehacer_mismo(os.path.join(self.root, "2026-11-01 Semanal.wav"), "h1",
                                                        {"hablantes": ["raro"]}))
        voices.write_sidecar(side, {"version": 1, "hablantes": ["raro"]})
        self.assertFalse(app_voces.Voces._rehacer_mismo(os.path.join(self.root, "2026-11-01 Semanal.wav"), "h1",
                                                        {"hablantes": {"h1": {}}}))

    def test_un_fallo_al_calcular_se_queda_para_la_proxima(self):
        def falla(rel, h, cancel=None):
            raise server.ApiError("No se pudo calcular la huella de ese hablante.")
        self.app.jobs.speaker_vector = falla
        res = self.app.rehacer_huellas()
        self.assertEqual(res["fallidas"], 2)
        self.assertEqual(len(self.app.jobs.voices.pendientes()), 4)

    def test_con_voces_apagadas_no_se_toca_nada(self):
        with open(self.voces, "rb") as f:
            antes = f.read()
        app = self.nueva_app(voices_enabled=False)
        app.jobs.speaker_vector = self.speaker_vector
        self.assertIsNone(app.rehacer_huellas())
        with open(self.voces, "rb") as f:
            self.assertEqual(f.read(), antes)
        self.assertEqual(self.calculadas, [])

    def test_sin_la_libreria_de_voces_solo_se_migra(self):
        with mock.patch.object(voices, "available", return_value=(False, "No module named 'sherpa_onnx'")):
            self.assertIsNone(self.app.rehacer_huellas())
        self.assertEqual(self.calculadas, [])
        ahora = leer_voces(self.voces)
        self.assertEqual([p["nombre"] for p in ahora["personas"]], [p["nombre"] for p in self.antes["personas"]])
        self.assertEqual(len(self.app.jobs.voices.pendientes()), 4)

    def test_sin_modelos_los_baja_primero(self):
        listos = [False]

        def bajar():
            self.app.voices_dl = {"running": True}

            def fin():
                time.sleep(0.2)
                listos[0] = True
                self.app.voices_dl["running"] = False
            threading.Thread(target=fin, daemon=True).start()
        self.app._download_models = bajar
        with mock.patch.object(voices, "models_ready", side_effect=lambda d: listos[0]):
            res = self.app.rehacer_huellas()
        self.assertEqual(res["rehechas"], 2)

    def test_si_no_se_pueden_bajar_los_modelos_no_se_pierde_nada(self):
        def bajar():
            self.app.voices_dl = {"running": False, "error": "HTTP 403"}
        self.app._download_models = bajar
        with mock.patch.object(voices, "models_ready", return_value=False):
            self.assertIsNone(self.app.rehacer_huellas())
        self.assertEqual(len(self.app.jobs.voices.pendientes()), 4)

    def test_dos_a_la_vez_solo_una(self):
        grabando = [True]
        self.app.rec = types.SimpleNamespace(alive=lambda: grabando[0])
        t = threading.Thread(target=self.app.rehacer_huellas, daemon=True)
        t.start()
        time.sleep(0.2)
        self.assertIsNone(self.app.rehacer_huellas())  # la otra sigue en marcha: esta no hace nada
        grabando[0] = False
        t.join(10)
        self.assertEqual(len(self.calculadas), 2)

    def test_un_audio_solo_en_linea_no_se_lee(self):
        flat = self.app.library.refresh() and self.app.library.flat
        for a in flat:
            a["cloud"] = True  # OneDrive «solo en línea»: leerlo lo descargaria
        self.app.library.refresh = lambda max_age=0: None
        res = self.app.rehacer_huellas()
        self.assertEqual((res["rehechas"], self.calculadas), (0, []))

    def test_la_misma_instancia_de_voces_aunque_la_pidan_dos_hilos(self):
        jobs = self.app.jobs
        vistos, ir = [], threading.Event()

        def pide():
            ir.wait()
            vistos.append(jobs.voices)
        hilos = [threading.Thread(target=pide) for _ in range(8)]
        real = voices.Voices

        def lenta(path):  # que dos hilos se crucen al crearla
            time.sleep(0.05)
            return real(path)
        with mock.patch.object(voices, "Voices", side_effect=lenta):
            for t in hilos:
                t.start()
            ir.set()
            for t in hilos:
                t.join(10)
        self.assertEqual(len({id(v) for v in vistos}), 1)

    def test_main_lo_arranca_en_su_hilo(self):
        with open(os.path.join(APP, "server.py"), encoding="utf-8") as f:
            arbol = ast.parse(f.read())
        main = next(n for n in arbol.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        targets = [ast.unparse(k.value) for n in ast.walk(main) if isinstance(n, ast.Call)
                   and ast.unparse(n.func) == "threading.Thread" for k in n.keywords if k.arg == "target"]
        self.assertIn("Handler.app.rehacer_huellas_al_arrancar", targets)

    def test_activar_voces_lo_pide_tambien(self):
        pedidos = []
        self.app.rehacer_huellas = lambda *a: pedidos.append(a)
        with mock.patch.object(app_voces, "log"):
            self.app.voices_toggle({"on": True})
        time.sleep(0.2)
        self.assertEqual(pedidos, [()])


class SubprocesoCancelableTests(TempTree):
    def test_cancel_mata_el_subproceso(self):
        """speaker_vector/_voices_subprocess con cancel(): el proceso de voces se corta (como con un trabajo)."""
        with open(os.path.join(self.app_dir, "voices.py"), "w", encoding="utf-8") as f:
            f.write("import time\ntime.sleep(60)\n")
        jobs = bare_jobs([])
        t0 = time.time()
        with self.assertRaises(engines.Cancelled):
            jobs._voices_subprocess(None, ["--fingerprint", "h1"], cancel=lambda: time.time() > t0 + 0.5)
        self.assertLess(time.time() - t0, 15)
        vistos = []
        jobs.voice_vectors = {}
        jobs._voices_subprocess = lambda job, args, idle_timeout=1800, cancel=None: vistos.append(cancel) or {
            "huellas": {"h1": [0.1] * voices.DIM}, "segundos": 30.0}
        self.wav("r.wav")
        corta = lambda: False  # noqa: E731
        self.assertEqual(jobs.speaker_vector("r.wav", "h1", cancel=corta)[1], 30.0)
        self.assertIs(vistos[0], corta)


class SinHuellasTrasElCambioTests(Reunion):
    """Una persona que solo tiene huellas por rehacer tenia huellas: sus asignaciones de un sidecar de antes del
    07/11/2026 (sin «origen») pudieron ser automaticas y no pasan a «manual» al volver a identificar."""

    def test_no_pasa_a_manual(self):
        path = os.path.join(self.data_dir, "voces.json")
        shutil.copy(VIEJO, path)
        self.v = self.app.jobs._voices = voices.Voices(path)  # el voces.json de antes, migrado
        ids = {p["nombre"]: p["id"] for p in self.v.personas()}
        # un sidecar como los de antes del 07/11 (sin «origen»): h3, Beta (migrada: sin huellas, con una por rehacer);
        # h1, Gamma (nunca tuvo huellas: solo pudo ponerse a mano); h2, del usuario por el micro
        voices.write_sidecar(self.sidecar(), {
            "version": 1, "creado": "2026-11-05T10:00:00", "tu": "h2",
            "tramos": [{"inicio": 0, "fin": 10, "h": "h1"}, {"inicio": 10, "fin": 20, "h": "h2"},
                       {"inicio": 20, "fin": 30, "h": "h1"}, {"inicio": 30, "fin": 40, "h": "h3"}],
            "hablantes": {"h1": {"segundos": 20.0, "persona": ids["Persona Gamma"], "provisional": False},
                          "h2": {"segundos": 20.0, "persona": ids["Yo"], "provisional": False},
                          "h3": {"segundos": 20.0, "persona": ids["Persona Beta"], "provisional": False}},
            "micro": {"paso": 0.25, "activo": "0" * 40 + "1" * 40 + "0" * 80}})
        self.app.jobs.voice_vectors["r.wav"] = {h: list(map(float, v)) for h, v in self.HUELLAS.items()}
        jobs = self.app.jobs

        def fake_subprocess(job, args, idle_timeout=1800):
            new = voices.read_sidecar(self.sidecar())
            for i in new["hablantes"].values():
                i.update(persona=None, score=None, provisional=False)
            new["creado"], new["tu"] = "2026-11-15T12:00:00", None
            voices.write_sidecar(args[args.index("--out") + 1], new)
            return {"huellas": {h: list(map(float, v)) for h, v in self.HUELLAS.items()}}
        jobs._voices_subprocess, jobs.auto_summary = fake_subprocess, (lambda rel: None)
        job = server.Job("r.wav", "r.wav", "voces", None)
        with mock.patch.object(voices, "models_ready", return_value=True):
            jobs._run_voices(job)
        self.assertEqual(job.status, "done", job.error)
        hablantes = self.leer()["hablantes"]
        self.assertNotEqual(hablantes["h3"].get("persona"), ids["Persona Beta"])  # era automatica
        self.assertEqual((hablantes["h1"].get("persona"), hablantes["h1"].get("origen")),
                         (ids["Persona Gamma"], "manual"))  # la regla de siempre


class DiagnosticoVocesTests(unittest.TestCase):
    """Las lineas nuevas de «Quién habla» salen en «Copiar diagnóstico» con lo técnico y sin nombres."""

    LINEAS = [
        (f"   voces.json: modelo de huellas {voices.MODEL_ID}; 4 huellas por rehacer desde sus reuniones", None),
        ("   huella por rehacer (Junta Omega.m4a, h2): MemoryError", "   huella por rehacer (<texto>, <texto>): "
                                                                     "MemoryError"),
        ("   huellas rehechas con el modelo nuevo: {'rehechas': 2, 'sin_audio': 1, 'fallidas': 0}", None),
        ("🗣 4 huellas de voz por rehacer con el modelo nuevo: se rehacen en segundo plano", None),
        ("🗣 1 huella de voz por rehacer con el modelo nuevo: se rehacen en segundo plano", None),
        ("   huellas por rehacer: se espera a que termine lo que está en marcha (grabando)", None),
        ("   huellas por rehacer: sin los modelos de voces (HTTP 403: Omega); se intentará la próxima vez",
         "   huellas por rehacer: sin los modelos de voces (<texto> (HTTP 403)); se intentará la próxima vez"),
        ("🗣 huellas de voz con el modelo nuevo: 2 rehechas, 1 sin su reunión, 0 por reintentar", None),
        ("   voces.json no está; se recupera de voces.json.bak", None),
        ("   idioma: es-en-pt (80%)", None),
        ("   directo: idioma es-en-pt desde el siguiente trozo", None),
    ]

    def test_lo_tecnico_se_queda(self):
        h = "2026-11-15 10:00:00 I "
        for linea, esperado in self.LINEAS:
            with self.subTest(linea):
                self.assertEqual(server.diag_line(h + linea), h + (esperado or linea))


# ----------------------------------------------------------------------------- 54: es-en-pt en todo el servidor

class TresIdiomasTests(TempTree):
    def test_el_mismo_idioma_re_en_todas_partes(self):
        import biblioteca
        import listas
        self.assertIs(biblioteca.IDIOMA_RE, engines.IDIOMA_RE)
        self.assertIs(listas.IDIOMA_RE, engines.IDIOMA_RE)
        self.assertIs(server.IDIOMA_RE, engines.IDIOMA_RE)
        self.assertEqual(plan.IDIOMA_RE.pattern, engines.IDIOMA_RE.pattern)  # copiado: plan no importa engines
        self.assertTrue(server.IDIOMA_RE.match("es-en-pt"))
        self.assertFalse(server.IDIOMA_RE.match("es-fr-it"))

    def test_la_reunion_se_queda_con_la_mezcla_de_tres(self):
        self.wav("r.wav")
        app = server.App.__new__(server.App)
        app.notes_lock, app.library = threading.Lock(), types.SimpleNamespace(at=1)
        app.jobs = types.SimpleNamespace(refresh_prompt=lambda rel: None, schedule_outputs=lambda rel: None)
        out = app.notes_save({"path": "r.wav", "props": {"idioma": "es-en-pt"}})
        self.assertEqual(out["props"]["idioma"], "es-en-pt")
        self.assertEqual(server.idioma_de(out["props"], {"language": "es"}), "es-en-pt")
        self.assertEqual(server.check_perfil({"language": "es-en-pt"})["language"], "es-en-pt")

    def test_idioma_del_repaso_con_tres(self):
        self.assertEqual(server.idioma_repaso(["es-en", "pt"]), "es-en-pt")
        self.assertEqual(server.idioma_repaso(["es", "en", "pt"]), "es-en-pt")
        self.assertEqual(server.idioma_repaso(["en", "pt"]), "en-pt")  # como antes
        self.assertEqual(server.idioma_repaso(["fr", "es", "en"]), "")  # sin esa mezcla: detectar

    def test_frase_de_estilo_de_la_mezcla(self):
        for lang in ("es-en-pt", "es-en", "es-pt", "", None):
            self.assertEqual(server.vocab_fit("Alfa, Beta", lang)["style_tokens"],
                             engines.count_tokens(engines.style_for(lang)), lang)
        self.assertGreater(server.vocab_fit("Alfa", "es-en-pt")["style_tokens"], 0)

    def test_desplegable_y_plan(self):
        with open(os.path.join(APP, "static", "app.js"), encoding="utf-8") as f:
            self.assertIn("['es-en-pt', 'Español + inglés + portugués (mezcla)']", f.read())
        self.assertEqual(plan.IDIOMAS["es-en-pt"], "Español + inglés + portugués")
        self.assertFalse(plan.idioma_fijo("es-en-pt"))


# ----------------------------------------------------------------------------- 35: el repaso final, por tramos

class RepasoPorTramosTests(LiveJobs):
    def test_el_repaso_recibe_los_tramos_del_directo(self):
        jobs = self.jobs()
        jobs._cloud_ready = lambda: True
        visto = {}

        def transcribe(*a, **k):
            visto.update(k, language=a[3])
            raise engines.Cancelled()
        jobs.cloud.transcribe = transcribe
        job = self.live_job()
        job.language, job.idiomas_directo = "pt", ["en", "pt"]
        job.idiomas_tramos = [(0.0, "en"), (61.5, "pt")]
        with mock.patch.object(server, "log"):
            self.assertTrue(jobs._final_pass(job))
            fp = next(j for j in jobs.list if j.final_pass)
            self.assertEqual(fp.idiomas_tramos, [(0.0, "en"), (61.5, "pt")])
            jobs._run(fp)
        self.assertEqual(visto["tramos_idioma"], [(0.0, "en"), (61.5, "pt")])
        self.assertEqual(visto["language"], "en-pt")  # para un tramo sin idioma propio

    def test_un_trabajo_que_no_es_repaso_no_los_lleva(self):
        jobs = self.jobs()
        jobs._cloud_ready = lambda: True
        visto = {}

        def transcribe(*a, **k):
            visto.update(k)
            raise engines.Cancelled()
        jobs.cloud.transcribe = transcribe
        j = server.Job("r.wav", "r.wav", "nube", None)
        j.idiomas_tramos = [(0.0, "en"), (61.5, "pt")]  # (no deberia tenerlos; aunque los tuviera)
        with mock.patch.object(server, "log"):
            jobs._run(j)
        self.assertIsNone(visto["tramos_idioma"])

    def test_sin_cambios_de_idioma_va_como_siempre(self):
        jobs = self.jobs()
        jobs._cloud_ready = lambda: True
        visto = {}

        def transcribe(*a, **k):
            visto.update(k)
            raise engines.Cancelled()
        jobs.cloud.transcribe = transcribe
        with mock.patch.object(server, "log"):
            jobs._final_pass(self.live_job())
            jobs._run(next(j for j in jobs.list if j.final_pass))
        self.assertIsNone(visto["tramos_idioma"])

    def test_el_cupo_gratuito_de_groq_lo_respeta_el_reparto_de_claves(self):
        """Partes de ~90 s en vez de 10 min: 1 h son ~40 peticiones. Con una sola clave van, como poco, a 3 s una de
        otra (20 por minuto) y los segundos de audio son los mismos (7.200 por hora)."""
        self.assertGreaterEqual(engines.RATE_SPACING["whisper"], 60 / 20)
        partes = engines.CloudEngine.plan_parts(3600.0, [], 600, None, tramos=[(0.0, "en"), (1800.0, "pt")])
        self.assertAlmostEqual(sum(b - a for a, b, _ in partes), 3600.0, places=3)


# ----------------------------------------------------------------------------- 11: los 500

class ServidorDePrueba:
    def __init__(self, app):
        self.srv = server.Server(("127.0.0.1", 0), server.Handler)
        self.port = self.srv.server_address[1]
        self.saved = (server.PORT, server.URL, server.Handler.app, server.Handler.token)
        server.PORT, server.URL = self.port, f"http://127.0.0.1:{self.port}/"
        server.Handler.app, server.Handler.token = app, "prueba-enganches"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def pedir(self, ruta, cuerpo=None):
        req = urllib.request.Request(server.URL + ruta.lstrip("/"), data=None if cuerpo is None else json.dumps(
            cuerpo).encode(), method="GET" if cuerpo is None else "POST", headers={
                "X-Audios": "1", "Content-Type": "application/json", "X-Chuleta-Token": "prueba-enganches"})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def cerrar(self):
        self.srv.shutdown()
        self.srv.server_close()
        server.PORT, server.URL, server.Handler.app, server.Handler.token = self.saved


class ErroresInternosTests(unittest.TestCase):
    def setUp(self):
        self.app = mock.Mock()
        self.s = ServidorDePrueba(self.app)
        self.addCleanup(self.s.cerrar)
        p = mock.patch.object(api, "log_exc")
        self.log_exc = p.start()
        self.addCleanup(p.stop)

    def test_un_500_da_la_frase_y_el_detalle(self):
        self.app.config.side_effect = ValueError("C:\\ruta\\rara: algo")
        self.app.rec_siguiente.side_effect = KeyError("path")
        for ruta, cuerpo, detalle in (("/api/config", None, "ValueError: C:\\ruta\\rara: algo"),
                                      ("/api/rec/siguiente", {}, "KeyError: 'path'")):
            code, r = self.s.pedir(ruta, cuerpo)
            esperado = {"error": f"Algo ha fallado dentro de {server.APP_NAME}", "detalle": detalle}
            self.assertEqual((code, r), (500, esperado), ruta)
        self.assertEqual(self.log_exc.call_count, 2)

    def test_un_error_de_conexion_dentro_de_la_ruta_es_un_500(self):
        """Una ruta que habla con la nube y esta le corta (RemoteDisconnected es un ConnectionError): eso sí es un
        fallo, y se contesta."""
        self.app.test_cloud.side_effect = http.client.RemoteDisconnected("cortada")
        code, r = self.s.pedir("/api/cloud/test", {})
        self.assertEqual((code, r["detalle"]), (500, "RemoteDisconnected: cortada"))

    def test_quien_ya_se_fue_no_deja_traza(self):
        """El aviso flotante manda su POST y se cierra: escribir la respuesta da ConnectionResetError (en Windows,
        WinError 10054). Antes, log_exc dejaba la traza en server.log en cada ✕."""
        self.app.rec_siguiente.return_value = {"ok": True}
        real = api.Handler._json

        def json_cortado(handler, obj, code=200):
            if obj == {"ok": True}:
                raise ConnectionResetError(10054, "conexión cerrada por el otro lado")
            return real(handler, obj, code)
        with mock.patch.object(api.Handler, "_json", json_cortado):
            with self.assertRaises((http.client.RemoteDisconnected, ConnectionError, urllib.error.URLError)):
                self.s.pedir("/api/rec/siguiente", {"descartar": True, "origen": "aviso"})
        time.sleep(0.2)
        self.assertEqual(self.log_exc.call_count, 0)


# ----------------------------------------------------------------------------- 38

class SinProyectoTests(TempTree):
    def app(self):
        self.wav("r.wav")
        a = server.App.__new__(server.App)
        a.notes_lock, a.library = threading.Lock(), types.SimpleNamespace(at=1)
        a.jobs = types.SimpleNamespace(refresh_prompt=lambda rel: None, schedule_outputs=lambda rel: None)
        return a

    def md(self):
        return server.parse_notes(server.read_text(os.path.join(self.root, "r.md")))[0]

    def test_se_guarda_en_la_cabecera(self):
        a = self.app()
        self.assertEqual(a.notes_save({"path": "r.wav", "props": {"sin_proyecto": "sí"}})["props"]["sin_proyecto"],
                         "sí")
        self.assertEqual(self.md(), {"sin_proyecto": "sí"})
        a.notes_save({"path": "r.wav", "props": {"titulo": "Charla"}})  # otra cosa no lo quita
        self.assertEqual(self.md(), {"titulo": "Charla", "sin_proyecto": "sí"})
        a.notes_save({"path": "r.wav", "props": {"sin_proyecto": ""}})
        self.assertEqual(self.md(), {"titulo": "Charla"})
        a.notes_save({"path": "r.wav", "props": {"sin_proyecto": "<b>cualquier cosa</b>"}})  # solo «sí» o nada
        self.assertNotIn("sin_proyecto", self.md())
        a.notes_save({"path": "r.wav", "props": {"sin_proyecto": True}})
        self.assertEqual(self.md()["sin_proyecto"], "sí")

    def test_elegir_un_proyecto_lo_quita(self):
        self.write("Alfa/Vocabulario/General.txt", "alfa\n")
        a = self.app()
        a.notes_save({"path": "r.wav", "props": {"sin_proyecto": "sí"}})
        out = a.notes_save({"path": "r.wav", "props": {"vocabulario": "Alfa/Vocabulario/General.txt"}})
        self.assertNotIn("sin_proyecto", out["props"])
        self.assertNotIn("sin_proyecto", self.md())
        self.assertEqual(self.md()["workstream"], "General")

    def test_una_reunion_con_proyecto_no_es_de_ninguno(self):
        """Revisión 1 (P2-6): con su proyecto (workstream, lo que mira Hoy) ya puesto, un «ninguno» que llegue (la
        migración de lo apuntado en el PC, otra ventana) no se guarda."""
        self.write("Alfa/Vocabulario/General.txt", "alfa\n")
        a = self.app()
        a.notes_save({"path": "r.wav", "props": {"vocabulario": "Alfa/Vocabulario/General.txt"}})
        out = a.notes_save({"path": "r.wav", "props": {"sin_proyecto": "sí"}})
        self.assertNotIn("sin_proyecto", out["props"])
        self.assertNotIn("sin_proyecto", self.md())

    def test_el_arbol_lo_ve(self):
        a = self.app()
        a.notes_save({"path": "r.wav", "props": {"sin_proyecto": "sí"}})
        lib = server.Library(server.Durations(os.path.join(self.data_dir, "durations.json")))
        lib.refresh()
        self.assertEqual(lib.find("r.wav")["props"].get("sin_proyecto"), "sí")


class AgendaOcupadaTests(unittest.TestCase):
    def app(self):
        a = server.App.__new__(server.App)
        a.rec, a.meeting, a.meeting_prev = None, None, None
        return a

    def test_grabando_llamada_o_nada(self):
        a = self.app()
        self.assertIsNone(a._agenda_ocupada())
        a.meeting_prev = "Teams"  # el registro de privacidad: hay llamada aunque el aviso de llamadas este apagado
        self.assertEqual(a._agenda_ocupada(), "llamada")
        a.rec = types.SimpleNamespace(alive=lambda: True)
        self.assertEqual(a._agenda_ocupada(), "grabando")

    def test_va_en_api_hoy(self):
        from test_hoy import ServidorTests
        t = ServidorTests("test_api_hoy_y_ctrl_alt_r_con_la_misma_reunion")
        t.setUp()
        self.addCleanup(t.tearDown)
        a = t.app(outlook_on=True)
        self.assertIsNone(a.hoy({})["agenda"]["ocupado"])
        a.meeting = {"app": "Teams"}
        self.assertEqual(a.hoy({})["agenda"]["ocupado"], "llamada")


class AvisoSiguienteTests(unittest.TestCase):
    def app(self):
        a = server.App.__new__(server.App)
        a.siguiente, a._siguiente_cache = {"hasta": time.time() + 60, "excluir": []}, (0.0, None)
        a.vigia = types.SimpleNamespace(tipo="siguiente", cerrados=0)
        a.vigia.cerrar_aviso = lambda: setattr(a.vigia, "cerrados", a.vigia.cerrados + 1)
        return a

    def test_la_x_del_aviso_no_lo_mata(self):
        a = self.app()
        with mock.patch.object(server, "log"):
            self.assertEqual(a.rec_siguiente({"descartar": True, "origen": "aviso"}), {"ok": True})
        self.assertTrue(a.siguiente["descartada"])
        self.assertEqual(a.vigia.cerrados, 0)

    def test_ahora_no_desde_la_app_si_lo_cierra(self):
        a = self.app()
        with mock.patch.object(server, "log"):
            a.rec_siguiente({"descartar": True})
        self.assertEqual(a.vigia.cerrados, 1)


# ----------------------------------------------------------------------------- B8: la muestra de una voz

class MuestraTests(TempTree):
    def test_lo_mas_largo_de_esa_voz_sola(self):
        tramos = [{"inicio": 0, "fin": 4, "h": "h1"}, {"inicio": 4, "fin": 20, "h": "h2"},
                  {"inicio": 6, "fin": 7, "h": "h1"}, {"inicio": 20, "fin": 23, "h": "h1"}]
        # h2 de 4 a 20, con h1 encima de 6 a 7: lo mas largo de h2 solo es 7-20, sus 5 primeros s
        self.assertEqual(app_voces.muestra_de({"tramos": tramos}, "h2"), {"inicio": 7.0, "fin": 12.0})
        self.assertEqual(app_voces.muestra_de({"tramos": tramos}, "h1"), {"inicio": 0.0, "fin": 4.0})
        self.assertIsNone(app_voces.muestra_de({"tramos": tramos}, "h9"))
        self.assertIsNone(app_voces.muestra_de(None, "h1"))
        # todo solapado: su tramo mas largo, aunque se oiga a otro
        sola = [{"inicio": 0, "fin": 10, "h": "h1"}, {"inicio": 0.5, "fin": 9.8, "h": "h2"}]
        self.assertEqual(app_voces.muestra_de({"tramos": sola}, "h2"), {"inicio": 0.5, "fin": 5.5})
        raros = [{"inicio": "x", "fin": 3, "h": "h1"}, {"fin": 3, "h": "h1"}, {"inicio": 5, "fin": 5, "h": "h1"},
                 {"inicio": 1, "fin": 2.5, "h": "h1"}]
        self.assertEqual(app_voces.muestra_de({"tramos": raros}, "h1"), {"inicio": 1.0, "fin": 2.5})

    def test_la_ruta(self):
        self.wav("r.wav")
        voices.write_sidecar(os.path.join(self.root, "r.hablantes.json"), {
            "version": 1, "creado": "2026-11-15T10:00:00", "tramos": [{"inicio": 10, "fin": 14.8, "h": "h2"}],
            "hablantes": {"h2": {"segundos": 4.8}}})
        app = server.App.__new__(server.App)
        self.assertEqual(app.speaker_sample({"path": "r.wav", "h": "h2"}),
                         {"inicio": 10.0, "fin": 14.8, "creado": "2026-11-15T10:00:00"})
        with self.assertRaises(server.ApiError):
            app.speaker_sample({"path": "r.wav", "h": "h1"})
        s = ServidorDePrueba(app)
        self.addCleanup(s.cerrar)
        self.assertEqual(s.pedir("/api/hablantes/muestra?path=r.wav&h=h2")[1]["inicio"], 10.0)


# ----------------------------------------------------------------------------- B5: quién hizo el resumen

class ProveedorDelResumenTests(TempTree):
    def jobs(self, **st):
        j = bare_jobs([])
        j.settings = server.Store(os.path.join(self.data_dir, "settings.json"), server.DEFAULT_SETTINGS)
        j.settings.update(lambda d: d.update(st))
        j.library = types.SimpleNamespace(at=1, find=lambda rel: {"duration": 60, "date": "2026-11-15T10:00:00"})
        j.summary_missing = lambda: None
        j._with_speakers = lambda abs_path, segs, meta: segs
        j.write_outputs = lambda rel: None
        j.summarizer = types.SimpleNamespace(summarize=lambda *a: ("## Resumen\nTexto.", "openai/gpt-oss-120b"))
        j._summarizer = lambda st: j.summarizer
        return j

    def resumir(self, **st):
        self.wav("r.wav")
        self.write("r.srt", "1\n00:00:00,000 --> 00:00:02,000\nhola\n")
        jobs = self.jobs(**st)
        job = server.Job("r.wav", "r.wav", "resumen", None)
        with mock.patch.object(server, "log"):
            jobs._run_summary(job)
        self.assertEqual(job.status, "done", job.error)
        app = server.App.__new__(server.App)
        app.jobs = types.SimpleNamespace(summary_ready=lambda: True)
        return app.summary_get({"path": "r.wav"})

    def test_groq(self):
        r = self.resumir(procesado="api")
        self.assertEqual((r["meta"]["proveedor"], r["meta"]["modelo"]), ("Groq", "openai/gpt-oss-120b"))
        self.assertEqual(r["text"], "## Resumen\nTexto.")

    def test_tu_cuenta_de_claude(self):
        r = self.resumir(procesado="agente")
        self.assertEqual(r["meta"]["proveedor"], "tu cuenta de Claude")

    def test_una_api_propia_sin_el_nombre_de_su_servidor(self):
        st = dict(server.DEFAULT_SETTINGS["cloud"], provider="custom", base_url="https://pasarela.empresa.example/v1")
        r = self.resumir(procesado="api", cloud=st)
        self.assertEqual(r["meta"]["proveedor"], "una API compatible con OpenAI")
        self.assertNotIn("pasarela", server.read_text(os.path.join(self.root, "r.resumen.md")))

    def test_el_documento_no_lleva_la_cabecera(self):
        self.resumir(procesado="api")
        jobs = self.jobs()
        jobs.library = types.SimpleNamespace(find=lambda rel: {})
        doc = jobs.document_for("r.wav")
        self.assertEqual((doc["summary"], doc["summary_model"]), ("## Resumen\nTexto.", "openai/gpt-oss-120b"))


@unittest.skipUnless(shutil.which("node"), "node no instalado")
class ResumenAutorJsTests(unittest.TestCase):
    def test_con_proveedor_y_sin_el(self):
        from test_frontend import js_defs, read
        defs = js_defs(read("app.js"), ["MES", "esc"]) + "\n" + js_defs(read("notas.js"), ["resumenAutor"])
        code = ("console.log(resumenAutor({modelo: 'openai/gpt-oss-120b', proveedor: 'Groq', generado: "
                "'2026-11-12 16:09'}));"
                "console.log(resumenAutor({modelo: 'claude-sonnet-4-5', proveedor: 'tu cuenta de Claude'}));"
                "console.log(resumenAutor({modelo: 'claude-sonnet-4-5'}));"  # uno de antes, sin proveedor
                "console.log(resumenAutor({proveedor: 'Groq'}));")
        r = subprocess.run(["node", "-e", defs + "\n" + code], capture_output=True, text=True, encoding="utf-8",
                           timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), [
            "Resumen hecho con IA por Groq (openai/gpt-oss-120b) el 12 nov 2026 a las 16:09",
            "Resumen hecho con IA por tu cuenta de Claude (claude-sonnet-4-5)",
            "Resumen hecho con IA por Claude (claude-sonnet-4-5)",
            "Resumen hecho con IA por Groq"])


# ----------------------------------------------------------------------------- la app de verdad, con voces de antes

class ArranqueConVocesViejasTests(unittest.TestCase):
    """server.py de verdad con el voces.json que dejo el codigo de antes: arranca, y Ajustes › Voces enseña las mismas
    personas, con su nombre, «Soy yo» y consentimiento, y sus huellas por rehacer (sin sherpa-onnx aqui no se calculan:
    eso lo prueban RehacerAlArrancarTests y, en Windows, scripts/e2e/voces_memoria.py)."""

    def test_las_personas_siguen(self):
        tmp = tempfile.mkdtemp(prefix="tenca-enganches-")
        self.addCleanup(shutil.rmtree, tmp, True)
        root, datos = os.path.join(tmp, "Tenca"), os.path.join(tmp, "datos")
        shutil.copytree(APP, os.path.join(root, "_app"), ignore=shutil.ignore_patterns("__pycache__"))
        os.makedirs(datos)
        shutil.copy(VIEJO, os.path.join(datos, "voces.json"))
        with open(os.path.join(datos, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"voices_enabled": True}, f)
        with open(os.path.join(root, "2026-11-01 Semanal.wav"), "wb") as f:
            f.write(wav_bytes(0.5))
        port = puerto_libre()
        log = open(os.path.join(tmp, "salida.log"), "w", encoding="utf-8")
        self.addCleanup(log.close)
        proc = subprocess.Popen([sys.executable, os.path.join(root, "_app", "server.py"), "--port", str(port),
                                 "--data", datos, "--no-browser"], cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                env=dict(os.environ, PYTHONIOENCODING="utf-8"))

        def parar():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self.addCleanup(parar)
        base, fin, token, estado = f"http://127.0.0.1:{port}", time.time() + 60, None, None
        while time.time() < fin and proc.poll() is None:
            try:
                with open(os.path.join(datos, "sesion.token"), encoding="utf-8") as f:
                    token = f.read().strip()
                req = urllib.request.Request(base + "/api/voces", headers={"X-Chuleta-Token": token})
                estado = json.load(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5))
                break
            except (OSError, ValueError):
                time.sleep(0.3)
        if estado is None:
            self.skipTest("el servidor de prueba no arrancó")
        antes = {p["id"]: p for p in leer_voces(VIEJO)["personas"]}
        ahora = {p["id"]: p for p in estado["personas"]}
        self.assertEqual(set(ahora), set(antes))
        for pid, p in antes.items():
            self.assertEqual((ahora[pid]["nombre"], ahora[pid]["es_usuario"], ahora[pid]["consentimiento"]),
                             (p["nombre"], p["es_usuario"], p["consentimiento"]))
            self.assertEqual((ahora[pid]["huellas"], ahora[pid]["por_rehacer"]), (0, len(p["huellas"])))
        self.assertEqual(estado["usuario"], next(pid for pid, p in antes.items() if p["es_usuario"]))
        self.assertIsNone(proc.poll())


if __name__ == "__main__":
    unittest.main()
