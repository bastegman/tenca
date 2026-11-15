# -*- coding: utf-8 -*-
"""El PC despierto mientras se graba o se transcribe (punto 30 del review del 14/11/2026).

Sin esto, Windows podia suspender el PC por inactividad con una grabacion presencial en marcha (sin teclado ni raton
durante la reunion) o con la cola de transcripcion local a medias: al volver, la reunion no estaba transcrita. Ahora,
mientras se graba o hay trabajos en cola o en marcha, la app le pide a Windows que no se suspenda solo
(SetThreadExecutionState con ES_CONTINUOUS | ES_SYSTEM_REQUIRED) y lo suelta al acabar (ES_CONTINUOUS). No enciende la
pantalla ni impide suspenderlo a mano (tapa, boton o menu): eso lo decide el usuario (documentacion de Microsoft).

La peticion es del hilo que la hace y se va con el: la pide y la suelta siempre el mismo, el vigilante de Grabar
(Grabar._monitor_watchdog, que vive lo que la app y mira cada 2 s). Fuera de Windows no hace nada."""
import ctypes
import time

from common import WINDOWS, log

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
MOTIVOS = ("grabando", "transcribiendo", "grabando y transcribiendo")


def _set_thread_execution_state():
    """kernel32.SetThreadExecutionState (o None fuera de Windows): devuelve el estado anterior, 0 si falla."""
    if not WINDOWS:
        return None
    f = ctypes.WinDLL("kernel32", use_last_error=True).SetThreadExecutionState
    f.argtypes, f.restype = [ctypes.c_uint32], ctypes.c_uint32
    return f


def motivo(grabando, trabajos):
    """La razon para pedirlo (una de MOTIVOS) o "" si no hay ninguna."""
    if grabando and trabajos:
        return MOTIVOS[2]
    return MOTIVOS[0] if grabando else (MOTIVOS[1] if trabajos else "")


class Despierto:
    """Lo que se ha pedido a Windows. al_dia(motivo) solo llama a Windows al cambiar (de nada a algo o al reves);
    siempre desde el mismo hilo. Nunca lanza: el vigilante que lo llama no puede morir por esto."""

    def __init__(self, llamar=_set_thread_execution_state):
        self._f = llamar() if callable(llamar) else None
        self.pedido, self.motivo, self.desde, self.error, self.veces = False, "", None, None, 0

    @property
    def disponible(self):
        return self._f is not None

    def al_dia(self, por):
        try:
            self.motivo = por if por in MOTIVOS else ""
            quiere = bool(self.motivo)
            if self._f is None or quiere == self.pedido:
                return self.pedido
            r = self._f(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if quiere else 0))
            if not r:  # NULL: no se ha podido (se vuelve a probar en la vuelta siguiente; el log, una vez)
                err = ctypes.get_last_error() if WINDOWS else 0
                if self.error != err:
                    log(f"   PC despierto: Windows no lo acepta (error {err})")
                self.error = err
                return self.pedido
            self.pedido, self.error = quiere, None
            self.desde = time.time() if quiere else None
            if quiere:
                self.veces += 1
                log(f"   PC despierto: pedido a Windows ({self.motivo})")
            else:
                log("   PC despierto: ya no hace falta, se suelta")
        except Exception:  # noqa: BLE001
            pass
        return self.pedido

    def estado(self):
        """Para el diagnostico: una linea."""
        if self._f is None:
            return "PC despierto: no disponible (solo en Windows)"
        if self.pedido:
            mins = int((time.time() - (self.desde or time.time())) // 60)
            return f"PC despierto: pedido a Windows ({self.motivo}) desde hace {mins} min · {self.veces} veces"
        if self.error is not None:
            return f"PC despierto: Windows no lo acepta (error {self.error})"
        return f"PC despierto: no hace falta ahora (nada grabando ni en cola) · {self.veces} veces en esta sesión"
