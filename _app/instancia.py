# -*- coding: utf-8 -*-
"""Instancia unica y arranque: la marca de instancia (instancia-<puerto>.lock), el puerto, el ping a otra copia (la
misma, otra version o una hermana que arranca a la vez), retirar una version anterior, abrir la interfaz (ui_url,
open_ui), el token de sesion y fatal().

Salio tal cual de server.py al partirlo (15/11/2026); server.py lo reexporta. PORT, URL y DATA_DIR los fija main() al
arrancar: server.py los copia aqui (server.COMPARTIDAS)."""
import atexit
import errno
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import engines
from api import HOST, PORT, URL, Handler, Server
from common import APP_NAME, TOKEN_FILE, TOKEN_HEADER, WINDOWS, alert, local_opener, log, log_exc, read_token
from rutas import DATA_DIR, VERSION


def ui_url(token=None):
    """La direccion con la que se abre la interfaz: con ?t=<token> el servidor entrega la cookie de sesion y redirige a
    "/" (P1-02). Sin token (el de esta copia si no se pasa otro), la de siempre."""
    token = Handler.token if token is None else token
    return URL + (f"?t={token}" if token else "")


UI_REOPEN_GAP = 15.0  # s alrededor del Edge del arranque en los que no se abre otro: aun puede estar abriendose
_UI_OPENED = {"inicio": 0.0, "ultimo": 0.0}  # time.monotonic() del Edge del arranque y del ultimo (0: nunca)
_UI_LOCK = threading.Lock()


def open_ui(token=None, inicio=False):
    """La interfaz en Edge (modo app, sin barra de direcciones) o, sin Edge, en el navegador por defecto. Es el
    camino de siempre y el de reserva cuando no hay ventana propia (window.py). Con dos lanzamientos seguidos, el Edge
    del arranque (`inicio`) y el que pide el acceso directo serian dos ventanas: en UI_REOPEN_GAP s alrededor del
    primero no se abre otro, en ningun orden. Despues, cada peticion abre el suyo (Edge cerrado: hay que volver a
    abrirlo). True si lo abre."""
    with _UI_LOCK:
        ahora = time.monotonic()
        antes = _UI_OPENED["ultimo" if inicio else "inicio"]
        if antes and ahora - antes < UI_REOPEN_GAP:
            log(f"   Edge se ha abierto hace {ahora - antes:.0f} s: no se abre otro")
            return False
        _UI_OPENED["ultimo"] = ahora
        if inicio:
            _UI_OPENED["inicio"] = ahora
    url = ui_url(token)
    for exe in (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"):
        exe = os.path.expandvars(exe)
        if os.path.exists(exe):
            subprocess.Popen([exe, f"--app={url}", "--window-size=1500,920"])
            return True
    webbrowser.open(url)
    return True


def _local_request(path, token, data=None, timeout=1.5):
    """Peticion a la copia que corre en este puerto: sin proxy (P1-13) y con el token de DATA_DIR (P1-02)."""
    headers = {TOKEN_HEADER: token} if token else {}
    if data is not None:
        headers.update({"X-Audios": "1", "Content-Type": "application/json"})
    req = urllib.request.Request(URL + path, data=data, method="POST" if data is not None else "GET", headers=headers)
    with local_opener().open(req, timeout=timeout) as r:
        return json.load(r)


def probe_running(token=None, timeout=1.5):
    """Que hay en el puerto: "app" (esta app, y acepta nuestro token), "libre" (nadie escucha), "arrancando" (acepta la
    conexion pero no contesta: otra copia entre reservar el puerto y empezar a servir) u "otro" (otro programa, u otra
    sesion de Windows con su propio token)."""
    token = read_token(DATA_DIR) if token is None else token
    try:
        r = _local_request("api/ping", token, timeout=timeout)
    except urllib.error.HTTPError:
        return "otro"
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError):
            return "libre"
        return "arrancando" if isinstance(e.reason, TimeoutError) else "otro"
    except TimeoutError:
        return "arrancando"
    except Exception:
        return "otro"
    if r.get("app") != "audios":
        return "otro"
    # "otra_version": nuestra app, pero de otra version (sin `version` en el ping, anterior al 02/11/2026, o distinta):
    # una actualizacion en el sitio con la anterior aun abierta, o un acceso directo viejo. Si se le cediera la ventana,
    # serviria los ficheros nuevos con la API vieja y los botones no harian nada (02/11/2026)
    return "app" if r.get("version") == VERSION else "otra_version"


def _wait_port_free(wait, token=None):
    """True si el puerto queda libre antes de `wait` s."""
    end = time.time() + wait
    while time.time() < end:
        if probe_running(token, timeout=1.0) == "libre":
            return True
        time.sleep(0.25)
    return probe_running(token, timeout=1.0) == "libre"


def _port_owner_pid(port):
    """PID que escucha en 127.0.0.1:port (netstat; solo Windows), o None."""
    try:
        r = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=15,
                           creationflags=engines.NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(f":{port}") \
                and parts[1].rsplit(":", 1)[0] in ("127.0.0.1", "0.0.0.0") and parts[3].upper() == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


def _is_python_process(pid):
    """El proceso es python.exe o pythonw.exe (tasklist; solo Windows): la app anterior, nunca otro programa."""
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                           timeout=15, creationflags=engines.NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.startswith('"python.exe"') or line.startswith('"pythonw.exe"') for line in r.stdout.splitlines())


def retire_other_version(token=None, wait=10.0):
    """Otra version de la app tiene el puerto. Se le pide que se apague (POST /api/apagar, desde el 31/10/2026) y, si
    no sabe o no lo hace, en Windows se termina su proceso: nunca si esta grabando. Devuelve el estado final del
    puerto (probe_running) o "grabando"."""
    token = read_token(DATA_DIR) if token is None else token
    try:
        ping = _local_request("api/ping", token)
    except Exception:
        ping = {}
    if ping.get("version") == VERSION:  # entretanto el puerto lo ha tomado una copia de esta version: no se toca
        return "app"
    vieja, vieja_pid = ping.get("version") or ("anterior al 02/11/2026" if ping else "desconocida"), ping.get("pid")
    log(f"otra versión de {APP_NAME} ({vieja}) tiene el puerto {PORT}: se le pide que se cierre")
    try:
        if _local_request("api/rec/status", token).get("recording"):
            log("   está grabando: no se toca")
            return "grabando"
    except Exception:
        pass
    try:
        _local_request("api/apagar", token, b"{}", timeout=5)
    except Exception as e:  # 404 en las versiones sin /api/apagar, 409 si grabando, o sin respuesta
        log(f"   /api/apagar: {e}")
    if _wait_port_free(wait / 2, token):
        log("   cerrada")
        return "libre"
    if WINDOWS and probe_running(token) != "app":  # si el puerto es ya de una copia de esta version, no se mata
        pid = _port_owner_pid(PORT)
        if pid and pid != os.getpid() and (not vieja_pid or pid == vieja_pid) and _is_python_process(pid):
            log(f"   no se ha cerrado: se termina su proceso (pid {pid})")
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15,
                               creationflags=engines.NO_WINDOW)
            except (OSError, subprocess.SubprocessError):
                log_exc()
            if _wait_port_free(wait / 2, token):
                log("   cerrada")
                return "libre"
    estado = probe_running(token)
    log(f"   sigue ahí ({estado})")
    return estado


def already_running(token=None):
    """La app ya corre en este puerto y es la nuestra (contesta con el token de DATA_DIR): no la de otro usuario."""
    return probe_running(token) == "app"


def allow_foreground(pid=None):
    """Windows solo deja ponerse delante a quien tiene permiso: el acceso directo que se acaba de pulsar lo tiene y
    se lo cede a la copia que corre (`pid`; sin el, a cualquiera, ASFW_ANY), para que su ventana no se quede detras
    parpadeando en la barra de tareas. Solo Windows."""
    if not WINDOWS:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.AllowSetForegroundWindow.argtypes = [ctypes.c_ulong]
        return bool(user32.AllowSetForegroundWindow(int(pid) if pid else 0xFFFFFFFF))
    except Exception:
        return False


def ask_running_to_show(token=None):
    """Pide a la copia que ya corre que ensene su ventana (POST /api/mostrar) y le cede el primer plano. Devuelve su
    respuesta ({"ok": true} y, si su ventana aun se esta abriendo, "pending"; si ha abierto Edge, "browser") o None si
    no contesta que si: entonces se abre Edge desde aqui, como antes."""
    token = read_token(DATA_DIR) if token is None else token
    try:
        allow_foreground(_local_request("api/ping", token).get("pid"))
    except Exception:
        pass
    try:
        r = _local_request("api/mostrar", token, b"{}", timeout=5)
    except Exception as e:
        log(f"   /api/mostrar: {e}")
        return None
    return r if isinstance(r, dict) and r.get("ok") else None


INSTANCE_FILE = "instancia-{port}.lock"
_INSTANCE = []  # el descriptor del fichero bloqueado: abierto mientras viva el proceso (el sistema lo suelta al morir)
_LOCK_BYTE = 1 << 30  # en Windows se bloquea este byte, lejos del contenido (pid y hora), que las demas copias leen
INSTANCE_WAIT = 8.0  # s que una segunda copia espera a que la otra conteste o suelte la marca (si se esta cerrando)
INSTANCE_STUCK = 180.0  # s con la marca sin contestar a partir de los que se avisa de que la otra copia no responde


def _lock_once(path):
    """True: bloqueado por este proceso; False: lo tiene otro; None: no se ha podido comprobar."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)  # no heredable (PEP 446): recorder, worker… no lo retienen
    except OSError as e:
        log(f"   marca de instancia: no se puede abrir ({e})")
        return None
    try:
        if WINDOWS:
            import msvcrt
            os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)  # msvcrt.locking bloquea desde la posicion actual (sin esperar)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if isinstance(e, BlockingIOError) or (WINDOWS and e.errno in (errno.EACCES, errno.EDEADLK)):
            return False
        log(f"   marca de instancia: no se puede bloquear ({e})")
        return None
    try:  # quien la tiene y desde cuando: lo lee una segunda copia para saber si esta se ha quedado colgada
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()} {int(time.time())}\n".encode("ascii"))
    except OSError:
        pass
    _INSTANCE[:] = [fd]
    return True


def instance_holder(data_dir, port):
    """(pid, segundos desde que tomo la marca) de la copia que la tiene, o (None, None) si no se sabe."""
    try:
        with open(os.path.join(data_dir, INSTANCE_FILE.format(port=port)), "rb") as f:
            pid, t = f.read(64).decode("ascii").split()[:2]
        return int(pid), max(0, int(time.time()) - int(t))
    except (OSError, ValueError, UnicodeDecodeError):
        return None, None


def release_instance():
    """Suelta la marca antes de tiempo (fatal: esta copia no va a arrancar; que la siguiente no la espere)."""
    for fd in _INSTANCE:
        try:
            os.close(fd)
        except OSError:
            pass
    _INSTANCE[:] = []


def instance_lock(data_dir, port, wait=0.0):
    """Instancia unica: DATA_DIR\\instancia-<puerto>.lock bloqueado mientras viva esta copia (lo suelta el sistema al
    terminar el proceso, tambien si se cuelga o lo matan). True: esta copia es la app; False: otra copia con la misma
    carpeta de datos y el mismo puerto sigue viva, arrancando o abierta (`wait` s esperando a que lo suelte); None: no
    se ha podido comprobar y se sigue como antes, solo por el puerto. Va antes que el puerto: un segundo lanzamiento
    lo sabe al instante, sin esperar a que la primera copia conteste (en un PC lento tarda decenas de segundos)."""
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError as e:
        log(f"   marca de instancia: sin carpeta de datos ({e})")
        return None
    path = os.path.join(data_dir, INSTANCE_FILE.format(port=port))
    end = time.monotonic() + wait
    while True:
        r = _lock_once(path)
        if r is not False or time.monotonic() >= end:
            return r
        time.sleep(0.25)


def wait_instance(estado, wait=None):
    """Otra copia tiene la marca y no contesta como la app (`estado`): arranca (doble clic en el acceso directo), se
    esta cerrando (Apagar y volver a abrir enseguida), tarda en contestar (abierta pero ocupada) o, en una
    actualizacion, esta retirando la version anterior ("hermana"). Hasta INSTANCE_WAIT s a que conteste (se le pedira
    la ventana) o suelte la marca (entonces arranca esta). Devuelve (instancia, estado) como en main()."""
    end = time.monotonic() + (INSTANCE_WAIT if wait is None else wait)
    while time.monotonic() < end:
        time.sleep(0.5)
        instancia = instance_lock(DATA_DIR, PORT)
        if instancia is not False:  # la ha soltado (o ya no se puede comprobar): esta copia sigue como un arranque
            return instancia, probe_running()
        estado = probe_running(timeout=1.5)
        if estado == "app":
            break
    return False, estado


def _holder_is_port_owner():
    """La marca la tiene quien contesta en el puerto (su /api/ping lleva su pid desde el 07/11/2026). Si no (la otra
    version es anterior a la marca), la tiene una copia hermana de esta version."""
    pid, _ = instance_holder(DATA_DIR, PORT)
    try:
        return bool(pid) and pid == _local_request("api/ping", read_token(DATA_DIR)).get("pid")
    except Exception:
        return False


def show_running(no_browser, estado="app"):
    """Esta copia sobra (la app ya corre, o se esta abriendo en otra copia): le pide su ventana y termina. Sin
    respuesta de la otra (`estado` distinto de "app": aun no sirve), nada: su ventana saldra sola al terminar de
    arrancar; si lleva mas de INSTANCE_STUCK s sin contestar, se avisa de que no responde. Si contesta pero no ensena
    nada (no deberia pasar con la misma version), Edge desde aqui, como antes."""
    if estado != "app":
        log(f"otra copia tiene la marca y no contesta ({estado}): si se está abriendo, su ventana saldrá sola · pid "
            f"{os.getpid()}")
        pid, hace = instance_holder(DATA_DIR, PORT)
        if pid:
            log(f"   la otra copia: pid {pid}, desde hace {hace} s")
        if pid and hace > INSTANCE_STUCK:
            alert(f"{APP_NAME} no responde",
                  f"{APP_NAME} ya está abierta (pid {pid}, desde hace {hace // 60} min) pero no contesta.\n\nSi su "
                  f"ventana no aparece, ciérrala en el Administrador de tareas (pythonw.exe, pid {pid}) y vuelve a "
                  f"abrir {APP_NAME}.")
        return
    log(f"la app ya estaba abierta: se enseña su ventana · pid {os.getpid()}")
    if no_browser:
        return
    r = ask_running_to_show()
    if r is None:
        log("   no contesta a /api/mostrar: se abre en Edge desde aquí")
        open_ui(read_token(DATA_DIR))
    elif r.get("pending"):
        log("   aún se está abriendo: su ventana saldrá sola")


PORT_WAIT = 20.0  # s que se espera a otra copia que arranca a la vez (doble clic en el acceso directo)


def reserve_port(wait=None):
    """P1-13: abre el puerto ANTES de crear App() (y sus hilos: atajo Ctrl+Alt+R, vigilante de reuniones, directo).
    Si ya lo tiene otra copia que esta arrancando a la vez, espera a que conteste y devuelve None (la llamada le pide
    su ventana); si lo tiene otro programa, el aviso de siempre y fuera."""
    end = time.time() + (PORT_WAIT if wait is None else wait)
    while True:
        try:
            return Server((HOST, PORT), Handler)
        except OSError as e:
            err = e
        estado = probe_running()
        if estado == "app":
            return None
        if estado in ("otro", "otra_version") or time.time() >= end:
            break
        time.sleep(0.3)  # "libre" (se acaba de soltar) o "arrancando": otra vuelta
    log(f"puerto {PORT} ocupado ({estado}): {err}")
    if estado == "otra_version":
        fatal(f"Hay abierta otra versión de {APP_NAME} que no se ha podido cerrar.\n\nCiérrala (su ventana de "
              f"consola o, en el Administrador de tareas, pythonw.exe) y vuelve a abrir {APP_NAME}.")
    fatal(f"No se puede usar el puerto {PORT}: lo tiene otro programa ({err}).\n\nCierra ese programa (o arranca "
          f"{APP_NAME} con --port 8766) y vuelve a abrir {APP_NAME}.")


def write_token(data_dir, token):
    """DATA_DIR\\sesion.token (UTF-8, sin salto de linea; DATA_DIR es privado del usuario)."""
    os.makedirs(data_dir, exist_ok=True)
    fd = os.open(os.path.join(data_dir, TOKEN_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(token)


def remove_token(data_dir, token):
    """Al apagar: borra sesion.token si sigue siendo el de esta sesion (no el de otra copia que arrancara despues)."""
    try:
        if token and read_token(data_dir) == token:
            os.remove(os.path.join(data_dir, TOKEN_FILE))
    except OSError:
        pass


def start_session(data_dir):
    """P1-02: un token nuevo en cada arranque. Lo exige el Handler en /api/* y /media, y lo leen de DATA_DIR el acceso
    directo, el aviso flotante, el instalador y las pruebas."""
    token = secrets.token_urlsafe(32)
    write_token(data_dir, token)
    Handler.token = token
    atexit.register(remove_token, data_dir, token)  # salidas normales (Ctrl+C); shutdown() lo borra antes de _exit
    return token


def fatal(text):
    """Error de arranque: al log, a la consola si la hay y, sin consola (pythonw.exe), un aviso de Windows."""
    release_instance()  # mientras el aviso esta abierto, otro lanzamiento no debe esperar a esta copia
    alert(f"{APP_NAME} no ha podido arrancar", text)
    sys.exit(1)
