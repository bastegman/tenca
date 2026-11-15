# -*- coding: utf-8 -*-
"""Lo que rodea a la grabadora propia (recorder.py) y es casi todo de Windows: atajo global Ctrl+Alt+R, pitidos,
avisos (toast), cierre de la cabecera WAV, el proceso de la grabadora y la deteccion de una reunion en curso.
Fuera de Windows cada pieza se desactiva o se limita a escribir en el log.
"""
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time

import bandeja
import directo_avisos
import engines
import live
import voices
from common import WINDOWS, log


class GlobalHotkey(threading.Thread):
    """Ctrl+Alt+R en todo Windows (aunque la app este en segundo plano o estes en Teams).
    RegisterHotKey exige que el registro y el bucle de mensajes vayan en el mismo hilo."""

    MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, VK_R, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x52, 0x0312

    def __init__(self, callback):
        super().__init__(daemon=True, name="hotkey")
        self.callback, self.ok, self.error, self.last = callback, False, None, 0.0

    def run(self):
        if not WINDOWS:
            self.error = "el atajo global solo existe en Windows"
            return
        import ctypes
        import ctypes.wintypes as wt
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, self.MOD_CONTROL | self.MOD_ALT | self.MOD_NOREPEAT, self.VK_R):
            self.error = "Ctrl+Alt+R ya lo usa otro programa"
            log(f"   atajo no disponible: {self.error}")
            return
        self.ok = True
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == self.WM_HOTKEY and time.time() - self.last > 1.5:  # evita dobles pulsaciones
                self.last = time.time()
                threading.Thread(target=self.callback, daemon=True).start()


def cue(kind):
    """Pitido corto y bajito: sube al empezar, baja al parar (se oye sin mirar la pantalla)."""
    if not WINDOWS:
        return

    def play():
        try:
            import io
            import wave
            import winsound
            import numpy as np
            notes = {"start": (660, 880), "stop": (880, 660), "error": (300, 220)}[kind]
            sr, parts = 44100, []
            for f in notes:
                t = np.arange(int(sr * 0.09)) / sr
                parts.append(np.sin(2 * np.pi * f * t) * np.minimum(1, np.minimum(t, t[::-1]) * 200))
                parts.append(np.zeros(int(sr * 0.04)))
            pcm = (np.concatenate(parts) * 0.18 * 32767).astype("<i2")
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes(pcm.tobytes())
            winsound.PlaySound(buf.getvalue(), winsound.SND_MEMORY)
        except Exception:
            pass
    play()


# Aviso de Windows (toast) con PowerShell. El script es FIJO: el titulo y el texto le llegan por variables de entorno
# y nunca se pegan dentro del codigo. Asi un nombre de proyecto o de tema con comillas tipograficas (‘ ’ ‚ ‛, que
# PowerShell tambien toma por comillas simples), « " », $, ` o saltos de linea no puede cerrar la cadena ni ejecutar
# nada, y «Pla d’acció» sale tal cual en el aviso.
TOAST_ENV = ("CHULETA_AVISO_TITULO", "CHULETA_AVISO_TEXTO")
TOAST_PS = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType = WindowsRuntime] > $null;"
            "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            "$x = $t.GetElementsByTagName('text');"
            f"$x.Item(0).AppendChild($t.CreateTextNode([string]$env:{TOAST_ENV[0]})) > $null;"
            f"$x.Item(1).AppendChild($t.CreateTextNode([string]$env:{TOAST_ENV[1]})) > $null;"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
            "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
            ".Show([Windows.UI.Notifications.ToastNotification]::new($t))")


def toast_text(s, limit=400):
    """Texto apto para una variable de entorno (sin NUL) y de un largo razonable para un aviso."""
    return str(s if s is not None else "").replace("\x00", "")[:limit]


def toast_command(title, body):
    """(argv, env) del PowerShell que ensena el aviso: el script no depende de los textos, que van en `env`."""
    import base64
    env = dict(os.environ)
    env[TOAST_ENV[0]], env[TOAST_ENV[1]] = toast_text(title), toast_text(body)
    argv = ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
            "-EncodedCommand", base64.b64encode(TOAST_PS.encode("utf-16-le")).decode()]
    return argv, env


def notify(title, body):
    """Notificacion de Windows (por si la ventana de la app no esta a la vista). Con el icono de la bandeja puesto, por
    su globo (sale con el icono y el nombre de la app); si no, o si el globo no se puede pedir, por PowerShell (sale
    firmada por «Windows PowerShell»)."""
    if not WINDOWS:
        log(f"   aviso: {title} — {body}")
        return
    try:
        if bandeja.globo(toast_text(title), toast_text(body)):
            return
    except Exception:  # el aviso no se pierde por el globo: va por PowerShell
        pass
    argv, env = toast_command(title, body)
    try:
        subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=engines.NO_WINDOW)
    except (OSError, ValueError):
        pass


def close_wav(path):
    """Corrige la cabecera de un WAV que se quedo abierto (grabadora que murio sin cerrar)."""
    info = live.wav_info(path)
    if not info or info["data_size"] is not None:
        return
    size = os.path.getsize(path)
    with open(path, "r+b") as f:
        f.seek(4)
        f.write(struct.pack("<I", size - 8))
        f.seek(info["data_off"] - 4)
        f.write(struct.pack("<I", size - info["data_off"]))


# Dos pistas (punto 20 del review del 14/11/2026): micro a la izquierda y lo que suena a la derecha (pistas.py). El
# directo, la cola y la nube leen la suma, que es la mezcla de antes.
PISTAS = True
PASO_TU = 0.25  # recorder.PasosTu.PASO (pistas.PASO)


# Espacio en disco al empezar a grabar (punto 1 del review del 14/11/2026: no se miraba en ningun sitio). El WAV ocupa
# 691 MB por hora (48 kHz, estereo, 16 bits); recorder.py lo vuelve a mirar cada 30 s mientras graba.
WAV_BYTES_S = 48000 * 4
DISCO_MIN_MB = 500        # por debajo no se empieza
DISCO_AVISO_MIN = 120     # por debajo de ~2 h se avisa al empezar
DISCO_FIN_MIN = 15        # y mientras graba, cuando quedan ~15 min


def disco_libre(folder):
    """Bytes libres en el disco de `folder` (None si no se puede saber)."""
    try:
        return shutil.disk_usage(folder).free
    except (OSError, ValueError):
        return None


def minutos_txt(m):
    return f"{m} min" if m < 90 else f"{m / 60:.1f} h".replace(".", ",")


def disco_al_empezar(folder, libre=None):
    """(error, aviso) para empezar a grabar en `folder`: error si no llega a DISCO_MIN_MB (no se empieza), aviso si
    no caben ~2 h. (None, None) si hay sitio o no se puede saber."""
    libre = disco_libre(folder) if libre is None else libre
    if libre is None:
        return None, None
    mb, minutos = int(libre / 1e6), int(libre / WAV_BYTES_S / 60)
    if mb < DISCO_MIN_MB:
        return (f"No queda sitio en el disco para grabar: hay {mb} MB libres y hacen falta al menos {DISCO_MIN_MB} MB "
                "(una hora de reunión ocupa unos 690 MB). Libera espacio o graba en otra carpeta."), None
    if minutos < DISCO_AVISO_MIN:
        return None, f"queda poco disco: caben unos {minutos_txt(minutos)} de grabación"
    return None, None


def ultima_linea(path, desde=0, limite=8192):
    """La ultima linea de recorder.log escrita desde `desde` (bytes) que dice algo de un fallo; None si no hay."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            fin = f.tell()
            f.seek(max(desde, fin - limite))
            txt = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(txt.splitlines()):
        line = re.sub(r"^\d\d:\d\d:\d\d ", "", line.strip()).lstrip("✖ ").strip()
        if line and re.search(r"Error|Exception|error|no se puede|se para", line):
            return line[:200]
    return None


class RecorderProcess:
    """recorder.py en un proceso aparte: graba (o solo mide niveles) y manda su estado por stdout.

    on_end(self): la grabadora ha terminado sin que nadie la parase (stop() no se llamo): murio, la mataron o se paro
    por un fallo. motivo() dice por que (su linea «fatal» o la ultima de recorder.log). on_end se pone cuando ya graba
    (rec_start): si no llega a arrancar, eso lo dice rec_start y no se sigue en una continuacion. Si ya habia terminado
    al ponerlo, se avisa en ese momento."""

    TIMELINE_S = 30.0  # la linea de tiempo del micro (la de «Tú») se guarda cada 30 s, no solo al parar
    _on_end, stopping, fatal, avisado, disco_avisado, tl_busy, tl_at = None, False, None, False, False, False, 0.0
    apuntado = False  # el servidor ya apunto el corte (rec_status deja de dar «cortada» provisional)
    tu = None  # la linea de «Tú» de la grabadora (bytearray; None en los objetos que los tests montan con __new__)
    visto = None  # time.monotonic() del ultimo estado: si deja de llegar, la grabadora esta atascada (sin_estado_s)

    def __init__(self, path, mic, sys_dev, app_dir, data_dir):
        self.path, self.started, self.status, self.ended = path, time.time(), {}, False
        self.stopping, self.fatal, self._on_end = False, None, None
        self.avisado, self.end_lock = False, threading.Lock()
        self.levels = []  # (t, mic dB, sys dB) cada 0,25 s: cuando hablo yo (para etiquetar «Tu», sin biometria)
        self.tu = bytearray()  # la linea de «Tú» que manda la grabadora («tu»/«tu_i»), ya alineada con el archivo
        # fuente callada (aviso de la franja del directo): con cada estado; en_llamada lo pone el vigilante de llamadas
        self.callada, self.en_llamada = directo_avisos.Callada(log=log), False
        self.disco_avisado, self.tl_at, self.tl_busy = False, time.monotonic(), False
        self.log_path = os.path.join(data_dir, "recorder.log")
        try:
            self.log_off = os.path.getsize(self.log_path)
        except OSError:
            self.log_off = 0
        args = [path, *(["--pistas"] if PISTAS else [])] if path else ["--monitor"]
        err = open(self.log_path, "ab")
        self.p = subprocess.Popen([sys.executable, "-u", os.path.join(app_dir, "recorder.py"), *args,
                                   "--mic", mic or "default", "--sys", sys_dev or "default"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, cwd=app_dir,
                                  # prioridad alta: gasta muy poco, pero si se retrasa se pierde audio
                                  creationflags=engines.NO_WINDOW
                                  | getattr(subprocess, "ABOVE_NORMAL_PRIORITY_CLASS", 0))
        err.close()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            try:
                st = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(st, dict):
                continue
            self.visto = now = time.monotonic()  # una lectura del reloj por estado
            if st.get("fatal"):  # el porque de un fallo, justo antes de terminar
                self.fatal = str(st["fatal"])[:200]
                continue
            self.status = st
            self.note_levels(self.status)
            if self.path:
                try:
                    self.callada.feed(self.status, now, self.en_llamada)
                    self._cada_rato(st, now)
                except Exception:  # un aviso no puede parar la lectura del estado (ni la grabacion)
                    pass
        if self.path and os.path.isfile(self.path):
            try:
                close_wav(self.path)  # por si murio sin cerrar la cabecera
            except OSError:
                pass
        self.ended = True
        self._avisar_fin()

    @property
    def on_end(self):
        return self._on_end

    @on_end.setter
    def on_end(self, fn):
        with self.end_lock:
            self._on_end = fn
        self._avisar_fin()  # si ya habia terminado, ahora

    def _avisar_fin(self):
        if self._on_end is None:  # aun no graba (o es el medidor): nada que avisar; al ponerlo se vuelve a mirar
            return
        with self.end_lock:
            fn = self._on_end
            if not (self.ended and fn and self.path and not self.stopping) or self.avisado:
                return
            self.avisado = True
        try:
            fn(self)
        except Exception as e:  # el aviso de la grabacion cortada no puede romper nada mas
            log(f"   ✖ aviso de grabación cortada: {e.__class__.__name__}: {e}")

    def _cada_rato(self, st, now):
        """Con cada estado: la linea de tiempo del micro cada TIMELINE_S (si Tenca se cae, «Tú» no se pierde) y el
        aviso de Windows cuando quedan ~15 min de disco. La escritura va en otro hilo: este no puede pararse (la
        tuberia de Windows se llena en ~1,5 s y entonces se para la grabadora)."""
        if now - self.tl_at >= self.TIMELINE_S and not self.tl_busy and self.levels:
            self.tl_at, self.tl_busy = now, True
            threading.Thread(target=self._guardar_timeline, daemon=True, name="micro-timeline").start()
        m = st.get("disco_min")
        if isinstance(m, (int, float)) and m <= DISCO_FIN_MIN and not self.disco_avisado:
            self.disco_avisado = True
            log(f"   ⚠ queda poco disco: unos {m} min de grabación")
            notify("⚠ Queda poco disco", f"Caben unos {int(m)} min más de grabación. Libera espacio o la grabación "
                   "se cortará.")

    def _guardar_timeline(self):
        try:
            tl = self.timeline()
            if tl and self.path and os.path.isfile(self.path):
                voices.write_micro(voices.sidecar_path(self.path), tl)
        except Exception:
            pass
        finally:
            self.tl_busy = False

    def sin_estado_s(self):
        """Segundos sin estado de la grabadora (manda uno cada 0,25 s): con un disco de red colgado, por ejemplo, la
        escritura se bloquea y no avisa de nada."""
        return round(time.monotonic() - (self.visto or time.monotonic()), 1)

    def motivo(self):
        """Por que termino la grabadora (para la interfaz): su linea «fatal», la ultima de recorder.log que dice algo
        de un fallo, o que se cerro de golpe."""
        return (self.fatal or ultima_linea(self.log_path, self.log_off)
                or "la grabadora se ha cerrado de golpe, sin dejar motivo")

    def note_levels(self, st):
        if not self.path or not isinstance(st, dict) or st.get("t") is None:
            return
        bits, i = st.get("tu"), st.get("tu_i")
        if isinstance(bits, str) and bits and isinstance(i, int) and 0 <= i < 10 ** 7 and set(bits) <= {"0", "1"}:
            if self.tu is None:
                self.tu = bytearray()
            if len(self.tu) < i:  # una linea perdida: lo que falta, sin voz
                self.tu.extend(b"0" * (i - len(self.tu)))
            self.tu[i:i + len(bits)] = bits.encode("ascii")
        mic = (st.get("mic") or {}).get("db") if isinstance(st.get("mic"), dict) else None
        if mic is None or st.get("mic_muted"):
            return
        sysdb = (st.get("sys") or {}).get("db") if isinstance(st.get("sys"), dict) else None
        self.levels.append((st["t"], mic, sysdb))

    def timeline(self):
        """{"paso", "activo"} de la grabacion (None si no se grabo el micro). Con la linea de la grabadora (desde el
        15/11/2026), esa: sale de la decision bloque a bloque de su nivelador (su voz y no el eco, con la puerta de eco)
        y va con el archivo («desfase» 0). Si no (una grabadora anterior), la de los niveles, como antes."""
        if self.tu:
            return {"paso": PASO_TU, "activo": self.tu.decode("ascii"), "desfase": 0.0, "fuente": "grabadora"}
        return voices.mic_timeline(self.levels[:]) if self.levels else None

    def alive(self):
        return not self.ended and self.p.poll() is None

    def send(self, cmd):
        try:
            self.p.stdin.write((cmd + "\n").encode())
            self.p.stdin.flush()
            return True
        except OSError:
            return False

    def stop(self, timeout=10):
        with self.end_lock:
            self.stopping = True  # lo que termine a partir de aqui es una parada pedida, no un corte
        try:
            self.p.stdin.write(b"stop\n")
            self.p.stdin.flush()
        except OSError:
            pass
        end = time.time() + timeout
        while time.time() < end and not self.ended:
            time.sleep(0.05)
        if not self.ended:
            self.p.kill()
            time.sleep(0.3)
            if self.path:
                close_wav(self.path)
        return self.status.get("t", 0)


class Cortes:
    """Grabaciones que se cortaron sin que nadie las parase (la grabadora termino sola) y las que se recuperaron al
    arrancar (Tenca se cerro de golpe: un corte de luz, un cuelgue, Windows Update). Lo que la interfaz avisa
    (estado()) y la lista de los WAV propios que estan abiertos (abiertas.json en la carpeta de datos): al arrancar
    solo se reparan esos, y solo si ya no crecen; nunca un WAV de otro programa (OBS) que siga grabando."""

    MAX_REINTENTOS = 3   # continuaciones automaticas por grabacion (que un fallo que se repite no entre en bucle)
    REINTENTO_S = 2.0    # lo que se espera antes de seguir en la continuacion
    GUARDA_S = 12 * 3600  # lo que la interfaz sigue viendo un corte (hasta que lo cierre)
    QUIETO_S = 3.0       # al arrancar: un WAV que no crece en este tiempo ya no lo escribe nadie

    def __init__(self, data_dir):
        self.file = os.path.join(data_dir, "grabaciones_abiertas.json")
        self.lock, self.lista, self.n = threading.Lock(), [], 0

    def _leer(self):
        try:
            with open(self.file, encoding="utf-8") as f:
                d = json.load(f)
            return [str(p) for p in d if isinstance(p, str)] if isinstance(d, list) else []
        except (OSError, ValueError):
            return []

    def _cambiar_abiertas(self, fn):
        with self.lock:
            antes = self._leer()
            ahora = fn(list(antes))
            if ahora == antes:
                return
            try:
                if ahora:
                    tmp = self.file + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(ahora, f, ensure_ascii=False)
                    os.replace(tmp, self.file)
                elif os.path.exists(self.file):
                    os.remove(self.file)
            except OSError as e:
                log(f"   ✖ no se pudo apuntar la grabación abierta: {e}")

    def abierta(self, path):
        self._cambiar_abiertas(lambda ps: ps if path in ps else ps + [path])

    def cerrada(self, path):
        self._cambiar_abiertas(lambda ps: [p for p in ps if p != path])

    def apuntar(self, **kw):
        with self.lock:
            self.n += 1
            e = dict(kw, id=f"{int(time.time() * 1000)}-{self.n}", at=time.time())
            self.lista = [x for x in self.lista if time.time() - x["at"] < self.GUARDA_S][-19:] + [e]
            return e

    def cambiar(self, id_, **kw):
        with self.lock:
            for x in self.lista:
                if x["id"] == id_:
                    x.update(kw)

    def estado(self):
        with self.lock:
            return [dict(x) for x in self.lista if time.time() - x["at"] < self.GUARDA_S]

    def recuperar(self, al_recuperar, esperar=None):
        """Al arrancar: lo que sigue apuntado es una grabacion que Tenca no vio terminar (se cerro grabando: un corte
        de luz, un cuelgue, la mataron, Windows se apago). Cierra la cabecera de las que quedaron abiertas y ya no
        crecen, y llama a al_recuperar(ruta, mtime) por cada una, tambien por las que la grabadora llego a cerrar sola
        (al cerrarse Tenca, la grabadora para y cierra el WAV: el corte hay que decirlo igual). Las que siguen
        creciendo no se tocan (se miran otra vez al arrancar)."""
        cand, hechos = [], []
        for p in self._leer():
            info = live.wav_info(p) if os.path.isfile(p) else None
            if not info:
                self.cerrada(p)  # ya no existe o no es un WAV
                continue
            if info.get("data_size") is not None:  # la cerro la grabadora: no hay que tocarla, solo decirlo
                self.cerrada(p)
                hechos.append(p)
                try:
                    al_recuperar(p, os.path.getmtime(p))
                except OSError:
                    pass
                continue
            try:
                cand.append((p, os.path.getsize(p), os.path.getmtime(p)))
            except OSError:
                continue
        if not cand:
            return hechos
        time.sleep(self.QUIETO_S if esperar is None else esperar)
        for p, size, mtime in cand:
            try:
                if os.path.getsize(p) != size or os.path.getmtime(p) != mtime:
                    log(f"   la grabación abierta {os.path.basename(p)} sigue creciendo: no se toca")
                    continue
                close_wav(p)
            except OSError as e:
                log(f"   ✖ no se pudo cerrar la grabación {os.path.basename(p)}: {e}")
                continue
            self.cerrada(p)
            hechos.append(p)
            al_recuperar(p, mtime)
        return hechos


# ----------------------------------------------------------------------------- reunion en curso
# --- deteccion de reunion (como Notion): Teams/Zoom/… usando el micro y sin grabar
MEETING_APPS = (("msteams", "Teams"), ("teams.exe", "Teams"), ("zoom", "Zoom"), ("webex", "Webex"),
                ("slack", "Slack"), ("skype", "Skype"), ("chrome.exe", "el navegador"),
                ("msedge.exe", "el navegador"), ("firefox.exe", "el navegador"))


# El registro dice "en uso" cuando LastUsedTimeStart tiene valor y LastUsedTimeStop es 0. Esa pareja se queda asi si
# la app se cerro mal (Windows no apunta el fin): el 02/11/2026 el aviso flotante y el chip «en llamada» salieron al
# arrancar un lunes sin ninguna reunion. Por eso una entrada solo cuenta si empezo hace poco (MIC_MAX_AGE_S) y, cuando
# se sabe que .exe es, si ese proceso sigue en marcha.
MIC_MAX_AGE_S = 12 * 3600
# entradas empaquetadas (sin ruta en el nombre de la clave): los .exe que las usan
PACKAGED_EXES = {"msteams": ("ms-teams.exe", "teams.exe", "msteams.exe"), "skype": ("skype.exe", "skypeapp.exe"),
                 "zoom": ("zoom.exe",), "webex": ("webex.exe", "ciscocollabhost.exe", "atmgr.exe"),
                 "slack": ("slack.exe",)}
_mic_logged = {}  # entrada -> ultimo estado contado ("uso", "rancia", "cerrada"): cada cambio se registra una vez


def filetime_age(ft, now=None):
    """Segundos desde un FILETIME del registro (centenas de ns desde 1601) hasta ahora; None si el valor no vale."""
    try:
        secs = int(ft) / 10_000_000 - 11644473600
    except (TypeError, ValueError, OverflowError):
        return None
    return (time.time() if now is None else now) - secs


def mic_in_use(start, stop, now=None, max_age=MIC_MAX_AGE_S):
    """La entrada dice "en uso" (empezo y no termino) y empezo hace menos de max_age (y no en el futuro)."""
    if not start or stop:
        return False
    age = filetime_age(start, now)
    return age is not None and -300 <= age <= max_age


def entry_exes(name):
    """Los .exe que corresponden a una entrada del registro: la ruta del .exe (NonPackaged, con '#' por '\\') o, en las
    empaquetadas, los de PACKAGED_EXES. () si no se sabe (entonces no se comprueba el proceso)."""
    if "#" in name:
        return (name.rsplit("#", 1)[-1].lower(),)
    low = name.lower()
    return next((exes for k, exes in PACKAGED_EXES.items() if k in low), ())


_IMAGES = {"at": -1e9, "set": None}
IMAGES_CACHE_S = 3.0  # el vigilante de llamadas y el del fin de la llamada leen en el mismo paso: un tasklist, no dos


def running_images():
    """Nombres (en minusculas) de los procesos en marcha, segun tasklist; None si no se sabe (fuera de Windows)."""
    if not WINDOWS:
        return None
    if time.monotonic() - _IMAGES["at"] < IMAGES_CACHE_S and _IMAGES["set"] is not None:
        return _IMAGES["set"]
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10,
                           creationflags=engines.NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    out = {line.split('","')[0].strip('"').lower() for line in r.stdout.splitlines() if line.startswith('"')}
    _IMAGES.update(at=time.monotonic(), set=out)
    return out


def _mic_note(name, estado, texto):
    if _mic_logged.get(name) != estado:
        _mic_logged[name] = estado
        log(texto)


def mic_scan(entries, now=None, images=None):
    """App de reuniones con el micro abierto entre las entradas (nombre, LastUsedTimeStart, LastUsedTimeStop) del
    registro, o None. `images` son los procesos en marcha (running_images(); se consultan solo si hace falta)."""
    for name, start, stop in entries:
        low = name.lower()
        app = next((label for k, label in MEETING_APPS if k in low), None)
        if not app:
            continue
        if not start or stop:
            _mic_logged.pop(name, None)
            continue
        age = filetime_age(start, now)
        if not mic_in_use(start, stop, now):
            _mic_note(name, "rancia", f"   micro: {app} ({name}) figura en uso desde hace "
                      f"{(age or 0) / 3600:.1f} h sin cerrar: entrada rancia del registro, no cuenta")
            continue
        exes = entry_exes(name)
        if exes:
            if images is None:
                images = running_images()
            if images is not None and not any(e in images for e in exes):
                _mic_note(name, "cerrada", f"   micro: {app} ({name}) figura en uso pero su proceso no está en marcha: "
                          "no cuenta")
                continue
        _mic_note(name, "uso", f"   micro en uso por {app} ({name}) desde hace {(age or 0) / 60:.0f} min")
        return app
    return None


def mic_entries():
    """(nombre, LastUsedTimeStart, LastUsedTimeStop) de las entradas del registro de privacidad del micro."""
    import winreg
    base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
    out = []
    for sub in (base, base + r"\NonPackaged"):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(key, name) as sk:
                        start = winreg.QueryValueEx(sk, "LastUsedTimeStart")[0]
                        stop = winreg.QueryValueEx(sk, "LastUsedTimeStop")[0]
                except OSError:
                    continue
                out.append((name, start, stop))
    return out


def mic_meeting_app():
    """App de reuniones que tiene ahora mismo el micro abierto (segun el registro de privacidad), o None."""
    if not WINDOWS:
        return None
    try:
        return mic_scan(mic_entries())
    except Exception:
        return None


def teams_mic(entries, now=None, images=None):
    """El micro de Teams segun las entradas del registro (nombre, LastUsedTimeStart, LastUsedTimeStop), para saber si
    la llamada que se graba ha terminado (directo_avisos.FinDeLlamada):
      "uso"   alguna entrada de Teams lo tiene abierto (empezo hace menos de MIC_MAX_AGE_S y Teams esta en marcha);
      "libre" ninguna lo tiene: lo soltaron (con su hora de fin), nunca lo usaron, o figura abierto pero Teams ya no
              esta en marcha (se cerro Teams: «cuando se cierra el Teams o la reunion», msg 1479; Windows no siempre
              apunta el fin si el proceso se va de golpe, y sin Teams no hay llamada en este PC);
      "duda"  figura abierto y no se sabe que procesos hay (tasklist fallo).
    Una entrada abierta de hace mas de MIC_MAX_AGE_S es rancia y no cuenta (como en mic_scan)."""
    estado = "libre"
    for name, start, stop in entries:
        low = name.lower()
        if not any(k in low for k in ("msteams", "teams.exe")) or not start or stop:
            continue
        if not mic_in_use(start, stop, now):
            continue
        if images is None:
            images = running_images()
        if images is None:
            estado = "duda"
            continue
        if any(e in images for e in entry_exes(name)):
            return "uso"
    return estado


def teams_mic_estado():
    """teams_mic con el registro de este PC; "duda" si no se puede leer (o fuera de Windows: nunca se para nada)."""
    if not WINDOWS:
        return "duda"
    try:
        return teams_mic(mic_entries())
    except Exception:
        return "duda"
