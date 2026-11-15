# -*- coding: utf-8 -*-
"""«Mis pendientes» (review del 14/11/2026, punto 28): las tareas sin hacer de todas las reuniones, en Hoy.

La fuente de verdad es el propio <audio>.resumen.md: una tarea es una línea «- [ ] Responsable: tarea — fecha
[mm:ss]» (el formato que pide engines al resumir; también «* [ ]», «+ [ ]» y «1. [ ]») y hacerla es cambiar esa línea a
«- [x]». Así «La última vez» de Hoy (plan.tareas_abiertas), la vista del resumen y «Mis pendientes» cuentan lo mismo,
sin otro fichero que se desincronice.

Al regenerar un resumen (trabajos.Jobs._run_summary) no se pierde lo hecho: conservar_hechas marca ya hechas las tareas
nuevas que son las mismas (mismo texto normalizado: nunca por parecido) y añade como hechas al final de los próximos
pasos las que el resumen nuevo ya no trae. LOCK lo comparten esa escritura y «marcar» (leer, cambiar y escribir): un
clic durante una regeneración no se pierde.

Funciones puras (sin disco) salvo Cache, que relee un resumen solo si cambia su fecha o su tamaño y no abre los que
OneDrive tiene «solo en línea» (leerlos los descargaría y, sin conexión, puede bloquear)."""
import os
import re
import threading

from common import fold, read_text
from rutas import CLOUD_ONLY

# un leer-cambiar-escribir del .resumen.md a la vez: «marcar» (Hoy y la vista del resumen) y el resumen regenerado
LOCK = threading.Lock()

# «- [ ] texto», «* [x] texto», «+ [X] texto», «1. [ ] texto», «2) [ ] texto», con sangría o sin ella; el texto no puede
# estar vacío. Las mismas líneas abiertas que cuenta plan.tareas_abiertas (test_pendientes lo comprueba).
_TAREA = re.compile(r"^([ \t]*(?:[-*+]|\d+[.)])[ \t]+\[)([ xX])\][ \t]*(\S.*?)[ \t\r]*$")
# un minuto de la transcripción entre corchetes: [12:34], [1:02:10], [105:20], [12:34–13:10], [1:12, 3:40]
_MINUTOS = re.compile(r"\[\s*(?:\d+:)?\d{1,3}:\d{2}(?:[^\]\n]*)\]")
_PRIMER_MINUTO = re.compile(r"\[\s*(?:(\d+):)?(\d{1,3}):(\d{2})")
_CABECERA = re.compile(r"^[ \t]*#{1,6}[ \t]+(.*?)[ \t#]*$")
CABECERAS_PASOS = ("proximos pasos", "next steps")  # normalizadas (engines.SUMMARY_HEADINGS["next"])
NINGUNO = ("ninguno", "ninguna", "none", "nada", "ninguno claro", "ninguna clara", "none clear")
CABECERA_NUEVA = "## Próximos pasos"
# quién eres tú en un responsable: «Yo» (como etiqueta el resumen al usuario), «Tú»/«Tu» y, en un resumen en inglés,
# «Me» o «I» (normalizados)
YO = ("yo", "tu", "me", "i")
_PARTES_RESPONSABLE = re.compile(r"\s*(?:,|/|&|\+|;|\by\b|\be\b|\band\b)\s*")


class NoEsta(ValueError):
    """La tarea que se quiere marcar ya no está en el resumen (se regeneró o se editó)."""


def normalizar(s):
    """La clave para comparar tareas: sin los minutos [mm:ss], sin mayúsculas ni tildes, sin puntuación y con los
    espacios normalizados. «Ana: enviar el **acta** [12:34]» y «ana enviar el acta» son la misma tarea."""
    s = fold(_MINUTOS.sub(" ", s or ""))
    return " ".join(re.sub(r"[\W_]+", " ", s).split())


def primer_segundo(s):
    """El primer minuto citado ([mm:ss] o [h:mm:ss]) en segundos, o None."""
    m = _PRIMER_MINUTO.search(s or "")
    if not m:
        return None
    h, mm, ss = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
    return h * 3600 + mm * 60 + ss


def partir(texto):
    """(responsable, tarea) de «Responsable: tarea — fecha [mm:ss]», para enseñarla: sin los minutos, sin negritas y
    sin el guion que queda al final. Sin «Algo: » corto delante, el responsable va vacío."""
    t = _MINUTOS.sub(" ", texto or "")
    t = " ".join(re.sub(r"\*\*|__", "", t).split())
    k = t.find(": ")
    resp = ""
    if k > 0:
        cabeza = t[:k].strip(" *_")
        if cabeza and len(cabeza) <= 40 and len(cabeza.split()) <= 5 and not re.search(r"https?$", cabeza, re.I):
            resp, t = cabeza, t[k + 2:]
    return resp, t.strip().rstrip(" —–-·,;:").strip()


def es_tuya(responsable, nombre_usuario=None):
    """La tarea es del usuario: su responsable (o uno de ellos, «Yo y Ana») es «Yo», «Tú»/«Tu» o el nombre de la
    persona «Soy yo» de las voces (exacto, sin mayúsculas ni tildes; «¿Ana?», la provisional, también)."""
    suyo = normalizar(nombre_usuario) if nombre_usuario else ""
    for parte in _PARTES_RESPONSABLE.split(fold(responsable or "")):
        p = normalizar(parte)
        if p and (p in YO or (suyo and p == suyo)):
            return True
    return False


def extraer(texto):
    """Las tareas de un texto, en orden: [{i (línea, desde 0), hecha, texto (lo que va tras la casilla, tal cual),
    clave (normalizar), responsable, tarea, segundo}]. `pos` es dónde va la «x» en la línea (para marcarla)."""
    out = []
    for i, linea in enumerate((texto or "").split("\n")):
        m = _TAREA.match(linea)
        if not m:
            continue
        resp, tarea = partir(m.group(3))
        out.append({"i": i, "hecha": m.group(2) != " ", "texto": m.group(3), "clave": normalizar(m.group(3)),
                    "responsable": resp, "tarea": tarea, "segundo": primer_segundo(m.group(3)), "pos": m.end(1)})
    return out


def abiertas(texto):
    return sum(1 for t in extraer(texto) if not t["hecha"])


def _poner(linea, t, hecha):
    return linea[:t["pos"]] + ("x" if hecha else " ") + linea[t["pos"] + 1:]


def marcar(texto, i, tarea, hecha):
    """(texto nuevo, línea, cambiado): la tarea `tarea` (su texto, como lo dio extraer) hecha o sin hacer. Se busca en
    la línea `i`; si ya no está ahí (el resumen se editó), la misma tarea en otra línea (la más cercana que aún no esté
    así). Si no está en ninguna, NoEsta: nunca se marca otra por parecido."""
    clave = normalizar(tarea)
    tareas = [t for t in extraer(texto) if clave and t["clave"] == clave]
    if not tareas:
        raise NoEsta("Esa tarea ya no está en el resumen: puede que se haya regenerado o editado. Vuelve a abrirlo.")
    t = next((t for t in tareas if t["i"] == i), None)
    if t is None:
        cambian = [t for t in tareas if t["hecha"] != hecha] or tareas
        t = min(cambian, key=lambda x: (abs(x["i"] - i), x["i"]))
    if t["hecha"] == hecha:
        return texto, t["i"], False
    lineas = texto.split("\n")
    lineas[t["i"]] = _poner(lineas[t["i"]], t, hecha)
    return "\n".join(lineas), t["i"], True


def _seccion_pasos(lineas):
    """Índice de la cabecera «Próximos pasos» / «Next steps», o None."""
    for k, linea in enumerate(lineas):
        m = _CABECERA.match(linea)
        if m and normalizar(m.group(1)) in CABECERAS_PASOS:
            return k
    return None


def conservar_hechas(viejo, nuevo):
    """El resumen `nuevo` sin perder lo que estaba hecho en `viejo`: (a) sus tareas con el mismo texto normalizado que
    una hecha del viejo salen ya hechas; (b) las hechas del viejo que no están en el nuevo (ni hechas ni sin hacer) se
    añaden hechas tras la última tarea del nuevo, o tras su cabecera de próximos pasos (sin el «Ninguno» que hubiera),
    o al final con su cabecera. Nunca se marca hecha una tarea nueva por parecido: solo por igualdad normalizada."""
    hechas = [t for t in extraer(viejo) if t["hecha"] and t["clave"]]
    if not hechas:
        return nuevo
    claves = {t["clave"] for t in hechas}
    lineas = nuevo.split("\n")
    tareas = extraer(nuevo)
    for t in tareas:
        if not t["hecha"] and t["clave"] in claves:
            lineas[t["i"]] = _poner(lineas[t["i"]], t, True)
    en_el_nuevo, sobran = {t["clave"] for t in tareas}, []
    for t in hechas:
        if t["clave"] not in en_el_nuevo:
            en_el_nuevo.add(t["clave"])  # (una vez aunque el viejo la tuviera repetida)
            sobran.append("- [x] " + t["texto"])
    if not sobran:
        return "\n".join(lineas)
    if tareas:
        k = tareas[-1]["i"] + 1
        while k < len(lineas) and lineas[k][:1] in (" ", "\t") and lineas[k].strip() and not _TAREA.match(lineas[k]):
            k += 1  # lo que sigue sangrado es de esa tarea
        lineas[k:k] = sobran
        return "\n".join(lineas)
    h = _seccion_pasos(lineas)
    if h is not None:
        fin = next((k for k in range(h + 1, len(lineas)) if _CABECERA.match(lineas[k])), len(lineas))
        llenas = [k for k in range(h + 1, fin) if lineas[k].strip()]
        if len(llenas) == 1 and normalizar(re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s*", "", lineas[llenas[0]])) in NINGUNO:
            del lineas[llenas[0]]
        lineas[h + 1:h + 1] = sobran
        return "\n".join(lineas)
    return "\n".join(lineas).rstrip("\n") + "\n\n" + CABECERA_NUEVA + "\n" + "\n".join(sobran)


def separar_cabecera(texto):
    """(meta, cuerpo, desde) de un .resumen.md: la cabecera «---» modelo/proveedor/generado, el resumen sin ella (sin
    los espacios de los extremos) y la línea del fichero en la que empieza el cuerpo (la vista del resumen marca las
    tareas por su línea: la del fichero es la del cuerpo + desde)."""
    meta, desde = {}, 0
    lineas = texto.split("\n")
    if lineas and lineas[0].strip() == "---":
        end = next((k for k in range(1, len(lineas)) if lineas[k].strip() == "---"), 0)
        for linea in lineas[1:end]:
            k, _, v = linea.partition(":")
            meta[k.strip()] = v.strip()
        resto = lineas[end + 1:]
        desde = end + 1 + next((k for k, linea in enumerate(resto) if linea.strip()), len(resto))
        texto = "\n".join(resto).strip()
    return meta, texto, desde


def solo_en_linea(st):
    return bool(getattr(st, "st_file_attributes", 0) & CLOUD_ONLY)


class Cache:
    """Las tareas de cada .resumen.md, releídas solo si cambian su fecha o su tamaño. Un resumen «solo en línea» de
    OneDrive no se abre (None); tampoco uno que no se puede leer."""

    def __init__(self, stat=os.stat):
        self.lock, self.datos, self.stat = threading.Lock(), {}, stat

    def tareas(self, path):
        try:
            st = self.stat(path)
        except OSError:
            return None
        if solo_en_linea(st):
            return None
        clave = (st.st_mtime_ns, st.st_size)
        with self.lock:
            hit = self.datos.get(path)
        if hit and hit[0] == clave:
            return hit[1]
        try:
            tareas = extraer(read_text(path))
        except OSError:
            return None
        with self.lock:
            self.datos[path] = (clave, tareas)
        return tareas

    def olvidar(self, path):
        """Tras escribir el resumen: con la misma fecha (discos con fechas de 2 s) y el mismo tamaño («[ ]» y «[x]»
        miden lo mismo) la caché no vería el cambio."""
        with self.lock:
            self.datos.pop(path, None)

    def podar(self, vivos):
        """Fuera lo de los resúmenes que ya no están en la biblioteca (borrados, movidos)."""
        with self.lock:
            for p in [p for p in self.datos if p not in vivos]:
                del self.datos[p]


CACHE = Cache()
