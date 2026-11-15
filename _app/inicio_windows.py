# -*- coding: utf-8 -*-
"""«Arrancar con Windows»: el acceso directo de la carpeta Inicio (copia del del menu Inicio que pone INSTALAR.bat),
los de nombres anteriores de la app y su estado en StartupApproved.

Salio tal cual de server.py al partirlo (15/11/2026); server.py lo reexporta."""
import os
import re
import shutil
import struct

from common import APP_NAME, WINDOWS, log, log_exc
from rutas import APP_DIR


def shell_folder(csidl):
    """Carpeta especial de Windows (SHGetFolderPathW, la que respeta la redireccion por directiva de empresa; las mismas
    que [Environment]::GetFolderPath del instalador y el desinstalador), o None."""
    if not WINDOWS:
        return None
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
            return buf.value
    except Exception:
        log_exc()
    return None


def _startup_lnks():
    """(acceso directo del menu Inicio que pone INSTALAR.bat, el de la carpeta Inicio de Windows) o (None, None)."""
    if not WINDOWS:
        return None, None
    appdata = os.environ.get("APPDATA")
    progs = shell_folder(0x02) or (appdata and os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs"))
    startup = shell_folder(0x07) or (progs and os.path.join(progs, "Startup"))  # CSIDL_PROGRAMS, CSIDL_STARTUP
    if not (progs and startup):
        return None, None
    return os.path.join(progs, f"{APP_NAME}.lnk"), os.path.join(startup, f"{APP_NAME}.lnk")


def _lnk_cadenas(data):
    """Las cadenas de un acceso directo (.lnk, formato MS-SHLLINK): {"workdir", "args", "icon", ...} de su StringData
    (detras de la cabecera de 76 bytes, la lista de IDs y LinkInfo, segun LinkFlags), o None si no es un .lnk que se
    entienda."""
    if len(data) < 76 or data[:4] != b"\x4c\x00\x00\x00":
        return None
    flags = struct.unpack_from("<I", data, 20)[0]
    pos, out = 76, {}
    try:
        if flags & 0x1:  # HasLinkTargetIDList: tamano (2 bytes) y la lista
            pos += 2 + struct.unpack_from("<H", data, pos)[0]
        if flags & 0x2:  # HasLinkInfo: su tamano (4 bytes) lo incluye
            pos += struct.unpack_from("<I", data, pos)[0]
        uni = bool(flags & 0x80)  # IsUnicode
        for bit, key in ((0x4, "name"), (0x8, "relpath"), (0x10, "workdir"), (0x20, "args"), (0x40, "icon")):
            if flags & bit:
                n = struct.unpack_from("<H", data, pos)[0] * (2 if uni else 1)
                raw = data[pos + 2:pos + 2 + n]
                if len(raw) < n:
                    return None
                out[key] = raw.decode("utf-16-le" if uni else "latin-1", "replace")
                pos += 2 + n
    except struct.error:
        return None
    return out


def _lnk_apunta_a(path, carpeta):
    """¿El acceso directo `path` es de esta copia de la app (`carpeta`, su _app)? Lo es si su carpeta de trabajo es
    esa, o si los argumentos («…\\_app\\server.py») o el icono («…\\_app\\static\\…», a veces con
    %USERPROFILE%) llevan una ruta de dentro de ella, en sus cadenas de .lnk (_lnk_cadenas). Si no, se busca en el
    fichero entero (UTF-16LE, en las dos alineaciones) la carpeta seguida de la barra: sin la barra, «…\\_app» casaba
    tambien con «…\\_app2», otra carpeta. Sin mayusculas. Sin poder leerlo, no lo es."""
    try:
        with open(path, "rb") as f:
            data = f.read(1 << 16)
    except OSError:
        return False

    def norm(p):
        p = re.sub(r"%([^%]+)%", lambda m: os.environ.get(m[1], m[0]), p or "")  # %USERPROFILE%… del icono
        return os.path.normcase(os.path.normpath(p.strip().strip('"'))).casefold() if p.strip() else ""

    base = norm(carpeta)
    dentro = os.path.join(base, "")
    cads = _lnk_cadenas(data)
    if cads is not None:
        if norm(cads.get("workdir")) == base:
            return True
        if any(dentro in norm(cads.get(k)) for k in ("args", "icon")):
            return True
    return any(dentro in os.path.normcase(data[i:].decode("utf-16-le", "ignore")).casefold() for i in (0, 1))


def _startup_antiguos(destino):
    """Las copias de «Arrancar con Windows» con el nombre anterior de la app («Chuleta.lnk») que haya en la carpeta
    Inicio de Windows y sean de esta copia (llevan la ruta de su _app): siguen abriendola al entrar y no deben quedar
    junto a la de ahora (se abriria dos veces). instalar.ps1 ya las migra; esto cubre lo que quede (una instalacion que
    no pudo). Un acceso directo con ese nombre que no es de esta copia no se cuenta ni se toca."""
    from common import ARRANQUE_ANTIGUOS
    carpeta = os.path.dirname(destino)
    return [p for p in (os.path.join(carpeta, f"{n}.lnk") for n in ARRANQUE_ANTIGUOS)
            if os.path.isfile(p) and _lnk_apunta_a(p, APP_DIR)]


STARTUP_APPROVED = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\StartupApproved\\StartupFolder"


def _startup_aprobado_copiar(desde, hacia, clave=STARTUP_APPROVED):
    """Aplicaciones de inicio (Administrador de tareas) guarda si se desactivo un acceso directo de la carpeta Inicio
    por su nombre de fichero (HKCU\\<clave>, REG_BINARY; sin valor = activado). Al pasar «Chuleta.lnk» a «Tenca.lnk» se
    copia su valor, como Copy-ArranqueAprobado del instalador: lo desactivado sigue desactivado aunque la migracion la
    haga la app (un instalador que no pudo). El del nombre viejo se deja. Sin valor o sin registro, nada."""
    if not WINDOWS:
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, clave, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as k:
            v, tipo = winreg.QueryValueEx(k, desde)
            if tipo != winreg.REG_BINARY or not v:
                return
            winreg.SetValueEx(k, hacia, 0, winreg.REG_BINARY, v)
        log(f"arrancar con Windows: {'desactivado' if v[0] == 3 else 'activado'} en Aplicaciones de inicio, como lo "
            f"tenía «{desde[:-4]}»")
    except (ImportError, OSError):
        pass


def _startup_quitar(rutas):
    for p in rutas:
        try:
            os.remove(p)
            log(f"arrancar con Windows: quitado el acceso directo antiguo «{os.path.basename(p)[:-4]}»")
        except OSError as e:
            log(f"   arrancar con Windows: {e}")


def inicio_windows_leer():
    """«Arrancar con Windows»: True o False; None si no se puede (fuera de Windows, o sin el acceso directo del menu
    Inicio, que es el que se copia: una copia descomprimida sin instalar). Tambien True con una copia que aun lleve un
    nombre anterior de la app: sigue abriendola al entrar en Windows."""
    origen, destino = _startup_lnks()
    if not origen:
        return None
    if os.path.isfile(destino) or _startup_antiguos(destino):
        return True
    return False if os.path.isfile(origen) else None


def inicio_windows_poner(activado):
    """Copia (o quita) el acceso directo del menu Inicio en la carpeta Inicio de Windows (shell:startup): mismo destino,
    iconos y AppUserModelID, sin registro ni permisos de administrador. Las copias con un nombre anterior de la app se
    quitan en los dos casos (nunca dos). Devuelve el estado que queda."""
    origen, destino = _startup_lnks()
    if not origen:
        return None
    try:
        if activado and os.path.isfile(origen):
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            shutil.copy2(origen, destino)
            log("arrancar con Windows: activado")
        elif not activado and os.path.isfile(destino):
            os.remove(destino)
            log("arrancar con Windows: desactivado")
    except OSError as e:
        log(f"   arrancar con Windows: {e}")
        return inicio_windows_leer()
    if activado and not os.path.isfile(destino):
        return inicio_windows_leer()  # sin el de Inicio no hay copia nueva: las antiguas siguen siendo lo unico que hay
    _startup_quitar(_startup_antiguos(destino))
    return inicio_windows_leer()


def inicio_windows_al_dia():
    """Al arrancar: si esta activado y el acceso directo del menu Inicio ha cambiado (lo rehace INSTALAR.bat al
    actualizar), la copia de la carpeta Inicio se pone igual. Si la copia aun lleva un nombre anterior de la app
    («Chuleta.lnk»), pasa a ser «<APP_NAME>.lnk» (sigue activado) y la antigua se quita."""
    origen, destino = _startup_lnks()
    try:
        antiguos = _startup_antiguos(destino) if destino else []
        if origen and os.path.isfile(origen) and (antiguos and not os.path.isfile(destino)):
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            shutil.copy2(origen, destino)
            log(f"arrancar con Windows: sigue activado, ahora como «{APP_NAME}»")
            _startup_aprobado_copiar(os.path.basename(antiguos[0]), os.path.basename(destino))
        if origen and os.path.isfile(destino) and os.path.isfile(origen):
            with open(origen, "rb") as a, open(destino, "rb") as b:
                if a.read() != b.read():
                    shutil.copy2(origen, destino)
                    log("arrancar con Windows: acceso directo actualizado")
        if antiguos and os.path.isfile(destino):
            _startup_quitar(antiguos)
    except OSError as e:
        log(f"   arrancar con Windows: {e}")
