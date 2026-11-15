# -*- coding: utf-8 -*-
"""recorder.py de verdad con el soundcard falso y fallos de disco de mentira (para tests/test_grabadora.py; no es un
test). Se lanza como recorder.py, con los mismos argumentos, y estas variables:

  FAKE_APP         la carpeta _app
  FAKE_ESCRITURA   "a-b": entre esos segundos (contados desde que empieza a escribir audio, no desde que arranca: en
                   Windows arrancar puede llevar segundos), escribir mas alla del final del WAV da OSError(ENOSPC),
                   como un disco lleno (reescribir la cabecera, que no necesita sitio nuevo, sigue funcionando);
                   "a-": desde a
  FAKE_RETRY_S     WRITE_RETRY_S (cuanto se reintenta antes de rendirse)
  FAKE_DISCO_MB    lo que dice shutil.disk_usage que queda libre"""
import collections
import errno
import os
import sys
import time

sys.path.insert(0, os.environ["FAKE_APP"])
import recorder  # noqa: E402

if os.environ.get("FAKE_RETRY_S"):
    recorder.WRITE_RETRY_S = float(os.environ["FAKE_RETRY_S"])
if os.environ.get("FAKE_DISCO_MB"):
    _Uso = collections.namedtuple("_Uso", "total used free")
    _libre = float(os.environ["FAKE_DISCO_MB"]) * 1e6
    recorder.shutil.disk_usage = lambda p: _Uso(1e12, 1e12 - _libre, _libre)
_a, _, _b = (os.environ.get("FAKE_ESCRITURA") or "").partition("-")
VENTANA = (float(_a), float(_b) if _b else float("inf")) if _a else None
_open = open


class _Lleno:
    """El fichero del WAV: lo que lo alarga falla dentro de VENTANA."""

    def __init__(self, f):
        self.f, self.t0 = f, None  # t0: cuando empieza a escribir audio (despues de la cabecera)

    def write(self, b):
        if self.t0 is None and self.f.tell() >= 44:
            self.t0 = time.monotonic()
        t = time.monotonic() - self.t0 if self.t0 is not None else -1
        if VENTANA and VENTANA[0] <= t < VENTANA[1] and self.f.tell() >= os.fstat(self.f.fileno()).st_size:
            raise OSError(errno.ENOSPC, "No space left on device")
        return self.f.write(b)

    def __getattr__(self, k):
        return getattr(self.f, k)


def _abrir(path, mode="r", *a, **k):
    f = _open(path, mode, *a, **k)
    return _Lleno(f) if "w" in mode and str(path).endswith(".wav") else f


recorder.open = _abrir
sys.argv = [os.path.join(os.environ["FAKE_APP"], "recorder.py")] + sys.argv[1:]
recorder.main()
