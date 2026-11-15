# -*- coding: utf-8 -*-
"""Las listas de vocabulario (las de cada proyecto, <proyecto>/Vocabulario/*.txt, y las de las carpetas de listas
conectadas), cual le toca a cada reunion (find_vocab, vocab_for), importarlas, y el perfil de reunion de los ajustes
(carpeta, lista, contexto y salidas; perfil_de, resolve_perfil, check_perfil) con su idioma.

Salio tal cual de server.py al partirlo (15/11/2026); server.py lo reexporta."""
import copy
import os
import re
import shutil
import unicodedata

import calendario
import engines
import salidas
from ajustes import DEFAULT_SETTINGS, PROCESADOS
from biblioteca import IDIOMA_RE, VOCAB_FOLDER, parse_notes, temas
from common import APP_NAME, ApiError, clean_name, fold, log, log_exc, natural_key, read_text
from rutas import (APP_DIR, AUDIO_EXT, CLOUD_ONLY, LINKS, ROOT, VLINK, VOCAB_LINKS, _inside, in_app_dir, link_id,
                   rel_of, root_name, safe)


LIVE_PRESETS = tuple(p["id"] for p in engines.PRESETS)  # en directo: por lotes y beam 1, cambia el modelo


VOCAB_MAX_BYTES = 64 * 1024  # una .txt de una carpeta conectada mas grande no es una lista (Whisper lee ~45 terminos)


def _decode_list(raw):
    """Texto de una lista: UTF-8 (con o sin BOM), UTF-16 con BOM (Excel «Texto Unicode», el Bloc de notas «Unicode») o,
    linea a linea, la pagina de codigos de Windows donde no es UTF-8 (cp1252: el Bloc de notas antiguo y Excel «Texto
    delimitado por tabulaciones»). Antes todo se leia como UTF-8 y «José» llegaba a Whisper como «Jos�». Por lineas:
    un byte suelto (o un fichero leido a medio guardar) solo cambia su linea; con BOM de UTF-8, esa linea lleva «�»."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    bom = raw.startswith(b"\xef\xbb\xbf")
    out = []
    for line in (raw[3:] if bom else raw).split(b"\n"):
        try:
            out.append(line.decode("utf-8"))
        except UnicodeDecodeError:
            out.append(line.decode("utf-8", errors="replace") if bom else line.decode("cp1252", errors="replace"))
    return "\n".join(out)


def read_vocab(path):
    desc, terms = [], []
    with open(path, "rb") as f:
        text = _decode_list(f.read())
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            desc.append(s.lstrip("#").strip())
        elif s:
            terms += engines.split_terms(s.replace("\t", ","))  # columnas de Excel separadas por tabuladores
    return " ".join(d for d in desc if d), terms


def _one_per_line(path):
    """La lista esta escrita con un termino por linea (sin comas, puntos y comas ni tabuladores) y tiene mas de uno."""
    with open(path, "rb") as f:
        lines = [s.strip() for s in _decode_list(f.read()).splitlines()]
    lines = [s for s in lines if s and not s.startswith("#")]
    return len(lines) > 1 and not any(re.search(r"[,;\t]", s) for s in lines)


def vocab_link_name(path):
    """Nombre de proyecto de una carpeta de listas conectada: el de la carpeta o, si se llama Vocabulario, el de la
    carpeta que la contiene (la del proyecto, como al importar)."""
    path = os.path.normpath(path)
    name = os.path.basename(path)
    if fold(name) == VOCAB_FOLDER and os.path.basename(os.path.dirname(path)):
        name = os.path.basename(os.path.dirname(path))
    return unicodedata.normalize("NFC", name)


def _vocab_link_ok(f, base, audio_stems, avisos):
    """Una .txt de una carpeta conectada vale como lista si no es oculta (._x.txt de un USB de Mac), ni la transcripcion
    de una grabacion de al lado (<audio>.txt, <audio>.hablantes.txt), ni esta solo en OneDrive (leerla la descargaria:
    se salta con aviso), ni pesa mas de VOCAB_MAX_BYTES (no es una lista: tambien con aviso)."""
    if f.name.startswith((".", "~")):
        return False
    stem = f.name[:-4].lower()
    if stem in audio_stems or (stem.endswith(".hablantes") and stem[:-len(".hablantes")] in audio_stems):
        return False
    try:
        st = f.stat()
    except OSError:
        return False
    rel = os.path.relpath(f.path, base).replace(os.sep, "\\")
    if getattr(st, "st_file_attributes", 0) & CLOUD_ONLY:
        avisos.append({"lista": rel, "motivo": "nube"})
        return False
    if st.st_size > VOCAB_MAX_BYTES:
        avisos.append({"lista": rel, "motivo": "grande", "kb": -(-st.st_size // 1024)})
        return False
    # un documento de reunion de los destinos de salida (lleva la marca «generado_por» arriba): un destino con
    # «subcarpeta por proyecto» que contiene la carpeta conectada los escribiria ahi
    return not salidas.is_ours(f.path, "txt")


def _list_vocab(avisos=None, conectadas=True):
    """list_vocab y, en `avisos` (id de carpeta conectada -> [{"lista", "motivo"}]), las listas que se saltan de las
    carpetas conectadas. conectadas=False: solo las del arbol (sin leer las carpetas conectadas, que pueden estar en
    OneDrive o en la red)."""
    out, seen = [], set()

    def norm(p):
        return os.path.normcase(os.path.normpath(p))

    vlinks = list(VOCAB_LINKS.items())
    # el proyecto de las listas de la raiz se llama como su carpeta, igual que todos (root_name: «Chuleta» si se instalo
    # encima de Chuleta); asi sus reuniones y sus documentos «por proyecto» siguen con el nombre de antes (09/11/2026)
    top_names = {norm(ROOT): root_name(), **{norm(l["path"]): l["name"] for l in LINKS.values()}}
    top_names.update({norm(l["path"]): l["name"] for _, l in vlinks if norm(l["path"]) not in top_names})

    def add(f, project, d, extra):
        key = norm(f.path)
        if key in seen:  # una carpeta conectada que contiene otra de la app: la lista ya esta, con su id de siempre
            return
        seen.add(key)
        out.append(dict({"id": rel_of(f.path), "project": project, "folder": rel_of(d),
                         "name": unicodedata.normalize("NFC", f.name[:-4]), "_abs": f.path}, **extra))

    def txts(d, ok):
        entries = list(os.scandir(d))
        files = [f for f in entries if f.is_file() and f.name.lower().endswith(".txt")]
        if ok:  # carpeta conectada: fuera las transcripciones de las grabaciones de al lado y lo que no es una lista
            stems = {s.lower() for s, x in (os.path.splitext(e.name) for e in entries) if x.lower() in AUDIO_EXT}
            files = [f for f in files if ok(f, stems)]
        return sorted(files, key=lambda f: (fold(f.name) != "general.txt", natural_key(f.name)))

    def walk(d, depth, max_depth=3, ok=None, extra=None):
        try:
            entries = sorted(os.scandir(d), key=lambda e: natural_key(e.name))
        except OSError:
            return
        for e in entries:
            if not e.is_dir() or e.name.startswith((".", "_")) or in_app_dir(e.path):
                continue
            if fold(e.name) != VOCAB_FOLDER:
                if depth < max_depth:
                    walk(e.path, depth + 1, max_depth, ok, extra)
                continue
            project = unicodedata.normalize("NFC", top_names.get(norm(d)) or os.path.basename(d))
            try:
                files = txts(e.path, ok)
            except OSError:
                continue
            for f in files:
                add(f, project, d, extra or {})

    walk(ROOT, 0)
    for l in LINKS.values():  # tambien las carpetas vinculadas (p. ej. la del proyecto en OneDrive)
        walk(l["path"], 0)
    # y las carpetas de listas conectadas, en tres formas: <carpeta>\*.txt sueltas y <carpeta>\Vocabulario\*.txt (del
    # proyecto que da su nombre la carpeta: vocab_link_name) y <carpeta>\<Proyecto>\Vocabulario\*.txt
    for vid, l in (vlinks if conectadas else ()):
        base, warn = l["path"], [] if avisos is None else avisos.setdefault(vid, [])
        ok = lambda f, stems, base=base, warn=warn: _vocab_link_ok(f, base, stems, warn)  # noqa: E731
        extra = {"conectada": VLINK + vid}
        try:
            loose = txts(base, ok)
        except OSError:
            continue  # la carpeta ya no esta (renombrada, sin VPN, disco desconectado): sin listas
        first = len(out)
        for f in loose:
            add(f, unicodedata.normalize("NFC", l["name"]), base, dict(extra, _dir=base))
        walk(base, 0, max_depth=1, ok=ok, extra=extra)
        # dentro de una misma carpeta conectada, un tema por proyecto: Infra.txt suelta y Vocabulario\Infra.txt se
        # llamarian igual y, por nombres, no se sabria cual usar (find_vocab daria ninguna). Vale la primera
        seen_names, keep = set(), out[:first]
        for v in out[first:]:
            key = (fold(v["project"]), fold(v["name"]))
            if key in seen_names:
                warn.append({"lista": os.path.relpath(v["_abs"], base).replace(os.sep, "\\"), "motivo": "repetida"})
                continue
            seen_names.add(key)
            keep.append(v)
        out[:] = keep
    return out


def list_vocab():
    return _list_vocab()


def vocab_links_estado(vs=None, avisos=None):
    """Para la interfaz: cada carpeta de listas conectada con su ruta interna, nombre, ruta, si existe, cuantas listas
    da y lo que se salta (avisos). `vs` y `avisos`: los de una llamada a _list_vocab ya hecha."""
    if vs is None:
        avisos = {}
        vs = _list_vocab(avisos)
    out = []
    for vid, l in list(VOCAB_LINKS.items()):
        rel = VLINK + vid
        out.append({"rel": rel, "name": l["name"], "path": l["path"], "existe": os.path.isdir(l["path"]),
                    "listas": sum(1 for v in vs if v.get("conectada") == rel), "avisos": (avisos or {}).get(vid, [])})
    return out


def _project_dir(v):
    """La carpeta del proyecto de una lista: la que contiene su carpeta Vocabulario (o, con las listas sueltas de una
    carpeta conectada, esa carpeta: si no, seria la de encima y podria contener reuniones de otro proyecto)."""
    return v.get("_dir") or os.path.dirname(os.path.dirname(v["_abs"]))


def root_names():
    """Los nombres del proyecto de las listas de la raiz: el de su carpeta (root_name, con el que se llama ahora), el
    de la app y los que tuvo («Chuleta», «Notas»: reuniones y ajustes guardados con ellos). Tambien para la interfaz
    (/api/config: notas.js, deProyecto)."""
    from common import APP_FORMER_NAMES
    return list(dict.fromkeys((root_name(), APP_NAME) + tuple(APP_FORMER_NAMES)))


def _is_app_name(project):
    """El proyecto de las listas de la raiz con cualquiera de sus nombres (root_names)."""
    return fold(project or "") in {fold(a) for a in root_names()}


def _of_project(v, project):
    """La lista es del proyecto `project`: se llama asi o, con el nombre de la app (o uno antiguo), es de la raiz."""
    return fold(v["project"]) == fold(project or "") or (
        _is_app_name(project) and os.path.normcase(os.path.normpath(_project_dir(v)))
        == os.path.normcase(os.path.normpath(ROOT)))


def _project_known(project, vs):
    """Algun proyecto se llama `project`: el de la app (la raiz siempre existe) o el de alguna lista de `vs`."""
    return _is_app_name(project) or any(_of_project(v, project) for v in vs)


def _norm_dir(p):
    return os.path.normcase(os.path.normpath(p))


def _closest(lists, near):
    """De varias listas (proyectos que se llaman igual), la del proyecto que contiene la reunion `near` (el mas
    cercano); None si no hay una sola."""
    if not near:
        return None
    mine = sorted((v for v in lists if _inside(os.path.dirname(near), _project_dir(v))),
                  key=lambda v: -len(_norm_dir(_project_dir(v))))
    if mine and (len(mine) == 1 or len(_norm_dir(_project_dir(mine[0]))) > len(_norm_dir(_project_dir(mine[1])))):
        return mine[0]
    return None


def find_vocab(project, name, id_=None, near=None):
    """La lista de un tema. Por id (la ruta interna de la lista: estable aunque otro proyecto se llame igual), si se
    llama como dicen `project` y `name` (los nombres de la cabecera de la reunion; sin ellos, el id basta, y si ningun
    proyecto se llama ya como dice la cabecera, basta el tema: la vinculada tiene otro nombre en este PC, o el otro PC
    escribio el suyo); si no, por nombres: proyecto + tema (el proyecto de las listas de la raiz tambien con sus
    nombres antiguos). Si dos
    proyectos se llaman igual, `near` (la ruta del audio de la reunion) elige la del proyecto que la contiene; si sigue
    sin estar claro, ninguna. Con proyecto nunca se toma la lista de otro proyecto (antes, si solo habia un tema con
    ese nombre, se usaba aunque fuera de otro cliente).
    Si ningun proyecto se llama ya asi (se renombro su carpeta fuera de la app, o la vinculada tiene otro nombre en
    este PC), vale la lista de ese tema si es la unica fuera de la raiz y la reunion esta en la carpeta de su proyecto
    (la mas cercana de las que tienen listas): es su proyecto con el nombre nuevo. Con dos o mas no se sabe cual es,
    porque la reunion puede estar guardada en la carpeta de otro cliente. Con un id guardado que no es el de esa lista
    (si no, ya se habria devuelto), tampoco: la reunion ya tenia esa ruta cuando se eligio su lista, asi que no estaba
    dentro de la carpeta renombrada (la ruta de las de dentro ha cambiado con ella y no tienen id)."""
    vs = list_vocab()
    if id_:
        hit = next((v for v in vs if v["id"] == id_), None)
        if hit and (not name or (fold(hit["name"]) == fold(name) and (
                not project or _of_project(hit, project) or not _project_known(project, vs)))):
            return hit
        if not name:
            return None
    if not name:
        return None
    same = [v for v in vs if fold(v["name"]) == fold(name)]
    if not project:
        return same[0] if len(same) == 1 else None
    named = [v for v in same if _of_project(v, project)]
    if len(named) == 1:
        return named[0]
    if named:  # varios proyectos con ese nombre: el que contiene la reunion
        hit = _closest(named, near)
        own = [v for v in named if not v.get("conectada")]
        # si no, la de la app o la vinculada si solo hay una: conectar una carpeta con las mismas listas (las que se
        # importaron de ella, por ejemplo) no deja sin vocabulario a las reuniones que ya lo tenian
        return hit or (own[0] if len(own) == 1 and len(own) < len(named) else None)
    if id_ or not near or _project_known(project, vs):
        return None
    root = _norm_dir(ROOT)  # la raiz no cuenta: las contiene todas y sus listas solo valen por su nombre
    cands = [v for v in same if _norm_dir(_project_dir(v)) != root]
    homes = {_norm_dir(_project_dir(v)) for v in vs} - {root}
    home = max((d for d in homes if _inside(os.path.dirname(near), d)), key=len, default=None)
    return cands[0] if len(cands) == 1 and home == _norm_dir(_project_dir(cands[0])) else None


def theme_vocab(props, abs_audio, changed=()):
    """El tema de una reunion cuando la peticion lo cambia (al grabar y en la pagina de la reunion): en `props` (la
    cabecera de su .md) quedan los nombres, proyecto y workstream, y el id de su lista va a temas() (ver Temas).
    Por id (`vocabulario` en la peticion; "" = sin tema), proyecto y tema salen de esa lista; por nombres, find_vocab
    la busca (y el proyecto pasa a llamarse como ahora: «Notas» es el de la raiz), salvo que la que ya tenia se llame
    asi: con dos proyectos que se llaman igual, los nombres no dicen cual. Al guardar las notas u otra propiedad no
    cambia nada (una reunion de antes se queda con su cabecera). `changed`: lo que trae la peticion (lo demas viene
    del .md)."""
    changed = changed or {}
    if not {"proyecto", "workstream", "vocabulario"} & set(changed):
        return
    rel, store = rel_of(abs_audio), temas()
    by_id = "vocabulario" in changed
    vid = str(changed.get("vocabulario") or "") if by_id else ""
    try:
        v = find_vocab(None, None, vid) if vid else None
        if by_id and (v or not vid):  # elegido por id: proyecto y tema salen de esa lista ("" = sin tema)
            props.pop("proyecto", None)
            props.pop("workstream", None)
        elif props.get("workstream"):  # por nombres (o un id que ya no existe: como si no hubiera venido)
            v = find_vocab(props.get("proyecto"), props["workstream"], store.get(rel), near=abs_audio)
        else:
            v = None
    except OSError:
        return
    if v:
        props.update(proyecto=v["project"], workstream=v["name"])
    try:
        store.set(rel, v["id"] if v else None)
    except OSError:
        log_exc()


def vocab_info(v, language):
    """Para la interfaz: cuantos terminos hay y cuales no le caben a Whisper junto a la frase de estilo."""
    desc, terms = read_vocab(v["_abs"])
    style = engines.style_for(language)
    text = ", ".join(terms)
    _, dropped = engines.build_prompt(style, text)
    return dict({k: x for k, x in v.items() if not k.startswith("_")}, desc=desc, terms=len(terms), text=text,
                tokens=engines.count_tokens(text), style_tokens=engines.count_tokens(style),
                limit=engines.PROMPT_TOKENS, dropped=dropped)


def vocab_fit(text, language):
    """Lo mismo que vocab_info pero para una lista que se esta escribiendo (aun sin guardar)."""
    style = engines.style_for(language)
    terms = engines.split_terms(text)
    joined = ", ".join(terms)
    return {"terms": len(terms), "tokens": engines.count_tokens(joined), "style_tokens": engines.count_tokens(style),
            "limit": engines.PROMPT_TOKENS, "dropped": engines.build_prompt(style, joined)[1]}


def md_props(abs_audio):
    """Propiedades de la cabecera del .md de una grabacion ({} si no hay o no se puede leer)."""
    md = os.path.splitext(abs_audio)[0] + ".md"
    try:
        return parse_notes(read_text(md))[0] if os.path.isfile(md) else {}
    except OSError:
        return {}


def vocab_for(abs_audio, st, props=None):
    """Vocabulario para Whisper: el del tema de la reunion o, si no tiene, el general de Ajustes."""
    props = md_props(abs_audio) if props is None else props
    try:
        vid = temas().get(rel_of(abs_audio)) if props.get("workstream") else None  # el id solo desempata
        v = find_vocab(props.get("proyecto"), props.get("workstream"), vid, near=abs_audio)
        if v:
            return ", ".join(read_vocab(v["_abs"])[1])
    except OSError:
        pass
    return st.get("prompt") or ""


# Lo que se aplica a cada grabacion: carpeta de destino, idioma, tema de vocabulario y contexto libre. El perfil
# por defecto vive en los ajustes ("perfil" + "language"); por reunion, idioma y contexto van en la cabecera del .md
# y el directo los relee en cada trozo (Jobs.refresh_prompt). Prioridad: lo elegido para esa reunion > lo que fija
# el perfil > la sugerencia de la reunion en curso (Teams/Outlook) > lo ultimo usado.
PERFIL_KEYS = ("folder", "vocab", "context", "outputs")


def perfil_de(st):
    """El perfil por defecto normalizado (con el idioma, que sigue en "language")."""
    p = dict(DEFAULT_SETTINGS["perfil"], **(st.get("perfil") or {}))
    return {"folder": p["folder"] if isinstance(p.get("folder"), str) else None,
            "vocab": p["vocab"] if isinstance(p.get("vocab"), str) else None,
            "context": " ".join(str(p.get("context") or "").split())[:engines.ABOUT_CHARS],
            "outputs": salidas.destinos_de(p.get("outputs")),
            "language": st.get("language") or ""}


def perfil_folder(st):
    """Carpeta fija del perfil si sigue existiendo; None si el perfil esta en automatico o la carpeta ya no esta."""
    rel = perfil_de(st)["folder"]
    if rel is None:
        return None
    try:
        safe(rel, "dir")
    except ApiError:
        return None
    return rel


def perfil_vocab_fijo(st):
    """Tema fijo del perfil ("" = sin vocabulario, tambien fijo) si su lista sigue existiendo; None si el perfil esta
    en automatico o la lista ya no esta (borrada o renombrada fuera de la app): entonces vale lo automatico, como con
    la carpeta (perfil_folder), y no una grabacion sin vocabulario."""
    vid = perfil_de(st)["vocab"]
    if not vid:
        return vid
    try:
        return vid if find_vocab(None, None, vid) else None
    except OSError:
        return None


def perfil_vocab(st, sugerido=None):
    """Tema de una grabacion nueva: el fijo del perfil (puede ser "" = sin vocabulario); si el perfil lo deja en
    automatico, el que sugiera la reunion en curso y, si no, el ultimo elegido."""
    fijo = perfil_vocab_fijo(st)
    return fijo if fijo is not None else (sugerido or st.get("rec_vocab") or "")


def idioma_repaso(usados):
    """Idioma del repaso final de un directo en el que se cambió el idioma de la reunión (msg 1150: empezó en inglés y
    pasó al portugués). Con el idioma del final para toda la grabación, lo dicho antes se pasaría a ese idioma; con
    los que se usaron, su mezcla si existe (en + pt → «en-pt»; es + en + pt → «es-en-pt») y si no, detectar ("").
    None: no cambió (el de siempre, el de la reunión al terminar). usados: los idiomas fijados en la sesión, en orden
    (None = detectar)."""
    usados = list(usados or [])
    if len(usados) < 2:
        return None
    sueltos = set()
    for u in usados:
        if not u:
            return ""  # en algún momento se detectaba: detectar
        sueltos.update(engines.PAIRS.get(u, (u,)))
    if len(sueltos) == 1:
        return next(iter(sueltos))
    return engines.mix_code(sueltos) or ""  # la mezcla de esos dos o de los tres (es-en-pt); si no la hay, detectar


def idioma_de(props, st):
    """Idioma de una reunion: el de su .md ("auto" = detectar) o, si no lo fija, el del perfil (Ajustes)."""
    v = str((props or {}).get("idioma") or "").strip().lower()
    if v == "auto":
        return ""
    return v if IDIOMA_RE.match(v) else (st.get("language") or "")


def meeting_setup(abs_audio, st):
    """Lo que esta reunion le pide a Whisper: vocabulario del tema, contexto libre e idioma. Se lee al arrancar
    una transcripcion y, en directo, en cada trozo (refresh_prompt lo vuelve a leer al cambiarlo en la pagina)."""
    props = md_props(abs_audio)
    return {"prompt": vocab_for(abs_audio, st, props),
            "about": " ".join(str(props.get("contexto") or "").split())[:engines.ABOUT_CHARS],
            "language": idioma_de(props, st)}


def resolve_perfil(st, sug=None, elegido=None, auto_folder=None):
    """Lo que se aplica a una grabacion nueva y de donde sale cada campo ("origen": elegido / perfil / reunion /
    ultimo). `sug` es la sugerencia de la reunion en curso (calendario.suggest) y `elegido` lo que se cambio en el
    dialogo para esa reunion; `auto_folder(vocab_id)` calcula la carpeta automatica (la ultima usada)."""
    sug, elegido, p = dict(sug or {}), dict(elegido or {}), perfil_de(st)
    auto_folder = auto_folder or (lambda vid: st.get("rec_folder") or "")
    out, origen = {}, {}

    def pick(key, *cands):
        for origin, val in cands:
            if val is not None:
                out[key], origen[key] = val, origin
                return

    pick("vocab", ("elegido", elegido.get("vocab")), ("perfil", perfil_vocab_fijo(st)),
         ("reunion", sug.get("vocab") or None),
         ("ultimo", st.get("rec_vocab") or ""))
    auto = auto_folder(out["vocab"])
    if isinstance(auto, tuple):  # (carpeta, origen) de rec_folder_origin: la regla entera (proyecto antes que perfil)
        pick("folder", ("elegido", elegido.get("folder")), (auto[1], auto[0]))
    else:
        pick("folder", ("elegido", elegido.get("folder")), ("perfil", perfil_folder(st)), ("ultimo", auto or ""))
    pick("language", ("elegido", elegido.get("language")), ("perfil", p["language"]))
    pick("context", ("elegido", elegido.get("context")), ("perfil", p["context"]))
    pick("name", ("elegido", elegido.get("name") or None), ("reunion", sug.get("name") or None), ("ultimo", ""))
    pick("participantes", ("elegido", elegido.get("participantes") or None),
         ("reunion", calendario.join_participants(sug.get("participants")) or None), ("ultimo", ""))
    return dict(out, origen=origen)


def check_perfil(b, known=None, actual=None):
    """Valida lo que llega para el perfil (solo las claves presentes) y lo devuelve normalizado. `known`: los destinos
    de salida ya guardados (se aceptan aunque ahora no esten disponibles; los nuevos tienen que existir). `actual`: el
    perfil guardado; su carpeta y su lista, sin cambiar, se aceptan aunque ahora no se encuentren (una vinculada
    desconectada, OneDrive sin conexion): Ajustes las devuelve tal cual con cualquier «Guardar» y no deben perderse ni
    impedir guardar lo demas (revision 2 de B2, P1-3)."""
    out, actual = {}, actual or {}
    if "folder" in b:
        rel = b["folder"]
        if rel is not None:
            rel = str(rel).replace("\\", "/").strip("/")
            if rel != actual.get("folder"):
                folder = safe(rel, "dir")
                if in_app_dir(folder):
                    raise ApiError("No se puede grabar ahí")
        out["folder"] = rel
    if "vocab" in b:
        vid = b["vocab"]
        if vid is not None:
            vid = str(vid).strip()
            if vid and vid != actual.get("vocab") and not find_vocab(None, None, vid):
                raise ApiError("Ese workstream ya no existe.")
        out["vocab"] = vid
    if "context" in b:
        out["context"] = " ".join(str(b["context"] or "").split())[:engines.ABOUT_CHARS]
    if "outputs" in b:
        roots = [ROOT] + [l["path"] for l in LINKS.values()]  # donde viven las grabaciones: ahi no van documentos
        old = [d["path"] for d in salidas.destinos_de(known)]
        out["outputs"] = salidas.check_destinos(b["outputs"], APP_DIR, roots, old)
        for o in out["outputs"]:  # ni en una carpeta de listas conectada ni encima de una (con «subcarpeta por
            # proyecto», los documentos caerian dentro): sus .txt se tomarian por listas
            if any(_inside(o["path"], k) and _inside(k, o["path"]) for k in old):
                continue  # ya estaba guardado: se acepta (si no, no se podria guardar ningun otro ajuste)
            for l in list(VOCAB_LINKS.values()):
                if _inside(o["path"], l["path"]):
                    raise ApiError(f"Esa carpeta es la de tus listas de vocabulario («{l['name']}»): los documentos "
                                   "de las reuniones se tomarían por listas. Elige otra carpeta para los documentos.")
                if _inside(l["path"], o["path"]):
                    raise ApiError(f"Dentro de esa carpeta está la de tus listas de vocabulario («{l['name']}»): con "
                                   "«subcarpeta por proyecto», los documentos de las reuniones podrían caer en ella. "
                                   "Elige otra carpeta para los documentos.")
    if "language" in b:
        lang = str(b["language"] or "").strip().lower()
        if lang and not IDIOMA_RE.match(lang):
            raise ApiError("Idioma no válido: " + lang)
        out["language"] = lang
    return out


def update_perfil(d, cambios):
    """Aplica a los ajustes `d` un perfil ya validado por check_perfil."""
    perfil = dict(DEFAULT_SETTINGS["perfil"], **(d.get("perfil") or {}))
    for k in PERFIL_KEYS:
        if k in cambios:
            perfil[k] = cambios[k]
    d["perfil"] = perfil
    if "language" in cambios:
        d["language"] = cambios["language"]


SIN_AUDIO = "Sin micrófono y sin el audio del PC no se graba nada: deja al menos uno de los dos."
AJUSTES_CAMBIADOS = (f"Los ajustes han cambiado desde que abriste Ajustes (en otra ventana o al reiniciar "
                     f"{APP_NAME}) y esto no se ha guardado, para no borrar nada. Ya ves lo guardado: vuelve a "
                     "hacer el cambio.")


def ajustes_previstos(st, a):
    """Los ajustes con lo que Ajustes › Al grabar tiene escrito y sin guardar (solo para enseñar el plan; no se guarda
    nada). Se aceptan las claves de esa sección, validadas como al guardar; lo que no vale se ignora."""
    st = copy.deepcopy(st)
    lang = str(a.get("language") or "").strip().lower() if "language" in a else None
    if lang is not None and (not lang or IDIOMA_RE.match(lang)):
        st["language"] = lang
    for k in ("rec_mic", "rec_sys"):
        if isinstance(a.get(k), str) and a[k].strip():
            st[k] = a[k].strip()[:300]
    if "rec_directo" in a:
        st["rec_directo"] = bool(a["rec_directo"])
    if a.get("live_preset") in LIVE_PRESETS:
        st["live_preset"] = a["live_preset"]
    if a.get("preset") in engines.PRESETS_BY_ID:  # Procesado › Motores: el de después («Solo grabar»)
        st["preset"] = a["preset"]
    if a.get("procesado") in PROCESADOS:  # la opción marcada en Procesado, aplicada o no: «Guardar» también la aplica
        st["procesado"] = a["procesado"]
    s = a.get("summary") if isinstance(a.get("summary"), dict) else {}
    for k, clave in (("auto", "summary_auto"), ("local", "summary_auto_local")):
        if k in s:
            st[clave] = bool(s[k])
    if isinstance(a.get("prompt"), str):  # el vocabulario de las reuniones sin proyecto (Proyectos)
        st["prompt"] = a["prompt"]
    if "carpeta_por_reunion" in a:  # rama de carpetas: una carpeta por reunión
        st["carpeta_por_reunion"] = a["carpeta_por_reunion"] is not False
    if isinstance(a.get("perfil"), dict):
        try:
            claves = ("folder", "vocab", "context")
            # con el perfil guardado como referencia: su carpeta o su lista, aunque ahora no se encuentren, no tiran el
            # resto de lo escrito (el contexto) como «no vale» (revisión 3 de B2)
            update_perfil(st, check_perfil({k: x for k, x in a["perfil"].items() if k in claves}, actual=perfil_de(st)))
        except ApiError:
            pass
    return st


def _con_o_sin(d, k, val):
    """Copia del diccionario `d` con `k` = `val`, o sin `k` si `val` es None (vuelve a heredar)."""
    out = dict(d or {})
    if val is None:
        out.pop(k, None)
    else:
        out[k] = val
    return out


def has_vocab_dir(folder):
    """La carpeta es la de un proyecto: tiene una carpeta Vocabulario (sus listas toman de ella el nombre)."""
    try:
        with os.scandir(folder) as it:
            return any(e.is_dir() and fold(e.name) == VOCAB_FOLDER for e in it)
    except OSError:
        return False


def check_vocab_link(path, outputs=()):
    """Valida una carpeta para conectarla como carpeta de listas y la devuelve normalizada. No vale: la de la app (ni
    una que la contenga o contenga tu usuario: demasiado grande), una de dentro de la app o de una carpeta vinculada
    (sus listas <Proyecto>\\Vocabulario\\*.txt ya se leen solas), una que se solape con otra ya conectada, la de un
    destino de salida (`outputs`: ahi se escriben las reuniones como documentos, que se tomarian por listas) ni una
    con grabaciones (sus .txt son transcripciones)."""
    path = os.path.normpath(os.path.expandvars((path or "").strip().strip('"')))
    if not os.path.isabs(path) or not os.path.isdir(path):
        raise ApiError("Esa carpeta no existe: " + path)
    if os.path.splitdrive(path)[1] in ("", os.sep):
        raise ApiError("Elige una carpeta, no una unidad entera.")
    if in_app_dir(path):
        raise ApiError("Esa carpeta es la de la propia app.")
    if _inside(ROOT, path) or _inside(os.path.expanduser("~"), path):
        raise ApiError(f"Esa carpeta es demasiado grande (contiene la de {APP_NAME} o tu usuario): elige la carpeta "
                       "donde tienes las listas.")
    if _inside(path, ROOT):
        raise ApiError(f"Esa carpeta ya está dentro de {APP_NAME}: las listas de <Proyecto>\\Vocabulario\\ se leen "
                       "solas, sin conectarla.")
    for l in list(LINKS.values()):
        if _inside(path, l["path"]):
            raise ApiError(f"Esa carpeta está dentro de «{l['name']}», que ya está vinculada: pon las listas en "
                           f"«{l['name']}\\Vocabulario» y se leen solas.")
    vid = link_id(path)
    for other, l in list(VOCAB_LINKS.items()):
        if other != vid and (_inside(path, l["path"]) or _inside(l["path"], path)):
            raise ApiError(f"Se solapa con la carpeta de listas «{l['name']}», que ya está conectada.")
    for d in outputs or ():
        if _inside(path, d):
            raise ApiError("En esa carpeta se guardan los documentos de las reuniones (destinos de salida): se "
                           "tomarían por listas. Elige la carpeta donde tienes solo las listas.")
    audio = salidas.first_audio(path)
    if audio:
        raise ApiError(f"Esa carpeta tiene grabaciones («{audio}»): sus .txt son transcripciones. Elige la carpeta "
                       "donde tienes solo las listas.")
    return path


def import_vocab(src, project=None):
    """Copia a este PC las listas de vocabulario de otra carpeta: dentro de `src`, cada <proyecto>/Vocabulario/*.txt;
    o la propia `src` si es un proyecto (tiene una carpeta Vocabulario) o una carpeta Vocabulario suelta (va al
    proyecto `project` o, si no se da, al nombre de su carpeta padre). Cada lista va a la carpeta Vocabulario del
    proyecto (la que ya tenga; si es nuevo, <raiz>/<proyecto>/Vocabulario) y nunca se pisa una que exista.
    Devuelve {copiadas, omitidas, proyectos}."""
    src = os.path.normpath(os.path.expandvars((src or "").strip().strip('"')))
    if not os.path.isabs(src) or not os.path.isdir(src):
        raise ApiError("Esa carpeta no existe: " + src)
    if in_app_dir(src):
        raise ApiError("Esa carpeta es la de la propia app.")

    def vocab_dir(d):
        try:
            return next((e.path for e in os.scandir(d) if e.is_dir() and fold(e.name) == VOCAB_FOLDER), None)
        except OSError:
            return None

    found = []  # (proyecto, carpeta Vocabulario)
    if fold(os.path.basename(src)) == VOCAB_FOLDER:
        found.append((project or os.path.basename(os.path.dirname(src)), src))
    elif vocab_dir(src):
        found.append((project or os.path.basename(src), vocab_dir(src)))
    else:
        for e in sorted(os.scandir(src), key=lambda e: natural_key(e.name)):
            if e.is_dir() and not e.name.startswith((".", "_")) and vocab_dir(e.path):
                found.append((e.name, vocab_dir(e.path)))
    if not found:
        raise ApiError("En esa carpeta no hay listas de vocabulario (<Proyecto>\\Vocabulario\\<Workstream>.txt).")
    existing = list_vocab()
    copied, skipped, projects = 0, 0, []
    for proj, folder in found:
        try:
            proj = clean_name(proj)
        except ApiError:
            skipped += 1
            continue
        same = next((v for v in existing if fold(v["project"]) == fold(proj)), None)
        vdir = os.path.dirname(same["_abs"]) if same else os.path.join(ROOT, proj, "Vocabulario")
        files = [f for f in os.scandir(folder) if f.is_file() and f.name.lower().endswith(".txt")]
        if not files:
            continue
        os.makedirs(vdir, exist_ok=True)
        projects.append(proj)
        for f in sorted(files, key=lambda f: natural_key(f.name)):
            try:
                name = clean_name(f.name[:-4])
            except ApiError:
                skipped += 1
                continue
            dest = os.path.join(vdir, name + ".txt")
            if os.path.exists(dest):
                skipped += 1
                continue
            shutil.copyfile(f.path, dest)
            copied += 1
    log(f"🔤 vocabulario importado de {src}: {copied} listas copiadas, {skipped} omitidas ({', '.join(projects)})")
    return {"copiadas": copied, "omitidas": skipped, "proyectos": projects}
