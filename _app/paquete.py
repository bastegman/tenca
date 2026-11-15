# -*- coding: utf-8 -*-
"""Compartir y Acerca de: el zip instalable para otro PC (build_package), la version que lleva, la licencia, los
enlaces del repo y de donaciones, y abrir una carpeta en el Explorador.

Salio tal cual de server.py al partirlo (15/11/2026): App hereda de esta clase y server.py la reexporta."""
import os
import re
import subprocess
import sys
import time

from aviso import WINDOWS
from common import APP_NAME, read_text
from rutas import APP_DIR, ROOT, STATIC_DIR, version_file


REPO_URL = "https://github.com/bastegman/tenca"  # el repo se llamaba «chuleta» (GitHub redirige)
# Acerca de > «invitar al autor a un café»: la pagina de donaciones de PayPal del autor (abre el navegador del sistema)
DONAR_URL = "https://www.paypal.com/donate?business=bdo.st.f%40gmail.com&currency_code=EUR"
PKG_ROOT = APP_NAME  # carpeta raiz del zip instalable (Tenca/INSTALAR.bat, Tenca/_app/...)

# Acerca de > el tributo a la tenca (Mimus thenca): «Historia natural y biologia reproductiva de la Tenca (Mimus thenca)
# en Chile central», Boletin del Museo Nacional de Historia Natural de Chile 61: 43-53 (2012). El DOI es del Museo
# (prefijo 10.54830 en Crossref) y sobrevive a un cambio de su web
TENCA_URL = "https://doi.org/10.54830/bmnhn.v61.2012.161"


EXPORT_DIR = os.path.join(APP_DIR, "export")  # INSTALAR.bat, ABRIR TENCA.bat y LEEME.txt (van a la raiz)


def export_code():
    """Todo el codigo de _app (.py, .bat y .ps1; sin subcarpetas ni python.txt): un modulo nuevo entra solo."""
    return sorted(n for n in os.listdir(APP_DIR)
                  if n.endswith((".py", ".bat", ".ps1")) and os.path.isfile(os.path.join(APP_DIR, n))
                  and not de_un_nombre_anterior(n))


def de_un_nombre_anterior(name):
    """Un fichero que la app tenia con un nombre anterior (el «ABRIR ….bat» de la raiz, el .bat de _app y los
    iconos). El instalador no borra nada de _app al actualizar, asi que en una copia instalada encima de Chuleta siguen
    ahi; el paquete de «Compartir» no los lleva (saldria el nombre viejo, y dos lanzadores). notas.js no es uno: es la
    funcion de tomar notas."""
    from common import APP_FORMER_NAMES
    viejos = {x.casefold() for n in APP_FORMER_NAMES
              for x in (f"ABRIR {n}.bat", f"{n}.bat", f"{n}.ico", f"{n}.png")}
    return name.casefold() in viejos


def desktop_dir():
    if WINDOWS:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0 and os.path.isdir(buf.value):
            return buf.value  # CSIDL_DESKTOPDIRECTORY (puede estar dentro de OneDrive)
    return os.path.join(os.path.expanduser("~"), "Desktop")


def package_version():
    """La version que lleva un zip generado desde esta copia: la de su _app/version.txt si lo tiene (una copia
    instalada que vuelve a exportar conserva la suya); si no, la fecha de hoy. scripts/build_zip.py la calcula del
    commit y la pasa explicitamente."""
    return version_file() or time.strftime("%Y-%m-%d")


def build_package(dest, extra=(), version=None):
    """Zip con la app lista para instalar en otro PC. Lleva solo el codigo, el instalador, el LEEME, la licencia y
    _app/version.txt: ni grabaciones, ni notas, ni ajustes, ni la API key (eso vive en DATA_DIR, que no se toca).
    extra: [(archivo, ruta en el zip)] opcional, p. ej. las listas de vocabulario por workstream.
    version: la cadena de _app/version.txt («AAAA-MM-DD · sha»), que sustituye tambien al {FECHA} del LEEME."""
    import zipfile
    version = (version or "").strip() or package_version()
    root = PKG_ROOT + "/"

    def text(path, keep_marker=False):  # .bat/.ps1 con CRLF; BOM en .ps1 (PowerShell 5 lo leeria como ANSI) y .txt
        t = read_text(path).replace("\r\n", "\n").replace("\n", "\r\n")
        if not keep_marker:
            t = t.replace("{FECHA}", version)
        return (("\ufeff" if path.endswith((".ps1", ".txt")) else "") + t).encode("utf-8")

    files = [(os.path.join(APP_DIR, n), root + "_app/" + n) for n in export_code()]
    files += [(e.path, root + "_app/static/" + e.name) for e in os.scandir(STATIC_DIR)
              if e.is_file() and not de_un_nombre_anterior(e.name)]
    templates = [e for e in os.scandir(EXPORT_DIR) if e.is_file() and not de_un_nombre_anterior(e.name)]
    files += [(e.path, root + e.name) for e in templates]
    # las plantillas van tambien dentro de _app/export (con su {FECHA}): la copia instalada podra volver a exportar
    files += [(e.path, root + "_app/export/" + e.name) for e in templates]
    lic = os.path.join(os.path.dirname(APP_DIR), "LICENSE")  # MIT: el aviso acompana a cada copia
    if os.path.isfile(lic):
        files.append((lic, root + "LICENSE"))
    files += list(extra)
    tmp = dest + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for src, arc in sorted(files, key=lambda f: f[1]):
            if src.endswith((".bat", ".ps1", ".txt")):
                z.writestr(arc, text(src, keep_marker=arc.startswith(root + "_app/export/")))
            else:
                z.write(src, arc)
        z.writestr(root + "_app/version.txt", (version + "\r\n").encode("utf-8"))  # server.py lo ensena en Ajustes
    os.replace(tmp, dest)
    return len(files) + 1


def license_name():
    """Nombre de la licencia segun la primera linea de LICENSE («MIT License» -> «MIT»); vacio si no hay fichero."""
    try:
        first = read_text(os.path.join(ROOT, "LICENSE")).strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""
    return re.sub(r"\s+licen[cs]e$", "", first, flags=re.I)


def open_dir(path):
    """Abre una carpeta en el Explorador (o el Finder / el gestor de archivos)."""
    if WINDOWS:
        subprocess.Popen(["explorer", path])
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
