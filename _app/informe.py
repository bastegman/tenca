# -*- coding: utf-8 -*-
"""Informe tecnico por reunion, solo numeros (punto 32 del review del 14/11/2026).

Varias investigaciones se quedaron sin cerrar por falta de datos de la reunion que fue mal («la nube va lenta»):
las medidas estaban en el log y en el estado del directo mientras duraba. Ahora cada reunion deja, junto al audio,
<audio>.informe.json (va con ella al moverla o borrarla: biblioteca.SIDE_EXTS), con una seccion por fase:
- grabacion (grabadora propia): segundos, audio perdido por fuente, eco (retardo, atenuacion, tiempo atenuado);
- directo: tramos de motor y modelo (nube o PC), trozos, fallidos y segundos perdidos, latencia p50/p95 y por fases,
  espera por cupo, caidas a local, cupo al acabar y memoria pico;
- transcripcion (la de la cola) o repaso (el final, en la nube): motor, modelo, segundos de audio, lo que tardo y la
  memoria pico.

Solo numeros y nombres tecnicos: ni texto de la reunion, ni su nombre, ni rutas, ni nombres de dispositivos; un modelo
o un proveedor que no sea uno conocido (uno escrito a mano para una pasarela propia) va como «otro». Una copia de los
GUARDADOS ultimos (sin la ruta) queda en DATA_DIR/informes.json, de donde «Copiar diagnostico» saca los 3 ultimos.
Nada de esto puede romper una grabacion ni un trabajo: todo lo que falle aqui se calla."""
import ctypes
import json
import math
import os
import re
import sys
import threading
import time
import uuid

import engines
from common import WINDOWS, write_bytes
from diagnostico import DIAG_MODELS
from rutas import DATA_DIR, VERSION

SUFIJO = ".informe.json"
INDICE = "informes.json"
GUARDADOS = 10
FORMATO = 1
TRAMOS_MAX = 40
ESTADOS = ("done", "error", "cancelled")
_LOCK = threading.Lock()


def _num(v, nd=2):
    """Un numero finito redondeado, o None (nada que no sea un numero)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return round(float(v), nd) if nd else int(round(v))


def _modelo(m):
    return m if isinstance(m, str) and m in DIAG_MODELS else "otro"


def _proveedor(cfg):
    p = (cfg or {}).get("provider") or "groq"
    return p if isinstance(p, str) and p in engines.PROVIDERS else "otro"


def _motor_de(preset, cfg, model=None):
    """(motor, proveedor, modelo) de un preset: «nube» con su proveedor y modelo, o «pc» con el de Whisper."""
    p = engines.PRESETS_BY_ID.get(preset) or {}
    if p.get("engine") == "cloud":
        return "nube", _proveedor(cfg), _modelo(model or (cfg or {}).get("model"))
    return "pc", None, _modelo(p.get("model"))


# ----------------------------------------------------------------------------- lo que apunta el directo


class ApuntesDirecto:
    """Lo que va pasando en un directo: lo apunta live.run por trozo (trozo, pausa) y Jobs al caer a local."""

    def __init__(self):
        self.lock = threading.Lock()
        self.tramos, self.pausa_s, self.caidas_a_local = [], 0.0, 0

    def trozo(self, motor, proveedor, modelo, desde_s, dur_s, ok=True):
        with self.lock:
            t = self.tramos[-1] if self.tramos else None
            if t is None or (t["motor"], t["proveedor"], t["modelo"]) != (motor, proveedor, modelo):
                if len(self.tramos) >= TRAMOS_MAX:  # un directo que cambia de modelo sin parar: los primeros
                    return
                t = {"motor": motor, "proveedor": proveedor, "modelo": modelo, "desde_s": desde_s,
                     "hasta_s": desde_s, "trozos": 0, "fallidos": 0, "perdidos_s": 0.0}
                self.tramos.append(t)
            t["desde_s"] = min(t["desde_s"], desde_s)
            t["hasta_s"] = max(t["hasta_s"], desde_s + dur_s)
            t["trozos"] += 1
            if not ok:
                t["fallidos"] += 1
                t["perdidos_s"] += dur_s

    def pausa(self, s):
        with self.lock:
            self.pausa_s += s

    def tramos_publicos(self):
        with self.lock:
            return [dict(t, desde_s=_num(t["desde_s"], 1), hasta_s=_num(t["hasta_s"], 1),
                         perdidos_s=_num(t["perdidos_s"], 1)) for t in self.tramos]


def trozo(job, cloud, cfg, desde_s, dur_s, ok=True):
    """live.run, por cada trozo que manda a transcribir: con que motor y modelo, de donde a donde y si salio."""
    inf = getattr(job, "informe", None)
    if inf is None:
        return
    try:
        if cloud:
            motor = ("nube", _proveedor(cfg), _modelo(engines.CloudEngine.live_model(cfg)))
        else:
            motor = _motor_de(getattr(job, "preset", None), cfg)
        inf.trozo(*motor, float(desde_s), float(dur_s), bool(ok))
    except Exception:  # noqa: BLE001
        pass


def pausa(job, s):
    """live.run, en pausa por falta de cupo (cada vuelta, lo que espera)."""
    inf = getattr(job, "informe", None)
    if inf is not None:
        try:
            inf.pausa(float(s))
        except Exception:  # noqa: BLE001
            pass


def caida_a_local(job):
    """Jobs: el directo pasa a este PC sin que nadie lo pida (la nube no estaba lista o se ha apagado en Ajustes)."""
    inf = getattr(job, "informe", None)
    if inf is not None:
        with inf.lock:
            inf.caidas_a_local += 1


# ----------------------------------------------------------------------------- memoria pico


def _pico_windows(pid=None):
    class PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
            (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                           "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                           "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_uint32]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.GetCurrentProcess() if pid is None else k32.OpenProcess(0x1000, 0, pid)  # QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        c = PMC()
        c.cb = ctypes.sizeof(c)
        if not k32.K32GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return None
        return c.PeakWorkingSetSize / 2 ** 20
    finally:
        if pid is not None:
            k32.CloseHandle(h)


def pico_mb(pid=None):
    """Memoria pico (working set) del proceso, en MB, desde que arranco: este (pid None) u otro vivo. None si no se
    puede saber (fuera de Windows, de otro proceso solo en Linux)."""
    try:
        if WINDOWS:
            v = _pico_windows(pid)
        elif pid is None:
            import resource
            r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            v = r / 2 ** 20 if sys.platform == "darwin" else r / 1024  # bytes en macOS, KiB en Linux
        else:
            with open(f"/proc/{int(pid)}/status", encoding="ascii") as f:
                v = next((int(l.split()[1]) / 1024 for l in f if l.startswith("VmHWM:")), None)
        return _num(v, 0)
    except Exception:  # noqa: BLE001
        return None


def memoria(jobs=None):
    """{app, whisper_directo, whisper_cola}: el pico de cada proceso desde que arranco (MB; None si no corre o no se
    sabe). El de Whisper se reinicia al recargar el modelo (worker_keepalive_min)."""
    out = {"app": pico_mb()}
    for nombre, w in (("whisper_directo", getattr(jobs, "live_worker", None)),
                      ("whisper_cola", getattr(getattr(jobs, "local", None), "worker", None))):
        p = getattr(w, "p", None)
        try:
            vivo = p is not None and p.poll() is None
        except Exception:  # noqa: BLE001
            vivo = False
        out[nombre] = pico_mb(p.pid) if vivo else None
    return out


# ----------------------------------------------------------------------------- secciones


def _callado(f):
    """Una seccion que falla al montarse no rompe a quien la pide (parar la grabacion, acabar un trabajo): None."""
    def g(*a, **k):
        try:
            return f(*a, **k)
        except Exception:  # noqa: BLE001
            return None
    g.__name__, g.__doc__ = f.__name__, f.__doc__
    return g


@_callado
def grabacion(abs_path, status, segundos=None):
    """Al parar la grabadora propia (rec_stop): segundos, audio perdido por llegar tarde por fuente y el eco medido.
    Del estado de la grabadora solo los numeros: no el nombre de los dispositivos."""
    st = status if isinstance(status, dict) else {}

    def eco(x):
        x = x if isinstance(x, dict) else {}
        return {"activa": bool(x.get("activa")), "retardo_ms": _num(x.get("retardo_ms"), 0),
                "atenuacion_db": _num(x.get("atenuacion_db"), 1), "atenuado_s": _num(x.get("bajado_s"), 1)}
    eco_st = st.get("eco") if isinstance(st.get("eco"), dict) else None
    sec = {"segundos": _num(segundos if segundos is not None else st.get("t"), 1),
           "perdido_s": {k: _num((st.get(k) or {}).get("lost"), 2) for k in ("mic", "sys") if isinstance(st.get(k),
                                                                                                         dict)},
           "pendiente_s": _num(st.get("pendiente_s"), 1),
           "eco": {"micro": eco(eco_st.get("micro")), "salida": eco(eco_st.get("salida"))} if eco_st else None}
    return anotar(abs_path, "grabacion", sec)


@_callado
def directo(job, abs_path, jobs=None):
    """Al terminar un directo (Jobs._run_live): lo que apunto ApuntesDirecto y lo ultimo que publico live.run
    (job.directo)."""
    inf = getattr(job, "informe", None)
    d = getattr(job, "directo", None) or {}
    lat = d.get("latency") if isinstance(d.get("latency"), dict) else {}
    tramos = inf.tramos_publicos() if inf is not None else []
    q = d.get("quota") if isinstance(d.get("quota"), dict) else None
    sec = {"estado": job.status if job.status in ESTADOS else "otro",
           "grabado_s": _num(getattr(job, "recorded", None), 1),
           "tramos": tramos,
           "trozos": sum(t["trozos"] for t in tramos), "fallidos": sum(t["fallidos"] for t in tramos),
           "perdidos_s": _num(sum(t["perdidos_s"] or 0 for t in tramos), 1),
           "pausa_cupo_s": _num(inf.pausa_s, 0) if inf is not None else None,
           "caidas_a_local": inf.caidas_a_local if inf is not None else None,
           "latencia_s": {"n": _num(lat.get("n"), 0), "p50": _num(lat.get("p50")), "p95": _num(lat.get("p95")),
                          "fases": {k: {"p50": _num(v.get("p50")), "p95": _num(v.get("p95"))}
                                    for k, v in (lat.get("phases") or {}).items()
                                    if isinstance(k, str) and k.isalpha() and isinstance(v, dict)}},
           "cupo": {k: _num(q.get(k), 1) for k in ("keys", "invalid", "remaining", "limit", "used", "blocked_for")}
           if q else None,
           "memoria_mb": memoria(jobs)}
    return anotar(abs_path, "directo", sec)


@_callado
def trabajo(job, abs_path, jobs=None, cfg=None):
    """Al terminar una transcripcion de la cola o el repaso final (Jobs._worker): motor, modelo, audio y tiempos."""
    if getattr(job, "kind", None) != "transcribe" or getattr(job, "live", False):
        return None
    motor, prov, modelo = _motor_de(job.preset, cfg)
    inicio, fin = getattr(job, "started", None), getattr(job, "finished", None)
    tardo = (fin - inicio) if isinstance(inicio, (int, float)) and isinstance(fin, (int, float)) else None
    audio = getattr(job, "duration", None)
    sec = {"estado": job.status if job.status in ESTADOS else "otro", "motor": motor, "proveedor": prov,
           "modelo": modelo, "audio_s": _num(audio, 1), "tardo_s": _num(tardo, 1),
           "x_tiempo_real": _num(audio / tardo, 1) if audio and tardo else None, "memoria_mb": memoria(jobs)}
    return anotar(abs_path, "repaso" if getattr(job, "final_pass", False) else "transcripcion", sec)


# ----------------------------------------------------------------------------- ficheros


def ruta_de(abs_path):
    return os.path.splitext(abs_path)[0] + SUFIJO


def leer(abs_path, estricto=False):
    """El informe de esa reunion ({} si no hay o no se puede leer). Con estricto (para escribir encima), un fichero que
    existe pero no se deja leer (OneDrive o el antivirus lo tienen un instante) se reintenta unos 2 s y, si sigue sin
    poder, OSError: mejor no anotar esta seccion que pisar las que ya tenia."""
    for intento in range(20 if estricto else 1):
        try:
            with open(ruta_de(abs_path), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except FileNotFoundError:
            return {}
        except ValueError:  # roto: se rehace
            return {}
        except OSError:
            if not estricto:
                return {}
            if intento == 19:
                raise
            time.sleep(0.1)
    return {}


def anotar(abs_path, seccion, datos):
    """Pone (o cambia) una seccion del informe de la reunion y su copia en DATA_DIR/informes.json. Devuelve el informe
    o None si no se pudo (nada aqui lanza)."""
    try:
        if not os.path.isfile(abs_path):  # la reunion ya no esta ahi: sin informe suelto
            return None
        with _LOCK:
            d = leer(abs_path, estricto=True)
            if d.get("formato") != FORMATO or not isinstance(d.get("secciones"), dict):
                d = {"formato": FORMATO, "id": uuid.uuid4().hex[:8], "secciones": {}}
            d.update(app=VERSION, actualizado=time.strftime("%Y-%m-%d %H:%M"))
            d["secciones"][seccion] = datos
            write_bytes(ruta_de(abs_path), json.dumps(d, ensure_ascii=False, indent=1).encode("utf-8"))
            _al_indice(d)
            return d
    except Exception:  # noqa: BLE001
        return None


def _al_indice(d):
    ruta = os.path.join(DATA_DIR, INDICE)
    try:
        with open(ruta, encoding="utf-8") as f:
            lista = json.load(f).get("informes") or []
    except (OSError, ValueError, AttributeError):
        lista = []
    lista = [x for x in lista if isinstance(x, dict) and x.get("id") != d["id"]] + [d]
    lista = sorted(lista, key=lambda x: str(x.get("actualizado") or ""))[-GUARDADOS:]
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"formato": FORMATO, "informes": lista}, f, ensure_ascii=False)
    os.replace(tmp, ruta)


def recientes(n=3):
    """Los n informes mas recientes (de DATA_DIR/informes.json), el ultimo primero."""
    try:
        with open(os.path.join(DATA_DIR, INDICE), encoding="utf-8") as f:
            lista = [x for x in json.load(f).get("informes") or [] if isinstance(x, dict)]
    except (OSError, ValueError, AttributeError):
        return []
    return sorted(lista, key=lambda x: str(x.get("actualizado") or ""), reverse=True)[:n]


_FECHA = re.compile(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?(?: · [0-9a-f]{7,12})?")


def _limpio(x, clave=""):
    """Para el diagnostico, otra vez, por si el fichero no lo escribio la app: numeros, si/no y nulos; de los textos,
    solo los de una lista cerrada (motor, proveedor, modelo conocido, estado) y la fecha y la version. Lo demas, fuera
    (las claves, con forma de nombre tecnico)."""
    if x is None or isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return _num(x, 2)
    if isinstance(x, str):
        ok = (x in ("nube", "pc", "otro") or x in engines.PROVIDERS or x in DIAG_MODELS or x in ESTADOS
              or clave in ("actualizado", "app") and _FECHA.fullmatch(x))
        return x if ok else "<texto>"
    if isinstance(x, list):
        return [_limpio(v, clave) for v in x[:TRAMOS_MAX]]
    if isinstance(x, dict):
        return {k: _limpio(v, k) for k, v in x.items() if isinstance(k, str) and re.fullmatch(r"[a-z0-9_]{1,30}", k)}
    return None


def diagnostico(n=3):
    """Las lineas de «Copiar diagnostico»: los n ultimos informes, uno por linea (JSON compacto)."""
    r = recientes(n)
    if not r:
        return ["Informes técnicos de las últimas reuniones: ninguno todavía"]
    return [f"Informes técnicos de las {len(r)} últimas reuniones (solo números):"] + [
        "  " + json.dumps(_limpio({k: x.get(k) for k in ("actualizado", "app", "secciones")}), ensure_ascii=False,
                          separators=(",", ":")) for x in r]
