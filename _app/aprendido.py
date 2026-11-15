# -*- coding: utf-8 -*-
"""Aprender de tus correcciones (la parte de los ficheros y los ajustes; las propuestas estan en aprender.py): las
reglas aplicadas a cada reunion (originales/<md5>/aprendido.json), deshacerlas y anadir un termino a una lista.

Salio tal cual de server.py al partirlo (15/11/2026); server.py lo reexporta."""
import errno
import json
import os
from collections import Counter

import aprender
from ajustes import aprendido_de
from biblioteca import TX_SAVE_LOCK, temas
from carpetas import backup_dir, copia_original
from common import ApiError, fold, log, parse_srt, read_text, save_text, write_bytes
from listas import VOCAB_MAX_BYTES, _is_app_name, find_vocab, md_props, read_vocab
from rutas import CLOUD_ONLY, rel_of, safe


# (aprender.py, item 17, 09/11/2026). En los ajustes ("aprendido"): las reglas que aceptaste, {"de", "a",
# "proyecto", "at"}, que se aplican al terminar cada transcripcion de ese proyecto ("" = las reuniones sin proyecto), y
# lo que dijiste que no ("no": terminos; "no_reglas": [de, a], en aprender.normal). Lo que Tenca cambio en cada reunion
# va en su copia del original (originales/<md5>/aprendido.json): se mueve, se restaura y se borra con ella.
APRENDIDO_FILE = "aprendido.json"


def mismo_proyecto(a, b):
    """Dos nombres del mismo proyecto: iguales sin tildes ni mayusculas, o los dos de la raiz (root_names)."""
    return fold(a or "") == fold(b or "") or (bool(a) and bool(b) and _is_app_name(a) and _is_app_name(b))


def lista_de_reunion(abs_audio, props=None):
    """La lista del tema de la reunion (una de list_vocab) o None: sin tema, Whisper recibe el vocabulario general."""
    props = md_props(abs_audio) if props is None else props
    if not props.get("workstream"):
        return None
    try:
        vid = temas().get(rel_of(abs_audio))
        return find_vocab(props.get("proyecto"), props.get("workstream"), vid, near=abs_audio)
    except OSError:
        return None


def proyecto_de_reunion(abs_audio, props=None):
    """El proyecto de la reunion para las reglas aprendidas: el de su lista o, si ya no esta, el de su cabecera ("" =
    sin proyecto)."""
    props = md_props(abs_audio) if props is None else props
    v = lista_de_reunion(abs_audio, props)
    return v["project"] if v else (props.get("proyecto") or "")


def original_de(rel):
    """Lo que escribio Whisper (la copia de originales) como [{"start", "text"}], o None si no hay copia."""
    bdir = backup_dir(rel)
    for ext in (".srt", ".txt"):
        p = os.path.join(bdir, "original" + ext)
        if os.path.isfile(p):
            text = read_text(p)
            if ext == ".srt":
                return parse_srt(text)
            return [{"start": None, "end": None, "text": l.strip()} for l in text.splitlines() if l.strip()]
    return None


def registro_aprendido(rel):
    """Lo que Tenca ha cambiado en esta reunion: {"cambios": [{"start", "de", "a", "pos", "texto"}], "deshechas":
    [normal(de)], "respondidas": [normal(termino)]} (las reglas que deshiciste aqui no se vuelven a aplicar en ella, y
    lo que ya anadiste desde ella no se vuelve a proponer en ella, aunque fuera a otra lista)."""
    try:
        with open(os.path.join(backup_dir(rel), APRENDIDO_FILE), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)  # noqa: E731
    cambios = [c for c in data.get("cambios") or [] if isinstance(c, dict) and num(c.get("start"))
               and isinstance(c.get("de"), str) and isinstance(c.get("a"), str) and c["a"]
               and (c.get("pos") is None or (isinstance(c["pos"], int) and c["pos"] >= 0))
               and (c.get("texto") is None or isinstance(c["texto"], str))]
    return {"cambios": cambios, "deshechas": [x for x in data.get("deshechas") or [] if isinstance(x, str)],
            "respondidas": [x for x in data.get("respondidas") or [] if isinstance(x, str)]}


def guardar_registro(rel, reg):
    p = os.path.join(backup_dir(rel), APRENDIDO_FILE)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False)
    os.replace(tmp, p)


def _escribir_bloques(base, bloques):
    """El .srt (con sus tiempos tal cual) y el .txt de una transcripcion cambiada por aprender.py."""
    save_text(base + ".srt", aprender.srt_texto(bloques), "utf-8")
    save_text(base + ".txt", "\n".join(b["text"] for b in bloques) + "\n", "utf-8-sig")


def aplicar_aprendido(rel, st):
    """Al terminar una transcripcion (Jobs._after_transcribe, antes de los destinos, las voces y el resumen): las reglas
    que aceptaste para el proyecto de la reunion, solo en las frases que nadie ha tocado (las que siguen como las
    escribio Whisper, o las nuevas de una transcripcion que continua) y nunca las que deshiciste en esta reunion. La
    primera vez se guarda lo de Whisper (Restaurar original) y cada cambio queda apuntado para marcarlo y deshacerlo.
    Devuelve cuantos cambios ha hecho."""
    todas = aprendido_de(st)["reglas"]
    if not todas:
        return 0
    with TX_SAVE_LOCK:  # no a la vez que una correccion a mano o un «Deshacer» de esta u otra reunion
        return _aplicar_aprendido(rel, todas)


def _aplicar_aprendido(rel, todas):
    abs_path = safe(rel, "file")
    proyecto = proyecto_de_reunion(abs_path)
    # las de su proyecto y que sigan siendo seguras (una escrita a mano en settings.json o de una version anterior,
    # como «plan» → «Plan», no se aplica)
    reglas = [r for r in todas if mismo_proyecto(r["proyecto"], proyecto) and aprender.regla_valida(r["de"], r["a"])]
    base = os.path.splitext(abs_path)[0]
    if not reglas or not os.path.isfile(base + ".srt"):
        return 0
    bloques = aprender.srt_bloques(read_text(base + ".srt"))
    orig = original_de(rel)
    antes = {aprender.clave(s["start"]): s["text"] for s in orig or [] if s.get("start") is not None}
    reg = registro_aprendido(rel)
    deshechas, nuevos = set(reg["deshechas"]), []
    repes = aprender.repetidos(bloques)  # dos frases con el mismo inicio: no se sabria marcar ni deshacer cada una
    for b in bloques:
        o = antes.get(b["start"])
        if (o is not None and o != b["text"]) or b["start"] in repes:
            continue  # una frase que has corregido tu (o que ya cambio Tenca): no se toca
        texto, hechos = aprender.aplicar(b["text"], reglas, deshechas)
        if hechos:
            b["text"] = texto
            nuevos += [dict(h, start=b["start"], texto=texto) for h in hechos]
    if not nuevos:
        return 0
    copia_original(rel, base, auto=True)
    reg["cambios"] += nuevos
    guardar_registro(rel, reg)  # antes que el texto: si esto falla, no queda ningun cambio sin su marca
    _escribir_bloques(base, bloques)
    n = len(nuevos)
    log(f"✦ {os.path.basename(abs_path)}: {n} cambio{'s' if n != 1 else ''} con lo aprendido de tus correcciones")
    return n


def _por_inicio(cambios):
    out = {}
    for c in cambios:
        out.setdefault(aprender.clave(c["start"]), []).append(c)
    return out


def aprendido_publico(rel, segments):
    """Para la transcripcion en la interfaz: lo que cambio Tenca, {"marcas": [{"start", "pos", "len", "de", "a"}] (las
    que aun se encuentran en el texto) y "reglas": [{"de", "a", "veces"}] (para «Deshacer»)}, o None si nada."""
    reg = registro_aprendido(rel)
    if not reg["cambios"]:
        return None
    textos = {aprender.clave(s["start"]): s["text"] for s in segments if s.get("start") is not None}
    orig = {aprender.clave(s["start"]): s["text"] for s in original_de(rel) or [] if s.get("start") is not None}
    marcas, reglas = [], {}
    for start, es in _por_inicio(reg["cambios"]).items():
        texto = textos.get(start)
        pos = aprender.posiciones(texto, es, orig.get(start)) if texto is not None else [None] * len(es)
        for e, p in zip(es, pos):
            if p is None:
                continue
            # en unidades UTF-16, como cuenta JavaScript (un emoji delante movia el subrayado)
            marcas.append({"start": e["start"], "pos": _utf16(texto[:p]), "len": _utf16(e["a"]), "de": e["de"],
                           "a": e["a"]})
            r = reglas.setdefault((aprender.normal(e["de"]), e["a"]), {"formas": Counter(), "a": e["a"], "veces": 0})
            r["formas"][e["de"]] += 1
            r["veces"] += 1
    if not marcas:
        return None
    return {"marcas": marcas, "reglas": [{"de": r["formas"].most_common(1)[0][0], "a": r["a"], "veces": r["veces"]}
                                         for r in reglas.values()]}


def _utf16(s):
    return len(s.encode("utf-16-le")) // 2


def deshacer_aprendido(rel, de, a):
    """«Deshacer» de una regla en una reunion: vuelve a poner lo que escribio Whisper donde Tenca puso `a` en lugar de
    `de` (las frases que has cambiado despues y ya no se sabe donde esta, no) y no la vuelve a aplicar en esta
    reunion. Devuelve (deshechos, no deshechos)."""
    abs_path = safe(rel, "file")
    base = os.path.splitext(abs_path)[0]
    reg = registro_aprendido(rel)
    k = aprender.normal(de)

    def quitar(e):
        return aprender.normal(e["de"]) == k and e["a"] == a
    if not k or not any(quitar(e) for e in reg["cambios"]):
        raise ApiError("Ese cambio ya no está en esta transcripción.")
    if not os.path.isfile(base + ".srt"):
        raise ApiError("No se encuentra la transcripción (.srt) de esta reunión.")
    bloques = aprender.srt_bloques(read_text(base + ".srt"))
    por_inicio = {b["start"]: b for b in bloques}
    orig = {aprender.clave(s["start"]): s["text"] for s in original_de(rel) or [] if s.get("start") is not None}
    hechos = saltados = 0
    quedan = []
    for start, es in _por_inicio(reg["cambios"]).items():
        b = por_inicio.get(start)
        if not any(quitar(e) for e in es):
            quedan += es
            continue
        if b is None:  # la frase ya no esta (la borraste)
            saltados += sum(1 for e in es if quitar(e))
            quedan += [e for e in es if not quitar(e)]
            continue
        texto, q, n, s = aprender.deshacer(b["text"], es, quitar, orig.get(start))
        b["text"], hechos, saltados = texto, hechos + n, saltados + s
        quedan += q
    if hechos:
        _escribir_bloques(base, bloques)
    reg["cambios"] = quedan
    if k not in reg["deshechas"]:
        reg["deshechas"].append(k)
    guardar_registro(rel, reg)
    log(f"↺ {os.path.basename(abs_path)}: {hechos} cambio{'s' if hechos != 1 else ''} aprendido"
        f"{'s' if hechos != 1 else ''} deshecho{'s' if hechos != 1 else ''}")
    return hechos, saltados


def _nombre_lista(v):
    return os.path.basename(v["_abs"])


def motivo_lista(e, v):
    """Por que no se ha podido escribir en una lista, en una frase y con que hacer."""
    nombre, carpeta = _nombre_lista(v), os.path.dirname(v["_abs"])
    if isinstance(e, FileNotFoundError) or not os.path.isdir(carpeta):
        return (f"No encuentro la carpeta de «{nombre}»: ¿sin conexión o con el disco desconectado? Vuelve a probar "
                "cuando esté, o elige otra lista.")
    if isinstance(e, PermissionError):
        if getattr(e, "winerror", None) in (32, 33):
            return f"«{nombre}» está abierta en otro programa. Ciérrala y vuelve a probar."
        return (f"No puedo escribir en «{nombre}»: es de solo lectura (quizá es una carpeta compartida). Añádelo tú en "
                "el archivo, o elige otra lista.")
    return f"No se ha podido escribir en «{nombre}» ({e.__class__.__name__}). Vuelve a probar, o elige otra lista."


def anadir_a_lista(v, termino):
    """Añade `termino` al final de la lista `v` (una de list_vocab, tambien de una carpeta conectada) sin tocarle nada
    mas (aprender.anadir_termino). Devuelve si ya estaba. ApiError con el motivo si no se puede."""
    path = v["_abs"]
    try:
        if aprender.normal(termino) in {aprender.normal(t) for t in read_vocab(path)[1]}:
            return True
        info = os.stat(path)
        if getattr(info, "st_file_attributes", 0) & CLOUD_ONLY:
            raise ApiError(f"«{_nombre_lista(v)}» solo está en OneDrive, no en este PC: ábrela una vez para que se "
                           "descargue y vuelve a probar.")
        if info.st_size > VOCAB_MAX_BYTES:
            raise ApiError(f"«{_nombre_lista(v)}» es demasiado grande para ser una lista. Elige otra.")
        if not os.access(path, os.W_OK):
            raise PermissionError(errno.EACCES, "solo lectura", path)
        with open(path, "rb") as f:
            raw = f.read()
        nuevo = aprender.anadir_termino(raw, termino)
        write_bytes(path, nuevo)
    except aprender.NoSePuede:
        raise ApiError(f"«{_nombre_lista(v)}» está guardada en un formato que no sé tocar sin estropearla. Añádelo tú "
                       "en el archivo, o elige otra lista.")
    except OSError as e:
        raise ApiError(motivo_lista(e, v))
    return False
