# -*- coding: utf-8 -*-
"""Qué pasa con el audio de cada reunión (punto 17 del review del 14/11/2026): «Guardar» (por defecto), «Guardar 15
días» o «Borrar al terminar». Es de cada reunión, no un ajuste fijo: se elige al grabar (la tarjeta, plan.py) y se
cambia en su página. Va en la cabecera de su .md (`audio: 15-dias | borrar`; sin la clave, se guarda).

Borrar quita solo el audio (todos sus ficheros: los audios con el mismo nombre que comparten textos): a la papelera de
Windows, nunca en firme (papelera_motivo: ni en una unidad sin papelera, ni si no cabe, ni con la directiva que la
desactiva), y solo cuando la reunión ya está transcrita entera, sin huecos, y resumida, sus voces no están a medias y
nada de la app va a usar su audio (debe_borrar). Sus textos (transcripción, resumen, notas, quién habla) se quedan, y la
reunión sigue en Tenca con la misma ruta gracias a MARCA_SIN_AUDIO (<nombre>.audio.json, junto a ellos): cómo era el
audio y cuándo se mandó a la papelera (scan la lee y la pinta como la reunión de siempre, sin audio).

«Sin huecos» (Huecos): un directo en la nube que se saltó algún trozo deja la reunión apuntada aquí aunque se dé por
completa; solo sale cuando una transcripción de la reunión entera termina bien (el repaso final o «Volver a
transcribir»). Hasta entonces el audio es lo único con lo que recuperar ese texto.

«15 días» cuenta desde que terminó la grabación (la fecha del audio: la grabadora lo escribe hasta el final; se
conserva al moverlo). Lo comprueba un vigilante al arrancar y cada pocos minutos, nunca mientras se graba
(app_conservar.Conservar.audio_vigia)."""
import json
import os
import shutil
import threading
import time
from datetime import datetime, timedelta

from common import WINDOWS, ApiError, fold, log, read_text
from rutas import AUDIO_EXT, DATA_DIR, MARCA_SIN_AUDIO

POLITICAS = ("guardar", "15-dias", "borrar")
POR_DEFECTO = "guardar"
DIAS = 15
CLAVE = "audio"  # la de la cabecera del .md
TEXTOS = {"guardar": "Guardar", "15-dias": f"Guardar {DIAS} días", "borrar": "Borrar al terminar"}
# las formas que se entienden (la cabecera se puede editar a mano); lo demás, «guardar» (lo seguro)
FORMAS = {"borrar": "borrar", "borrar-al-terminar": "borrar", "15-dias": "15-dias", "15dias": "15-dias",
          "guardar-15-dias": "15-dias", "guardar": "guardar"}
# por qué no se borra todavía (debe_borrar y el vigilante); la interfaz lo dice con su frase (conservar.js)
ESPERAS = ("grabando", "sin_texto", "a_medias", "huecos", "sin_resumen", "voces", "plazo", "ocupada", "nube",
           "sin_papelera", "grande", "en_uso")
COLA_S = 60.0          # sin saberse completa, el texto tiene que llegar a menos de esto del final del audio
BARRIDO_S = 600        # el vigilante mira cada 10 min (y al poco de cambiar la de una reunión o de acabar un trabajo)
PRIMERO_S = 120        # el primero, 2 min después de arrancar: sin competir con el arranque
DRIVE_FIXED = 3        # GetDriveTypeW: un disco del PC (los de red, USB y CD no tienen papelera)
CABE = 0.9             # el audio tiene que caber con holgura en la papelera de su unidad
POR_DEFECTO_PAPELERA = 0.05  # sin tamaño configurado, se cuenta con un 5 % del disco (lo menos que reserva Windows)
FASES = ("borrando", "borrado")


def politica(v):
    """El código de una política tal como venga en la cabecera del .md («15 días», «Borrar al terminar»…); lo que no
    es una de FORMAS es «guardar» (lo seguro: «borrar nunca» o «150 días» no borran)."""
    return FORMAS.get(fold(" ".join(str(v or "").split())).replace(" ", "-"), POR_DEFECTO)


def politica_de(props):
    return politica((props or {}).get(CLAVE))


def marca_de(audio_abs):
    return os.path.splitext(audio_abs)[0] + MARCA_SIN_AUDIO


def leer_marca(path):
    """Lo que dice un <nombre>.audio.json de la app (dict con su fase), o None: no se lee, o es de otro programa."""
    try:
        d = json.loads(read_text(path))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("fase") in FASES else None


def _escribir_marca(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def audios_de(audio_abs):
    """Los ficheros de audio de esa reunión: el suyo y los que se llaman igual en su carpeta (un .mkv y su .mp4 de OBS,
    un .m4a y su .wav), que comparten textos, notas y marca."""
    d, stem = os.path.dirname(audio_abs), os.path.splitext(os.path.basename(audio_abs))[0].lower()
    try:
        out = [os.path.join(d, n) for n in os.listdir(d) if os.path.splitext(n)[0].lower() == stem
               and os.path.splitext(n)[1].lower() in AUDIO_EXT and os.path.isfile(os.path.join(d, n))]
    except OSError:
        out = []
    return sorted(out, key=lambda p: os.path.normcase(p) != os.path.normcase(audio_abs)) or [audio_abs]


# ----------------------------------------------------------------------------- reuniones con huecos en el texto

class Huecos:
    """Reuniones cuyo texto se sabe que tiene huecos (rutas internas): un directo en la nube que se saltó algún trozo
    (live.run lo deja en job.live_error) y aún no se ha transcrito entera bien. Sigue a la reunión si se mueve o
    se renombra su carpeta (moved) y sale al borrarla."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.data = {str(k) for k in json.load(f)}
        except (OSError, ValueError, TypeError):
            self.data = set()

    def __contains__(self, rel):
        return rel in self.data

    def set(self, rel, con_huecos=True):
        with self.lock:
            data = set(self.data)
            (data.add if con_huecos else data.discard)(rel)
            self._save(data)

    def moved(self, old, new, folder=False):
        """Lo de `old` pasa a `new` (con folder, todo lo de dentro de la carpeta `old`)."""
        def dest(k):
            if k == old:
                return new
            if folder and k.startswith(old + "/"):
                return new + k[len(old):]
            return k
        with self.lock:
            self._save({dest(k) for k in self.data})

    def _save(self, data):
        if data == self.data:
            return
        self.data = data
        try:
            with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(sorted(data), f, ensure_ascii=False)
            os.replace(self.path + ".tmp", self.path)
        except OSError as e:  # vale en memoria
            log(f"   no se pudo guardar {os.path.basename(self.path)} ({e.__class__.__name__})")


_HUECOS, _HUECOS_LOCK = {}, threading.Lock()


def huecos():
    """Huecos del DATA_DIR vigente (uno por carpeta de datos: los tests la cambian)."""
    path = os.path.join(DATA_DIR, "texto_con_huecos.json")
    with _HUECOS_LOCK:
        h = _HUECOS.get(path)
        if h is None:
            h = _HUECOS[path] = Huecos(path)
        return h


# ----------------------------------------------------------------------------- cuándo toca

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


def entera(a):
    """La transcripción llega de verdad al final: sin huecos conocidos y, o se sabe completa (COMPLETE: un directo o
    una transcripción que llegaron al final; la biblioteca lo pone en _completa), o el .srt acaba a menos de COLA_S
    del final del audio. La etiqueta «a medias» de la interfaz (is_partial) tolera hasta un 5 %: para borrar, no."""
    d, end = a.get("duration"), a.get("tx_end")
    if a.get("partial") or d is None or a.get("_huecos"):
        return False
    if a.get("_completa"):
        return True
    return bool(a.get("srt")) and end is not None and d - end <= COLA_S


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
    if a.get("_huecos"):
        return False, "huecos"
    if not entera(a):
        return False, "a_medias"
    if not a.get("has_summary"):
        return False, "sin_resumen"
    if (a.get("speakers") or {}).get("parcial"):  # «Quién habla» a medias (punto 56): sin audio, ya no se haría
        return False, "voces"
    if pol == "15-dias":
        plazo = plazo_de(a)
        if plazo is None or (ahora or datetime.now()) < plazo:
            return False, "plazo"
    if a.get("cloud"):  # solo en OneDrive: borrarlo aquí sería borrarlo de la nube (no va a la papelera del PC)
        return False, "nube"
    return True, None


def publico(a, props):
    """Lo que la interfaz necesita para decir qué pasará con el audio de una reunión (scan → /api/tree): la política,
    hasta cuándo (15 días) y, si no es «guardar», por qué aún no (la interfaz añade lo que sabe ella: un trabajo en
    marcha, que se graba)."""
    pol = politica_de(props)
    out = {"audio_pol": pol}
    if pol == "15-dias":
        plazo = plazo_de(a)
        if plazo:
            out["audio_hasta"] = plazo.isoformat(timespec="minutes")
    if pol != POR_DEFECTO and not a.get("sin_audio"):
        motivo = debe_borrar(a, pol)[1]
        if motivo:
            out["audio_espera"] = motivo
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


def _registro(raiz, ruta, nombre):
    import winreg
    try:
        with winreg.OpenKey(raiz, ruta) as k:
            return int(winreg.QueryValueEx(k, nombre)[0])
    except (OSError, ValueError, TypeError):
        return None


def _papelera_config(guid):
    """(NukeOnDelete, MaxCapacity en MB, NoRecycleFiles) de la papelera de ese volumen y de las directivas, del registro
    (None si no está)."""
    import winreg
    base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket"
    rutas_reg = ([base + r"\Volume" + "\\" + guid] if guid else []) + [base]
    nuke = next((v for v in (_registro(winreg.HKEY_CURRENT_USER, r, "NukeOnDelete") for r in rutas_reg)
                 if v is not None), None)
    cap = next((v for v in (_registro(winreg.HKEY_CURRENT_USER, r, "MaxCapacity") for r in rutas_reg)
                if v is not None), None)
    # la directiva «No mover los archivos eliminados a la Papelera de reciclaje» (habitual en un PC de empresa)
    pol = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"
    sin = any(_registro(r, pol, "NoRecycleFiles") == 1 for r in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE))
    return nuke, cap, sin


def papelera_motivo(path, size):
    """None si ese archivo puede ir a la papelera de Windows; si no, el motivo («sin_papelera»: una unidad de red, un
    USB, una papelera puesta para borrar al momento o la directiva que la desactiva; «grande»: no cabe y Windows lo
    borraría en firme sin preguntar). Fuera de Windows (solo desarrollo) to_recycle_bin lo mueve a DATA_DIR/papelera:
    siempre se puede."""
    if not WINDOWS:
        return None
    try:
        import ctypes
        raiz, guid = _volumen(path)
        if not raiz or ctypes.windll.kernel32.GetDriveTypeW(raiz) != DRIVE_FIXED:
            return "sin_papelera"
        nuke, cap, sin = _papelera_config(guid)
        if nuke == 1 or sin:
            return "sin_papelera"
        limite = cap * 1024 * 1024 if cap else shutil.disk_usage(raiz).total * POR_DEFECTO_PAPELERA
        if size > limite * CABE:
            return "grande"
    except Exception as e:  # sin poder comprobarlo no se borra
        log(f"   audio: no se pudo comprobar la papelera ({e.__class__.__name__})")
        return "sin_papelera"
    return None


def borrar_audio(audio_abs, a, pol, a_papelera):
    """Manda el audio (y solo el audio: audios_de) de una reunión a la papelera y deja su MARCA_SIN_AUDIO. Quien llama
    ya ha comprobado debe_borrar y que nada de la app la está usando, con el candado de mover/borrar cogido.
    a_papelera: carpetas.to_recycle_bin (si Windows fuera a borrar en firme, pregunta; si se le dice que no, lanza
    ApiError("sin_papelera")). La marca se escribe antes («borrando») y se completa después («borrado»): si la app se
    cierra en medio, el siguiente vigilante ve las dos cosas y lo resuelve (fase_marca). Devuelve la marca."""
    grupo = audios_de(audio_abs)
    try:
        sts = [os.stat(p) for p in grupo]
    except OSError:
        raise ApiError("El audio ya no está.")
    for p, st in zip(grupo, sts):
        motivo = papelera_motivo(p, st.st_size)
        if motivo:
            raise ApiError(motivo)
    marca = {"version": 1, "fase": "borrando", "archivo": os.path.basename(audio_abs),
             "archivos": [os.path.basename(p) for p in grupo], "ext": os.path.splitext(audio_abs)[1].lower(),
             "bytes": sum(st.st_size for st in sts), "duracion_s": a.get("duration"),
             "fin": datetime.fromtimestamp(sts[0].st_mtime).isoformat(timespec="seconds"),
             "politica": pol, "borrado": datetime.now().isoformat(timespec="seconds")}
    path = marca_de(audio_abs)
    _escribir_marca(path, marca)
    try:
        a_papelera(grupo, aviso_firme=True)
        if any(os.path.exists(p) for p in grupo):
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


def fase_marca(audio_abs):
    """Una marca junto a un audio que sí está: «borrando» (la app se cerró a medio borrar: la marca sobra) o «borrado»
    (el usuario lo ha sacado de la papelera: se guarda, para no volver a borrarlo; quien llama cambia antes la política
    y después quita la marca). None si no hay marca o no hay audio. Una marca que no es de la app cuenta como
    «borrando» (sobra)."""
    path = marca_de(audio_abs)
    if not os.path.isfile(path) or not os.path.isfile(audio_abs):
        return None
    return (leer_marca(path) or {}).get("fase") or "borrando"


def quitar_marca(audio_abs):
    try:
        os.remove(marca_de(audio_abs))
        return True
    except OSError:
        return False


def entrada_sin_audio(marca_path, stem, rel_dir, st=None):
    """Lo que scan pone en la biblioteca para una reunión cuyo audio se mandó a la papelera: como la de un audio (la
    misma ruta, que ya no existe), con sin_audio, cuándo se borró y la duración que tenía. None si la marca no es de
    la app (otro programa con un «.audio.json») o no se puede leer: entonces no es una reunión."""
    m = leer_marca(marca_path)
    if m is None:
        return None
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
