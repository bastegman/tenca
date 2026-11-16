# -*- coding: utf-8 -*-
"""App, la parte de quien habla (voices.py): estado, activar, identificar, personas, unir, olvidar, reasignar y
exportar.

Salio tal cual de server.py al partirlo (15/11/2026): App hereda de esta clase y server.py la reexporta."""
import contextlib
import json
import os
import threading
import time

import carga
import engines
import voces_directo
import voices
from biblioteca import Transcripts
from carpetas import display_rel
from common import ApiError, log, log_exc, save_text
from rutas import DATA_DIR, alias_of, rel_of, safe


MUESTRA_S = 5.0  # «Oír» una voz (B8): unos segundos de ella sola
# Rehacer las huellas es trabajo de fondo: nunca debe notarse (incidencia del 16/11/2026, «se pilló el portátil» al
# actualizar en un portátil modesto). Al arrancar espera un rato y a que el PC lleve un tiempo tranquilo; cada huella va
# en un proceso en reposo (IDLE_PRIORITY_CLASS) con 1 hilo, una a una y con pausa entre ellas, y se para si el PC va
# justo (CPU de todo el PC o memoria libre: carga.py). Medido ese día (Mac, CAM++, reunión de 39 min): una huella son
# ~3 s de CPU y 0,26 GB con 1 hilo; con 4, 2,1 s de pared y 4,5 s de CPU (más CPU para ganar 0,8 s)
REHACER_ESPERA_S = 120.0  # al arrancar, antes de nada: que pase lo pesado del arranque (ventana, modelo del directo)
REHACER_CALMA_S = 60.0  # y, despues, tanto tiempo seguido sin nada de Tenca en marcha y con el PC tranquilo
REHACER_PAUSA_S = 10.0  # entre una huella y la siguiente: a sorbos, nunca seguido
REHACER_HILOS = 1  # hilos de onnxruntime para cada huella (voices.hilos() da 2-4: para quien espera, no para el fondo)
REHACER_CPU_MAX_S = 1200.0  # si la CPU del PC no baja en 20 min, el resto de la tanda ya no la mira (en reposo y con
# 1 hilo cede sola)
# memoria libre: para empezar una huella, 1,5 GB; si en la tanda ya se lleva media hora esperando memoria (un PC que
# vive con poca), basta con 0,9 GB. Mientras calcula, por debajo de 0,6 GB se corta (y se repite en otra vuelta): con
# el mismo umbral para empezar y para cortar, el propio proceso (~0,26 GB) se cortaba a si mismo (revision 1)
REHACER_MIN_LIBRE = int(1.5 * carga.GB)
REHACER_MIN_LIBRE_TRAS = int(0.9 * carga.GB)
REHACER_MEMORIA_MAX_S = 1800.0
REHACER_CORTE_LIBRE = int(0.6 * carga.GB)
_ahora, _dormir = time.monotonic, time.sleep  # (los tests de la espera los cambian por un reloj falso)
REHACER_PASO_S = 15.0  # mientras se graba (o hay un directo, una llamada o se identifican voces): se mira cada tanto
REHACER_DESCARGA_S = 1800.0  # lo que se espera a la descarga de los modelos de voces antes de dejarlo para otra vez


def muestra_de(data, h, largo=MUESTRA_S):
    """{inicio, fin} de unos segundos (como mucho `largo`) en que solo habla h, de los tramos del sidecar: el trozo
    mas largo de h que no pisa a nadie mas, desde su principio. Si todo lo de h se solapa con otra voz, su tramo mas
    largo. None si h no tiene tramos."""
    mios, otros = [], []
    for t in (data or {}).get("tramos") or []:
        try:
            a, b = float(t["inicio"]), float(t["fin"])
        except (KeyError, TypeError, ValueError):
            continue
        if b > a:
            (mios if t.get("h") == h else otros).append((a, b))
    if not mios:
        return None

    def unir(tramos):  # ordenados y sin solapes; los que se tocan, juntos (una frase partida en dos tramos)
        out = []
        for a, b in sorted(tramos):
            if out and a <= out[-1][1] + 0.01:
                out[-1] = (out[-1][0], max(out[-1][1], b))
            else:
                out.append((a, b))
        return out
    mios, otros, libres, j = unir(mios), unir(otros), [], 0  # se recorren una vez (una de 3 h tiene miles)
    for a, b in mios:  # lo de h, menos lo que dice otra voz a la vez
        while j < len(otros) and otros[j][1] <= a:
            j += 1
        k = j
        while k < len(otros) and otros[k][0] < b and a < b:
            c, d = otros[k]
            if c > a:
                libres.append((a, c))
            a, k = max(a, d), k + 1
        if b > a:
            libres.append((a, b))
    # un trozo suelto de menos de 1 s no se entiende: entonces, el tramo mas largo aunque se oiga a otro a la vez
    a, b = max([x for x in libres if x[1] - x[0] >= 1.0] or mios, key=lambda x: x[1] - x[0])
    return {"inicio": round(a, 2), "fin": round(min(b, a + largo), 2)}


class Voces:
    # --- quien habla (voces)
    def voices_state(self, q=None):
        st = self.settings.get()
        on = bool(st.get("voices_enabled"))
        ok, why = voices.available() if on else (None, None)
        dl = dict(self.voices_dl) if self.voices_dl else None
        return {"enabled": on, "available": ok, "why": why,
                "models": {"ready": voices.models_ready(DATA_DIR), "downloading": bool(dl and dl.get("running")),
                           "frac": (dl or {}).get("frac"), "stage": (dl or {}).get("stage"),
                           "error": (dl or {}).get("error")},
                "personas": self.jobs.voices.public() if on else [],
                "usuario": (self.jobs.voices.user() or {}).get("id") if on else None,
                "store_error": self.jobs.voices.error if on else None, "umbral": voices.UMBRAL}

    def _download_models(self):
        def run():
            def report(stage=None, frac=None):
                if stage:
                    self.voices_dl["stage"] = stage
                if frac is not None:
                    self.voices_dl["frac"] = round(float(frac), 3)
            try:
                voices.ensure_models(DATA_DIR, report)
                log("   modelos de voces descargados")
            except Exception as e:
                self.voices_dl["error"] = str(e) or e.__class__.__name__
                log(f"✖ modelos de voces: {self.voices_dl['error']}")
            finally:
                self.voices_dl["running"] = False
        if self.voices_dl and self.voices_dl.get("running"):
            return
        self.voices_dl = {"running": True, "frac": 0.0, "stage": "Descargando modelos de voces…", "error": None}
        threading.Thread(target=run, daemon=True, name="voices-models").start()

    def voices_toggle(self, b):
        on = bool(b.get("on"))
        if on:
            ok, why = voices.available()
            if not ok:  # no se activa: la opcion no serviria de nada en este PC
                raise ApiError("La librería de voces (sherpa-onnx) no está instalada o no carga en este PC: vuelve a "
                               f"ejecutar INSTALAR.bat. Detalle: {why[:200]}")
        self.settings.update(lambda d: d.update(voices_enabled=on))
        if on and not voices.models_ready(DATA_DIR):
            self._download_models()
        if on:  # las huellas de un modelo anterior que hubiera, ya con los modelos (rehacer_huellas espera la descarga)
            threading.Thread(target=self.rehacer_huellas, daemon=True, name="rehacer-huellas").start()
        else:  # lo de los bloques de «Quién habla» mientras se graba (punto 56) lleva huellas de voz: fuera
            voces_directo.purgar(todo=True)
        log(f"🗣 voces {'activadas' if on else 'desactivadas'}")
        return self.voices_state()

    def voices_identify(self, b):
        return {"id": self.jobs.submit_voices(b.get("path"))}

    def speakers_get(self, q):
        abs_path = safe(q.get("path"), "reunion")
        data = self.jobs.speakers_for(abs_path)
        job = self.jobs.active_for(rel_of(abs_path), kinds=("voces",))
        if not data:
            return {"exists": False, "job": job.public() if job else None, "enabled": self.jobs.voices_on()}
        real = [i for i in data["hablantes"].values() if not i.get("breve")]
        return {"exists": True, "enabled": self.jobs.voices_on(), "job": job.public() if job else None,
                # mientras se graba con el directo en la nube (punto 56): identificadas hasta ese segundo
                "parcial": (data.get("parcial") or {}).get("hasta") if isinstance(data.get("parcial"), dict) else None,
                "hablantes": data["hablantes"], "tu": data.get("tu"), "creado": data.get("creado"),
                "frases": data.get("frases", []), "who": data.get("_who", []),
                "n": len(real), "known": sum(1 for i in real if i.get("persona") and not i.get("provisional")),
                "personas": self.jobs.voices.public(), "usuario": (self.jobs.voices.user() or {}).get("id")}

    def speaker_sample(self, q):
        """GET /api/hablantes/muestra?path=&h=: {inicio, fin} para «Oír» esa voz en el bloque «Quién habla» (B8), de
        los tramos de su sidecar (no hace falta el .srt). La interfaz, sin esto, usa la frase mas larga de la
        transcripcion."""
        abs_path = safe(q.get("path"), "reunion")
        data = voices.read_sidecar(voices.sidecar_path(abs_path))
        m = muestra_de(data, q.get("h"))
        if m is None:
            raise ApiError("Esa voz ya no está en esta reunión (¿se volvió a identificar?).", 404)
        return dict(m, creado=(data or {}).get("creado"))

    # --- huellas de un modelo anterior (al actualizar): se rehacen con el de ahora desde el audio de sus reuniones
    def rehacer_huellas(self, espera=0.0, calma=0.0):
        """Las huellas que voices.Voices._migrar dejo por rehacer (las del modelo anterior no se pueden comparar con
        las de ahora) se recalculan desde el audio de las reuniones en que se pulso «Recordar», en este hilo y una a
        una (voices.rehacer_huellas): cada persona conserva id, nombre, «Soy yo» y consentimiento, y vuelve a
        reconocerse por la voz. Nunca mientras se graba, hay un directo o una llamada o se identifican voces: espera
        (_rehacer_esperar). Con «Quién habla» apagado no hace nada (al activarlo, voices_toggle lo vuelve a pedir); sin
        los modelos, primero los baja. Lo que no encuentra o no puede calcular se queda para la proxima vez. La llaman
        main() al arrancar (espera: que no compita con el arranque; calma: y que el PC lleve ese rato tranquilo) y
        voices_toggle; dos a la vez, solo una. Cada huella, en reposo, con 1 hilo y con pausa (REHACER_*)."""
        lock = self.__dict__.setdefault("_rehacer_lock", threading.Lock())
        if not lock.acquire(blocking=False):
            return None
        try:
            # lo ya esperado en esta tanda (CPU que no baja, memoria que no llega): vale para todas sus huellas
            self._rehacer_tanda = {"cpu_desde": None, "cpu_vencida": False, "mem_desde": None}
            if espera:
                time.sleep(espera)
            if calma and self.jobs.voices_on() and self.jobs.voices.pendientes():
                self._rehacer_calma(calma)
            return self._rehacer_huellas()
        except Exception:  # en su hilo: nunca tumba nada; se intenta en el proximo arranque
            log_exc()
            return None
        finally:
            lock.release()

    def rehacer_huellas_al_arrancar(self):
        """main(): rehacer_huellas en su hilo, tras REHACER_ESPERA_S y REHACER_CALMA_S tranquilo."""
        return self.rehacer_huellas(REHACER_ESPERA_S, REHACER_CALMA_S)

    def _rehacer_huellas(self):
        if not self.jobs.voices_on():
            return None
        v = self.jobs.voices  # abrirlo es lo que migra un voces.json de antes
        pend = v.pendientes()
        if not pend:
            return None
        ok, why = voices.available()
        if not ok:
            log(f"   huellas por rehacer: la librería de voces no carga en este PC ({why})")
            return None
        log(f"🗣 {len(pend)} huella{'s' if len(pend) != 1 else ''} de voz por rehacer con el modelo nuevo: se rehacen "
            "en segundo plano")
        cancelled = lambda: bool(getattr(self.jobs, "closing", False))  # noqa: E731 (apagando)
        if not voices.models_ready(DATA_DIR):
            log("   huellas por rehacer: faltan los modelos de voces; se descargan primero")
            self._download_models()  # el de Ajustes › Voces, con su progreso (si ya se estaban bajando, ese)
            fin = time.monotonic() + REHACER_DESCARGA_S
            while (self.voices_dl or {}).get("running") and time.monotonic() < fin and not cancelled():
                time.sleep(2)
            if not voices.models_ready(DATA_DIR):
                motivo = (self.voices_dl or {}).get("error") or "no han terminado de bajar"
                log(f"   huellas por rehacer: sin los modelos de voces ({motivo}); se intentará la próxima vez")
                return None
        # las reuniones por el nombre de su audio (lo que guardaba «Recordar»): la lista de la biblioteca, la que ve la
        # interfaz, una vez; sin los audios de OneDrive que solo estan en linea (leerlos los descargaria) ni los que
        # se estan grabando
        self.library.refresh(max_age=30)
        por_nombre = {}
        for a in list(self.library.flat):
            # (una reunion cuyo audio ya esta en la papelera, punto 17, no sirve: para la huella hace falta oirla)
            if a.get("cloud") or a.get("recording") or not a.get("_spk") or a.get("sin_audio"):
                continue
            por_nombre.setdefault(a.get("name") or os.path.basename(a["path"]), []).append(a["_abs"])
        cortadas = []

        def buscar(nombre):
            # se espera antes de leer el sidecar y elegir el hablante: lo que el usuario corrija mientras (una
            # grabacion puede durar horas) ya se ve (revision 1, P1-1)
            self._rehacer_esperar(cancelled)
            return list(por_nombre.get(nombre) or [])

        def calcular(audio, h, side):
            self._rehacer_pausa(cancelled)  # entre huella y huella, un respiro (tambien antes de la primera)
            self._rehacer_esperar(cancelled)
            if cancelled() or not self.jobs.voices_on():  # apagando, o «Voces» desactivado entretanto: ni una mas
                return None, None
            rel = rel_of(audio)
            # se volvio a identificar, se esta identificando o el usuario ha corregido ese hablante: esta vez no
            if not self._rehacer_mismo(audio, h, side) or self.jobs.active_for(rel, kinds=("voces",)):
                return None, None
            try:  # si empieza una grabacion (o algo de _rehacer_ocupado) o se acaba la memoria mientras se calcula,
                # se corta; en reposo y con 1 hilo: con el PC ocupado, Windows no le da turno
                vec, secs = self.jobs.speaker_vector(rel, h, cancel=lambda: cancelled() or bool(
                    self._rehacer_ocupado()) or carga.poca_memoria(REHACER_CORTE_LIBRE), reposo=True,
                    hilos=REHACER_HILOS)
            except engines.Cancelled:
                cortadas.append(rel)
                raise
            if not self._rehacer_mismo(audio, h, side):  # corregido mientras se calculaba
                return None, None
            return vec, secs

        res = {"rehechas": 0, "sin_audio": 0, "fallidas": 0}
        for _ in range(3):  # lo que se corto por una grabacion, otra vez al terminarla (una vuelta mas, como mucho dos)
            del cortadas[:]
            r = voices.rehacer_huellas(v, buscar, calcular, cancelled)
            res = dict(r, rehechas=res["rehechas"] + r["rehechas"])
            if not cortadas or cancelled():
                break
        log(f"🗣 huellas de voz con el modelo nuevo: {res['rehechas']} rehecha{'s' if res['rehechas'] != 1 else ''}, "
            f"{res['sin_audio']} sin su reunión, {res['fallidas']} por reintentar")
        return res

    @staticmethod
    def _rehacer_mismo(audio, h, side):
        """El sidecar dice de h lo mismo que cuando se eligio: la misma identificacion («creado») y la misma persona,
        puesta igual («origen», provisional). Si no, se ha vuelto a identificar o el usuario lo ha corregido (otra
        persona, nadie, «Son la misma persona»): la huella de h ya no es de esa persona."""
        ahora, side = voices.read_sidecar(voices.sidecar_path(audio)) or {}, side or {}
        uno, otro = ahora.get("hablantes"), side.get("hablantes")  # (un sidecar tocado a mano: no un objeto)
        a = uno.get(h) if isinstance(uno, dict) else None
        b = otro.get(h) if isinstance(otro, dict) else None
        return (ahora.get("creado") == side.get("creado") and isinstance(a, dict) and isinstance(b, dict)
                and all(a.get(k) == b.get(k) for k in ("persona", "origen", "provisional")))

    def _rehacer_ocupado(self):
        """Por que no se rehacen huellas ahora (un codigo) o None: grabando (la grabadora de la app o OBS), con un
        directo, en una llamada o identificando voces (dos procesos de voces a la vez no caben en un portatil)."""
        rec = getattr(self, "rec", None)
        if rec is not None and rec.alive():
            return "grabando"
        with self.jobs.lock:
            vivos = [j for j in self.jobs.list if j.status in ("queued", "running")]
            held = list(getattr(self.jobs, "held", None) or {})
        if any(getattr(j, "live", False) for j in vivos):
            return "directo"
        if any(j.kind == "voces" for j in vivos):
            return "identificando_voces"
        # una transcripcion (Whisper en el PC) y lo que encadena al acabar (las voces aun no estan en cola): tambien
        # ocupan el PC (revision 1, P2-2). Un resumen no (en la nube o el agente del PC: poco), aunque su hilo tambien
        # retenga la reunion (held) mientras corre (revision 2, P2-A)
        resumiendo = {j.path for j in vivos if j.kind == "summary"}
        if any(j.kind != "summary" for j in vivos) or any(r not in resumiendo for r in held):
            return "transcribiendo"
        if any(a.get("recording") for a in list(getattr(self.library, "flat", None) or [])):
            return "grabando"
        if getattr(self, "meeting", None):
            return "llamada"
        return None

    def _rehacer_esperar(self, cancelled):
        """Espera a que no haya nada de _rehacer_ocupado ni el PC vaya justo (_rehacer_pc), o a que se apague la app."""
        self._rehacer_tranquilo(cancelled, 0.0)

    def _rehacer_calma(self, segundos, cancelled=None):
        """Al arrancar: espera a que Tenca y el PC lleven `segundos` seguidos tranquilos («tras un rato sin
        actividad»): lo que acaba de terminar suele encadenar algo (una transcripcion, sus voces, el resumen)."""
        self._rehacer_tranquilo(cancelled or (lambda: bool(getattr(self.jobs, "closing", False))), segundos)

    def _tanda(self):
        t = getattr(self, "_rehacer_tanda", None)
        if t is None:
            t = self._rehacer_tanda = {"cpu_desde": None, "cpu_vencida": False, "mem_desde": None}
        return t

    def _rehacer_tranquilo(self, cancelled, segundos):
        dicho, desde, t = None, None, self._tanda()
        while not cancelled():
            motivo = self._rehacer_ocupado()
            pc = None if motivo else self._rehacer_pc()
            ahora = _ahora()
            if pc == "cpu":  # una CPU que nunca baja (otro programa a tope) no deja la tanda para siempre
                t["cpu_desde"] = ahora if t["cpu_desde"] is None else t["cpu_desde"]
                if t["cpu_vencida"] or ahora - t["cpu_desde"] >= REHACER_CPU_MAX_S:
                    t["cpu_vencida"], pc = True, None
            else:
                t["cpu_desde"] = None
            if pc == "memoria" and t["mem_desde"] is None:  # desde cuando se espera memoria en esta tanda
                t["mem_desde"] = ahora
            if not motivo and not pc:
                desde = ahora if desde is None else desde
                if ahora - desde >= segundos:
                    return
            else:
                desde = None
                if (motivo or pc) != dicho:
                    if motivo:
                        log(f"   huellas por rehacer: se espera a que termine lo que está en marcha ({motivo})")
                    else:
                        log(f"   huellas por rehacer: el PC va justo ({pc}); se espera")
                    dicho = motivo or pc
            _dormir(REHACER_PASO_S)

    def _rehacer_pc(self):
        """"memoria" (poca libre), "cpu" (todo el PC ocupado) o None: carga.ocupado (fuera de Windows, siempre None).
        La memoria que hace falta: REHACER_MIN_LIBRE o, tras REHACER_MEMORIA_MAX_S esperandola en la tanda, menos.
        Con la CPU ya vencida en la tanda, solo la memoria."""
        t = self._tanda()
        libre = REHACER_MIN_LIBRE
        if t["mem_desde"] is not None and _ahora() - t["mem_desde"] >= REHACER_MEMORIA_MAX_S:
            libre = REHACER_MIN_LIBRE_TRAS
        try:
            if t["cpu_vencida"]:
                return "memoria" if carga.poca_memoria(libre) else None
            return carga.ocupado(min_libre=libre)
        except Exception:
            return None

    def _rehacer_pausa(self, cancelled):
        fin = _ahora() + REHACER_PAUSA_S
        while not cancelled() and _ahora() < fin:
            _dormir(min(1.0, max(0.0, fin - _ahora())))

    def _speaker_check(self, abs_path, h, creado=None, data=None):
        """Antes de corregir un hablante: que exista, que no se esten identificando las voces de esa reunion (lo que
        se dijera ahora se perderia) y que la etiqueta se abriera sobre esta identificacion y no sobre una anterior
        (`creado` del sidecar: con otra, «h2» puede ser otra voz y «Recordar» guardaria la huella de otro)."""
        if self.jobs.active_for(rel_of(abs_path), kinds=("voces",)):
            raise ApiError("Se están identificando las voces de esta reunión: espera a que termine para corregirlas.")
        data = data or voices.read_sidecar(voices.sidecar_path(abs_path))
        if not data or h not in (data.get("hablantes") or {}):
            raise ApiError("Ese hablante ya no está en esta reunión (¿se volvió a identificar?).")
        if creado and data.get("creado") and creado != data["creado"]:
            raise ApiError("Las voces de esta reunión se han vuelto a identificar: abre otra vez la etiqueta.")
        return data

    def _not_the_user(self, rel, h, before, target):
        """El usuario dice que el hablante h (que la app tomaba por el: `before`) es otra persona o nadie: se quitan de
        su persona las huellas casi iguales a esa voz (la de otra persona guardada como suya). Si no se puede calcular
        la huella de h, no se toca nada."""
        v = self.jobs.voices
        uid = (v.user() or {}).get("id")
        if not uid or before != uid or target == uid or not (v.find(uid) or {}).get("huellas") \
                or not self.jobs.voices_on():
            return 0
        try:
            vec, _ = self.jobs.speaker_vector(rel, h)
        except Exception:
            return 0
        n = v.drop_similar(uid, vec)
        if n:
            log(f"🗣 {n} huella{'s' if n != 1 else ''} quitada{'s' if n != 1 else ''} de tu persona: era la voz de "
                "otro hablante")
        return n

    def _voices_lock(self):
        """Quien corrige los hablantes de una reunion desde la interfaz coge el candado de las notas: mientras la
        reunion se mueve (B1c, _move_now) espera y, al soltarlo, alias_of ya da su ruta nueva. Sin el, la correccion
        se escribia en la ruta vieja y se perdia (revision de la integracion del 08/11/2026). Y el del sidecar
        (voices.SIDECAR_LOCK): lo identificado por bloques mientras se graba (punto 56) no lo pisa entre leerlo y
        escribirlo (revision 1 de motor-lote2b, P2)."""
        stack = contextlib.ExitStack()
        lock = getattr(self, "notes_lock", None)
        if lock is not None:
            stack.enter_context(lock)
        stack.enter_context(voices.SIDECAR_LOCK)
        return stack

    def _speaker_update(self, abs_path, h, fn, creado=None, then=None):
        """Aplica fn(info, data) al hablante h y guarda el sidecar; con `then`, then(ruta) tambien dentro del candado
        (la respuesta: si se calculara fuera, un movimiento que empieza justo despues la dejaria a medias). Devuelve
        (la ruta de la reunion, la nueva si se acaba de mover; lo que devuelva then)."""
        with self._voices_lock():
            abs_path = safe(alias_of(rel_of(abs_path)), "reunion")
            data = self._speaker_check(abs_path, h, creado)
            fn(data["hablantes"][h], data)
            voices.write_sidecar(voices.sidecar_path(abs_path), data)
            self.library.at = 0
            self.jobs.schedule_outputs(rel_of(abs_path))  # destinos de salida: el documento lleva quien habla
            return abs_path, then(abs_path) if then else None

    def _persona_for_speaker(self, v, b, nombre, consent):
        """Quien es un hablante segun lo que se pide desde su etiqueta: la persona del usuario («Soy yo»; escribir
        su nombre nunca: un compañero puede llamarse igual), una que ya se llama asi, la que tenia con el nombre
        corregido (solo con `renombrar`, y nunca la del usuario) o una nueva. Escribir un nombre distinto NO renombra
        a nadie: dice que es otra persona."""
        when = time.strftime("%Y-%m-%dT%H:%M:%S") if consent else None
        user = v.user()
        if b.get("es_usuario"):  # «Soy yo» no usa el cuadro (puede traer el nombre de otra persona): «Yo», y el
            return user["id"] if user else v.create("Yo", True, when)["id"]  # nombre se cambia en Ajustes
        if not nombre:
            raise ApiError("Escribe un nombre.")
        same = v.by_name(nombre)
        cur = v.find(b.get("id")) if b.get("id") else None
        if b.get("renombrar") and cur and not cur.get("es_usuario") and (not same or same["id"] == cur["id"]):
            if nombre != cur["nombre"]:  # corregir una errata: el nombre nuevo sale en todas sus reuniones
                v.rename(cur["id"], nombre)
                self.jobs.outputs_for_persona(cur["id"])
            return cur["id"]
        if same:
            return same["id"]
        return v.create(nombre, False, when)["id"]

    def voices_person(self, b):
        """Nombre, «Soy yo» y «Recordar» de un hablante (de una reunion: path + h) o de una persona ya guardada
        (Ajustes › Voces: id). Desde un hablante decide QUIEN es ese hablante (_persona_for_speaker); la persona del
        usuario solo cambia de nombre desde Ajustes y solo recibe huellas de un hablante que es el."""
        v = self.jobs.voices
        if b.get("recordar") and not self.jobs.voices_on():
            raise ApiError("Activa «Voces» en Ajustes ⚙ para guardar huellas.")
        pid, path, h = b.get("id"), b.get("path"), b.get("h")
        consent = b.get("consentimiento")
        nombre = " ".join(str(b.get("nombre") or "").split())[:80]
        if pid and not v.find(pid):
            raise ApiError("Esa persona ya no existe.")
        if b.get("recordar"):  # antes de cambiar nada: sin consentimiento no se toca ni la persona ni la reunion
            if not consent:
                raise ApiError("Para guardar la huella hace falta marcar la casilla de consentimiento.")
            if not (path and h):
                raise ApiError("Falta de qué reunión y hablante tomar la huella.")
            # identificada solo en parte (por bloques mientras se grababa, punto 56): «h2» puede ser otra voz cuando
            # se identifique entera; antes de poner el nombre, no después (revisión 2, P3)
            if (voices.read_sidecar(voices.sidecar_path(safe(alias_of(path), "reunion"))) or {}).get("parcial"):
                raise ApiError("Las voces de esta reunión están identificadas solo en parte: ponle nombre sin "
                               "«Recordar» y guarda su huella cuando estén enteras.", 409)
        abs_path = None
        if path and not h:
            safe(path, "reunion")  # como siempre: una reunion que ya no existe se dice
        if path and h:
            with self._voices_lock():  # si la reunion se esta moviendo (B1c), espera y lee la de la ruta nueva
                abs_path = safe(alias_of(path), "reunion")
                side = self._speaker_check(abs_path, h, b.get("creado"))  # antes de crear a nadie
            if b.get("recordar") and side.get("creado") and not b.get("creado"):  # guardar una huella: solo de la
                raise ApiError("Vuelve a abrir la etiqueta para guardar la huella.")  # identificacion que se ve
            before = side["hablantes"][h].get("persona")
            pid = self._persona_for_speaker(v, b, nombre, consent)
            uid = (v.user() or {}).get("id")
            abs_path = self._speaker_update(abs_path, h, lambda info, d: voices.assign(d, h, pid, uid),
                                            b.get("creado"))[0]
            # fuera del candado (puede calcular una huella en un subproceso), con la ruta de ahora
            self._not_the_user(alias_of(rel_of(abs_path)), h, before, pid)
        elif not pid:
            if not nombre and not b.get("es_usuario"):
                raise ApiError("Escribe un nombre.")
            when = time.strftime("%Y-%m-%dT%H:%M:%S") if consent else None
            pid = v.create(nombre or "Yo", bool(b.get("es_usuario")), when)["id"]
        else:  # Ajustes › Voces: nombre y «Soy yo» de una persona por su id
            renamed = False
            if nombre and nombre != v.find(pid)["nombre"]:
                v.rename(pid, nombre)
                renamed = True
            if b.get("es_usuario") is not None:
                v.set_user(pid, bool(b.get("es_usuario")))
                renamed = True
            if renamed:  # destinos de salida: el nombre sale en los documentos de todas sus reuniones
                self.jobs.outputs_for_persona(pid)
        remembered = None
        if b.get("recordar"):
            rel = alias_of(rel_of(abs_path))  # la ruta de ahora: la reunion puede haberse movido entretanto
            vec, secs = self.jobs.speaker_vector(rel, h)
            data = voices.read_sidecar(voices.sidecar_path(safe(rel, "reunion"))) or {}
            secs = secs or (data.get("hablantes", {}).get(h) or {}).get("segundos") or 0
            remembered = v.remember(pid, vec, secs, os.path.basename(abs_path), time.strftime("%Y-%m-%dT%H:%M:%S"))
            log(f"🗣 huella guardada: {v.find(pid)['nombre']} ({remembered} huellas)")
        return {"id": pid, "huellas": remembered, "personas": v.public()}

    def voices_merge(self, b):
        """«Fusionar» dos personas (keep se queda, other desaparece). Primero se reescriben los sidecars que citan a
        other y solo despues se borra: si una reunion no se puede escribir (OneDrive o el antivirus con el fichero
        abierto), other sigue existiendo, se dice cuantas faltan y «Fusionar» se puede repetir (antes la persona se
        borraba primero y esas reuniones se quedaban con un id que ya no existe; revision de la integracion del
        08/11/2026)."""
        v = self.jobs.voices
        keep, other = b.get("id_a"), b.get("id_b")
        a_, b_ = v.find(keep), v.find(other)
        if keep != other and (not a_ or not b_):
            raise ApiError("Alguna de las dos personas ya no existe.")
        if keep != other and (a_.get("es_usuario") or b_.get("es_usuario")):
            raise ApiError("Tu persona no se fusiona con otras: si ese hablante no eres tú, ponle su nombre o pulsa "
                           "«No soy yo»; si eres tú, «Soy yo».")
        n, fallidas, hechas = 0, [], []
        if keep != other:
            self.library.refresh(max_age=5)
            for a in list(self.library.flat):  # sin mirar a["_spk"]: en pleno movimiento el sidecar ya esta en la nueva
                with self._voices_lock():  # como las correcciones: si se esta moviendo, espera y va a la ruta nueva
                    try:
                        abs_path = safe(alias_of(a["path"]), "reunion")
                    except ApiError:
                        continue  # ya no esta
                    side = voices.sidecar_path(abs_path)
                    if not os.path.isfile(side):
                        continue
                    try:  # que no se pueda LEER (OneDrive sin descargar, el antivirus) tambien es una que falta
                        data = json.loads(voices.read_text(side))
                    except OSError as e:
                        fallidas.append(f"{display_rel(rel_of(abs_path))} ({e.__class__.__name__})")
                        continue
                    except ValueError:  # roto: no es de nadie (como read_sidecar)
                        continue
                    if not isinstance(data, dict) or not any(
                            i.get("persona") == other for i in (data.get("hablantes") or {}).values()):
                        continue
                    cambiados = [h for h, i in data["hablantes"].items() if i.get("persona") == other]
                    for h in cambiados:
                        data["hablantes"][h]["persona"] = keep
                    try:  # una reunion con el fichero abierto no deja las demas a medias
                        voices.write_sidecar(side, data)
                    except OSError as e:
                        fallidas.append(f"{display_rel(rel_of(abs_path))} ({e.__class__.__name__})")
                        continue
                    hechas.append((a["path"], cambiados))
                    self.jobs.schedule_outputs(rel_of(abs_path))  # destinos de salida: con el nombre que queda
                n += 1
        self.library.at = 0
        if fallidas:
            log(f"🗣 fusión a medias ({n} reuniones actualizadas); sin actualizar: {', '.join(fallidas)}")
            raise ApiError(f"{len(fallidas)} {'reunión no se ha' if len(fallidas) == 1 else 'reuniones no se han'} "
                           "podido actualizar (¿abiertas en OneDrive o en otro programa?). Las dos personas siguen "
                           "ahí: ciérralas y vuelve a fusionar.")
        try:
            v.merge(keep, other)
        except (KeyError, ValueError) as e:  # cambio entretanto (otra pestaña)
            hay_keep, hay_other = v.find(keep) is not None, v.find(other) is not None
            if not hay_other:  # other ya no esta (olvidada o fusionada con otra): deshacer la dejaria huerfana
                raise ApiError("Alguna de las dos personas ya no existe."
                               + (" Sus reuniones ya muestran a la que se queda." if hay_keep else ""))
            sin_deshacer = self._merge_deshacer(hechas, keep, other)
            aviso = ("Alguna de las dos personas ya no existe." if isinstance(e, KeyError) else
                     "Tu persona no se fusiona con otras: si ese hablante no eres tú, ponle su nombre o pulsa "
                     "«No soy yo»; si eres tú, «Soy yo».")
            if not sin_deshacer:
                aviso += " No se ha cambiado ninguna reunión."
            elif hay_keep:
                aviso += (f" {sin_deshacer} {'reunión se ha' if sin_deshacer == 1 else 'reuniones se han'} quedado con "
                          "la persona elegida (¿abiertas en otro programa?).")
            raise ApiError(aviso)
        log(f"🗣 personas fusionadas ({n} reuniones actualizadas)")
        return {"id": keep, "reuniones": n, "personas": v.public()}

    def _merge_deshacer(self, hechas, keep, other):
        """Vuelve a poner `other` en los hablantes a los que voices_merge acababa de poner `keep` (hechas: [(rel,
        [h…])]); los que ya eran de keep se quedan. Devuelve cuantas reuniones no se han podido deshacer."""
        malas = 0
        for rel, hs in hechas:
            with self._voices_lock():
                try:
                    abs_path = safe(alias_of(rel), "reunion")
                except ApiError:
                    continue  # la reunion ya no esta (borrada entretanto): no hay nada que deshacer
                try:
                    side = voices.sidecar_path(abs_path)
                    data = json.loads(voices.read_text(side))
                    for h in hs:
                        if (data.get("hablantes") or {}).get(h, {}).get("persona") == keep:
                            data["hablantes"][h]["persona"] = other
                    voices.write_sidecar(side, data)
                    self.jobs.schedule_outputs(rel_of(abs_path))  # los documentos, otra vez con el nombre de antes
                except (OSError, ValueError, AttributeError):
                    log_exc()
                    malas += 1
        return malas

    def voices_forget(self, b):
        v = self.jobs.voices
        if b.get("all"):
            v.forget_all()
            log("🗣 todas las huellas olvidadas")
            self.jobs.outputs_for_persona(None)  # destinos de salida: los documentos ya no llevan esos nombres
        else:
            try:
                v.forget(b.get("id"))
            except KeyError:
                raise ApiError("Esa persona ya no existe.")
            log("🗣 persona olvidada")
            self.jobs.outputs_for_persona(b.get("id"))
        self.library.at = 0
        return {"personas": v.public()}

    def voices_reassign(self, b):
        """Este hablante es otra persona ya guardada (o nadie: «No es esta persona» / «No soy yo»)."""
        pid, h = b.get("persona") or None, b.get("h")
        v = self.jobs.voices
        if pid and not v.find(pid):
            raise ApiError("Esa persona ya no existe.")
        uid = (v.user() or {}).get("id")
        with self._voices_lock():  # si la reunion se esta moviendo (B1c), espera y lee la de la ruta nueva
            abs_path = safe(alias_of(b.get("path")), "reunion")
            before = (self._speaker_check(abs_path, h, b.get("creado"))["hablantes"][h]).get("persona")
        abs_path, out = self._speaker_update(abs_path, h, lambda info, d: voices.assign(d, h, pid, uid),
                                             b.get("creado"), then=lambda p: self.speakers_get({"path": rel_of(p)}))
        # fuera del candado (puede calcular una huella en un subproceso), con la ruta de ahora
        self._not_the_user(alias_of(rel_of(abs_path)), h, before, pid)
        return out

    def speakers_join(self, b):
        """«Es el mismo que…»: dos hablantes de esta reunion son la misma voz (la app la partio en dos)."""
        h, con, v = b.get("h"), b.get("con"), self.jobs.voices
        with self._voices_lock():  # dentro: si la reunion se acaba de mover (B1c), su ruta nueva
            abs_path = safe(alias_of(b.get("path")), "reunion")
            data = self._speaker_check(abs_path, h, b.get("creado"))
            if not con or con == h or con not in (data.get("hablantes") or {}):
                raise ApiError("Elige otro hablante de esta reunión.")
            try:
                voices.join_speakers(data, h, con, usuario=(v.user() or {}).get("id"), names=v.names())
            except ValueError as e:
                raise ApiError(str(e))
            voices.write_sidecar(voices.sidecar_path(abs_path), data)
            rel = rel_of(abs_path)
            for k in (h, con):  # la huella en memoria de `con` ya no es la de toda su voz: «Recordar» la recalcula
                (self.jobs.voice_vectors.get(rel) or {}).pop(k, None)
            self.library.at = 0
            self.jobs.schedule_outputs(rel)
            return self.speakers_get({"path": rel})  # dentro: un movimiento que empezara ahora la dejaria a medias

    def voices_export(self, b):
        with self._voices_lock():  # escribe junto al audio: si la reunion se esta moviendo, espera y va a la nueva
            abs_path = safe(alias_of(b.get("path")), "reunion")
            data = self.jobs.speakers_for(abs_path)
            if not data or not data.get("_who"):
                raise ApiError("Esta reunión no tiene hablantes identificados (o no tiene .srt).")
            segs = Transcripts().load(os.path.splitext(abs_path)[0])["segments"]
            out = os.path.splitext(abs_path)[0] + ".hablantes.txt"
            save_text(out, voices.export_text(segs, data["_who"], data, self.jobs.voices.names(),
                                              usuario=data.get("_usuario")), "utf-8-sig")
        self.library.at = 0
        return {"path": rel_of(out), "ext": ".hablantes.txt"}
