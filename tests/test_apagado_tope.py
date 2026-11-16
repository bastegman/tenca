# -*- coding: utf-8 -*-
"""Apagar termina el proceso en pocos segundos pase lo que pase (lookfeel-e2e «e.apagar_proceso_muere_5s», que fallaba
de vez en cuando: el proceso seguia vivo a los 5 s de escribir «apagado»).

- shutdown(): cerrar la ventana y parar el servidor HTTP tienen un tope cada uno (apagado.SALIDA_TOPE_S): si se quedan
  colgados, el proceso termina igual.
- main(): cuando la ventana se cierra (o el servidor para) porque se apaga, el hilo principal no sale por su cuenta
  (la finalizacion de Python podia tardar y dejar a medias el hilo que llama a os._exit): espera y, si tarda, termina
  el proceso el.
"""
import logging
import threading
import time
import unittest
from unittest import mock

from support import codigo_del_servidor, server

import apagado  # noqa: E402


class Colgado:
    def __init__(self):
        self.llamado = threading.Event()

    def __call__(self):
        self.llamado.set()
        time.sleep(30)


class TopeTests(unittest.TestCase):
    def apagar(self, win=None, srv=None):
        salida = threading.Event()
        exit_fn = mock.Mock(side_effect=lambda code: salida.set())
        t0 = time.monotonic()
        with mock.patch.object(server.Handler, "app", mock.Mock()), mock.patch.object(server, "SERVER", srv), \
                mock.patch.object(server, "WINDOW", win), mock.patch.object(logging, "shutdown"):
            hilo = threading.Thread(target=server.shutdown, args=("prueba",), kwargs={"exit_fn": exit_fn}, daemon=True)
            hilo.start()
            salida.wait(6)
        return salida.is_set(), time.monotonic() - t0, exit_fn

    def test_una_ventana_que_no_se_cierra_no_impide_salir(self):
        win = mock.Mock()
        win.destroy = Colgado()
        ok, dura, exit_fn = self.apagar(win=win, srv=mock.Mock())
        self.assertTrue(ok, "no ha llegado a os._exit")
        self.assertTrue(win.destroy.llamado.is_set())
        self.assertLess(dura, apagado.SALIDA_TOPE_S + 1.5)
        exit_fn.assert_called_once_with(0)

    def test_un_servidor_que_no_para_no_impide_salir(self):
        srv = mock.Mock()
        srv.shutdown = Colgado()
        ok, dura, _ = self.apagar(win=mock.Mock(), srv=srv)
        self.assertTrue(ok)
        self.assertLess(dura, apagado.SALIDA_TOPE_S + 1.5)

    def test_los_dos_colgados_como_mucho_dos_topes(self):
        win, srv = mock.Mock(), mock.Mock()
        win.destroy, srv.shutdown = Colgado(), Colgado()
        ok, dura, _ = self.apagar(win=win, srv=srv)
        self.assertTrue(ok)
        self.assertLess(dura, 2 * apagado.SALIDA_TOPE_S + 1.5)
        self.assertLessEqual(2 * apagado.SALIDA_TOPE_S + 0.5, 4.5)  # con el medio segundo de schedule_shutdown, < 5 s

    def test_sin_colgarse_sale_enseguida(self):
        ok, dura, _ = self.apagar(win=mock.Mock(), srv=mock.Mock())
        self.assertTrue(ok)
        self.assertLess(dura, 1.0)

    def test_el_error_de_un_paso_va_al_log_y_se_sigue(self):
        win = mock.Mock()
        win.destroy.side_effect = RuntimeError("x")
        with mock.patch.object(apagado, "log_exc") as le:
            ok, _, _ = self.apagar(win=win, srv=mock.Mock())
        self.assertTrue(ok)
        le.assert_called()


class HiloPrincipalTests(unittest.TestCase):
    def test_esperar_salida_termina_si_tarda(self):
        exit_fn = mock.Mock()
        t0 = time.monotonic()
        apagado.esperar_salida(0.2, exit_fn=exit_fn)
        self.assertGreaterEqual(time.monotonic() - t0, 0.19)
        exit_fn.assert_called_once_with(0)

    def test_un_apagado_de_prueba_no_marca_el_proceso_y_arrancar_lo_limpia(self):
        with mock.patch.object(server.Handler, "app", None), mock.patch.object(server, "SERVER", None), \
                mock.patch.object(server, "WINDOW", None), mock.patch.object(logging, "shutdown"):
            server.shutdown("prueba", exit_fn=mock.Mock())
        self.assertFalse(apagado.apagando())  # con otro exit_fn (los tests) no es un apagado del proceso
        apagado._APAGANDO.set()
        apagado.arranca()
        self.assertFalse(apagado.apagando())

    def test_main_no_deja_salir_al_hilo_principal_mientras_se_apaga(self):
        src = codigo_del_servidor()
        main = src[src.index("def main():"):]
        self.assertIn("arranca()", main[:main.index("PORT = int(")])
        ventana = main[main.index("if WINDOW.quitting:"):main.index('log("la ventana se ha cerrado sin apagar')]
        self.assertIn("esperar_salida()", ventana)
        self.assertIn("esperar_salida()", main[main.index("srv.serve_forever()\n"):])


if __name__ == "__main__":
    unittest.main()
