# -*- coding: utf-8 -*-
"""Lo que se escapa de un hilo o del programa, al registro (punto 33 del review del 14/11/2026).

Con el acceso directo la app corre con pythonw.exe, que no tiene sys.stderr: la traza de una excepcion que se escapa de
un hilo (o del hilo principal) no iba a ninguna parte, y un fallo de Python en C (un acceso a memoria en una
libreria) tampoco dejaba nada. Ahora:
- threading.excepthook y sys.excepthook escriben la traza en server.log;
- si el que muere es un hilo vigia (VIGIAS: las colas de trabajos, el directo automatico, la llamada en curso…), se
  apunta en CAIDOS para avisar en la interfaz (/api/jobs) y en «Copiar diagnostico»: sin ese hilo, algo deja de
  funcionar hasta reiniciar;
- faulthandler escribe en DATA_DIR/fallos-graves.log las trazas de todos los hilos si Python cae por un fallo grave.
  Al arrancar, el de la sesion anterior (si tiene algo) pasa a fallos-graves.anterior.log. En Windows tambien salen
  ahi algunos accesos a memoria que el sistema recupera (los ve antes que quien los atiende): un volcado no es por si
  solo un cierre.
"""
import faulthandler
import os
import re
import sys
import threading
import time
import traceback

from common import LOGGER, TOKEN_FILE, log

FALLOS_GRAVES = "fallos-graves.log"
FALLOS_GRAVES_ANTERIOR = "fallos-graves.anterior.log"

# los hilos sin los que algo se para hasta reiniciar: nombre (o el de su funcion, «Thread-3 (_monitor_watchdog)») ->
# lo que deja de funcionar, dicho para el usuario
VIGIAS = {
    "worker-local": "la cola de transcripción en este PC",
    "worker-cloud": "la cola de transcripción en la nube",
    "worker-resumen": "los resúmenes",
    "worker-voces": "«Quién habla»",
    "live-watch": "el directo automático de las grabaciones de OBS",
    "meeting-watch": "el aviso de llamada en curso",
    "agenda-outlook": "la agenda de Outlook",
    "http": "la conexión con la ventana",
    "_monitor_watchdog": "el medidor de Grabar y el «PC despierto»",
}

CAIDOS = []  # [{"id", "hilo", "que", "a_las", "error"}]: los vigias que han muerto en esta sesion
_LOCK = threading.Lock()
# el fichero de faulthandler (tiene que seguir abierto mientras corre la app), cuantas lineas dejo la sesion anterior
# (apartadas en ESTA sesion: un .anterior de hace semanas no cuenta) y si aquella termino bien (None: no se sabe)
# arrastrado: el volcado de la anterior no se pudo apartar y sigue en el de esta (se escribe detras)
_ESTADO = {"fichero": None, "anterior": 0, "limpia": None, "arrastrado": False}


def vigia_de(nombre):
    """La clave de VIGIAS de un hilo por su nombre («worker-local», o «Thread-3 (_monitor_watchdog)» desde Python 3.10)
    o None si no es un vigia."""
    if nombre in VIGIAS:
        return nombre
    m = re.fullmatch(r"Thread-\d+ \((\w+)\)", nombre or "")
    return m.group(1) if m and m.group(1) in VIGIAS else None


def _traza(tipo, valor, tb):
    return "".join(traceback.format_exception(tipo, valor, tb)).rstrip()


def hilo(args):
    """threading.excepthook: la traza a server.log y, si es un vigia, a CAIDOS. Como el de Python, SystemExit no se
    dice. Nunca lanza nada (un fallo aqui se perderia igual)."""
    if args.exc_type is SystemExit:
        return
    try:
        nombre = args.thread.name if args.thread is not None else "?"
        error = getattr(args.exc_type, "__name__", "error")
        LOGGER.error("✖ hilo %s: se ha parado por un error (%s)\n%s", nombre, error,
                     _traza(args.exc_type, args.exc_value, args.exc_traceback))
        v = vigia_de(nombre)
        if v:
            with _LOCK:
                CAIDOS.append({"id": len(CAIDOS) + 1, "hilo": v, "que": VIGIAS[v], "a_las": time.strftime("%H:%M"),
                               "error": error})
            log(f"   ✖ se ha parado {VIGIAS[v]}: hasta reiniciar no funcionará")
    except Exception:  # noqa: BLE001
        pass


def programa(tipo, valor, tb):
    """sys.excepthook: lo que se escapa del hilo principal (sin consola no se veia). Ctrl+C, como siempre."""
    if issubclass(tipo, KeyboardInterrupt):
        return sys.__excepthook__(tipo, valor, tb)
    try:
        LOGGER.error("✖ error sin atender (%s)\n%s", tipo.__name__, _traza(tipo, valor, tb))
    except Exception:  # noqa: BLE001
        pass
    if sys.stderr is not None:  # con consola, tambien ahi
        try:
            sys.__excepthook__(tipo, valor, tb)
        except Exception:  # noqa: BLE001
            pass


def ganchos():
    """Al arrancar (main), justo despues de setup_logging: lo que se escape de un hilo o del principal, a server.log."""
    threading.excepthook = hilo
    sys.excepthook = programa


def instalar(data_dir):
    """faulthandler en DATA_DIR/fallos-graves.log (el de la sesion anterior, si tiene algo, pasa a
    fallos-graves.anterior.log), y los ganchos si aun no estan. En main, solo cuando esta copia ya es LA app (despues de
    la instancia unica): una segunda que se abre y se cierra no puede apartar ni vaciar el fichero de la que corre.
    Devuelve si faulthandler quedo puesto."""
    ganchos()
    ruta, modo = os.path.join(data_dir, FALLOS_GRAVES), "w"
    _ESTADO["anterior"], _ESTADO["arrastrado"] = 0, False
    # la sesion anterior termino bien si borro su sesion.token (shutdown() lo borra antes de os._exit; atexit, con
    # Ctrl+C): si sigue ahi (instalar va justo antes de start_session, que lo reescribe), se cerro de golpe
    # (sin settings.json es la primera vez: no hay sesion anterior de la que decir nada)
    primera = not os.path.exists(os.path.join(data_dir, "settings.json"))
    _ESTADO["limpia"] = None if primera else not os.path.exists(os.path.join(data_dir, TOKEN_FILE))
    if _ESTADO["limpia"] is False:  # tambien al cerrar la consola de Tenca.bat (no pasa por atexit)
        log("   la sesión anterior terminó sin apagar")
    try:
        if os.path.getsize(ruta) > 0:
            try:
                os.replace(ruta, os.path.join(data_dir, FALLOS_GRAVES_ANTERIOR))
            except OSError:  # no se puede apartar: se escribe detras, sin vaciarlo
                modo, _ESTADO["arrastrado"] = "a", True
            else:
                _ESTADO["anterior"] = len(volcado(os.path.join(data_dir, FALLOS_GRAVES_ANTERIOR)))
                log(f"   la sesión anterior dejó un volcado de faulthandler ({_ESTADO['anterior']} líneas): "
                    f"{FALLOS_GRAVES_ANTERIOR}")
    except OSError:
        pass
    try:
        antes, _ESTADO["fichero"] = _ESTADO["fichero"], open(ruta, modo, encoding="utf-8")
        faulthandler.enable(file=_ESTADO["fichero"], all_threads=True)
        if antes is not None:  # una segunda llamada (no en la app): el de antes no se queda abierto
            antes.close()
        return True
    except (OSError, ValueError, RuntimeError) as e:
        log(f"   sin volcado de fallos graves ({e.__class__.__name__})")
        return False


_FRAME = re.compile(r'\s*File "(.+)", line (\d+) in ([\w<>.]+)')


def volcado(ruta, maximo=40):
    """De un fichero de faulthandler, solo lo tecnico: la cabecera («Windows fatal exception: access violation»,
    «Current thread») y cada llamada como «server.py:123 main» (el .py sin su ruta, que lleva el nombre del usuario).
    Lo demas, fuera. Como mucho `maximo` lineas (las primeras)."""
    try:
        with open(ruta, encoding="utf-8", errors="replace") as f:
            lineas = f.read().splitlines()
    except OSError:
        return []
    out = []
    for l in lineas:
        m, s = _FRAME.match(l), l.strip()
        if m:
            out.append(f"  {os.path.basename(m.group(1).replace(chr(92), '/'))}:{m.group(2)} {m.group(3)}")
        elif s.startswith(("Fatal Python error:", "Windows fatal exception:")):
            cabeza, _, que = s.partition(":")
            out.append(f"{cabeza}: {re.sub(r'[^A-Za-z0-9 _]', '', que).strip()[:60]}")
        elif s.startswith(("Current thread", "Thread 0x")):
            out.append("Current thread" if s.startswith("Current") else "Thread")
        if len(out) >= maximo:
            break
    return out


def caidos():
    with _LOCK:
        return [dict(c) for c in CAIDOS]


def diagnostico(data_dir):
    """Las lineas de «Copiar diagnostico»: los vigias caidos, como termino la sesion anterior y los volcados de
    faulthandler (solo lo tecnico). El de la sesion anterior, solo si se aparto al arrancar esta (no uno de hace
    semanas). Un volcado de una sesion que termino bien no fue una caida: en Windows, faulthandler tambien vuelca
    accesos a memoria que se recuperan (.NET de la ventana, COM)."""
    c = caidos()
    out = ["Hilos vigía: " + ("todos en marcha" if not c else " · ".join(
        f"✖ {x['hilo']} parado a las {x['a_las']} ({x['error']})" for x in c))]
    limpia = _ESTADO["limpia"]
    if limpia is not None:
        de_golpe = "terminó sin apagar (se cerró la consola o el proceso, corte de luz o fallo grave)"
        out.append("Sesión anterior: " + ("terminó bien" if limpia else de_golpe))
    volcados = ([(FALLOS_GRAVES_ANTERIOR, "sesión anterior")] if _ESTADO["anterior"] else []) + [
        (FALLOS_GRAVES, "esta sesión")]
    for nombre, etiqueta in volcados:
        v = volcado(os.path.join(data_dir, nombre))
        if v:
            if etiqueta == "esta sesión" and _ESTADO["arrastrado"]:
                nota = "con el de la sesión anterior, que no se pudo apartar"
            elif etiqueta == "esta sesión":
                nota = "sigue abierta: un error recuperado"
            else:
                nota = {True: "terminó bien: un error recuperado, no una caída", False: "terminó sin apagar",
                        None: "sin saber cómo terminó"}[limpia]
            out += [f"Volcado de faulthandler ({etiqueta}; {nota}):", *v]
    return out
