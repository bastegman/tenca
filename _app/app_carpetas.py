# -*- coding: utf-8 -*-
"""App, la parte de las carpetas y archivos: favoritos e iconos, eliminar (a la papelera), importar y subir audio,
mover reuniones (tambien en pleno directo, con los movimientos pendientes), crear y renombrar carpetas, mostrar y
abrir.

Salio tal cual de server.py al partirlo (15/11/2026): App hereda de esta clase y server.py la reexporta."""
import contextlib
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time

import calendario
import conservar
import engines
from ajustes import moved_rel, settings_moved
from biblioteca import (COMPLETE, DATE_RE, IDIOMA_RE, SIDE_EXTS, Movimientos, carpetas_reunion, dump_notes, parse_notes,
                        temas)
from carpetas import (FORMATOS_SUBIR, SUBIENDO, backup_dir, display_rel, fecha_subida, fit_meeting_dir, free_dir,
                      limpiar_subidas, move_files, own_meeting_dir, por_reunion, theme_moved, to_recycle_bin)
from common import APP_NAME, WINDOWS, ApiError, clean_name, fold, log, log_exc, read_text, save_text
from listas import _norm_dir, find_vocab, has_vocab_dir, list_vocab, perfil_de
from recording import notify
from rutas import (ALIASES, ALIASES_LOCK, AUDIO_EXT, DATA_DIR, LINK, LINKS, MARCA_SIN_AUDIO, ROOT, TEXT_EXT, VLINK,
                   _inside, alias_of, in_app_dir, is_link_root, rel_of, safe, sin_audio, vocab_link_path)


class Carpetas:
    # --- favoritos e iconos de carpeta
    def favorite(self, b):
        item = {"kind": b.get("kind"), "path": b.get("path")}
        if item["kind"] not in ("a", "f", "d"):
            raise ApiError("Tipo no válido")

        def upd(d):
            favs = [f for f in d.get("favorites", []) if f != item]
            if b.get("on"):
                favs.append(item)
            d["favorites"] = favs
        self.settings.update(upd)
        return {"favorites": self.settings.get()["favorites"]}

    def folder_icon(self, b):
        safe(b.get("path"), "dir")
        icon = (b.get("icon") or "").strip()[:4]

        def upd(d):
            icons = dict(d.get("folder_icons", {}))
            if icon:
                icons[b["path"]] = icon
            else:
                icons.pop(b["path"], None)
            d["folder_icons"] = icons
        self.settings.update(upd)
        return {"ok": True}

    def _rename_refs(self, old, new, folder=False):
        """Favoritos e iconos siguen al audio/carpeta cuando se mueve o renombra."""
        def moved(p):
            if p == old:
                return new
            if folder and p.startswith(old + "/"):
                return new + p[len(old):]
            return p

        def upd(d):
            d["favorites"] = [dict(f, path=moved(f["path"])) for f in d.get("favorites", [])]
            d["folder_icons"] = {moved(k): v for k, v in d.get("folder_icons", {}).items()}
        self.settings.update(upd)

    # --- eliminar (a la papelera) e importar
    def delete(self, b):
        with self._move_lock():  # no a la vez que se mueve (dejaria archivos sueltos); despues, su ruta nueva
            return self._delete(dict(b, path=alias_of(b.get("path"))))

    def _delete(self, b):
        p = safe(b.get("path"), "reunion")  # tambien una reunion cuyo audio ya esta en la papelera (sus textos)
        rel = rel_of(p)
        a = self.library.find(rel)
        mine = self.rec and self.rec.alive() and os.path.normcase(self.rec.path) == os.path.normcase(p)
        if self.jobs.active_for(rel) or (a and a.get("recording")) or mine:
            raise ApiError("Se está grabando o transcribiendo; espera a que termine.")
        if self.jobs.active_for(rel, kinds=("summary",)):
            raise ApiError("Se está resumiendo esta reunión; espera a que termine (o cancela el resumen en la cola).")
        group = [p] if os.path.isfile(p) else []
        stem, ext = os.path.splitext(p)
        own = None
        if ext.lower() in AUDIO_EXT:
            group += [stem + e for e in SIDE_EXTS if os.path.isfile(stem + e)]
            own = own_meeting_dir(p)
        for attempt in range(8):  # el reproductor puede tener el audio abierto un instante
            try:
                to_recycle_bin(group)
                break
            except ApiError:
                if attempt == 7:
                    raise
                time.sleep(0.3)
        if own:  # su carpeta (carpeta por reunion), si se ha quedado vacia
            try:
                os.rmdir(own)
                carpetas_reunion().discard(rel_of(own))
            except OSError:
                pass
        COMPLETE.set(rel, False)
        conservar.huecos().set(rel, False)
        self._movs().pop(rel)  # un movimiento pendiente (cambiar la carpeta en pleno directo) ya no tiene a quien mover
        self.jobs._cloud_text().set(rel, False)  # otra reunion que acabe con este nombre no hereda su origen
        shutil.rmtree(backup_dir(rel), ignore_errors=True)
        self.jobs.voice_vectors.pop(rel, None)
        try:
            temas().set(rel, None)
        except OSError:
            log_exc()
        self.settings.update(lambda d: d.update(favorites=[f for f in d.get("favorites", []) if f["path"] != rel]))
        copias = 0
        if ext.lower() in AUDIO_EXT:  # sus documentos de los destinos de salida que nadie ha cambiado, tambien
            try:
                copias = self.jobs.forget_outputs(rel)
            except Exception:
                log_exc()
        self.library.at = 0
        log(f"🗑 {rel} → papelera ({len(group)} archivos" + (f" y {copias} documentos de los destinos" if copias else "")
            + ")")
        return {"ok": True, "count": len(group), "copias": copias}

    def import_file(self, handler, q):
        """Subir un audio (o .txt/.md) arrastrandolo a la ventana: se copia a la carpeta indicada."""
        folder = safe(q.get("folder", ""), "dir")
        if in_app_dir(folder):
            raise ApiError("No se puede guardar ahí")
        name = clean_name(os.path.basename(q.get("name") or ""))
        stem, ext = os.path.splitext(name)
        if ext.lower() not in AUDIO_EXT | TEXT_EXT:
            raise ApiError("Solo audios (wav, mp3, m4a…) o textos (.txt, .md, .srt)")
        target, n = os.path.join(folder, name), 2
        while os.path.exists(target):
            target, n = os.path.join(folder, f"{stem} ({n}){ext}"), n + 1
        remaining = int(handler.headers.get("Content-Length") or 0)
        tmp = target + ".subiendo"
        with open(tmp, "wb") as f:
            while remaining > 0:
                chunk = handler.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining > 0:
            os.remove(tmp)
            raise ApiError("La subida se cortó.")
        os.replace(tmp, target)
        self.library.at = 0
        log(f"⤓ importado: {rel_of(target)}")
        return {"path": rel_of(target)}

    def subir(self, handler, q):
        """POST /api/subir?folder&name&fecha&vocab&language&contexto&preset&archivo (09/11/2026: subir un audio, venga
        de donde venga). El cuerpo es el archivo tal cual (el del móvil, un audio de WhatsApp,
        otra grabadora); lo demás es lo que da POST /api/grabar/plan con subir. Entra como una grabación hecha con la
        tarjeta: «AAAA-MM-DD HH-MM-SS - Nombre» con la fecha del audio, en su carpeta de reunión dentro de la principal,
        con el proyecto, el idioma y el contexto en su .md, y a la cola de transcripción. Antes de guardarlo se
        comprueba con la misma cadena que la cola (engines.probar_audio) que se puede transcribir: si no, no se guarda
        nada y se dice por qué."""
        original = os.path.basename(str(q.get("archivo") or "").replace("\\", "/")) or "El archivo"
        ext = os.path.splitext(original)[1].lower()
        if ext not in AUDIO_EXT:
            raise ApiError(f"«{original}» no es un audio que {APP_NAME} sepa leer. Sirven {FORMATOS_SUBIR}.")
        folder = safe(q.get("folder", ""), "dir")
        if in_app_dir(folder):
            raise ApiError("No se puede guardar ahí")
        st = self.settings.get()
        cuando = fecha_subida(q.get("fecha"))
        lt = time.localtime(cuando)
        nombre = " ".join(str(q.get("name") or "").split())[:120]
        label = ""
        if nombre:
            try:
                label = clean_name(nombre)
            except ApiError:
                label = calendario.nombre_archivo(nombre)
        stem = time.strftime("%Y-%m-%d %H-%M-%S", lt) + (f" - {label}" if label else "")
        # se recibe en un archivo oculto de la carpeta principal (el árbol no enseña los que empiezan por punto) y con
        # su extensión, para que PyAV lo reconozca al comprobarlo
        remaining = int(handler.headers.get("Content-Length") or 0)
        if remaining < 0:
            raise ApiError("Cuerpo no válido")
        limpiar_subidas(folder)
        tmp = os.path.join(folder, f"{SUBIENDO}{secrets.token_hex(4)}{ext}")
        total, guardado, mdir_nueva = remaining, False, None
        try:  # lo recibido a medias (disco lleno, la ventana se cierra a mitad) no se queda en la carpeta
            with open(tmp, "wb") as f:
                while remaining > 0:
                    chunk = handler.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            if remaining <= 0:
                handler.body_read = True
            if remaining > 0:
                raise ApiError("La subida se cortó.")
            consejo = " Si en el móvil se oye bien, vuelve a pasarlo al PC o conviértelo a mp3."
            if not total:
                raise ApiError(f"«{original}» está vacío.{consejo}")
            try:
                dur = engines.probar_audio(tmp)
            except engines.EngineError as e:
                raise ApiError(f"«{original}» {e}.{consejo}")
            if dur and dur > engines.AUDIO_MAX_S:  # la cola no lo podría decodificar entero (fix-subir-memoria)
                raise ApiError(f"«{original}» {engines.DEMASIADO_LARGO}.")
            # su carpeta «AAAA-MM-DD Nombre», como al grabar (con la fecha del audio, no la de hoy)
            mname = fit_meeting_dir(folder, label, lt, stem) if por_reunion(st) else None
            mdir = free_dir(folder, mname) if mname else None
            dest = mdir or folder
            base, n = stem, 2
            # ni otro audio ni sus notas con ese nombre (dos audios con el mismo nombre compartirían el .txt y el .md)
            while any(os.path.exists(os.path.join(dest, base + e)) for e in AUDIO_EXT | set(SIDE_EXTS)):
                base, n = f"{stem} ({n})", n + 1
            target = os.path.join(dest, base + ext)
            if mdir:
                try:
                    if not os.path.isdir(mdir):
                        os.makedirs(mdir)
                        mdir_nueva = mdir
                except OSError as e:
                    raise ApiError(f"No se puede crear la carpeta de la reunión en {display_rel(rel_of(folder))} "
                                   f"({e.__class__.__name__}).")
                carpetas_reunion().add(rel_of(mdir))
            for intento in range(20):  # Defender u OneDrive pueden tener el archivo abierto un instante (WinError 32)
                try:
                    os.replace(tmp, target)
                    break
                except PermissionError:
                    if intento == 19:
                        raise
                    time.sleep(0.1)
            guardado = True
        except OSError as e:  # sin permiso de escritura, el disco lleno… (una ApiError no es OSError: pasa tal cual)
            raise ApiError(f"No se ha podido guardar en {display_rel(rel_of(folder))}: "
                           f"{e.strerror or e.__class__.__name__}.")
        finally:
            if not guardado:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                if mdir_nueva:  # la carpeta de la reunión creada para él, si se ha quedado vacía
                    try:
                        os.rmdir(mdir_nueva)
                        carpetas_reunion().discard(rel_of(mdir_nueva))
                    except OSError:
                        pass
        try:  # la fecha del audio también como la de modificación (la que enseña el Explorador)
            os.utime(target, (cuando, cuando))
        except OSError:
            pass
        rel, avisos, props = rel_of(target), [], {}
        v = None
        if q.get("vocab"):
            try:
                v = find_vocab(None, None, q["vocab"])
            except (ApiError, OSError):
                v = None
        if v:
            props.update(proyecto=v["project"], workstream=v["name"], vocabulario=v["id"])
        if "language" in q:
            lang = str(q.get("language") or "").strip().lower()
            props["idioma"] = lang if lang and IDIOMA_RE.match(lang) else "auto"
        ctx = q.get("contexto") if "contexto" in q else perfil_de(st)["context"]
        ctx = " ".join(str(ctx or "").split())[:engines.ABOUT_CHARS]
        if ctx:
            props["contexto"] = ctx
        if conservar.politica(q.get("conservar")) != conservar.POR_DEFECTO:  # qué pasa con su audio (punto 17)
            props[conservar.CLAVE] = conservar.politica(q.get("conservar"))
        if props:
            try:
                self.notes_save({"path": rel, "props": props, "_al_grabar": True})
            except Exception as e:
                log_exc()
                avisos.append(f"sin cabecera de notas ({e.msg if isinstance(e, ApiError) else e})")
        self.library.at = 0
        job = None
        preset = str(q.get("preset") or st.get("preset") or "rapido")
        try:  # el idioma de la reunión es el de su .md (None: el de cada reunión, como «Transcribir los pendientes»)
            job = (self.jobs.submit([rel], preset, None) or [None])[0]
        except ApiError as e:
            avisos.append(f"no se ha puesto a transcribir: {e.msg}")
        log(f"⤒ subido: {rel} ({original}, {total} bytes" + (f", {dur:.0f} s" if dur else "") + ")"
            + (f" [{v['project']} · {v['name']}]" if v else "") + (f" · aviso: {'; '.join(avisos)}" if avisos else ""))
        return {"path": rel, "job": job, "carpeta": rel_of(dest), "duracion": dur, "aviso": "; ".join(avisos) or None}

    def _move_lock(self):
        """Un solo movimiento de reuniones a la vez (el de la interfaz y los pendientes al terminar)."""
        lock = self.__dict__.get("move_lock")
        if lock is None:
            lock = self.__dict__.setdefault("move_lock", threading.RLock())
        return lock

    def _grabando(self, rel):
        """Esa reunion se esta grabando (con la grabadora de la app o con OBS)."""
        r = getattr(self, "rec", None)
        if r and r.alive() and os.path.normcase(rel_of(r.path)) == os.path.normcase(rel):
            return True
        find = getattr(getattr(self, "library", None), "find", None)
        a = find(rel) if find else None
        return bool(a and a.get("recording"))

    def _ocupada(self, rel):
        """Por que no se pueden mover ahora los archivos de esa reunion (una frase), o None: se esta grabando,
        transcribiendo (directo y repaso final incluidos), identificando sus voces o resumiendo, o tiene el repaso
        final pendiente de un cierre de la app. Con None, nada de la app va a escribir en sus archivos."""
        if self._grabando(rel):
            return "Se está grabando esta reunión"
        return self.jobs.busy_for(rel)

    def move(self, b):
        src = safe(b.get("path"), "reunion")
        dest_dir = safe(b.get("folder", ""), "dir")
        if in_app_dir(dest_dir):
            raise ApiError("No se puede mover ahí")
        stem, ext = os.path.splitext(os.path.basename(src))
        new_stem = clean_name(b["name"]) if b.get("name") else stem
        if ext.lower() in AUDIO_EXT:
            why = self._ocupada(rel_of(src))
            if why and new_stem != stem:
                raise ApiError(f"{why}: podrás renombrarla cuando termine.")
            if why:  # se mueve sola al terminar (como «Cambiar» en la cabecera del directo)
                return self._destino_pendiente(rel_of(src), rel_of(dest_dir))
        own = own_meeting_dir(src) if ext.lower() in AUDIO_EXT else None
        # a la carpeta que contiene la suya, sin renombrarla: sacarla de su carpeta (se ve como carpeta porque tiene
        # algo mas dentro; lo demas se queda alli)
        fuera = bool(own) and new_stem == stem and _norm_dir(dest_dir) == _norm_dir(os.path.dirname(own))
        with self._move_lock():
            new = self._move_now(src, dest_dir, new_stem, fuera=fuera)
        if ext.lower() in AUDIO_EXT:
            self._movs().pop(rel_of(src))  # movida a mano: lo que estuviera pendiente ya no vale
        return {"path": new}

    def _move_now(self, src, dest_dir, new_stem=None, target=None, fuera=False):
        """Mueve (o renombra) una reunion con todo lo suyo a `dest_dir` y devuelve la ruta interna nueva del audio.
        Si vive en su carpeta (la creo la app para ella: own_meeting_dir) se mueve la carpeta entera, con lo que tenga
        dentro: queda dentro de `dest_dir` con su nombre (el de la reunion renombrada, o « (2)» si ya hay otra asi) y,
        si `dest_dir` es su propia carpeta, solo se renombra. Si no (o con `fuera`: sacarla de su carpeta), el audio y
        sus archivos hermanos, como siempre. Nunca pisa nada y o todo o nada (move_files). `target`: la carpeta de la
        reunion ya elegida (un movimiento pendiente que se retoma tras un cierre)."""
        stem, ext = os.path.splitext(os.path.basename(src))
        new_stem = new_stem or stem
        audio = ext.lower() in AUDIO_EXT
        own = own_meeting_dir(src) if audio and not fuera else None
        if own and _norm_dir(dest_dir) == _norm_dir(own):
            dest_dir = os.path.dirname(own)  # su propia carpeta: se renombra en su sitio
        if own and _inside(dest_dir, own):
            raise ApiError("No se puede mover una reunión dentro de su propia carpeta.")

        def renamed(f):  # lo que va detras del nombre (.txt, .srt, .md, .resumen.md): asi .resumen.md no acaba en .md
            return new_stem + os.path.basename(f)[len(stem):]
        base = os.path.dirname(src)
        sibs = [os.path.join(base, stem + e) for e in SIDE_EXTS
                if audio and os.path.isfile(os.path.join(base, stem + e))]
        if own:
            if target:
                new_dir = target
            else:
                name = os.path.basename(own)
                m = DATE_RE.match(new_stem)
                if new_stem != stem and m:  # renombrada: su carpeta, con el nombre nuevo
                    try:
                        when = time.strptime(m.group(0)[:10] + " " + m.group(0)[11:], "%Y-%m-%d %H-%M-%S")
                        name = fit_meeting_dir(dest_dir, new_stem[m.end():].strip(" -_·"), when, new_stem) or name
                    except ValueError:  # una fecha que no existe (2026-13-45): la carpeta se queda como estaba
                        pass
                new_dir = free_dir(dest_dir, name, mine=own)
            mine = {os.path.normcase(f) for f in [src] + sibs}
            moves = [(os.path.join(own, n), os.path.join(new_dir, n)) for n in sorted(os.listdir(own))
                     if os.path.normcase(os.path.join(own, n)) not in mine]
        else:
            new_dir, moves = dest_dir, []
        # el audio el ultimo: si algo falla a medias, la reunion sigue en su ruta (move_files deshace lo movido). Sin
        # audio (ya en la papelera, conservar.py), su marca, que es la que la hace reunion
        nuevo = os.path.join(new_dir, renamed(src))
        if audio and sin_audio(src):
            if os.path.exists(nuevo):  # alli hay otro audio con ese nombre: la marca acabaria siendo suya
                raise ApiError("Ya existe un archivo con ese nombre: " + os.path.basename(nuevo))
            sibs.sort(key=lambda f: f.lower().endswith(MARCA_SIN_AUDIO))
            moves += [(f, os.path.join(new_dir, renamed(f))) for f in sibs]
        else:
            moves += [(f, os.path.join(new_dir, renamed(f))) for f in sibs] + [(src, nuevo)]
        seen = {}
        for f, t in moves:  # nada se pisa: ni lo que ya hay en el destino ni dos archivos que acabarian con un nombre
            k = os.path.normcase(t)
            if (k != os.path.normcase(f) and os.path.exists(t)) or seen.get(k, f) != f:
                raise ApiError("Ya existe un archivo con ese nombre: " + os.path.basename(t))
            seen[k] = f
        made = own is not None and not os.path.isdir(new_dir)
        if made:
            try:
                os.makedirs(new_dir)
            except OSError as e:
                raise ApiError(f"No se puede crear la carpeta {display_rel(rel_of(new_dir))} ({e.__class__.__name__}).")
        with self._write_locks():  # unas notas o una correccion que llegan ahora esperan y van a la ruta nueva
            try:
                move_files(moves)
            except ApiError:
                if made:
                    try:
                        os.rmdir(new_dir)
                    except OSError:
                        pass
                raise
            return self._after_move(src, moves, own, new_dir, fuera, dest_dir, nuevo)

    def _write_locks(self):
        """Los candados de quien escribe en los archivos de una reunion desde la interfaz (notas_save,
        save_transcript): mientras se mueve, esperan; al soltarlos, su ruta ya lleva a la nueva (ALIASES)."""
        stack = contextlib.ExitStack()
        if getattr(self, "notes_lock", None) is not None:
            stack.enter_context(self.notes_lock)
        stack.enter_context(self._tx_save_lock)
        return stack

    def _after_move(self, src, moves, own, new_dir, fuera, dest_dir, nuevo=None):
        old, new = rel_of(src), rel_of(nuevo or moves[-1][1])
        reg = carpetas_reunion()
        if own and _norm_dir(new_dir) != _norm_dir(own):  # la carpeta de la reunion se ha quedado vacia
            try:
                os.rmdir(own)
            except OSError:
                log(f"   no se pudo quitar la carpeta vacía {rel_of(own)}")
            old_dir, new_dir_rel = rel_of(own), rel_of(new_dir)
            reg.discard(old_dir)
            reg.add(new_dir_rel)
            self._rename_refs(old_dir, new_dir_rel, folder=True)  # favoritos e icono de la carpeta
            if getattr(self, "settings", None) is not None:
                self.settings.update(lambda d: settings_moved(d, old_dir, new_dir_rel, folder=True))
        elif fuera and os.path.dirname(src) != dest_dir:  # sacada de su carpeta: esa ya no es de ninguna reunion
            reg.discard(rel_of(os.path.dirname(src)))
            try:
                os.rmdir(os.path.dirname(src))
            except OSError:
                pass
        self._moved_refs(old, new)
        log(f"↪ {old}  →  {new}")
        return new

    def _moved_refs(self, old, new):
        """Lo que la app guarda por ruta interna sigue a la reunion movida o renombrada: «completa», el origen de su
        texto, sus huellas de voz en memoria, su tema, favoritos e iconos, sus documentos de los destinos de salida y la
        copia del original. Las peticiones que lleguen con la ruta vieja van a la nueva (ALIASES)."""
        if old == new:
            return
        self.library.at = 0
        COMPLETE.moved(old, new)
        conservar.huecos().moved(old, new)  # los huecos de su texto (punto 17) van con ella
        self.jobs._cloud_text().moved(old, new)  # el origen de su texto va con ella
        vv = getattr(self.jobs, "voice_vectors", None)
        if vv is not None and old in vv:
            vv[new] = vv.pop(old)
        try:  # su tema la sigue
            temas().moved(old, new)
        except OSError:
            log_exc()
        self._rename_refs(old, new)
        self.jobs.outputs_moved(old, new)  # sus documentos de los destinos siguen siendo suyos
        old_b, new_b = backup_dir(old), backup_dir(new)
        if os.path.isdir(old_b) and not os.path.exists(new_b):  # la copia del original viaja con el audio
            os.replace(old_b, new_b)
        with ALIASES_LOCK:
            ALIASES[old] = new
            for k in [k for k, v in ALIASES.items() if v == old]:  # A -> B -> C: A va directo a C
                ALIASES[k] = new
            ALIASES.pop(new, None)
        if getattr(self.jobs, "settings", None) is not None:  # el documento de los destinos dice de donde sale
            self.jobs.schedule_outputs(new)

    # --- cambiar la carpeta en pleno directo (B1c): se mueve al terminar todo
    MOV_ESPERAS = (3, 10, 30, 60, 120)  # segundos entre reintentos si un archivo esta en uso (OneDrive, antivirus)

    MOV_INTENTOS = 12  # ~20 min: despues se deja donde esta y se dice

    def _movs(self):
        """Los movimientos pendientes (Movimientos) del DATA_DIR vigente."""
        path = os.path.join(DATA_DIR, "movimientos_pendientes.json")
        reg = self.__dict__.get("_movs_reg")
        if reg is None or reg.path != path:
            reg = self.__dict__["_movs_reg"] = Movimientos(path)
        return reg

    def _donde_esta(self, abs_audio):
        """La carpeta principal donde esta ahora la reunion: la que contiene su carpeta (si la tiene) o la suya."""
        own = own_meeting_dir(abs_audio)
        return os.path.dirname(own) if own else os.path.dirname(abs_audio)

    def _destino_final(self, abs_audio, to_rel, target=None):
        """Donde acabara la reunion si se mueve a `to_rel`: su carpeta dentro (con « (2)» si ya hay otra) o la propia
        carpeta (una reunion sin carpeta suya)."""
        if target:
            return target
        own = own_meeting_dir(abs_audio)
        try:
            dest = safe(to_rel, "dir")
        except ApiError:
            return to_rel
        return rel_of(free_dir(dest, os.path.basename(own), mine=own)) if own else rel_of(dest)

    def _pendiente_publico(self, rel, e):
        try:
            abs_audio = safe(rel, "file")
        except ApiError:
            abs_audio = None
        dest = self._destino_final(abs_audio, e["to"], e.get("target")) if abs_audio else e["to"]
        return {"folder": e["to"], "destino": dest, "display": display_rel(dest), "error": e.get("error"),
                "intentos": e.get("intentos", 0), "por": e.get("por") or "carpeta"}

    def destino(self, b):
        """POST /api/reunion/destino {path, folder | folder_abs | null}: la carpeta principal a la que mover esta
        reunion. Si se esta grabando o procesando, se apunta y se mueve sola en cuanto no quede nada en marcha (directo,
        repaso, voces y resumen); si no, ahora. folder null (o la carpeta donde ya esta) = Deshacer. folder_abs: una
        carpeta del PC (el selector de Windows); si esta fuera de la app, se vincula como «Conectar carpeta»."""
        folder = b.get("folder")
        if b.get("folder_abs"):
            p = os.path.normpath(os.path.expandvars(str(b["folder_abs"]).strip().strip('"')))
            inside = _inside(p, ROOT) or any(_inside(p, l["path"]) for l in LINKS.values())
            folder = rel_of(p) if inside and os.path.isdir(p) else self.link_add({"path": p})["rel"]
        with self._move_lock():  # con un movimiento en marcha se espera: despues, la ruta nueva (y ya no hay nada)
            src = safe(alias_of(b.get("path")), "reunion")  # también sin audio (punto 17): se mueve con sus textos
            if os.path.splitext(src)[1].lower() not in AUDIO_EXT:
                raise ApiError("Solo se puede cambiar la carpeta de una reunión.")
            rel = rel_of(src)
            if folder is None:
                if self._movs().pop(rel) is not None:
                    log(f"📁 {rel}: ya no se mueve (deshecho)")
                return {"path": rel, "pendiente": None}
            return self._destino_pendiente(rel, str(folder).replace("\\", "/").strip("/"))

    def _destino_pendiente(self, rel, folder_rel):
        with self._move_lock():
            return self._destino_pendiente_locked(rel, folder_rel)

    def _destino_pendiente_locked(self, rel, folder_rel):
        src = safe(alias_of(rel), "reunion")
        rel = rel_of(src)
        dest = safe(folder_rel, "dir")
        if in_app_dir(dest):
            raise ApiError("No se puede mover ahí")
        own = own_meeting_dir(src)
        if own and _norm_dir(dest) == _norm_dir(own):  # su propia carpeta (se ve como carpeta si tiene algo mas)
            dest = os.path.dirname(own)
        if _norm_dir(dest) == _norm_dir(self._donde_esta(src)):  # la carpeta donde ya esta: nada que mover
            if self._movs().pop(rel) is not None:
                log(f"📁 {rel}: ya no se mueve (deshecho)")
            return {"path": rel, "pendiente": None}
        if own and _inside(dest, own):
            raise ApiError("No se puede mover una reunión dentro de su propia carpeta.")
        if not self._ocupada(rel):  # ya no se escribe en ella: se mueve ahora
            self._movs().pop(rel)
            new = self._move_now(src, dest)
            self._nota_movida(rel, new)
            return {"path": new, "pendiente": None, "movida": True}
        e = {"to": rel_of(dest), "at": time.time()}
        self._movs().set(rel, e)
        log(f"📁 {rel}: se moverá a {e['to']} al terminar")
        return {"path": rel, "pendiente": self._pendiente_publico(rel, e)}

    def _al_quedar_libre(self, rel):
        """Jobs.on_idle: la reunion ha terminado su ultimo trabajo. Si tiene un movimiento pendiente, se hace en otro
        hilo (no se para la cola mientras se mueve un audio grande). Y el vigilante del audio (punto 17) mira si ya
        toca mandar el suyo a la papelera («Borrar al terminar»: justo despues del resumen)."""
        if hasattr(self, "audio_despertar"):
            self.audio_despertar()
        if self._movs().get(rel):
            threading.Thread(target=self._aplicar_movimientos, args=(rel,), daemon=True, name="mover").start()

    def _tick_movimientos(self):
        """Jobs.on_tick (cada 3 s): si hay algo apuntado que ya toca, se mueve en otro hilo; el vigilante de OBS no
        espera a un movimiento lento (una copia entre unidades)."""
        now = time.time()
        if not any(e.get("next", 0) <= now for _, e in self._movs().items()):
            return
        t = self.__dict__.get("_mover_hilo")
        if t is None or not t.is_alive():
            t = threading.Thread(target=self._aplicar_movimientos, daemon=True, name="mover")
            self.__dict__["_mover_hilo"] = t
            t.start()

    def _aplicar_movimientos(self, only=None):
        """Mueve las reuniones con un movimiento pendiente que ya han quedado libres (o solo `only`). Lo llaman el final
        de cada trabajo (on_idle), la parada de la grabacion y la vuelta de 3 s (on_tick), que tambien hace los que se
        quedaron de antes de un cierre de la app y reintenta los que fallaron (con espera: MOV_ESPERAS)."""
        movs = self._movs()
        pend = [(r, e) for r, e in movs.items() if only is None or r == only]
        if not pend:
            return 0
        lock = self._move_lock()
        if not lock.acquire(blocking=False):
            return 0  # otro hilo esta moviendo: la vuelta siguiente (3 s) lo vuelve a mirar
        n = 0
        try:
            for rel, _ in pend:
                e = movs.get(rel)  # con el candado: un «Deshacer» o un cambio de justo antes manda
                if not e or e.get("next", 0) > time.time() or self._ocupada(rel):
                    continue
                n += self._mover_pendiente(rel, e)
        finally:
            lock.release()
        return n

    def _mover_pendiente(self, rel, e):
        movs = self._movs()
        try:
            src = safe(rel, "file")
        except ApiError:
            src = None
        target = e.get("target")
        if src is None and e.get("nuevo"):  # la app se cerro a medio mover: el audio (el ultimo) ya esta alli
            try:
                moved = safe(e["nuevo"], "file")
            except ApiError:
                moved = None
            if moved:
                movs.pop(rel)
                if target:  # su carpeta: la vieja, si se ha quedado vacia, fuera; el registro y sus referencias
                    old_dir = rel.rpartition("/")[0]
                    try:
                        os.rmdir(safe(old_dir, "dir"))
                    except (ApiError, OSError):
                        pass
                    carpetas_reunion().discard(old_dir)
                    carpetas_reunion().add(target)
                    self._rename_refs(old_dir, target, folder=True)
                    self.settings.update(lambda d: settings_moved(d, old_dir, target, folder=True))
                self._moved_refs(rel, e["nuevo"])
                self._nota_movida(rel, e["nuevo"])
                return 1
        if src is None:
            movs.pop(rel)
            log(f"   movimiento pendiente de {rel}: la reunión ya no está ahí, se olvida")
            return 0
        try:
            dest = safe(e["to"], "dir")
            if in_app_dir(dest):
                raise ApiError("No se puede mover ahí")
        except ApiError:
            movs.pop(rel)
            msg = f"La carpeta {display_rel(e['to'])} ya no existe: la reunión se queda donde estaba."
            log(f"   movimiento pendiente de {rel}: la carpeta {e['to']} ya no existe, se queda donde está")
            self._nota_movida(rel, None, msg)
            notify("No se ha movido la reunión", msg)
            return 0
        own = own_meeting_dir(src)
        # a donde va (la carpeta de la reunion y su audio), apuntado mientras se mueve: si la app se cierra a medias,
        # el siguiente intento termina ahi. Un intento que falla lo deshace todo (move_files) y lo olvida
        if own:
            target = target or rel_of(free_dir(dest, os.path.basename(own), mine=own))
            e["target"] = target if _norm_dir(safe(target)) != _norm_dir(own) else None
        e["nuevo"] = rel_of(os.path.join(safe(target) if own else dest, os.path.basename(src)))
        movs.set(rel, e)
        try:
            new = self._move_now(src, dest, target=safe(target) if own else None)
        except ApiError as err:
            if not getattr(err, "incompleto", False):
                e.pop("target", None)
            e.pop("nuevo", None)
            n = int(e.get("intentos") or 0) + 1
            if n > self.MOV_INTENTOS:  # un error que no cambia (otra unidad, ruta larga, sin permiso): se deja
                movs.pop(rel)
                msg = f"No se ha podido mover la reunión a {display_rel(e['to'])}: {err.msg} Se queda donde está."
                log(f"   movimiento pendiente de {rel}: {err.msg} Se deja tras {n - 1} intentos")
                self._nota_movida(rel, None, msg)
                notify("No se ha movido la reunión", msg)
                return 0
            wait = self.MOV_ESPERAS[min(n, len(self.MOV_ESPERAS)) - 1]
            e.update(intentos=n, error=err.msg, next=time.time() + wait)
            movs.set(rel, e)
            if n == 1 or n % 10 == 0:
                log(f"   movimiento pendiente de {rel}: {err.msg} Se reintenta en {wait} s")
            if n == 5:
                notify("No se ha podido mover la reunión", f"{err.msg} Se sigue intentando.")
            return 0
        movs.pop(rel)
        self._nota_movida(rel, new)
        notify("Reunión movida", "→ " + display_rel(rel_of(os.path.dirname(safe(new)))))
        return 1

    def _nota_movida(self, old, new, error=None):
        """Lo que la interfaz avisa al terminar (y con lo que la pagina de la reunion pasa a su ruta nueva)."""
        notas = self.__dict__.setdefault("_movidas", [])
        carpeta = rel_of(os.path.dirname(safe(new))) if new else None
        notas.append({"de": old, "a": new, "carpeta": carpeta, "display": display_rel(carpeta) if new else None,
                      "error": error, "at": time.time()})
        del notas[:-20]

    def movimientos_publicos(self):
        """Para /api/jobs: los pendientes (ruta -> a donde, y el error del ultimo intento) y los hechos hace <10 min."""
        now = time.time()
        return {"pendientes": {r: self._pendiente_publico(r, e) for r, e in self._movs().items()},
                "hechos": [n for n in self.__dict__.get("_movidas", []) if now - n["at"] < 600]}

    def mkdir(self, b):
        parent = safe(b.get("parent", ""), "dir")
        p = os.path.join(parent, clean_name(b.get("name")))
        if os.path.exists(p):
            raise ApiError("Ya existe esa carpeta")
        os.makedirs(p)
        self.library.at = 0
        return {"path": rel_of(p)}

    def rename_folder(self, b):
        with self._move_lock():  # no a la vez que se mueve una reunion (la carpeta podria ser su destino)
            return self._rename_folder(b)

    def _rename_folder(self, b):
        if is_link_root(b.get("path")):  # carpeta vinculada: solo cambia el nombre que se ve en la app
            lid = b["path"].strip("/")[len(LINK):]
            if lid not in LINKS:
                raise ApiError("Esa carpeta ya no está vinculada.")
            old_name, new_name = LINKS[lid]["name"], clean_name(b.get("name"))
            LINKS[lid]["name"] = new_name
            self._save_links()
            if has_vocab_dir(LINKS[lid]["path"]):  # sus listas toman el nombre de la carpeta vinculada
                self.settings.update(lambda d: settings_moved(d, "", "", project=(old_name, new_name)))
                try:  # y el proyecto de sus reuniones (el nombre ya ha cambiado: si esto falla se anota)
                    self._themes_moved(LINK + lid, (old_name, new_name))
                except Exception:
                    log_exc()
            return {"path": b["path"].strip("/")}
        src = safe(b.get("path"), "dir")
        if os.path.normcase(src) == os.path.normcase(ROOT) or in_app_dir(src):
            raise ApiError("Esa carpeta no se puede renombrar")
        rel = rel_of(src)
        if any(j["path"].startswith(rel + "/") and j["status"] in ("queued", "running") for j in self.jobs.snapshot()):
            raise ApiError("Hay audios de esta carpeta transcribiéndose; espera a que terminen.")
        if self.rec and self.rec.alive() and rel_of(self.rec.path).startswith(rel + "/"):
            raise ApiError("Se está grabando dentro de esta carpeta; para la grabación antes de renombrarla.")
        dst = os.path.join(os.path.dirname(src), clean_name(b.get("name")))
        if os.path.normcase(dst) != os.path.normcase(src) and os.path.exists(dst):
            raise ApiError("Ya existe una carpeta con ese nombre")
        os.rename(src, dst)
        self.jobs._cloud_text().moved(rel_of(src), rel_of(dst), folder=True)  # el origen del texto de sus reuniones
        self._rename_refs(rel_of(src), rel_of(dst), folder=True)
        try:  # la carpeta ya esta renombrada: si algo de esto falla se anota, pero no se devuelve error
            self._folder_moved(rel_of(src), rel_of(dst), (os.path.basename(src), os.path.basename(dst))
                               if has_vocab_dir(dst) else None)
        except Exception:
            log_exc()
        self.library.at = 0
        return {"path": rel_of(dst)}

    def _folder_moved(self, old, new, project=None):
        """Una carpeta renombrada: lo que la app guarda por ruta interna la sigue, como al mover una reunion (move):
        la copia del original de cada transcripcion corregida, el estado «completa», las huellas de voz en memoria, el
        tema de cada reunion (temas()), los documentos de los destinos de salida y los ajustes (carpeta y tema del
        perfil, ultima carpeta y ultimo tema, carpeta de cada proyecto; con `project`, el proyecto tambien cambia de
        nombre, tambien en la cabecera de sus reuniones: _themes_moved)."""
        dst = safe(new, "dir")
        for d, _, files in os.walk(dst):
            for n in files:
                if os.path.splitext(n)[1].lower() not in AUDIO_EXT:
                    continue
                r_new = rel_of(os.path.join(d, n))
                r_old = moved_rel(r_new, new, old, folder=True)
                if r_old is None:
                    continue
                old_b, new_b = backup_dir(r_old), backup_dir(r_new)
                if os.path.isdir(old_b) and not os.path.exists(new_b):
                    os.replace(old_b, new_b)
        conservar.huecos().moved(old, new, folder=True)
        for r in [r for r in list(COMPLETE.data) if moved_rel(r, old, new, folder=True)]:
            COMPLETE.moved(r, moved_rel(r, old, new, folder=True))
        vv = self.jobs.voice_vectors
        for r in [r for r in list(vv) if moved_rel(r, old, new, folder=True)]:
            vv[moved_rel(r, old, new, folder=True)] = vv.pop(r)
        try:  # el tema de sus reuniones y el id de sus listas
            temas().moved(old, new, folder=True)
        except OSError:
            log_exc()
        self.jobs.outputs_moved(old, new, folder=True)
        self.settings.update(lambda d: settings_moved(d, old, new, folder=True, project=project))
        self._movs().moved(old, new, folder=True)  # los movimientos pendientes: la reunion y la carpeta de destino
        carpetas_reunion().moved(old, new, folder=True)  # y las carpetas de reunion que hay dentro
        self._themes_moved(new, project)

    def _themes_moved(self, new, project=None):
        """Al renombrar la carpeta de un proyecto (o la vinculada, que da su nombre a las listas), las reuniones de ese
        proyecto lo siguen en la cabecera de su .md, esten donde esten (theme_moved): si no, al volver a transcribirlas
        se quedaban con el vocabulario general, porque nunca se toma la lista de otro proyecto. El id de las que lo
        tenian ya lo ha seguido temas(); las que no, se quedan con el de la lista de su tema en la carpeta renombrada.
        Si el nombre nuevo ya es el de otro proyecto (una vinculada que se llama igual), las reuniones de ese otro sin
        id se quedan con el de la suya. Si no, con dos proyectos que se llaman igual, los nombres de la cabecera ya no
        dicen cual es: las de fuera de las dos carpetas pasaban al vocabulario general y las del otro que estan dentro
        de la renombrada tomaban su lista. `new`: la ruta interna de la carpeta, ya renombrada. Devuelve cuantas
        cabeceras han cambiado."""
        if not project:
            return 0  # ningun proyecto cambia de nombre (el de una lista es el de la carpeta que tiene Vocabulario)
        lists = {v["id"]: v for v in list_vocab()}
        ambiguous = any(fold(v["project"]) == fold(project[0]) for v in lists.values())
        own, others = {}, {}  # tema -> ids: las listas del proyecto renombrado y las de otros que ya se llamaban asi

        def renamed(vid):
            return moved_rel(vid, new, new, folder=True) is not None

        for v in lists.values():
            if v["folder"] == new:
                own.setdefault(fold(v["name"]), []).append(v["id"])
            elif fold(v["project"]) == fold(project[1]) != fold(project[0]) and not renamed(v["id"]):
                others.setdefault(fold(v["name"]), []).append(v["id"])
        store = temas()
        self.library.at = 0
        self.library.refresh()
        n = 0
        for a in list(self.library.flat):
            if not a.get("_md") or not (a.get("props") or {}).get("workstream"):
                continue
            with self.notes_lock:
                try:
                    props, body = parse_notes(read_text(a["_md"]))
                    vid = store.get(a["path"])
                    ch = theme_moved(props, vid, new, lists, project, ambiguous, renamed(a["path"]))
                    ids = []
                    if ch:
                        save_text(a["_md"], dump_notes(dict(props, **ch), body), "utf-8")
                        n += 1
                        ids = own.get(fold(props["workstream"])) or []
                    elif others and fold(props.get("proyecto") or "") == fold(project[1]) and (
                            vid not in lists or renamed(vid)):  # su id, si lo tiene, era de una lista que se llamaba
                        ids = others.get(fold(props["workstream"])) or []  # distinto de su cabecera
                    if len(ids) == 1:
                        store.set(a["path"], ids[0])
                except OSError:
                    log_exc()
        if n:
            self.library.at = 0
            log(f"   tema de {n} reuniones: siguen a la carpeta renombrada {new}")
        return n

    def reveal(self, b):
        rel = str(b.get("path") or "")
        p = vocab_link_path(rel) if rel.replace("\\", "/").strip("/").startswith(VLINK) else safe(rel)
        if sin_audio(p):  # su audio ya esta en la papelera (conservar.py): se senala lo que queda, su transcripcion
            p = next((os.path.splitext(p)[0] + e for e in (".txt", ".srt", ".md", MARCA_SIN_AUDIO)
                      if os.path.isfile(os.path.splitext(p)[0] + e)), p)
        if WINDOWS:
            if os.path.isdir(p):
                subprocess.Popen(["explorer", p])
            else:
                subprocess.Popen(f'explorer /select,"{p}"')
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", p] if os.path.isfile(p) else ["open", p])
        else:
            subprocess.Popen(["xdg-open", p if os.path.isdir(p) else os.path.dirname(p)])
        return {"ok": True}

    def open_file(self, b):
        p = safe(b.get("path"))
        if b.get("ext"):
            p = os.path.splitext(p)[0] + b["ext"]
        ext = os.path.splitext(p)[1].lower()
        if not os.path.isfile(p) or ext not in TEXT_EXT | AUDIO_EXT:
            raise ApiError("No se puede abrir ese archivo")
        if WINDOWS:
            os.startfile(p)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])
        return {"ok": True}
