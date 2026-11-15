# -*- coding: utf-8 -*-
"""Compartir y Acerca de: el zip instalable para otro PC (build_package), la version que lleva, la licencia, los
enlaces del repo y de donaciones, y abrir una carpeta en el Explorador.

Salio tal cual de server.py al partirlo (15/11/2026): App hereda de esta clase y server.py la reexporta."""
import os
import re
import subprocess
import sys
import time

from common import APP_NAME, WINDOWS, read_text
from rutas import APP_DIR, ROOT, STATIC_DIR, version_file


REPO_URL = "https://github.com/bastegman/tenca"  # el repo se llamaba «chuleta» (GitHub redirige)
# Acerca de > «invitar al autor a un café»: la pagina de donaciones de PayPal del autor (abre el navegador del sistema)
DONAR_URL = "https://www.paypal.com/donate?business=bdo.st.f%40gmail.com&currency_code=EUR"
PKG_ROOT = APP_NAME  # carpeta raiz del zip instalable (Tenca/INSTALAR.bat, Tenca/_app/...)

# Acerca de > el tributo a la tenca (Mimus thenca): «Historia natural y biologia reproductiva de la Tenca (Mimus thenca)
# en Chile central», Boletin del Museo Nacional de Historia Natural de Chile 61: 43-53 (2012). El DOI es del Museo
# (prefijo 10.54830 en Crossref) y sobrevive a un cambio de su web
TENCA_URL = "https://doi.org/10.54830/bmnhn.v61.2012.161"

# Acerca de > «Buscar versión nueva» (punto 60 del review del 14/11/2026): UNA peticion a la API de Releases de GitHub
# cuando se pulsa, nunca sola. Los Releases los crea scripts/publicar_release.py con el tag vAAAA.MM.DD-<sha corto> y
# la app lleva «AAAA-MM-DD · sha corto» en _app/version.txt (build_zip): se comparan el sha y, si no es el mismo, la
# fecha. Sin claves ni datos del usuario: solo un User-Agent con el nombre de la app (GitHub lo exige)
RELEASES_API = "https://api.github.com/repos/bastegman/tenca/releases/latest"
RELEASES_URL = REPO_URL + "/releases"
TAG_RE = re.compile(r"v(\d{4})\.(\d{2})\.(\d{2})-([0-9a-f]{7,40})")
VERSION_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?: · ([0-9a-f]{7,40}))?")
VERSION_TIMEOUT = (10, 15)  # s para conectar y para la respuesta


def comparar_version(tuya, tag):
    """«al_dia» (el mismo sha), «nueva» (la publicada es de un dia posterior), «posterior» (la tuya es de despues de
    la ultima publicada: un zip de main sin Release), «mismo_dia» (otra del mismo dia: no se sabe cual va antes) o
    «sin_version» (una copia de trabajo, sin version.txt: lleva fecha y hora del codigo). ValueError si el tag no es
    de los que crea publicar_release.py."""
    m = TAG_RE.fullmatch(str(tag or ""))
    if not m:
        raise ValueError(f"tag inesperado: {str(tag)[:40]}")
    fecha_u, sha_u = "-".join(m.groups()[:3]), m.group(4)
    v = VERSION_RE.fullmatch(str(tuya or "").strip())
    if not v or not v.group(4):
        return "sin_version"
    fecha_t, sha_t = "-".join(v.groups()[:3]), v.group(4)
    if sha_t.startswith(sha_u) or sha_u.startswith(sha_t):
        return "al_dia"
    return "nueva" if fecha_u > fecha_t else ("posterior" if fecha_u < fecha_t else "mismo_dia")


def ultima_version(get=None):
    """La ultima version publicada: {"tag", "version" («AAAA-MM-DD · sha»), "url" (la pagina del Release), "fecha"}.
    Una sola peticion. Si no se puede saber, ValueError con la frase para el usuario."""
    if get is None:
        import requests
        get = requests.get
    # CHULETA_PRUEBAS_URL_RELEASES: un GitHub falso en local (tests/test_ui_lote2a.py); nunca en la app de verdad
    api = os.environ.get("CHULETA_PRUEBAS_URL_RELEASES") or RELEASES_API
    try:
        r = get(api, timeout=VERSION_TIMEOUT, allow_redirects=True,
                headers={"Accept": "application/vnd.github+json", "User-Agent": APP_NAME,
                         "X-GitHub-Api-Version": "2022-11-28"})
    except Exception as e:  # noqa: BLE001  sin red, proxy, certificados, DNS…
        raise ValueError(f"No se ha podido consultar GitHub ({e.__class__.__name__}): sin conexión o la red no lo "
                         "deja. Vuelve a probar más tarde.")
    code = getattr(r, "status_code", None)
    if code == 404:
        raise ValueError("Todavía no hay ninguna versión publicada.")
    if code in (403, 429):
        raise ValueError("GitHub no deja consultar ahora (demasiadas consultas desde esta red). Prueba en un rato.")
    if code != 200:
        raise ValueError(f"GitHub ha respondido HTTP {code}. Vuelve a probar más tarde.")
    try:
        d = r.json()
    except ValueError:
        d = None
    tag = d.get("tag_name") if isinstance(d, dict) else None
    m = TAG_RE.fullmatch(str(tag or ""))
    if not m:
        raise ValueError("GitHub ha respondido algo que no es una versión de la app.")
    url = d.get("html_url") if isinstance(d.get("html_url"), str) else ""
    if not url.startswith(RELEASES_URL + "/"):  # solo una pagina de Releases de este repo (se abre en el navegador)
        url = RELEASES_URL + "/tag/" + tag
    fecha = d.get("published_at") if isinstance(d.get("published_at"), str) else ""
    return {"tag": tag, "version": f"{'-'.join(m.groups()[:3])} · {m.group(4)}", "url": url,
            "fecha": fecha[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", fecha) else None}


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
