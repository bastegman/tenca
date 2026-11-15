# -*- coding: utf-8 -*-
"""La cola de trabajos (Jobs, Job): transcribir, el directo, resumir, las pasadas pendientes y la precarga del modelo
local, y los ficheros que escribe el directo (live_writer, LiveOutputs).

Salio tal cual de server.py al partirlo (15/11/2026); server.py lo reexporta. COMPLETE y DATA_DIR los fijan App() y
main() al arrancar: server.py los copia aqui (server.COMPARTIDAS)."""
import contextlib
import itertools
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

import engines
import explicar
import live
import salidas
import voices
from ajustes import NUBE_OFF, agente_de, moved_rel, procesado_de
from aprendido import aplicar_aprendido
from biblioteca import (CLOUD_TEXT_LOCK, COMPLETE, DATE_RE, PARTICIPANTES_ORIGENES, TEXTOS_SUGERIDOS, CloudText,
                        Complete, Transcripts, apply_edits, dump_notes, parse_notes, participantes_confirmados,
                        srt_block, traduccion_destino, write_outputs)
from carpetas import backup_dir, save_original, to_recycle_bin
from common import APP_NAME, ApiError, default_threads, log, log_exc, parse_srt, read_text, save_text, write_bytes
from listas import LIVE_PRESETS, idioma_repaso, meeting_setup, perfil_de
from rutas import APP_DIR, DATA_DIR, in_app_tree, safe


TEXT_GEN = itertools.count(1)  # generaciones del texto en directo (Job.text_gen); next() es atómico con el GIL


def live_writer(job):
    """El escritor del directo (live.run lo llama tras cada trozo con todas las frases): guarda lo que dio Whisper
    (job.raw) y escribe el .txt y el .srt con las correcciones a mano. Si lo que llega cambia frases ya escritas (el
    directo rehace: cambio de idioma, «rehacer desde el principio»), sube job.text_gen y apunta desde qué segundo
    (job.rewrites), con el mismo candado con que /api/transcript lee el texto y su generación."""
    def writer(segs):
        with job.edit_lock:
            segs, old = list(segs), job.raw
            if old and segs[:len(old)] != old:  # se rehace lo ya escrito: desde la primera frase que cambia
                i = next((n for n, (x, y) in enumerate(zip(segs, old)) if x != y), min(len(segs), len(old)))
                desde = [float(v[i][0]) for v in (old, segs) if i < len(v) and v[i][0] is not None]
                job.text_gen = next(TEXT_GEN)
                job.rewrites.append((job.text_gen, min(desde) if desde else 0.0))
            job.raw = segs
            try:
                job.out.write(apply_edits(job.raw, job.edits))
            except OSError as e:  # se reintenta con el siguiente trozo
                log(f"   no se pudo escribir la transcripción todavía ({e.__class__.__name__})")
    return writer


class LiveOutputs:
    """El .txt y el .srt del directo: lo nuevo se ANADE al final; solo si cambia algo de antes
    (una correccion, "rehacer desde el principio") se reescribe el archivo, y siempre en el sitio."""

    def __init__(self, audio_abs):
        self.base = os.path.splitext(audio_abs)[0]
        self.done, self.sizes = None, {}  # lo que ya esta en disco y el tamano que deberia tener
        self.lock = threading.Lock()

    def _same_on_disk(self):
        try:
            return all(os.path.getsize(self.base + e) == n for e, n in self.sizes.items())
        except OSError:
            return False  # lo han borrado o movido: se escribe entero

    def write(self, segs):
        segs = list(segs)
        with self.lock:
            d = self.done
            if d is not None and segs[:len(d)] == d and self._same_on_disk():
                new = segs[len(d):]
                if not new:
                    return
                txt = "".join(t + "\n" for _, _, t in new).encode("utf-8")
                # en el .srt cada bloque va separado del anterior por una linea en blanco
                srt = "".join(("\n" if d or k else "") + srt_block(len(d) + k + 1, a, b, t)
                              for k, (a, b, t) in enumerate(new)).encode("utf-8")
                write_bytes(self.base + ".txt", txt, append=True)
                write_bytes(self.base + ".srt", srt, append=True)
            else:
                write_outputs(self.base + ".wav", segs)  # (solo se usa para sacar la ruta base)
            self.done = segs
            self.sizes = {e: os.path.getsize(self.base + e) for e in (".txt", ".srt")}


class Job:
    def __init__(self, path, name, preset, language):
        self.id = uuid.uuid4().hex[:10]
        self.path, self.name, self.preset, self.language = path, name, preset, language
        self.engine = engines.PRESETS_BY_ID[preset]["engine"] if preset in engines.PRESETS_BY_ID else preset
        self.kind = "summary" if preset == "resumen" else "voces" if preset == "voces" else "transcribe"
        self.then_summary = False  # voces: al terminar, lanzar el resumen automatico (asi lleva los nombres)
        self.status, self.progress, self.stage = "queued", 0.0, "En cola"
        self.created, self.started, self.finished = time.time(), None, None
        self.t_transcribe = None
        self.error, self.duration, self.detected, self.eta = None, None, None, None
        self.cancel = False
        # solo en directo: segundos grabados / ya transcritos, y contador de reescrituras del .srt
        self.live, self.recording, self.recorded, self.transcribed, self.rev = False, False, 0.0, 0.0, 0
        self.restart = False
        self.resume_from = None  # continuar una transcripcion a medias desde este segundo
        self.final_pass = False  # repaso del archivo entero al terminar un directo en la nube
        self.opts = None  # solo en directo: opciones que live.run va leyendo (vocabulario incluido)
        self.translate, self.translation, self.directo = False, [], None  # directo en la nube (live.py publish)
        self.translate_to = "es"  # destino de la traduccion: «es» o «en» (start_live pone el de la reunion)
        # correcciones a mano durante el directo: {inicio de la frase: texto}, sobre lo que da Whisper (raw)
        self.edits, self.raw, self.out, self.edit_lock = {}, [], None, threading.Lock()
        # cada vez que el directo rehace texto ya escrito (cambio de idioma, «rehacer desde el principio») cambia
        # text_gen y se apunta desde qué segundo (rewrites): lo que se corrige sobre un texto anterior no toca lo
        # rehecho. Sale de un contador de todo el proceso: el directo siguiente de la misma reunión (pausar y volver a
        # transcribir) no repite números de este (revisión 2)
        self.text_gen, self.rewrites = next(TEXT_GEN), []

    def public(self):
        now = time.time()
        d = {"id": self.id, "path": self.path, "name": self.name, "preset": self.preset,
             "engine": self.engine, "status": self.status, "progress": round(self.progress, 4),
             "stage": self.stage, "error": self.error, "duration": self.duration,
             "language": self.detected, "eta": self.eta, "created": self.created,
             "elapsed": ((self.finished or now) - self.started) if self.started else None,
             "resume_from": self.resume_from, "kind": self.kind, "final_pass": self.final_pass}
        if self.live:
            d.update(live=True, recording=self.recording, recorded=round(self.recorded, 1),
                     transcribed=round(self.transcribed, 1), rev=self.rev, directo=self.directo)
        if self.error:  # el error en una frase con qué hacer (explicar.py); «error» sigue siendo el técnico
            d["aviso"] = explicar.explicar(self.error)
        return d


def keepalive_s(st):
    """Segundos que un proceso de Whisper sigue cargado sin trabajo (ajuste worker_keepalive_min; 0 = 5 min, como
    antes de existir el ajuste). Mientras esta cargado ocupa su RAM (medido: small ~450 MB, medium ~1 GB)."""
    try:
        m = int(st.get("worker_keepalive_min", 30) or 0)
    except (TypeError, ValueError):
        m = 30
    return m * 60 if m > 0 else engines.WorkerClient.IDLE_S


def preload_model(st, cached=None):
    """(worker, modelo) que conviene dejar cargado al arrancar: el del directo si es local y ya esta descargado; si
    no, el de la cola. None si worker_preload esta apagado o no hay modelo en cache (descargar no es precargar).
    Con «API externa» solo el del directo, si es en el PC: la cola en el PC carga su modelo al llegarle un trabajo
    (2-6 s en uno de minutos) y, mientras tanto, no ocupa memoria que no hace falta (medium ~1 GB; fix-lento)."""
    if not st.get("worker_preload", True):
        return None
    cached = cached or engines.model_cached
    cuales = (("live", "live_preset"),) if procesado_de(st) == "api" else (("live", "live_preset"), ("batch", "preset"))
    for which, key in cuales:
        p = engines.PRESETS_BY_ID.get(st.get(key) or "")
        if p and p["engine"] == "local" and cached(p["model"]):
            return which, p["model"]
    return None


def live_cloud_cfg(st):
    """Ajustes de la nube para el directo: los de cloud y el modelo del resumen (la traduccion lo usa con una
    pasarela propia). _run_live los vuelve a leer en cada trozo, no una copia del arranque."""
    return dict(st.get("cloud") or {}, summary_model=st.get("summary_model"))


class PendingPasses(Complete):
    """Reuniones con el repaso final pendiente (en cola o a medias), en DATA_DIR/repasos_pendientes.json: se apunta
    al ponerlo en cola y se quita al terminar (bien, con error o cancelado a mano). Lo que siga apuntado al arrancar
    se quedo a medias al apagar la app (o al cerrar la sesion de Windows) y se retoma.

    Con cada reunion va local_part: si su directo tuvo algun trozo en local (sin el dato, el formato anterior, una lista
    de rutas, se da por hecho que si). Si el repaso no acaba, el texto que queda es el del directo, y lo transcrito en
    local no sale del PC por el resumen automatico sin permiso. Quien lo decide es CloudText, que vive en DATA_DIR y
    sobrevive al reinicio igual que este fichero: local_part queda como constancia en el repaso (Job.local_part)."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, list):  # formato anterior: solo las rutas
            raw = {rel: {} for rel in raw if isinstance(rel, str)}
        self.data = {rel: v for rel, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}

    def _save(self):
        with open(self.path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, sort_keys=True)
        os.replace(self.path + ".tmp", self.path)

    def set(self, rel, done=True, local_part=True):
        """done: apuntarla (con local_part) o, con False, quitarla."""
        with self.lock:
            if done:
                info = {"local_part": bool(local_part)}
                if self.data.get(rel) == info:
                    return
                self.data[rel] = info
            elif rel in self.data:
                del self.data[rel]
            else:
                return
            self._save()

    def local_part(self, rel):
        """Si el directo de esa reunion tuvo algun trozo en local (sin el dato, si)."""
        return (self.data.get(rel) or {}).get("local_part") is not False


_SALIDAS_LOCK = threading.Lock()  # temporizadores de la reescritura agrupada de los destinos de salida
# una sola Voices (voces.json) aunque la pidan dos hilos a la vez: el que rehace las huellas al arrancar
# (app_voces.rehacer_huellas) y la interfaz. Con dos, cada una con su copia de las personas, la que guardara despues
# pisaria lo de la otra (revision 3 de motor-lote1, P2-9)
_VOICES_LOCK = threading.Lock()


def proveedor_resumen(st):
    """Quien hace el resumen, para la cabecera del .resumen.md (proveedor:): la interfaz lo dice encima del resumen
    («Resumen hecho con IA por Groq…», «…por tu cuenta de Claude…») sin adivinarlo por el nombre del modelo (B5)."""
    if procesado_de(st) == "agente":
        cls, _ = agente_de(st)
        return "tu cuenta de Claude" if cls.tipo == "claude" else f"tu cuenta de {cls.label}"
    prov = (st.get("cloud") or {}).get("provider") or "groq"
    if prov == "custom":  # sin el nombre del servidor: puede ser la pasarela de una empresa
        return "una API compatible con OpenAI"
    return (engines.PROVIDERS.get(prov) or {}).get("label") or ""


class Jobs:
    def __init__(self, settings, library):
        self.settings, self.library = settings, library
        self.list, self.lock = [], threading.Lock()
        self.claimed = set()  # grabaciones de la propia app: el directo lo arranca quien graba, no el vigilante
        self.queues = {"local": queue.Queue(), "cloud": queue.Queue()}
        # Whisper corre en procesos aparte (worker.py): uno para la cola y otro para el directo,
        # cada uno con su modelo en memoria; siguen cargados worker_keepalive_min tras el ultimo trabajo
        keep = lambda: keepalive_s(self.settings.get())  # noqa: E731
        self.local = engines.LocalEngine(DATA_DIR, "batch", idle_s=keep)
        self.live_worker = engines.WorkerClient("live", DATA_DIR, idle_s=keep)
        self.cloud = engines.CloudEngine(os.path.join(DATA_DIR, "tmp"), log=log)
        if self.cloud.purge_tmp():  # trozos de audio de una sesion que se corto (apagado, actualizacion, cuelgue)
            log("   borrados los trozos de audio temporales que dejó la sesión anterior")
        self.summarizer = engines.Summarizer(keys=self.cloud.keys)  # en la nube (Groq); mismas claves y cupo
        self.summarizer_agente = None  # con el agente del PC (procesado "agente"): se crea al primer uso
        self.queues["resumen"] = queue.Queue()
        self.queues["voces"] = queue.Queue()  # quien habla: un subproceso de prioridad baja, de uno en uno
        self._voices, self.voice_vectors = None, {}  # voces.json (se abre al primer uso) y huellas por reunion
        self._registro, self._salidas_timers = None, {}  # salidas.json (documentos escritos) y reescrituras pendientes
        self.closing = False  # apagando: un repaso final cancelado queda pendiente para el proximo arranque
        # trabajos que se estan ejecutando o terminando por reunion (busy_for): lo que encadenan al acabar (voces,
        # resumen) ya esta en cola cuando se sueltan. on_idle(rel): la reunion ha quedado libre; on_tick(): cada 3 s
        # (App: los movimientos pendientes, cambiar la carpeta en pleno directo)
        self.held, self.on_idle, self.on_tick = {}, None, None
        for kind in self.queues:
            threading.Thread(target=self._worker, args=(kind,), daemon=True, name=f"worker-{kind}").start()
        threading.Thread(target=self._watch_recordings, daemon=True, name="live-watch").start()
        threading.Thread(target=self._resume_final_passes, daemon=True, name="repasos-pendientes").start()

    def shutdown(self):
        """Apagar: lo que esta en cola se cancela, lo que corre recibe la senal de cancelar y los procesos de Whisper
        (cola y directo) se matan. Los trozos ya escritos (.txt/.srt en directo) se quedan; los de audio que se
        estaban subiendo a la nube (DATA_DIR/tmp) se borran (lo que siga abierto, al arrancar la proxima vez)."""
        with self.lock:
            self.closing = True  # un repaso final en cola o a medias queda apuntado: se retoma al volver a abrir
            for j in self.list:
                if j.status == "queued":
                    j.status = "cancelled"
                elif j.status == "running":
                    j.cancel = True
        for w in (getattr(self.local, "worker", None), self.live_worker):
            try:
                if w is not None:
                    w.kill()
            except Exception:
                log_exc()
        try:
            if getattr(self, "cloud", None) is not None:
                self.cloud.purge_tmp()
        except Exception:
            log_exc()

    def preload(self):
        """Al arrancar: deja cargado el modelo local que va a usar el directo (o, si el directo es en la nube, el de
        la cola) en su proceso de prioridad baja, para que el primer trozo de la reunion no espere a la carga
        (medido: 2-6 s en frio). Devuelve (worker, modelo) o None. Pensado para un hilo aparte: no bloquea nada."""
        st = self.settings.get()
        pick = preload_model(st)
        if not pick:
            return None
        which, model = pick
        w = self.live_worker if which == "live" else self.local.worker
        t0 = time.time()
        try:
            if w.ensure(model, int(st.get("threads") or default_threads())):
                log(f"modelo {model} precargado para {'el directo' if which == 'live' else 'la cola'} "
                    f"en {time.time() - t0:.1f} s")
        except engines.EngineError as e:
            log(f"no se pudo precargar {model}: {e}")
        return pick

    @property
    def voices(self):
        if self._voices is None:
            with _VOICES_LOCK:
                if self._voices is None:
                    self._voices = voices.Voices(os.path.join(DATA_DIR, "voces.json"))
        return self._voices

    def active_for(self, path, kinds=("transcribe",)):
        """Trabajo en cola o en marcha sobre ese audio. Por defecto solo transcripciones (directo incluido):
        un resumen en marcha no impide editar, restaurar ni volver a transcribir la reunion."""
        with self.lock:
            return next((j for j in self.list if j.path == path and j.kind in kinds
                         and j.status in ("queued", "running")), None)

    def _hold(self, rel):
        """Un trabajo de esa reunion empieza a ejecutarse (en su hilo): hasta _release, busy_for la da por ocupada."""
        with self.lock:
            held = self.__dict__.setdefault("held", {})
            held[rel] = held.get(rel, 0) + 1

    def _release(self, rel):
        """El trabajo ha terminado del todo (con lo que encadena ya en cola): si la reunion queda libre, on_idle."""
        with self.lock:
            held = self.__dict__.setdefault("held", {})
            n = held.get(rel, 0) - 1
            if n > 0:
                held[rel] = n
            else:
                held.pop(rel, None)
        cb = getattr(self, "on_idle", None)
        if n <= 0 and cb and not self.busy_for(rel):
            try:
                cb(rel)
            except Exception:
                log_exc()

    def busy_for(self, rel):
        """Por que la app va a escribir aun en los archivos de esa reunion (una frase), o None: un trabajo suyo en cola,
        en marcha o terminando (lo que encadena al acabar, voces y resumen, ya esta en cola cuando se suelta: hold),
        o su repaso final pendiente de un cierre de la app (se retoma al arrancar)."""
        kinds = {"summary": "Se está resumiendo esta reunión",
                 "voces": "Se están identificando las voces de esta reunión"}
        with self.lock:
            held = (getattr(self, "held", None) or {}).get(rel)
            j = next((j for j in self.list if j.path == rel and j.status in ("queued", "running")), None)
        if j:
            return kinds.get(j.kind, "Se está transcribiendo esta reunión")
        if held:
            return "Se está procesando esta reunión"
        try:
            if rel in self._pending().data:
                return "Falta el repaso final de esta reunión"
        except Exception:
            log_exc()
        return None

    # --- en directo
    def _watch_recordings(self):
        """Arranca la transcripcion en directo en cuanto aparece una grabacion de OBS en curso. En la misma vuelta, el
        resto de cosas de cada 3 s (on_tick: los movimientos pendientes de las reuniones que ya han quedado libres)."""
        while True:
            time.sleep(3)
            if getattr(self, "on_tick", None):
                try:
                    self.on_tick()
                except Exception:
                    log_exc()
            try:
                if not self.settings.get().get("live_auto"):
                    continue
                self.library.refresh(max_age=2.5)
                for a in list(self.library.flat):
                    if not a.get("recording"):
                        continue
                    with self.lock:  # una vez por grabacion (si el usuario la para, no se relanza)
                        seen = a["path"] in self.claimed or any(j.path == a["path"] and j.live for j in self.list)
                    if not seen and not self.active_for(a["path"]):
                        self.start_live(a["path"], auto=True)
            except Exception:
                log_exc()

    def start_live(self, rel, preset=None, auto=False):
        st = self.settings.get()
        preset = preset or st.get("live_preset") or "medium"
        if preset not in LIVE_PRESETS:
            preset = "medium"
        if preset == "nube" and procesado_de(st) != "api":
            preset = "medium"  # la nube esta desactivada en Ajustes: en local
        if preset == "nube":
            try:
                engines.CloudEngine.endpoint(st["cloud"])
            except engines.EngineError as e:
                preset = "medium"  # sin API key: en local
                # y se dice: hasta fix-lento caia en silencio a Whisper en el PC (medium, ~1 GB y la CPU ocupada
                # durante toda la reunion) y se veia como «la nube va lenta» o «cargando» (fix-groq, 10/11/2026)
                log(f"   directo: la nube no está lista ({e}); se transcribe en este PC con {preset}")
        abs_path = safe(rel, "file")
        if self.active_for(rel):
            raise ApiError("Ese audio ya se está transcribiendo.")
        job = Job(rel, os.path.basename(rel), preset, meeting_setup(abs_path, st)["language"] or None)
        job.engine, job.live, job.recording = "live", True, True
        job.translate_to = traduccion_destino(rel, st)  # el ultimo de esta reunion o el idioma del resumen (msg 1152)
        # algun trozo en local (o, al retomar tras un reinicio, lo de antes sin constar que fuera en la nube): el
        # resumen automatico no lo sube a la nube sin permiso (CloudText, Jobs.auto_summary)
        retoma = os.path.isfile(os.path.splitext(abs_path)[0] + ".srt")
        job.local_part = (engines.PRESETS_BY_ID[preset]["engine"] != "cloud"
                          or (retoma and rel not in self._cloud_text()))
        self._cloud_text().set(rel, not job.local_part)
        job.status, job.started, job.stage = "running", time.time(), "Empezando en directo…"
        COMPLETE.set(rel, False)  # hasta que termine bien, no se da por completa
        with self.lock:
            self.list.append(job)
        self._hold(rel)  # hasta que el directo acabe con lo que encadene (repaso final, voces, resumen)
        threading.Thread(target=self._run_live_held, args=(job, abs_path, st), daemon=True, name="live").start()
        log(f"● {job.name}  transcripción en directo{' (automática)' if auto else ''}")
        return job.id

    def _final_pass(self, job):
        """Al terminar un directo en la nube se repasa la grabacion entera de una vez: Whisper ve
        mucho mas contexto que en trozos de 8-15 s y el texto queda mejor (1 h ~ 2 min en Groq).
        Mientras tanto se sigue viendo el del directo. No se hace si lo corregiste a mano."""
        st = self.settings.get()
        if not st.get("live_final_pass", True) or job.preset != "nube" or job.recorded < 30:
            return False
        if job.edits or os.path.isdir(backup_dir(job.path)):
            log(f"   {job.name}: sin repaso final (tiene correcciones a mano)")
            return False
        try:
            self._cloud_ready()
        except ApiError:
            return False
        # local_part del directo: si tuvo algun trozo en local (sin el dato, si: lo seguro)
        idioma = idioma_repaso(getattr(job, "idiomas_directo", None))
        if idioma is not None:
            log(f"   {job.name}: el idioma cambió en el directo; el repaso final, con "
                f"{idioma or 'detección'} (no solo el del final)")
        # y los cambios de idioma que vio el directo (live: job.idiomas_tramos): cada tramo se repasa con el suyo
        # (punto 35; engines.CloudEngine.plan_parts)
        self._queue_final_pass(job.path, job.name, job.language, local_part=getattr(job, "local_part", True),
                               idioma_repaso=idioma, tramos_idioma=getattr(job, "idiomas_tramos", None))
        return True

    def _queue_final_pass(self, rel, name, language, local_part=True, idioma_repaso=None, tramos_idioma=None):
        """El repaso final de esa reunion a la cola: el del directo que acaba de terminar o, al arrancar, el que quedo
        apuntado (_resume_final_passes). Queda apuntado en repasos_pendientes.json hasta que termine: si se apaga
        antes, se retoma al arrancar. local_part: si su directo tuvo algun trozo en local (sin el dato, si: lo seguro).
        Va con el repaso (j.local_part) y se apunta con el. Si el repaso no acaba, el texto que queda es el del directo,
        y el resumen automatico mira CloudText (apuntado al empezar el directo): lo transcrito en local no sale del PC
        sin permiso, tampoco con un repaso retomado tras reiniciar (tests/test_integracion.py)."""
        j = Job(rel, name, "nube", language)
        j.final_pass, j.stage, j.local_part = True, "En cola: repaso final en la nube", local_part is not False
        # el idioma cambió en pleno directo (idioma_repaso, y en qué segundo: tramos_idioma): antes de entrar en la
        # cola, que el hilo lo vea. No se apunta en repasos_pendientes.json: uno retomado tras reiniciar usa el de la
        # reunión al terminar
        j.idioma_repaso, j.idiomas_tramos = idioma_repaso, list(tramos_idioma or ()) or None
        self._pending().set(rel, local_part=j.local_part)
        with self.lock:
            self.list.append(j)
        self.queues[j.engine].put(j)
        log(f"↻ {name}  repaso final de la grabación entera en la nube")
        return j

    def _pending(self):
        """Los repasos finales pendientes (PendingPasses), en el DATA_DIR vigente."""
        path = os.path.join(DATA_DIR, "repasos_pendientes.json")
        reg = getattr(self, "_pending_reg", None)
        if reg is None or reg.path != path:
            reg = self._pending_reg = PendingPasses(path)
        return reg

    def _final_pass_over(self, rel, keep_live_text=False):
        """El repaso final de esa reunion ya no esta pendiente. keep_live_text: no se hizo (cancelado a mano o ya no se
        puede hacer): se queda el texto del directo y con el se hacen los destinos y el resumen."""
        self._hold(rel)  # hasta encadenar voces y resumen (lo llama tambien un hilo suelto: cancelar, arrancar)
        try:
            try:
                self._pending().set(rel, False)
            except OSError:
                log_exc()
            if keep_live_text:
                log("   se mantiene el texto del directo")
                self._after_transcribe(rel)
        finally:
            self._release(rel)

    def _resume_final_passes(self, delay=3.0):
        """Al arrancar: los repasos finales que quedaron en cola o a medias al apagar se vuelven a poner en cola, y al
        terminar se hacen los destinos y el resumen como siempre. Si ya no se puede (sin nube, el repaso apagado en
        Ajustes, la reunion corregida a mano), se hacen con el texto del directo; si la reunion ya no esta, se
        olvida."""
        time.sleep(delay)
        try:
            for rel in sorted(self._pending().data):
                try:
                    abs_path = safe(rel, "file")
                except ApiError:
                    log(f"   repaso final pendiente de {rel}: la reunión ya no está ahí, se olvida")
                    self._final_pass_over(rel)
                    continue
                if self.active_for(rel):
                    continue
                ok = self.settings.get().get("live_final_pass", True) and not os.path.isdir(backup_dir(rel))
                if ok:
                    try:
                        self._cloud_ready()
                    except ApiError:
                        ok = False
                if ok:
                    log(f"   {os.path.basename(abs_path)}: el repaso final se quedó pendiente al apagar; se retoma")
                    self._queue_final_pass(rel, os.path.basename(abs_path), None,
                                           local_part=self._pending().local_part(rel))
                else:
                    log(f"   {os.path.basename(abs_path)}: el repaso final pendiente ya no se puede hacer")
                    self._final_pass_over(rel, keep_live_text=True)
        except Exception:
            log_exc()

    def refresh_prompt(self, rel):
        """Cambio el tema, el idioma o el contexto de una reunion: si se esta transcribiendo en directo, live.run lo
        lee en el siguiente trozo (vocabulario y contexto libre); el idioma, al momento (cancela el trozo en vuelo y
        rehace lo ultimo). Devuelve, para el aviso de la pagina, lo que va a pasar con el idioma en ese directo
        (live.lang_switch_info) o None si no hay directo."""
        with self.lock:
            jobs = [j for j in self.list if j.path == rel and j.live and j.status == "running" and j.opts]
        info = None
        for j in jobs:
            antes = j.opts.get("language") or None
            j.opts.update(meeting_setup(safe(rel), self.settings.get()))
            log(f"   directo {j.name}: vocabulario, idioma y contexto actualizados")
            if info is None:
                with getattr(j, "edit_lock", contextlib.nullcontext()):
                    escrito, edits = list(getattr(j, "raw", None) or []), dict(getattr(j, "edits", None) or {})
                info = live.lang_switch_info(getattr(j, "directo", None), escrito, edits,
                                             j.opts.get("language") or None, antes)
        return info

    def _live_job(self, job_id):
        with self.lock:
            job = next((j for j in self.list if j.id == job_id and j.live), None)
        if not job or job.status != "running":
            raise ApiError("Esa transcripción en directo ya no está en marcha.")
        return job

    def _check_live_preset(self, preset):
        if preset not in LIVE_PRESETS:
            raise ApiError("Modelo desconocido: " + str(preset))
        if engines.PRESETS_BY_ID[preset]["engine"] == "cloud":
            if procesado_de(self.settings.get()) != "api":
                raise ApiError(NUBE_OFF)
            self._cloud_ready()

    def set_live_preset(self, job_id, preset, restart=False):
        """Cambia el modelo del directo: desde el siguiente trozo, o rehaciendo todo desde el principio."""
        job = self._live_job(job_id)
        if preset:
            self._check_live_preset(preset)
            job.preset = preset
            if engines.PRESETS_BY_ID[preset]["engine"] != "cloud":
                job.local_part = True
                self._cloud_text().set(job.path, False)
            self.settings.update(lambda d: d.update(live_preset=preset))
        if restart:
            job.restart = True
            job.stage = "Rehaciendo desde el principio…"
        log(f"   directo {job.name}: {'rehacer con' if restart else 'modelo'} {job.preset}")
        return job.public()

    def _run_live_held(self, job, abs_path, st):
        try:
            self._run_live(job, abs_path, st)
        finally:
            self._release(job.path)

    def _run_live(self, job, abs_path, st):
        resume = []
        srt = os.path.splitext(abs_path)[0] + ".srt"
        if os.path.isfile(srt):  # la app se reinicio a mitad de grabacion: se retoma, no se repite
            try:
                resume = [(s["start"], s["end"], s["text"]) for s in parse_srt(read_text(srt))]
            except OSError:
                resume = []
        opts = {"preset": job.preset, **meeting_setup(abs_path, st),  # language, prompt (vocabulario) y about
                "threads": int(st.get("threads") or default_threads()),
                "worker": self.live_worker, "cloud": self.cloud, "cloud_cfg": live_cloud_cfg(st),
                # los ajustes de la nube vigentes en cada trozo (no una copia del arranque): una clave pegada, cambiada
                # o quitada en Ajustes vale desde el siguiente
                "cloud_cfg_fn": lambda: live_cloud_cfg(self.settings.get()),
                "tmp_dir": os.path.join(DATA_DIR, "tmp"), "resume": resume, "summarizer": self.summarizer,
                # idiomas propuestos que ya rechazaste en esta grabación (B6): no se vuelven a proponer, tampoco si el
                # directo se para y se reanuda
                "rechazos": self.__dict__.setdefault("rechazos_idioma", {}).setdefault(job.path, set())}
        job.opts = opts  # live.run los lee en cada trozo: si cambias tema, idioma o contexto, vale desde el siguiente
        job.out = LiveOutputs(abs_path)
        if resume:  # lo que ya estaba escrito no se reescribe: lo nuevo se anade detras
            job.out.done, job.raw = list(resume), list(resume)
            job.out.sizes = {e: os.path.getsize(os.path.splitext(abs_path)[0] + e) for e in (".txt", ".srt")
                             if os.path.isfile(os.path.splitext(abs_path)[0] + e)}

        writer = live_writer(job)

        try:
            segs, lang = live.run(job, abs_path, opts, writer, log)
            job.detected = lang
            err = getattr(job, "live_error", None)  # el ultimo trozo de la nube que no salio (live.run lo salta)
            if segs:
                writer(segs)
                if job.edits:  # lo que transcribio Whisper, para poder "Restaurar original" despues
                    save_original(job.path, segs)
                COMPLETE.set(job.path)  # llego hasta el final (si se corta antes, queda "a medias")
                if not self._final_pass(job):  # si hay repaso final, el resumen se hace al acabar este
                    self._after_transcribe(job.path)
                job.status, job.stage, job.progress = "done", "Hecho", 1.0
                log(f"✔ {job.name}  directo terminado ({job.recorded / 60:.0f} min grabados)")
            elif self._final_pass(job):  # sin texto del directo (p. ej. fallaron todos los trozos): lo hace el repaso
                job.status, job.progress = "done", 1.0
                job.stage = "Sin texto en directo: el repaso final transcribe la grabación entera"
                log(f"   {job.name}: el directo no dejó texto ({err or 'sin voz'}); lo transcribe el repaso final")
            else:
                job.status, job.stage = "error", "Error"
                job.error = (f"No se ha podido transcribir en directo ({err}). El audio se conserva: transcríbelo "
                             "desde la cola." if err else "No se ha detectado voz en la grabación.")
        except engines.Cancelled:
            job.status, job.stage = "cancelled", "Directo detenido"
            log(f"■ {job.name}  directo detenido")
        except Exception as e:
            job.status, job.stage, job.error = "error", "Error", str(e) or e.__class__.__name__
            log(f"✖ {job.name}  {job.error}")
            log_exc()
            # si la grabacion ya habia terminado, el repaso final (y tras el, los destinos y el resumen) se hace igual
            if not job.recording and self._final_pass(job):
                job.stage = "Error en el directo: el repaso final transcribe la grabación entera"
        finally:
            job.recording = False
            job.finished = time.time()
            self.library.at = 0

    def submit(self, paths, preset, language, resume=False):
        """resume: las que se quedaron a medias se continuan desde donde acaba el texto (el resto, enteras).
        language: "" = detectar, un codigo = ese idioma para todas, None = el de cada reunion (el de su .md o, si no
        lo fija, el del perfil; «Transcribir todos los pendientes»): lo decide _run con meeting_setup."""
        if preset not in engines.PRESETS_BY_ID:
            raise ApiError("Velocidad desconocida: " + str(preset))
        if engines.PRESETS_BY_ID[preset]["engine"] == "cloud":
            if procesado_de(self.settings.get()) != "api":
                raise ApiError(NUBE_OFF)
            self._cloud_ready()
        added = []
        for rel in paths:
            safe(rel, "file")
            if self.active_for(rel):
                continue
            a = self.library.find(rel)
            if a and a.get("recording"):
                raise ApiError("Se está grabando todavía: usa «Transcribir en directo».")
            job = Job(rel, os.path.basename(rel), preset, language or None)
            job.per_meeting = language is None
            if resume and a and a.get("partial"):
                job.resume_from = a["tx_end"]
            with self.lock:
                self.list.append(job)
            self.queues[job.engine].put(job)
            added.append(job.id)
        return added

    # --- resumen con IA: con la API de chat (procesado "api") o con el agente del PC (procesado "agente")
    def summary_missing(self):
        """Por que no se puede resumir ahora (None si se puede), segun donde se procesan las reuniones."""
        st = self.settings.get()
        mode = procesado_de(st)
        if mode == "local":
            return ("El resumen con IA está desactivado: en Ajustes → «Dónde se procesan tus reuniones» elige «Con tu "
                    "cuenta de Claude» o «API externa».")
        if mode == "agente":
            cls, _ = agente_de(st)
            if cls.find():
                return None
            return f"No encuentro {cls.label} en este PC: sigue el paso a paso de Ajustes y pulsa «Comprobar»."
        c = st["cloud"]
        if engines.chat_url(c) and (c.get("api_key") or "").strip():
            return None
        # con Azure OpenAI: que el resumen aun no funciona ahi (no "configura la nube")
        return (engines.chat_missing(c)
                or "El resumen necesita una API key de la nube (Ajustes ⚙ → Procesado).")

    def summary_ready(self):
        return self.summary_missing() is None

    def _summarizer(self, st):
        """El Summarizer que toca: el de la API de chat o, con procesado "agente", uno con el agente del PC."""
        if procesado_de(st) != "agente":
            return self.summarizer
        cls, _ = agente_de(st)
        cur = getattr(self, "summarizer_agente", None)
        if cur is None or cur.agente.tipo != cls.tipo:
            cur = self.summarizer_agente = engines.Summarizer(agente=cls(settings_dir=DATA_DIR))
        return cur

    def submit_summary(self, rel, auto=False):
        abs_path = safe(rel, "file")
        missing = self.summary_missing()
        if missing:
            raise ApiError(missing)
        if not any(os.path.isfile(os.path.splitext(abs_path)[0] + e) for e in (".srt", ".txt")):
            raise ApiError("Primero hay que transcribir la reunión.")
        with self.lock:
            if any(j.path == rel and j.kind == "summary" and j.status in ("queued", "running") for j in self.list):
                raise ApiError("Ya se está resumiendo.")
            job = Job(rel, os.path.basename(rel), "resumen", None)
            job.stage = "En cola para resumir"
            self.list.append(job)
        self.queues["resumen"].put(job)
        log(f"✨ {job.name}  resumen{' (automático)' if auto else ''} en cola")
        return job.id

    def _cloud_text(self):
        """CloudText del DATA_DIR vigente, uno solo aunque lo pidan dos hilos a la vez (con dos, uno podria volver a
        escribir en disco un «en la nube» que el otro ya habia quitado)."""
        path = os.path.join(DATA_DIR, "transcritas_en_la_nube.json")
        with CLOUD_TEXT_LOCK:
            reg = getattr(self, "_cloud_reg", None)
            if reg is None or reg.path != path:
                reg = self._cloud_reg = CloudText(path)
        return reg

    def _tx_in_cloud(self, rel):
        """¿El texto que tiene ahora esa reunion se transcribio entero en la nube? Lo dice CloudText, que apunta cada
        transcripcion al escribir; sin dato, no."""
        return rel in self._cloud_text()

    def auto_summary(self, rel):
        """Al terminar de transcribir: resumen automatico si es una reunion reciente (las antiguas se
        resumen a mano, para no gastar de golpe el cupo diario de Groq). El resumen manda el texto, las notas y el
        contexto al proveedor de la nube: si el texto no se transcribio entero en la nube (_tx_in_cloud), solo con el
        consentimiento de Ajustes (summary_auto_local). A mano, siempre. Con el agente del PC (procesado "agente") el
        consentimiento es haber elegido esa opcion, con su aviso: todo lo transcrito en local se resume."""
        st = self.settings.get()
        if not st.get("summary_auto", True) or not self.summary_ready():
            return
        mode = procesado_de(st)
        a = self.library.find(rel)
        when = None
        if a and a.get("date"):
            try:
                when = time.mktime(time.strptime(a["date"], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                when = None
        when = when or (a or {}).get("mtime") or 0
        if time.time() - when > 3 * 86400:
            return
        if mode != "agente" and not self._tx_in_cloud(rel) and not st.get("summary_auto_local"):
            log(f"   {os.path.basename(rel)}: transcrita en local, sin resumen automático (no sale del PC; se puede "
                "resumir a mano)")
            return
        try:
            self.submit_summary(rel, auto=True)
        except ApiError:
            pass

    def _run_summary(self, job):
        job.status, job.started, job.stage = "running", time.time(), "Preparando el resumen…"
        try:
            missing = self.summary_missing()  # por si Ajustes cambio mientras esperaba en la cola
            if missing:
                raise engines.EngineError(missing)
            abs_path = safe(job.path, "file")
            base = os.path.splitext(abs_path)[0]
            data = Transcripts().load(base)
            if not data or not data["segments"]:
                raise engines.EngineError("No hay transcripción que resumir.")
            a = self.library.find(job.path) or {}
            props, notes = ({}, "")
            if os.path.isfile(base + ".md"):
                props, notes = parse_notes(read_text(base + ".md"))
            st = self.settings.get()
            d = a.get("duration") or 0
            meta = {"title": props.get("titulo") or os.path.splitext(job.name)[0],
                    "date": (a.get("date") or "").replace("T", " ")[:16] or "fecha desconocida",
                    "duration": f"{round(d / 60)} min" if d else "duración desconocida",
                    # los sugeridos por Outlook/Teams no salen del PC hasta que el usuario los confirma o edita
                    "participants": participantes_confirmados(props), "notes": notes,
                    "extra": st.get("summary_extra", ""), "about": props.get("contexto", "")}
            segments = self._with_speakers(abs_path, data["segments"], meta)
            cfg = dict(st["cloud"], summary_model=st.get("summary_model"), summary_lang=st.get("summary_lang"),
                       agente_modelo=agente_de(st)[1]["modelo"])

            def report(frac=None, stage=None, **k):
                if stage:
                    job.stage = stage
                if frac is not None:
                    job.progress = max(job.progress, frac)

            text, model = self._summarizer(st).summarize(segments, meta, cfg, report, lambda: job.cancel)
            prov = proveedor_resumen(st)  # el mismo st que eligió el Summarizer
            head = (f"---\nmodelo: {model}\n" + (f"proveedor: {prov}\n" if prov else "")
                    + f"generado: {time.strftime('%Y-%m-%d %H:%M')}\n---\n\n")
            save_text(base + ".resumen.md", head + text.strip() + "\n", "utf-8")
            job.status, job.progress, job.stage = "done", 1.0, "Resumen listo"
            log(f"✔ {job.name}  resumen listo ({(time.time() - job.started):.0f} s, {model})")
            self.write_outputs(job.path)  # destinos de salida: el documento con el resumen
        except engines.Cancelled:
            job.status, job.stage = "cancelled", "Cancelado"
        except Exception as e:
            job.status, job.stage, job.error = "error", "Error", str(e) or e.__class__.__name__
            log(f"✖ {job.name}  resumen: {job.error}")
            if not isinstance(e, (engines.EngineError, ApiError)):
                log_exc()
        finally:
            job.finished = time.time()
            self.library.at = 0

    def _cloud_ready(self):
        """La nube se puede usar ahora: elegida en Ajustes («API externa») y con su endpoint. Lo comprueban tambien
        los trabajos al empezar (_run, _run_summary): lo que entro en cola con la nube no sale si se apago entre
        tanto."""
        st = self.settings.get()
        if procesado_de(st) != "api":
            raise ApiError(NUBE_OFF)
        try:
            engines.CloudEngine.endpoint(st["cloud"])
            return True
        except engines.EngineError as e:
            raise ApiError(str(e))

    def nube_apagada(self):
        """Ajustes deja de ser «API externa»: lo que estuviera en la nube no sigue. Los directos en nube pasan a local
        desde el siguiente trozo (como set_live_preset) y los trabajos de nube y los resumenes en cola se cancelan."""
        cambiados = []
        with self.lock:
            for j in self.list:
                en_nube = (engines.PRESETS_BY_ID.get(j.preset) or {}).get("engine") == "cloud"
                if j.status == "running" and j.live and en_nube:
                    j.preset, j.local_part = "medium", True
                    cambiados.append(j)
                elif j.status == "queued" and (j.engine == "cloud" or j.kind == "summary"):
                    j.status, j.stage = "cancelled", "Cancelado: la nube se ha desactivado en Ajustes"
                    j.finished = time.time()
        for j in cambiados:
            self._cloud_text().set(j.path, False)
            log(f"   directo {j.name}: modelo medium (la nube se ha desactivado en Ajustes)")
        return len(cambiados)

    # --- quien habla (voces): subproceso de prioridad baja al terminar cada transcripcion
    def voices_on(self):
        return voices.enabled(self.settings.get())

    def _after_transcribe(self, rel):
        """Transcripcion terminada: primero las voces (si estan activas) y el resumen al acabar estas, para que
        lleve los nombres; si no, el resumen directamente. Antes, el documento en los destinos de salida y, antes de
        todo, lo que aprendiste a corregir (aplicar_aprendido): asi el resumen y los documentos ya lo llevan bien."""
        self._aprendido(rel)
        self.write_outputs(rel)
        if not self.auto_voices(rel):
            self.auto_summary(rel)

    def _aprendido(self, rel):
        """aplicar_aprendido, nunca a costa de lo demas: si falla, la reunion sigue con sus destinos y su resumen."""
        try:
            aplicar_aprendido(rel, self.settings.get())
        except Exception:
            log_exc()

    # --- destinos de salida (salidas.py): la reunion como documento en otras carpetas
    def document_for(self, rel):
        """Lo que va al documento: titulo, fecha, duracion, propiedades, resumen (sin su cabecera), notas y
        transcripcion (con quien habla, si se sabe)."""
        abs_path = safe(rel, "file")
        base = os.path.splitext(abs_path)[0]
        props, notes = parse_notes(read_text(base + ".md")) if os.path.isfile(base + ".md") else ({}, "")
        # participantes sugeridos por Outlook/Teams sin confirmar: el destino (quiza sincronizado) no los lleva
        props = dict(props, participantes=participantes_confirmados(props))
        props.pop("participantes_origen", None)
        summary, model = "", None
        if os.path.isfile(base + ".resumen.md"):
            text = read_text(base + ".resumen.md")
            m = re.match(r"---\n(.*?)\n---\n", text, re.S)  # cabecera modelo/generado del .resumen.md
            if m:
                model = next((l.partition(":")[2].strip() for l in m.group(1).splitlines()
                              if l.startswith("modelo:")), None)
                text = text[m.end():]
            summary = text.strip()
        data = Transcripts().load(base)
        segments = data["segments"] if data else []
        if segments:
            try:  # con quien habla, si se sabe (no es imprescindible para el documento)
                segments = self._with_speakers(abs_path, segments, {})
            except Exception:
                log_exc()
        a = self.library.find(rel) or {}
        stem = os.path.splitext(os.path.basename(abs_path))[0]
        when = (a.get("date") or "").replace("T", " ")[:16]
        if not when:
            m = DATE_RE.search(stem)
            when = f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}" if m else ""
        d = a.get("duration") or 0
        return {"stem": stem, "title": props.get("titulo") or stem, "date": when,
                "duration": f"{round(d / 60)} min" if d else "", "props": props, "source": rel, "summary": summary,
                "summary_model": model, "notes": notes, "segments": segments, "app_name": APP_NAME}

    @property
    def registro(self):
        """salidas.json: que documento de cada destino es de que reunion (y su hash). Se abre al primer uso."""
        if getattr(self, "_registro", None) is None:
            self._registro = salidas.Registro(os.path.join(DATA_DIR, "salidas.json"))
        return self._registro

    def write_outputs(self, rel):
        """Escribe la reunion en los destinos de salida del perfil (si hay y la reunion ya tiene transcripcion).
        Nunca mueve ni toca el original; solo sobrescribe los documentos que genero la propia app (salidas.py). En una
        carpeta de la app (su raiz o una vinculada, donde viven las grabaciones) no se escribe nunca: un destino
        guardado antes de que eso se comprobara, o que haya quedado dentro al vincular una carpeta, solo da un aviso."""
        try:
            destinos = perfil_de(self.settings.get())["outputs"]
            if not destinos:
                return []
            base = os.path.splitext(safe(rel, "file"))[0]
            if not any(os.path.isfile(base + e) for e in (".srt", ".txt")):
                return []
            done = salidas.write_all(self.document_for(rel), destinos, self.registro, log, blocked=in_app_tree)
            self._note_outputs(rel, done)
            return done
        except Exception:
            log_exc()
            return []

    def _note_outputs(self, rel, done):
        """Lo que no se ha podido escribir queda como aviso para Ajustes (GET /api/perfil): los de esta reunion se
        sustituyen por los de esta escritura (que cubre todos los destinos), y se guardan como mucho 30."""
        with _SALIDAS_LOCK:
            avisos = getattr(self, "_salidas_avisos", None)
            if avisos is None:
                avisos = self._salidas_avisos = {}
            for key in [k for k, a in avisos.items() if a["reunion"] == rel]:
                del avisos[key]
            for path, accion in done:
                if accion not in ("escrito", "igual"):
                    avisos[os.path.normcase(path)] = {"path": path, "accion": accion, "reunion": rel, "at": time.time()}
            for key in sorted(avisos, key=lambda k: avisos[k]["at"])[:-30]:
                del avisos[key]

    def outputs_warnings(self):
        """Avisos de los destinos de salida, el mas reciente primero (solo de los destinos que siguen en el perfil)."""
        destinos = perfil_de(self.settings.get())["outputs"]
        with _SALIDAS_LOCK:
            avisos = list((getattr(self, "_salidas_avisos", None) or {}).values())
        avisos = [a for a in avisos if any(salidas.inside(a["path"], d["path"]) for d in destinos)]
        return sorted(avisos, key=lambda a: -a["at"])

    def flush_outputs(self, budget=15.0):
        """Al apagar: las reescrituras que esperaban su temporizador (schedule_outputs, 4 s) se hacen ya, y se espera a
        las que estan en marcha; si no, el ultimo cambio de las notas o de la transcripcion no llegaria a los destinos.
        Con un tope de `budget` segundos en total. Devuelve las reuniones escritas."""
        with _SALIDAS_LOCK:
            timers = getattr(self, "_salidas_timers", None) or {}
            pending = list(timers.items())
            timers.clear()
        t0, done = time.time(), []
        for rel, t in pending:
            t.cancel()
            if time.time() - t0 > budget:
                log(f"   destinos de salida: no ha dado tiempo a escribir {rel} al apagar")
                continue
            self.write_outputs(rel)
            done.append(rel)
        for t in threading.enumerate():  # las que ya vencieron y estan escribiendo
            if getattr(t, "salidas", False) and t is not threading.current_thread():
                t.join(max(0.0, budget - (time.time() - t0)))
        return done

    def outputs_moved(self, old, new, folder=False):
        """Una reunion (o una carpeta entera) cambia de ruta: sus documentos siguen siendo suyos en el registro y la
        reescritura que estuviera pendiente se hace con la ruta nueva."""
        try:
            self.registro.moved(old, new, folder)
        except OSError:
            log_exc()
        with _SALIDAS_LOCK:
            timers = getattr(self, "_salidas_timers", None) or {}
            moved = [(r, m) for r in list(timers) for m in [moved_rel(r, old, new, folder)] if m is not None]
            for r, _ in moved:
                timers.pop(r).cancel()
        for _, m in moved:
            self.schedule_outputs(m)

    def forget_outputs(self, rel):
        """La reunion se ha borrado: sus documentos de los destinos del perfil que siguen tal cual se escribieron van
        a la papelera; los que se cambiaron alli (o los de una carpeta que ya no es destino) se quedan. Devuelve cuantos
        se han mandado a la papelera."""
        with _SALIDAS_LOCK:
            t = (getattr(self, "_salidas_timers", None) or {}).pop(rel, None)
        if t:
            t.cancel()
        try:
            entries = self.registro.entries(rel)
        except OSError:
            return 0
        if not entries:
            return 0
        destinos = perfil_de(self.settings.get())["outputs"]
        intact = []
        for path, sha in entries:
            try:
                if any(salidas.inside(path, d["path"]) for d in destinos) and os.path.isfile(path) \
                        and salidas.sha256_of(path) == sha:
                    intact.append(path)
            except OSError:
                pass
        n = 0
        if intact:
            try:
                to_recycle_bin(intact)
                n = len(intact)
            except (ApiError, OSError) as e:
                log(f"   no se pudieron mandar a la papelera los documentos de {rel}: {getattr(e, 'msg', e)}")
                intact = []
        self.registro.forget([p for p, _ in entries])  # la reunion ya no existe: ninguno es de nadie
        return n

    def outputs_for_persona(self, pid=None):
        """Quien habla cambia de nombre (o se fusiona u olvida): los documentos de las reuniones donde sale (con None,
        las de cualquier persona reconocida) se reescriben, que llevan los nombres."""
        if not perfil_de(self.settings.get())["outputs"]:
            return 0
        self.library.refresh(max_age=5)
        n = 0
        for a in list(self.library.flat):
            data = voices.read_sidecar(a["_spk"]) if a.get("_spk") else None
            if data and any(i.get("persona") and (pid is None or i.get("persona") == pid)
                            for i in (data.get("hablantes") or {}).values()):
                self.schedule_outputs(a["path"])
                n += 1
        return n

    def schedule_outputs(self, rel, delay=4.0):
        """Reescritura al guardar cambios en las notas o la transcripcion: agrupada (se guarda mientras se escribe)."""
        if not perfil_de(self.settings.get())["outputs"]:
            return
        if delay <= 0:
            return self.write_outputs(rel)
        with _SALIDAS_LOCK:
            timers = getattr(self, "_salidas_timers", None)
            if timers is None:
                timers = self._salidas_timers = {}
            old = timers.pop(rel, None)
            if old:
                old.cancel()
            t = threading.Timer(delay, lambda: self._fire_outputs(rel, t))
            t.daemon, t.salidas = True, True  # flush_outputs las encuentra al apagar
            timers[rel] = t
        t.start()

    def _fire_outputs(self, rel, timer):
        """Vence la espera: se quita de la lista solo si sigue siendo el temporizador vigente (si entretanto se guardo
        otra vez, el nuevo ya ocupa su sitio y no hay que perderlo) y escribe."""
        with _SALIDAS_LOCK:
            timers = getattr(self, "_salidas_timers", {})
            if timers.get(rel) is timer:
                timers.pop(rel, None)
        self.write_outputs(rel)

    def auto_voices(self, rel):
        if not self.voices_on():
            return False
        with self.lock:
            cur = next((j for j in self.list if j.path == rel and j.kind == "voces"
                        and j.status in ("queued", "running")), None)
        if cur:
            cur.then_summary = True
            return True
        try:
            self.submit_voices(rel, auto=True, then_summary=True)
            return True
        except ApiError as e:
            log(f"   voces: {e.msg}")
            return False

    def submit_voices(self, rel, auto=False, then_summary=False):
        safe(rel, "file")
        if not self.voices_on():
            raise ApiError("Activa «Voces» en Ajustes ⚙ para identificar quién habla.")
        ok, why = voices.available()
        if not ok:
            raise ApiError("La librería de voces (sherpa-onnx) no está instalada o no carga: vuelve a ejecutar "
                           f"INSTALAR.bat ({why[:160]})")
        a = self.library.find(rel)
        if a and a.get("recording"):
            raise ApiError("Se está grabando todavía: las voces se identifican al terminar.")
        with self.lock:
            if any(j.path == rel and j.kind == "voces" and j.status in ("queued", "running") for j in self.list):
                raise ApiError("Ya se están identificando las voces de esta reunión.")
            job = Job(rel, os.path.basename(rel), "voces", None)
            job.stage, job.then_summary = "En cola para identificar voces", then_summary
            self.list.append(job)
        self.queues["voces"].put(job)
        log(f"🗣 {job.name}  voces{' (automático)' if auto else ''} en cola")
        return job.id

    def _voices_subprocess(self, job, args, idle_timeout=1800, cancel=None):
        """python voices.py … con prioridad baja; lee su progreso; cancelar = matar el proceso. Sin job (la huella de
        «Recordar», con el usuario esperando a «Guardar»), por debajo de lo normal y no en reposo: en reposo, con el PC
        ocupado (una llamada, otra identificacion de voces), Windows lo deja sin turno (fix-lento, 10/11/2026).
        cancel(): otra forma de cancelar, sin job (rehacer las huellas al arrancar: si empieza una grabacion)."""
        log_path = os.path.join(DATA_DIR, "voces.log")
        try:  # rotacion sencilla: el stderr de onnxruntime no debe crecer sin tope
            if os.path.getsize(log_path) > 1_000_000:
                os.replace(log_path, log_path + ".1")
        except OSError:
            pass
        err = open(log_path, "ab")
        p = subprocess.Popen([sys.executable, "-u", os.path.join(APP_DIR, "voices.py"), *args, "--data", DATA_DIR,
                              "--threads", str(voices.hilos())], stdout=subprocess.PIPE, stderr=err, cwd=APP_DIR,
                             creationflags=engines.NO_WINDOW | ((voices.IDLE_PRIORITY or engines.BELOW_NORMAL)
                                                                if job is not None else engines.BELOW_NORMAL))
        err.close()
        q = queue.Queue()

        def reader():
            for line in p.stdout:
                try:
                    q.put(json.loads(line))
                except ValueError:
                    pass
            q.put(None)
        threading.Thread(target=reader, daemon=True).start()
        last = time.time()
        try:
            while True:
                if (job and job.cancel) or (cancel and cancel()):
                    raise engines.Cancelled()
                try:
                    m = q.get(timeout=0.25)
                except queue.Empty:
                    if time.time() - last > idle_timeout:
                        raise engines.EngineError("La identificación de voces no dio señales de vida en "
                                                  f"{idle_timeout // 60} min; se ha cortado.")
                    continue
                last = time.time()
                if m is None:
                    raise engines.EngineError("El proceso de voces se cerró de golpe "
                                              "(detalles en %LOCALAPPDATA%\\AudiosApp\\voces.log).")
                if m.get("type") == "progress":
                    if job:
                        if m.get("stage"):
                            job.stage = m["stage"]
                        if m.get("frac") is not None:
                            job.progress = max(job.progress, min(float(m["frac"]), 0.99))
                elif m.get("type") == "error":
                    raise engines.EngineError(m.get("error") or "error al identificar voces")
                elif m.get("type") == "result":
                    return m
        finally:
            if p.poll() is None:
                p.kill()
            try:
                p.wait(timeout=5)
            except Exception:
                pass
            borrar_pcm_voces(p.pid)

    def speaker_vector(self, rel, h, cancel=None):
        """Huella de un hablante de una reunion: de la memoria del servidor o recalculada en un subproceso (cancel():
        cortarlo, engines.Cancelled)."""
        vec = (self.voice_vectors.get(rel) or {}).get(h)
        if vec:
            return vec, None
        abs_path = safe(rel, "file")
        args = ["--fingerprint", h, "--diarize", abs_path, "--out", voices.sidecar_path(abs_path)]
        m = self._voices_subprocess(None, args, idle_timeout=600, cancel=cancel)
        vec = (m.get("huellas") or {}).get(h)
        if not vec:
            raise ApiError("No se pudo calcular la huella de ese hablante.")
        self.voice_vectors.setdefault(rel, {})[h] = vec
        return vec, m.get("segundos")

    def speakers_for(self, abs_path, data=None):
        """Sidecar con las frases casadas con el .srt ACTUAL (barato) y los nombres resueltos; None si no hay."""
        data = data or voices.read_sidecar(voices.sidecar_path(abs_path))
        if not data or not data.get("tramos"):
            return None
        segs = Transcripts().load(os.path.splitext(abs_path)[0])
        who = voices.refresh_frases(data, segs["segments"]) if segs and segs["kind"] == "srt" else []
        names = self.voices.names()
        uid = (self.voices.user() or {}).get("id")
        for h, info in data.get("hablantes", {}).items():
            if info.get("persona") and info["persona"] not in names:  # persona olvidada
                info["persona"], info["provisional"] = None, False
            info["nombre"] = names.get(info.get("persona")) if info.get("persona") else None
            info["label"] = voices.label(h, data, names, usuario=uid)
            info["usuario"] = voices.is_user(h, data, names, usuario=uid)
        data["_who"], data["_usuario"] = who, uid
        return data

    def _with_speakers(self, abs_path, segments, meta):
        """Para el resumen: cada frase con quien la dice (nombre, «Yo» o «Persona N») si hay diarizacion."""
        data = self.speakers_for(abs_path)
        if not data or not data.get("_who"):
            return segments
        names = self.voices.names()
        out = [dict(s, who=voices.label(w, data, names, for_llm=True, usuario=data.get("_usuario")))
               for s, w in zip(segments, data["_who"])]
        meta["speakers"] = True
        known = [i["nombre"] for i in data["hablantes"].values()
                 if i.get("nombre") and not i.get("provisional") and not i.get("breve")]
        if known and not meta.get("participants"):
            meta["participants"] = ", ".join(dict.fromkeys(known))
        return out

    def _participant_hint(self, abs_path, old):
        """Cuantas personas nombra «Participantes» del .md: pista para no partir a una persona en varias
        (voices.consolidate). Solo si los sugirio Teams/Outlook o si aun no se habia identificado nunca: despues,
        _suggest_participants pudo rellenarlo solo con las voces reconocidas, y seria un tope demasiado bajo."""
        md = os.path.splitext(abs_path)[0] + ".md"
        try:
            props = parse_notes(read_text(md))[0] if os.path.isfile(md) else {}
        except (OSError, ValueError):
            return 0
        if old.get("tramos") and props.get("participantes_origen") not in PARTICIPANTES_ORIGENES:  # la grabadora
            # deja un sidecar con solo «micro»: eso aun no es una identificacion
            return 0
        texto = props.get("participantes")  # exacto solo si es el texto que escribio la app (no basta el origen: el
        # .md puede editarse fuera y conservar la nota «sugeridos por…»)
        return voices.count_participants(texto, sugeridos=" ".join(str(texto or "").split()) in TEXTOS_SUGERIDOS)

    def _suggest_participants(self, abs_path, data):
        """Propiedad «Participantes» con los nombres reconocidos, solo si esta vacia (no se pisa lo escrito a mano)."""
        names = self.voices.names()
        known = [names[i["persona"]] for i in data.get("hablantes", {}).values()
                 if i.get("persona") in names and not i.get("provisional") and not i.get("breve")]
        if not known:
            return
        md = os.path.splitext(abs_path)[0] + ".md"
        props, body = parse_notes(read_text(md)) if os.path.isfile(md) else ({}, "")
        if props.get("participantes"):
            return
        props["participantes"] = ", ".join(dict.fromkeys(known))
        save_text(md, dump_notes(props, body), "utf-8")

    def _run_voices(self, job):
        job.status, job.started, job.stage = "running", time.time(), "Preparando…"
        restore = None  # (sidecar, contenido anterior): si algo falla tras rediarizar, se deja como estaba
        try:
            abs_path = safe(job.path, "file")

            def report(stage=None, frac=None):
                if stage:
                    job.stage = stage
                if frac is not None:
                    job.progress = max(job.progress, min(float(frac), 0.99))
            if not voices.models_ready(DATA_DIR):
                try:
                    voices.ensure_models(DATA_DIR, report, lambda: job.cancel)
                except voices.VoicesError as e:
                    if job.cancel:
                        raise engines.Cancelled()
                    raise engines.EngineError(str(e))
            side = voices.sidecar_path(abs_path)
            old = voices.read_sidecar(side) or {}  # lo que el usuario ya dijo de esta reunion (se conserva)
            restore = (side, json.loads(json.dumps(old))) if old else None
            uid = (self.voices.user() or {}).get("id")
            # sin huellas y sin huellas por rehacer: una persona que solo tiene las de antes del cambio de modelo
            # (voices.Voices._migrar) si tenia huellas, y sus asignaciones de antes del 07/11 pudieron ser automaticas
            sin_huellas = {p["id"] for p in self.voices.personas() if not p.get("huellas") and not p.get("rehacer")}
            for i in (old.get("hablantes") or {}).values():  # sidecar de antes del 07/11/2026, sin «origen»: una
                if not i.get("origen") and i.get("persona") in sin_huellas - {uid}:  # persona sin huellas solo
                    i["origen"] = "manual"  # pudo ponerse a mano
            # el subproceso reescribe el sidecar con hablantes nuevos (h1, h2… otra vez): las huellas en memoria de la
            # identificacion anterior ya no son de esos hablantes y «Recordar» guardaria la voz de otro
            self.voice_vectors.pop(job.path, None)
            n = self._participant_hint(abs_path, old)
            res = self._voices_subprocess(job, ["--diarize", abs_path, "--out", side,
                                                *(["--participantes", str(n)] if n else [])])
            huellas = res.get("huellas") or {}
            data = voices.read_sidecar(side)
            if not data or not data.get("tramos"):
                raise engines.EngineError("No se ha detectado voz en la grabación.")
            voices.identify(data, huellas, self.voices)
            voices.carry_manual(old, data, usuario=uid, known=self.voices.names())
            srt = Transcripts().load(os.path.splitext(abs_path)[0])
            if srt and srt["kind"] == "srt":
                voices.refresh_frases(data, srt["segments"])
            voices.write_sidecar(side, data)
            restore = None
            self.voice_vectors[job.path] = huellas
            try:
                self._suggest_participants(abs_path, data)
            except OSError:
                log_exc()
            real = [i for i in data["hablantes"].values() if not i.get("breve")]
            known = sum(1 for i in real if i.get("persona") and not i.get("provisional"))
            job.status, job.progress = "done", 1.0
            job.stage = (f"Voces: {len(real)} {'persona' if len(real) == 1 else 'personas'} · {known} "
                         f"reconocida{'s' if known != 1 else ''}")
            log(f"✔ {job.name}  {job.stage.lower()} ({time.time() - job.started:.0f} s)")
            self.schedule_outputs(job.path)  # destinos de salida: el documento con quien habla (y participantes)
        except engines.Cancelled:
            job.status, job.stage = "cancelled", "Cancelado"
            log(f"■ {job.name}  voces canceladas")
        except Exception as e:
            job.status, job.stage, job.error = "error", "Error", str(e) or e.__class__.__name__
            log(f"✖ {job.name}  voces: {job.error}")
            if not isinstance(e, (engines.EngineError, ApiError, voices.VoicesError)):
                log_exc()
        finally:
            job.finished = time.time()
            if restore:  # el subproceso pudo dejar un sidecar nuevo sin lo que el usuario ya habia dicho
                try:
                    voices.write_sidecar(*restore)
                except OSError:
                    log_exc()
            self.library.at = 0
            if job.then_summary:
                self.auto_summary(job.path)

    def cancel(self, job_id):
        queued_pass = None
        with self.lock:
            for j in self.list:
                if j.id == job_id and j.status in ("queued", "running"):
                    j.cancel = True
                    if j.status == "queued":
                        j.status, j.stage, j.finished = "cancelled", "Cancelado", time.time()
                        queued_pass = j.path if getattr(j, "final_pass", False) else None
                    else:
                        j.stage = "Cancelando…"
                    break
            else:
                return False
        if queued_pass:  # el repaso final en cola: se queda el texto del directo (los destinos y el resumen, con el)
            threading.Thread(target=self._final_pass_over, args=(queued_pass, True), daemon=True,
                             name="repaso-cancelado").start()
        return True

    def clear(self):
        with self.lock:
            self.list = [j for j in self.list if j.status in ("queued", "running")]

    def snapshot(self):
        with self.lock:
            return [j.public() for j in self.list[-60:]]

    def _worker(self, kind):
        q = self.queues[kind]
        while True:
            job = q.get()
            if job.status != "queued":
                continue
            self._hold(job.path)  # hasta que termine con lo que encadene (voces, resumen): la reunion esta ocupada
            try:
                if job.kind == "summary":
                    self._run_summary(job)
                elif job.kind == "voces":
                    self._run_voices(job)
                else:
                    self._run(job)
            except Exception as e:
                log_exc()
                job.status, job.stage, job.error = "error", "Error", str(e) or e.__class__.__name__
                job.finished = job.finished or time.time()
            finally:
                self._release(job.path)

    def _run(self, job):
        job.status, job.started, job.stage = "running", time.time(), "Empezando…"
        preset = engines.PRESETS_BY_ID[job.preset]
        st = self.settings.get()
        log(f"▶ {job.name}  [{preset['label']}]")

        def report(frac=None, stage=None, transcribing=False):
            if transcribing and job.t_transcribe is None:
                job.t_transcribe = time.time()
            if stage is not None:
                job.stage = ("Repaso final · " if job.final_pass else "") + stage
            if frac is not None:
                job.progress = max(job.progress, frac)
                if job.t_transcribe and job.progress > 0.03:
                    el = time.time() - job.t_transcribe
                    base = 0.12 if job.engine == "cloud" else 0.0
                    done = (job.progress - base) / (1 - base)
                    if done > 0.02:
                        job.eta = round(el * (1 - done) / done)

        try:
            abs_path = safe(job.path, "file")
            a = self.library.find(job.path)
            start = float(job.resume_from or 0)
            total = (a or {}).get("duration") or engines.media_duration(abs_path)
            job.duration = max(1.0, total - start) if total else None  # lo que queda por transcribir
            if start:
                log(f"   continuando desde {start / 60:.1f} min")
            ms = meeting_setup(abs_path, st)  # vocabulario del tema (o el general), contexto libre e idioma del .md
            vocab, about = ms["prompt"], ms["about"]
            # el repaso final usa el idioma que tenga la reunion al terminar (pudo cambiarse); en lote, «el de cada
            # reunion»: el de su .md o el del perfil
            if job.final_pass or getattr(job, "per_meeting", False):
                job.language = ms["language"] or None
            if job.final_pass and getattr(job, "idioma_repaso", None) is not None:  # cambió en pleno directo
                job.language = job.idioma_repaso or None
            if preset["engine"] == "cloud":
                self._cloud_ready()  # ApiError si la nube se apago en Ajustes mientras esperaba en la cola
            if preset["engine"] == "local":
                segs, lang, dur = self.local.transcribe(abs_path, preset, job.language, vocab,
                                                        int(st.get("threads") or default_threads()),
                                                        report, lambda: job.cancel, start=start, about=about)
            else:
                # el repaso final de un directo que cambió de idioma: cada tramo con el suyo (job.language queda para
                # un tramo sin idioma propio, el primero si el directo empezó detectando)
                segs, lang, dur = self.cloud.transcribe(abs_path, job.duration, st["cloud"], job.language,
                                                        vocab, job.id, report, lambda: job.cancel,
                                                        start=start, about=about, tramos_idioma=getattr(
                                                            job, "idiomas_tramos", None) if job.final_pass else None)
            if job.cancel:
                raise engines.Cancelled()
            if start:
                # se anade detras de lo que ya habia (incluidas las correcciones a mano, que se respetan)
                base = os.path.splitext(abs_path)[0]
                old = [(s["start"], s["end"], s["text"]) for s in parse_srt(read_text(base + ".srt"))]
                new = [s for s in segs if s[1] > start + 0.3]
                write_outputs(abs_path, old + new)
                if not new:
                    log("   en lo que faltaba no hay voz: se da por completa")
            else:
                if not segs:
                    raise engines.EngineError("No se ha detectado voz en el audio.")
                write_outputs(abs_path, segs)
                shutil.rmtree(backup_dir(job.path), ignore_errors=True)  # el "original" es ahora este
            # en la nube solo si lo fue este trabajo y, al continuar una a medias, tambien lo de antes
            self._cloud_text().set(job.path, job.engine == "cloud" and (not start or job.path in self._cloud_text()))
            COMPLETE.set(job.path)  # aunque el texto acabe antes que el audio, esta completa
            if job.final_pass:
                self._final_pass_over(job.path)
            self._after_transcribe(job.path)
            job.duration, job.detected = dur or job.duration, lang
            job.status, job.progress, job.stage, job.eta = "done", 1.0, "Hecho", 0
            el = time.time() - (job.t_transcribe or job.started)
            if job.duration and el > 1:
                speed = job.duration / el

                def upd(d):
                    old = d.setdefault("speed", {}).get(job.preset)
                    d["speed"][job.preset] = round(speed if not old else 0.5 * old + 0.5 * speed, 2)
                self.settings.update(upd)
            log(f"✔ {job.name}  {(time.time() - job.started) / 60:.1f} min"
                + (f"  ({job.duration / max(el, 1):.1f}x tiempo real)" if job.duration else ""))
        except engines.Cancelled:
            job.status, job.stage = "cancelled", "Cancelado"
            log(f"■ {job.name}  cancelado")
            if job.final_pass:
                if getattr(self, "closing", False):  # al apagar: sigue pendiente y se retoma al volver a abrir
                    log("   el repaso final queda pendiente para el próximo arranque")
                else:  # cancelado a mano: se queda el texto del directo, y con el los destinos y el resumen
                    self._final_pass_over(job.path, keep_live_text=True)
        except Exception as e:
            job.status, job.stage, job.error = "error", "Error", str(e) or e.__class__.__name__
            log(f"✖ {job.name}  {job.error}")
            if not isinstance(e, (engines.EngineError, ApiError)):
                log_exc()
            if job.final_pass:  # se queda el texto del directo, y con el se resume
                self._aprendido(job.path)  # como en _after_transcribe, antes de soltar la reunion (_final_pass_over)
                self._final_pass_over(job.path)
                log("   se mantiene el texto del directo")
                self.write_outputs(job.path)
                self.auto_summary(job.path)
        finally:
            job.finished = time.time()
            self.library.at = 0


def borrar_pcm_voces(pid):
    """El audio decodificado de un subproceso de voces (voices.main lo borra al acabar, pero uno cancelado o que muere
    no llega a su finally: ~115 MB por hora de audio hasta el siguiente arranque; revisión de fix-subir-memoria)."""
    d = os.path.join(DATA_DIR, "tmp")
    try:
        nombres = [e.name for e in os.scandir(d) if e.name.startswith(f"voces-{pid}-") and e.name.endswith(".pcm")]
    except OSError:
        return
    for n in nombres:
        try:
            os.remove(os.path.join(d, n))
        except OSError:
            pass
