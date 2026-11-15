# -*- coding: utf-8 -*-
"""App, la parte de las reuniones: Acerca de y el diagnostico, la configuracion que lee la interfaz, el arbol, la
transcripcion, el resumen, las notas y etiquetas, buscar y la lista de trabajos.

Salio tal cual de server.py al partirlo (15/11/2026): App hereda de esta clase y server.py la reexporta."""
import os
import platform
import sys
import time
import webbrowser

import engines
import fallos
import informe
import voices
from ajustes import agente_de, procesado_de, proveedor_elegido
from api import WINDOW, captura_estado
from aprendido import aprendido_publico
from biblioteca import (IDIOMA_RE, NOTE_KEYS, PARTICIPANTES_ORIGENES, clean_icon, dump_notes, parse_notes,
                        public_transcript, temas, with_theme_id)
from carpetas import backup_dir, por_reunion, project_folders_resolved, rec_folder_origin
from common import APP_NAME, ApiError, fold, log, log_exc, parse_srt, read_text, save_text
from diagnostico import DIAG_FREE, DIAG_MODELS, diag_line, masked, scrub
from listas import _norm_dir, find_vocab, perfil_de, root_names, theme_vocab
from paquete import (DONAR_URL, RELEASES_URL, REPO_URL, TENCA_URL, comparar_version, license_name, open_dir,
                     ultima_version)
from rutas import AUDIO_EXT, DATA_DIR, LINK, LINKS, ROOT, TEXT_EXT, VERSION, alias_of, rel_of, root_name, safe


class Reuniones:
    def acerca_abrir(self, b):
        """Ajustes > Acerca de: abre la carpeta de datos (DATA_DIR: ajustes, API key cifrada, registros) en el
        Explorador, o el repositorio, la pagina de PayPal («Invitar al autor a un cafe») o la de la tenca en el
        navegador."""
        que = b.get("que")
        if que == "datos":
            os.makedirs(DATA_DIR, exist_ok=True)
            open_dir(DATA_DIR)
        elif que == "repo":
            webbrowser.open(REPO_URL)
        elif que == "donar":
            webbrowser.open(DONAR_URL)
        elif que == "tenca":
            webbrowser.open(TENCA_URL)
        elif que == "release":  # la pagina del Release que encontro «Buscar versión nueva» (o la lista de Releases)
            webbrowser.open((getattr(self, "_version_nueva", None) or {}).get("url") or RELEASES_URL)
        elif que == "claves":  # el asistente: la página de claves del proveedor (solo las de engines.PROVIDERS)
            url = (engines.PROVIDERS.get(b.get("proveedor") or "groq") or {}).get("keys")
            if not url:
                raise ApiError("Ese proveedor no tiene página de claves: pídesela a quien te dio la API.")
            webbrowser.open(url)
        else:
            raise ApiError("No sé qué abrir")
        return {"ok": True}

    def version_buscar(self, b):
        """POST /api/version/buscar: Acerca de > «Buscar versión nueva». Una sola peticion a GitHub, solo al pulsar
        (punto 60 del review del 14/11/2026): si hay una version mas nueva que esta, con el enlace a su Release."""
        try:
            u = ultima_version()
        except ValueError as e:
            log(f"   versión nueva: no se pudo consultar ({e})")
            raise ApiError(str(e))
        estado = comparar_version(VERSION, u["tag"])
        self._version_nueva = u  # «Ver la versión» abre esta pagina (acerca_abrir, que="release")
        log(f"   versión nueva: consultada ({estado})")
        return {"estado": estado, "tuya": VERSION, "ultima": u["version"], "url": u["url"], "fecha": u["fecha"]}

    def diagnostico(self, q):
        """Texto para pedir ayuda (Acerca de > Copiar diagnostico), con lista blanca: version, Python y sistema, motor y
        modelo, como es la carpeta de la app (sin su ruta), contadores y las ultimas 30 lineas de server.log por
        diag_line (lo fijo de cada mensaje de la app, cifras y codigos de error; lo demas, «<texto>»). Se pega donde se
        pide ayuda, a veces en un sitio publico: ni nombres ni rutas ni texto libre, y las claves enmascaradas."""
        st = self.settings.get()
        cloud = st.get("cloud") or {}
        preset = next((p for p in engines.PRESETS if p["id"] == st.get("preset")), None)

        def tech(v):
            return v if v in DIAG_MODELS else DIAG_FREE
        if preset and preset["engine"] == "cloud":
            prov = cloud.get("provider") if cloud.get("provider") in engines.PROVIDERS else DIAG_FREE
            motor = f"nube ({prov}) · modelo {tech(cloud.get('model'))}"
        else:
            motor = f"local ({tech(st.get('preset'))}) · modelo {tech(preset['model']) if preset else DIAG_FREE}"
        si = lambda c: "sí" if c else "no"  # noqa: E731
        default = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "AudiosApp")
        propia = os.path.normcase(os.path.abspath(DATA_DIR)) == os.path.normcase(os.path.abspath(default))
        datos = ("en la de siempre (%LOCALAPPDATA%\\AudiosApp)" if os.environ.get("LOCALAPPDATA") else
                 "en la de siempre (~/AudiosApp)") if propia else "en otra carpeta"
        en_red, no_ascii = ROOT.startswith(("\\\\", "//")), any(ord(c) > 127 for c in ROOT)
        try:
            lines = read_text(os.path.join(DATA_DIR, "server.log")).splitlines()[-30:]
        except OSError:
            lines = ["(no hay server.log)"]
        keys = [cloud.get("api_key"), *engines.all_extra_keys(cloud)]  # las adicionales de todos los proveedores
        mode = procesado_de(st)
        ver = engines.VERSION_RX.match(str(st.get("agente_version") or ""))
        donde = {"local": "solo en este PC", "api": "API externa"}.get(mode) or (
            f"agente ({agente_de(st)[0].label}" + (f" {ver.group(0)}" if ver else "") + ")")
        corregidos = getattr(self.settings, "corregidos", None)  # las claves que Store puso por defecto al arrancar
        corregidos = [str(k) for k in corregidos] if isinstance(corregidos, list) else []
        info = [f"{APP_NAME} · versión {VERSION}", f"Python {sys.version.split()[0]} · {platform.platform()}",
                f"Dónde se procesa: {donde}",
                f"Transcripción: {motor} · directo: {tech(st.get('live_preset') or 'medium')}",
                f"Carpeta de {APP_NAME}: en OneDrive: {si('onedrive' in ROOT.lower())} · en una unidad de red: "
                f"{si(en_red)} · con tildes u otros caracteres no ASCII: {si(no_ascii)} · {len(ROOT)} caracteres",
                f"Datos: {datos}",
                f"Carpetas vinculadas: {len(LINKS)} · destinos de salida: {len(perfil_de(st)['outputs'])}",
                # puntos 5, 30, 33 y 32 del review del 14/11/2026: solo nombres de ajustes, estados y numeros
                "Ajustes corregidos al arrancar: " + (", ".join(corregidos) or "ninguno"),
                self.despierto.estado() if hasattr(self, "despierto") else "PC despierto: sin datos",
                *fallos.diagnostico(DATA_DIR), *informe.diagnostico(3),
                "", f"server.log (últimas 30 líneas; lo que no es técnico va como {DIAG_FREE}):",
                *map(diag_line, lines)]
        return {"texto": scrub("\n".join(info), keys)}

    def _aviso_resumen_local(self):
        """Al arrancar: la primera vez sobre unos ajustes de una version anterior. Antes, el resumen automatico subia a
        la nube tambien lo transcrito en local; si a quien actualiza le pasaba (resumen automatico activado, nube con
        API key y transcripcion en local: el modelo de la cola o del directo, o reuniones ya transcritas asi), la
        interfaz se lo avisa una vez (config -> summary.aviso_local). En una instalacion nueva no hay que avisar."""
        st = self.settings.get()
        if st.get("aviso_resumen_local") is not None:
            return
        c = st.get("cloud") or {}

        def local(p):
            return (engines.PRESETS_BY_ID.get(p) or {}).get("engine") == "local"
        afecta = (os.path.isfile(self.settings.path) and st.get("summary_auto", True)
                  and not st.get("summary_auto_local") and bool(engines.chat_url(c))
                  and bool((c.get("api_key") or "").strip())
                  and (local(st.get("preset")) or local(st.get("live_preset") or "medium")
                       or any(local(p) for p in st.get("speed") or {})))
        self.settings.update(lambda d: d.update(aviso_resumen_local=bool(afecta)))
        if afecta:
            log("   aviso pendiente: el resumen automático ya no sube a la nube lo transcrito en local")

    # --- GET
    def _agente_info(self, st):
        """Para Ajustes: el agente configurado, si esta instalado, su version y cuando se comprobo."""
        cls, a = agente_de(st)
        ruta = cls.find()
        return {"tipo": a["tipo"], "modelo": a["modelo"], "label": cls.label,
                "modelos": [list(m) for m in cls.MODELOS], "encontrado": bool(ruta), "ruta": ruta,
                "version": st.get("agente_version") or None, "comprobado": st.get("agente_comprobado") or None}

    def config(self, q):
        st = self.settings.get()
        mode = procesado_de(st)
        presets = []
        for p in engines.PRESETS:
            if p["engine"] == "cloud" and mode != "api":  # la nube no se ofrece si no se ha elegido «API externa»
                continue
            d = {k: v for k, v in p.items()}
            d["speed"] = st["speed"].get(p["id"], p["speed"])
            d["cached"] = p["engine"] == "cloud" or engines.model_cached(p["model"])
            m = engines.WHISPER_MODELS.get(p.get("model"))
            d["size_mb"] = round(sum(s for _, s, _ in m[2]) / 1e6) if m else None  # lo que descarga la primera vez
            presets.append(d)
        cloud = dict(st["cloud"])
        cloud["elegido"] = proveedor_elegido(st)  # sin elegir (instalacion nueva): la interfaz no preselecciona ninguno
        cloud["has_key"] = bool(cloud.get("api_key"))
        cloud["api_key"] = masked(cloud.get("api_key"))
        # claves adicionales: enmascaradas (nunca en claro) y por proveedor, cada una con el suyo; en extra_keys, las
        # del proveedor configurado (las unicas que se usan)
        by = {p: [masked(k) for k in (cloud.pop(f, None) or []) if k] for p, f in engines.EXTRA_KEYS_FIELDS.items()}
        cloud["extra_keys_by"], cloud["extra_keys"] = by, by.get(cloud.get("provider") or "groq", [])
        cloud["quota"] = (self.jobs.cloud.keys.public(st["cloud"], model=engines.CloudEngine.live_model(st["cloud"]))
                          if cloud["has_key"] and getattr(self.jobs, "cloud", None) is not None else None)
        return {"root": ROOT, "root_name": root_name(), "root_names": root_names(), "version": VERSION,
                "app_name": APP_NAME, "window": WINDOW is not None, "config_rev": self.settings.rev,
                "acerca": {"licencia": license_name(), "repo": REPO_URL, "datos": DATA_DIR, "donar": DONAR_URL,
                           "tenca": TENCA_URL},
                "presets": presets, "providers": engines.PROVIDERS, "cloud": cloud,
                "audio_ext": sorted(AUDIO_EXT),  # «Subir audio»: lo que se ofrece en el selector y se sube al soltar
                "procesado": mode, "agente": self._agente_info(st),
                "ultima_prueba": (st.get("ultima_prueba") if (st.get("ultima_prueba") or {}).get("modo") == mode
                                  else None),
                "preset": st["preset"], "language": st["language"], "threads": st["threads"],
                "prompt": st["prompt"], "cpu_count": os.cpu_count(),
                "live_auto": bool(st.get("live_auto")), "live_preset": st.get("live_preset") or "medium",
                "live_final_pass": bool(st.get("live_final_pass", True)),
                "hotkey": {"ok": self.hotkey.ok, "error": self.hotkey.error, "keys": "Ctrl+Alt+R"},
                "favorites": st.get("favorites") or [], "folder_icons": st.get("folder_icons") or {},
                "links": [{"rel": LINK + k, "name": l["name"], "path": l["path"]} for k, l in LINKS.items()],
                "vocab_links": self._vocab_links_public(),  # carpetas de listas conectadas (no son de grabaciones)
                # carpeta principal de cada proyecto, la asignada o la suya en el arbol (rec_folder_origin); las
                # asignadas tal cual, aparte
                "rec_folder": st.get("rec_folder") or "", "project_folders": project_folders_resolved(st),
                "project_folders_asignadas": dict(st.get("project_folders") or {}),
                "workstream_folders": dict(st.get("workstream_folders") or {}),
                "carpeta_por_reunion": por_reunion(st),
                "project_langs": st.get("project_langs") or {}, "workstream_langs": st.get("workstream_langs") or {},
                "rec_directo": st.get("rec_directo", True) is not False, "rec_mic": st.get("rec_mic") or "default",
                "rec_sys": st.get("rec_sys") or "all",
                "meeting_detect": bool(st.get("meeting_detect", True)),
                "ocultar_captura": captura_estado(st),
                "outlook_on": bool(st.get("outlook_on")), "rec_aviso": bool(st.get("rec_aviso", True)),
                "summary": {"auto": bool(st.get("summary_auto", True)), "local": bool(st.get("summary_auto_local")),
                            "aviso_local": bool(st.get("aviso_resumen_local")),  # una vez, tras actualizar
                            "model": st.get("summary_model"),
                            "lang": st.get("summary_lang") or "es", "extra": st.get("summary_extra") or "",
                            "models": engines.SUMMARY_MODELS, "ready": self.jobs.summary_ready()},
                "voices": {"enabled": bool(st.get("voices_enabled")), "models_ready": voices.models_ready(DATA_DIR)},
                "worker_keepalive_min": int(st.get("worker_keepalive_min", 30) or 0),
                "worker_preload": bool(st.get("worker_preload", True)),
                "lost_secrets": list(getattr(self.settings, "lost", [])),
                "settings_broken": getattr(self.settings, "broken", None),  # settings.json ilegible al arrancar
                "perfil": perfil_de(st)}

    def tree(self, q):
        public, version = self.library.snapshot(max_age=1.0)
        if q.get("v") == version:
            return {"version": version, "same": True}
        return {"version": version, "tree": public}

    def transcript(self, q):
        abs_path = safe(q.get("path"), "file")
        activo = getattr(getattr(self, "jobs", None), "active_for", None)
        job = activo(rel_of(abs_path)) if activo is not None else None
        if job is not None and job.live and getattr(job, "edit_lock", None) is not None:
            # en directo, el texto y su generación a la vez (el escritor del directo los cambia con este candado): el
            # editor manda la generación al guardar y lo que se haya rehecho después no se toca (_save_live_edits)
            with job.edit_lock:
                out = public_transcript(self.transcripts.load(os.path.splitext(abs_path)[0]))
                out["text_gen"] = getattr(job, "text_gen", 0)
        else:
            out = public_transcript(self.transcripts.load(os.path.splitext(abs_path)[0]))
        out["has_backup"] = os.path.isdir(backup_dir(rel_of(abs_path)))
        if out["has_backup"] and out["kind"] == "srt":  # lo que cambio Tenca con lo que aprendio (marcas y Deshacer)
            try:
                out["aprendido"] = aprendido_publico(rel_of(abs_path), out["segments"])
            except OSError:
                log_exc()
        return out

    # --- resumen con IA
    def summary_get(self, q):
        abs_path = safe(q.get("path"), "file")
        p = os.path.splitext(abs_path)[0] + ".resumen.md"
        if not os.path.isfile(p):
            return {"exists": False, "ready": self.jobs.summary_ready()}
        text, meta = read_text(p), {}
        lines = text.split("\n")
        if lines and lines[0].strip() == "---":
            end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), 0)
            for l in lines[1:end]:
                k, _, v = l.partition(":")
                meta[k.strip()] = v.strip()
            text = "\n".join(lines[end + 1:]).strip()
        return {"exists": True, "text": text, "meta": meta, "ready": self.jobs.summary_ready()}

    def summarize(self, b):
        with self._move_lock():
            return {"id": self.jobs.submit_summary(alias_of(b.get("path")))}

    # --- notas y propiedades (estilo Notion)
    def notes_get(self, q):
        abs_path = safe(q.get("path"), "file")
        md = os.path.splitext(abs_path)[0] + ".md"
        if not os.path.isfile(md):
            return {"props": {}, "body": "", "mtime": 0}
        props, body = parse_notes(read_text(md))
        return {"props": with_theme_id(props, rel_of(abs_path)), "body": body, "mtime": os.path.getmtime(md)}

    def notes_save(self, b):
        """Guarda las notas y/o propiedades (solo lo que venga en la peticion)."""
        tema = bool({"proyecto", "workstream", "vocabulario"} & set(b.get("props") or {})) and not b.get("_al_grabar")
        with self.notes_lock:
            # dentro del candado: si la reunion se acaba de mover (se mueve con el cogido), su ruta nueva
            abs_path = safe(alias_of(b.get("path")), "file")
            md = os.path.splitext(abs_path)[0] + ".md"
            props, body = parse_notes(read_text(md)) if os.path.isfile(md) else ({}, "")
            antes = self._tema_de(props, abs_path) if tema else None
            for k, v in (b.get("props") or {}).items():
                if k in NOTE_KEYS:
                    if k == "etiquetas":
                        v = [" ".join(str(t).split()) for t in (v or []) if str(t).strip()]
                    elif k == "idioma":  # codigo o "auto"; vacio = se quita la clave (vuelve al del perfil)
                        v = str(v or "").strip().lower()
                        v = v if IDIOMA_RE.match(v) else ""
                    elif k == "contexto":
                        v = " ".join(str(v or "").split())[:engines.ABOUT_CHARS]
                    elif k == "icono":
                        v = clean_icon(v)
                    elif k == "participantes_origen":  # solo «sugeridos por»; vacio = confirmados
                        v = str(v or "").strip().lower()
                        v = v if v in PARTICIPANTES_ORIGENES else ""
                    props[k] = v
            # escribir (o confirmar tal cual) los participantes los hace del usuario: ya pueden ir al resumen y a los
            # destinos. Solo siguen «sugeridos» si quien los guarda dice de donde vienen (rec_start)
            if "participantes" in (b.get("props") or {}) and "participantes_origen" not in (b.get("props") or {}):
                props.pop("participantes_origen", None)
            if not props.get("participantes"):
                props.pop("participantes_origen", None)
            theme_vocab(props, abs_path, b.get("props") or {})  # el tema: nombres a la cabecera, id a temas()
            if "body" in b:
                body = b.get("body") or ""
            text = dump_notes(props, body)
            if text:
                save_text(md, text, "utf-8")
            elif os.path.isfile(md):
                os.remove(md)  # sin notas ni propiedades: no se deja un archivo vacio
        directo = None
        if {"proyecto", "workstream", "idioma", "contexto", "vocabulario"} & set(b.get("props") or {}):
            directo = self.jobs.refresh_prompt(rel_of(abs_path))  # el idioma, ya; lo demas, en el siguiente trozo
        self.jobs.schedule_outputs(rel_of(abs_path))  # destinos de salida: el documento con las notas nuevas
        self.library.at = 0
        out = {"props": with_theme_id(props, rel_of(abs_path)),
               "mtime": os.path.getmtime(md) if os.path.isfile(md) else 0}
        if directo is not None and "idioma" in (b.get("props") or {}):
            out["directo"] = {"idioma": directo}  # el aviso dice cuándo se verá el idioma nuevo (perfilSavedText)
        if tema:
            try:
                out["pendiente"] = self._tema_movido(abs_path, antes, props)
            except Exception:  # las notas ya estan guardadas: si esto falla, se anota y no se devuelve error
                log_exc()
        return out

    @staticmethod
    def _tema_de(props, abs_path):
        """La lista del tema que dice la cabecera de una reunion (o None)."""
        if not props.get("workstream"):
            return None
        try:
            return find_vocab(props.get("proyecto"), props["workstream"], temas().get(rel_of(abs_path)), near=abs_path)
        except OSError:
            return None

    def _tema_movido(self, abs_path, antes, props):
        """Cambiar el proyecto (el tema) en pleno directo hace lo mismo que «Cambiar» la carpeta (mock 01b, nota 3): si
        la reunion estaba en la carpeta principal que le tocaba por su tema (no en una elegida a mano), queda apuntada
        para moverse a la del tema nuevo cuando acabe todo. Fuera del directo, el tema no mueve nada. Lo apuntado asi
        sigue al tema mientras no se haya movido; una carpeta elegida con «Cambiar» manda. Devuelve lo pendiente."""
        with self._move_lock():
            return self._tema_movido_locked(abs_path, antes, props)

    def _tema_movido_locked(self, abs_path, antes, props):
        rel = rel_of(abs_path)
        movs = self._movs()
        e = movs.get(rel)
        auto = bool(e and e.get("por") == "tema")
        if (e and not auto) or (not auto and not self._grabando(rel)):
            return None
        st = self.settings.get()
        cur = _norm_dir(self._donde_esta(abs_path))
        try:
            old_main = safe(rec_folder_origin(antes, st)[0], "dir")
            new_main = safe(rec_folder_origin(self._tema_de(props, abs_path), st)[0], "dir")
        except ApiError:
            return None
        if not auto and (_norm_dir(old_main) != cur or rel not in self.__dict__.get("_por_regla", ())):
            return None  # estaba en una carpeta elegida a mano (o no salio de la regla al grabar): no la cambia
        if _norm_dir(new_main) == cur:
            if auto:
                movs.pop(rel)
                log(f"📁 {rel}: ya no se mueve (deshecho)")
            return None
        e = {"to": rel_of(new_main), "at": time.time(), "por": "tema"}
        movs.set(rel, e)
        log(f"📁 {rel}: se moverá a {e['to']} al terminar")
        return self._pendiente_publico(rel, e)

    def tags(self, q):
        self.library.refresh(max_age=5)
        seen = {}
        for a in self.library.flat:
            for t in (a.get("props") or {}).get("etiquetas") or []:
                seen[t] = seen.get(t, 0) + 1
        return {"tags": sorted(seen, key=lambda t: (-seen[t], fold(t)))}

    def doc(self, q):
        abs_path = safe(q.get("path"), "file")
        ext = os.path.splitext(abs_path)[1].lower()
        if ext not in TEXT_EXT:
            raise ApiError("Tipo de archivo no soportado")
        text = read_text(abs_path)
        out = {"name": os.path.basename(abs_path), "ext": ext, "text": text}
        if ext == ".srt":
            out["segments"] = parse_srt(text)
        return out

    def search(self, q):
        query = fold(q.get("q", "")).strip()
        terms = [t for t in query.split() if t]
        if not terms or len(query) < 2:
            return {"results": []}
        self.library.refresh(max_age=5)
        results = []
        for a in self.library.flat:
            data = self.transcripts.load(os.path.splitext(a["_abs"])[0]) if (a["txt"] or a["srt"]) else None
            hits = [s for s in data["segments"] if all(t in s["_f"] for t in terms)] if data else []
            if a.get("_sum"):  # y en el resumen con IA
                try:
                    hits = [{"start": None, "text": "✨ " + l} for l, f in self.transcripts.doc_lines(a["_sum"])
                            if all(t in f for t in terms) and not l.startswith(("---", "modelo:", "generado:"))] + hits
                except OSError:
                    pass
            if a.get("_md"):  # tambien en las notas y propiedades (titulo, etiquetas, participantes)
                try:
                    hits = [{"start": None, "text": "📝 " + l} for l, f in self.transcripts.doc_lines(a["_md"])
                            if all(t in f for t in terms) and l.strip() != "---"] + hits
                except OSError:
                    pass
            in_name = all(t in fold(a["path"]) for t in terms)
            if hits or in_name:
                results.append({"kind": "audio", "path": a["path"], "name": a["name"], "folder": a["folder"],
                                "date": a["date"], "label": a["label"], "count": len(hits),
                                "hits": [{"start": s["start"], "text": s["text"]} for s in hits[:6]]})
        tree, _ = self.library.refresh(max_age=5)
        stack = [tree]
        while stack:
            node = stack.pop()
            stack.extend(node["folders"])
            for d in node["docs"]:
                if d["size"] > 3_000_000:
                    continue
                try:
                    lines = self.transcripts.doc_lines(safe(d["path"]))
                except OSError:
                    continue
                hits = [l for l, f in lines if all(t in f for t in terms)]
                if hits:
                    results.append({"kind": "doc", "path": d["path"], "name": d["name"], "folder": node["path"],
                                    "count": len(hits), "hits": [{"start": None, "text": l[:300]} for l in hits[:6]]})
        results.sort(key=lambda r: -r["count"])
        return {"results": results[:100], "total": sum(r["count"] for r in results)}

    def jobs_list(self, q):
        # hilos_caidos: un hilo vigia que se ha parado (fallos.py, punto 33): la interfaz avisa de reiniciar
        return {"jobs": self.jobs.snapshot(), "movimientos": self.movimientos_publicos(),
                "hilos_caidos": fallos.caidos()}
