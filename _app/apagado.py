# -*- coding: utf-8 -*-
"""El icono de la bandeja (bandeja.py: abrir, grabar, salir, arrancar con Windows), los avisos de Windows (tell,
ask_yes_no) y el apagado ordenado (shutdown).

Salio tal cual de server.py al partirlo (15/11/2026); server.py lo reexporta. SERVER, WINDOW y DATA_DIR los fija
main() al arrancar: server.py los copia aqui (server.COMPARTIDAS)."""
import logging
import os
import threading
import time

from api import SERVER, WINDOW, Handler
from bandeja import Bandeja
from common import APP_NAME, WINDOWS, ApiError, log, log_exc
from inicio_windows import inicio_windows_leer, inicio_windows_poner
from instancia import open_ui, remove_token
from rutas import DATA_DIR


def tray_estado():
    """Lo que dice el icono: segundos de la grabacion en curso (o None) y trabajos en cola o en marcha."""
    a = Handler.app
    if a is None:
        return {"grabando": None, "trabajos": 0}
    rec = a.rec
    with a.jobs.lock:
        n = sum(1 for j in a.jobs.list if j.status in ("queued", "running"))
    return {"grabando": time.time() - rec.started if rec and rec.alive() else None, "trabajos": n}


def tray_balloon(titulo, texto):
    w = WINDOW
    return bool(w is not None and w.tray is not None and w.tray.alive and w.tray.balloon(titulo, texto))


def tray_abrir():
    """Clic en el icono (o «Abrir»): la ventana propia, delante."""
    w = WINDOW
    if w is not None and w.request_show() is None:
        open_ui()


def tray_grabar():
    """«Grabar» / «Parar la grabación» del menu: lo mismo que Ctrl+Alt+R (los avisos los da rec_toggle)."""
    if Handler.app is not None:
        try:
            Handler.app.rec_toggle({"source": "bandeja"})
        except ApiError:
            pass  # rec_toggle ya lo ha dicho (toast) y anotado


def tell(titulo, texto):
    """Aviso de Windows (MessageBox, delante de todo) desde un hilo cualquiera; fuera de Windows, al log. Para lo que no
    puede quedarse en un globo de la bandeja (con «No molestar» Windows no los ensena)."""
    log(f"   aviso: {titulo}")
    if not WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, texto, titulo, 0x0 | 0x40 | 0x10000 | 0x40000)  # MB_OK, ICONINFORMATION
    except Exception:
        log_exc()


def ask_yes_no(titulo, texto):
    """Pregunta Si/No de Windows (MessageBox delante de todo), desde un hilo cualquiera. Fuera de Windows, si."""
    if not WINDOWS:
        return True
    try:
        import ctypes
        # MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2 | MB_SETFOREGROUND | MB_TOPMOST; IDYES = 6
        return ctypes.windll.user32.MessageBoxW(None, texto, titulo, 0x4 | 0x20 | 0x100 | 0x10000 | 0x40000) == 6
    except Exception:
        log_exc()
        return False


def tray_salir():
    """«Salir» del icono de la bandeja: el mismo apagado que Ajustes > Apagar (App.apagar). Grabando, no (un globo lo
    dice); con trabajos en marcha, la misma pregunta que la pagina antes de apagar."""
    a = Handler.app
    if a is None:
        return
    if a.rec and a.rec.alive():
        log("   salir desde la bandeja: está grabando, no se sale")
        tell(f"{APP_NAME} está grabando", "Para la grabación antes de salir (Ctrl+Alt+R o su botón Parar).")
        return
    n = tray_estado()["trabajos"]
    if n and not ask_yes_no(f"Salir de {APP_NAME}",
                            f"Hay {n} trabajo{'s' if n != 1 else ''} en marcha (transcripciones o resúmenes): se "
                            "cancelan (lo ya escrito se conserva) y podrás relanzarlos al volver.\n\n"
                            f"¿Salir de {APP_NAME}?"):
        log("   salir desde la bandeja: cancelado (trabajos en marcha)")
        return
    try:
        a.apagar({"origen": "bandeja"})
    except ApiError as e:  # ha empezado a grabar entretanto
        tell(APP_NAME, e.msg)


def make_tray(ico_path):
    """El icono de la bandeja (solo Windows, con la ventana propia): bandeja.Bandeja, aun sin poner."""
    if not WINDOWS:
        return None
    return Bandeja(ico_path, {"abrir": tray_abrir, "grabar": tray_grabar, "salir": tray_salir,
                              "inicio": (inicio_windows_leer, inicio_windows_poner)}, tray_estado)


def shutdown(reason, exit_fn=os._exit):
    """Apagado ordenado (Ajustes > Apagar): medidor/grabadora, trabajos y procesos de Whisper, servidor HTTP, ventana
    y fin del proceso. recorder.py, worker.py y voices.py mueren solos al quedarse sin padre. No llamarlo desde el
    hilo que ejecuta serve_forever (SERVER.shutdown esperaria a si mismo)."""
    log(f"apagando ({reason})")
    try:
        if Handler.app is not None:
            Handler.app.shutdown_prepare()
    except Exception:
        log_exc()
    remove_token(DATA_DIR, Handler.token)  # os._exit no pasa por atexit
    try:  # el icono de la bandeja, antes de cerrar el log (window.destroy() lo repetiria sin efecto)
        if WINDOW is not None and getattr(WINDOW, "tray", None) is not None:
            WINDOW.tray.close()
    except Exception:
        log_exc()
    log("apagado")
    logging.shutdown()  # antes de parar el servidor: en cuanto serve_forever vuelve, el hilo principal sale solo
    try:
        if WINDOW is not None:
            WINDOW.destroy()
    except Exception:
        pass
    try:
        if SERVER is not None:
            SERVER.shutdown()
    except Exception:
        pass
    exit_fn(0)


def schedule_shutdown(reason, delay=0.5):
    """Termina el proceso al cabo de `delay` s: tiempo para que la respuesta de /api/apagar llegue a la pagina."""
    t = threading.Timer(delay, shutdown, args=(reason,))
    t.daemon = True
    t.start()
    return t
