# -*- coding: utf-8 -*-
"""Rutas de la app y sus carpetas: APP_DIR, ROOT y STATIC_DIR, las carpetas vinculadas (LINKS, ":<id>/…") y las de
listas conectadas (VOCAB_LINKS, ":v<id>/…"), safe() y rel_of(), las extensiones que se reconocen y la version del
codigo. Es la base de los demas modulos de _app: no importa ninguno salvo common.

Salio tal cual de server.py al partirlo (15/11/2026); server.py lo reexporta. LINKS, VOCAB_LINKS y ALIASES son los
mismos objetos en los dos (nunca se reasignan, solo se vacian y rellenan)."""
import hashlib
import os
import threading
import time
import unicodedata

from common import APP_NAME, ApiError, read_text


APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
ROOT = os.path.dirname(APP_DIR)
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "AudiosApp")


def root_name():
    """Como se llama de verdad la carpeta de la app (ROOT) en el disco: «Tenca» en una instalacion nueva y «Chuleta» en
    una instalada encima de Chuleta, que no renombra la carpeta de las reuniones (decision 1 del renombrado,
    09/11/2026). Es lo que se ensena como raiz de una ruta («Chuleta\\Proyecto Alfa\\…», lo que se ve en el
    Explorador) y el nombre del proyecto de las listas de la raiz, como el de cualquier otro proyecto es el de su
    carpeta. Se calcula cada vez: los tests cambian ROOT."""
    return unicodedata.normalize("NFC", os.path.basename(os.path.normpath(ROOT)) or ROOT.rstrip("\\/") or APP_NAME)


# los del movil y de los mensajeros tambien (09/11/2026, «Subir audio»; todos probados con el PyAV de la app): .oga de
# Telegram, .amr/.3gp/.3gpp de los Android antiguos, .3ga de grabadoras de llamadas, .caf/.qta/.aif(f) de Apple y el
# .waptt de los WhatsApp antiguos (un Ogg Opus con otro nombre)
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".aac", ".wma", ".opus", ".mkv", ".mov", ".webm",
             ".oga", ".amr", ".3gp", ".3gpp", ".3ga", ".caf", ".qta", ".aif", ".aiff", ".waptt"}
TEXT_EXT = {".txt", ".md", ".srt"}


MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "video/mp4",
        ".flac": "audio/flac", ".ogg": "audio/ogg", ".opus": "audio/ogg", ".aac": "audio/aac",
        ".webm": "audio/webm", ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".wma": "audio/x-ms-wma",
        ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".oga": "audio/ogg", ".waptt": "audio/ogg", ".amr": "audio/amr", ".3gp": "audio/3gpp",
        ".3gpp": "audio/3gpp", ".3ga": "audio/3gpp", ".caf": "audio/x-caf", ".qta": "video/quicktime",
        ".aif": "audio/aiff", ".aiff": "audio/aiff",
        ".woff2": "font/woff2",  # las tipografías de la marca (static/*.woff2)
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}
# Atributos de OneDrive "solo en linea": leer el archivo dispararia la descarga
CLOUD_ONLY = 0x400000 | 0x1000


VERSION_FILE = os.path.join(APP_DIR, "version.txt")  # lo escribe scripts/build_zip.py: «AAAA-MM-DD · sha corto»


def code_mtime():
    """Fecha del codigo: cambia si se actualiza la app (la ventana abierta lo ve y se recarga sola)."""
    files = [os.path.join(APP_DIR, n) for n in os.listdir(APP_DIR) if n.endswith(".py")]
    files += [os.path.join(STATIC_DIR, n) for n in os.listdir(STATIC_DIR)]
    return max(os.path.getmtime(f) for f in files)


def version_file():
    """Primera linea de _app/version.txt («2026-10-31 · abc1234», la escribe scripts/build_zip.py y viaja en el zip),
    o None si esta copia no la tiene (una copia de trabajo del repositorio)."""
    try:
        v = read_text(VERSION_FILE).strip().splitlines()[0].strip()
        return v or None
    except (OSError, IndexError):
        return None


def read_version():
    """La version que se ensena en Ajustes > Acerca de: la del zip si existe; si no, fecha y hora del codigo."""
    return version_file() or time.strftime("%Y-%m-%d %H:%M", time.localtime(code_mtime()))


BUILD = format(int(code_mtime()), "x")  # recarga automatica de la ventana al actualizar: sigue basada en el mtime
VERSION = read_version()


# Carpetas vinculadas: cualquier carpeta del PC (p. ej. la del proyecto en OneDrive) que se usa como
# si estuviera dentro de la carpeta de la app. Sus rutas internas empiezan por ":<id>" (":" no puede ir en un nombre
# de archivo de Windows, asi que nunca choca con una carpeta de verdad).
LINK = ":"
LINKS = {}  # id -> {"name", "path"} (se cargan de los ajustes al arrancar)
# Reuniones movidas en esta sesion (ruta interna vieja -> nueva): una peticion que llegue con la vieja (la pagina que
# la tenia abierta, unas notas que se guardan justo al moverla) va a la nueva (alias_of, en Handler)
ALIASES, ALIASES_LOCK = {}, threading.Lock()


def alias_of(rel):
    """La ruta nueva de una reunion movida en esta sesion si `rel` ya no existe; si no, `rel` tal cual."""
    if not isinstance(rel, str) or not rel:
        return rel
    with ALIASES_LOCK:
        new = ALIASES.get(rel.replace("\\", "/").strip("/"))
    if not new:
        return rel
    try:
        if os.path.exists(safe(rel)):  # en la ruta vieja vuelve a haber algo: manda lo que hay
            return rel
    except ApiError:
        pass
    return new


# Carpetas de listas conectadas (vocabulario desde una carpeta, msg 1148): sus listas .txt se leen en vivo, sin copiar
# nada. Van aparte de LINKS: no salen en el arbol de reuniones ni como destino de grabacion, y safe() no las resuelve
# (ahi no se graba, ni se mueve, ni se transcribe nada). Las rutas internas de sus listas empiezan por ":v<id>" (el id
# de LINKS es hexadecimal: nunca empieza por "v", asi que no chocan).
VLINK = LINK + "v"
VOCAB_LINKS = {}  # id -> {"name", "path"} (ajuste "vocab_links"; se cargan al arrancar)


def link_id(path):
    return hashlib.md5(os.path.normcase(os.path.normpath(path)).encode("utf-8")).hexdigest()[:6]


def _inside(p, base):
    p, base = os.path.normcase(os.path.normpath(p)), os.path.normcase(os.path.normpath(base))
    return p == base or p.startswith(base.rstrip(os.sep) + os.sep)


def rel_of(abs_path):
    for lid, l in LINKS.items():
        if _inside(abs_path, l["path"]):
            rel = os.path.relpath(abs_path, l["path"])
            return LINK + lid + ("" if rel == "." else "/" + rel.replace(os.sep, "/"))
    if VOCAB_LINKS and not _inside(abs_path, ROOT):  # una lista de una carpeta conectada
        for vid, l in list(VOCAB_LINKS.items()):
            if _inside(abs_path, l["path"]):
                rel = os.path.relpath(abs_path, l["path"])
                return VLINK + vid + ("" if rel == "." else "/" + rel.replace(os.sep, "/"))
    rel = os.path.relpath(abs_path, ROOT)
    return "" if rel == "." else rel.replace(os.sep, "/")


def vocab_link_path(rel):
    """Ruta absoluta de `rel` (":v<id>/…") dentro de su carpeta de listas conectada (para abrirla en el Explorador)."""
    rel = (rel or "").replace("\\", "/").strip("/")
    vid, _, rest = rel[len(VLINK):].partition("/")
    l = VOCAB_LINKS.get(vid) if rel.startswith(VLINK) else None
    if not l:
        raise ApiError("Esa carpeta de listas ya no está conectada.", 404)
    p = os.path.normpath(os.path.join(l["path"], rest))
    if not _inside(p, l["path"]):
        raise ApiError(f"Ruta fuera de las carpetas de {APP_NAME}", 403)
    return p


def is_link_root(rel):
    return (rel or "").startswith(LINK) and "/" not in rel.strip("/")


def safe(rel, must=None):
    """Ruta absoluta dentro de la carpeta de la app o de una carpeta vinculada (no deja salir de ahi)."""
    rel = (rel or "").replace("\\", "/").strip("/")
    base = ROOT
    if rel.startswith(LINK):
        lid, _, rel = rel[len(LINK):].partition("/")
        if lid not in LINKS:
            raise ApiError(f"Esa carpeta ya no está vinculada a {APP_NAME}", 404)
        base = LINKS[lid]["path"]
    p = os.path.normpath(os.path.join(base, rel))
    if not _inside(p, base):
        raise ApiError(f"Ruta fuera de las carpetas de {APP_NAME}", 403)
    if must == "file" and not os.path.isfile(p):
        raise ApiError("No existe el archivo: " + rel, 404)
    if must == "dir" and not os.path.isdir(p):
        raise ApiError("No existe la carpeta: " + rel, 404)
    return p


def in_app_dir(p):
    a = os.path.normcase(APP_DIR)
    return os.path.normcase(p) == a or os.path.normcase(p).startswith(a + os.sep)


def in_app_tree(p):
    """p esta en una carpeta de la app: la de su codigo, su raiz o una carpeta vinculada (donde viven las grabaciones).
    Ahi no se escriben documentos de los destinos de salida."""
    return in_app_dir(p) or _inside(p, ROOT) or any(_inside(p, l["path"]) for l in LINKS.values())
