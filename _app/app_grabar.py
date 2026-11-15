# -*- coding: utf-8 -*-
"""App, la parte de grabar: el plan de grabacion (plan.py: carpeta, lista, idioma y lo que se graba), la grabadora
(recording.py: empezar, parar, cortes y continuaciones), el apagado y la ventana.

Salio tal cual de server.py al partirlo (15/11/2026): App hereda de esta clase y server.py la reexporta."""
import json
import os
import subprocess
import sys
import threading
import time

import calendario
import despierto as despierto_mod
import engines
import informe
import plan as grabar_plan
import voices
from ajustes import agente_de, procesado_de
from apagado import schedule_shutdown
from api import WINDOW
from biblioteca import IDIOMA_RE, PARTICIPANTES_ORIGENES, TEXTOS_SUGERIDOS, carpetas_reunion
from carpetas import (display_rel, fecha_subida, fit_meeting_dir, free_dir, por_reunion, project_home,
                      rec_folder_origin, ruta_txt)
# el plan la busca con globals().get (la regla de la rama de carpetas), como rec_folder_origin: tiene que estar aqui
from carpetas import meeting_dir_preview  # noqa: F401
from common import APP_NAME, ApiError, clean_name, log, log_exc
from instancia import open_ui
from listas import (LIVE_PRESETS, SIN_AUDIO, _con_o_sin, _norm_dir, _project_dir, ajustes_previstos, check_perfil,
                    find_vocab, list_vocab, perfil_de, perfil_folder, read_vocab, update_perfil)
import pistas
from paquete import desktop_dir
from recording import Cortes, RecorderProcess, cue, disco_al_empezar, notify, teams_mic_estado
from rutas import APP_DIR, BUILD, DATA_DIR, LINKS, alias_of, in_app_dir, rel_of, root_name, safe


# Lo que rec_start espera a que la grabadora cree el WAV con su cabecera antes de escribir la del .md y arrancar el
# directo. Eran 3 s: con el PC cargado la grabadora tardo mas (medido en la revision de la integracion del 08/11/2026)
# y la reunion se quedaba sin cabecera ni directo («No existe el archivo»). Se sale en cuanto existe.
REC_ARRANQUE_S = 10.0


def teams_estado():
    """(micro de Teams, títulos de las ventanas de reunión de Teams) para directo_avisos.FinDeLlamada: ("uso" |
    "libre" | "duda", función que devuelve un set de títulos o None si no se pueden leer). Las ventanas solo se leen
    cuando hace falta. CHULETA_PRUEBAS_TEAMS (solo los E2E): un JSON {"mic": "libre", "ventanas": ["Comité"]} en vez
    del registro y las ventanas; si no se puede leer, "duda" (nunca se para nada)."""
    fake = os.environ.get("CHULETA_PRUEBAS_TEAMS")
    if fake:
        try:
            with open(fake, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return "duda", lambda: None
        v = d.get("ventanas")
        return str(d.get("mic") or "duda"), lambda: None if v is None else set(v)
    return teams_mic_estado(), calendario.teams_reuniones


class Grabar:
    # --- plan de grabacion (plan.py): un solo calculo para la tarjeta, Ctrl+Alt+R, el aviso flotante e Inicio
    def _motores(self, st):
        mode = procesado_de(st)
        cls = agente_de(st)[0]
        return {"presets": {p["id"]: p for p in engines.PRESETS if not (p["engine"] == "cloud" and mode != "api")},
                "procesado": mode, "agente": "Claude" if cls.tipo == "claude" else cls.label,
                "proveedor": (engines.PROVIDERS.get((st.get("cloud") or {}).get("provider") or "groq") or {}).get(
                    "label") or "la nube",
                "resumen_auto": st.get("summary_auto", True) is not False,
                "resumen_local": bool(st.get("summary_auto_local")),
                "sin_clave": not ((st.get("cloud") or {}).get("api_key") or "").strip()}

    # orígenes de la regla de carpetas (rama `carpetas`, rec_folder_origin) -> (origen, texto) de la tarjeta
    _ORIGENES_REGLA = {"workstream": "workstream", "proyecto": "proyecto", "carpeta_proyecto": "proyecto",
                       "perfil": "ajustes", "ultimo": "ultima", "raiz": "ajustes"}

    def _carpeta_origen(self, st, full):
        """(origen, texto) de la carpeta que ha decidido rec_folder_for. Con la regla de la rama de carpetas
        (rec_folder_origin(v, st) -> (carpeta, origen), de módulo), manda la suya si da esa misma carpeta; si no (o
        sin ella), se reconoce comparando con lo que hay en los ajustes."""
        regla = globals().get("rec_folder_origin")

        def texto(origen, v, rel):
            if origen == "workstream":
                return "carpeta del workstream"
            if origen == "proyecto":
                return f"carpeta de {v['project']}"
            if origen == "ultima":
                return "la última usada"
            return f"la carpeta de {APP_NAME}" if not rel else "Ajustes › Al grabar"

        def origen(v, rel):
            rel = rel or ""
            if callable(regla):
                try:
                    r_rel, o = regla(full.get(v["id"]) if v else None, st, full.values())
                except Exception:
                    log_exc()
                else:
                    o = self._ORIGENES_REGLA.get(o, "ajustes")
                    if (r_rel or "") == rel and (v or o not in ("workstream", "proyecto")):
                        return o, texto(o, v, rel)
            if v:
                wf = (st.get("workstream_folders") or {}).get(v["id"])
                if wf is not None and wf == rel:
                    return "workstream", texto("workstream", v, rel)
                pf = (st.get("project_folders") or {}).get(v["project"])
                if (pf is not None and pf == rel) or (v["id"] in full and rel == rel_of(_project_dir(full[v["id"]]))):
                    return "proyecto", texto("proyecto", v, rel)
            fija = perfil_folder(st)
            if fija is not None and fija == rel:
                return "ajustes", texto("ajustes", v, rel)
            if rel and rel == (st.get("rec_folder") or ""):
                return "ultima", texto("ultima", v, rel)
            return "ajustes", texto("ajustes", v, rel)
        return origen

    def _carpeta_plan(self, v, st, full):
        """La carpeta base de una grabación nueva: la que decide rec_folder_for. Con proyecto, si esa no es suya (la
        fija de Ajustes, que es la de las reuniones sin proyecto, o la última usada, que puede ser la de otro cliente),
        la del proyecto: la del workstream, la asignada al proyecto o, sin asignar, la carpeta del proyecto en el árbol
        (la que contiene su Vocabulario; 11a′ del rediseño, msg 1158). Con la regla de la rama de carpetas
        (rec_folder_origin), su rec_folder_for ya lo decide así y esto no hace nada: una sola regla."""
        fv = full.get(v["id"]) if v else None
        # con las listas ya leídas (full): si no, cada lista de una carpeta conectada las volvía a leer todas
        # (project_home), y Ajustes › Proyectos las leía una vez por lista (revisión de la fusión de ux-grabar)
        rel = self.rec_folder_for(fv, st, full.values())
        if not fv or callable(globals().get("rec_folder_origin")):
            return rel
        # el mismo orden que la regla de la rama de carpetas: la del workstream, la asignada al proyecto, la del
        # proyecto en el árbol; sin ninguna, la de rec_folder_for (la fija de Ajustes o la última usada)
        for cand in ((st.get("workstream_folders") or {}).get(v["id"]),
                     (st.get("project_folders") or {}).get(v["project"]), rel_of(_project_dir(fv))):
            if cand is None:
                continue
            try:
                if not in_app_dir(safe(cand, "dir")):
                    return cand
            except ApiError:
                continue
        return rel  # sin carpeta propia en la app (p. ej. listas de una carpeta conectada): lo de siempre, a la vista

    def plan_grabar(self, pedido, st=None, sug=None, plantilla=False, cuando=None):
        """El plan de una grabación nueva con el origen de cada valor (ver plan.py). `pedido`: lo elegido en la
        tarjeta; `sug`: la reunión en curso (si no se da, la última lectura del vigilante o Teams al momento).
        `cuando`: la fecha de la reunión (segundos) para su carpeta «AAAA-MM-DD Nombre»; por defecto, ahora (al subir
        un audio, la del audio)."""
        st = st or self.settings.get()
        if pedido.get("subir"):
            sug = {}  # un audio que ya existe: ni Teams ni Outlook
        if sug is None:
            sug = self._reunion_ahora(st)[0]
        if not pedido.get("subir") and not plantilla:  # lo preparado en Hoy para esta reunión, si es ahora
            prep = self._preparada(pedido["nombre"] if "nombre" in pedido else sug.get("name"), st)
            if prep:
                pedido = grabar_plan.con_preparada(pedido, prep)
        full, listas = {}, []
        for v in list_vocab():
            try:
                terms = read_vocab(v["_abs"])[1]
            except OSError:
                terms = None
            full[v["id"]] = v
            listas.append({"id": v["id"], "project": v["project"], "name": v["name"],
                           "n": len(terms) if terms is not None else None, "terms": terms or []})
        lib = getattr(self, "library", None)
        if lib is not None and hasattr(lib, "refresh") and getattr(lib, "tree", None) is None:
            try:
                lib.refresh(max_age=60)  # Ctrl+Alt+R nada más arrancar: el historial, aunque no se haya abierto la app
            except Exception:
                log_exc()
        hist = grabar_plan.historial_de(list(getattr(lib, "flat", None) or []), listas)
        p = grabar_plan.resolver(
            st, listas, sug, hist, pedido,
            carpeta_de=lambda v: self._carpeta_plan(v, st, full),
            carpeta_origen=self._carpeta_origen(st, full),
            sugerir_titulo=lambda nombre: calendario.suggest({"subject": nombre}, listas),
            motores=self._motores(st), links={k: l["name"] for k, l in LINKS.items()},
            carpeta_reunion=self._carpeta_reunion(st, plantilla, cuando), raiz=root_name())
        # la carpeta de Ajustes (la de las reuniones sin proyecto) guardada que ahora no se encuentra (una vinculada
        # desconectada): se graba en otra y se dice por qué en su línea (revisión 3 de B2)
        fija = perfil_de(st)["folder"]
        car = next((x for x in p["lineas"] if x["clave"] == "carpeta"), None)
        if fija is not None and perfil_folder(st) is None and car and car["origen"] in ("ajustes", "ultima") \
                and not car.get("aviso"):
            ruta = ruta_txt(fija)
            car["aviso"] = f"No se encuentra la carpeta de las reuniones sin proyecto ({ruta}): se guarda aquí."
        # un proyecto sin carpeta propia (sus listas vienen de una carpeta conectada y no se le ha asignado ninguna)
        # graba en la de Ajustes o en la última usada, que puede ser la de otro cliente: se dice cómo fijarle una
        proy = p["proyecto"].get("proyecto")
        if proy and car and car["origen"] in ("ajustes", "ultima") and not car.get("aviso"):
            car["aviso"] = (f"{proy} no tiene carpeta propia: asígnale una en Ajustes › Proyectos o, al grabar, con "
                            f"«Cambiar» y «Todas las de {proy}».")
        # un nombre escrito con \ / : * ? " < > | (p. ej. «Comité: Q4») no vale como nombre de archivo y rec_start lo
        # rechazaría al empezar: se graba sin esos signos, como la carpeta de la reunión (rec_destino), y se dice
        n = p["rec"].get("name") or ""
        if n:
            try:
                clean_name(n)
            except ApiError:
                p["rec"]["name"] = p["nombre"]["archivo"] = calendario.nombre_archivo(n)
        return p

    PLANTILLA_REUNION = "AAAA-MM-DD Nombre de la reunión"

    @classmethod
    def _carpeta_reunion(cls, st, plantilla=False, cuando=None):
        """La carpeta «AAAA-MM-DD Nombre» que creará rec_start dentro de la base, o None. Con la rama de carpetas
        (B1b): su meeting_dir_preview, con el nombre como lo limpia rec_start (rec_destino hace lo mismo). Sin ella,
        None: la grabación va en la base. plantilla (Ajustes › Al grabar, sin reunión): sin nombre, el patrón y no
        «AAAA-MM-DD HH-MM» de ahora, que cambiaría cada minuto."""
        previa = globals().get("meeting_dir_preview")
        if not callable(previa):
            return None

        def reunion(base, nombre):
            if plantilla and not nombre:
                try:
                    activa = previa(base or "", "", st) != (base or "")
                except (ApiError, OSError):
                    return None
                return f"{base}/{cls.PLANTILLA_REUNION}".lstrip("/") if activa else None
            label = ""
            if nombre:
                try:
                    label = clean_name(nombre)
                except ApiError:
                    label = calendario.nombre_archivo(nombre)
            try:
                sub = previa(base or "", label, st, **({} if cuando is None else {"when": cuando}))
            except (ApiError, OSError):
                return None
            return sub if sub and sub != (base or "") else None
        return reunion

    def _carpetas_de_proyectos(self, st):
        """Ajustes › Proyectos: la carpeta principal de cada proyecto y de cada workstream con su origen, con el mismo
        cálculo que el plan (lo que dice la tabla es lo que se usará al grabar). `sin_asignar`: la que tendría el
        proyecto si se le quita la suya (para la opción «Sin asignar» al editarla)."""
        full = {v["id"]: v for v in list_vocab()}

        def una(v, s):
            rel = self._carpeta_plan(v, s, full)
            o, t = self._carpeta_origen(s, full)(v, rel)
            return {"rel": rel or "", "ruta": ruta_txt(rel), "origen": o, "texto": t}
        sin_ws = dict(st, workstream_folders={})
        proyectos, workstreams = {}, {}
        for v in full.values():
            workstreams[v["id"]] = una(v, st)
            if v["project"] not in proyectos:
                pf = {k: x for k, x in (st.get("project_folders") or {}).items() if k != v["project"]}
                proyectos[v["project"]] = dict(una(v, sin_ws), sin_asignar=una(v, dict(sin_ws, project_folders=pf)))
        return {"proyectos": proyectos, "workstreams": workstreams}

    def grabar_plan_api(self, b):
        """POST /api/grabar/plan {nombre?, vocab?, participantes?, cambios?, recientes?}: lo que se va a usar al grabar,
        línea a línea con su origen, y el cuerpo exacto de /api/rec/start (rec). Con recientes, también las reuniones
        recientes para el autocompletado del nombre."""
        pedido = {k: b[k] for k in ("nombre", "vocab", "participantes") if k in b}
        if isinstance(b.get("cambios"), dict):
            pedido["cambios"] = b["cambios"]
        cuando = None
        if b.get("subir"):  # «Subir audio»: el plan de subir uno que ya existe (ver plan.resolver)
            pedido.update(subir=True, archivo=str(b.get("archivo") or ""), varios=bool(b.get("varios")))
            cuando = fecha_subida(b.get("fecha"))
        st = self.settings.get()
        if isinstance(b.get("ajustes"), dict):  # Ajustes › Al grabar: cómo quedaría con lo que aún no se ha guardado
            st = ajustes_previstos(st, b["ajustes"])
        p = self.plan_grabar(pedido, st=st, plantilla=isinstance(b.get("ajustes"), dict), cuando=cuando)
        if b.get("recientes"):
            vs = list_vocab()
            p["recientes"] = grabar_plan.recientes(grabar_plan.historial_de(
                list(getattr(getattr(self, "library", None), "flat", None) or []), vs), {v["id"]: v for v in vs})
        if b.get("proyectos"):  # Ajustes › Proyectos: la carpeta de cada uno, con lo que aún no se ha guardado
            p["carpetas"] = self._carpetas_de_proyectos(st)
        return p

    def grabar_recordar(self, b):
        """POST /api/grabar/recordar {clave, valor, alcance, vocab?}: «Cambiar» de la tarjeta (o de Ajustes) con «Usarlo
        en» las del proyecto, las del workstream o todas (Ajustes). «Solo esta reunión» no guarda nada: va en el
        pedido del plan. valor None en el proyecto o el workstream = quitarlo (vuelve a heredar)."""
        clave, alcance, valor = b.get("clave"), b.get("alcance"), b.get("valor")
        v = find_vocab(None, None, b.get("vocab")) if b.get("vocab") else None
        if alcance not in ("proyecto", "workstream", "ajustes"):
            raise ApiError("¿Para qué reuniones? Elige las del proyecto o todas.")
        if alcance != "ajustes" and not v:
            raise ApiError("Elige primero el proyecto de la reunión.")
        sitio = {"proyecto": "project", "workstream": "workstream"}.get(alcance)
        if clave == "idioma":
            lang = None if valor is None else str(valor or "").strip().lower()
            if lang and not IDIOMA_RE.match(lang):
                raise ApiError("Idioma no válido: " + lang)
            lang = "" if lang == "auto" else lang
            if alcance == "ajustes":
                cambios = {"language": lang or ""}
            else:
                k = v["id"] if sitio == "workstream" else v["project"]
                cambios = {sitio + "_langs": lambda cur: _con_o_sin(cur, k, lang)}
        elif clave == "carpeta":
            rel = None if valor is None else str(valor or "").replace("\\", "/").strip("/")
            if rel is not None and in_app_dir(safe(rel, "dir")):
                raise ApiError("No se puede grabar ahí")
            if alcance == "ajustes":
                cambios = {"perfil": {"folder": rel}}
            elif alcance == "workstream":  # Ajustes › Proyectos: la de un workstream (None: la del proyecto)
                cambios = {"workstream_folders": lambda cur: _con_o_sin(cur, v["id"], rel)}
            else:
                cambios = {"project_folders": lambda cur: _con_o_sin(cur, v["project"], rel)}
        elif clave in ("mic", "sys", "live", "preset", "contexto") and alcance == "ajustes":
            if clave == "preset" and valor not in LIVE_PRESETS:
                raise ApiError("Ese motor no existe.")
            otro = self.settings.get().get("rec_sys" if clave == "mic" else "rec_mic")
            if clave in ("mic", "sys") and valor == "none" and otro == "none":
                raise ApiError(SIN_AUDIO)
            if clave == "contexto":
                cambios = {"perfil": check_perfil({"context": valor})}
            elif clave == "live":
                cambios = {"rec_directo": bool(valor)}
            else:
                key = {"mic": "rec_mic", "sys": "rec_sys", "preset": "live_preset"}[clave]
                cambios = {key: str(valor or "")[:300] or ("all" if clave == "sys" else "default")}
        else:
            raise ApiError("Eso no se puede guardar así.")

        def upd(d):
            for k, x in cambios.items():
                if k == "perfil":
                    update_perfil(d, x)
                else:  # los diccionarios por proyecto, sobre lo guardado en ese momento (dentro del cerrojo)
                    d[k] = x(d.get(k)) if callable(x) else x
        self.settings.update(upd)
        log(f"   al grabar: {clave} para " + ("todas (Ajustes)" if alcance == "ajustes" else alcance))
        return {"ok": True}

    # --- grabadora
    def _monitor_watchdog(self):
        while True:  # si la ventana de "Grabar" se cierra sin avisar, el medidor se apaga solo
            time.sleep(2)
            m = self.monitor
            if m and time.time() - self.monitor_seen > 8:
                self.monitor = None
                m.stop(timeout=2)
            # el PC despierto mientras se graba o hay trabajos (punto 30): desde este hilo, que vive lo que la app (la
            # peticion a Windows es del hilo que la hace)
            try:
                self.despierto.al_dia(self._motivo_despierto())
            except Exception:  # noqa: BLE001  el vigilante no puede pararse por esto
                pass

    @property
    def despierto(self):
        """Lo pedido a Windows para que no se suspenda (despierto.py): uno por App, al primer uso."""
        d = self.__dict__.get("_despierto")
        if d is None:  # setdefault: si dos hilos llegan a la vez, los dos se quedan con el mismo
            d = self.__dict__.setdefault("_despierto", despierto_mod.Despierto())
        return d

    def _motivo_despierto(self):
        """Por que no debe suspenderse: grabando (la grabadora propia) y/o con trabajos en cola o en marcha."""
        r = self.rec
        jobs = getattr(self, "jobs", None)
        trabajos = False
        if jobs is not None:
            with jobs.lock:
                trabajos = any(j.status in ("queued", "running") for j in jobs.list)
        return despierto_mod.motivo(bool(r and r.alive()), trabajos)

    def rec_devices(self, q):
        at, cached = self.devices_cache
        if not cached or time.time() - at > 30 or q.get("refresh"):
            r = subprocess.run([sys.executable, os.path.join(APP_DIR, "recorder.py"), "--list"], cwd=APP_DIR,
                               capture_output=True, timeout=30, creationflags=engines.NO_WINDOW)
            try:
                cached = json.loads(r.stdout.decode("utf-8", "replace"))
            except ValueError:
                raise ApiError("No se pudieron leer los dispositivos de audio: "
                               + r.stderr.decode("utf-8", "replace")[-300:])
            self.devices_cache = (time.time(), cached)
        st = self.settings.get()
        mic, sys_dev = st.get("rec_mic") or "default", st.get("rec_sys") or "all"
        # si lo guardado ya no esta conectado, se propone el predeterminado (no un dispositivo fantasma)
        if mic not in ("default", "none") and not any(m["id"] == mic for m in cached["mics"]):
            mic = "default"
        if sys_dev not in ("all", "default", "none") and not any(o["id"] == sys_dev for o in cached["outputs"]):
            sys_dev = "all"
        return dict(cached, mic=mic, sys=sys_dev)

    def rec_status(self, q):
        r, m = self.rec, self.monitor
        if q.get("monitor"):
            self.monitor_seen = time.time()
        out = {"recording": bool(r and r.alive()), "monitor": bool(m and m.alive()), "meeting": self.meeting,
               "build": BUILD}
        if r and r.alive():
            out.update(path=rel_of(r.path), started=r.started, resumen=getattr(self, "rec_resumen", ""), **r.status)
            out["sin_estado_s"] = r.sin_estado_s()  # la grabadora atascada (escritura bloqueada): la franja lo dice
            # avisos de la franja del directo (B6): fuente callada y «paro en 30 s»
            out["callada"] = r.callada.estado(time.monotonic()) if getattr(r, "callada", None) else {}
            quedan = self.fin_llamada.quedan(time.monotonic())
            out["fin_llamada"] = {"app": "Teams", "quedan_s": quedan} if quedan is not None else None
        elif m and m.alive():
            out.update(m.status)
        p = self.parada_auto
        if not out["recording"] and p and time.time() - p["at"] < 120:
            out["parada"] = p  # se paró sola al terminar la llamada: la interfaz lo dice al ver que ya no se graba
        out["cortes"] = self.cortes.estado()  # cortadas y recuperadas (la interfaz las avisa hasta que las cierres)
        c = next((x for x in reversed(out["cortes"]) if x["tipo"] == "cortada"), None)
        if not out["recording"] and r and c and c["path"] == rel_of(r.path):
            out["cortada"] = c  # la ultima grabacion no se paro: se corto (no es «Grabación guardada»)
        elif (not out["recording"] and r and r.path and getattr(r, "on_end", None) and not getattr(r, "stopping", True)
              and not getattr(r, "apuntado", True)):
            # el proceso ya no esta y aun no se ha apuntado el corte (se esta leyendo lo ultimo): ya es un corte
            out["cortada"] = {"tipo": "cortada", "path": rel_of(r.path), "pendiente": True}
        if not out["recording"]:
            out["siguiente"] = self._siguiente_pub()
        return out

    def rec_monitor(self, b):
        """Medidores de nivel para probar micro y audio del ordenador antes de grabar."""
        with self.rec_lock:
            old, self.monitor = self.monitor, None
            if old:
                old.stop(timeout=2)
            if b.get("stop") or (self.rec and self.rec.alive()):
                return {"ok": True}
            self.monitor_seen = time.time()
            self.monitor = RecorderProcess(None, b.get("mic"), b.get("sys"), APP_DIR, DATA_DIR)
        return {"ok": True}

    def rec_start(self, b, _cont=None):
        """_cont: la continuacion de una grabacion cortada (solo _continuar; nunca desde la peticion HTTP)."""
        with self.rec_lock:
            if self.rec and self.rec.alive():
                raise ApiError("Ya hay una grabación en marcha.")
            mic, sys_dev = b.get("mic") or "default", b.get("sys") or "default"
            if mic == "none" and sys_dev == "none":
                raise ApiError("Elige al menos el micrófono o el audio del ordenador.")
            if "name" not in b and "vocab" not in b:  # sin nombre ni tema (otra app): la reunión en curso, si la hay
                sug = self._reunion_ahora()[0]
                b = dict(b, name=sug.get("name") or "", vocab=sug.get("vocab") or "",
                         participantes=b.get("participantes") or calendario.join_participants(sug.get("participants")),
                         sugerido=b.get("sugerido") or sug.get("source") or "")
            folder = safe(b.get("folder", ""), "dir")  # la carpeta principal (rec_folder_for)
            if in_app_dir(folder):
                raise ApiError("No se puede grabar ahí")
            try:  # la que da la regla con su tema: si es esta, la carpeta no se eligio a mano (_tema_movido)
                vb = find_vocab(None, None, b["vocab"]) if b.get("vocab") else None
                por_regla = _norm_dir(safe(rec_folder_origin(vb, self.settings.get())[0], "dir")) == _norm_dir(folder)
            except (ApiError, OSError):
                por_regla = False
            lt = time.localtime()
            label = clean_name(b["name"]) if b.get("name") else ""
            name = time.strftime("%Y-%m-%d %H-%M-%S", lt) + (f" - {label}" if label else "")
            cont = _cont  # la grabacion se corto: se sigue en la misma carpeta (_rec_terminada)
            # una carpeta por reunion, «AAAA-MM-DD Nombre», dentro de la principal (msg 1158); se crea al grabar
            mname = fit_meeting_dir(folder, label, lt, name) if por_reunion(self.settings.get()) and not cont else None
            mdir = free_dir(folder, mname) if mname else None
            path = os.path.join(cont["dir"] if cont else mdir or folder, name + ".wav")
            if os.path.exists(path):
                raise ApiError("Ya existe un archivo con ese nombre.")
            made = mdir is not None and not os.path.isdir(mdir)
            if mdir:
                try:
                    os.makedirs(mdir, exist_ok=True)
                except OSError as e:  # sin permiso de escritura (p. ej. una carpeta de solo lectura): se dice
                    raise ApiError(f"No se puede crear la carpeta de la reunión en {display_rel(rel_of(folder))} "
                                   f"({e.__class__.__name__}).")
                carpetas_reunion().add(rel_of(mdir))
            if por_regla:
                self.__dict__.setdefault("_por_regla", set()).add(rel_of(path))
            if self.monitor:
                self.monitor.stop(timeout=2)
                self.monitor = None
            rel, props = rel_of(path), {}
            # la grabacion es de la app: mientras dure, el vigilante no arranca el directo por su cuenta
            # (si se eligio "solo grabar", no se transcribe hasta que tu lo pidas)
            self.jobs.claimed.add(rel)
            started = False
            try:
                err_disco, aviso_disco = disco_al_empezar(os.path.dirname(path))  # 691 MB por hora
                if err_disco:
                    raise ApiError(err_disco)
                self.cortes.abierta(path)  # antes de que exista: si Tenca se cae, al arrancar se repara su cabecera
                if cont and cont.get("corte"):  # antes de que la interfaz vea que graba: «Sigue grabando en …»
                    self.cortes.cambiar(cont["corte"], continuacion=rel)
                self.rec = RecorderProcess(path, mic, sys_dev, APP_DIR, DATA_DIR)
                if cont and cont.get("mute"):  # el micro silenciado: antes de que escriba nada (lee stdin al empezar)
                    self.rec.send("mute")
                fin = time.monotonic() + REC_ARRANQUE_S
                while time.monotonic() < fin:  # espera a que exista el archivo con su cabecera
                    if os.path.isfile(path) and os.path.getsize(path) >= 44 or not self.rec.alive():
                        break
                    time.sleep(0.05)
                if not self.rec.alive():
                    raise ApiError(f"La grabadora no ha podido arrancar: {self.rec.motivo()} "
                                   "(ver %LOCALAPPDATA%\\AudiosApp\\recorder.log).")
                # desde aqui la grabadora esta grabando: lo que falle ya no la para ni se calla (02/11/2026: un error
                # tras arrancarla aviso «No se pudo grabar» y dejo la grabacion en marcha sin decirlo, hasta que el
                # vigilante arranco el directo por su cuenta y la segunda pulsacion la paro)
                started = True
                self.parada_auto = None  # «se paró al terminar la llamada» era de la grabación anterior
                avisos = [aviso_disco] if aviso_disco else []
                self.rec_body, self.rec_cadena = dict(b), cont or {"intentos": 0, "nombre": label}
                self.rec.on_end = self._rec_terminada  # desde aqui, si se corta sola, se avisa y se sigue
                preset = b.get("preset") or self.settings.get().get("live_preset") or "medium"
                v = find_vocab(None, None, b["vocab"]) if b.get("vocab") else None
                props = {"proyecto": v["project"], "workstream": v["name"], "vocabulario": v["id"]} if v else {}
                if b.get("participantes"):  # sugeridos por Teams/Outlook: a la cabecera del .md, con su origen
                    completo = " ".join(str(b["participantes"]).split())
                    props["participantes"] = completo[:500]
                    # sin confirmar: ni el resumen en la nube ni los destinos de salida los llevan hasta que el
                    # usuario los confirme o los edite en la pagina de la reunion (participantes_confirmados)
                    origen = str(b.get("sugerido") or "").strip().lower()
                    if origen in PARTICIPANTES_ORIGENES:
                        props["participantes_origen"] = origen
                        # para la pista de quien habla: solo si es, entero, el texto que la app escribe con una
                        # sugerencia reciente (dialogo, Ctrl+Alt+R, aviso); lo que traiga otro programa, o cortado, no
                        vigia = getattr(self, "vigia", None)
                        ult = (getattr(vigia, "ultimo", None) or {}).get("sugerencia") or {}
                        recientes = getattr(vigia, "participantes_recientes", None)
                        listas = [ult.get("participants")] + (recientes() if callable(recientes) else [])
                        if len(completo) <= 500 and any(
                                completo == " ".join(calendario.join_participants(p).split()) for p in listas if p):
                            if len(TEXTOS_SUGERIDOS) > 500:
                                TEXTOS_SUGERIDOS.clear()
                            TEXTOS_SUGERIDOS.add(completo)
                # perfil de la reunion, a la cabecera del .md: idioma y contexto (el directo, la cola y el resumen
                # los leen de ahi; se pueden cambiar en la pagina de la reunion) y la carpeta a la que copiar textos
                perfil = perfil_de(self.settings.get())
                if "language" in b:
                    lang = str(b.get("language") or "").strip().lower()
                    props["idioma"] = lang if lang and IDIOMA_RE.match(lang) else "auto"
                ctx = b.get("contexto") if "contexto" in b else perfil["context"]
                ctx = " ".join(str(ctx or "").split())[:engines.ABOUT_CHARS]
                if ctx:
                    props["contexto"] = ctx
                if props:  # antes de arrancar el directo, para que ya use ese vocabulario
                    try:
                        self.notes_save({"path": rel, "props": props, "_al_grabar": True})
                    except Exception as e:
                        log_exc()
                        avisos.append(f"sin cabecera de notas ({e.msg if isinstance(e, ApiError) else e})")

                # fuera de upd, que corre con el candado de los ajustes: project_home de una lista conectada lee las
                # carpetas de listas (OneDrive, la red) y mientras tanto nadie podria leer los ajustes
                try:  # con la grabadora ya en marcha nada de esto la para (02/11/2026): un fallo, al log y aviso
                    sin_casa = bool(v) and not por_regla and project_home(v) is None
                except Exception as e:
                    log_exc()
                    sin_casa = False
                    avisos.append(f"sin mirar la carpeta del proyecto ({e})")

                def upd(d):
                    if "vocab" in b:
                        d["rec_vocab"] = v["id"] if v else ""
                    # la proxima vez (y con Ctrl+Alt+R) se graba en la misma carpeta. Desde el plan, solo la de una
                    # reunion sin proyecto y si no se eligio «solo esta reunion»: la carpeta de un proyecto no pasa a
                    # ser «la ultima usada» de las reuniones sin proyecto (revision de B1)
                    if not b.get("plan") or (not v and b.get("recordar_carpeta", True)):
                        d["rec_folder"] = rel_of(folder)
                    # si el proyecto no tiene «la suya» en el arbol (solo tiene listas de una carpeta de vocabulario
                    # conectada: project_home), la elegida a mano pasa a ser su carpeta principal (9f49590). Tambien
                    # desde el plan: una eleccion explicita manda (decision de Claudia, 09/11/2026); pero no si en la
                    # tarjeta se eligio «solo esta reunion» (recordar_carpeta), que tambien es explicita
                    if sin_casa and b.get("recordar_carpeta", True):
                        d["project_folders"] = dict(d.get("project_folders") or {}, **{v["project"]: rel_of(folder)})
                    if b.get("plan"):
                        # desde el plan (tarjeta, Ctrl+Alt+R, aviso): lo demas de esta reunion no pasa a ser el
                        # predeterminado; eso se guarda a proposito con «Usarlo en» (D1)
                        return
                    # "solo grabar" y "sin micro" son para una grabacion concreta: no se quedan como
                    # predeterminado (la siguiente, y Ctrl+Alt+R, vuelven a transcribir y a grabar el micro)
                    if mic != "none":
                        d["rec_mic"] = mic
                    d.update(rec_sys=sys_dev, live_preset=preset if preset in LIVE_PRESETS else "medium")
                    # el idioma elegido para ESTA reunion va en su .md; el ajuste general solo lo cambia el perfil
                try:
                    if not cont:  # la continuacion no cambia lo predeterminado
                        self.settings.update(upd)
                except Exception as e:
                    log_exc()
                    avisos.append(f"sin guardar los ajustes de la grabación ({e})")
                self.library.at = 0
                if b.get("live"):
                    try:
                        self.jobs.start_live(rel, preset)
                    except Exception as e:
                        log_exc()
                        avisos.append(f"el directo no ha arrancado ({e.msg if isinstance(e, ApiError) else e})")
                        self.jobs.claimed.discard(rel)  # que el vigilante lo intente por su cuenta
            finally:
                if not started:
                    self.jobs.claimed.discard(rel)
                    self.cortes.cerrada(path)
                    if made:  # la carpeta de la reunion recien creada, si la grabadora no llego a escribir nada
                        try:
                            os.rmdir(mdir)
                            carpetas_reunion().discard(rel_of(mdir))
                        except OSError:
                            pass
        self.rec_resumen = " ".join(str(b.get("resumen") or "").split())[:300]  # lo que dice el plan (Inicio, toasts)
        self.siguiente, self._siguiente_cache = None, (0.0, None)  # empieza otra grabación: lo propuesto, fuera
        try:  # la reunión que se graba: al parar, «la siguiente» es otra (_siguiente); la continuación es la misma
            self.grabada = self.grabada if cont else self._que_se_graba(b.get("name"))
        except Exception:
            log_exc()
            self.grabada = [{"asunto": b["name"], "inicio": None}] if b.get("name") else []
        log(f"● Grabando: {rel}" + (f" [{v['project']} · {v['name']}]" if v else "")
            + ("" if b.get("live") else " (solo grabar, sin transcribir)")
            + (f" · aviso: {'; '.join(avisos)}" if avisos else ""))
        return {"path": rel, "workstream": f"{v['project']} · {v['name']}" if v else None,
                "participantes": props.get("participantes"), "aviso": "; ".join(avisos) or None,
                "carpeta": rel_of(os.path.dirname(path)), "carpeta_principal": rel_of(folder)}

    def rec_mic(self, b):
        """Silenciar / reactivar mi micro a mitad de grabacion (se graba silencio en su lugar)."""
        r = self.rec
        if not r or not r.alive():
            raise ApiError("No hay ninguna grabación en marcha.")
        mute = bool(b.get("mute"))
        if not r.send("mute" if mute else "unmute"):
            raise ApiError("No se pudo hablar con la grabadora.")
        r.status["mic_muted"] = mute  # para que la interfaz lo vea al momento
        log(f"   micro {'silenciado' if mute else 'reactivado'}")
        return {"mic_muted": mute}

    def audio_mezclado(self, b):
        """«Exportar audio mezclado» (reproductor, pistas.js): una grabación en dos pistas a un WAV normal en el
        Escritorio, con lo que se oye (tú, los demás y el eco). La que se está grabando, no."""
        abs_path = safe(alias_of(b.get("path")), "file")
        r = self.rec
        grabando = r.path if r and r.alive() else None
        try:
            out = pistas.exportar_peticion(abs_path, desktop_dir(), b, grabando=grabando)
        except ValueError as e:
            raise ApiError(str(e))
        log(f"🎧 audio mezclado exportado ({out['segundos'] / 60:.1f} min)")
        return out

    def rec_stop(self, b):
        with self.rec_lock:
            r = self.rec
            if r and not r.alive() and self._cancelar_continuacion(r):  # «Parar» en los 2 s antes de la continuacion
                return {"path": rel_of(r.path), "seconds": (r.status or {}).get("t", 0)}
            if not r or not r.alive():
                raise ApiError("No hay ninguna grabación en marcha.")
            secs = r.stop()
            self.rec = None
            self.jobs.claimed.discard(rel_of(r.path))
            self.cortes.cerrada(r.path)
        if getattr(r, "fatal", None):  # parada pedida, pero no se pudo escribir lo ultimo: se dice
            log(f"✖ Grabación parada a medias: {rel_of(r.path)} ({r.fatal})")
            cue("error")
            notify("✖ Grabación guardada a medias", r.fatal)
        try:  # cuando hablo yo (micro por encima del audio del PC): sirve para etiquetar «Tu» sin biometria
            tl = r.timeline()
            if tl and os.path.isfile(r.path):
                voices.write_micro(voices.sidecar_path(r.path), tl)
        except Exception:
            log_exc()
        if os.path.isfile(r.path):  # informe tecnico (punto 32): segundos, audio perdido y eco; antes de moverla
            informe.grabacion(r.path, r.status, secs)
        self.library.at = 0
        log(f"■ Grabación parada: {rel_of(r.path)} ({secs / 60:.1f} min)")
        # «Ya ha empezado «X» · Grabar · Ahora no»: durante SIGUIENTE_S, la reunión de ahora que no sea la grabada
        self.siguiente = {"excluir": list(getattr(self, "grabada", None) or []), "parada": time.time(),
                          "hasta": time.time() + self.SIGUIENTE_S, "descartada": False, "flotante": False,
                          "dicha": False}
        self._siguiente_cache = (0.0, None)
        if self._movs().get(rel_of(r.path)):  # «solo grabar» no deja trabajos: si tenia otra carpeta, se mueve ya
            threading.Thread(target=self._aplicar_movimientos, args=(rel_of(r.path),), daemon=True,
                             name="mover").start()
        return {"path": rel_of(r.path), "seconds": secs, "aviso": getattr(r, "fatal", None)}

    def _rec_terminada(self, r):
        """La grabadora ha terminado sin que nadie la parase (RecorderProcess.on_end, en su hilo de lectura): se dice
        (rec_status «cortada», aviso de Windows, sonido de error y «Grabación interrumpida a las hh:mm» en la reunión),
        se guarda la línea de tiempo del micro, se suelta la reserva y se sigue grabando en una «(continuación)» en la
        misma carpeta a los ~2 s, como mucho Cortes.MAX_REINTENTOS veces."""
        rel, motivo, a_las = rel_of(r.path), r.motivo(), time.strftime("%H:%M")
        cadena = dict(self.rec_cadena or {"intentos": 0, "nombre": ""})
        parada = getattr(self, "_sin_seguir", None) is r  # «Parar» justo al morir: no se sigue
        sigue = self.rec is r and cadena["intentos"] < Cortes.MAX_REINTENTOS and not parada
        # lo primero, antes de tocar el disco: un sondeo que llegue ahora ya ve «cortada»
        e = self.cortes.apuntar(tipo="cortada", path=rel, nombre=os.path.basename(r.path), a_las=a_las, motivo=motivo,
                                sigue=sigue, continuacion=None, error="parada a mano" if parada else None)
        r.apuntado = True  # rec_status deja de dar el «cortada» provisional
        self.jobs.claimed.discard(rel)
        self.cortes.cerrada(r.path)
        try:
            tl = r.timeline()
            if tl and os.path.isfile(r.path):
                voices.write_micro(voices.sidecar_path(r.path), tl)
        except Exception:
            log_exc()
        try:
            if os.path.isfile(r.path):
                self.notes_save({"path": rel, "props": {"interrumpida": f"{a_las} · {motivo}"}, "_al_grabar": True})
        except Exception:
            log_exc()
        if os.path.isfile(r.path):  # informe tecnico (punto 32), tambien de la que se corto
            informe.grabacion(r.path, r.status)
        self.library.at = 0
        log(f"✖ Grabación interrumpida a las {a_las}: {rel} ({motivo})"
            + (" · se sigue en una continuación" if sigue else " · no se sigue"))
        cue("error")
        notify("✖ Grabación interrumpida", f"A las {a_las}: {motivo}. "
               + ("Intento seguir grabando en la misma carpeta." if sigue else f"Ya no se graba: abre {APP_NAME}."))
        if sigue:
            threading.Thread(target=self._continuar, args=(r, e["id"], cadena), daemon=True,
                             name="continuar-grabacion").start()

    def _cancelar_continuacion(self, r):
        """Se cortó y aún no se sigue (los ~2 s de Cortes.REINTENTO_S): ya no se sigue. True si había algo que
        cancelar."""
        c = next((x for x in reversed(self.cortes.estado()) if x["tipo"] == "cortada"), None)
        if c and c["path"] == rel_of(r.path):
            if not (c["sigue"] and not c["continuacion"]):
                return False
            self.cortes.cambiar(c["id"], sigue=False, error="parada a mano")
        elif not (r.on_end and not r.stopping):  # ni cortada ni a punto de apuntarse como cortada
            return False
        self._sin_seguir = r  # tambien si el corte aun no se ha apuntado: _rec_terminada ya no sigue
        log("■ Parada a mano antes de seguir en la continuación")
        return True

    def _continuar(self, r, corte, cadena):
        """La continuación de una grabación cortada: lo mismo (tema, participantes, idioma, directo) en su carpeta."""
        time.sleep(Cortes.REINTENTO_S)
        with self.toggle_lock:
            if self.rec is not r or getattr(self, "_sin_seguir", None) is r:  # se grabo otra cosa o se pidio parar
                self.cortes.cambiar(corte, sigue=False)
                return
            n = cadena["intentos"] + 1
            nombre = f"{cadena.get('nombre') or ''} {'(continuación)' if n == 1 else f'(continuación {n})'}".strip()
            sigue_en = dict(cadena, intentos=n, dir=os.path.dirname(r.path), corte=corte,
                            mute=bool((r.status or {}).get("mic_muted")))
            body = dict(self.rec_body or {}, name=nombre)
            try:
                res = self.rec_start(body, _cont=sigue_en)
            except Exception as err:
                msg = err.msg if isinstance(err, ApiError) else f"{err.__class__.__name__}: {err}"
                self.cortes.cambiar(corte, sigue=False, error=msg, continuacion=None)
                with self.rec_lock:  # la continuacion no arranco: rec_status vuelve a dar la cortada, no «guardada»
                    if self.rec is not r and not (self.rec and self.rec.alive()):
                        self.rec = r
                log(f"   ✖ no se ha podido seguir grabando: {msg}")
                cue("error")
                notify("✖ No se ha podido seguir grabando", msg)
                return
        if (r.status or {}).get("mic_muted"):  # el micro estaba silenciado a proposito: tambien en la continuacion
            try:
                self.rec_mic({"mute": True})
            except ApiError:
                log_exc()
        log(f"● Sigue grabando tras el corte: {res['path']} ({n} de {Cortes.MAX_REINTENTOS})")
        notify(f"● {APP_NAME} sigue grabando", f"En «{os.path.basename(res['path'])}», en la misma carpeta.")

    def _recuperar_grabaciones(self):
        """Al arrancar: las grabaciones propias que quedaron con la cabecera abierta (Tenca se cerró de golpe) se
        cierran y se dice (Cortes.recuperar: solo las de Tenca y solo si ya no crecen)."""
        def hecha(p, mtime):
            hoy = time.strftime("%Y-%m-%d") == time.strftime("%Y-%m-%d", time.localtime(mtime))
            a_las = time.strftime("%H:%M" if hoy else "%d/%m %H:%M", time.localtime(mtime))
            motivo = f"{APP_NAME} se cerró mientras grababa (se apagó el PC, un cuelgue o un corte de luz)"
            rel = None
            try:
                rel = rel_of(p)
                self.notes_save({"path": rel, "props": {"interrumpida": f"{a_las} · {motivo}"}, "_al_grabar": True})
            except Exception:
                log_exc()
            self.cortes.apuntar(tipo="recuperada", path=rel, nombre=os.path.basename(p), a_las=a_las, motivo=motivo,
                                sigue=False, continuacion=None, error=None)
            self.library.at = 0
            log(f"   Se recuperó la grabación {rel or p}, cortada a las {a_las}: cabecera del WAV cerrada")
        try:
            self.cortes.recuperar(hecha)
        except Exception:
            log_exc()

    def shutdown_prepare(self):
        """Antes de terminar el proceso: medidor y grabadora (si quedara alguna) y los trabajos."""
        with self.rec_lock:
            for p in (self.monitor, self.rec):
                if p and p.alive():
                    try:
                        p.stop(timeout=3)
                    except Exception:
                        log_exc()
            self.monitor = self.rec = None
        try:  # destinos de salida: las reescrituras pendientes (4 s) no se pierden al apagar
            self.jobs.flush_outputs()
        except Exception:
            log_exc()
        self.jobs.shutdown()

    def apagar(self, b):
        """Ajustes > Apagar y «Salir» del icono de la bandeja (tray_salir, con b["origen"] == "bandeja"): las unicas
        salidas explicitas cuando no hay consola, y la misma. Con una grabacion en curso se niega; si no, responde y el
        proceso termina al cabo de 0,5 s (schedule_shutdown)."""
        if self.rec and self.rec.alive():
            raise ApiError(f"Hay una grabación en curso. Para la grabación antes de apagar {APP_NAME}.", 409)
        with self.jobs.lock:
            activos = sum(1 for j in self.jobs.list if j.status in ("queued", "running"))
        bandeja = b.get("origen") == "bandeja"
        self._movil_parar()  # deja de escuchar en la wifi (si el código del móvil estaba abierto)
        log(f"apagar pedido desde {'la bandeja' if bandeja else 'la interfaz'} ({activos} trabajos en cola o en "
            "marcha)")
        schedule_shutdown("bandeja > Salir" if bandeja else "Ajustes > Apagar")
        return {"ok": True, "activos": activos}

    def ocultar(self, b):
        """La pagina, tras el aviso de «sigue en segundo plano» (o sin el), pide ocultar la ventana propia."""
        return {"ok": bool(WINDOW is not None and WINDOW.hide())}

    def mostrar(self, b):
        """El acceso directo, al ver que la app ya corre, pide la ventana: la propia si existe. Si aun se esta abriendo
        (pending: en un PC lento, decenas de segundos), nada mas: sale sola y se trae al frente; abrir Edge ahi era la
        segunda ventana del 02/11/2026. Sin ventana propia, Edge (open_ui no lo repite si lo acaba de abrir)."""
        if WINDOW is not None:
            r = WINDOW.request_show()
            if r == "pending":
                return {"ok": True, "pending": True}
            if r:
                return {"ok": True}
        open_ui()
        return {"ok": True, "browser": True}
