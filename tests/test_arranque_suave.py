# -*- coding: utf-8 -*-
"""El trabajo pesado de fondo no puede tumbar un portatil modesto (incidencia del 16/11/2026: al instalar encima de la
version anterior, «Windows PowerShell is not responding» y el portatil «no responde»).

- carga.py: lo ocupado que va el PC (CPU de todo el PC y memoria libre; solo se mide en Windows).
- Rehacer las huellas de voz: cada huella en un proceso en reposo y con 1 hilo, una a una y con pausa; al arrancar,
  tras un rato y con el PC tranquilo un tiempo seguido; si el PC va justo, espera (con la CPU, como mucho 20 min; con
  poca memoria, lo que haga falta) y, si se acaba la memoria mientras calcula, se corta.
- La precarga del modelo de Whisper no se hace con poca memoria libre.
- La precarga de las transcripciones no abre las «solo en linea» de OneDrive.
- El diagnostico dice la memoria del PC; las lineas nuevas del log tienen plantilla.
- instalar.ps1: lo que tarda va en segundo plano (Invoke-Fondo) y la ventana se sigue atendiendo.
"""
import os
import re
import sys
import threading
import time
import types
import unittest
from unittest import mock

from support import APP, TempTree, bare_jobs, server
from test_enganches import RehacerBase

import app_voces  # noqa: E402
import biblioteca  # noqa: E402
import carga  # noqa: E402
import diagnostico  # noqa: E402
import trabajos  # noqa: E402
import voices  # noqa: E402


# ----------------------------------------------------------------------------- carga.py

class CargaTests(unittest.TestCase):
    def test_cpu_de_todo_el_pc_con_dos_lecturas(self):
        lecturas = iter([(1000, 10000), (1600, 14000)])  # 600 de 4000 inactivo: 85 % ocupada
        dormido = []
        with mock.patch.object(carga, "_tiempos", lambda: next(lecturas)):
            self.assertAlmostEqual(carga.cpu(2.0, dormir=dormido.append), 0.85)
        self.assertEqual(dormido, [2.0])

    def test_sin_lectura_no_se_sabe(self):
        with mock.patch.object(carga, "_tiempos", return_value=None):
            self.assertIsNone(carga.cpu(0))
        lecturas = iter([(10, 100), (10, 100)])  # sin tiempo entre las dos
        with mock.patch.object(carga, "_tiempos", lambda: next(lecturas)):
            self.assertIsNone(carga.cpu(0, dormir=lambda s: None))

    def test_ocupado(self):
        gb = carga.GB
        with mock.patch.object(carga, "memoria", return_value=(8 * gb, int(0.5 * gb))), \
                mock.patch.object(carga, "cpu", side_effect=AssertionError("con poca memoria no se mide la CPU")):
            self.assertEqual(carga.ocupado(), "memoria")
            self.assertTrue(carga.poca_memoria())
        with mock.patch.object(carga, "memoria", return_value=(8 * gb, 3 * gb)), \
                mock.patch.object(carga, "cpu", return_value=0.9):
            self.assertEqual(carga.ocupado(), "cpu")
            self.assertFalse(carga.poca_memoria())
        with mock.patch.object(carga, "memoria", return_value=(8 * gb, 3 * gb)), \
                mock.patch.object(carga, "cpu", return_value=0.3):
            self.assertIsNone(carga.ocupado())
        with mock.patch.object(carga, "memoria", return_value=None), mock.patch.object(carga, "cpu", return_value=None):
            self.assertIsNone(carga.ocupado())  # donde no se sabe (fuera de Windows), nunca se espera

    @unittest.skipIf(sys.platform == "win32", "fuera de Windows")
    def test_fuera_de_windows_no_mide(self):
        self.assertIsNone(carga.memoria())
        self.assertIsNone(carga.cpu(0))
        self.assertIsNone(carga.ocupado(muestra=0))

    @unittest.skipUnless(sys.platform == "win32", "solo Windows")
    def test_en_windows_mide_de_verdad(self):
        total, libre = carga.memoria()
        self.assertGreater(total, 1 * carga.GB)
        self.assertGreater(libre, 0)
        self.assertLessEqual(libre, total)
        c = carga.cpu(0.5)
        self.assertIsNotNone(c)
        self.assertGreaterEqual(c, 0.0)
        self.assertLessEqual(c, 1.0)

    def test_linea_del_diagnostico_solo_numeros(self):
        with mock.patch.object(carga, "memoria", return_value=(int(15.6 * carga.GB), int(4.25 * carga.GB))):
            linea = carga.diagnostico()
        self.assertRegex(linea, r"^PC: \d+ hilos de CPU · memoria 15,6 GB, libre 4,2 GB$")
        with mock.patch.object(carga, "memoria", return_value=None):
            self.assertRegex(carga.diagnostico(), r"^PC: \d+ hilos de CPU · memoria: sin datos$")


# ----------------------------------------------------------------------------- rehacer las huellas, a sorbos

class RehacerSuaveTests(RehacerBase):
    """Sobre la instalacion de antes del cambio de modelo de test_enganches (4 personas, 4 huellas de TitaNet)."""

    def setUp(self):
        super().setUp()
        self.llamadas = []

        def sv(rel, h, cancel=None, **kw):
            self.llamadas.append(dict(kw, rel=rel, h=h, cancel=cancel))
            return self.speaker_vector(rel, h)
        self.app.jobs.speaker_vector = sv

    def test_cada_huella_en_reposo_y_con_un_hilo(self):
        res = self.app.rehacer_huellas()
        self.assertEqual(res["rehechas"], 2)
        self.assertEqual(len(self.llamadas), 2)
        for c in self.llamadas:
            self.assertIs(c.get("reposo"), True)
            self.assertEqual(c.get("hilos"), 1)
        self.assertEqual(app_voces.REHACER_HILOS, 1)

    def test_si_se_acaba_la_memoria_mientras_calcula_se_corta(self):
        self.app.rehacer_huellas()
        corta = self.llamadas[0]["cancel"]
        self.assertFalse(corta())
        with mock.patch.object(carga, "poca_memoria", return_value=True):
            self.assertTrue(corta())

    def test_con_el_pc_ocupado_espera_y_lo_dice(self):
        estados = iter(["cpu", "memoria", None, None, None, None, None, None, None, None])
        with mock.patch.object(carga, "ocupado", lambda *a, **k: next(estados, None)), \
                mock.patch.object(app_voces, "log") as log:
            res = self.app.rehacer_huellas()
        self.assertEqual(res["rehechas"], 2)
        dicho = [c.args[0] for c in log.call_args_list]
        self.assertIn("   huellas por rehacer: el PC va justo (cpu); se espera", dicho)
        self.assertIn("   huellas por rehacer: el PC va justo (memoria); se espera", dicho)
        for linea in ("   huellas por rehacer: el PC va justo (cpu); se espera",
                      "   huellas por rehacer: el PC va justo (memoria); se espera"):  # en el diagnostico, entera
            self.assertEqual(diagnostico.diag_line(linea), linea)

    def test_con_poca_memoria_no_calcula(self):
        hecho = threading.Event()
        t = threading.Thread(target=lambda: (self.app.rehacer_huellas(), hecho.set()), daemon=True)
        with mock.patch.object(carga, "ocupado", lambda *a, **k: "memoria"):
            t.start()
            time.sleep(0.6)
            self.assertEqual(self.llamadas, [])  # sigue esperando
            self.app.jobs.closing = True  # apagar la app lo termina
            t.join(5)
        self.assertTrue(hecho.is_set())
        self.assertEqual(self.llamadas, [])

    def test_una_cpu_que_nunca_baja_no_lo_deja_para_siempre(self):
        with mock.patch.object(carga, "ocupado", lambda *a, **k: "cpu"), \
                mock.patch.object(app_voces, "REHACER_CPU_MAX_S", 0.2):
            res = self.app.rehacer_huellas()
        self.assertEqual(res["rehechas"], 2)

    def test_pausa_entre_huellas(self):
        momentos = []
        sv = self.app.jobs.speaker_vector

        def con_hora(rel, h, cancel=None, **kw):
            momentos.append(time.monotonic())
            return sv(rel, h, cancel=cancel, **kw)
        self.app.jobs.speaker_vector = con_hora
        with mock.patch.object(app_voces, "REHACER_PAUSA_S", 0.4):
            t0 = time.monotonic()
            self.app.rehacer_huellas()
        self.assertEqual(len(momentos), 2)
        self.assertGreaterEqual(momentos[0] - t0, 0.35)
        self.assertGreaterEqual(momentos[1] - momentos[0], 0.35)

    def test_apagar_durante_la_pausa_corta_enseguida(self):
        with mock.patch.object(app_voces, "REHACER_PAUSA_S", 30.0):
            t = threading.Thread(target=self.app.rehacer_huellas, daemon=True)
            t0 = time.monotonic()
            t.start()
            time.sleep(0.3)
            self.app.jobs.closing = True
            t.join(5)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(self.llamadas, [])


class RehacerAlArrancarSuaveTests(RehacerBase):
    def test_al_arrancar_espera_un_rato_y_calma(self):
        self.assertGreaterEqual(app_voces.REHACER_ESPERA_S, 120)
        self.assertGreaterEqual(app_voces.REHACER_CALMA_S, 60)
        pedidos = []
        self.app.rehacer_huellas = lambda *a: pedidos.append(a)
        app_voces.Voces.rehacer_huellas_al_arrancar(self.app)
        self.assertEqual(pedidos, [(app_voces.REHACER_ESPERA_S, app_voces.REHACER_CALMA_S)])

    def test_la_calma_tiene_que_ser_seguida(self):
        """Tenca y el PC, tranquilos `calma` segundos seguidos: un rato ocupado en medio vuelve a empezar la cuenta."""
        reloj = [0.0]
        ocupado = {5.0: "transcribiendo", 6.0: None}  # de 5 a 6 s, una transcripcion: la cuenta vuelve a cero

        def ahora():
            return reloj[0]

        def dormir(s):
            reloj[0] += 1.0

        estado = [None]

        def app_ocupada():
            for t in sorted(ocupado):
                if reloj[0] >= t:
                    estado[0] = ocupado[t]
            return estado[0]
        self.app._rehacer_ocupado = app_ocupada
        with mock.patch.object(app_voces, "_ahora", ahora), mock.patch.object(app_voces, "_dormir", dormir), \
                mock.patch.object(app_voces, "REHACER_PASO_S", 1.0), mock.patch.object(app_voces, "log"):
            self.app._rehacer_calma(10.0)
        self.assertEqual(reloj[0], 16.0)  # tranquilo desde el 6: hasta el 16

    def test_sin_huellas_por_rehacer_no_espera_la_calma(self):
        app = self.nueva_app()
        app._rehacer_calma = mock.Mock(side_effect=AssertionError("no hacia falta esperar"))
        app.jobs._voices = mock.Mock(pendientes=lambda: [])
        app.jobs.voices_on = lambda: True
        self.assertIsNone(app.rehacer_huellas(0, 60))


class SubprocesoEnReposoTests(TempTree):
    def test_speaker_vector_de_fondo_en_reposo_y_con_un_hilo(self):
        jobs = bare_jobs([])
        jobs.voice_vectors = {}
        self.wav("r.wav")
        vistos = []

        class P:
            pid = 4242

            def __init__(self, argv, **kw):
                vistos.append((argv, kw))
                self.stdout = iter([b'{"type": "result", "huellas": {"h1": [0.1]}, "segundos": 3.0}\n'])

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        with mock.patch.object(trabajos.subprocess, "Popen", P), mock.patch.object(trabajos, "borrar_pcm_voces"):
            jobs.speaker_vector("r.wav", "h1", reposo=True, hilos=1)
            jobs.voice_vectors = {}
            jobs.speaker_vector("r.wav", "h1")  # «Recordar», con el usuario esperando: como antes
        (fondo, kw_fondo), (recordar, kw_recordar) = vistos
        self.assertEqual(fondo[fondo.index("--threads") + 1], "1")
        self.assertEqual(recordar[recordar.index("--threads") + 1], str(voices.hilos()))
        if os.name == "nt":
            self.assertTrue(kw_fondo["creationflags"] & voices.IDLE_PRIORITY)
            self.assertFalse(kw_recordar["creationflags"] & voices.IDLE_PRIORITY)
        elif trabajos.shutil.which("nice"):
            self.assertEqual(fondo[:3], ["nice", "-n", "19"])
            self.assertNotIn("nice", recordar)


# ----------------------------------------------------------------------------- precargas del arranque

class PrecargaTests(TempTree):
    def jobs(self):
        j = bare_jobs([])
        j.settings = types.SimpleNamespace(get=lambda: {"live_preset": "medium", "preset": "rapido"})
        j.live_worker = mock.Mock()
        j.local = types.SimpleNamespace(worker=mock.Mock())
        return j

    def test_con_poca_memoria_libre_no_precarga_el_modelo(self):
        j = self.jobs()
        with mock.patch.object(trabajos, "preload_model", return_value=("live", "medium")), \
                mock.patch.object(carga, "memoria", return_value=(8 * carga.GB, int(1.5 * carga.GB))), \
                mock.patch.object(trabajos, "log") as log:
            self.assertIsNone(j.preload())
        j.live_worker.ensure.assert_not_called()
        linea = log.call_args_list[-1].args[0]
        self.assertEqual(linea, "no se precarga medium: el PC tiene 1.5 GB de memoria libre (se carga al empezar la "
                                "reunión)")
        self.assertEqual(diagnostico.diag_line(linea), linea)

    def test_con_memoria_o_sin_saberlo_precarga_como_antes(self):
        for mem in ((16 * carga.GB, 6 * carga.GB), None):
            with self.subTest(mem=mem):
                j = self.jobs()
                with mock.patch.object(trabajos, "preload_model", return_value=("live", "medium")), \
                        mock.patch.object(carga, "memoria", return_value=mem), mock.patch.object(trabajos, "log"):
                    self.assertEqual(j.preload(), ("live", "medium"))
                j.live_worker.ensure.assert_called_once()

    def test_las_transcripciones_solo_en_linea_no_se_abren_al_arrancar(self):
        self.write("a.srt", "1\n00:00:00,000 --> 00:00:01,000\nhola\n")
        self.write("b.srt", "1\n00:00:00,000 --> 00:00:01,000\nadios\n")
        lib = types.SimpleNamespace(flat=[{"_abs": os.path.join(self.root, n + ".wav"), "txt": False, "srt": True}
                                          for n in ("a", "b")])
        t = biblioteca.Transcripts()
        leidos = []
        load = t.load
        t.load = lambda base: leidos.append(os.path.basename(base)) or load(base)
        with mock.patch.object(biblioteca, "_solo_en_linea", lambda p: p.endswith("b.srt")):
            t.warm(lib, pausa=0)
        self.assertEqual(leidos, ["a"])


# ----------------------------------------------------------------------------- diagnostico

class DiagnosticoPCTests(TempTree):
    def test_el_diagnostico_dice_la_memoria_del_pc(self):
        app = server.App.__new__(server.App)
        app.settings = server.Store(os.path.join(self.data_dir, "settings.json"), server.DEFAULT_SETTINGS)
        with mock.patch.object(carga, "memoria", return_value=(8 * carga.GB, 2 * carga.GB)):
            texto = app.diagnostico({})["texto"]
        self.assertIn("PC: ", texto)
        self.assertIn("memoria 8,0 GB, libre 2,0 GB", texto)


# ----------------------------------------------------------------------------- instalar.ps1

def _sin_heredocs(ps1):
    """El script sin el contenido de sus here-strings @'...'@ (lo que corre en segundo plano con Invoke-Fondo) ni el
    del C# de Add-Type (@"..."@)."""
    return re.sub(r"@'\n.*?\n'@", "@''@", re.sub(r'@"\n.*?\n"@', '@""@', ps1, flags=re.S), flags=re.S)


def _funcion(ps1, nombre):
    m = re.search(r"^function " + re.escape(nombre) + r"\b.*?^\}$", ps1, re.S | re.M)
    return m.group(0) if m else ""


class InstaladorNoSeCongelaTests(unittest.TestCase):
    """Con ventana, el hilo de la ventana no puede quedarse segundos esperando: lo que tarda va en Invoke-Fondo (un
    runspace aparte) y la ventana se atiende mientras. En Windows lo mide de verdad lookfeel-e2e
    («*.ventana_responde»)."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(APP, "instalar.ps1"), encoding="utf-8-sig") as f:
            cls.ps1 = f.read().replace("\r\n", "\n")

    def test_lo_que_tarda_no_va_en_el_hilo_de_la_ventana(self):
        fuera = _sin_heredocs(self.ps1)
        # funciones que solo se ejecutan dentro de Invoke-Fondo (se les pasa su texto)
        for n in ("Find-Python", "Test-Grabadora", "Hide-Console", "Add-TipoLnk"):
            self.assertIn(f'@("{n}")', fuera, n)
            fuera = fuera.replace(_funcion(fuera, n), "")
        for patron in (r"&\s*\$py\b", r"&\s*winget\b", r"Get-NetTCPConnection", r"Get-CimInstance", r"\bnetstat\b",
                       r"Invoke-RestMethod", r"Start-Sleep -Seconds", r"Add-Type -(TypeDefinition|MemberDefinition)"):
            self.assertNotRegex(fuera, patron)
        # los Start-Sleep que quedan, de pocos milisegundos y atendiendo la ventana (Wait-Ui, Invoke-Fondo, Finish-Ui)
        for m in re.finditer(r"Start-Sleep -Milliseconds (\$?\w+)", fuera):
            self.assertIn(m.group(1), ("30", "40", "50", "$ms"), m.group(0))

    def test_invoke_fondo_atiende_la_ventana_mientras_espera(self):
        f = _funcion(self.ps1, "Invoke-Fondo")
        self.assertIn("$ps.BeginInvoke()", f)
        self.assertRegex(f, r"while \(-not \$h\.IsCompleted\) \{ Read-Fondo \$q; Pump; Start-Sleep -Milliseconds 40 \}")
        self.assertIn("[System.Windows.Forms.Application]::DoEvents()", _funcion(self.ps1, "Pump"))
        self.assertIn("Pump", _funcion(self.ps1, "Wait-Ui"))

    def test_pip_y_las_comprobaciones_van_en_segundo_plano(self):
        pip = _funcion(self.ps1, "Pip-Install")
        self.assertIn("Invoke-Fondo $CodigoExe", pip)
        for mod in ("faster_whisper", "sherpa_onnx", "win32com.client", "webview"):
            self.assertRegex(self.ps1, r'Invoke-Fondo \$CodigoSalida @\{ exe = \$py; argv = @\("-c", "import '
                             + re.escape(mod))

    def test_el_log_dice_la_hora_de_cada_paso_y_lo_mas_largo_sin_atender(self):
        self.assertIn('Log-File ("> $text  [" + (Get-Date -Format "HH:mm:ss") + "]")', _funcion(self.ps1, "Step"))
        self.assertIn("ventana: lo más largo sin atenderla", _funcion(self.ps1, "Finish-Ui"))
        self.assertIn("$tm.Add_Tick(", _funcion(self.ps1, "New-InstallerWindow"))

    def test_el_e2e_mide_si_la_ventana_responde(self):
        with open(os.path.join(os.path.dirname(APP), "scripts", "e2e", "lookfeel_e2e.ps1"), encoding="utf-8") as f:
            e2e = f.read()
        run = _funcion(e2e.replace("\r\n", "\n"), "Run-Installer")
        self.assertIn("[E2E.Sonda]::Responde($h, 500)", run)
        self.assertIn('Check "$label.ventana_responde" ($peor -lt 2.5)', run)
        self.assertIn('Run-Installer $vExtr "v.chuleta_instalador" 20 $false', e2e)  # el de la Chuleta de antes, no


@unittest.skipUnless(sys.platform == "win32", "solo Windows (PowerShell 5.1)")
class InvokeFondoWindowsTests(unittest.TestCase):
    """Invoke-Fondo de verdad, sin ventana: devuelve lo del bloque, pasa las lineas por Say y la funcion enviada."""

    def ps(self, body):
        from test_desinstalar import run_ps_functions
        return run_ps_functions(os.path.join(APP, "instalar.ps1"),
                                ["Pump", "Wait-Ui", "Get-FnText", "Invoke-Fondo", "Read-Fondo", "Test-Grabadora"],
                                "$script:ui = $null; $script:fondo = $null; $script:dichas = @(); "
                                "function Log-File($t) { }; "
                                "function Say($t, $c = 'Gray') { $script:dichas += \"$c|$t\" }; "
                                + body)

    def test_devuelve_y_dice(self):
        out = self.ps(
            "$codigo = @'\nSay 'uno'; Say 'dos' Yellow; [pscustomobject]@{ v = $x * 2 }\n'@\n"
            "$r = Invoke-Fondo $codigo @{ x = 21 }; Write-Output ('R=' + $r.v); "
            "Write-Output ('D=' + ($script:dichas -join ';'))")
        self.assertIn("R=42", out)
        self.assertIn("D=Gray|uno;Yellow|dos", out)

    def test_un_programa_y_su_codigo_de_salida(self):
        py = sys.executable.replace("'", "''")
        out = self.ps(
            "$c = @'\n& $exe @argv 2>&1 | ForEach-Object { Say \"    $_\" }\n"
            "[pscustomobject]@{ rc = $LASTEXITCODE }\n'@\n"
            f"$r = Invoke-Fondo $c @{{ exe = '{py}'; argv = @('-c', 'import sys; print(''hola''); sys.exit(3)') }}; "
            "Write-Output ('RC=' + $r.rc); Write-Output ('D=' + ($script:dichas -join ';'))")
        self.assertIn("RC=3", out)
        self.assertIn("D=Gray|    hola", out)

    def test_una_funcion_enviada_y_un_fallo(self):
        out = self.ps("$r = Invoke-Fondo '[pscustomobject]@{ v = [bool](Test-Grabadora) }' @{} @('Test-Grabadora'); "
                      "Write-Output ('G=' + $r.v); $f = Invoke-Fondo 'throw \"mal\"'; "
                      "Write-Output ('F=' + ($null -eq $f))")
        self.assertIn("G=False", out)
        self.assertIn("F=True", out)


if __name__ == "__main__":
    unittest.main()
