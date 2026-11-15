# -*- coding: utf-8 -*-
"""server.py partido en modulos (15/11/2026, punto 23 del review): server.py queda como fachada. Lo que main() fija al
arrancar (PORT, URL, DATA_DIR, SERVER, WINDOW) lo leen tambien los modulos que salieron de el, cada uno con su copia:
tienen que valer siempre lo mismo."""
import ast
import os
import unittest
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


if __name__ == "__main__":
    unittest.main()
