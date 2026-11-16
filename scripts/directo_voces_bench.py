# -*- coding: utf-8 -*-
"""¿«Quién habla» mientras se graba (punto 56, voces_directo.py) hace más lento el directo en la nube?

    python scripts/directo_voces_bench.py --habla es.wav --voces reunion_larga.wav --data DATA_DIR [--rondas 3]

Mismo audio, en tiempo real, rondas alternas: «sin» (solo el directo) y «con» (el directo y, a la vez, la
identificación de un bloque de 30 min tal como la lanza la app: voices.py --rejilla --hasta 1800 --threads 1, en
reposo: nice 19 fuera de Windows, IDLE_PRIORITY_CLASS en Windows; se relanza si acaba antes que la ronda, con la caché
vacía cada vez para que de verdad diarice).

El directo es live.run de verdad sobre un WAV que crece en tiempo real (como la grabadora), con su VAD (Silero de
faster-whisper) y su remuestreo; la nube es una API falsa que tarda --latencia segundos por trozo (sin red: lo que se
mide es lo que cambia en este PC). Latencia = la del propio directo (live.Latency): del final del audio de cada trozo,
cuando ya estaba en el fichero, al texto escrito. La pantalla añade su sondeo (igual en las dos).

--carga N: N procesos más con un núcleo cada uno ocupado, en prioridad normal, en las dos (un PC con la llamada y
otros programas): así se ve si el subproceso en reposo le quita algo al directo cuando la CPU no sobra.

--habla: un WAV con voz (scripts/directo_bench.py synth lo hace con `say`); se repite hasta --minutos.
--voces: un audio de al menos 30 min (el bloque que se identifica). --data: una carpeta con los modelos de voces en
models/voces (los de la app). Necesita numpy, PyAV, faster-whisper y sherpa-onnx (los de la app).
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "_app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

import engines  # noqa: E402
import live  # noqa: E402
from directo_bench import Job, Recorder, q, read_wav16  # noqa: E402

SR = live.SR


class LatenciaEspia(live.Latency):
    """live.Latency con sus muestras a mano (el log las redondea a 0,1 s)."""
    ultimas = []

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        LatenciaEspia.ultimas.append(self)


live.Latency = LatenciaEspia


class NubeFalsa:
    """La API de la nube, sin red: tarda `latencia` segundos de verdad por trozo y devuelve un texto."""

    def __init__(self, latencia):
        self.latencia, self.calls = latencia, []
        self.keys = engines.KeyPool()

    def transcribe_array(self, cfg, audio, language, prompt, tmp_dir, job_id, stage, cancelled, context=None,
                         model=None, about=None, timing=None):
        secs = len(audio) / SR
        self.calls.append(secs)
        time.sleep(self.latencia)
        return [(0.0, secs, f"trozo {len(self.calls)} ({secs:.1f} s)")], "spanish"


class Voces:
    """La identificación de un bloque, como voces_directo: en reposo, 1 hilo, relanzada mientras dure la ronda."""

    def __init__(self, audio, data, hilos=1):
        self.audio, self.data, self.hilos = audio, data, hilos
        self.parar, self.vueltas, self.fallos = False, 0, 0
        self.hilo = threading.Thread(target=self._bucle, daemon=True)

    def _bucle(self):
        while not self.parar:
            cache = tempfile.mkdtemp(prefix="voces-bloques-")
            out = os.path.join(cache, "parcial.json")
            nice = ["nice", "-n", "19"] if os.name != "nt" and shutil.which("nice") else []
            flags = getattr(subprocess, "IDLE_PRIORITY_CLASS", 0)
            self.p = subprocess.Popen([*nice, sys.executable, "-u", os.path.join(APP, "voices.py"), "--diarize",
                                       self.audio, "--out", out, "--rejilla", "--cache", cache, "--hasta", "1800",
                                       "--data", self.data, "--threads", str(self.hilos)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
            while self.p.poll() is None and not self.parar:
                time.sleep(0.2)
            if self.p.poll() is None:
                self.p.kill()
                self.p.wait()
            elif self.p.returncode == 0:
                self.vueltas += 1
            else:
                self.fallos += 1
            shutil.rmtree(cache, ignore_errors=True)

    def empezar(self):
        self.hilo.start()
        time.sleep(3)  # que ya esté diarizando (cargar el audio y los modelos) cuando empieza el directo

    def terminar(self):
        self.parar = True
        self.hilo.join(30)


def ronda(y, latencia, con_voces, args):
    voces = Voces(args.voces, args.data) if con_voces else None
    if voces:
        voces.empezar()
    tmp = tempfile.mkdtemp(prefix="directo-voces-")
    grow = os.path.join(tmp, "grabacion.wav")
    Recorder(grow, y, real=True)  # el WAV que crece en tiempo real (un hilo suyo)
    job, nube, logs = Job(), NubeFalsa(latencia), []
    cfg = {"provider": "groq", "api_key": "clave-de-mentira"}
    opts = {"preset": "nube", "language": None, "prompt": None, "threads": 1, "worker": None, "cloud": nube,
            "cloud_cfg": cfg, "tmp_dir": tmp, "resume": []}
    t0 = time.time()
    hijos0 = _cpu_hijos()
    try:
        live.run(job, grow, opts, lambda s: None, logs.append)
    finally:
        if voces:
            voces.terminar()
        shutil.rmtree(tmp, ignore_errors=True)
    lats = list(LatenciaEspia.ultimas[-1].samples)
    return {"lats": lats, "trozos": len(nube.calls), "dur": time.time() - t0,
            "voces": (voces.vueltas, voces.fallos, round(_cpu_hijos() - hijos0, 1)) if voces else None,
            "carga": os.getloadavg()[0] if hasattr(os, "getloadavg") else None}


def _cpu_hijos():
    try:
        import resource
        r = resource.getrusage(resource.RUSAGE_CHILDREN)
        return r.ru_utime + r.ru_stime
    except (ImportError, OSError):
        return 0.0


def fmt(x):
    return "—" if x != x else f"{x:.2f}".replace(".", ",")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--habla", required=True)
    ap.add_argument("--voces", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--minutos", type=float, default=3.0)
    ap.add_argument("--rondas", type=int, default=3)
    ap.add_argument("--latencia", type=float, default=0.4)
    ap.add_argument("--carga", type=int, default=0)
    a = ap.parse_args()
    quemadores = [subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(a.carga)]
    y0 = read_wav16(a.habla)
    y = np.tile(y0, int(np.ceil(a.minutos * 60 * SR / len(y0))))[:int(a.minutos * 60 * SR)]
    todos = {"sin": [], "con": []}
    try:
        _rondas(a, y, todos)
    finally:
        for p in quemadores:
            p.kill()
    for modo in ("sin", "con"):
        xs = todos[modo]
        print(f"TOTAL {modo}: p50 {fmt(q(xs, .5))} s · p95 {fmt(q(xs, .95))} s · media {fmt(sum(xs) / len(xs))} s · "
              f"n {len(xs)}" if xs else f"TOTAL {modo}: sin datos")
    return 0


def _rondas(a, y, todos):
    for i in range(a.rondas):
        for modo in ("sin", "con"):
            r = ronda(y, a.latencia, modo == "con", a)
            todos[modo] += r["lats"]
            print(f"ronda {i + 1} {modo}: latencia p50 {fmt(q(r['lats'], .5))} s · p95 {fmt(q(r['lats'], .95))} s · "
                  f"max {fmt(max(r['lats']) if r['lats'] else float('nan'))} s · n {len(r['lats'])} · "
                  f"trozos {r['trozos']} · {r['dur']:.0f} s"
                  + (f" · voces: {r['voces'][0]} bloques terminados, {r['voces'][1]} fallos, {r['voces'][2]} s de CPU"
                     if r["voces"] else "")
                  + (f" · carga {r['carga']:.1f}" if r["carga"] is not None else ""), flush=True)


if __name__ == "__main__":
    sys.exit(main())
