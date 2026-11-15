# -*- coding: utf-8 -*-
"""server.py partido en modulos (15/11/2026, punto 23 del review): server.py queda como fachada. Lo que main() fija al
arrancar (PORT, URL, DATA_DIR, SERVER, WINDOW) lo leen tambien los modulos que salieron de el, cada uno con su copia:
tienen que valer siempre lo mismo."""
import ast
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from support import APP, server
import api  # noqa: E402
import engines  # noqa: E402
import rutas  # noqa: E402


class CompartidasTests(unittest.TestCase):
    def test_asignar_en_server_llega_a_los_modulos(self):
        """`server.PORT = …` (los tests, mock.patch.object) llega a api.Handler, y al restaurar vuelve tambien."""
        antes = server.PORT
        with mock.patch.object(server, "PORT", antes + 1):
            self.assertEqual(api.PORT, antes + 1)
        self.assertEqual((server.PORT, api.PORT), (antes, antes))
        ventana = object()
        with mock.patch.object(server, "WINDOW", ventana):
            self.assertIs(api.WINDOW, ventana)
        self.assertIs(api.WINDOW, server.WINDOW)

    def test_un_parche_en_server_llega_a_lo_que_salio(self):
        """mock.patch.object(server, "safe", …) llega a rutas.alias_of como cuando alias_of estaba en server.py; un
        nombre de common (log) llega a los modulos de server.PARTES, no a engines ni al resto de _app."""
        with mock.patch.object(server, "safe", side_effect=server.ApiError("x", 403)) as safe:
            with server.ALIASES_LOCK:
                server.ALIASES["vieja.wav"] = "nueva.wav"
            try:
                self.assertEqual(rutas.alias_of("vieja.wav"), "nueva.wav")
            finally:
                with server.ALIASES_LOCK:
                    server.ALIASES.pop("vieja.wav", None)
        safe.assert_called_once_with("vieja.wav")
        self.assertIs(rutas.safe, server.safe)
        log_engines = vars(engines).get("log")
        with mock.patch.object(server, "log") as log:
            self.assertIs(api.log, log)
            self.assertIs(vars(engines).get("log"), log_engines)
        self.assertIs(api.log, server.log)

    def test_compartir_copia_lo_que_fija_main(self):
        """main() asigna con `global` (sin pasar por la fachada) y despues llama a _compartir."""
        antes = server.WINDOW
        ventana = object()
        vars(server)["WINDOW"] = ventana  # como `WINDOW = …` dentro de main()
        try:
            self.assertIsNot(api.WINDOW, ventana)
            server._compartir("WINDOW")
            self.assertIs(api.WINDOW, ventana)
        finally:
            server.WINDOW = antes
        self.assertIs(api.WINDOW, antes)

    def test_mismo_nombre_mismo_objeto(self):
        """Un nombre que esta en server y en un modulo de PARTES es el mismo objeto en los dos: la fachada copia por
        identidad (un modulo con una variable propia del mismo nombre se quedaria sin los parches de los tests)."""
        self.assertEqual(sorted(m.__name__ for m in server._partes()), sorted(server.PARTES))
        distintos = [f"{m.__name__}.{k}" for m in server._partes() for k, v in vars(m).items()
                     if not k.startswith("__") and k in vars(server) and vars(server)[k] is not v]
        self.assertEqual(distintos, [])

    def test_la_fachada_solo_al_importarla(self):
        """Ejecutado como la app (python server.py, __name__ == "__main__") server es un modulo normal: el __class__
        solo se cambia al importarlo (los tests)."""
        self.assertIsInstance(server, server._Fachada)
        with open(os.path.join(APP, "server.py"), encoding="utf-8") as f:
            arbol = ast.parse(f.read())
        cambios = [n for n in ast.walk(arbol) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Attribute) and t.attr == "__class__" for t in n.targets)]
        self.assertEqual(len(cambios), 1)
        guarda = next(n for n in arbol.body if isinstance(n, ast.If) and cambios[0] in n.body)
        self.assertEqual(ast.unparse(guarda.test), "__name__ != '__main__'")

    def test_cada_asignacion_global_se_comparte(self):
        """En server.py, cada asignacion a una de COMPARTIDAS dentro de una funcion (main(), App.__init__), tambien en
        tupla o con +=, va seguida en su bloque de un _compartir con ese nombre, y entre las dos solo puede haber otras
        asignaciones de COMPARTIDAS (revision 1: mientras dure lo de en medio, las partes verian el valor viejo)."""
        with open(os.path.join(APP, "server.py"), encoding="utf-8") as f:
            arbol = ast.parse(f.read())
        compartidas = set(server.COMPARTIDAS)

        def asignados(st):
            dianas = st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, ast.AugAssign) else []
            return {x.id for t in dianas for x in ast.walk(t) if isinstance(x, ast.Name)} & compartidas

        def compartir(st):
            if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call) and getattr(st.value.func, "id", "") \
                    == "_compartir":
                return {a.value for a in st.value.args if isinstance(a, ast.Constant)}
            return None

        vistas = []
        for fn in [n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef)]:
            globales = {x for n in ast.walk(fn) if isinstance(n, ast.Global) for x in n.names} & compartidas
            if not globales:
                continue
            for bloque in [n for n in ast.walk(fn) if isinstance(getattr(n, "body", None), list)]:
                for cuerpo in (bloque.body, getattr(bloque, "orelse", []) or []):
                    for i, st in enumerate(cuerpo):
                        nombres = asignados(st) & globales
                        if not nombres:
                            continue
                        vistas.append((fn.name, st.lineno))
                        hechos = set()
                        for sig in cuerpo[i + 1:]:
                            c = compartir(sig)
                            if c is not None:
                                hechos |= c
                                if nombres <= hechos:
                                    break
                            elif not asignados(sig):
                                break
                        self.assertTrue(nombres <= hechos, f"{fn.name}(), línea {st.lineno}: falta _compartir"
                                        f"{tuple(sorted(nombres - hechos))} antes de seguir")
        # main(): PORT, URL, DATA_DIR, SERVER y las tres de WINDOW; App.__init__: COMPLETE
        self.assertGreaterEqual(len([v for v in vistas if v[0] == "main"]), 7)
        self.assertIn("__init__", [v[0] for v in vistas])


class ArranqueComoLaAppTests(unittest.TestCase):
    """python server.py (__name__ == "__main__", sin la fachada): main() pasa --port a api.Handler con _compartir.
    Por una ruta con un enlace (en macOS, los temporales de /var son /private/var): el __file__ de server.py no lleva
    el enlace resuelto y el de los modulos importados si (el fallo de la revision del 15/11/2026: Host rechazado)."""

    def test_el_puerto_de_la_linea_de_ordenes_llega_al_handler(self):
        tmp = tempfile.mkdtemp(prefix="tenca-partido-")
        self.addCleanup(shutil.rmtree, tmp, True)
        app = os.path.join(tmp, "enlace")
        try:
            os.symlink(APP, app, target_is_directory=True)
        except (OSError, NotImplementedError):  # Windows sin permiso de enlaces: la ruta de verdad
            app = APP
        datos = os.path.join(tmp, "datos")
        os.makedirs(datos)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with open(os.path.join(tmp, "salida.log"), "w", encoding="utf-8") as log:
            proc = subprocess.Popen([sys.executable, os.path.join(app, "server.py"), "--port", str(port), "--data",
                                     datos, "--no-browser"], cwd=tmp, stdout=log, stderr=subprocess.STDOUT,
                                    env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.terminate)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        token, fin, vistos = None, time.time() + 60, []
        while time.time() < fin and proc.poll() is None:
            f = os.path.join(datos, "sesion.token")
            if os.path.isfile(f):
                with open(f, encoding="utf-8") as fh:
                    token = fh.read().strip()
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/ping", headers={"X-Chuleta-Token": token})
                try:
                    self.assertEqual(json.load(opener.open(req, timeout=2)).get("app"), "audios")
                    break
                except urllib.error.HTTPError as e:
                    vistos.append(e.code)
                    if e.code == 403:
                        break
                except OSError:
                    pass
            time.sleep(0.3)
        else:
            with open(os.path.join(tmp, "salida.log"), encoding="utf-8") as fh:
                self.fail("no arrancó: " + fh.read()[-1500:])
        self.assertNotIn(403, vistos, "el Handler no tiene el puerto de --port (api.PORT sin _compartir)")


class NadaNuevoEnServerTests(unittest.TestCase):
    """Regla desde que se partio (15/11/2026, punto 23 del review): nada nuevo en server.py; cada cosa en su modulo
    (y el modulo, en server.PARTES). server.py solo lleva la fachada, main() y la cabeza de App (__init__ y lo que usa
    App._movil_*); lo que sale de aqui se quita tambien de estas listas, que solo pueden encoger."""

    NIVEL_SUPERIOR = {"SERVER", "WINDOW", "PARTES", "COMPARTIDAS", "_partes", "_compartir", "_Fachada", "App", "arg",
                      "main"}
    # lo que server.py importaba antes de partirse y ya no usa: sigue siendo server.X (revision 1, H4)
    IMPORTADOS_ANTES = ("contextlib copy hashlib hmac itertools json queue re secrets struct unicodedata urllib uuid "
                        "Counter datetime timedelta BaseHTTPRequestHandler ThreadingHTTPServer parse_qs urlparse "
                        "aprender explicar grabar_plan probar salidas window_mod Bandeja local_opener read_token "
                        "disco_al_empezar teams_mic_estado").split()
    APP = {"rec_body", "rec_cadena", "cortes", "__init__", "rec_toggle", "_reparar_proveedor", "_migrar_nombre_raiz",
           "_movil_lock", "_movil_rec", "_movil_ultima", "_movil_red", "_movil_datos", "_movil_parar", "movil_red",
           "movil_abrir", "movil_datos", "movil_estado", "movil_cerrar"}

    @staticmethod
    def nombres(cuerpo):
        out = set()
        for n in cuerpo:
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                out.add(n.name)
            elif isinstance(n, (ast.Assign, ast.AnnAssign)):
                out |= {x.id for tg in (n.targets if isinstance(n, ast.Assign) else [n.target])
                        for x in ast.walk(tg) if isinstance(x, ast.Name)}
        return out

    def test_nada_nuevo_en_server_py(self):
        with open(os.path.join(APP, "server.py"), encoding="utf-8") as f:
            arbol = ast.parse(f.read())
        nuevos = self.nombres(arbol.body) - self.NIVEL_SUPERIOR
        self.assertEqual(nuevos, set(), "nada nuevo en server.py: va en su módulo (y el módulo, en server.PARTES)")
        app = next(n for n in arbol.body if isinstance(n, ast.ClassDef) and n.name == "App")
        nuevos = self.nombres(app.body) - self.APP
        self.assertEqual(nuevos, set(), "nada nuevo en App dentro de server.py: va en su clase base (app_*.py)")

    def test_claude_windows_sigue_mirando_lo_que_era_server_py(self):
        """El workflow de Claude en Windows se lanza con los cambios de server.py: tambien con los de sus partes."""
        with open(os.path.join(APP, "..", ".github", "workflows", "claude-windows.yml"), encoding="utf-8") as f:
            yml = f.read()
        faltan = [n for n in ("server",) + server.PARTES if f'- "_app/{n}.py"' not in yml]
        self.assertEqual(faltan, [])

    def test_lo_que_se_busca_con_globals_esta_en_su_modulo(self):
        """globals().get("x") en una parte busca x en ESE modulo: al salir de server.py tiene que haberse llevado x
        (el plan de grabacion de app_grabar busca asi rec_folder_origin y meeting_dir_preview)."""
        faltan = []
        for n in ("server",) + server.PARTES:
            with open(os.path.join(APP, n + ".py"), encoding="utf-8") as f:
                arbol = ast.parse(f.read())
            for x in ast.walk(arbol):
                if (isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute) and x.func.attr == "get"
                        and isinstance(x.func.value, ast.Call) and getattr(x.func.value.func, "id", "") == "globals"
                        and x.args and isinstance(x.args[0], ast.Constant)):
                    if x.args[0].value not in vars(sys.modules[n]):
                        faltan.append(f"{n}: {x.args[0].value}")
        self.assertEqual(faltan, [])

    def test_server_conserva_lo_que_importaba(self):
        self.assertEqual([k for k in self.IMPORTADOS_ANTES if not hasattr(server, k)], [])

    def test_ningun_global_en_las_partes(self):
        """Solo server.py reasigna variables de modulo con `global` (y las comparte): una parte que lo hiciera tendria
        un valor que server y las demas no ven."""
        con = []
        for n in server.PARTES:
            with open(os.path.join(APP, n + ".py"), encoding="utf-8") as f:
                con += [f"{n}:{x.lineno}" for x in ast.walk(ast.parse(f.read())) if isinstance(x, ast.Global)]
        self.assertEqual(con, [])

    def test_las_partes_importan_de_donde_server(self):
        """Un nombre que server.py importa de fuera de PARTES (common, recording…) las partes lo importan del mismo
        sitio (revision 1, H1: WINDOWS de aviso en vez de common; el mismo bool, pero cargaba aviso.py)."""
        def origenes(nombre):
            with open(os.path.join(APP, nombre + ".py"), encoding="utf-8") as f:
                arbol = ast.parse(f.read())
            return {(a.asname or a.name): n.module for n in arbol.body if isinstance(n, ast.ImportFrom)
                    and n.module not in server.PARTES for a in n.names}
        de_server = origenes("server")
        distintos = [f"{p}: {k} de {m} (server: {de_server[k]})" for p in server.PARTES
                     for k, m in origenes(p).items() if k in de_server and de_server[k] != m]
        self.assertEqual(distintos, [])

    def test_ningun_modulo_importa_server(self):
        """Ejecutado como la app, server es __main__: `import server` cargaria una segunda copia de todo."""
        for n in server.PARTES:
            with open(os.path.join(APP, n + ".py"), encoding="utf-8") as f:
                arbol = ast.parse(f.read())
            for x in ast.walk(arbol):
                if isinstance(x, ast.Import):
                    self.assertNotIn("server", [a.name for a in x.names], n)
                elif isinstance(x, ast.ImportFrom):
                    self.assertNotEqual(x.module, "server", n)


if __name__ == "__main__":
    unittest.main()
