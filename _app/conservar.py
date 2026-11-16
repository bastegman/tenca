# -*- coding: utf-8 -*-
"""Qué pasa con el audio de cada reunión (punto 17 del review del 14/11/2026): «Guardar» (por defecto), «Guardar 15
días» o «Borrar al terminar». Es de cada reunión, no un ajuste fijo: se elige al grabar (la tarjeta, plan.py) y se
cambia en su página. Va en la cabecera de su .md (`audio: guardar | 15-dias | borrar`).

Borrar quita solo el audio: a la papelera de Windows, nunca en firme (papelera_motivo: ni en una unidad sin papelera,
ni si no cabe), y solo cuando la reunión ya está transcrita entera y resumida y nada de la app va a usar su audio
(debe_borrar). Sus textos (transcripción, resumen, notas, quién habla) se quedan, y la reunión sigue en Tenca con la
misma ruta gracias a MARCA_SIN_AUDIO (<nombre>.audio.json, junto a ellos): cómo era el audio y cuándo se mandó a la
papelera (scan la lee y la pinta como la reunión de siempre, sin audio).

«15 días» cuenta desde que terminó la grabación (la fecha del audio: la grabadora lo escribe hasta el final; se
conserva al moverlo). Lo comprueba un vigilante al arrancar y cada pocos minutos, nunca mientras se graba
(app_carpetas.Carpetas.audio_vigia)."""
import json
import os
import shutil
import time
from datetime import datetime, timedelta

from common import WINDOWS, ApiError, fold, log, read_text
from rutas import AUDIO_EXT, MARCA_SIN_AUDIO

POLITICAS = ("guardar", "15-dias", "borrar")
POR_DEFECTO = "guardar"
DIAS = 15
CLAVE = "audio"  # la de la cabecera del .md
TEXTOS = {"guardar": "Guardar", "15-dias": f"Guardar {DIAS} días", "borrar": "Borrar al terminar"}
# por qué no se borra todavía (debe_borrar); la interfaz lo dice con su frase (notas.js)
ESPERAS = ("grabando", "sin_texto", "a_medias", "sin_resumen", "plazo", "ocupada", "nube", "sin_papelera", "grande",
           "no_esta")
BARRIDO_S = 600        # el vigilante mira cada 10 min (y al poco de cambiar la de una reunión o de acabar un trabajo)
PRIMERO_S = 120        # el primero, 2 min después de arrancar: sin competir con el arranque
DRIVE_FIXED = 3        # GetDriveTypeW: un disco del PC (los de red, USB y CD no tienen papelera)
CABE = 0.9             # el audio tiene que caber con holgura en la papelera de su unidad
POR_DEFECTO_PAPELERA = 0.05  # sin tamaño configurado, se cuenta con un 5 % del disco (lo menos que reserva Windows)


def politica(v):
    """El código de una política tal como venga (la cabecera del .md se puede editar a mano: «15 días», «Borrar al
    terminar»…); lo que no se entiende es «guardar» (lo seguro)."""
    k = fold(" ".join(str(v or "").split())).replace(" ", "-")
    if k.startswith("borrar"):
        return "borrar"
    if k.startswith(f"{DIAS}") or k.startswith(f"guardar-{DIAS}"):
        return "15-dias"
    return POR_DEFECTO


def politica_de(props):
    return politica((props or {}).get(CLAVE))


def marca_de(audio_abs):
    return os.path.splitext(audio_abs)[0] + MARCA_SIN_AUDIO


def leer_marca(path):
    """Lo que dice un <nombre>.audio.json (dict) o None si no se puede leer o no es de la app."""
    try:
        d = json.loads(read_text(path))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _escribir_marca(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def fin_de(a):
    """Cuándo terminó la grabación: la fecha del audio (datetime) o None."""
    try:
        return datetime.fromtimestamp(float(a.get("mtime")))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def plazo_de(a):
    """Hasta cuándo se guarda con «15 días» (datetime) o None."""
    fin = fin_de(a)
    return fin + timedelta(days=DIAS) if fin else None


def debe_borrar(a, pol, ahora=None):
    """(True, None) si ya toca mandar el audio de esa reunión (una entrada de la biblioteca) a la papelera; si no,
    (False, motivo de ESPERAS) o (False, None) si no hay nada que hacer (se guarda, o ya no tiene audio). Lo que no
    sabe la biblioteca (un trabajo de la app sobre ella, una unidad sin papelera) lo mira quien llama."""
    if pol not in ("15-dias", "borrar") or a.get("sin_audio"):
        return False, None
    if a.get("recording"):
        return False, "grabando"
    if not (a.get("txt") or a.get("srt")):
        return False, "sin_texto"
    if a.get("partial") or a.get("duration") is None:  # sin saber cuánto dura, no se sabe si el texto llega al final
        return False, "a_medias"
    if not a.get("has_summary"):
        return False, "sin_resumen"
    if pol == "15-dias":
        plazo = plazo_de(a)
        if plazo is None or (ahora or datetime.now()) < plazo:
            return False, "plazo"
    if a.get("cloud"):  # solo en OneDrive: borrarlo aquí sería borrarlo de la nube (no va a la papelera del PC)
        return False, "nube"
    return True, None


def publico(a, props):
    """Lo que la interfaz necesita para decir qué pasará con el audio de una reunión (scan → /api/tree)."""
    pol = politica_de(props)
    out = {"audio_pol": pol}
    if pol == "15-dias":
        plazo = plazo_de(a)
        if plazo:
            out["audio_hasta"] = plazo.isoformat(timespec="minutes")
    return out


# ----------------------------------------------------------------------------- la papelera de Windows

def _volumen(path):
    """(raíz de la unidad, GUID del volumen «{…}» o None) en Windows."""
    import ctypes
    k32 = ctypes.windll.kernel32
    buf = ctypes.create_unicode_buffer(1024)
    if not k32.GetVolumePathNameW(os.path.abspath(path), buf, len(buf)):
        return None, None
    raiz = buf.value
    vol = ctypes.create_unicode_buffer(128)
    guid = None
    if k32.GetVolumeNameForVolumeMountPointW(raiz, vol, len(vol)):
        v = vol.value  # \\?\Volume{xxxxxxxx-…}\
        i, j = v.find("{"), v.find("}")
        guid = v[i:j + 1] if 0 <= i < j else None
    return raiz, guid


def _papelera_config(guid):
    """(NukeOnDelete, MaxCapacity en MB) de la papelera de ese volumen, del registro (None si no está)."""
    import winreg
    nuke = cap = None
    rutas_reg = [r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume" + "\\" + guid] if guid else []
    rutas_reg.append(r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket")
    for r in rutas_reg:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r) as k:
                for nombre in ("NukeOnDelete", "MaxCapacity"):
                    try:
                        v = int(winreg.QueryValueEx(k, nombre)[0])
                    except (OSError, ValueError, TypeError):
                        continue
                    if nombre == "NukeOnDelete" and nuke is None:
                        nuke = v
                    elif nombre == "MaxCapacity" and cap is None:
                        cap = v
        except OSError:
            continue
    return nuke, cap


def papelera_motivo(path, size):
    """None si ese archivo puede ir a la papelera de Windows; si no, el motivo («sin_papelera»: una unidad de red, un
    USB o una papelera puesta para borrar al momento; «grande»: no cabe y Windows lo borraría en firme sin preguntar).
    Fuera de Windows (solo desarrollo) to_recycle_bin lo mueve a DATA_DIR/papelera: siempre se puede."""
    if not WINDOWS:
        return None
    try:
        import ctypes
        raiz, guid = _volumen(path)
        if not raiz or ctypes.windll.kernel32.GetDriveTypeW(raiz) != DRIVE_FIXED:
            return "sin_papelera"
        nuke, cap = _papelera_config(guid)
        if nuke == 1:
            return "sin_papelera"
        limite = cap * 1024 * 1024 if cap else shutil.disk_usage(raiz).total * POR_DEFECTO_PAPELERA
        if size > limite * CABE:
            return "grande"
    except Exception as e:  # sin poder comprobarlo no se borra
        log(f"   audio: no se pudo comprobar la papelera ({e.__class__.__name__})")
        return "sin_papelera"
    return None


def borrar_audio(audio_abs, a, pol, a_papelera):
    """Manda el audio (y solo el audio) de una reunión a la papelera y deja su MARCA_SIN_AUDIO. Quien llama ya ha
    comprobado debe_borrar y que nada de la app la está usando, con el candado de mover/borrar cogido. a_papelera:
    carpetas.to_recycle_bin. La marca se escribe antes («borrando») y se completa después («borrado»): si la app se
    cierra en medio, el siguiente vigilante ve las dos cosas y lo resuelve (resolver_marca). Devuelve la marca."""
    try:
        st = os.stat(audio_abs)
    except OSError:
        raise ApiError("El audio ya no está.")
    motivo = papelera_motivo(audio_abs, st.st_size)
    if motivo:
        raise ApiError(motivo)
    marca = {"version": 1, "fase": "borrando", "archivo": os.path.basename(audio_abs),
             "ext": os.path.splitext(audio_abs)[1].lower(), "bytes": st.st_size,
             "duracion_s": a.get("duration"), "fin": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
             "politica": pol, "borrado": datetime.now().isoformat(timespec="seconds")}
    path = marca_de(audio_abs)
    _escribir_marca(path, marca)
    try:
        a_papelera([audio_abs], aviso_firme=True)
        if os.path.exists(audio_abs):
            raise ApiError("El audio sigue en su sitio.")
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    marca["fase"] = "borrado"
    try:
        _escribir_marca(path, marca)
    except OSError:  # el audio ya está en la papelera: con «borrando» y sin audio, la reunión ya se ve sin él
        pass
    return marca


def resolver_marca(audio_abs):
    """Una marca junto a un audio que sí está: la app se cerró a medio borrar («borrando»: la marca sobra) o el usuario
    lo ha sacado de la papelera («borrado»: se guarda, para no volver a borrarlo). Devuelve "recuperado", "a_medias"
    o None (no había marca)."""
    path = marca_de(audio_abs)
    if not os.path.isfile(path) or not os.path.isfile(audio_abs):
        return None
    m = leer_marca(path) or {}
    try:
        os.remove(path)
    except OSError:
        return None
    return "recuperado" if m.get("fase") == "borrado" else "a_medias"


def entrada_sin_audio(marca_path, stem, rel_dir, st=None):
    """Lo que scan pone en la biblioteca para una reunión cuyo audio se mandó a la papelera: como la de un audio (la
    misma ruta, que ya no existe), con sin_audio, cuándo se borró y la duración que tenía."""
    m = leer_marca(marca_path) or {}
    ext = str(m.get("ext") or ".wav").lower()
    if ext not in AUDIO_EXT:  # la marca se puede editar a mano: con otra cosa, la reunión se pinta como un WAV
        ext = ".wav"
    name = stem + ext
    try:
        dur = round(float(m["duracion_s"]), 1) if m.get("duracion_s") is not None else None
    except (TypeError, ValueError):
        dur = None
    try:
        mtime = datetime.fromisoformat(str(m.get("fin"))).timestamp()
    except (TypeError, ValueError):
        mtime = st.st_mtime if st is not None else time.time()
    return {"name": name, "ext": ext, "sin_audio": True, "audio_borrado": str(m.get("borrado") or "")[:19] or None,
            "audio_bytes": m.get("bytes") if isinstance(m.get("bytes"), int) else None, "_dur": dur,
            "mtime": mtime, "rel": f"{rel_dir}/{name}" if rel_dir else name}


def error_sin_audio(audio_abs):
    """ApiError para lo que necesita el audio en una reunión que ya no lo tiene (o None si lo tiene)."""
    if os.path.isfile(audio_abs) or not os.path.isfile(marca_de(audio_abs)):
        return None
    m = leer_marca(marca_de(audio_abs)) or {}
    cuando = str(m.get("borrado") or "")[:10]
    return ApiError("El audio de esta reunión ya no está: se mandó a la papelera de Windows"
                    + (f" el {cuando[8:10]}/{cuando[5:7]}/{cuando[:4]}" if len(cuando) == 10 else "")
                    + ". Los textos siguen aquí; si lo necesitas, recupéralo de la papelera.", 409)
