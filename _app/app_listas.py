# -*- coding: utf-8 -*-
"""App, la parte de las carpetas vinculadas, las listas de vocabulario (de proyecto y de carpetas conectadas) y el
perfil de reunion.

Salio tal cual de server.py al partirlo (15/11/2026): App hereda de esta clase y server.py la reexporta."""
import os
import subprocess
import sys

import engines
from ajustes import settings_moved
from biblioteca import temas
from carpetas import project_folders_resolved, rec_folder_origin, to_recycle_bin
from common import APP_NAME, ApiError, clean_name, fold, log, save_text
from listas import (_list_vocab, _one_per_line, check_perfil, check_vocab_link, find_vocab, import_vocab, list_vocab,
                    perfil_de, read_vocab, resolve_perfil, update_perfil, vocab_fit, vocab_info, vocab_link_name,
                    vocab_links_estado)
from rutas import LINK, LINKS, ROOT, VLINK, VOCAB_LINKS, _inside, in_app_dir, link_id, rel_of, safe


class Listas:
    # --- carpetas vinculadas (fuera de Audios)
    def _load_links(self):
        LINKS.clear()
        for l in self.settings.get().get("links") or []:
            LINKS[link_id(l["path"])] = {"name": l.get("name") or os.path.basename(l["path"]), "path": l["path"]}

    def _save_links(self):
        self.settings.update(lambda d: d.update(links=[dict(l) for l in LINKS.values()]))
        self.library.at = 0

    def link_pick(self, b):
        """Abre el selector de carpetas de Windows (en este PC) y devuelve la que elijas. b.title: lo que dice la
        ventana (por defecto, la carpeta donde grabar; tambien se usa para la carpeta de copias y para importar)."""
        # la ruta vuelve en bytes UTF-8 escritos a mano: con pythonw (el acceso directo) la salida del hijo iria en
        # cp1252 y «José» o «Compañía» llegarian con «�» (o el hijo fallaria con «Ł»)
        script = ("import sys, tkinter as tk\nfrom tkinter import filedialog\n"
                  "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
                  "p = filedialog.askdirectory(parent=r, title=sys.argv[1], mustexist=True,"
                  " initialdir=sys.argv[2] or None)\n"
                  "sys.stdout.buffer.write((p or '').encode('utf-8', 'surrogatepass')); sys.stdout.flush()\n")
        start = os.path.dirname(ROOT) if not LINKS else os.path.dirname(next(iter(LINKS.values()))["path"])
        try:
            title = " ".join(str(b.get("title") or "").split())[:80] or "Carpeta donde guardar las grabaciones"
            r = subprocess.run([sys.executable, "-c", script, title, start],
                               capture_output=True, timeout=900, creationflags=engines.NO_WINDOW,
                               env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"))
        except (OSError, subprocess.SubprocessError) as e:
            raise ApiError(f"No se pudo abrir el selector de carpetas ({e.__class__.__name__}); pega la ruta a mano.")
        try:
            path = r.stdout.decode("utf-8", "surrogatepass").strip()
        except UnicodeDecodeError:
            path = r.stdout.decode("utf-8", "replace").strip()
        if r.returncode != 0 and not path:
            raise ApiError("No se pudo abrir el selector de carpetas; pega la ruta a mano.")
        return {"path": os.path.normpath(path) if path else ""}

    def link_add(self, b):
        path = os.path.normpath(os.path.expandvars((b.get("path") or "").strip().strip('"')))
        if not os.path.isabs(path) or not os.path.isdir(path):
            raise ApiError("Esa carpeta no existe: " + path)
        if os.path.splitdrive(path)[1] in ("", os.sep):
            raise ApiError("Elige una carpeta, no una unidad entera.")
        if _inside(path, ROOT):
            raise ApiError(f"Esa carpeta ya está dentro de {APP_NAME}: no hace falta vincularla.")
        if _inside(ROOT, path) or _inside(os.path.expanduser("~"), path):
            raise ApiError(f"Esa carpeta es demasiado grande (contiene la de {APP_NAME} o tu usuario): "
                           "elige una más concreta.")
        lid = link_id(path)
        for other, l in LINKS.items():
            if other != lid and (_inside(path, l["path"]) or _inside(l["path"], path)):
                raise ApiError(f"Se solapa con la carpeta ya vinculada «{l['name']}».")
        if lid not in LINKS:
            before = self._vocab_ids() if VOCAB_LINKS else {}  # una carpeta de listas conectada puede quedar dentro
            LINKS[lid] = {"name": clean_name(b.get("name") or os.path.basename(path)), "path": path}
            self._save_links()
            log(f"🔗 carpeta vinculada: {path}")
            if before:
                self._vocab_ids_moved(before)
        return {"rel": LINK + lid, "name": LINKS[lid]["name"]}

    def link_remove(self, b):
        with self._move_lock():
            return self._link_remove(b)

    def _link_remove(self, b):
        """Deja de mostrarla en la app. No borra ni mueve nada de la carpeta."""
        rel = b.get("path") or ""
        lid = rel[len(LINK):].split("/")[0] if rel.startswith(LINK) else ""
        if lid not in LINKS:
            raise ApiError("Esa carpeta no está vinculada.")
        busy = any(j["path"].startswith(LINK + lid) and j["status"] in ("queued", "running")
                   for j in self.jobs.snapshot())
        if busy or (self.rec and self.rec.alive() and rel_of(self.rec.path).startswith(LINK + lid)):
            raise ApiError("Se está grabando o transcribiendo algo de esa carpeta; espera a que termine.")
        before = self._vocab_ids() if VOCAB_LINKS else {}
        l = LINKS.pop(lid)
        self._save_links()
        if before:
            self._vocab_ids_moved(before)

        def quitar(d):
            d.update(
                favorites=[f for f in d.get("favorites", []) if not f["path"].startswith(LINK + lid)],
                rec_folder="" if (d.get("rec_folder") or "").startswith(LINK + lid) else d.get("rec_folder", ""),
                project_folders={k: v for k, v in (d.get("project_folders") or {}).items()
                                 if not v.startswith(LINK + lid)},
                workstream_folders={k: v for k, v in (d.get("workstream_folders") or {}).items()
                                    if not v.startswith(LINK + lid) and not k.startswith(LINK + lid)})
            # «Reuniones sin proyecto» en esa carpeta: vuelve a la automatica. Ajustes guarda al elegirla (14/11/2026):
            # antes de quitarla podia no estar guardada; ahora lo esta y se quedaba apuntando a una que ya no existe
            pf = d.get("perfil")
            if isinstance(pf, dict) and (pf.get("folder") or "").startswith(LINK + lid):
                pf["folder"] = None
        self.settings.update(quitar)
        log(f"🔗 carpeta desvinculada (no se ha tocado nada): {l['path']}")
        return {"ok": True}

    def rec_folder_for(self, v, st, vs=None):
        """La carpeta principal donde se graba una reunion con el tema `v` (la regla, en rec_folder_origin). Dentro,
        rec_start crea la carpeta de la reunion (carpeta por reunion); para saber cual sera, rec_destino. `vs`: las
        listas de list_vocab, si ya se tienen."""
        return rec_folder_origin(v, st, vs)[0]

    def carpeta_principal(self, b):
        """POST /api/proyecto/carpeta {project, vocab?, folder}: asigna la carpeta principal de un proyecto (o, con
        `vocab`, la de ese workstream: el id de su lista). folder None = sin asignar (vuelve a la del proyecto en el
        arbol). Devuelve las carpetas principales de cada proyecto (como /api/config)."""
        project, vid, rel = str(b.get("project") or "").strip(), str(b.get("vocab") or "").strip(), b.get("folder")
        if vid:
            v = find_vocab(None, None, vid)
            if not v:
                raise ApiError("Ese workstream ya no existe.")
            project = v["project"]
        if not project:
            raise ApiError("Falta el proyecto.")
        if rel is not None:
            rel = str(rel).replace("\\", "/").strip("/")
            if in_app_dir(safe(rel, "dir")):
                raise ApiError("No se puede grabar ahí")
        key, name = ("workstream_folders", vid) if vid else ("project_folders", project)

        def upd(d):
            m = dict(d.get(key) or {})
            if rel is None:
                m.pop(name, None)
            else:
                m[name] = rel
            d[key] = m
        self.settings.update(upd)
        log(f"   carpeta principal de {project}{' (workstream)' if vid else ''}: "
            + (rel if rel is not None else "la suya"))
        st = self.settings.get()
        return {"project_folders": project_folders_resolved(st),
                "workstream_folders": dict(st.get("workstream_folders") or {})}

    # --- vocabulario por workstream
    def vocab(self, q):
        st = self.settings.get()
        lang = q.get("language", st.get("language") or "")
        items, avisos = [], {}
        vs = _list_vocab(avisos)
        for v in vs:
            try:
                items.append(vocab_info(v, lang))
            except OSError:
                pass  # archivo a medio guardar o solo en OneDrive sin conexion
        return {"items": items, "last": st.get("rec_vocab") or "", "general": bool(st.get("prompt")),
                "carpetas": vocab_links_estado(vs, avisos)}  # las carpetas de listas conectadas

    def vocab_check(self, b):
        return vocab_fit(b.get("terms") or "", b.get("language", self.settings.get().get("language") or ""))

    def vocab_save(self, b):
        """Crear o editar un tema (y con el, un proyecto nuevo): <proyecto>/Vocabulario/<workstream>.txt."""
        project, name = clean_name(b.get("project")), clean_name(b.get("name"))
        terms = engines.split_terms(b.get("terms") or "")
        if not terms:
            raise ApiError("Escribe al menos un término (separados por comas).")
        desc = " ".join((b.get("desc") or "").split())
        old = find_vocab(None, None, b["id"]) if b.get("id") else None
        if old and old.get("conectada") and fold(old["project"]) != fold(project):
            # sacarla de la carpeta conectada seria borrar alli el archivo del usuario (os.remove, sin papelera)
            raise ApiError(f"«{old['name']}» está en tu carpeta de listas conectada: su proyecto («{old['project']}») "
                           "sale del nombre de su carpeta y aquí no se cambia. Cámbialo en el Explorador, o crea el "
                           "tema en el otro proyecto con «Nuevo».")
        same = next((v for v in list_vocab() if fold(v["project"]) == fold(project)), None)
        if old and fold(old["project"]) == fold(project):
            # mismo proyecto: en su misma carpeta (la conectada, si es de una). Antes se iba a la del primer proyecto
            # con ese nombre, y con dos que se llaman igual la lista se mudaba de carpeta al guardarla
            same = old
        # proyecto que ya existe: en su carpeta de vocabulario; si es nuevo, <raiz>/<proyecto>/Vocabulario
        vdir = os.path.dirname(same["_abs"]) if same else os.path.join(ROOT, project, "Vocabulario")
        path = os.path.join(vdir, name + ".txt")
        moved = old and os.path.normcase(old["_abs"]) != os.path.normcase(path)
        if (not old or moved) and os.path.exists(path):
            raise ApiError(f"Ya hay un workstream «{name}» en {project}.")
        per_line = False
        if old:
            try:
                if not moved and read_vocab(old["_abs"]) == (desc, terms):
                    return {"id": old["id"]}  # sin cambios: no se reescribe (la lista puede ser un fichero suyo)
                per_line = _one_per_line(old["_abs"])
            except OSError:
                pass
        os.makedirs(vdir, exist_ok=True)
        # como estaba escrita: un termino por linea si la lista era asi (las de las carpetas conectadas suelen serlo)
        save_text(path, (f"# {desc}\n" if desc else "") + ("\n" if per_line else ", ").join(terms) + "\n", "utf-8")
        if moved:  # se ha renombrado (o cambiado de proyecto): fuera el archivo anterior
            os.remove(old["_abs"])
            # el perfil y el ultimo tema elegido lo siguen (si no, Ctrl+Alt+R se quedaba sin vocabulario)
            self.settings.update(lambda d: settings_moved(d, old["id"], rel_of(path)))
        self.library.at = 0
        log(f"🔤 vocabulario guardado: {project} · {name} ({len(terms)} términos)")
        return {"id": rel_of(path)}

    def vocab_import(self, b):
        """POST /api/vocab/import {path, project?}: copia las listas de otra carpeta (ver import_vocab)."""
        r = import_vocab(b.get("path"), (b.get("project") or "").strip() or None)
        self.library.at = 0
        return r

    # --- carpetas de listas conectadas (se leen en vivo: nada se copia)
    def _load_vocab_links(self):
        VOCAB_LINKS.clear()
        for l in self.settings.get().get("vocab_links") or []:
            if isinstance(l, dict) and isinstance(l.get("path"), str) and l["path"].strip():
                VOCAB_LINKS[link_id(l["path"])] = {"name": l.get("name") or vocab_link_name(l["path"]),
                                                   "path": l["path"]}

    def _save_vocab_links(self):
        self.settings.update(lambda d: d.update(vocab_links=[dict(l) for l in list(VOCAB_LINKS.values())]))

    @staticmethod
    def _vocab_ids():
        """El id de cada lista por su ruta: para seguir a las que cambian de id al vincular o desvincular carpetas."""
        try:
            return {os.path.normcase(v["_abs"]): v["id"] for v in list_vocab()}
        except OSError:
            return {}

    def _vocab_ids_moved(self, before):
        """Listas que han cambiado de id (una carpeta conectada que queda dentro de una vinculada, o al reves): el tema
        fijo del perfil, el ultimo elegido y el de cada reunion las siguen. Si no, Ctrl+Alt+R grababa sin vocabulario
        (el id guardado ya no era el de ninguna lista)."""
        after = self._vocab_ids()
        pares = [(old, after[k]) for k, old in before.items() if k in after and after[k] != old]
        if not pares:
            return

        def upd(d):
            for old, new in pares:
                settings_moved(d, old, new)
        self.settings.update(upd)
        for old, new in pares:
            temas().moved(old, new, folder=True)  # solo cambian los valores: ninguna reunion tiene esa ruta
        log(f"   {len(pares)} listas de vocabulario con ruta interna nueva: el perfil y los temas las siguen")

    @staticmethod
    def _vocab_links_public():
        return [{"rel": VLINK + k, "name": l["name"], "path": l["path"]} for k, l in list(VOCAB_LINKS.items())]

    def vocab_link_add(self, b):
        """POST /api/vocab/link/add {path, name?}: conecta una carpeta de listas (check_vocab_link). Sus .txt se leen en
        vivo al grabar y al transcribir; nada se copia. Devuelve cuantas listas da y lo que se salta. Sin ninguna .txt
        que valga como lista (ni con aviso), no se conecta: seguramente no es la carpeta que se queria elegir."""
        path = check_vocab_link(b.get("path"), [d["path"] for d in perfil_de(self.settings.get())["outputs"]])
        vid = link_id(path)
        ya = vid in VOCAB_LINKS
        if not ya:
            name = clean_name(b.get("name") or vocab_link_name(path))
            VOCAB_LINKS[vid] = {"name": name, "path": path}
            estado = next(e for e in vocab_links_estado() if e["rel"] == VLINK + vid)
            if not estado["listas"] and not estado["avisos"]:
                VOCAB_LINKS.pop(vid, None)
                raise ApiError("En esa carpeta no hay listas: archivos .txt con los términos separados por comas o uno "
                               "por línea, sueltos o en <Proyecto>\\Vocabulario\\.")
            self._save_vocab_links()
            log(f"🔤 carpeta de listas conectada: {path} ({estado['listas']} listas)")
        estado = next(e for e in vocab_links_estado() if e["rel"] == VLINK + vid)
        return dict(estado, ya=ya, vocab_links=self._vocab_links_public())

    def vocab_link_remove(self, b):
        """POST /api/vocab/link/remove {rel}: deja de leer esa carpeta. No borra ni mueve nada de ella. El tema fijo del
        perfil y el ultimo elegido, si eran de alli, vuelven a «automatico» y a ninguno, y sus workstreams pierden la
        carpeta principal asignada (como al borrar un tema o desvincular una carpeta) y el idioma asignado."""
        rel = str(b.get("rel") or b.get("path") or "")
        vid = rel[len(VLINK):].split("/")[0] if rel.startswith(VLINK) else ""
        if vid not in VOCAB_LINKS:
            raise ApiError("Esa carpeta de listas no está conectada.")
        l = VOCAB_LINKS.pop(vid)
        self._save_vocab_links()
        mine = lambda x: isinstance(x, str) and x.startswith(VLINK + vid + "/")  # noqa: E731

        def upd(d):
            if mine(d.get("rec_vocab")):
                d["rec_vocab"] = ""
            if mine((d.get("perfil") or {}).get("vocab")):
                d["perfil"] = dict(d["perfil"], vocab=None)
            for clave in ("workstream_folders", "workstream_langs"):
                if any(mine(k) for k in (d.get(clave) or {})):
                    d[clave] = {k: x for k, x in d[clave].items() if not mine(k)}
        self.settings.update(upd)
        log(f"🔤 carpeta de listas desconectada (no se ha tocado nada): {l['path']}")
        return {"ok": True, "vocab_links": self._vocab_links_public()}

    # --- perfil de reunion
    def perfil_get(self, q):
        """GET /api/perfil: el perfil por defecto y lo que se aplicaria ahora mismo a una grabacion (con la reunion
        en curso, si la hay), y los avisos de los destinos de salida (documentos que no se han podido escribir)."""
        st = self.settings.get()
        sug = self._reunion_ahora(st)[0]
        auto = lambda vid: rec_folder_origin(find_vocab(None, None, vid) if vid else None, st)  # noqa: E731
        avisos = getattr(getattr(self, "jobs", None), "outputs_warnings", None)  # sin cola (tests): ninguno
        return {"perfil": perfil_de(st), "resuelto": resolve_perfil(st, sug, auto_folder=auto),
                "avisos": avisos() if avisos else []}

    def perfil_save(self, b):
        """POST /api/perfil: guarda el perfil por defecto (solo las claves que vengan: folder, vocab, context,
        outputs, language). Lo usan Ajustes y «Guardar como perfil por defecto» del dialogo Grabar.
        Con "comprobar": solo valida los destinos de salida que vengan (Ajustes, al anadir una carpeta) y los devuelve
        normalizados, sin guardar nada: asi la interfaz rechaza al momento una carpeta que no vale."""
        if b.get("comprobar"):
            return {"outputs": check_perfil({"outputs": b.get("outputs") or []})["outputs"]}
        actual = perfil_de(self.settings.get())
        cambios = check_perfil(b, actual["outputs"], actual)
        self.settings.update(lambda d: update_perfil(d, cambios))
        log("   perfil de reunión guardado: " + ", ".join(sorted(cambios)))
        return {"perfil": perfil_de(self.settings.get())}

    def vocab_delete(self, b):
        v = find_vocab(None, None, b.get("id"))
        if not v:
            raise ApiError("Ese workstream ya no existe.")
        to_recycle_bin([v["_abs"]])  # a la papelera: se puede recuperar

        def upd(d):  # el ultimo tema elegido y el fijo del perfil, si eran este: el perfil vuelve a «automatico»
            d["rec_vocab"] = "" if d.get("rec_vocab") == v["id"] else d.get("rec_vocab", "")
            if (d.get("perfil") or {}).get("vocab") == v["id"]:
                d["perfil"] = dict(d["perfil"], vocab=None)
            if v["id"] in (d.get("workstream_folders") or {}):  # su carpeta principal deja de estar asignada
                d["workstream_folders"] = {k: x for k, x in d["workstream_folders"].items() if k != v["id"]}
        self.settings.update(upd)
        self.library.at = 0
        log(f"🗑 vocabulario a la papelera: {v['project']} · {v['name']}")
        return {"ok": True}
