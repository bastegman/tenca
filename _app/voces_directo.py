# -*- coding: utf-8 -*-
"""«Quién habla» mientras se graba, solo con el directo en la nube (punto 56 del review del 14/11/2026). Con el
directo en este PC, Whisper ya ocupa la CPU.

Cada vez que la grabación completa un bloque de 30 min (voices.BLOQUE_S), se identifican las voces de lo grabado hasta
ahí con el mismo subproceso de siempre (voices.py), en bloques fijos (--rejilla) y guardando lo de cada bloque
(--cache): solo se diariza el bloque nuevo. El resultado va al sidecar de la reunión marcado como parcial, y la página
del directo enseña quién habla en lo ya identificado. Al terminar, la identificación de siempre (trabajos._run_voices)
usa esos bloques y solo diariza el último: «Quién habla» llega antes.

El directo es la prioridad (no puede ir más lento por esto): con el directo en este PC no se hace
nada (Whisper ya usa la CPU), y en la nube el subproceso va en prioridad de reposo (Windows; nice 19 fuera), con un
solo hilo, y se corta en cuanto la grabación termina, el directo pasa a este PC o la app se cierra. Lo medido está en
scripts/directo_bench.py (--con-voces) y en la nota de la rama."""
import hashlib
import os
import shutil
import threading
import time
import uuid

import engines
import voices
from common import log, log_exc
from rutas import DATA_DIR, safe

PASO_S = 15        # cada cuánto se mira si algún directo en la nube ha completado un bloque
VIEJO_S = 12 * 3600  # lo de los bloques de una reunión que no se ha cerrado en tanto tiempo (la app se cerró…), fuera
MARGEN_S = 20      # segundos grabados más allá del bloque: la grabadora escribe con ~1 s de retraso
HILOS = 1          # hilos de onnxruntime: el resto de la CPU, para el directo y la llamada


def en_la_nube(job):
    return (engines.PRESETS_BY_ID.get(getattr(job, "preset", None)) or {}).get("engine") == "cloud"


def cache_dir(audio_abs):
    """Lo de cada bloque de esa grabación (voices.bloque_guardar): en una carpeta suya, que se borra en cuanto termina
    su identificación de siempre (lleva las huellas de voz de todos los que hablan; revisión 1, P2), al arrancar si
    es vieja y al apagar «Voces»."""
    k = hashlib.md5(os.path.normcase(os.path.abspath(audio_abs)).encode("utf-8")).hexdigest()[:16]
    return os.path.join(DATA_DIR, voices.BLOQUES_DIR, k)


def borrar_cache(audio_abs):
    shutil.rmtree(cache_dir(audio_abs), ignore_errors=True)


def purgar(todo=False):
    """Las carpetas de bloques viejas (o todas: «Voces» apagado). Devuelve cuántas."""
    base, n = os.path.join(DATA_DIR, voices.BLOQUES_DIR), 0
    try:
        entradas = list(os.scandir(base))
    except OSError:
        return 0
    for e in entradas:
        try:
            viejo = time.time() - e.stat().st_mtime > VIEJO_S
        except OSError:
            continue
        if todo or viejo:
            if e.is_dir(follow_symlinks=False):
                shutil.rmtree(e.path, ignore_errors=True)
            else:
                try:
                    os.remove(e.path)
                except OSError:
                    continue
            n += 1
    return n


class VocesDirecto:
    def __init__(self, jobs):
        self.jobs = jobs
        self.lock = threading.Lock()   # el del sidecar: el parcial no se escribe a la vez que el de _run_voices
        self.hechos = {}               # reunión -> segundos ya identificados en directo
        self.finales = set()           # reuniones cuya identificación de siempre ya ha empezado: nada parcial después
        self.en_curso = None           # (reunión, hasta) mientras corre un subproceso

    # --- el hilo
    def bucle(self):
        while not getattr(self.jobs, "closing", False):
            time.sleep(PASO_S)
            try:
                self.paso()
            except Exception:
                log_exc()

    def candidatos(self):
        """Los directos en la nube con la grabación en marcha y el bloque que les toca (si las voces están listas)."""
        jobs = self.jobs
        try:
            if not jobs.voices_on() or not voices.available()[0] or not voices.models_ready(DATA_DIR):
                return []
        except Exception:
            return []
        with jobs.lock:
            vivos = [j for j in jobs.list
                     if j.live and j.status == "running" and j.recording and en_la_nube(j)]
        out = []
        for j in vivos:
            hasta = int((j.recorded or 0) // voices.BLOQUE_S) * voices.BLOQUE_S
            if hasta and hasta > self.hechos.get(j.path, 0) and (j.recorded or 0) >= hasta + MARGEN_S \
                    and j.path not in self.finales:
                out.append((j, hasta))
        return out

    def paso(self):
        for job, hasta in self.candidatos():
            self.identificar(job, hasta)

    def sigue(self, job):
        """El bloque solo se identifica mientras el directo siga en la nube con la grabación en marcha."""
        return (job.status == "running" and job.recording and en_la_nube(job) and job.path not in self.finales
                and not getattr(self.jobs, "closing", False))

    # --- un bloque
    def identificar(self, job, hasta):
        jobs, rel = self.jobs, job.path
        try:
            abs_path = safe(rel, "file")
        except Exception:
            return False
        tmp = os.path.join(DATA_DIR, "tmp", f"voces-parcial-{uuid.uuid4().hex[:8]}.json")
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        t0 = time.time()
        self.en_curso = (rel, hasta)
        try:
            res = jobs._voices_subprocess(None, ["--diarize", abs_path, "--out", tmp, "--rejilla", "--cache",
                                                 cache_dir(abs_path), "--hasta", str(hasta)],
                                          idle_timeout=1800, cancel=lambda: not self.sigue(job), reposo=True,
                                          hilos=HILOS)
            data = voices.read_sidecar(tmp)
        except engines.Cancelled:
            log(f"🗣 quién habla en directo: cortado ({hasta // 60:.0f} min; la grabación ha terminado o el directo "
                "ya no va en la nube)")
            return False
        except Exception as e:
            log(f"🗣 quién habla en directo: no se ha podido ({e.__class__.__name__}); se hará al terminar")
            self.hechos[rel] = hasta  # no se reintenta este bloque: el siguiente, o al terminar
            return False
        finally:
            self.en_curso = None
            try:
                os.remove(tmp)
            except OSError:
                pass
        if not data or not data.get("tramos"):
            self.hechos[rel] = hasta
            return False
        with self.lock, voices.SIDECAR_LOCK:
            if not self.sigue(job) or jobs.active_for(rel, kinds=("voces",)):
                return False
            side = voices.sidecar_path(abs_path)
            old = voices.read_sidecar(side) or {}
            uid = (jobs.voices.user() or {}).get("id")
            voices.identify(data, res.get("huellas") or {}, jobs.voices)
            # lo que dijiste de las voces del bloque anterior (sus nombres), medido solo hasta donde llegaba
            antes = (old.get("parcial") or {}).get("hasta") if isinstance(old.get("parcial"), dict) else None
            voices.carry_manual(old, data, usuario=uid, known=jobs.voices.names(), hasta=antes)
            data["micro"] = old.get("micro")
            data["parcial"] = {"hasta": hasta}
            data["rejilla"] = voices.BLOQUE_S  # al terminar, los mismos bloques (_run_voices: --rejilla y la cache)
            voices.write_sidecar(side, data)
            # (las huellas no se guardan en jobs.voice_vectors: con el bloque siguiente, «h2» puede ser otra voz;
            # «Recordar» espera al final: Jobs.speaker_vector)
            jobs.voice_vectors.pop(rel, None)
        self.hechos[rel] = hasta
        jobs.library.at = 0
        real = [i for i in data["hablantes"].values() if not i.get("breve")]
        log(f"🗣 quién habla en directo: {hasta // 60:.0f} min identificados ({len(real)} personas, "
            f"{time.time() - t0:.0f} s)")
        return True

    def final(self, rel, audio_abs):
        """Empieza la identificación de siempre de esa reunión: lo parcial ya no se escribe (y el subproceso de un
        bloque que siga en marcha se corta). Devuelve los argumentos de voices.py para usar los mismos bloques y lo ya
        hecho; quien llama solo los pasa si el sidecar dice que se identificó en directo («rejilla») y, al terminar
        bien, borra la carpeta de sus bloques (borrar_cache)."""
        with self.lock:
            self.finales.add(rel)
        return ["--rejilla", "--cache", cache_dir(audio_abs)]
