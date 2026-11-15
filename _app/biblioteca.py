# -*- coding: utf-8 -*-
"""La biblioteca de reuniones: el arbol de carpetas (scan, Library), las duraciones, las transcripciones (Transcripts)
y sus ficheros (.txt, .srt), las notas .md (parse_notes, dump_notes), los temas, los movimientos pendientes, las
carpetas por reunion y los destinos de la traduccion.

Salio tal cual de server.py al partirlo (15/11/2026); server.py lo reexporta. COMPLETE lo crea App() al arrancar:
server.py lo copia aqui (server.COMPARTIDAS). DATA_DIR, igual (lo fija main())."""
import hashlib
import json
import os
import re
import threading
import time

import engines
import live
import voices
from ajustes import moved_rel
from common import (APP_NAME, SRT_TIME, ApiError, fmt_srt, fold, log, log_exc, natural_key, parse_srt, read_text,
                    save_text, wav_duration)
from rutas import AUDIO_EXT, CLOUD_ONLY, DATA_DIR, LINK, LINKS, ROOT, TEXT_EXT, in_app_dir, is_link_root, safe


# lo que acompana a cada audio (se mueve y borra con el): texto, subtitulos, notas, resumen, hablantes y su exportacion
SIDE_EXTS = (".txt", ".srt", ".md", ".resumen.md", voices.SIDECAR_EXT, ".hablantes.txt")


DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ _-](\d{2})-(\d{2})-(\d{2})")


class Durations:
    def __init__(self, path):
        self.path, self.lock, self.dirty = path, threading.Lock(), False
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.cache = json.load(f)
        except (OSError, ValueError):
            self.cache = {}

    def has(self, rel, size, mtime):
        with self.lock:
            return f"{rel}|{size}|{int(mtime)}" in self.cache

    def get(self, abs_path, rel, size, mtime, cloud, srt_abs=None, compute=True):
        key = f"{rel}|{size}|{int(mtime)}"
        with self.lock:
            if key in self.cache:
                return self.cache[key]
        if not compute:
            return None
        d = None
        if cloud:
            # no tocamos el audio (OneDrive lo descargaria); usamos el final del .srt si lo hay
            if srt_abs:
                try:
                    segs = parse_srt(read_text(srt_abs))
                    d = segs[-1]["end"] if segs else None
                except OSError:
                    d = None
        elif abs_path.lower().endswith(".wav"):
            d = wav_duration(abs_path, size)
        else:
            d = engines.media_duration(abs_path)
        if d:
            d = round(d, 1)
            with self.lock:
                self.cache[key] = d
                self.dirty = True
        return d

    def flush(self):
        with self.lock:
            if not self.dirty:
                return
            data, self.dirty = dict(self.cache), False
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, self.path)


def scan(dirs=None):
    """Arbol de carpetas con audios (+ estado de transcripcion) y documentos sueltos.
    dirs: si se pasa un dict, se rellena con carpeta -> mtime_ns (Library lo usa para saber si hay que volver a
    escanear: la fecha de una carpeta cambia con cada alta, baja o renombrado dentro de ella)."""
    flat = []
    mias = carpetas_reunion().keys()  # las carpetas de reunion que creo la app (rutas internas en normcase)

    def walk(abs_dir, rel, depth=0, max_depth=99):
        node = {"name": os.path.basename(abs_dir) if rel else APP_NAME, "path": rel,
                "folders": [], "audios": [], "docs": []}
        try:
            if dirs is not None:
                dirs[abs_dir] = os.stat(abs_dir).st_mtime_ns
            entries = list(os.scandir(abs_dir))
        except OSError:
            return node
        files = {}
        for e in entries:
            if e.name.startswith((".", "~$")):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    # las carpetas Vocabulario no son reuniones ni documentos: las lee list_vocab. La de una reunion
                    # se mira aunque este un nivel por debajo del tope (la principal puede estar en el ultimo)
                    sub = f"{rel}/{e.name}" if rel else e.name
                    if not in_app_dir(e.path) and fold(e.name) != VOCAB_FOLDER and (
                            depth < max_depth or os.path.normcase(sub) in mias):
                        node["folders"].append(walk(e.path, sub, depth + 1, max_depth))
                elif e.is_file():
                    files[e.name.lower()] = e
            except OSError:
                continue
        audio_stems = {os.path.splitext(k)[0] for k in files if os.path.splitext(k)[1] in AUDIO_EXT}
        for low, e in files.items():
            stem_low, ext = os.path.splitext(low)
            frel = f"{rel}/{e.name}" if rel else e.name
            try:
                st = e.stat()
            except OSError:
                continue
            cloud = bool(getattr(st, "st_file_attributes", 0) & CLOUD_ONLY)
            if ext in AUDIO_EXT:
                stem = os.path.splitext(e.name)[0]
                txt, srt = files.get(stem_low + ".txt"), files.get(stem_low + ".srt")
                m = DATE_RE.search(stem)
                label = ""
                if m:
                    # el prefijo de las grabadoras y los móviles (AUDIO_, VID-, REC…) va delante de la fecha; el
                    # nombre que pone la app, detrás: solo se quita delante. Antes se quitaba de todo, y «… - Audio
                    # de WhatsApp» quedaba en «de WhatsApp» y «Recursos humanos», en «ursos humanos» (09/11/2026)
                    antes = re.sub(r"^(AUDIO|VID|REC)$", "", stem[:m.start()].strip(" -_·"), flags=re.I)  # solo
                    antes = re.sub(r"^(AUDIO|VID|REC)[-_ ]+", "", antes)  # delante de un nombre, en mayúsculas
                    label = (antes + " " + stem[m.end():]).strip(" -_·")
                    label = re.sub(r"^_?\d{2,6}$", "", label)
                tx_mtime = 0
                for t in (txt, srt):
                    try:
                        tx_mtime = max(tx_mtime, t.stat().st_mtime) if t else tx_mtime
                    except OSError:
                        pass
                summ = files.get(stem_low + ".resumen.md")  # resumen con IA
                md = files.get(stem_low + ".md")  # notas y propiedades de la reunion
                spk = files.get(stem_low + voices.SIDECAR_EXT)  # quien habla
                try:
                    md_mtime = md.stat().st_mtime if md else 0
                    spk_mtime = spk.stat().st_mtime if spk else 0
                except OSError:
                    md_mtime, spk_mtime = 0, 0
                a = {"name": e.name, "path": frel, "stem": stem, "ext": ext, "size": st.st_size,
                     "mtime": st.st_mtime, "cloud": cloud, "txt": bool(txt), "srt": bool(srt), "tx_mtime": tx_mtime,
                     "date": "{}-{}-{}T{}:{}:{}".format(*m.groups()) if m else None,
                     "label": label, "folder": rel, "notes_mtime": md_mtime, "has_summary": bool(summ),
                     "spk_mtime": spk_mtime,
                     "_abs": e.path, "_srt": srt.path if srt else None, "_md": md.path if md else None,
                     "_sum": summ.path if summ else None, "_spk": spk.path if spk else None}
                node["audios"].append(a)
                flat.append(a)
            elif ext in TEXT_EXT and stem_low not in audio_stems and not any(
                    low.endswith(x) and low[:-len(x)] in audio_stems for x in (".resumen.md", ".hablantes.txt")):
                node["docs"].append({"name": e.name, "path": frel, "ext": ext, "size": st.st_size,
                                     "mtime": st.st_mtime, "cloud": cloud})
        node["folders"].sort(key=lambda n: natural_key(n["name"]))
        node["audios"].sort(key=lambda a: (a["date"] or "", natural_key(a["name"])))
        node["docs"].sort(key=lambda d: natural_key(d["name"]))
        # la carpeta de una reunion (la creo la app) con solo ella dentro: la interfaz la pinta como la reunion
        if rel and os.path.normcase(rel) in mias and not is_link_root(rel):
            node["mia"] = True  # la creo la app para una reunion (la interfaz: «Se guarda en», donde esta)
            if not node["folders"] and not node["docs"] and len(node["audios"]) == 1:
                node["por_reunion"] = True
        return node

    tree = walk(ROOT, "")
    linked = []
    for lid, l in LINKS.items():  # arriba del todo, antes que las carpetas de dentro de Audios
        node = walk(l["path"], LINK + lid, max_depth=LINK_DEPTH)
        node.update(name=l["name"], linked=l["path"], missing=not os.path.isdir(l["path"]))
        linked.append(node)
    tree["folders"][:0] = sorted(linked, key=lambda n: natural_key(n["name"]))
    return tree, flat


LINK_DEPTH = 4  # en una carpeta de proyecto grande no se baja mas (el arbol se repasa cada pocos segundos)


class Library:
    """El arbol de reuniones, con cache. Un sondeo (cada 6 s desde la pagina) no vuelve a recorrer el disco si ninguna
    carpeta ha cambiado de fecha (un stat por carpeta) y ninguna grabacion abierta ha crecido; `at = 0` (lo ponen
    los endpoints que escriben) fuerza el escaneo completo. Cada FULL_RESCAN_S se repasa todo de todas formas, en
    segundo plano, por si un .md o .srt se edito desde fuera (eso no toca la fecha de la carpeta)."""

    FULL_RESCAN_S = 30
    RECENT_S = 2.0  # una carpeta modificada hace menos de esto se da por cambiada (discos con fechas de 1-2 s)

    def __init__(self, durations):
        self.durations = durations
        self.lock = threading.Lock()
        self.tree, self.flat, self.version, self.at = None, [], "", 0
        self.public = None  # el arbol sin campos privados, listo para /api/tree (se calcula una vez por escaneo)
        self.pending_durations = False
        self.growth = {}  # ruta -> (tamano, cuando cambio): OBS no actualiza la fecha mientras graba
        self.ends = {}    # .srt -> (fecha, segundo en que acaba): para detectar transcripciones a medias
        self.notes = {}   # .md -> (fecha, propiedades, tiene texto)
        self.speakers = {}  # .hablantes.json -> (fecha, resumen {n, known, doubt, tu} o None si solo lleva el micro)
        self.dirs = {}    # carpeta -> mtime_ns en el ultimo escaneo
        self.watch = {}   # grabaciones abiertas (cambian sin tocar su carpeta): ruta -> (tamano, fecha)
        self.full_at, self.full_wall, self.clock = 0.0, 0.0, time.time  # clock: los tests ponen un reloj falso
        self.rescan_thread = None
        self.scans = 0    # escaneos completos (para los tests y el banco)

    def _props(self, a):
        p = a["_md"]
        if not p:
            return {}, False
        hit = self.notes.get(p)
        if hit and hit[0] == a["notes_mtime"]:
            return hit[1], hit[2]
        try:
            props, body = parse_notes(read_text(p))
        except OSError:
            props, body = {}, ""
        self.notes[p] = (a["notes_mtime"], props, bool(body.strip()))
        return props, bool(body.strip())

    def _speakers(self, a):
        """Resumen del sidecar de voces (con cache): None si no hay diarizacion (p. ej. solo la serie del micro)."""
        p = a.get("_spk")
        if not p:
            return None
        hit = self.speakers.get(p)
        if hit and hit[0] == a["spk_mtime"]:
            return hit[1]
        info = None
        try:
            d = json.loads(read_text(p))
            hs = d.get("hablantes") or {}
            if d.get("tramos") and hs:
                real = [i for i in hs.values() if not i.get("breve")]
                info = {"n": len(real), "known": sum(1 for i in real if i.get("persona") and not i.get("provisional")),
                        "doubt": sum(1 for i in real if i.get("persona") and i.get("provisional")),
                        "tu": bool(d.get("tu")),
                        # sin nombre: ni persona ni «Tú» (Hoy › Para revisar, «2 voces sin nombre»)
                        "sin_nombre": sum(1 for h, i in hs.items() if not i.get("breve") and not i.get("persona")
                                          and h != d.get("tu"))}
        except (OSError, ValueError, AttributeError):
            info = None
        self.speakers[p] = (a["spk_mtime"], info)
        return info

    def _tx_end(self, a):
        """Hasta que segundo llega la transcripcion (lee solo el final del .srt, con cache)."""
        p = a["_srt"]
        if not p:
            return None
        hit = self.ends.get(p)
        if hit and hit[0] == a["tx_mtime"]:
            return hit[1]
        end = None
        try:
            with open(p, "rb") as f:
                f.seek(max(0, os.path.getsize(p) - 4096))
                times = SRT_TIME.findall(f.read().decode("utf-8", "ignore"))
            if times:
                g = times[-1]
                end = round(int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7].ljust(3, "0")) / 1000, 1)
        except OSError:
            pass
        self.ends[p] = (a["tx_mtime"], end)
        return end

    def _recording(self, a):
        """True/duracion si es un WAV de OBS aun abierto (cabecera a 0xFFFFFFFF) y creciendo."""
        if a["ext"] != ".wav" or a["cloud"] or self.durations.has(a["path"], a["size"], a["mtime"]):
            return None
        info = live.wav_info(a["_abs"])
        if not info or info["data_size"] is not None:
            return None
        prev = self.growth.get(a["path"])
        if not prev or prev[0] != a["size"]:
            # la primera vez que se ve (p. ej. al arrancar), lo ultimo que crecio fue en su fecha de modificacion: un
            # WAV que OBS dejo abierto hace dias no es una grabacion en curso (02/11/2026: arrancaba un directo, y un
            # repaso final en la nube, con cada arranque)
            self.growth[a["path"]] = (a["size"], time.time() if prev else min(time.time(), a["mtime"]))
        a["recording"] = time.time() - self.growth[a["path"]][1] < live.STALLED_S
        return round(max(0, a["size"] - info["data_off"]) / info["byte_rate"], 1)

    def _changed(self):
        """True si algo ha podido cambiar en disco desde el ultimo escaneo completo: la fecha de alguna carpeta (alta,
        baja o renombrado dentro), o el tamano/fecha de una grabacion abierta. Un stat por carpeta: milisegundos."""
        for d, m in self.dirs.items():
            try:
                st = os.stat(d)
            except OSError:
                return True
            if st.st_mtime_ns != m or st.st_mtime > self.full_wall - self.RECENT_S:
                return True
        for p, (size, mtime) in self.watch.items():
            try:
                st = os.stat(p)
            except OSError:
                return True
            if st.st_size != size or st.st_mtime != mtime:
                return True
        return False

    def _rescan(self, now):
        """Escaneo completo (con self.lock cogido)."""
        dirs = {}
        tree, flat = scan(dirs)
        missing, watch = False, {}
        for a in flat:
            a["recording"] = False
            a["tx_end"] = self._tx_end(a)
            props, a["has_notes"] = self._props(a)
            a["props"] = with_theme_id(props, a["path"])  # con el id de su tema en este PC (la cache no lo lleva)
            a["speakers"] = self._speakers(a)
            a["has_speakers"] = bool(a["speakers"])
            open_dur = self._recording(a)
            if open_dur is not None:  # sin cachear: cambia cada pocos segundos
                a["duration"] = open_dur
                a["partial"] = False
                watch[a["_abs"]] = (a["size"], a["mtime"])
                continue
            # duraciones: las rapidas (cabecera WAV) ya; las demas (PyAV) en segundo plano
            quick = a["ext"] == ".wav" or a["cloud"]
            a["duration"] = self.durations.get(a["_abs"], a["path"], a["size"], a["mtime"], a["cloud"],
                                               a["_srt"], compute=quick)
            missing |= a["duration"] is None and not quick
            a["partial"] = is_partial(a)
        self.durations.flush()
        public = strip_private(tree)
        payload = json.dumps(public, ensure_ascii=False, sort_keys=True)
        self.tree, self.flat, self.public, self.at = tree, flat, public, now
        self.version = hashlib.md5(payload.encode("utf-8")).hexdigest()[:12]
        self.dirs, self.watch, self.full_at, self.full_wall = dirs, watch, now, time.time()
        self.scans += 1
        if missing and not self.pending_durations:
            self.pending_durations = True
            threading.Thread(target=self._fill_durations, daemon=True).start()

    def _rescan_later(self):
        """Escaneo completo en segundo plano (el sondeo que lo pide se lleva el arbol que ya hay, sin esperar)."""
        if self.rescan_thread is not None and self.rescan_thread.is_alive():
            return

        def run():
            with self.lock:
                self._rescan(self.clock())
        self.rescan_thread = threading.Thread(target=run, daemon=True, name="library-rescan")
        self.rescan_thread.start()

    def _refresh_locked(self, max_age):
        now = self.clock()
        if self.tree is not None and now - self.at < max_age:
            return
        if self.tree is not None and self.at and not self._changed():
            self.at = now  # nada ha cambiado: el mismo arbol
            if now - self.full_at >= self.FULL_RESCAN_S:
                self._rescan_later()
            return
        self._rescan(now)

    def refresh(self, max_age=0.0):
        with self.lock:
            self._refresh_locked(max_age)
            return self.tree, self.version

    def snapshot(self, max_age=0.0):
        """(arbol publico, version) para /api/tree, coherentes entre si."""
        with self.lock:
            self._refresh_locked(max_age)
            return self.public, self.version

    def _fill_durations(self):
        try:
            for a in list(self.flat):
                if a.get("duration") is None and not a["cloud"]:
                    self.durations.get(a["_abs"], a["path"], a["size"], a["mtime"], False, a["_srt"])
            self.durations.flush()
        finally:
            self.pending_durations = False
        with self.lock:
            self.at = 0  # fuerza re-escaneo en la proxima peticion

    def find(self, rel):
        self.refresh(max_age=5)
        for a in self.flat:
            if a["path"] == rel:
                return a
        return None


CLOUD_TEXT_LOCK = threading.Lock()


class CloudText:
    """Reuniones cuyo texto actual se transcribio entero en la nube: en la cola con la nube, en directo con todos sus
    trozos en la nube o con el repaso final. El resumen automatico solo sube a la nube estas sin el permiso de Ajustes
    (summary_auto_local). Lo apunta cada transcripcion al escribir el texto (Jobs._cloud_text) y vive en DATA_DIR, asi
    que no depende del trabajo que acaba de terminar ni del arranque: un repaso final retomado tras reiniciar que falla
    o se cancela deja el texto del directo, y cuenta lo que fue el directo. Lo que no esta cuenta como local. Sigue a la
    reunion si se mueve o se renombra (ella o su carpeta) y se borra con ella (moved, set), y cada entrada lleva la
    identidad del audio (ident): si en esa ruta hay ahora otro archivo (se movio o se borro fuera de la app y otra
    reunion llego con ese nombre), no cuenta. Caduca a los 7 dias (el resumen automatico, para las de los ultimos 3)."""

    KEEP_S = 7 * 86400

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.data = {str(k): self._entry(v) for k, v in json.load(f).items()}
        except (OSError, ValueError, TypeError, AttributeError, IndexError):
            self.data = {}

    @staticmethod
    def _entry(v):
        """[cuando, identidad del audio]; un numero suelto, sin identidad (0)."""
        t, ino = (v[0], v[1]) if isinstance(v, list) else (v, 0)
        return [float(t), int(ino)]

    @staticmethod
    def ident(rel):
        """La identidad del audio: st_ino, el indice del archivo en NTFS, que se conserva al moverlo o renombrarlo en la
        misma unidad y es otro en un archivo nuevo; 0 si no se sabe. Una entrada apuntada sin identidad vale por la
        ruta; una con identidad, solo si el audio que hay ahora en esa ruta es el mismo (__contains__)."""
        try:
            return os.stat(safe(rel, "file")).st_ino
        except (ApiError, OSError):
            return 0

    def __contains__(self, rel):
        e = self.data.get(rel)
        return (e is not None and time.time() - e[0] < self.KEEP_S  # caduca al consultar, no solo al escribir
                and (not e[1] or e[1] == self.ident(rel)))  # y tiene que ser el mismo audio

    def set(self, rel, cloud):
        now = time.time()
        with self.lock:
            data = {k: e for k, e in self.data.items() if k != rel and now - e[0] < self.KEEP_S}
            if cloud:
                data[rel] = [now, self.ident(rel)]
            self._save(data)

    def moved(self, old, new, folder=False):
        """La reunion `old` (con folder, la carpeta y lo que tiene dentro) pasa a ser `new`: lo apuntado va con ella, y
        lo que hubiera en la ruta nueva (de una reunion que ya no esta ahi) se olvida."""
        now = time.time()

        def dest(k):
            return new + k[len(old):] if k == old or (folder and k.startswith(old + "/")) else None
        with self.lock:
            vivas = {k: e for k, e in self.data.items() if now - e[0] < self.KEEP_S}
            data = {k: e for k, e in vivas.items()
                    if dest(k) is None and k != new and not (folder and k.startswith(new + "/"))}
            data.update({dest(k): e for k, e in vivas.items() if dest(k) is not None})
            self._save(data)

    def _save(self, data):
        """Con self.lock: los datos nuevos, en memoria y (si han cambiado) en disco."""
        if data == self.data:
            return
        self.data = data
        try:
            with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(self.path + ".tmp", self.path)
        except OSError as e:  # vale en memoria; sin el fichero, al reiniciar todo cuenta como local
            log(f"   no se pudo guardar {os.path.basename(self.path)} ({e.__class__.__name__})")
            try:
                os.remove(self.path)
            except OSError:
                pass


class Complete:
    """Audios cuya transcripcion se sabe completa aunque el texto acabe antes que el audio
    (p. ej. se siguio grabando en silencio al final). Asi no se marcan "a medias"."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.data = set(json.load(f))
        except (OSError, ValueError):
            self.data = set()

    def __contains__(self, rel):
        return rel in self.data

    def _save(self):
        with open(self.path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(sorted(self.data), f, ensure_ascii=False)
        os.replace(self.path + ".tmp", self.path)

    def set(self, rel, done=True):
        with self.lock:
            if done == (rel in self.data):
                return
            (self.data.add if done else self.data.discard)(rel)
            self._save()

    def moved(self, old, new):
        if old in self:
            self.set(old, False)
            self.set(new, True)


COMPLETE = None  # se crea al arrancar (necesita DATA_DIR)


class Temas:
    """El tema de cada reunion por el id de su lista (ruta interna del audio -> id de list_vocab), en este PC: con dos
    proyectos que se llaman igual, los nombres de la cabecera del .md (proyecto y workstream, que leen todas las
    versiones) no dicen cual es. No va en la cabecera: las versiones anteriores solo aceptan ahi las claves que
    conocen y, con una nueva, toman la cabecera entera por notas. El id solo vale mientras su lista se llame como dice
    la cabecera (si no, otra version u otro PC cambio el tema y mandan los nombres: find_vocab)."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        self.data = {k: v for k, v in data.items() if isinstance(v, str) and v} if isinstance(data, dict) else {}

    def get(self, rel):
        return self.data.get(rel)

    def _save(self):
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, self.path)

    def set(self, rel, vid):
        with self.lock:
            if self.data.get(rel) == (vid or None):
                return
            if vid:
                self.data[rel] = vid
            else:
                self.data.pop(rel, None)
            self._save()

    def moved(self, old, new, folder=False):
        """Una reunion (o, con folder, una carpeta) cambia de ruta: su tema la sigue; con folder, tambien el id de las
        listas que estan dentro."""
        with self.lock:
            out = {}
            for k, v in self.data.items():
                k2 = moved_rel(k, old, new, folder)
                v2 = moved_rel(v, old, new, folder=True) if folder else None
                out[k if k2 is None else k2] = v if v2 is None else v2
            if out != self.data:
                self.data = out
                self._save()


class Movimientos:
    """Reuniones que se moveran a otra carpeta en cuanto la app deje de escribir en ellas (cambiar la carpeta en pleno
    directo, msg 1198): ruta interna del audio -> {"to": la carpeta principal elegida, "at": cuando, y tras un intento
    fallido "target" (la carpeta de la reunion ya elegida: el siguiente va a la misma), "intentos", "next" y "error"}.
    En DATA_DIR/movimientos_pendientes.json: si la app se cierra antes, se hace al volver a abrirla."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        self.data = {k: v for k, v in data.items()
                     if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("to"), str)} \
            if isinstance(data, dict) else {}

    def _save(self):
        tmp = f"{self.path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError as e:  # vale en memoria; sin el fichero, un cierre de la app lo olvida
            log(f"   no se pudo guardar {os.path.basename(self.path)} ({e.__class__.__name__})")

    def get(self, rel):
        with self.lock:
            e = self.data.get(rel)
            return dict(e) if e else None

    def items(self):
        with self.lock:
            return [(k, dict(v)) for k, v in self.data.items()]

    def set(self, rel, entry):
        with self.lock:
            self.data[rel] = dict(entry)
            self._save()

    def pop(self, rel):
        with self.lock:
            e = self.data.pop(rel, None)
            if e is not None:
                self._save()
            return e

    def moved(self, old, new, folder=False):
        """Una carpeta (o una reunion) cambia de ruta: lo apuntado la sigue, la reunion y la carpeta de destino."""
        with self.lock:
            out = {}
            for k, v in self.data.items():
                k2 = moved_rel(k, old, new, folder)
                v = dict(v)
                for f in ("to", "target"):
                    m = moved_rel(v.get(f), old, new, folder=True)
                    if m is not None:
                        v[f] = m
                out[k if k2 is None else k2] = v
            if out != self.data:
                self.data = out
                self._save()


class CarpetasReunion:
    """Las carpetas de reunion que ha creado la app (carpeta por reunion), por su ruta interna. Solo estas son «suyas»
    (own_meeting_dir): una carpeta tuya que por casualidad se llama «AAAA-MM-DD Nombre», como las que algunos ya se
    hacian a mano, no se mueve ni se renombra con la reunion. En DATA_DIR/carpetas_reunion.json; siguen a los
    renombrados y movimientos que hace la app (moved). Sin el (otro PC, reinstalacion), la reunion se mueve como una
    de antes: sus archivos."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = []
        self.data = {os.path.normcase(r): r for r in data if isinstance(r, str) and r} if isinstance(data, list) else {}

    def _save(self):
        tmp = f"{self.path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(sorted(self.data.values()), f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            log(f"   no se pudo guardar {os.path.basename(self.path)} ({e.__class__.__name__})")

    def __contains__(self, rel):
        return isinstance(rel, str) and os.path.normcase(rel) in self.data

    def keys(self):
        with self.lock:
            return set(self.data)

    def add(self, rel):
        with self.lock:
            if self.data.get(os.path.normcase(rel)) != rel:
                self.data[os.path.normcase(rel)] = rel
                self._save()

    def discard(self, rel):
        with self.lock:
            if self.data.pop(os.path.normcase(rel or ""), None) is not None:
                self._save()

    def moved(self, old, new, folder=False):
        with self.lock:
            out = {}
            for r in self.data.values():
                m = moved_rel(r, old, new, folder)
                r = r if m is None else m
                out[os.path.normcase(r)] = r
            if out != self.data:
                self.data = out
                self._save()


_CARPETAS, _CARPETAS_LOCK = {}, threading.Lock()


def carpetas_reunion():
    """El registro CarpetasReunion de este DATA_DIR (los tests lo cambian)."""
    path = os.path.join(DATA_DIR, "carpetas_reunion.json")
    with _CARPETAS_LOCK:
        if path not in _CARPETAS:
            _CARPETAS[path] = CarpetasReunion(path)
        return _CARPETAS[path]


class TraduccionDestino:
    """Ultimo destino de la traduccion del directo («es» o «en») elegido en cada reunion (ruta interna del audio), en
    DATA_DIR/traduccion_destino.json: si la app se reinicia a mitad de reunion, o la apagas y la vuelves a encender,
    sigue al mismo idioma. No va en la cabecera del .md: las versiones anteriores toman una clave que no conocen por
    notas (Temas). Solo se usa durante el directo, asi que no sigue a la reunion si se mueve despues. Guarda las
    MAX ultimas."""

    MAX = 300

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        self.data = ({k: v for k, v in data.items() if isinstance(k, str) and v in live.TRANSLATE_TO}
                     if isinstance(data, dict) else {})

    def get(self, rel):
        return self.data.get(rel)

    def set(self, rel, to):
        if to not in live.TRANSLATE_TO:
            return
        with self.lock:
            if self.data.get(rel) == to and list(self.data)[-1:] == [rel]:
                return
            self.data.pop(rel, None)
            self.data[rel] = to  # al final: los mas recientes son los que se quedan
            while len(self.data) > self.MAX:
                self.data.pop(next(iter(self.data)))
            try:
                tmp = f"{self.path}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False)
                os.replace(tmp, self.path)
            except OSError:  # no poder recordarlo no impide traducir
                log_exc()


_TRAD, _TRAD_LOCK = {}, threading.Lock()


def traduccion_destinos():
    """El almacen TraduccionDestino de este PC (uno por DATA_DIR, que los tests cambian)."""
    path = os.path.join(DATA_DIR, "traduccion_destino.json")
    with _TRAD_LOCK:
        if path not in _TRAD:
            _TRAD[path] = TraduccionDestino(path)
        return _TRAD[path]


def traduccion_destino(rel, st):
    """A que idioma se traduce el directo de una reunion: el ultimo elegido en ella; si no, el idioma del resumen de
    Ajustes si es español o inglés; si no («el de la reunión»), español (msg 1152)."""
    to = traduccion_destinos().get(rel)
    if to in live.TRANSLATE_TO:
        return to
    lang = (st or {}).get("summary_lang") or "es"
    return lang if lang in live.TRANSLATE_TO else "es"


_TEMAS, _TEMAS_LOCK = {}, threading.Lock()


def temas():
    """El almacen Temas de este PC (DATA_DIR/temas.json; uno por DATA_DIR, que los tests cambian)."""
    path = os.path.join(DATA_DIR, "temas.json")
    with _TEMAS_LOCK:
        if path not in _TEMAS:
            _TEMAS[path] = Temas(path)
        return _TEMAS[path]


def is_partial(a):
    """Transcripcion que se quedo a medias: el .srt acaba bastante antes que el audio."""
    d, end = a.get("duration"), a.get("tx_end")
    return bool(a.get("srt") and d and end is not None and not a.get("recording") and d - end > max(60, 0.05 * d)
                and not (COMPLETE and a["path"] in COMPLETE))


def strip_private(node):
    # carpetas vinculadas (fuera de Audios) y la carpeta de una reunion (por_reunion, scan)
    extra = {k: node[k] for k in ("linked", "missing", "por_reunion", "mia") if k in node}
    return {"name": node["name"], "path": node["path"], **extra,
            "folders": [strip_private(f) for f in node["folders"]],
            "audios": [{k: v for k, v in a.items() if not k.startswith("_")} for a in node["audios"]],
            "docs": node["docs"]}


class Transcripts:
    """Lee .srt / .txt con cache por fecha de modificacion (para la busqueda global)."""

    def __init__(self):
        self.cache, self.lock = {}, threading.Lock()

    def load(self, base_abs):
        for ext in (".srt", ".txt"):
            p = base_abs + ext
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            with self.lock:
                hit = self.cache.get(p)
            if hit and hit[0] == mtime:
                return hit[1]
            text = read_text(p)
            if ext == ".srt":
                data = {"kind": "srt", "segments": parse_srt(text), "file": os.path.basename(p)}
            else:
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                data = {"kind": "txt", "segments": [{"start": None, "end": None, "text": l} for l in lines],
                        "file": os.path.basename(p)}
            for s in data["segments"]:
                s["_f"] = fold(s["text"])
            with self.lock:
                self.cache[p] = (mtime, data)
            return data
        return None

    def doc_lines(self, abs_path):
        try:
            mtime = os.path.getmtime(abs_path)
        except OSError:
            return []
        with self.lock:
            hit = self.cache.get(abs_path)
        if hit and hit[0] == mtime:
            return hit[1]
        lines = [l.strip() for l in read_text(abs_path).splitlines() if l.strip()]
        data = [(l, fold(l)) for l in lines]
        with self.lock:
            self.cache[abs_path] = (mtime, data)
        return data

    def warm(self, library):
        """Precarga todas las transcripciones para que la primera busqueda sea instantanea."""
        for a in list(library.flat):
            if a["txt"] or a["srt"]:
                try:
                    self.load(os.path.splitext(a["_abs"])[0])
                except OSError:
                    pass


def public_transcript(data):
    if not data:
        return {"kind": None, "segments": []}
    return {"kind": data["kind"], "file": data["file"],
            "segments": [{k: v for k, v in s.items() if k != "_f"} for s in data["segments"]]}


# <audio>.md junto al audio: cabecera con propiedades (como una pagina de base de datos de Notion)
# y debajo las notas en Markdown. Se mueve con el audio igual que el .txt y el .srt.
# idioma: codigo ("es", "pt", "es-en"…) o "auto" (detectar); sin la clave, el idioma del perfil (Ajustes).
# contexto: una o dos frases (de que va, quien participa) que reciben Whisper, tras el vocabulario, y el resumen.
# El id de la lista del tema no va aqui sino en temas() (ver Temas): las versiones anteriores solo aceptan las claves
# que conocen y, con una nueva, toman la cabecera entera por notas.
# participantes_origen: "outlook" o "teams" mientras los participantes son los que sugirio la reunion en curso al
# grabar y el usuario no los ha confirmado ni editado; mientras tanto no salen del PC (participantes_confirmados). En
# el .md no va como clave propia sino detras de los nombres (PARTICIPANTES_NOTA), por lo mismo que el id del tema.
NOTE_KEYS = ("titulo", "icono", "etiquetas", "estado", "participantes", "participantes_origen", "proyecto",
             "workstream", "idioma", "contexto",
             "interrumpida")  # «hh:mm · motivo»: la grabadora se corto (o Tenca se cerro) sin que nadie la parase
IDIOMA_RE = re.compile(r"^(auto|[a-z]{2}(-[a-z]{2})?)$")
NOTE_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*$")  # una clave de la cabecera (las que no conocemos se conservan)
ICON_BAD = re.compile(r"[<>&\"'\x00-\x1f\x7f]")
ICON_MAX = 8  # code points: cabe cualquier emoji compuesto (banderas, tonos de piel, familias con ZWJ)


def clean_icon(v):
    """El icono de una reunion es un emoji o un texto corto (como mucho 8 code points) sin < > & " ' ni controles; si
    no, ninguno. El .md puede venir de una carpeta vinculada compartida (OneDrive, SharePoint) y la interfaz pinta el
    icono como HTML: un valor con marcado ejecutaria codigo en la app (P0-2)."""
    v = " ".join(str(v or "").split())
    return v if len(v) <= ICON_MAX and not ICON_BAD.search(v) else ""


PARTICIPANTES_ORIGENES = ("outlook", "teams")
# Los textos de «Participantes» que la app ha escrito desde Outlook/Teams en esta sesion (calendario.join_participants:
# «Apellido, Nombre; …»). Solo con ellos la pista de quien habla cuenta cada trozo entre «;» como una persona
# (voices.count_participants sugeridos=True), tambien despues de «Confirmar» (que quita participantes_origen sin cambiar
# el texto). Editado a mano (en la app o fuera), el texto ya no esta aqui y se cuenta como texto libre, de mas. Solo en
# memoria: tras reiniciar la app, tambien como texto libre.
TEXTOS_SUGERIDOS = set()
# «Ana, Luis (sugeridos por Outlook, sin confirmar)»: asi las versiones publicadas (012bb24, 5c55e54), que solo
# aceptan las claves que conocen, leen la cabecera y ensenan los nombres con la nota. La clave participantes_origen
# (como la escribia fix-privacidad antes de fusionarse con fix-perfil) se sigue leyendo; al guardar pasa a la nota.
PARTICIPANTES_NOTA = " (sugeridos por {}, sin confirmar)"
PARTICIPANTES_NOMBRE = {"outlook": "Outlook", "teams": "Teams"}
_PARTICIPANTES_NOTA_RE = re.compile(r"^(.*\S)\s*\(sugeridos por (Outlook|Teams), sin confirmar\)$")


def participantes_del_md(v):
    """(nombres, origen) de la linea «participantes» del .md: con la nota del final, su origen; sin ella, None."""
    m = _PARTICIPANTES_NOTA_RE.match(v or "")
    return (m.group(1), m.group(2).lower()) if m else (v, None)


def participantes_confirmados(props):
    """Los participantes que pueden ir al resumen en la nube y a los destinos de salida: los escritos o confirmados
    por el usuario. Los sugeridos por Outlook/Teams (con participantes_origen) se quedan en el .md hasta entonces."""
    props = props or {}
    return "" if props.get("participantes_origen") else (props.get("participantes") or "")


def parse_notes(text):
    lines = text.replace("\r\n", "\n").split("\n")
    props, body = {}, text
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        # es cabecera si todo lo de dentro es "clave: valor" y alguna clave es nuestra (si no, era un separador). Las
        # claves que no conocemos (las de una version mas nueva) se conservan tal cual: asi esta version no entierra
        # en las notas la cabecera que escriba otra
        keys = [l.partition(":")[0].strip().lower() for l in lines[1:end] if l.strip()] if end else []
        if end and all(":" in l for l in lines[1:end] if l.strip()) and all(NOTE_KEY_RE.match(k) for k in keys) \
                and (not keys or any(k in NOTE_KEYS for k in keys)):
            for line in lines[1:end]:
                k, _, v = line.partition(":")
                k, v = k.strip().lower(), v.strip()
                if k == "icono":
                    v = clean_icon(v)
                elif k == "participantes":
                    v, origen = participantes_del_md(v)
                    if origen:
                        props["participantes_origen"] = origen
                elif k == "participantes_origen" and v not in PARTICIPANTES_ORIGENES:
                    v = ""  # como en notes_save: solo outlook o teams
                if k in NOTE_KEYS and v:
                    props[k] = [t.strip() for t in v.strip("[]").split(",") if t.strip()] if k == "etiquetas" else v
                elif v and k not in NOTE_KEYS:  # de una version mas nueva: se conserva tal cual (dump_notes la escribe)
                    props[k] = v
            body = "\n".join(lines[end + 1:]).lstrip("\n")
    return props, body


def dump_notes(props, body):
    head = []
    for k in list(NOTE_KEYS) + [k for k in props if k not in NOTE_KEYS and NOTE_KEY_RE.match(str(k))]:
        v = props.get(k)
        if v and k != "participantes_origen":  # el origen va detras de los participantes (PARTICIPANTES_NOTA)
            v = "[" + ", ".join(v) + "]" if k == "etiquetas" else " ".join(str(v).split())
            if k == "participantes" and props.get("participantes_origen") in PARTICIPANTES_NOMBRE:
                v += PARTICIPANTES_NOTA.format(PARTICIPANTES_NOMBRE[props["participantes_origen"]])
            head.append(f"{k}: {v}")
    out = ("---\n" + "\n".join(head) + "\n---\n\n" if head else "") + (body or "").strip("\n")
    return out + "\n" if out.strip() else ""


# <proyecto>/Vocabulario/<workstream>.txt: terminos separados por comas (las lineas que empiezan por #
# son la descripcion). Al grabar se elige el workstream: queda en la cabecera del .md de la reunion
# (proyecto + workstream) y Whisper recibe ese vocabulario en vez del general de Ajustes.
VOCAB_FOLDER = "vocabulario"


def with_theme_id(props, rel):
    """Las propiedades de una reunion para la interfaz: con `vocabulario`, el id de la lista de su tema en este PC
    (temas()), aunque no este en el .md. La interfaz lo usa para marcar la lista elegida (vocabOf en notas.js)."""
    vid = temas().get(rel) if (props or {}).get("workstream") else None
    return dict(props, vocabulario=vid) if vid else props


def srt_block(i, a, b, t):
    return f"{i}\n{fmt_srt(a)} --> {fmt_srt(b)}\n{t}\n"


def write_outputs(audio_abs, segs):
    base = os.path.splitext(audio_abs)[0]
    txt = "\n".join(t for _, _, t in segs) + "\n"
    srt = "\n".join(srt_block(i, a, b, t) for i, (a, b, t) in enumerate(segs, 1))
    # utf-8-sig (con BOM) para que Bloc de notas / Excel muestren bien los acentos
    save_text(base + ".txt", txt, "utf-8-sig")
    save_text(base + ".srt", srt, "utf-8")


def seg_key(start):
    """Identifica una frase por su inicio tal como queda en el .srt (milisegundos, 2 decimales al leerlo)."""
    return round(int(round(float(start) * 1000)) / 1000, 2)


def apply_edits(segs, edits):
    """Correcciones hechas a mano durante el directo ("" = frase borrada) sobre lo que da Whisper."""
    if not edits:
        return segs
    out = []
    for a, b, t in segs:
        t = edits.get(seg_key(a), t)
        if t:
            out.append((a, b, t))
    return out
