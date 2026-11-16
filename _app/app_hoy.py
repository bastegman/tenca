# -*- coding: utf-8 -*-
"""App, la parte de la reunion en curso y la agenda: la llamada que empieza o acaba (Teams, Outlook), la siguiente
reunion, la pantalla Hoy y sus reuniones preparadas, y «Mis pendientes» (punto 28: las tareas sin hacer de todas las
reuniones, con pendientes.py).

Salio tal cual de server.py al partirlo (15/11/2026): App hereda de esta clase y server.py la reexporta."""
import os
import time
from datetime import datetime, timedelta

import calendario
import directo_avisos
import engines
import pendientes
import plan as grabar_plan
from api import PORT
from app_grabar import teams_estado
from biblioteca import IDIOMA_RE
from common import APP_NAME, ApiError, log, log_exc, read_text
from listas import find_vocab, list_vocab, read_vocab
from recording import cue, mic_meeting_app, notify
from rutas import APP_DIR, AUDIO_EXT, alias_of, rel_of, safe


class Hoy:
    def _meeting_watch(self):
        """Cada 8 s, el vigilante de llamadas; mientras se confirma el fin de una llamada o cuentan los 30 s del
        «paro en 30 s», el fin de la llamada se mira cada segundo (una lectura nueva justo antes de parar)."""
        notified, siguiente = None, 0.0
        while True:
            time.sleep(1)
            try:
                if time.monotonic() >= siguiente:
                    siguiente = time.monotonic() + 8
                    notified = self._meeting_step(notified)
                elif self.fin_llamada.pendiente():
                    self._fin_llamada_tic()
            except Exception:
                pass

    def _meeting_step(self, notified):
        """Un paso del vigilante de llamadas (cada 8 s); devuelve la llamada ya avisada. «Avisarme si estoy en una
        llamada» (meeting_detect) es el interruptor general: apagado, ni toast, ni aviso flotante (aunque «Aviso
        flotante» siga marcado), ni el chip de la interfaz, que se pinta con `meeting`."""
        leido = mic_meeting_app()
        # dos lecturas seguidas (16 s) antes de dar la llamada por buena: una lectura suelta del registro de privacidad
        # no abre el aviso flotante, ni manda el toast, ni pinta el chip (02/11/2026: saltaban al arrancar sin reunion)
        app, self.meeting_prev = (leido if leido == self.meeting_prev else None), leido
        recording = bool(self.rec and self.rec.alive())
        if recording:  # para el aviso del sonido del ordenador callado: solo cuenta en una llamada (directo_avisos)
            self.rec.en_llamada = bool(leido)
        fin = getattr(self, "fin_llamada", None)
        if fin is not None and (recording or fin.pendiente()):  # sin grabar no se lee nada más
            try:
                self._fin_llamada_tic()
            except Exception:
                log_exc()
        st = self.settings.get()
        detect = st.get("meeting_detect", True)
        self.meeting = app if detect else None
        if not detect:
            self.vigia.cerrar_aviso()  # si estaba abierto cuando se desmarco
        if not recording:  # «Ya ha empezado «X»» tras parar: antes que el de la llamada (no se apilan)
            try:
                if self._siguiente_aviso(app, st):
                    notified = app
            except Exception:
                log_exc()
        # aviso flotante «Grabar / ✕» (calendario.py + aviso.py): una vez por llamada y nunca si ya se graba;
        # mientras dure la llamada se lee la reunión (Teams y, si está activado, Outlook) para sugerir
        if self.vigia.tic(app, recording, st if detect else dict(st, rec_aviso=False),
                          lambda: calendario.reunion_actual(st, self._vocab_terms()),
                          PORT, APP_DIR, creationflags=engines.NO_WINDOW,
                          tema_plan=(lambda: self.tema_del_plan(st)) if hasattr(self, "tema_del_plan") else None):
            notified = app  # el aviso flotante sustituye al toast de Windows en esa llamada
            log(f"   reunión detectada ({app}): aviso flotante abierto")
        if detect and app and not recording and notified != app:
            notified = app
            notify("¿Quieres tomar notas de la sesión?", "Pulsa Ctrl+Alt+R para empezar a tomar notas.")
            log(f"   reunión detectada ({app}): aviso enviado")
        if not app:
            notified = None
        return notified

    def _fin_llamada_tic(self):
        """Una lectura del fin de la llamada de Teams que se graba (directo_avisos.FinDeLlamada) y lo que toque."""
        r = self.rec
        grabando = rel_of(r.path) if r and r.alive() and r.path else None
        # con «Avisarme si estoy en una llamada» apagado no se vigilan las llamadas: tampoco para proponer parar
        vigila = grabando and self.settings.get().get("meeting_detect", True) is not False
        mic, ventana_fn = teams_estado() if vigila else ("duda", lambda: None)
        accion = self.fin_llamada.tic(grabando, time.monotonic(), mic, ventana_fn)
        if accion == "propone":
            log(f"   la llamada de Teams ha terminado: se para la grabación en {directo_avisos.PARA_EN_S:.0f} s si no "
                "se pide seguir")
            notify("La llamada ha terminado", f"{APP_NAME} para la grabación en {directo_avisos.PARA_EN_S:.0f} s. "
                   f"Abre {APP_NAME} y pulsa «No terminar» si no ha acabado.")
        elif accion == "cancela":
            log("   la llamada sigue (o no se puede saber): no se para la grabación")
        elif accion == "para":
            with self.toggle_lock:
                r = self.rec
                if not (r and r.alive() and rel_of(r.path) == grabando):
                    return  # entretanto se paró a mano (o es otra grabación)
                # antes de parar: el sondeo de la interfaz que vea «ya no se graba» tiene que ver también el porqué (si
                # no, en Windows, con el sonido de cue, salía «Grabación guardada» a secas: ui-smoke del 10/11/2026)
                self.parada_auto = {"motivo": "llamada", "path": grabando, "at": time.time()}
                try:
                    res = self.rec_stop({})
                except ApiError:
                    self.parada_auto = None
                    return
            # la reunión siguiente (ya empezada, o la que empiece) puede volver a ofrecer el aviso flotante «● Grabar»
            self.vigia.avisado = None
            cue("stop")
            log(f"■ {res['path']}: parada al terminar la llamada de Teams (sin «No terminar» en "
                f"{directo_avisos.PARA_EN_S:.0f} s)")
            notify("■ Grabación parada", "La llamada ha terminado · la transcripción termina sola")

    def rec_seguir(self, b):
        """«No terminar» del aviso «La llamada ha terminado: paro en 30 s»: no se para (hasta otra llamada)."""
        habia = self.fin_llamada.seguir_grabando()
        if habia:
            log("   «No terminar»: la grabación sigue aunque la llamada haya terminado")
        # grabando: si el clic llegó tarde (ya se había parado o se está parando), la interfaz lo dice
        parando = self.fin_llamada.parando
        return {"ok": True, "seguia": habia, "grabando": bool(self.rec and self.rec.alive()) and not parando}

    def _vocab_terms(self):
        """Listas de vocabulario con sus términos: con ellas calendario.suggest elige el tema de la reunión. La que no
        se puede leer (a medio sincronizar en OneDrive, sin VPN, borrada entre listar y leer) se salta, como en
        /api/vocab: si no, se perdía la sugerencia entera, con el nombre y los participantes de Outlook (revisión de la
        integración del 08/11/2026)."""
        out = []
        for v in list_vocab():
            try:
                out.append(dict(v, terms=read_vocab(v["_abs"])[1]))
            except OSError:
                continue
        return out

    def reunion_actual(self, q):
        """GET /api/reunion/actual: ventana de Teams, cita de Outlook (si está activado, o ?outlook=1 para probar)
        y la sugerencia de nombre, tema y participantes. Lo usan el botón «Probar» y el diálogo Grabar."""
        # ?outlook=0 (la línea de Teams en Ajustes): solo Teams, sin despertar a Outlook por COM, y sin guardarlo como
        # la última lectura (la tarjeta y Ctrl+Alt+R usarían una sin la cita)
        outlook = {"1": True, "0": False}.get(str(q.get("outlook") or ""))
        r = calendario.reunion_actual(self.settings.get(), self._vocab_terms(), outlook=outlook)
        return r if outlook is False else self.vigia.recordar(r)

    def calendario_estado(self, q):
        """GET /api/calendario/estado[?probar=1]: el calendario en una frase, {nivel, estado, frase, accion} (contrato
        en calendario.estado). Sin probar no pide permisos ni abre avisos de Outlook; con probar, lo comprueba todo."""
        return calendario.estado(self.settings.get().get("outlook_on"), probar=q.get("probar") in ("1", "true", "si"))

    def calendario_activar(self, b):
        """POST /api/calendario/activar {"activar": true|false}: enciende o apaga la lectura del calendario y devuelve
        el estado (al encender, el de «Probar»: si Outlook pregunta, el usuario está delante para contestar)."""
        on = b.get("activar") is True
        self.settings.update(lambda d: d.update(outlook_on=on))
        log(f"   calendario: {'activado' if on else 'apagado'} desde Ajustes")
        r = calendario.estado(on, probar=on)  # primero «Probar», con el usuario delante por si Outlook pregunta
        ag = getattr(self, "agenda", None)
        if ag is not None:
            ag.pedir()  # después, la agenda de Hoy (o fuera, si se apaga): las dos a la vez competirían por Outlook
        return r

    # --- la pantalla Hoy (B7) y la agenda de Outlook de un mes
    def _reunion_ahora(self, st=None, recordar=True, citas=None, excluir=None):
        """(sugerencia, info) de la reunión de ahora: la última lectura del vigilante (o la ventana de Teams al momento)
        y, si no da nombre, la cita de la agenda de Outlook ya leída que encaja con ahora (calendario.reunion_de_ahora).
        La usan el plan de grabar (tarjeta, Ctrl+Alt+R, aviso, Hoy) y /api/hoy: una sola reunión de ahora para todos.
        `recordar=False` (Hoy, que pregunta cada 30 s): la lectura al momento no se guarda como la del vigilante, que
        durante una llamada la quiere con Outlook. `citas`: la agenda que ya se tiene (la misma que se enseña).
        `excluir`: la reunión que se acaba de grabar, para «la siguiente» (_siguiente): la misma regla, sin ella."""
        st = st or self.settings.get()

        def leer():
            return calendario.reunion_actual(st, self._vocab_terms(), outlook=False)
        if hasattr(self.vigia, "lectura"):
            lect = self.vigia.lectura(leer, recordar=recordar)
        else:  # un vigilante que solo da la sugerencia (los dobles de los tests)
            lect = {"sugerencia": self.vigia.sugerencia(leer)}
        if citas is None:
            ag = getattr(self, "agenda", None)
            citas = ag.citas() if ag is not None and st.get("outlook_on") and ag.fresca() else []
        return calendario.reunion_de_ahora(lect, citas, self._vocab_terms, excluir=excluir)

    # --- «Ya ha empezado «X» · Grabar · Ahora no» al parar una grabación (B7)
    SIGUIENTE_S = 600  # tras parar, 10 min para ver empezar otra reunión; la que se propone, mientras dure

    # /api/rec/status llega cada 1,5 s (y cada 2 s de cada aviso flotante): la reunión de ahora, que lee Teams y las
    # listas de vocabulario, no se mira más de una vez cada 15 s (al parar y con «Ahora no», al momento)
    SIGUIENTE_CACHE_S = 15

    def _que_se_graba(self, nombre):
        """Lo que se graba, para no proponerlo al parar ([{asunto, inicio}], ver calendario.excluida): si la grabación
        se llama como la reunión de ahora (o no tiene nombre), esa reunión con su hora de inicio (así, otra con el mismo
        asunto a otra hora sí se propone); si se llama de otra forma (escrito a mano), solo ese nombre, sin hora: la
        cita en curso no era la grabada y se puede proponer. Una llamada de Teams sin cita (sin hora) queda fuera
        siempre."""
        info = self._reunion_ahora()[1]
        suyos = [x for x in dict.fromkeys((info or {}).get(k) for k in ("asunto", "titulo")) if x]
        out = []
        # sin hora (solo la ventana de Teams), no hay «otra a otra hora» que distinguir: fuera también (revisión 3)
        if suyos and (not nombre or not info.get("inicio") or calendario.excluida([{"asunto": a} for a in suyos],
                                                                                  nombre)):
            out = [{"asunto": a, "inicio": info.get("inicio")} for a in suyos]
        if nombre and not calendario.excluida(out, nombre):
            out.append({"asunto": nombre, "inicio": None})
        return out

    def _siguiente(self, st=None, now=None):
        """(sugerencia, info) de la reunión que ya ha empezado y no es la que se acaba de grabar, o None. Es la reunión
        de ahora de siempre (_reunion_ahora, con la agenda ya leída) sin la grabada, y solo si está en curso: una
        ventana de reunión de Teams o una cita que ya ha empezado y no ha acabado. Se mira durante SIGUIENTE_S tras
        parar y, una vez vista, mientras dure (hasta su hora de fin o mientras siga su ventana de Teams). Nada al
        grabar, tras «Ahora no» ni con otra grabación."""
        s = getattr(self, "siguiente", None)
        if not s or s.get("descartada") or (getattr(self, "rec", None) and self.rec.alive()):
            return None
        t = time.time() if now is None else now.timestamp()
        if t > s["hasta"]:
            return None
        st = st or self.settings.get()
        if st.get("meeting_detect", True) is False:  # «Avisarme si estoy en una llamada» apagado: ni Teams ni esto
            return None
        sug, info = self._reunion_ahora(st, recordar=False, excluir=s["excluir"])
        if not info or not info.get("en_curso") or not sug.get("name"):
            return None
        try:
            fin = datetime.fromisoformat(info["fin"]).timestamp() if info.get("fin") else None
        except (TypeError, ValueError):
            fin = None
        if fin is not None and t > fin:
            return None
        s["hasta"] = max(s["hasta"], fin if fin is not None else t + 60)
        return sug, info

    def _siguiente_pub(self, fresco=False):
        """Lo que ve la interfaz de _siguiente ({titulo, inicio, fin, teams}, o None), mirado como mucho cada
        SIGUIENTE_CACHE_S. Lo que falle no tumba /api/rec/status ni Hoy: sin propuesta."""
        if not getattr(self, "siguiente", None):
            return None
        at, pub = getattr(self, "_siguiente_cache", (0.0, None))
        if not fresco and time.monotonic() - at < self.SIGUIENTE_CACHE_S:
            return pub
        try:
            r = self._siguiente()
        except Exception:
            log_exc()
            r = None
        pub = None
        if r:
            sug, info = r
            # sin el proyecto: el que daría la sugerencia no es siempre el del plan (lo preparado, la última vez o el
            # fijo de Ajustes mandan), y lo que se ve tiene que ser lo que se graba (revisión 2, P1)
            pub = {"titulo": info["titulo"], "inicio": info.get("inicio"), "fin": info.get("fin"),
                   "teams": bool(info.get("teams"))}
            s = self.siguiente
            if s and not s.get("dicha"):
                s["dicha"] = True
                try:
                    hace = (datetime.now() - datetime.fromisoformat(info["inicio"])).total_seconds() / 60 \
                        if info.get("inicio") else None
                except (TypeError, ValueError):
                    hace = None
                log(f"   siguiente reunión: propuesta (empezó hace {max(0, int(hace))} min)" if hace is not None
                    else "   siguiente reunión: propuesta (en Teams)")
        self._siguiente_cache = (time.monotonic(), pub)
        return pub

    def rec_siguiente(self, b):
        """POST /api/rec/siguiente: «Ya ha empezado «X»». {grabar: X, origen?: "aviso"} graba esa reunión con su plan
        (plan_grabar, el mismo que la tarjeta de Hoy: con lo preparado, si lo hay), solo si sigue siendo la propuesta
        (lo que se ve es lo que se graba); {descartar: true} es «Ahora no» (hasta la próxima grabación)."""
        if b.get("descartar"):
            if self.siguiente:
                self.siguiente["descartada"] = True
                self._siguiente_cache = (0.0, None)
                # el flotante de la misma propuesta, si sigue a la vista; no si lo pide él mismo (su ✕, origen
                # «aviso»): ya se está cerrando solo, y matarlo cortaba la respuesta que espera (punto 38)
                if getattr(self.vigia, "tipo", None) == "siguiente" and b.get("origen") != "aviso":
                    self.vigia.cerrar_aviso()
                log("   siguiente reunión: «Ahora no»")
            return {"ok": True}
        aviso = b.get("origen") == "aviso"
        with self.toggle_lock:
            if self.rec and self.rec.alive():
                if aviso:  # alguien se adelantó (Ctrl+Alt+R, la tarjeta): no se toca
                    return {"recording": True, "path": rel_of(self.rec.path)}
                raise ApiError("Ya hay una grabación en marcha.")
            st = self.settings.get()
            r = self._siguiente(st)
            quiere = grabar_plan.norm_titulo(str(b.get("grabar") or ""))
            if not r or not quiere or grabar_plan.norm_titulo(r[1]["titulo"]) != quiere:
                self._siguiente_cache = (0.0, None)
                log("   siguiente reunión: ya no está en curso")
                raise ApiError("Esa reunión ya no está en curso: graba desde «Grabar» si sigues en ella.")
            log(f"   siguiente reunión: grabar desde {'el aviso' if aviso else 'la app'}")
            p = self.plan_grabar({}, st=st, sug=r[0])
            if aviso:
                cue("start")  # antes de empezar: así el pitido no queda grabado (como Ctrl+Alt+R)
            try:
                res = self.rec_start(dict(p["rec"], resumen=p["resumen"]))
            except ApiError as e:
                if aviso:
                    cue("error")
                    notify("No se pudo grabar", e.msg)
                raise
            if aviso:
                notify(f"● Grabando en {APP_NAME}", p["resumen"] + " · Ctrl+Alt+R para parar")
            return dict(res, recording=True, plan=p)

    def _siguiente_aviso(self, app, st):
        """El aviso flotante con «Ya ha empezado «X»» (vigilante, cada 8 s): una vez por parada, si está activado
        («Avisarme si estoy en una llamada» y «Aviso flotante») y no hay otro aviso abierto. Esa llamada queda avisada:
        al cerrarse, no sale además el «● Grabar» de la llamada."""
        s = getattr(self, "siguiente", None)
        if (not s or s.get("flotante") or s.get("descartada") or not st.get("meeting_detect", True)
                or st.get("rec_aviso", True) is False or self.vigia.abierto()):
            return False
        pub = self._siguiente_pub()
        if not pub:
            return False
        s["flotante"] = True
        horas = (f"{pub['inicio'][11:16]}–{pub['fin'][11:16]} · " if pub.get("inicio") and pub.get("fin")
                 else "En Teams · ")
        if not self.vigia.abrir_aviso(PORT, APP_DIR, creationflags=engines.NO_WINDOW,
                                      ocultar=st.get("ocultar_al_compartir", True) is not False,
                                      siguiente=pub["titulo"], titulo=f"Ya ha empezado «{pub['titulo']}»",
                                      sub=horas + "Pulsa Grabar para tomar notas"):
            return False
        if app:
            self.vigia.avisado = app
        log("   siguiente reunión: aviso flotante abierto")
        return True

    def _ultima_vez(self, nombre):
        """La última vez que se grabó la reunión que se llama así («La última vez (vie 30 oct): 3 tareas abiertas»): la
        más reciente de la biblioteca con ese título (el de su .md o el del archivo), sin la que se está grabando, con
        las tareas sin hacer de su resumen (None sin resumen)."""
        k = grabar_plan.norm_titulo(nombre)
        if not k:
            return None
        grabando = rel_of(self.rec.path) if getattr(self, "rec", None) and self.rec.alive() else None
        best = None
        for a in list(getattr(getattr(self, "library", None), "flat", None) or []):
            if a.get("recording") or a.get("path") == grabando or not a.get("date"):
                continue
            titulo = (a.get("props") or {}).get("titulo") or a.get("label") or ""
            if grabar_plan.norm_titulo(titulo) == k and (best is None or a["date"] > best["date"]):
                best = a
        if best is None:
            return None
        tareas = None
        if best.get("_sum"):
            try:
                tareas = grabar_plan.tareas_abiertas(read_text(best["_sum"]))
            except OSError:
                tareas = None
        return {"path": best["path"], "fecha": best["date"], "fecha_txt": grabar_plan.fecha_corta(best["date"]),
                "resumen": bool(best.get("_sum")), "tareas": tareas}

    # --- «Mis pendientes» (review del 14/11/2026, punto 28): las tareas sin hacer de todas las reuniones
    def _nombre_usuario(self):
        """El nombre de la persona «Soy yo» de las voces (si están activas), para saber qué tareas son tuyas; si no se
        puede saber, None (quedan las de «Yo» y «Tú»)."""
        try:
            jobs = getattr(self, "jobs", None)
            if jobs is None or not jobs.voices_on():
                return None
            return (jobs.voices.user() or {}).get("nombre") or None
        except Exception:
            log_exc()
            return None

    def pendientes_lista(self, q):
        """GET /api/pendientes: las tareas abiertas («- [ ] …») de los resúmenes de todas las reuniones, de la más
        reciente a la más antigua (y en su orden dentro de cada una): {tareas: [{path, titulo, fecha, fecha_txt, i,
        texto, responsable, tarea, segundo, tuya, sin_audio}], total, tuyas}. Sin la que se está grabando ni los
        resúmenes que OneDrive tiene «solo en línea» (pendientes.Cache no los abre). `i` y `texto` son lo que pide
        POST /api/pendientes/hecha."""
        grabando = rel_of(self.rec.path) if getattr(self, "rec", None) and self.rec.alive() and self.rec.path else None
        yo = self._nombre_usuario()
        lib = getattr(self, "library", None)
        if hasattr(lib, "refresh"):
            lib.refresh(max_age=5)
        out, vivos, sin_leer = [], set(), 0
        for a in list(getattr(lib, "flat", None) or []):
            p = a.get("_sum")
            if not p or a.get("recording") or a.get("path") == grabando:
                continue
            vivos.add(p)
            tareas = pendientes.CACHE.tareas(p)
            if tareas is None and pendientes.solo_en_linea_ruta(p):
                sin_leer += 1  # «solo en línea» de OneDrive: no se abre, pero se dice (revisión 1, P2)
            if not tareas:
                continue
            fecha = a.get("date") or ""
            if not fecha and a.get("mtime"):
                fecha = datetime.fromtimestamp(a["mtime"]).isoformat(timespec="seconds")
            titulo = (a.get("props") or {}).get("titulo") or a.get("label") or ""
            for t in tareas:
                if t["hecha"]:
                    continue
                out.append({"path": a["path"], "titulo": titulo, "fecha": fecha,
                            "fecha_txt": grabar_plan.fecha_corta(fecha), "i": t["i"], "texto": t["texto"],
                            "responsable": t["responsable"], "tarea": t["tarea"], "segundo": t["segundo"],
                            "tuya": pendientes.es_tuya(t["responsable"], yo), "sin_audio": bool(a.get("sin_audio"))})
        pendientes.CACHE.podar(vivos)
        out.sort(key=lambda x: x["fecha"], reverse=True)  # estable: cada reunión, en el orden de su resumen
        return {"tareas": out, "total": len(out), "tuyas": sum(1 for x in out if x["tuya"]), "sin_leer": sin_leer}

    PENDIENTE_MOVER_S = 5  # segundos que «marcar» espera a que acabe de moverse una reunión

    def _resumen_de(self, rel):
        """(ruta interna, .resumen.md) de la reunión `rel`, con su ruta nueva si se movió en esta sesión. El de la
        biblioteca si la reunión está en ella (también la que ya no tiene audio, con otra ruta); si no, el que va junto
        a su audio, y solo para una ruta de audio (no «.», una carpeta ni otro fichero: revisión 1)."""
        abs_path = safe(alias_of(rel))
        rel = rel_of(abs_path)
        a = next((x for x in list(getattr(getattr(self, "library", None), "flat", None) or [])
                  if x.get("path") == rel and x.get("_sum")), None)
        if a:
            return rel, a["_sum"]
        if not rel or os.path.splitext(abs_path)[1].lower() not in AUDIO_EXT:
            raise ApiError("Solo se marcan las tareas del resumen de una reunión.")
        return rel, os.path.splitext(abs_path)[0] + ".resumen.md"

    def pendiente_hecha(self, b):
        """POST /api/pendientes/hecha {path, i, texto, hecha}: la tarea hecha (o sin hacer) en el .resumen.md de esa
        reunión, que es lo que manda (pendientes.marcar_fichero: en la línea i si su texto sigue siendo ese, o donde
        esté ahora; si ya no está, un error que lo dice; solo cambia el carácter de la casilla). Los destinos de salida,
        que llevan el resumen, se reescriben.
        Candados: primero el de los resúmenes (pendientes.LOCK: mientras se escribe uno regenerado, el clic espera sin
        coger el de mover, que frenaría mover, borrar o el directo) y, dentro, el de mover (no escribir en la ruta
        vieja de una reunión que se está moviendo). Nadie coge los dos al revés: _run_summary solo coge el primero."""
        rel, i, texto, hecha = b.get("path"), b.get("i"), b.get("texto"), b.get("hecha")
        if (not isinstance(rel, str) or not rel or type(i) is not int or not isinstance(texto, str)
                or not texto.strip() or not isinstance(hecha, bool)):
            raise ApiError("Petición no válida: faltan path, i, texto o hecha.")
        with pendientes.LOCK:
            # el de mover, con tope: un movimiento largo (una copia a otra unidad) no deja parada aquí, con LOCK cogido,
            # la escritura de los resúmenes de las demás reuniones (revisión 2)
            mover = self._move_lock()
            if not mover.acquire(timeout=self.PENDIENTE_MOVER_S):
                raise ApiError("Se están moviendo reuniones: vuelve a marcarla en un momento.", 409)
            try:
                rel, p = self._resumen_de(rel)
                if not os.path.isfile(p):
                    raise ApiError("Esa reunión ya no tiene resumen.", 404)
                try:
                    linea, cambiado = pendientes.marcar_fichero(p, i, texto, hecha)
                except (pendientes.NoEsta, pendientes.NoSeToca) as e:
                    raise ApiError(str(e), 409)
                except OSError as e:  # de solo lectura o abierto en otro programa (revisión 1, P2)
                    raise ApiError("No se puede escribir en ese resumen: está protegido o abierto en otro programa. "
                                   f"Prueba otra vez en un momento ({e.__class__.__name__}).", 409)
            finally:
                mover.release()
        pendientes.CACHE.olvidar(p)
        if cambiado:
            log(f"   pendientes: tarea {'hecha' if hecha else 'sin hacer'} en {os.path.basename(rel)}")
            lib = getattr(self, "library", None)
            if lib is not None:
                lib.at = 0
            try:
                self.jobs.schedule_outputs(rel)  # el documento de los destinos de salida lleva el resumen
            except Exception:
                log_exc()
        return {"ok": True, "path": rel, "i": linea, "hecha": hecha, "cambiado": cambiado}

    PREPARADA_ANTES, PREPARADA_DESPUES = 10, 5  # minutos: lo preparado vale de 10 min antes a 5 min después

    @staticmethod
    def _nombre_preparada(s):
        """El asunto como lo deja la sugerencia (calendario.nombre_archivo: sin \\ / : * ? " < > | y recortado) y
        normalizado: «Comité: Q4» de Outlook es «Comité Q4» en la reunión de ahora."""
        return grabar_plan.norm_titulo(calendario.nombre_archivo(s))

    @classmethod
    def _clave_preparada(cls, asunto, inicio):
        return f"{inicio}|{cls._nombre_preparada(asunto)}"

    def _preparadas(self, st=None, dict_=False):
        """Lo preparado en settings.json (un fichero tocado a mano no tumba Hoy: lo que no sea un dict, fuera)."""
        # (desde update, con el dict que se está cambiando: settings.get() ahí se quedaría esperando al candado)
        d = (st if st is not None else self.settings.get()).get("preparadas")
        d = {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}
        return d if dict_ else list(d.values())

    def _preparada(self, nombre, st=None, now=None):
        """Lo preparado en Hoy («Preparar» en la agenda: {vocab?, idioma?, contexto?}) para la reunión que se llama así
        y es ahora (de 10 min antes de empezar a 5 min después de acabar), o None."""
        k = self._nombre_preparada(nombre)
        if not k:
            return None
        now = now or datetime.now()
        for p in self._preparadas(st):
            try:
                ini, fin = datetime.fromisoformat(p["inicio"]), datetime.fromisoformat(p["fin"])
            except (KeyError, TypeError, ValueError):
                continue
            if (self._nombre_preparada(p.get("asunto")) == k
                    and ini - timedelta(minutes=self.PREPARADA_ANTES) <= now <= fin + timedelta(
                        minutes=self.PREPARADA_DESPUES)):
                return {x: p[x] for x in ("vocab", "idioma", "contexto") if x in p}
        return None

    def hoy_preparar(self, b):
        """POST /api/hoy/preparar {asunto, inicio, fin, vocab?, idioma?, contexto?} (o quitar: true): «Preparar» una
        cita de la agenda. Lo que se dé (null = lo de siempre) se usa al grabarla, con «Grabar», la tarjeta,
        Ctrl+Alt+R o el aviso (plan_grabar). Se guarda en settings.json; lo de citas que acabaron hace más de un día,
        fuera."""
        asunto = " ".join(str(b.get("asunto") or "").split())[:200]
        try:
            ini = datetime.fromisoformat(str(b.get("inicio") or "")[:16])
            fin = datetime.fromisoformat(str(b.get("fin") or "")[:16])
        except ValueError:
            raise ApiError("Esa cita no tiene una hora válida.")
        if not asunto:
            raise ApiError("Esa cita no tiene asunto: no se puede preparar.")
        entrada = {"asunto": asunto, "inicio": ini.isoformat(timespec="minutes"),
                   "fin": fin.isoformat(timespec="minutes")}
        if not b.get("quitar"):
            if b.get("vocab") is not None:
                vid = str(b["vocab"])
                if vid and not find_vocab(None, None, vid):
                    raise ApiError("Ese proyecto ya no está: elige otro.")
                entrada["vocab"] = vid
            if b.get("idioma") is not None:
                lang = str(b["idioma"]).strip().lower()
                if lang and not IDIOMA_RE.match(lang):
                    raise ApiError("Idioma no válido: " + lang)
                entrada["idioma"] = "" if lang == "auto" else lang
            ctx = " ".join(str(b.get("contexto") or "").split())[:500]
            if ctx:
                entrada["contexto"] = ctx
        guardar = len(entrada) > 3  # sin nada que usar, es quitarla
        clave = self._clave_preparada(asunto, entrada["inicio"])
        limite = (datetime.now() - timedelta(days=1)).isoformat(timespec="minutes")

        def upd(d):
            prep = {k: v for k, v in self._preparadas(d, dict_=True).items()
                    if k != clave and str(v.get("fin") or "") >= limite}
            if guardar:
                prep[clave] = entrada
            d["preparadas"] = dict(sorted(prep.items(), key=lambda kv: kv[1].get("inicio") or "")[-200:])
        self.settings.update(upd)
        log(f"   hoy: reunión {'preparada' if guardar else 'sin preparar'} ({len(entrada) - 3} cosas)")
        return {"ok": True, "preparada": entrada if guardar else None}

    def hoy(self, q):
        """GET /api/hoy[?refrescar=1]: la pantalla Hoy sin esperar a Outlook. La reunión de ahora (título, de dónde
        sale y, si la agenda la tiene, sus horas) con su plan de grabar (plan.py, el mismo que la tarjeta y Ctrl+Alt+R:
        «Grabar» manda plan.rec a /api/rec/start), la última vez que se grabó esa reunión y la agenda del próximo mes,
        leída en segundo plano (_agenda_watch). Si la agenda es vieja (o refrescar=1), se pide otra lectura sin
        esperarla."""
        st = self.settings.get()
        activado = bool(st.get("outlook_on"))
        ag = getattr(self, "agenda", None)
        if ag is not None and activado and (q.get("refrescar") in ("1", "true") or (
                q.get("abrir") in ("1", "true") and ag.toca(calendario.AGENDA_VIEJA))):
            ag.pedir()  # el sondeo de cada 30 s no relee: cada lectura ocupa Outlook (y el candado COM)
        apagado = None
        if not activado:  # sin hablar con Outlook: apagado, Outlook nuevo, sin Outlook o no es Windows
            e = calendario.estado(False)
            apagado = (e["estado"], e["frase"])
        # una sola foto de la agenda para la reunión de ahora y la lista (el hilo puede estar renovándola)
        agenda = ag.publica(activado, apagado) if ag is not None else None
        if agenda is not None:  # por qué no se relee ahora (_agenda_paso): «Actualizar» lo dice (punto 38)
            agenda["ocupado"] = self._agenda_ocupada()
        fresca = ag is not None and ag.fresca()
        sug, info = self._reunion_ahora(st, recordar=False, citas=[dict(c) for c in agenda["citas"]]
                                        if agenda and activado and fresca else [])
        plan = self.plan_grabar({}, st=st, sug=sug)
        nombre = plan["nombre"]["valor"]
        prep = self._preparadas(st, dict_=True)
        for c in (agenda or {}).get("citas") or []:  # lo preparado de cada cita («Preparada · Alfa › General»)
            p = prep.get(self._clave_preparada(c.get("subject"), c.get("start")))
            if p:
                c["preparada"] = {x: p[x] for x in ("vocab", "idioma", "contexto") if x in p}
        return {"ahora": info, "plan": plan, "ultima": self._ultima_vez(nombre) if nombre else None, "agenda": agenda,
                "siguiente": self._siguiente_pub()}

    def _agenda_watch(self):
        """Hilo de la agenda de Outlook (B7): la lee al poco de arrancar, cada AGENDA_CADA s y cuando Hoy la pide."""
        ag = self.agenda
        forzado = ag.despertar.wait(5)  # que el arranque no compita con Outlook
        while True:
            ag.despertar.clear()
            try:
                self._agenda_paso(forzado)
            except Exception:
                log_exc()
            forzado = ag.despertar.wait(60)

    def _agenda_ocupada(self):
        """"grabando", "llamada" o None: lo que hace que _agenda_paso no relea Outlook ahora (lo está usando la
        reunión). La interfaz lo usa para decir «se leerá al colgar» en vez de esperar una lectura que no llega."""
        if getattr(self, "rec", None) and self.rec.alive():
            return "grabando"
        if getattr(self, "meeting", None) or getattr(self, "meeting_prev", None):
            return "llamada"
        return None

    def _agenda_paso(self, forzado=False, entorno=None):
        """Una vuelta del hilo de la agenda. Con el calendario apagado, se olvida lo leído. Mientras se graba o hay una
        llamada no se relee (Outlook es de la reunión y el vigilante lo consulta cada minuto), salvo que aún no haya
        nada. Lo que se sabe sin hablar con Outlook (cerrado, sin pywin32, Outlook nuevo…) se anota sin consultarlo."""
        ag, st = self.agenda, self.settings.get()
        if not st.get("outlook_on"):
            if ag.datos is not None or ag.fallo is not None:
                ag.olvidar()
            return
        if not (forzado or ag.toca()):
            return
        # en llamada: la lectura del registro de privacidad (meeting_prev), también con «Avisarme si estoy en una
        # llamada» apagado, que deja meeting en None
        if self._agenda_ocupada() and ag.datos is not None:
            return
        sesion = getattr(self, "agenda_sesion", None)
        if sesion is not None:  # pruebas de la interfaz (CHULETA_PRUEBAS_OUTLOOK)
            ag.refrescar(session=sesion)
            return
        fallo = calendario.sin_leer(entorno or calendario.entorno_outlook())
        if fallo:
            ag.anotar(fallo)
            return
        ag.refrescar()
