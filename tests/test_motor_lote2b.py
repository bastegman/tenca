# -*- coding: utf-8 -*-
"""Rama motor-lote2b del review del 14/11/2026.

Punto 17 · qué pasa con el audio de cada reunión (conservar.py, app_conservar.py): «Guardar» (por defecto), «Guardar 15
días» o «Borrar al terminar», por reunión. Borrar quita solo el audio, a la papelera y nunca en firme, y solo cuando la
reunión está transcrita entera y resumida y nada de la app la usa; nunca mientras se graba. Los textos se quedan y la
reunión sigue en la biblioteca con su ruta (la marca <nombre>.audio.json). Con dobles: sin Windows (la papelera es
DATA_DIR/papelera), salvo PapeleraWindowsTests."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

from support import TempTree, bare_jobs, server, srt_text, wav_bytes, FakeJob  # noqa: F401

import conservar  # noqa: E402
import plan as grabar_plan  # noqa: E402

WINDOWS = sys.platform == "win32"


def settings_store(tmp, **extra):
    st = server.Store(os.path.join(tmp, "settings.json"), server.DEFAULT_SETTINGS)
    if extra:
        st.update(lambda d: d.update(extra))
    return st


def make_app(t, **st):
    a = server.App.__new__(server.App)
    a.settings = settings_store(t.tmp, **st)
    a.rec_lock, a.notes_lock, a.rec, a.monitor = threading.Lock(), threading.Lock(), None, None
    a.library = server.Library(server.Durations(os.path.join(t.data_dir, "durations.json")))
    a.transcripts = server.Transcripts()
    a.jobs = bare_jobs([])
    a.jobs.settings, a.jobs.voice_vectors, a.jobs.started = a.settings, {}, []
    a.jobs.refresh_prompt = lambda rel: None
    a.jobs.schedule_outputs = lambda rel, delay=4.0: None
    a.jobs.forget_outputs = lambda rel: 0
    a.jobs.outputs_moved = lambda old, new, folder=False: None
    return a


RESUMEN = ("---\nmodelo: x\ngenerado: 2026-11-15 10:00\n---\n\n## Resumen\nTodo bien.\n## Próximos pasos\n"
           "- [ ] Ana: informe\n")


class Base(TempTree):
    def setUp(self):
        super().setUp()
        self.a = make_app(self)
        self._log = mock.patch.object(server, "log")
        self._log.start()
        self.addCleanup(self._log.stop)

    def reunion(self, rel="Alfa/2026-11-15 10-00-00 - Comité.wav", segundos=120.0, texto=True, resumen=True,
                pol=None, hace_dias=0, notas="Notas mías"):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(wav_bytes(segundos, rate=8000, channels=1))
        base = os.path.splitext(p)[0]
        if texto:
            self.write(os.path.relpath(base + ".srt", self.root), srt_text([(0, segundos - 1, "Hola a todos.")]))
            self.write(os.path.relpath(base + ".txt", self.root), "Hola a todos.\n")
        if resumen:
            self.write(os.path.relpath(base + ".resumen.md", self.root), RESUMEN)
        props = {"titulo": "Comité"}
        if pol:
            props["audio"] = pol
        self.write(os.path.relpath(base + ".md", self.root), server.dump_notes(props, notas))
        if hace_dias:
            t = time.time() - hace_dias * 86400
            os.utime(p, (t, t))
        self.a.library.at = 0
        return rel, p

    def entrada(self, rel):
        self.a.library.refresh()
        return self.a.library.find(rel)

    def en_papelera(self):
        d = os.path.join(self.data_dir, "papelera")
        return sorted(f for _, _, fs in os.walk(d) for f in fs) if os.path.isdir(d) else []


# ----------------------------------------------------------------------------- lógica pura

class PoliticaTests(unittest.TestCase):
    def test_politica_tal_como_venga(self):
        for v, esperado in (("borrar", "borrar"), ("Borrar al terminar", "borrar"), ("15-dias", "15-dias"),
                            ("15 días", "15-dias"), ("Guardar 15 días", "15-dias"), ("guardar", "guardar"),
                            ("", "guardar"), (None, "guardar"), ("basura", "guardar"), ("30 días", "guardar")):
            self.assertEqual(conservar.politica(v), esperado, v)

    def test_debe_borrar(self):
        base = {"txt": True, "srt": True, "partial": False, "duration": 600.0, "has_summary": True,
                "mtime": time.time(), "cloud": False}
        f = conservar.debe_borrar
        self.assertEqual(f(base, "guardar"), (False, None))
        self.assertEqual(f(base, "borrar"), (True, None))
        self.assertEqual(f(dict(base, sin_audio=True), "borrar"), (False, None))
        self.assertEqual(f(dict(base, recording=True), "borrar"), (False, "grabando"))
        self.assertEqual(f(dict(base, txt=False, srt=False), "borrar"), (False, "sin_texto"))
        self.assertEqual(f(dict(base, partial=True), "borrar"), (False, "a_medias"))
        self.assertEqual(f(dict(base, duration=None), "borrar"), (False, "a_medias"))
        self.assertEqual(f(dict(base, has_summary=False), "borrar"), (False, "sin_resumen"))
        self.assertEqual(f(dict(base, cloud=True), "borrar"), (False, "nube"))
        self.assertEqual(f(base, "15-dias"), (False, "plazo"))
        viejo = dict(base, mtime=time.time() - 16 * 86400)
        self.assertEqual(f(viejo, "15-dias"), (True, None))
        self.assertEqual(f(dict(base, mtime=time.time() - 14 * 86400), "15-dias"), (False, "plazo"))
        # sin resumen no se borra aunque haya pasado el plazo
        self.assertEqual(f(dict(viejo, has_summary=False), "15-dias"), (False, "sin_resumen"))

    def test_plazo_y_publico(self):
        t = datetime(2026, 11, 1, 18, 30).timestamp()
        self.assertEqual(conservar.publico({"mtime": t}, {"audio": "15 días"}),
                         {"audio_pol": "15-dias", "audio_hasta": "2026-11-16T18:30"})
        self.assertEqual(conservar.publico({"mtime": t}, {}), {"audio_pol": "guardar"})

    def test_el_plan_y_conservar_dicen_lo_mismo(self):
        """plan.CONSERVAR (copiado, plan no importa conservar) tiene las mismas políticas."""
        self.assertEqual(tuple(grabar_plan.CONSERVAR), conservar.POLITICAS)

    def test_safe_reunion(self):
        with self.assertRaises(server.ApiError):
            server.safe("no/existe.wav", "reunion")


# ----------------------------------------------------------------------------- la reunión sin su audio

class SinAudioTests(Base):
    def borrar(self, rel, p, pol="borrar"):
        a = self.entrada(rel)
        return conservar.borrar_audio(p, a, pol, server.to_recycle_bin)

    def test_quita_solo_el_audio_y_la_reunion_sigue(self):
        rel, p = self.reunion()
        marca = self.borrar(rel, p)
        self.assertFalse(os.path.exists(p))
        self.assertEqual(self.en_papelera(), [os.path.basename(p)])  # a la papelera (fuera de Windows, DATA_DIR)
        base = os.path.splitext(p)[0]
        for e in (".srt", ".txt", ".md", ".resumen.md", ".audio.json"):
            self.assertTrue(os.path.isfile(base + e), e)
        self.assertEqual((marca["fase"], marca["ext"], marca["duracion_s"]), ("borrado", ".wav", 120.0))
        a = self.entrada(rel)
        self.assertIsNotNone(a, "la reunión sigue en la biblioteca con la misma ruta")
        self.assertTrue(a["sin_audio"])
        self.assertEqual((a["duration"], a["has_summary"], a["srt"], a["partial"]), (120.0, True, True, False))
        self.assertTrue(a["audio_borrado"])
        # sus textos no salen como documentos sueltos
        tree = self.a.tree({})["tree"]
        alfa = next(f for f in tree["folders"] if f["name"] == "Alfa")
        self.assertEqual(alfa["docs"], [])
        self.assertEqual([x["path"] for x in alfa["audios"]], [rel])

    def test_lo_que_lee_textos_funciona_y_lo_que_necesita_el_audio_lo_dice(self):
        rel, p = self.reunion()
        self.borrar(rel, p)
        self.assertEqual(self.a.transcript({"path": rel})["kind"], "srt")
        self.assertTrue(self.a.summary_get({"path": rel})["exists"])
        self.assertEqual(self.a.notes_get({"path": rel})["body"].strip(), "Notas mías")
        self.a.notes_save({"path": rel, "body": "Notas nuevas"})
        self.assertIn("Notas nuevas", server.read_text(os.path.splitext(p)[0] + ".md"))
        self.assertEqual(self.a.speakers_get({"path": rel})["exists"], False)
        jobs = server.Jobs.__new__(server.Jobs)
        jobs.list, jobs.lock, jobs.settings, jobs.library = [], threading.Lock(), self.a.settings, self.a.library
        jobs.voice_vectors = {}
        for llamar in (lambda: jobs.submit([rel], "rapido", None), lambda: jobs.start_live(rel),
                       lambda: jobs.speaker_vector(rel, "h1")):
            with self.assertRaises(server.ApiError) as cm:
                llamar()
            self.assertIn("papelera", cm.exception.msg)
            self.assertEqual(cm.exception.code, 409)
        with mock.patch.object(jobs, "voices_on", return_value=True):
            with self.assertRaises(server.ApiError) as cm:
                jobs.submit_voices(rel)
        self.assertIn("papelera", cm.exception.msg)
        with self.assertRaises(server.ApiError):  # escuchar: no existe el archivo
            server.safe(rel, "file")
        # y no se puede volver a poner una política de borrar en una que ya no tiene audio
        with self.assertRaises(server.ApiError):
            self.a.notes_save({"path": rel, "props": {"audio": "borrar"}})

    def test_mover_y_renombrar_sin_audio(self):
        rel, p = self.reunion()
        self.borrar(rel, p)
        os.makedirs(os.path.join(self.root, "Beta"))
        r = self.a.move({"path": rel, "folder": "Beta", "name": "2026-11-15 10-00-00 - Otro"})
        self.assertEqual(r["path"], "Beta/2026-11-15 10-00-00 - Otro.wav")
        for e in (".srt", ".txt", ".md", ".resumen.md", ".audio.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.root, "Beta", "2026-11-15 10-00-00 - Otro" + e)), e)
        self.assertFalse(os.path.exists(os.path.dirname(p) + "/2026-11-15 10-00-00 - Comité.audio.json"))
        a = self.entrada(r["path"])
        self.assertTrue(a and a["sin_audio"])
        # a una carpeta con un audio de ese nombre, no (la marca acabaría siendo de otro audio)
        otro = os.path.join(self.root, "Alfa", "2026-11-15 10-00-00 - Otro.wav")
        with open(otro, "wb") as f:
            f.write(wav_bytes(1.0, rate=8000, channels=1))
        with self.assertRaises(server.ApiError):
            self.a.move({"path": r["path"], "folder": "Alfa"})

    def test_eliminar_una_sin_audio(self):
        rel, p = self.reunion()
        self.borrar(rel, p)
        out = self.a.delete({"path": rel})
        self.assertEqual(out["count"], 5)  # sus textos y la marca, a la papelera
        self.assertIsNone(self.entrada(rel))

    def test_su_carpeta_de_reunion_sigue_siendo_suya(self):
        rel, p = self.reunion("Alfa/2026-11-15 Comité/2026-11-15 10-00-00 - Comité.wav")
        server.carpetas_reunion().add("Alfa/2026-11-15 Comité")
        self.assertEqual(server.own_meeting_dir(p), os.path.dirname(p))
        self.borrar(rel, p)
        self.assertEqual(server.own_meeting_dir(p), os.path.dirname(p))

    def test_mostrar_en_carpeta_senala_lo_que_queda(self):
        rel, p = self.reunion()
        self.borrar(rel, p)
        with mock.patch.object(server.subprocess, "Popen") as po:
            self.a.reveal({"path": rel})
        self.assertIn(os.path.splitext(p)[0] + ".txt", " ".join(map(str, po.call_args[0][0])))

    def test_si_la_papelera_falla_no_queda_marca(self):
        rel, p = self.reunion()
        a = self.entrada(rel)

        def falla(paths, aviso_firme=False):
            raise server.ApiError("No se pudo mandar a la papelera (¿archivo en uso?).")
        with self.assertRaises(server.ApiError):
            conservar.borrar_audio(p, a, "borrar", falla)
        self.assertTrue(os.path.isfile(p))
        self.assertFalse(os.path.exists(conservar.marca_de(p)))

    def test_la_papelera_se_pide_avisando_si_fuera_en_firme(self):
        rel, p = self.reunion()
        pedido = []
        conservar.borrar_audio(p, self.entrada(rel), "borrar",
                               lambda paths, aviso_firme=False: (pedido.append(aviso_firme), os.remove(paths[0])))
        self.assertEqual(pedido, [True])

    def test_una_marca_con_otra_extension_es_un_wav(self):
        rel, p = self.reunion()
        os.remove(p)
        self.write(os.path.relpath(conservar.marca_de(p), self.root), json.dumps({"ext": ".md", "fase": "borrado"}))
        a = self.entrada(rel)
        self.assertEqual((a["ext"], a["path"]), (".wav", rel))
        tree = self.a.tree({})["tree"]
        self.assertEqual(next(f for f in tree["folders"] if f["name"] == "Alfa")["docs"], [])

    def test_rehacer_huellas_no_cuenta_con_una_sin_audio(self):
        """La huella de «Recordar» se rehace oyendo la reunión: una cuyo audio está en la papelera no se busca (la
        persona queda «sin su reunión» y conserva su nombre), en vez de fallar en cada arranque."""
        rel, p = self.reunion()
        voices_ = __import__("voices")
        voices_.write_sidecar(voices_.sidecar_path(p), {"version": 1, "tramos": [{"inicio": 0, "fin": 5, "h": "h1"}],
                                                        "hablantes": {"h1": {"segundos": 5}}})
        self.borrar(rel, p)
        self.a.library.at = 0
        visto = []

        def rehacer(v, buscar, calcular, cancelled):
            visto.append(buscar(os.path.basename(p)))
            return {"rehechas": 0, "sin_audio": 1, "fallidas": 0}
        self.a.jobs.voices_on = lambda: True
        self.a.jobs._voices = mock.Mock(pendientes=lambda: [{"id": "p1"}])
        self.a.jobs.closing = False
        with mock.patch.object(voices_, "available", return_value=(True, "")), \
                mock.patch.object(voices_, "models_ready", return_value=True), \
                mock.patch.object(voices_, "rehacer_huellas", rehacer), \
                mock.patch.object(self.a, "_rehacer_esperar", lambda c: None, create=True):
            self.a._rehacer_huellas()
        self.assertEqual(visto, [[]])

    def test_scan_no_se_cae_con_una_marca_rota(self):
        rel, p = self.reunion()
        os.remove(p)
        self.write(os.path.relpath(conservar.marca_de(p), self.root), "{esto no es json")
        a = self.entrada(rel)
        self.assertTrue(a["sin_audio"])
        self.assertIsNone(a["duration"])


# ----------------------------------------------------------------------------- el vigilante

class VigiaTests(Base):
    def test_borrar_al_terminar_con_texto_y_resumen(self):
        rel, p = self.reunion(pol="borrar")
        self.assertEqual(self.a.audio_repaso(), 1)
        self.assertFalse(os.path.exists(p))
        self.assertTrue(self.entrada(rel)["sin_audio"])
        self.assertEqual(self.a.audio_repaso(), 0)  # ya no hay nada que hacer

    def test_guardar_no_borra_nunca(self):
        rel, p = self.reunion(hace_dias=40)
        self.assertEqual(self.a.audio_repaso(), 0)
        self.assertTrue(os.path.isfile(p))

    def test_espera_al_resumen(self):
        rel, p = self.reunion(pol="borrar", resumen=False)
        self.assertEqual(self.a.audio_repaso(), 0)
        self.assertTrue(os.path.isfile(p))
        self.write(os.path.relpath(os.path.splitext(p)[0] + ".resumen.md", self.root), RESUMEN)
        self.a.library.at = 0
        self.assertEqual(self.a.audio_repaso(), 1)

    def test_espera_a_la_transcripcion_entera(self):
        rel, p = self.reunion(pol="borrar", segundos=900)
        self.write(os.path.relpath(os.path.splitext(p)[0] + ".srt", self.root), srt_text([(0, 60, "Hola.")]))
        self.a.library.at = 0
        self.assertTrue(self.entrada(rel)["partial"])
        self.assertEqual(self.a.audio_repaso(), 0)
        self.assertTrue(os.path.isfile(p))

    def test_quince_dias(self):
        rel, p = self.reunion("Alfa/a.wav", pol="15-dias", hace_dias=14)
        rel2, p2 = self.reunion("Alfa/b.wav", pol="15-dias", hace_dias=16)
        self.assertEqual(self.a.audio_repaso(), 1)
        self.assertTrue(os.path.isfile(p))
        self.assertFalse(os.path.exists(p2))
        self.assertEqual(json.loads(server.read_text(conservar.marca_de(p2)))["politica"], "15-dias")

    def test_nunca_mientras_se_graba(self):
        rel, p = self.reunion(pol="borrar")
        # la grabadora de la app
        self.a.rec = mock.Mock(alive=lambda: True)
        self.assertEqual(self.a.audio_repaso(), 0)
        self.a.rec = None
        # un directo (también el de OBS)
        self.a.jobs.list.append(FakeJob("otra.wav", status="running", live=True))
        self.assertEqual(self.a.audio_repaso(), 0)
        self.a.jobs.list.clear()
        # OBS grabando otra reunión (sin directo)
        self.wav("obs.wav", 3.0, open_header=True)
        self.a.library.at = 0
        self.assertEqual(self.a.audio_repaso(), 0)
        self.assertTrue(os.path.isfile(p))
        os.remove(os.path.join(self.root, "obs.wav"))
        self.a.library.at = 0
        self.assertEqual(self.a.audio_repaso(), 1)

    def test_no_mientras_la_app_la_usa(self):
        rel, p = self.reunion(pol="borrar")
        j = FakeJob(rel, kind="voces", status="queued")
        self.a.jobs.list.append(j)
        self.assertEqual(self.a.audio_repaso(), 0)
        self.a.jobs.list.clear()
        self.a.jobs._pending().set(rel)  # repaso final pendiente de un cierre
        self.assertEqual(self.a.audio_repaso(), 0)
        self.a.jobs._pending().set(rel, False)
        self.a._movs().set(rel, {"to": "Beta", "next": 0})  # un movimiento pendiente
        self.assertEqual(self.a.audio_repaso(), 0)
        self.a._movs().pop(rel)
        self.assertEqual(self.a.audio_repaso(), 1)

    def test_sin_papelera_o_no_cabe_se_queda_y_se_dice(self):
        rel, p = self.reunion(pol="borrar")
        with mock.patch.object(conservar, "papelera_motivo", return_value="grande"):
            self.assertEqual(self.a.audio_repaso(), 0)
        self.assertTrue(os.path.isfile(p))
        self.assertFalse(os.path.exists(conservar.marca_de(p)))
        self.assertEqual(self.entrada(rel)["audio_no"], "grande")
        self.assertEqual(self.a.audio_repaso(), 1)  # ya cabe
        self.assertNotIn(rel, self.a.library.audio_no)

    def test_cerrada_a_medio_borrar(self):
        """La app se cerró con la marca «borrando» y el audio aún en su sitio: la marca sobra y se vuelve a mirar."""
        rel, p = self.reunion(pol="borrar")
        with open(conservar.marca_de(p), "w", encoding="utf-8") as f:
            json.dump({"fase": "borrando", "ext": ".wav"}, f)
        self.assertEqual(self.a.audio_repaso(), 0)
        self.assertFalse(os.path.exists(conservar.marca_de(p)))
        self.assertEqual(self.a.audio_repaso(), 1)

    def test_sacado_de_la_papelera_se_guarda(self):
        rel, p = self.reunion(pol="borrar")
        self.assertEqual(self.a.audio_repaso(), 1)
        shutil.copyfile(os.path.join(self.data_dir, "papelera", os.listdir(os.path.join(self.data_dir, "papelera"))[0],
                                     os.path.basename(p)), p)  # «Restaurar» de la papelera
        self.a.library.at = 0
        self.assertEqual(self.a.audio_repaso(), 0)
        self.assertTrue(os.path.isfile(p))
        self.assertFalse(os.path.exists(conservar.marca_de(p)))
        a = self.entrada(rel)
        self.assertFalse(a.get("sin_audio"))
        self.assertEqual(a["audio_pol"], "guardar")
        self.assertEqual(self.a.audio_repaso(), 0)

    def test_cambiar_la_politica_despierta_al_vigilante(self):
        rel, p = self.reunion()
        self.a.notes_save({"path": rel, "props": {"audio": "Borrar al terminar"}})
        self.assertTrue(self.a._audio_evento().is_set())
        self.assertIn("audio: borrar", server.read_text(os.path.splitext(p)[0] + ".md"))
        self.a.notes_save({"path": rel, "props": {"audio": "guardar"}})
        self.assertNotIn("audio:", server.read_text(os.path.splitext(p)[0] + ".md"))  # lo de siempre: sin clave

    def test_quedar_libre_despierta_al_vigilante(self):
        self.a._al_quedar_libre("x.wav")
        self.assertTrue(self.a._audio_evento().is_set())

    def test_main_arranca_el_vigilante(self):
        with open(os.path.join(os.path.dirname(server.__file__), "server.py"), encoding="utf-8") as f:
            self.assertIn("target=Handler.app.audio_vigia", f.read())

    def test_otra_carpeta_vinculada_de_red_no(self):
        """papelera_motivo fuera de Windows siempre deja (DATA_DIR/papelera); en Windows lo prueba la CI."""
        self.assertIsNone(conservar.papelera_motivo(__file__, 10))


class GrabarTests(Base):
    def test_la_tarjeta_y_rec_start(self):
        p = grabar_plan.resolver({}, [], pedido={"cambios": {"conservar": "borrar"}})
        linea = next(l for l in p["lineas"] if l["clave"] == "conservar")
        self.assertEqual((linea["politica"], linea["origen"]), ("borrar", "reunion"))
        self.assertEqual(p["rec"]["conservar"], "borrar")
        p = grabar_plan.resolver({}, [])
        linea = next(l for l in p["lineas"] if l["clave"] == "conservar")
        self.assertEqual((linea["politica"], linea["origen"], p["rec"]["conservar"]), ("guardar", "defecto", "guardar"))
        self.assertEqual(grabar_plan.resolver({}, [], pedido={"cambios": {"conservar": "x"}})["rec"]["conservar"],
                         "guardar")

    def test_rec_start_lo_escribe_en_el_md(self):
        from test_carpetas import FakeRec
        os.makedirs(os.path.join(self.root, "Alfa"), exist_ok=True)
        body = {"mic": "default", "sys": "all", "folder": "Alfa", "live": False, "preset": "medium", "name": "Comité",
                "vocab": "", "conservar": "15-dias"}
        self.a.jobs.claimed = set()
        self.a._cortes = mock.Mock()
        with mock.patch.object(server, "RecorderProcess", FakeRec), \
                mock.patch.object(self.a, "_reunion_ahora", return_value=({}, None), create=True):
            try:
                r = self.a.rec_start(body)
            except Exception as e:  # sin todo lo de la app, rec_start puede quejarse después de escribir el .md
                r = {"error": str(e)}
        mds = [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(self.root, "Alfa")) for f in fs
               if f.endswith(".md")]
        self.assertEqual(len(mds), 1, r)
        self.assertIn("audio: 15-dias", server.read_text(mds[0]))


@unittest.skipUnless(WINDOWS, "la papelera de Windows solo existe en Windows")
class PapeleraWindowsTests(unittest.TestCase):
    """De verdad, en la CI de Windows: un disco del PC tiene papelera y el audio acaba en ella (no borrado en firme)."""

    def test_un_archivo_del_disco_va_a_la_papelera(self):
        import tempfile
        import uuid
        d = tempfile.mkdtemp(prefix="tenca-papelera-")
        p = os.path.join(d, f"tenca-prueba-{uuid.uuid4().hex[:8]}.wav")
        with open(p, "wb") as f:
            f.write(wav_bytes(1.0, rate=8000, channels=1))
        self.assertIsNone(conservar.papelera_motivo(p, os.path.getsize(p)))
        server.to_recycle_bin([p], aviso_firme=True)
        self.assertFalse(os.path.exists(p))
        ps = ("$n = (New-Object -ComObject Shell.Application).NameSpace(10).Items() | "
              f"Where-Object {{ $_.Name -like '{os.path.splitext(os.path.basename(p))[0]}*' }}; "
              "if ($n) { 'SI' } else { 'NO' }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=60).stdout
        self.assertIn("SI", out, "el archivo no está en la papelera de Windows")

    def test_una_ruta_de_red_no_tiene_papelera(self):
        self.assertEqual(conservar.papelera_motivo(r"\\127.0.0.1\c$\no-existe.wav", 10), "sin_papelera")

    def test_lo_que_no_cabe(self):
        import tempfile
        p = os.path.join(tempfile.gettempdir(), "x.wav")
        self.assertEqual(conservar.papelera_motivo(p, 10 ** 15), "grande")


# ----------------------------------------------------------------------------- punto 56: quién habla mientras se graba

import numpy as np  # noqa: E402

import voces_directo  # noqa: E402
import voices  # noqa: E402

VOCES_DATA = os.environ.get("NOTAS_VOCES_DATA", "")
HAVE_SHERPA = voices.available()[0] if VOCES_DATA else False
HAVE_SAY = bool(shutil.which("say") and shutil.which("afconvert"))


class BloquesFalsos:
    """voices.diarize sin sherpa-onnx: cada bloque da dos grupos que dependen solo de sus muestras (como la
    diarizacion de verdad, que es determinista), con huellas de una «persona» por signo de la media."""

    def __init__(self):
        self.llamadas = []

    def __call__(self, so, paths, threads, shift, umbral, part, t_off, samples16, cb, cancelled):
        self.llamadas.append(round(t_off, 1))
        n = len(part) / voices.SR
        rng = np.random.default_rng(int(abs(int(np.asarray(part[:1000], np.int64).sum())) % 2 ** 31))
        base = rng.standard_normal(voices.DIM).astype(np.float32)
        a = base / np.linalg.norm(base)
        b = -a
        mitad = round(n / 2, 3)
        return [([(t_off, t_off + mitad)], a, mitad), ([(t_off + mitad, t_off + n)], b, n - mitad)]


class DiarizeBloquesTests(TempTree):
    def setUp(self):
        super().setUp()
        self.falsos = BloquesFalsos()
        for p in (mock.patch.object(voices, "_bloque", self.falsos), mock.patch.object(voices, "_sherpa"),
                  mock.patch.object(voices, "models_ready", return_value=True),
                  mock.patch.object(voices, "BLOQUE_S", 10.0)):
            p.start()
            self.addCleanup(p.stop)
        self.cache = os.path.join(self.data_dir, voices.BLOQUES_DIR)
        rng = np.random.default_rng(3)
        self.audio = (rng.standard_normal(int(35 * voices.SR)) * 3000).astype(np.int16)  # 35 s: 3 bloques y medio

    def diar(self, x, **kw):
        return voices.diarize(x, self.data_dir, consolidar=False, **kw)

    def test_sin_rejilla_ni_cache_como_siempre(self):
        self.diar(self.audio)
        self.assertEqual(self.falsos.llamadas, [0.0, 8.8, 17.5, 26.2])  # 4 bloques iguales (35/4), como antes
        self.assertFalse(os.path.isdir(self.cache))

    def test_rejilla_fija(self):
        self.diar(self.audio, rejilla=True)
        self.assertEqual(self.falsos.llamadas, [0.0, 10.0, 20.0, 30.0])

    def test_lo_hecho_mientras_se_graba_no_se_repite_y_da_lo_mismo(self):
        sr = voices.SR
        self.diar(self.audio[:10 * sr], rejilla=True, cache=self.cache)  # min 10 (aqui, s): el primer bloque
        self.diar(self.audio[:20 * sr], rejilla=True, cache=self.cache)  # el segundo, y el primero de la cache
        self.assertEqual(self.falsos.llamadas, [0.0, 10.0])
        final = self.diar(self.audio, rejilla=True, cache=self.cache)  # al terminar: solo lo que faltaba
        self.assertEqual(self.falsos.llamadas, [0.0, 10.0, 20.0, 30.0])
        self.falsos.llamadas.clear()
        entera = self.diar(self.audio, rejilla=True)  # sin cache: todos, y el mismo resultado
        self.assertEqual(self.falsos.llamadas, [0.0, 10.0, 20.0, 30.0])
        self.assertEqual(final["tramos"], entera["tramos"])
        self.assertEqual(final["hablantes"], entera["hablantes"])
        for h in entera["huellas"]:
            np.testing.assert_allclose(final["huellas"][h], entera["huellas"][h], atol=1e-5)

    def test_otro_audio_no_usa_la_cache(self):
        self.diar(self.audio[:10 * voices.SR], rejilla=True, cache=self.cache)
        otro = self.audio.copy()
        otro[5] += 1  # una muestra distinta en el primer bloque
        self.diar(otro[:10 * voices.SR], rejilla=True, cache=self.cache)
        self.assertEqual(self.falsos.llamadas, [0.0, 0.0])

    def test_cache_rota_o_de_otro_modelo_se_recalcula(self):
        self.diar(self.audio[:10 * voices.SR], rejilla=True, cache=self.cache)
        (f,) = os.listdir(self.cache)
        with open(os.path.join(self.cache, f), "w", encoding="utf-8") as fh:
            fh.write("{roto")
        self.diar(self.audio[:10 * voices.SR], rejilla=True, cache=self.cache)
        with open(os.path.join(self.cache, f), encoding="utf-8") as fh:
            d = json.load(fh)
        d["modelo"] = "otro"
        with open(os.path.join(self.cache, f), "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        self.diar(self.audio[:10 * voices.SR], rejilla=True, cache=self.cache)
        self.assertEqual(self.falsos.llamadas, [0.0, 0.0, 0.0])

    def test_lo_viejo_de_la_cache_se_borra(self):
        os.makedirs(self.cache)
        viejo = os.path.join(self.cache, "x" * 40 + ".json")
        with open(viejo, "w") as fh:
            fh.write("{}")
        t = time.time() - voices.BLOQUES_VIEJO_S - 60
        os.utime(viejo, (t, t))
        self.diar(self.audio[:10 * voices.SR], rejilla=True, cache=self.cache)
        self.assertFalse(os.path.exists(viejo))

    def test_main_hasta_y_rejilla(self):
        """voices.py --hasta: solo lo ya grabado entero, y --rejilla/--cache llegan a diarize."""
        wav = os.path.join(self.tmp, "g.wav")
        with open(wav, "wb") as fh:
            fh.write(wav_bytes(data=self.audio.tobytes(), rate=voices.SR, channels=1))
        out = os.path.join(self.tmp, "p.json")
        visto = {}

        def diar(samples, data, threads, progress, participantes=None, rejilla=False, cache=None):
            visto.update(n=len(samples), rejilla=rejilla, cache=cache)
            return {"tramos": [{"inicio": 0, "fin": 1, "h": "h1"}], "hablantes": {"h1": {"segundos": 1.0}},
                    "huellas": {}, "duracion_s": len(samples) / voices.SR}
        with mock.patch.object(voices, "diarize", diar), mock.patch.object(voices, "models_ready", return_value=True), \
                mock.patch("sys.stdout"):
            rc = voices.main(["--diarize", wav, "--out", out, "--data", self.data_dir, "--rejilla", "--cache",
                              self.cache, "--hasta", "20"])
        self.assertEqual(rc, 0)
        self.assertEqual(visto, {"n": 20 * voices.SR, "rejilla": True, "cache": self.cache})


class VocesDirectoTests(Base):
    def setUp(self):
        super().setUp()
        jobs = self.a.jobs
        jobs.voice_vectors, jobs.library = {}, self.a.library
        jobs.closing = False
        jobs.voices_on = lambda: True
        jobs._voices = mock.Mock(user=lambda: {"id": "p-yo"}, names=lambda: {"p-yo": "Yo", "p-ana": "Ana"},
                                 personas=lambda: [])
        self.vd = voces_directo.VocesDirecto(jobs)
        for p in (mock.patch.object(voices, "available", return_value=(True, "")),
                  mock.patch.object(voices, "models_ready", return_value=True)):
            p.start()
            self.addCleanup(p.stop)
        self.rel = "Alfa/2026-11-16 10-00-00 - Comité.wav"
        self.wav(self.rel, 2.0, open_header=True)
        self.job = FakeJob(self.rel, status="running", live=True, preset="nube")
        self.job.recording, self.job.recorded = True, 0.0
        jobs.list.append(self.job)
        self.llamadas = []

    def subproceso(self, job, args, idle_timeout=1800, cancel=None, reposo=False, hilos=None):
        self.llamadas.append({"args": args, "reposo": reposo, "hilos": hilos, "cancel": cancel})
        out = args[args.index("--out") + 1]
        hasta = float(args[args.index("--hasta") + 1])
        voices.write_sidecar(out, {"version": 1, "tramos": [{"inicio": 0.0, "fin": hasta, "h": "h1"}],
                                   "hablantes": {"h1": {"segundos": hasta, "breve": False}}, "creado": "x"})
        return {"huellas": {"h1": [0.1] * voices.DIM}}

    def test_solo_con_el_directo_en_la_nube_y_al_completar_un_bloque(self):
        self.job.recorded = voices.BLOQUE_S + 5  # bloque completo, sin el margen aún
        self.assertEqual(self.vd.candidatos(), [])
        self.job.recorded = voices.BLOQUE_S + voces_directo.MARGEN_S
        self.assertEqual(self.vd.candidatos(), [(self.job, voices.BLOQUE_S)])
        self.job.preset = "medium"  # el directo en este PC: nada
        self.assertEqual(self.vd.candidatos(), [])
        self.job.preset = "nube"
        self.a.jobs.voices_on = lambda: False  # «Voces» apagado: nada
        self.assertEqual(self.vd.candidatos(), [])
        self.a.jobs.voices_on = lambda: True
        self.job.recording = False  # ya no se graba: lo hace la identificación de siempre
        self.assertEqual(self.vd.candidatos(), [])

    def test_identifica_el_bloque_y_lo_deja_parcial(self):
        side = voices.sidecar_path(os.path.join(self.root, self.rel))
        voices.write_micro(side, {"paso": 0.25, "activo": "0101"})  # (la linea del micro, si ya estuviera)
        self.job.recorded = voices.BLOQUE_S + 30
        with mock.patch.object(self.a.jobs, "_voices_subprocess", self.subproceso, create=True):
            self.vd.paso()
            self.vd.paso()  # el mismo bloque no se repite
        self.assertEqual(len(self.llamadas), 1)
        c = self.llamadas[0]
        self.assertEqual((c["reposo"], c["hilos"]), (True, 1))
        for x in ("--rejilla", "--cache", "--hasta"):
            self.assertIn(x, c["args"])
        self.assertEqual(c["args"][c["args"].index("--hasta") + 1], str(voices.BLOQUE_S))
        d = voices.read_sidecar(side)
        self.assertEqual(d["parcial"], {"hasta": voices.BLOQUE_S})
        self.assertEqual(d["rejilla"], voices.BLOQUE_S)
        self.assertEqual(d["micro"], {"paso": 0.25, "activo": "0101"})
        self.assertIn(self.rel, self.a.jobs.voice_vectors)
        # lo que pide la interfaz
        r = self.a.speakers_get({"path": self.rel})
        self.assertEqual((r["exists"], r["parcial"]), (True, voices.BLOQUE_S))
        self.a.library.at = 0
        self.a.library.refresh()
        self.assertTrue(self.a.library.find(self.rel)["speakers"]["parcial"])
        # y el siguiente bloque
        self.job.recorded = 2 * voices.BLOQUE_S + 30
        with mock.patch.object(self.a.jobs, "_voices_subprocess", self.subproceso, create=True):
            self.vd.paso()
        self.assertEqual(voices.read_sidecar(side)["parcial"], {"hasta": 2 * voices.BLOQUE_S})

    def test_se_corta_si_la_grabacion_termina_o_pasa_a_este_pc(self):
        self.job.recorded = voices.BLOQUE_S + 30
        with mock.patch.object(self.a.jobs, "_voices_subprocess", self.subproceso, create=True):
            self.vd.paso()
        cancel = self.llamadas[0]["cancel"]
        self.assertFalse(cancel())
        self.job.preset = "medium"
        self.assertTrue(cancel())
        self.job.preset, self.job.recording = "nube", False
        self.assertTrue(cancel())
        self.job.recording = True
        self.vd.final(self.rel)
        self.assertTrue(cancel())

    def test_nada_parcial_despues_de_la_identificacion_final(self):
        side = voices.sidecar_path(os.path.join(self.root, self.rel))
        self.job.recorded = voices.BLOQUE_S + 30
        lento = threading.Event()

        def sub(job, args, **kw):
            r = self.subproceso(job, args, **kw)
            lento.wait(5)  # mientras corre el bloque, empieza la identificacion de siempre
            return r
        with mock.patch.object(self.a.jobs, "_voices_subprocess", sub, create=True):
            t = threading.Thread(target=self.vd.paso)
            t.start()
            time.sleep(0.2)
            self.vd.final(self.rel)
            lento.set()
            t.join(10)
        self.assertIsNone(voices.read_sidecar(side))

    def test_un_fallo_no_para_nada_y_no_se_reintenta(self):
        self.job.recorded = voices.BLOQUE_S + 30

        def falla(*a, **k):
            raise engines_error("el subproceso de voces se cerró de golpe")
        with mock.patch.object(self.a.jobs, "_voices_subprocess", falla, create=True):
            self.vd.paso()
            self.vd.paso()
        self.assertEqual(self.vd.hechos[self.rel], voices.BLOQUE_S)

    def test_al_terminar_usa_los_mismos_bloques(self):
        """_run_voices: con un sidecar parcial («rejilla»), voices.py va con --rejilla y la cache; sin el, como
        siempre. Y la pista de participantes no se pierde por haber tramos parciales."""
        jobs = server.Jobs.__new__(server.Jobs)
        jobs.list, jobs.lock, jobs.settings, jobs.library = [], threading.Lock(), self.a.settings, self.a.library
        jobs.voice_vectors, jobs._voices = {}, self.a.jobs._voices
        jobs.voces_directo = self.vd
        jobs.schedule_outputs = lambda rel, delay=4.0: None
        jobs.auto_summary = lambda rel: None
        abs_path = os.path.join(self.root, self.rel)
        side = voices.sidecar_path(abs_path)
        visto = []

        def sub(job, args, **kw):
            visto.append(list(args))
            voices.write_sidecar(side, {"version": 1, "tramos": [{"inicio": 0.0, "fin": 5.0, "h": "h1"}],
                                        "hablantes": {"h1": {"segundos": 5.0, "breve": False}}, "creado": "y"})
            return {"huellas": {}}
        jobs._voices_subprocess = sub
        with mock.patch.object(voices, "models_ready", return_value=True):
            j = FakeJob(self.rel, kind="voces")
            j.then_summary, j.started, j.name, j.error = False, 0, "Comité", None
            jobs._run_voices(j)
            self.assertNotIn("--rejilla", visto[0])
            voices.write_sidecar(side, {"version": 1, "tramos": [{"inicio": 0.0, "fin": 5.0, "h": "h1"}],
                                        "hablantes": {"h1": {"segundos": 5.0}}, "parcial": {"hasta": 1800},
                                        "rejilla": 1800, "creado": "x"})
            jobs._run_voices(j)
        self.assertIn("--rejilla", visto[1])
        self.assertIn(voces_directo.cache_dir(), visto[1])
        self.assertIn(self.rel, self.vd.finales)
        d = voices.read_sidecar(side)
        self.assertNotIn("parcial", d)
        self.assertEqual(d["rejilla"], voices.BLOQUE_S)
        self.write(os.path.relpath(os.path.splitext(abs_path)[0] + ".md", self.root),
                   server.dump_notes({"participantes": "Ana, Luis, Marta"}, ""))
        self.assertEqual(jobs._participant_hint(abs_path, {"tramos": [1], "parcial": {"hasta": 1800}}), 3)

    def test_write_micro_y_el_parcial_no_se_pisan(self):
        """Los dos escriben el sidecar con el mismo candado; lo del uno sigue tras escribir el otro."""
        side = voices.sidecar_path(os.path.join(self.root, self.rel))
        self.job.recorded = voices.BLOQUE_S + 30
        with mock.patch.object(self.a.jobs, "_voices_subprocess", self.subproceso, create=True):
            self.vd.paso()
        voices.write_micro(side, {"paso": 0.25, "activo": "1"})
        d = voices.read_sidecar(side)
        self.assertEqual((bool(d["tramos"]), d["micro"]["activo"]), (True, "1"))
        self.assertIs(type(voices.SIDECAR_LOCK), type(threading.RLock()))

    def test_jobs_arranca_el_hilo(self):
        src = open(os.path.join(os.path.dirname(server.__file__), "trabajos.py"), encoding="utf-8").read()
        self.assertIn("target=self.voces_directo.bucle", src)


def engines_error(msg):
    return server.engines.EngineError(msg)


@unittest.skipUnless(HAVE_SHERPA and HAVE_SAY and voices.models_ready(VOCES_DATA),
                     "necesita sherpa-onnx, say y NOTAS_VOCES_DATA")
class BloquesDeVerdadTests(unittest.TestCase):
    """Con sherpa-onnx de verdad (bloques de 20 s): lo diarizado por bloques mientras «se graba» y lo de la grabación
    entera dan lo mismo, y al terminar solo se diariza el último bloque."""

    def test_igual_que_entera(self):
        from test_voices_integration import TEXT, meeting
        tmp = tempfile.mkdtemp(prefix="notas-bloques-")
        self.addCleanup(shutil.rmtree, tmp, True)
        x, _ = meeting(tmp, {"A": "Mónica", "B": "Jorge"}, TEXT)
        cache = os.path.join(tmp, "cache")
        llamadas, real = [], voices._bloque

        def espia(*a, **k):
            llamadas.append(a[6])
            return real(*a, **k)
        with mock.patch.object(voices, "BLOQUE_S", 20.0), mock.patch.object(voices, "_bloque", espia):
            for k in range(1, int(len(x) / voices.SR // 20) + 1):
                voices.diarize(x[:k * 20 * voices.SR], VOCES_DATA, rejilla=True, cache=cache)
            n_dir = len(llamadas)
            final = voices.diarize(x, VOCES_DATA, rejilla=True, cache=cache)
            self.assertEqual(len(llamadas) - n_dir, 1)  # solo el último
            entera = voices.diarize(x, VOCES_DATA, rejilla=True)
        self.assertEqual(final["tramos"], entera["tramos"])
        self.assertEqual({h: i["segundos"] for h, i in final["hablantes"].items()},
                         {h: i["segundos"] for h, i in entera["hablantes"].items()})


if __name__ == "__main__":
    unittest.main()
