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

    def test_main_comparte_cada_asignacion(self):
        """Cada asignacion de main() a una de COMPARTIDAS va seguida, en su bloque, de un _compartir con ese nombre."""
        with open(os.path.join(APP, "server.py"), encoding="utf-8") as f:
            arbol = ast.parse(f.read())
        main = next(n for n in arbol.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        vistas = 0
        for bloque in [n for n in ast.walk(main) if hasattr(n, "body") and isinstance(n.body, list)]:
            for cuerpo in (bloque.body, getattr(bloque, "orelse", []) or []):
                for i, st in enumerate(cuerpo):
                    nombres = {t.id for t in getattr(st, "targets", []) if isinstance(t, ast.Name)}
                    nombres &= set(server.COMPARTIDAS)
                    if not nombres:
                        continue
                    vistas += 1
                    despues = [a.value for c in cuerpo[i + 1:] for a in ast.walk(c) if isinstance(a, ast.Constant)
                               and isinstance(c, ast.Expr) and isinstance(c.value, ast.Call)
                               and getattr(c.value.func, "id", "") == "_compartir"]
                    self.assertTrue(nombres <= set(despues), f"main(), línea {st.lineno}: falta _compartir{nombres}")
        self.assertGreaterEqual(vistas, 6)  # PORT, URL, DATA_DIR, SERVER y las tres de WINDOW


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


if __name__ == "__main__":
    unittest.main()
