# -*- coding: utf-8 -*-
"""Las carpetas de las reuniones: la carpeta principal de cada proyecto y la de cada reunion (nombre, tope de ruta de
Windows, destino al grabar o al subir), mover archivos con sus acompanantes, la papelera y las copias de los
originales (backup_dir, save_original).

Salio tal cual de server.py al partirlo (15/11/2026); server.py lo reexporta. DATA_DIR lo fija main() al arrancar:
server.py lo copia aqui (server.COMPARTIDAS)."""
import errno
import hashlib
import json
import os
import re
import shutil
import time

import calendario
import plan as grabar_plan
import voices
from ajustes import moved_rel
from biblioteca import carpetas_reunion, write_outputs
from common import WINDOWS, ApiError, clean_name, fold, log, log_exc
from listas import _list_vocab, _norm_dir, _of_project, _project_dir, list_vocab, perfil_folder
from rutas import (AUDIO_EXT, DATA_DIR, LINKS, MARCA_SIN_AUDIO, ROOT, VOCAB_LINKS, _inside, in_app_dir, rel_of,
                   root_name, safe)


# Donde se graba (B1b y B2b): una carpeta por reunion, «AAAA-MM-DD Nombre», dentro de la carpeta principal de su
# workstream o de su proyecto. La principal se asigna (workstream_folders / project_folders); sin asignar, es la carpeta
# del proyecto en el arbol (la que tiene su carpeta Vocabulario). Sin tema: la fija del perfil y, si no, la ultima
# usada. Lo grabado antes se queda donde esta: solo lo nuevo va en su carpeta.
MEETING_DIR_MAX = 60  # caracteres del nombre de la reunion en el de su carpeta
MEETING_DIR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (.+?)(?: \((\d{1,3})\))?$")
WIN_PATH_MAX = 250  # rutas de Windows sin «rutas largas»: 260 con el terminador; se deja margen


def _en_carpeta_de_listas(d):
    """`d` esta en una carpeta de listas conectada: de las carpetas que la contienen (la raiz, las vinculadas y las de
    listas), la mas interior es de listas. Una vinculada dentro de una de listas es del arbol (el usuario la vinculo
    para grabar); una de listas dentro de una vinculada, no."""
    best = (-1, False)
    for path, listas in ([(ROOT, False)] + [(l["path"], False) for l in LINKS.values()]
                         + [(l["path"], True) for l in VOCAB_LINKS.values()]):
        n = len(os.path.normcase(os.path.normpath(path)))
        if n > best[0] and _inside(d, path):
            best = (n, listas)
    return best[1]


def project_home(v, vs=None):
    """Ruta interna de la carpeta del proyecto de una lista (la que contiene su carpeta Vocabulario), si esta en el
    arbol de la app (su raiz o una vinculada); None si no. Una carpeta de vocabulario conectada nunca es destino de
    grabacion (VLINK), tampoco si queda dentro de una vinculada (_en_carpeta_de_listas): una lista de alli usa la
    carpeta de su proyecto en el arbol, la de otra lista suya que si este (`vs`: las de list_vocab, si ya se tienen),
    y si no hay, ninguna."""
    if not v or not v.get("_abs"):
        return None
    d = _project_dir(v)
    if v.get("conectada") or _en_carpeta_de_listas(d):
        for o in (list_vocab() if vs is None else vs):
            if not o.get("conectada") and o.get("_abs") and _of_project(o, v["project"]):
                home = project_home(o, ())
                if home is not None:
                    return home
        return None
    if in_app_dir(d) or not (_inside(d, ROOT) or any(_inside(d, l["path"]) for l in LINKS.values())):
        return None
    return rel_of(d)


def _usable_dir(rel):
    """`rel` es una carpeta que existe en el arbol y donde se puede grabar."""
    if not isinstance(rel, str):
        return False
    try:
        return not in_app_dir(safe(rel, "dir"))
    except ApiError:
        return False


def rec_folder_origin(v, st, vs=None):
    """(carpeta principal, origen) de una grabacion nueva con el tema `v` (una lista de list_vocab, o None): la asignada
    a su workstream («workstream»), la asignada a su proyecto («proyecto») o la carpeta del proyecto en el arbol
    («carpeta_proyecto»); sin tema (o si ninguna existe ya), la fija del perfil («perfil»), la ultima usada («ultimo») o
    la raiz («raiz»). Es la unica regla: la usan Ctrl+Alt+R, el aviso de Teams, el dialogo y /api/perfil. `vs`: las
    listas de list_vocab, si ya se tienen (project_home)."""
    if v:  # en orden y solo hasta la primera que sirva (project_home de una lista conectada recorre las listas)
        for origin, get in (("workstream", lambda: (st.get("workstream_folders") or {}).get(v.get("id"))),
                            ("proyecto", lambda: (st.get("project_folders") or {}).get(v.get("project"))),
                            ("carpeta_proyecto", lambda: project_home(v, vs))):
            rel = get()
            if rel is not None and _usable_dir(rel):
                return rel, origin
    fija = perfil_folder(st)
    if fija is not None:
        return fija, "perfil"
    rel = st.get("rec_folder")
    if rel and _usable_dir(rel):
        return rel, "ultimo"
    return "", "raiz"


def project_folders_resolved(st):
    """proyecto -> carpeta principal de cada proyecto con listas (la asignada o la suya en el arbol), para la
    interfaz. Solo con las listas del arbol: las de una carpeta conectada no tienen carpeta propia (project_home) y la
    asignada a su proyecto sale igual en el segundo paso. Asi /api/config no lee las carpetas conectadas, que pueden
    estar en la red o sin sincronizar (revision de la integracion del 08/11/2026)."""
    out, vs = {}, _list_vocab(conectadas=False)
    for v in vs:
        if v["project"] not in out:
            # sin id: la del proyecto, no la de un workstream
            rel, origin = rec_folder_origin(dict(v, id=None), st, vs)
            if origin in ("proyecto", "carpeta_proyecto"):
                out[v["project"]] = rel
    for name, rel in (st.get("project_folders") or {}).items():  # asignadas a un proyecto sin listas
        if name not in out and _usable_dir(rel):
            out[name] = rel
    return out


def por_reunion(st):
    """El interruptor general de la carpeta por reunion (encendido por defecto)."""
    return st.get("carpeta_por_reunion", True) is not False


def meeting_dir_name(label="", when=None, limit=MEETING_DIR_MAX):
    """Nombre de la carpeta de una reunion: «AAAA-MM-DD Nombre» o, sin nombre (o si no cabe), «AAAA-MM-DD HH-MM».
    `when`: segundos o un struct_time (por defecto, ahora)."""
    lt = time.localtime(when) if isinstance(when, (int, float)) else (when or time.localtime())
    name = calendario.nombre_archivo(label, limit=limit) if label and limit >= 12 else ""
    return time.strftime("%Y-%m-%d", lt) + " " + (name or time.strftime("%H-%M", lt))


# «Subir audio»: lo que se dice cuando no vale un archivo (la lista entera está en AUDIO_EXT)
FORMATOS_SUBIR = "m4a, mp3, opus, ogg, wav, aac, webm, flac, amr o un vídeo mp4 o mov"
SUBIR_FECHA_MIN = 946684800  # 01/01/2000: antes de eso, la fecha del archivo no es la de la reunión


def fecha_subida(v):
    """La fecha de un audio subido, en segundos: la de la tarjeta («AAAA-MM-DDTHH:MM», hora local) o la del archivo
    (File.lastModified del navegador, en milisegundos). Si no hay o no es razonable (antes de 2000 o de mañana en
    adelante), ahora."""
    ahora, t = time.time(), None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        t = float(v)
    elif isinstance(v, str) and v.strip():
        x = v.strip()
        try:
            t = float(x)
        except ValueError:
            for fmt, n in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%dT%H:%M", 16)):
                try:
                    t = time.mktime(time.strptime(x[:n], fmt))
                    break
                except (ValueError, OverflowError):
                    continue
    if t is not None and t > 1e11:  # milisegundos
        t /= 1000
    if t is None or t != t or not (SUBIR_FECHA_MIN <= t <= ahora + 86400):
        return ahora
    return t


SUBIENDO = ".subiendo-"  # lo que se está recibiendo en «Subir audio» (el árbol no enseña lo que empieza por punto)
SUBIENDO_VIEJO_S = 6 * 3600


def limpiar_subidas(folder):
    """Borra de `folder` lo que quedó a medias de una subida de hace horas (la app se cerró o el PC se apagó mientras
    llegaba): en Windows el punto no lo oculta del Explorador, y en OneDrive se sincronizaría."""
    try:
        viejo = time.time() - SUBIENDO_VIEJO_S
        for e in os.scandir(folder):
            if e.name.startswith(SUBIENDO) and e.is_file(follow_symlinks=False) and e.stat().st_mtime < viejo:
                try:
                    os.remove(e.path)
                    log(f"   borrado un resto de subida: {rel_of(e.path)}")
                except OSError:
                    pass
    except OSError:
        pass


def own_meeting_dir(abs_audio):
    """La carpeta de la reunion si es «suya»: la creo la app para ella (carpetas_reunion) y no hay otra grabacion en
    todo su arbol. Si no (lo grabado antes, una carpeta tuya, o una con otra reunion dentro), None: se mueve como una
    reunion de antes, sus archivos."""
    d = os.path.dirname(abs_audio)
    if in_app_dir(d) or _norm_dir(d) in {_norm_dir(ROOT)} | {_norm_dir(l["path"]) for l in LINKS.values()}:
        return None
    if rel_of(d) not in carpetas_reunion():
        return None
    n = 0
    try:
        for _, _, files in os.walk(d):
            audios = [os.path.splitext(f.lower()) for f in files if os.path.splitext(f)[1].lower() in AUDIO_EXT]
            # una reunion cuyo audio ya esta en la papelera (conservar.py) tambien cuenta: su marca hace de audio
            stems = {s for s, _ in audios}
            n += len(audios) + sum(1 for f in files if f.lower().endswith(MARCA_SIN_AUDIO)
                                   and f.lower()[:-len(MARCA_SIN_AUDIO)] not in stems)
            if n > 1:
                return None
    except OSError:
        return None
    return d if n == 1 else None


def free_dir(parent, name, mine=None):
    """Ruta para la carpeta `name` dentro de `parent`: esa si no existe, esta vacia o es `mine` (la propia); si no,
    «name (2)», «name (3)»…"""
    for n in range(1, 500):
        p = os.path.join(parent, name if n == 1 else f"{name} ({n})")
        if mine and _norm_dir(p) == _norm_dir(mine):
            return p
        try:
            if not os.path.exists(p) or (os.path.isdir(p) and not os.listdir(p)):
                return p
        except OSError:
            continue
    raise ApiError("Hay demasiadas carpetas con ese nombre: " + name)


def fit_meeting_dir(parent, label, when, stem):
    """El nombre de la carpeta de la reunion que cabe en una ruta de Windows con su archivo mas largo dentro (el
    .hablantes.json): se recorta el nombre de la reunion y, si ni asi, «AAAA-MM-DD HH-MM». None si ni eso cabe: la
    grabacion va a la carpeta principal, como antes de la carpeta por reunion."""
    longest = len(stem) + len(voices.SIDECAR_EXT) + 6  # «\», « (n)» y margen
    limit = MEETING_DIR_MAX
    while True:
        name = meeting_dir_name(label, when, limit)
        if not WINDOWS or len(os.path.join(parent, name)) + longest <= WIN_PATH_MAX:
            return name
        if limit < 12:
            return None
        limit -= 8


def meeting_dir_preview(folder_rel, label="", st=None, when=None):
    """Ruta interna de la carpeta que tendria ahora una reunion grabada en `folder_rel` (sin crearla), o la propia
    carpeta si la carpeta por reunion esta apagada. Para ensenar «Se guarda en …» antes de grabar."""
    if st is not None and not por_reunion(st):
        return folder_rel
    parent = safe(folder_rel, "dir")
    lt = time.localtime(when) if isinstance(when, (int, float)) else (when or time.localtime())
    stem = time.strftime("%Y-%m-%d %H-%M-%S", lt) + (f" - {label}" if label else "")
    name = fit_meeting_dir(parent, label, lt, stem)
    return rel_of(free_dir(parent, name)) if name else folder_rel


def rec_destino(v, st, name="", when=None):
    """Lo que se usara para guardar una grabacion nueva: {"carpeta": la principal, "origen": de donde sale (ver
    rec_folder_origin), "reunion": la carpeta que se creara para ella (o la principal sin carpeta por reunion),
    "por_reunion"}. Para la tarjeta de grabar, Ctrl+Alt+R y Ajustes (la misma regla que rec_start)."""
    rel, origin = rec_folder_origin(v, st)
    label = ""
    if name:
        try:
            label = clean_name(name)
        except ApiError:
            label = calendario.nombre_archivo(name)
    try:
        reunion = meeting_dir_preview(rel, label, st, when)
    except ApiError:
        reunion = rel
    return {"carpeta": rel, "origen": origin, "reunion": reunion, "por_reunion": por_reunion(st)}


def ruta_txt(rel):
    """Una ruta interna como la ve el usuario, igual en la tarjeta de Grabar, en el aviso de Ctrl+Alt+R, en el directo y
    en los avisos de mover: «Chuleta\\Proyecto Alfa\\2026-11-07 Comité\\», con la raiz por el nombre que tiene de
    verdad en el disco (root_name) y una vinculada por el suyo (plan.ruta_txt)."""
    return grabar_plan.ruta_txt(rel, {k: l["name"] for k, l in LINKS.items()}, root_name())


display_rel = ruta_txt  # el nombre de antes (rama de carpetas): era «\\Proyecto Alfa\\…», sin la raiz


def move_one(f, t):
    """Mueve un archivo o una carpeta: en la misma unidad, os.rename (de una vez; con algo abierto dentro, falla sin
    tocar nada). Entre unidades, copia y borra, y solo archivos: shutil.move copiaria una carpeta entera y la borraria
    a medias si algo de dentro esta abierto (en Windows), y eso no se puede deshacer bien."""
    try:
        os.rename(f, t)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    if os.path.isdir(f):
        raise ApiError("La carpeta de la reunión tiene subcarpetas y el destino está en otra unidad: muévela desde el "
                       "Explorador.")
    shutil.copy2(f, t)
    try:
        os.remove(f)
    except OSError as e:
        try:
            os.remove(t)
        except OSError:
            pass
        # sin reintentar aqui: cada reintento copiaria otra vez el audio entero a la otra unidad
        raise ApiError("El archivo está en uso (¿OneDrive sincronizando o abierto en otro programa?). Prueba otra vez "
                       f"en unos segundos ({e.__class__.__name__}).")


def move_files(moves):
    """Mueve cada (origen, destino) en orden (move_one), con reintentos si el archivo esta en uso un instante (el
    reproductor, OneDrive, el antivirus). Si uno no se puede, deshace los ya movidos y lanza ApiError: o se mueve todo o
    nada. Quien llama comprueba antes que ningun destino existe (en Windows y en el Mac os.rename no avisa igual)."""
    done = []
    try:
        for f, t in moves:
            if f == t:
                continue
            for attempt in range(12):
                try:
                    move_one(f, t)
                    break
                except PermissionError:
                    if attempt == 11:
                        raise
                    time.sleep(0.25)
            done.append((f, t))
    except (OSError, ApiError) as e:
        incompleto = False
        for f, t in reversed(done):  # deshacer, tambien con reintentos (OneDrive coge lo recien llegado)
            for attempt in range(12):
                try:
                    move_one(t, f)
                    break
                except (OSError, ApiError):
                    if attempt == 11:
                        log_exc()
                        incompleto = True
                    else:
                        time.sleep(0.25)
        if isinstance(e, ApiError):
            err = e
        elif isinstance(e, PermissionError):
            err = ApiError("El archivo está en uso (¿OneDrive sincronizando o abierto en otro programa?). "
                           "Prueba otra vez en unos segundos.")
        else:
            err = ApiError(f"No se ha podido mover ({e.__class__.__name__}).")
        err.incompleto = incompleto  # algo se quedo en el destino: el siguiente intento termina alli
        raise err


def theme_moved(props, vid, new, lists, project, ambiguous=False, inside=False):
    """Lo que cambia en la cabecera de una reunion cuando la carpeta de un proyecto (o la vinculada) pasa de llamarse
    project[0] a project[1] (`new`: su ruta interna, ya renombrada): si la cabecera dice el nombre viejo, el nuevo,
    siempre que la reunion sea de ese proyecto: el id de la lista de su tema (`vid`, de temas(), que ya lo ha seguido)
    esta dentro o, sin id, ningun otro proyecto se sigue llamando asi (`ambiguous` False) o la reunion esta dentro
    (`inside`). `lists`: id -> lista, ya renombrada. Solo cambia el valor de `proyecto`, que todas las versiones leen;
    {} si nada cambia."""
    if not project or not props.get("workstream") or fold(props.get("proyecto") or "") != fold(project[0]) \
            or props.get("proyecto") == project[1]:
        return {}
    v = lists.get(vid) if vid else None
    if v and fold(v["name"]) == fold(props["workstream"]):  # el id de su tema dice de que proyecto es
        return {"proyecto": project[1]} if moved_rel(vid, new, new, folder=True) is not None else {}
    return {"proyecto": project[1]} if inside or not ambiguous else {}


def to_recycle_bin(paths, aviso_firme=False):
    """A la papelera de Windows (se puede restaurar), no un borrado definitivo.
    Fuera de Windows (solo desarrollo) se mueven a DATA_DIR/papelera/<fecha>/. aviso_firme: si Windows fuera a borrarlo
    en firme en vez de mandarlo a la papelera (no cabe, unidad sin papelera), que pregunte en lugar de hacerlo callado
    (FOF_WANTNUKEWARNING): lo usa el borrado del audio de conservar.py, que ademas lo comprueba antes."""
    if not WINDOWS:
        dest = os.path.join(DATA_DIR, "papelera", time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(dest, exist_ok=True)
        for p in paths:
            shutil.move(p, os.path.join(dest, os.path.basename(p)))
        return
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_void_p),
                    ("pTo", ctypes.c_void_p), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_void_p)]

    buf = ctypes.create_unicode_buffer("\0".join(paths) + "\0\0")
    flags = 0x40 | 0x10 | 0x4 | 0x400 | (0x4000 if aviso_firme else 0)  # ALLOWUNDO|NOCONFIRMATION|SILENT|NOERRORUI
    op = SHFILEOPSTRUCTW(None, 3, ctypes.cast(buf, ctypes.c_void_p), None, flags, False, None, None)  # (|WANTNUKE)
    r = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    # Windows iba a borrarlo en firme y se le ha dicho que no (sin error: 0, o ERROR_CANCELLED); con un error de
    # verdad (en uso…), lo de siempre
    if aviso_firme and op.fAnyOperationsAborted and r in (0, 1223):
        raise ApiError("sin_papelera")
    if r != 0 or op.fAnyOperationsAborted:
        raise ApiError("No se pudo mandar a la papelera (¿archivo en uso?).")


def backup_dir(rel):
    """Copia de lo que transcribio Whisper antes de editarlo a mano (fuera de OneDrive)."""
    return os.path.join(DATA_DIR, "originales", hashlib.md5(rel.encode("utf-8")).hexdigest())


def save_original(rel, segs):
    """Guarda lo que transcribio Whisper (si aun no hay copia), para poder restaurarlo."""
    bdir = backup_dir(rel)
    if os.path.isdir(bdir):
        return
    os.makedirs(bdir)
    write_outputs(os.path.join(bdir, "original.wav"), segs)
    with open(os.path.join(bdir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"path": rel, "at": time.time()}, f, ensure_ascii=False)


def copia_original(rel, base, auto=False):
    """La copia de lo que transcribio Whisper (si aun no hay): la hacen la primera correccion a mano y el primer cambio
    que Tenca aprende de ti (auto). Devuelve si la ha hecho ahora."""
    bdir = backup_dir(rel)
    if os.path.isdir(bdir):
        return False
    os.makedirs(bdir)
    for ext in (".srt", ".txt"):
        if os.path.isfile(base + ext):
            shutil.copyfile(base + ext, os.path.join(bdir, "original" + ext))
    with open(os.path.join(bdir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(dict({"path": rel, "at": time.time()}, **({"auto": True} if auto else {})), f, ensure_ascii=False)
    return True
