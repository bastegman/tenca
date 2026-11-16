"""Lo ocupado que va el PC, para el trabajo de fondo (rehacer las huellas de voz, precargar un modelo): que ceda si
el PC va justo y no se sume a lo que ya lo carga (incidencia del 16/11/2026: un portatil modesto «no responde» en el
primer arranque tras actualizar).

- memoria(): (total, disponible) en bytes de todo el PC (GlobalMemoryStatusEx).
- cpu(muestra): la parte de la CPU de todo el PC ocupada durante `muestra` segundos (GetSystemTimes), de 0 a 1.
- ocupado(): "memoria" si queda menos de MIN_LIBRE libre, "cpu" si la CPU va por encima de CPU_MAX, si no None.

Fuera de Windows (desarrollo y CI de Linux/Mac) no se mide: memoria() y cpu() devuelven None y ocupado() nunca dice
que si. Asi el trabajo de fondo funciona como antes donde no se sabe, y en Windows cede de verdad. Solo numeros: nada
de nombres de procesos ni rutas."""
import ctypes
import os
import time

WINDOWS = os.name == "nt"
GB = 1 << 30
# menos de esto libre: el PC ya tira del disco como memoria (o va a hacerlo) y un proceso mas lo congela. El de
# rehacer una huella ocupa ~0,26 GB (medido el 16/11/2026, Mac, CAM++); la precarga de un modelo de Whisper, 0,5-1,6
MIN_LIBRE = 1 * GB
CPU_MAX = 0.6  # mas del 60 % de la CPU de todo el PC durante la muestra: hay algo trabajando (una llamada, otra app)
MUESTRA_S = 3.0


if WINDOWS:
    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


def memoria():
    """(total, disponible) en bytes, o None si no se sabe (fuera de Windows o si Windows no contesta)."""
    if not WINDOWS:
        return None
    try:
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        return int(st.ullTotalPhys), int(st.ullAvailPhys)
    except Exception:
        return None


def _tiempos():
    """(inactivo, total) de todas las CPU en unidades de 100 ns desde el arranque, o None. El tiempo de kernel que da
    Windows ya incluye el inactivo."""
    if not WINDOWS:
        return None
    try:
        idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None

        def v(ft):
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        return v(idle), v(kernel) + v(user)
    except Exception:
        return None


def cpu(muestra=MUESTRA_S, dormir=time.sleep):
    """La parte ocupada de la CPU de todo el PC durante `muestra` segundos (0-1), o None si no se sabe. Duerme ese
    tiempo en el hilo que la llama: solo para hilos de fondo."""
    a = _tiempos()
    if a is None:
        return None
    dormir(muestra)
    b = _tiempos()
    if b is None:
        return None
    total = b[1] - a[1]
    if total <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - (b[0] - a[0]) / total))


def poca_memoria(min_libre=MIN_LIBRE):
    """True si queda menos de `min_libre` de memoria libre en el PC (sin esperar: se puede mirar a menudo)."""
    m = memoria()
    return bool(m and m[1] < min_libre)


def ocupado(min_libre=MIN_LIBRE, cpu_max=CPU_MAX, muestra=MUESTRA_S):
    """"memoria", "cpu" o None. Primero la memoria (no espera); la CPU, con una muestra de `muestra` segundos."""
    if poca_memoria(min_libre):
        return "memoria"
    c = cpu(muestra)
    if c is not None and c > cpu_max:
        return "cpu"
    return None


def diagnostico():
    """La linea del diagnostico: hilos de CPU y memoria (solo numeros)."""
    m = memoria()
    mem = (f"memoria {m[0] / GB:.1f} GB, libre {m[1] / GB:.1f} GB".replace(".", ",") if m else "memoria: sin datos")
    return f"PC: {os.cpu_count() or 0} hilos de CPU · {mem}"
