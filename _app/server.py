# -*- coding: utf-8 -*-
"""
Tenca: app local para ordenar, escuchar, leer y transcribir las grabaciones de reuniones.

  - Servidor HTTP de la libreria estandar en http://127.0.0.1:8765 (solo accesible desde este PC)
  - Interfaz en _app/static (HTML + JS sin compilar)
  - Transcripcion local (faster-whisper) o en la nube (engines.py)
  - Las transcripciones se guardan como siempre: <audio>.txt y <audio>.srt junto al audio

Arranque: acceso directo del escritorio (pythonw.exe server.py, sin consola; _app/Tenca.bat lo arranca con consola
para ver los mensajes). La interfaz se abre en una ventana propia (window.py, WebView2) o, si no se puede, en Edge.
"""
import atexit  # noqa: F401 (fachada: los tests parchean server.atexit)
import contextlib
import copy
import errno  # noqa: F401 (fachada: los tests usan server.errno)
import hashlib
import json
import logging  # noqa: F401 (fachada: los tests usan server.logging)
import os
import platform  # noqa: F401 (fachada: los tests usan server.platform)
import shutil
import subprocess
import sys
import threading
import time
import webbrowser  # noqa: F401 (fachada: los tests usan server.webbrowser)

import aprender
import calendario
import directo_avisos
import engines
import explicar
import live
import movil
import probar
import voices
from common import (APP_ID, APP_NAME, ApiError, BAD_NAME, LOGGER, SRT_TIME, WINDOWS,  # noqa: F401 (re-exportados)
                    alert, clean_name, default_threads, fmt_srt, fold, is_protected, log, log_exc, natural_key,
                    parse_srt, protect, read_text, save_text, setup_logging, unprotect, wav_duration, write_bytes)
from common import TOKEN_FILE, TOKEN_HEADER  # noqa: F401 (fachada: server.TOKEN_HEADER en los tests)
from recording import (Cortes, GlobalHotkey, RecorderProcess, cue, notify)
from recording import mic_meeting_app  # noqa: F401 (fachada: los tests usan server.mic_meeting_app)
from api import (  # noqa: F401 (fachada: server.X sigue valiendo)
    captura_estado, CSP, DRAIN_MAX, Handler, HOST, ocultar_al_compartir, PORT, Server, slow_request, SLOW_REQUEST_S,
    URL)
from diagnostico import (  # noqa: F401 (fachada: server.X sigue valiendo)
    DIAG_CHECKS, DIAG_CODES, diag_codes, diag_exceptions, DIAG_FREE, _DIAG_HEAD, _DIAG_INNER, diag_line, DIAG_LINES,
    DIAG_MAX, _diag_meeting_app, DIAG_MODELS, _DIAG_RX, _diag_rx, _diag_value, KEY_LIKE, masked, scrub)
from inicio_windows import (  # noqa: F401 (fachada: server.X sigue valiendo)
    inicio_windows_al_dia, inicio_windows_leer, inicio_windows_poner, _lnk_apunta_a, _lnk_cadenas, shell_folder,
    _startup_antiguos, STARTUP_APPROVED, _startup_aprobado_copiar, _startup_lnks, _startup_quitar)
from rutas import (  # noqa: F401 (fachada: server.X sigue valiendo)
    alias_of, ALIASES, ALIASES_LOCK, APP_DIR, AUDIO_EXT, BUILD, CLOUD_ONLY, code_mtime, DATA_DIR, in_app_dir,
    in_app_tree, _inside, is_link_root, LINK, link_id, LINKS, MIME, read_version, rel_of, ROOT, root_name, safe,
    STATIC_DIR, TEXT_EXT, VERSION, VERSION_FILE, version_file, VLINK, vocab_link_path, VOCAB_LINKS)
from ajustes import (  # noqa: F401 (fachada: server.X sigue valiendo)
    agente_de, aprendido_de, DEFAULT_SETTINGS, _idiomas_moved, MEETING_LIKE_RE, migrate_app_name, migrate_main_folders,
    moved_rel, NUBE_OFF, procesado_de, PROCESADOS, proveedor_elegido, PRUEBA_VOZ_DB, reparar_proveedor, settings_moved,
    Store, _una_reunion_de_ese_dia)
from instancia import (  # noqa: F401 (fachada: server.X sigue valiendo)
    allow_foreground, already_running, ask_running_to_show, fatal, _holder_is_port_owner, _INSTANCE, INSTANCE_FILE,
    instance_holder, instance_lock, INSTANCE_STUCK, INSTANCE_WAIT, _is_python_process, _local_request, _LOCK_BYTE,
    _lock_once, open_ui, _port_owner_pid, PORT_WAIT, probe_running, release_instance, remove_token, reserve_port,
    retire_other_version, show_running, start_session, _UI_LOCK, _UI_OPENED, UI_REOPEN_GAP, ui_url, wait_instance,
    _wait_port_free, write_token)
from apagado import (  # noqa: F401 (fachada: server.X sigue valiendo)
    ask_yes_no, make_tray, schedule_shutdown, shutdown, tell, tray_abrir, tray_balloon, tray_estado, tray_grabar,
    tray_salir)
from biblioteca import (  # noqa: F401 (fachada: server.X sigue valiendo)
    apply_edits, _CARPETAS, _CARPETAS_LOCK, carpetas_reunion, CarpetasReunion, clean_icon, CLOUD_TEXT_LOCK, CloudText,
    COMPLETE, Complete, DATE_RE, dump_notes, Durations, ICON_BAD, ICON_MAX, IDIOMA_RE, is_partial, Library, LINK_DEPTH,
    Movimientos, NOTE_KEY_RE, NOTE_KEYS, parse_notes, participantes_confirmados, participantes_del_md,
    PARTICIPANTES_NOMBRE, PARTICIPANTES_NOTA, _PARTICIPANTES_NOTA_RE, PARTICIPANTES_ORIGENES, public_transcript, scan,
    seg_key, SIDE_EXTS, srt_block, strip_private, Temas, _TEMAS, temas, _TEMAS_LOCK, TEXTOS_SUGERIDOS, _TRAD,
    _TRAD_LOCK, traduccion_destino, traduccion_destinos, TraduccionDestino, Transcripts, TX_SAVE_LOCK, VOCAB_FOLDER,
    with_theme_id, write_outputs)
from listas import (  # noqa: F401 (fachada: server.X sigue valiendo)
    AJUSTES_CAMBIADOS, ajustes_previstos, check_perfil, check_vocab_link, _closest, _con_o_sin, _decode_list,
    find_vocab, has_vocab_dir, idioma_de, idioma_repaso, import_vocab, _is_app_name, _list_vocab, list_vocab,
    LIVE_PRESETS, md_props, meeting_setup, _norm_dir, _of_project, _one_per_line, perfil_de, perfil_folder, PERFIL_KEYS,
    perfil_vocab, perfil_vocab_fijo, _project_dir, _project_known, read_vocab, resolve_perfil, root_names, SIN_AUDIO,
    theme_vocab, update_perfil, vocab_fit, vocab_for, vocab_info, vocab_link_name, _vocab_link_ok, vocab_links_estado,
    VOCAB_MAX_BYTES)
from carpetas import (  # noqa: F401 (fachada: server.X sigue valiendo)
    backup_dir, copia_original, display_rel, _en_carpeta_de_listas, fecha_subida, fit_meeting_dir, FORMATOS_SUBIR,
    free_dir, limpiar_subidas, MEETING_DIR_MAX, meeting_dir_name, meeting_dir_preview, MEETING_DIR_RE, move_files,
    move_one, own_meeting_dir, por_reunion, project_folders_resolved, project_home, rec_destino, rec_folder_origin,
    ruta_txt, save_original, SUBIENDO, SUBIENDO_VIEJO_S, SUBIR_FECHA_MIN, theme_moved, to_recycle_bin, _usable_dir,
    WIN_PATH_MAX)
from aprendido import (  # noqa: F401 (fachada: server.X sigue valiendo)
    anadir_a_lista, _aplicar_aprendido, aplicar_aprendido, APRENDIDO_FILE, aprendido_publico, deshacer_aprendido,
    _escribir_bloques, guardar_registro, lista_de_reunion, mismo_proyecto, motivo_lista, _nombre_lista, original_de,
    _por_inicio, proyecto_de_reunion, registro_aprendido, _utf16)
from trabajos import (  # noqa: F401 (fachada: server.X sigue valiendo)
    borrar_pcm_voces, Job, Jobs, keepalive_s, live_cloud_cfg, live_writer, LiveOutputs, PendingPasses, preload_model,
    _SALIDAS_LOCK, TEXT_GEN)
from paquete import (  # noqa: F401 (fachada: server.X sigue valiendo)
    build_package, de_un_nombre_anterior, desktop_dir, DONAR_URL, export_code, EXPORT_DIR, license_name, open_dir,
    package_version, PKG_ROOT, REPO_URL, TENCA_URL)
from app_grabar import (  # noqa: F401 (fachada: server.X sigue valiendo)
    Grabar, REC_ARRANQUE_S, teams_estado)
from app_reuniones import (  # noqa: F401 (fachada: server.X sigue valiendo)
    Reuniones)
from app_listas import (  # noqa: F401 (fachada: server.X sigue valiendo)
    Listas)
from app_carpetas import (  # noqa: F401 (fachada: server.X sigue valiendo)
    Carpetas)
from app_hoy import (  # noqa: F401 (fachada: server.X sigue valiendo)
    Hoy)
from window import Window, set_app_id


SERVER, WINDOW = None, None  # los pone main(): el servidor HTTP y la ventana propia (None si se abre en Edge)

# ----------------------------------------------------------------------------- server.py como fachada
# server.py se partio en modulos el 15/11/2026 (punto 23 del review). Regla desde entonces: nada nuevo aqui; cada cosa
# en su modulo (y el modulo, en PARTES). server.py reexporta lo que salio (server.X sigue valiendo) y mantiene iguales
# las copias de cada nombre:
# - lo que main() fija al arrancar (COMPARTIDAS): main() asigna con `global` y despues llama a _compartir();
# - solo cuando server se importa como modulo (los tests, scripts/rendimiento_bench.py), no al ejecutarlo como la app:
#   una asignacion desde fuera (`server.PORT = …`, mock.patch.object(server, "f", …)) llega tambien a los modulos de
#   PARTES que tenian el mismo objeto (_Fachada), como cuando todo ese codigo estaba aqui. Un mismo nombre en server y
#   en un modulo de PARTES es siempre el mismo objeto (test_servidor_partido lo comprueba).
# los modulos que salieron de server.py
PARTES = ("ajustes", "apagado", "api", "app_carpetas", "app_grabar", "app_hoy",
          "app_listas", "app_reuniones", "aprendido", "biblioteca", "carpetas", "diagnostico",
          "inicio_windows", "instancia", "listas", "paquete", "rutas", "trabajos")
COMPARTIDAS = ("PORT", "URL", "DATA_DIR", "SERVER", "WINDOW", "COMPLETE")  # COMPLETE: lo crea App()
# no APP_DIR (los tests lo cambian); con realpath: ejecutado como script, __file__ es la ruta tal cual y el de
# los modulos importados, la de sys.path[0], que en macOS y Linux viene sin enlaces (/var -> /private/var)
_AQUI = os.path.normcase(os.path.dirname(os.path.realpath(__file__)))


def _partes():
    """Los modulos de PARTES ya importados (los de esta carpeta: nunca otro del mismo nombre)."""
    out = []
    for n in PARTES:
        m = sys.modules.get(n)
        f = getattr(m, "__file__", None)
        if m is not None and f and os.path.normcase(os.path.dirname(os.path.realpath(f))) == _AQUI:
            out.append(m)
    return out


def _compartir(*nombres):
    """Copia a los modulos de PARTES que los tienen los valores de server.py de esos nombres."""
    for m in _partes():
        for k in nombres:
            if k in vars(m):
                setattr(m, k, globals()[k])


class _Fachada(type(sys)):
    """server importado como modulo: setattr(server, k, v) pone v tambien en los modulos de PARTES que tenian el mismo
    objeto que server (o siempre, si k es de COMPARTIDAS). Un nombre que server no tenia (mock.patch.object con
    create=True) no llega a ninguno, y por eso su delattr tampoco tiene nada que deshacer."""

    def __setattr__(self, k, v):
        antes = vars(self).get(k, _Fachada)  # _Fachada: «no estaba» (ningun modulo lo tiene)
        super().__setattr__(k, v)
        for m in _partes():
            if k in vars(m) and (vars(m)[k] is antes or k in COMPARTIDAS):
                setattr(m, k, v)


if __name__ != "__main__":  # la app (python server.py) no lo necesita: nadie le asigna nada desde fuera
    sys.modules[__name__].__class__ = _Fachada


# ----------------------------------------------------------------------------- utilidades


# ----------------------------------------------------------------------------- paquete para otra persona


# ----------------------------------------------------------------------------- estado persistente


# ----------------------------------------------------------------------------- arbol de carpetas


# ----------------------------------------------------------------------------- transcripciones


# ----------------------------------------------------------------------------- notas (estilo Notion)


# ----------------------------------------------------------------------------- vocabulario por workstream


# ----------------------------------------------------------------------------- perfil de reunion


# ----------------------------------------------------------------------------- carpeta principal y carpeta por reunion


# ----------------------------------------------------------------------------- carpetas de listas conectadas


# ----------------------------------------------------------------------------- importar listas de vocabulario


# ----------------------------------------------------------------------------- aprender de tus correcciones


# ----------------------------------------------------------------------------- cola de trabajos


# ----------------------------------------------------------------------------- API


class App(Grabar, Reuniones, Listas, Carpetas, Hoy):
    rec_body = rec_cadena = None  # con que se grabo lo ultimo: la continuacion de una grabacion cortada lo repite

    @property
    def cortes(self):
        """Grabaciones cortadas o recuperadas y la lista de las abiertas (recording.Cortes, en DATA_DIR)."""
        c = self.__dict__.get("_cortes")
        if c is None:
            c = self.__dict__["_cortes"] = Cortes(DATA_DIR)
        return c

    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        global COMPLETE
        COMPLETE = Complete(os.path.join(DATA_DIR, "completas.json"))
        _compartir("COMPLETE")
        self.settings = Store(os.path.join(DATA_DIR, "settings.json"), DEFAULT_SETTINGS)
        if self.settings.get().get("procesado") not in PROCESADOS:  # ajustes de antes: se decide una vez y se guarda
            self.settings.update(lambda d: d.update(procesado=procesado_de(d)))
        self._reparar_proveedor()
        self._aviso_resumen_local()
        self._load_links()
        if not self.settings.get().get("carpetas_v"):  # la carpeta por reunion (07/11/2026): una vez
            cambios = []
            self.settings.update(lambda d: cambios.extend(migrate_main_folders(d)))
            for k, old, new in cambios:
                log(f"   carpeta principal de {k}: era la de una reunión ({old}); pasa a {new or 'la raíz'}")
        self._migrar_nombre_raiz()
        self._load_vocab_links()
        self.library = Library(Durations(os.path.join(DATA_DIR, "durations.json")))
        self.transcripts = Transcripts()
        self.jobs = Jobs(self.settings, self.library)
        # cambiar la carpeta en pleno directo (msg 1198): se mueve cuando la reunion queda libre (al acabar su ultimo
        # trabajo o en la vuelta de 3 s, que tambien hace los que quedaron de antes de un cierre)
        self.jobs.on_idle, self.jobs.on_tick = self._al_quedar_libre, self._tick_movimientos
        # grabadora propia: una grabacion y, mientras la ventana de "Grabar" esta abierta, un medidor
        self.rec, self.monitor, self.monitor_seen = None, None, 0.0
        self.rec_lock, self.devices_cache = threading.Lock(), (0.0, None)
        # grabaciones cortadas (la grabadora termino sola) y las que quedaron abiertas: avisos, continuacion y reparar
        # al arrancar (punto 1 del review del 14/11/2026). rec_body/rec_cadena: con que se grabo, para la continuacion
        self._cortes = Cortes(DATA_DIR)
        self.toggle_lock = threading.Lock()
        self.notes_lock, self.meeting, self.meeting_prev = threading.Lock(), None, None
        self.voices_dl = None  # descarga de los modelos de voces en curso (progreso para la interfaz)
        self.vigia = calendario.Vigia()  # reunión en curso (Teams/Outlook): sugerencias y aviso flotante
        # «La llamada ha terminado: paro en 30 s · No terminar» (B6); parada: la última parada por eso (la interfaz
        # la avisa al ver que ya no se graba)
        self.fin_llamada, self.parada_auto = directo_avisos.FinDeLlamada(), None
        # «Ya ha empezado «X» · Grabar · Ahora no» al parar una grabación (msg 1479): lo que se grababa (para no
        # proponerla otra vez) y hasta cuándo se mira (_siguiente)
        self.grabada, self.siguiente, self._siguiente_cache = [], None, (0.0, None)
        # lo que ya se sabe de los asistentes de Outlook (si pregunta, no se le vuelve a preguntar solo al arrancar)
        calendario.ASISTENTES["estado"] = self.settings.get().get("outlook_asistentes") or None
        calendario.al_cambiar = lambda v: self.settings.update(lambda d: d.update(outlook_asistentes=v))
        # la agenda de Outlook de un mes para Hoy (B7), en segundo plano y en memoria; con CHULETA_PRUEBAS_OUTLOOK
        # (pruebas de la interfaz) sale de un fichero de citas en vez de Outlook
        self.agenda = calendario.Agenda()
        prueba = os.environ.get("CHULETA_PRUEBAS_OUTLOOK")
        self.agenda_sesion = calendario.sesion_de_prueba(prueba) if prueba else None
        threading.Thread(target=self._agenda_watch, daemon=True, name="agenda-outlook").start()
        threading.Thread(target=self._meeting_watch, daemon=True, name="meeting-watch").start()
        threading.Thread(target=self._monitor_watchdog, daemon=True).start()
        self.hotkey = GlobalHotkey(lambda: self.rec_toggle({"source": "hotkey"}))
        self.hotkey.start()
        # las grabaciones propias que quedaron abiertas (Tenca se cerro de golpe): se cierran y se dice
        threading.Thread(target=self._recuperar_grabaciones, daemon=True, name="recuperar-grabaciones").start()

    def rec_toggle(self, b):
        """Ctrl+Alt+R (o el mismo atajo dentro de la app): empieza con la ultima configuracion o para."""
        with self.toggle_lock:
            try:
                if self.rec and self.rec.alive():
                    if b.get("start_only"):  # el aviso flotante: si Ctrl+Alt+R se adelantó, no se para la grabación
                        return {"recording": True, "path": rel_of(self.rec.path)}
                    r = self.rec_stop({})
                    seg = r["seconds"]  # en segundos si no llego al minuto: «1 min» tapaba una grabacion de 5 s
                    if not r.get("aviso"):  # a medias ya lo ha dicho rec_stop (con su sonido de error)
                        cue("stop")
                        notify("■ Grabación guardada",
                               f"{f'{int(seg)} s' if seg < 60 else f'{round(seg / 60)} min'} · "
                               "la transcripción termina sola")
                    return dict(r, recording=False)
                st = self.settings.get()
                # el mismo plan que enseña la tarjeta de Grabar (plan.py): la reunión en curso (Teams/Outlook, sin
                # esperar a COM: calendario.py) o la que vio el aviso flotante al abrirse, y de ahí proyecto,
                # carpeta, idioma, vocabulario, motor y dispositivos, cada uno de su capa
                sug = self._reunion_ahora(st)[0]
                if b.get("name") or b.get("vocab"):
                    sug = dict(sug, name=b.get("name") or sug.get("name") or "",
                               vocab=b.get("vocab") or sug.get("vocab") or "")
                p = self.plan_grabar({}, st=st, sug=sug)
                body = dict(p["rec"], resumen=p["resumen"])
                if b.get("participantes"):
                    body.update(participantes=b["participantes"], sugerido=b.get("sugerido") or sug.get("source") or "")
                cue("start")  # antes de empezar: asi el pitido no queda grabado
                r = self.rec_start(body)
                # lo que avisa el plan (la carpeta, un proyecto sin carpeta propia, la API key), también aquí y
                # delante: el aviso de Windows corta lo que no cabe
                del_plan = "".join(f"{a} · " for a in dict.fromkeys(x["aviso"] for x in p["lineas"] if x.get("aviso")))
                # con proyecto, el resumen dice el proyecto y no la carpeta: la de la reunión, también (sin tarjeta,
                # es lo único que dice dónde se guarda; revisión de la fusión de ux-grabar), escrita como en la
                # tarjeta y al final: repite el nombre de la reunión y, si el aviso no cabe, es lo que se corta
                donde = (f" · se guarda en {ruta_txt(r['carpeta'])}"
                         if p["proyecto"].get("vocab") and r.get("carpeta") is not None else "")
                notify(f"● Grabando en {APP_NAME}", del_plan + p["resumen"] + " · Ctrl+Alt+R para parar"
                       + (f" · {r['aviso']}" if r.get("aviso") else "") + donde)
                return dict(r, recording=True, plan=p)
            except ApiError as e:
                cue("error")
                log(f"   no se pudo grabar (Ctrl+Alt+R): {e.msg}")  # el toast se va; el log se queda
                notify("No se pudo grabar", e.msg)
                raise

    def _reparar_proveedor(self):
        """Al arrancar: reparar_proveedor (fix-groq). Se mira sobre una copia y solo se escribe si hay algo que
        reparar. Devuelve la frase del log o None."""
        if not reparar_proveedor(self.settings.get()):
            return None
        hecho = []
        self.settings.update(lambda d: hecho.append(reparar_proveedor(d)))
        if hecho and hecho[0]:
            log(f"   proveedor de la nube: {hecho[0]}")
            return hecho[0]
        return None

    def _migrar_nombre_raiz(self):
        """Al arrancar, despues de cargar las vinculadas: los ajustes del proyecto de la raiz pasan al nombre de su
        carpeta (migrate_app_name), una vez por nombre. Devuelve [(clave, nombre anterior)]."""
        renombrados, raiz = [], root_name()
        if self.settings.get().get("nombre_v") != raiz:
            self.settings.update(lambda d: renombrados.extend(migrate_app_name(d, raiz)))
        for k, viejo in renombrados:
            log(f"   {k}: el proyecto de la raíz se llamaba «{viejo}»; pasa a «{raiz}», el nombre de su carpeta")
        return renombrados

    # --- «Subir audio › Desde el móvil» (movil.py): un código QR que el móvil abre por la wifi
    _movil_lock = threading.Lock()
    _movil_rec = None
    _movil_ultima = None  # el último código cerrado, para que la tarjeta siga enseñando lo que recibió
    _movil_red = (0.0, None)  # la última red buena y cuándo: «Mostrar el código» no vuelve a lanzar PowerShell

    def _movil_datos(self, b):
        """Lo elegido en el PC para lo que mande el móvil (lo que da /api/grabar/plan con subir, como en /api/subir):
        la carpeta se comprueba ya, para no enseñar un código que luego no puede guardar nada."""
        folder = safe(b.get("folder", ""), "dir")
        if in_app_dir(folder):
            raise ApiError("No se puede guardar ahí")
        return {k: str(b.get(k) or "") for k in ("folder", "vocab", "language", "contexto", "preset")} | {
            "folder": rel_of(folder)}

    def _movil_parar(self):
        with self._movil_lock:
            rec, App._movil_rec = App._movil_rec, None
            if rec:
                App._movil_ultima = rec  # lo recibido se sigue viendo en la tarjeta (y lo que llegaba, al terminar)
        if rec:
            rec.parar()

    def movil_red(self, q):
        """GET /api/movil/red: si se puede recibir del móvil en la red de ahora ({ok, ip, red} o {ok: False, motivo,
        texto}). Mira los perfiles de red de Windows (PowerShell, ~1 s)."""
        red = movil.red_para_movil()
        App._movil_red = (time.time(), red) if red["ok"] else (0.0, None)  # una mala tira la buena de antes
        return red if red["ok"] else dict(red, texto=movil.motivo_texto(red))

    def movil_abrir(self, b):
        """POST /api/movil/abrir {folder, vocab, language, contexto, preset}: un código nuevo (el anterior deja de
        valer). Solo en una red privada de Windows (decisión 3 de la lámina, msg 1390)."""
        datos = self._movil_datos(b)
        cuando, red = App._movil_red
        if not red or time.time() - cuando > 120:  # la de hace un momento (al abrir la tarjeta) vale; una mala, no
            red = movil.red_para_movil()
        if not red["ok"]:
            return dict(red, texto=movil.motivo_texto(red))
        try:
            rec = movil.Recepcion(red["ip"], datos, self.subir, STATIC_DIR, AUDIO_EXT)
        except OSError as e:
            raise ApiError(f"No se puede recibir en la wifi ({e.strerror or e.__class__.__name__}). Vuelve a probar en "
                           "un momento.")
        with self._movil_lock:  # el cambio, de una vez: dos «Mostrar el código» a la vez no dejan uno suelto
            viejo, App._movil_rec, App._movil_ultima = App._movil_rec, rec, None
            rec.empezar()
        if viejo:
            viejo.parar()
        return dict(rec.publico(), ok=True, red=red.get("red") or "", qr=movil.qr_svg(rec.url))

    def movil_datos(self, b):
        """POST /api/movil/datos {…}: «Cambiar» con el código ya en pantalla: lo que llegue desde ahora va con esto."""
        datos = self._movil_datos(b)
        rec = App._movil_rec
        if not rec or not rec.vigente():
            raise ApiError("El código ya no vale: pide uno nuevo.", 410)
        rec.datos = datos
        return rec.publico()

    def movil_estado(self, q):
        """GET /api/movil/estado: el código abierto y lo que va llegando, para el PC."""
        rec = App._movil_rec or App._movil_ultima
        return rec.publico() if rec else {"activa": False, "recibidos": []}

    def movil_cerrar(self, b):
        """POST /api/movil/cerrar {url?}: «Dejar de recibir», «Listo» o cerrar la ventana. Lo que estaba llegando se
        guarda. Con `url`, solo si es ese código (uno que se abría al cerrar la tarjeta no cierra el de otra)."""
        rec = App._movil_rec
        estado = rec.publico() if rec else {"activa": False, "recibidos": []}
        if b.get("url") and (not rec or rec.url != b.get("url")):
            return dict(estado, cerrado=False)
        self._movil_parar()
        return dict(estado, activa=False)

    # --- edicion de transcripciones
    _tx_save_lock = TX_SAVE_LOCK  # biblioteca: el mismo que usa aprendido.aplicar_aprendido

    def save_transcript(self, b):
        """Guarda lo corregido a mano. La primera vez copia lo que escribio Whisper (para poder volver). De uno en uno:
        al cerrar la pagina el mismo texto llega dos veces a la vez (keepalive y el fetch normal), y el segundo podia
        escribir lo corregido antes de que el primero copiara el original."""
        with self._tx_save_lock:  # dentro: si la reunion se acaba de mover, su ruta nueva (_move_now lo coge)
            return self._save_transcript(dict(b, path=alias_of(b.get("path"))))

    def _save_transcript(self, b):
        abs_path = safe(b.get("path"), "file")
        rel, base = rel_of(abs_path), os.path.splitext(abs_path)[0]
        segs = []
        for s in b.get("segments") or []:
            text = " ".join(str(s.get("text") or "").split())
            if text:
                segs.append((s.get("start"), s.get("end"), text))
        job = self.jobs.active_for(rel)
        if job and job.live:
            gen = b.get("gen")
            return self._save_live_edits(job, segs, base, gen if isinstance(gen, int) and not isinstance(gen, bool)
                                         else None)
        if job:
            raise ApiError("Se está haciendo el repaso final (1–2 min por hora de reunión); "
                           "podrás editar en cuanto termine."
                           if job.final_pass else "Se está transcribiendo; podrás editar cuando termine.")
        if not segs:
            raise ApiError("La transcripción no puede quedar vacía.")
        copia_original(rel, base)
        if os.path.isfile(base + ".srt") and all(a is not None and e is not None for a, e, _ in segs):
            write_outputs(abs_path, [(float(a), float(e), t) for a, e, t in segs])
        else:  # transcripcion solo en .txt (sin tiempos)
            save_text(base + ".txt", "\n".join(t for _, _, t in segs) + "\n", "utf-8-sig")
        self.library.at = 0
        self.jobs.schedule_outputs(rel)  # destinos de salida: el documento con la transcripcion corregida
        mt = max((os.path.getmtime(base + e) for e in (".txt", ".srt") if os.path.isfile(base + e)), default=0)
        return {"ok": True, "tx_mtime": mt, "count": len(segs)}

    def _save_live_edits(self, job, segs, base, gen=None):
        """Corregir mientras se transcribe en directo: se guardan las diferencias con lo que dio Whisper
        y se aplican al escribir cada trozo nuevo, asi el directo no pisa lo corregido. gen: la generación del texto
        que tenía el editor (/api/transcript → text_gen). Si desde entonces el directo ha rehecho texto ya escrito
        (cambio de idioma), lo que va desde ahí (rehecho_desde) no se toca: ni se corrige con el texto de antes ni se
        da por borrado (antes salía como «frase borrada» y se perdía lo rehecho)."""
        if not job.out:
            raise ApiError("El directo aún está empezando; prueba en unos segundos.")
        sent = {seg_key(a): t for a, _, t in segs if a is not None}
        last = max(sent) if sent else -1.0  # lo que llego despues aun no lo veias: no se toca
        with job.edit_lock:
            corte = None
            if gen is not None and gen != getattr(job, "text_gen", 0):
                # lo rehecho despues de la version del editor; ninguno (era la de otro directo de la reunion, que
                # empezo con el texto del archivo): como siempre
                corte = min((t for g, t in getattr(job, "rewrites", []) if g > gen), default=None)
            for a, _, t in job.raw:
                k = seg_key(a)
                if corte is not None and a >= corte - 1e-6:
                    continue  # rehecho despues de que el editor cargara su texto
                if k in sent:
                    if sent[k] != " ".join(t.split()):
                        job.edits[k] = sent[k]
                    else:
                        job.edits.pop(k, None)
                elif k <= last:
                    job.edits[k] = ""  # la dejaste vacia: frase borrada
            job.out.write(apply_edits(job.raw, job.edits))
            gen_ahora = getattr(job, "text_gen", 0)
        self.library.at = 0
        mt = max((os.path.getmtime(base + e) for e in (".txt", ".srt") if os.path.isfile(base + e)), default=0)
        return {"ok": True, "tx_mtime": mt, "count": len(segs), "live": True, "text_gen": gen_ahora,
                "rehecho_desde": corte}

    def restore_transcript(self, b):
        """«Restaurar el original». Como save_transcript, con el candado que coge _move_now: si la reunion se esta
        moviendo (B1c), espera y restaura en la ruta nueva; si no, dejaba el original en la carpeta vieja, borraba la
        copia y respondia «ok» (revision de la integracion del 08/11/2026)."""
        with self._tx_save_lock:
            return self._restore_transcript(dict(b, path=alias_of(b.get("path"))))

    def _restore_transcript(self, b):
        abs_path = safe(b.get("path"), "file")
        rel, base = rel_of(abs_path), os.path.splitext(abs_path)[0]
        bdir = backup_dir(rel)
        if not os.path.isdir(bdir):
            raise ApiError("No hay copia del original de esta transcripción.")
        if self.jobs.active_for(rel):
            raise ApiError("Se está transcribiendo; espera a que termine.")
        for ext in (".srt", ".txt"):
            src = os.path.join(bdir, "original" + ext)
            if os.path.isfile(src):
                with open(src, "rb") as f:
                    write_bytes(base + ext, f.read())
        shutil.rmtree(bdir, ignore_errors=True)
        self.library.at = 0
        self.jobs.schedule_outputs(rel)  # destinos de salida: el documento vuelve a la transcripcion original
        log(f"↺ {rel}: transcripción original restaurada")
        return {"ok": True}

    # --- aprender de tus correcciones (aprender.py)
    def _propuestas(self, abs_path):
        """Lo que se puede aprender de lo que corregiste en esta reunion (aprender.propuestas), con las listas donde ya
        esta cada termino, la lista de la reunion (la que recibe Whisper; "" = el vocabulario general) y todas las
        listas para elegir otra. Nada mientras se transcribe ni sin copia del original."""
        rel = rel_of(abs_path)
        vacio = {"propuestas": [], "lista": "", "proyecto": "", "listas": []}
        if self.jobs.active_for(rel):
            return vacio
        orig = original_de(rel)
        data = self.transcripts.load(os.path.splitext(abs_path)[0]) if orig else None
        if not orig or not data:
            return vacio
        apr = aprendido_de(self.settings.get())
        reg = registro_aprendido(rel)
        hechos = {(aprender.normal(c["de"]), aprender.normal(c["a"])) for c in reg["cambios"]}
        no = {aprender.normal(x) for x in apr["no"]} | set(reg["respondidas"])
        ps = aprender.propuestas(orig, data["segments"], hechos, no, [tuple(x) for x in apr["no_reglas"]])
        if not ps:
            return vacio
        props = md_props(abs_path)
        v = lista_de_reunion(abs_path, props)
        listas = self._vocab_terms()
        general = {aprender.normal(t) for t in engines.split_terms(self.settings.get().get("prompt") or "")}
        for p in ps:
            k = aprender.normal(p["termino"])
            p["en"] = [l["id"] for l in listas if k in {aprender.normal(t) for t in l["terms"]}]
            p["en_general"] = k in general
            for x in p["antes"]:  # la regla que ya tienes, en que proyectos (la interfaz no la ofrece otra vez alli)
                kd = aprender.normal(x["texto"])
                x["reglas_en"] = [r["proyecto"] for r in apr["reglas"]
                                  if aprender.normal(r["de"]) == kd and aprender.normal(r["a"]) == k]
        # el proyecto de la reunion (aunque su lista no este): si la propuesta va al vocabulario general, la casilla
        # de la regla viene sin marcar, porque valdria para las reuniones sin proyecto y no para las de este
        out = {"propuestas": ps, "lista": v["id"] if v else "", "proyecto": v["project"] if v else "",
               "proyecto_reunion": proyecto_de_reunion(abs_path, props),
               "listas": [{"id": l["id"], "project": l["project"], "name": l["name"],
                           "conectada": bool(l.get("conectada"))} for l in listas]}
        guardada = temas().get(rel) or ""
        lid = guardada[len(VLINK):].split("/")[0] if guardada.startswith(VLINK) else ""
        sin_carpeta = [k for k, l in list(VOCAB_LINKS.items()) if not os.path.isdir(l["path"])
                       and (k == lid or fold(l["name"]) == fold(props.get("proyecto") or ""))]
        if not v and props.get("workstream") and sin_carpeta:
            # su lista esta en una carpeta conectada que ahora no se encuentra (VPN, red, disco): no se elige otra por
            # el; si nunca tuvo lista o se borro, como siempre: el vocabulario general, que es el que uso Whisper
            out["lista"] = None
            out["falta"] = {"proyecto": props.get("proyecto") or "", "workstream": props["workstream"]}
        return out

    def aprender_get(self, q):
        """GET /api/aprender?path=: las propuestas de esa reunion (ver _propuestas)."""
        return self._propuestas(safe(q.get("path"), "file"))

    def aprender_anadir(self, b):
        """POST /api/aprender/anadir {path, termino, lista ("" = vocabulario general), reglas: [lo que escribio
        Whisper]}: tu sí. El termino va al final de la lista elegida (tambien en una carpeta conectada, sin tocarle nada
        mas) y, de las reglas seguras de esa propuesta, las que marcaste se aplican desde la siguiente transcripcion del
        proyecto de esa lista; las que desmarcaste no se vuelven a proponer. Solo vale lo que se esta proponiendo ahora
        en esa reunion: nada se aprende que no hayas visto."""
        abs_path = safe(b.get("path"), "file")
        termino = aprender.termino_valido(b.get("termino"))
        p = next((x for x in self._propuestas(abs_path)["propuestas"]
                  if termino and aprender.normal(x["termino"]) == aprender.normal(termino)), None)
        if not p:
            raise ApiError("Esa propuesta ya no está: puede que hayas vuelto a cambiar el texto. Recarga la reunión.")
        termino = p["termino"]
        quiere = {aprender.normal(x) for x in b.get("reglas") or [] if isinstance(x, str)}
        seguras = [x for x in p["antes"] if x["regla"]]
        reglas = [x for x in seguras if aprender.normal(x["texto"]) in quiere]
        if len(reglas) != len(quiere):
            raise ApiError("Esa corrección ya no se puede aprender. Recarga la reunión.")
        lista = str(b.get("lista") or "")
        v = find_vocab(None, None, lista) if lista else None
        if lista and not v:
            vid = lista[len(VLINK):].split("/")[0] if lista.startswith(VLINK) else ""
            if vid in VOCAB_LINKS and not os.path.isdir(VOCAB_LINKS[vid]["path"]):
                raise ApiError(f"No encuentro tu carpeta de listas «{VOCAB_LINKS[vid]['name']}»: ¿sin conexión o con "
                               "el disco desconectado? Vuelve a probar cuando esté, o elige otra lista.")
            raise ApiError("Esa lista ya no existe. Elige otra.")
        lang = self.settings.get().get("language") or ""
        if v:
            ya = anadir_a_lista(v, termino)
            try:
                cabe = termino not in vocab_info(v, lang)["dropped"]
            except OSError:
                cabe = True
        else:
            general = self.settings.get().get("prompt") or ""
            ya = aprender.normal(termino) in {aprender.normal(t) for t in engines.split_terms(general)}
            if not ya:
                general = aprender.anadir_texto(general, termino)
                self.settings.update(lambda d: d.update(prompt=general))
            cabe = termino not in vocab_fit(general, lang)["dropped"]
        proyecto, ahora = v["project"] if v else "", time.time()
        try:  # desde esta reunion ya no se vuelve a proponer (aunque haya ido a otra lista que la de la reunion)
            with self._tx_save_lock:
                reg = registro_aprendido(rel_of(abs_path))
                k = aprender.normal(termino)
                if k not in reg["respondidas"] and os.path.isdir(backup_dir(rel_of(abs_path))):
                    reg["respondidas"].append(k)
                    guardar_registro(rel_of(abs_path), reg)
        except OSError:
            log_exc()
        if reglas:
            def upd(d):
                a = aprendido_de(d)
                for x in reglas:  # una por lo que escribio Whisper y proyecto: la nueva sustituye a la vieja
                    kd = aprender.normal(x["texto"])
                    a["reglas"] = [r for r in a["reglas"] if not (aprender.normal(r["de"]) == kd
                                                                  and mismo_proyecto(r["proyecto"], proyecto))]
                    a["reglas"].append({"de": x["texto"], "a": termino, "proyecto": proyecto, "at": ahora})
                d["aprendido"] = a
            # la casilla desmarcada no se apunta como un «no» para siempre (no se veria en ningun sitio): en esta
            # reunion ya no se pregunta (respondidas) y, si lo vuelves a corregir en otra, se vuelve a ofrecer
            self.settings.update(upd)
        self.library.at = 0
        n = len(reglas)
        log(f"✦ aprendido: {termino} en {(v['project'] + ' · ' + v['name']) if v else 'el vocabulario general'} "
            f"({n} regla{'s' if n != 1 else ''})")
        return {"ya_estaba": ya, "cabe": cabe, "reglas": n,
                "lista": {k: v[k] for k in ("id", "project", "name")} if v else None,
                "prompt": None if v else self.settings.get().get("prompt") or ""}

    def aprender_no(self, b):
        """POST /api/aprender/no {termino}: «No»: ese termino no se vuelve a proponer (en ninguna reunion)."""
        termino = aprender.termino_valido(b.get("termino"))
        if not termino:
            raise ApiError("Falta el término.")
        k = aprender.normal(termino)

        def upd(d):
            a = aprendido_de(d)
            if k not in {aprender.normal(x) for x in a["no"]}:
                a["no"].append(termino)
            d["aprendido"] = a
        self.settings.update(upd)
        return {"ok": True}

    def aprender_deshacer(self, b):
        """POST /api/aprender/deshacer {path, de, a}: «Deshacer» de lo que cambio Tenca en esa reunion
        (deshacer_aprendido). Con el candado de las correcciones a mano, como Restaurar original."""
        with self._tx_save_lock:
            abs_path = safe(b.get("path"), "file")
            rel = rel_of(abs_path)
            if self.jobs.active_for(rel):
                raise ApiError("Se está transcribiendo; espera a que termine.")
            de = str(b.get("de") or "")
            hechos, saltados = deshacer_aprendido(rel, de, str(b.get("a") or ""))
        self.library.at = 0
        if hechos:
            self.jobs.schedule_outputs(rel)  # destinos de salida: el documento vuelve a lo que escribio Whisper
        # la regla sigue para las proximas reuniones: la interfaz ofrece olvidarla (su proyecto, para aprender_olvidar)
        proyecto = proyecto_de_reunion(abs_path)
        r = next((r for r in aprendido_de(self.settings.get())["reglas"] if aprender.normal(r["de"]) ==
                  aprender.normal(de) and mismo_proyecto(r["proyecto"], proyecto)), None)
        return {"deshechos": hechos, "no_deshechos": saltados, "regla": {"proyecto": r["proyecto"]} if r else None}

    def _reglas_publicas(self):
        return [{k: r[k] for k in ("de", "a", "proyecto")} for r in aprendido_de(self.settings.get())["reglas"]]

    def aprender_reglas(self, q):
        """GET /api/aprender/reglas: las reglas aprendidas y lo que dijiste que no se propusiera (Ajustes ›
        Proyectos)."""
        return {"reglas": self._reglas_publicas(), "no": aprendido_de(self.settings.get())["no"]}

    def aprender_regla(self, b):
        """POST /api/aprender/regla {antes: {de, proyecto} | null, de, a, proyecto}: cambiar una entrada del diccionario
        personal desde Ajustes (lo que escribio Whisper, lo correcto o el proyecto). Lo ya cambiado en reuniones
        anteriores no se toca: cada una sigue con su «Deshacer»."""
        de, a = aprender.termino_valido(b.get("de")), aprender.termino_valido(b.get("a"))
        if not de or not a:
            raise ApiError("Escribe lo que pone Whisper y lo correcto (sin comas ni punto y coma).")
        if de == a:
            raise ApiError("Lo que pone Whisper y lo correcto son iguales: no habría nada que cambiar.")
        toks = [t for t, _, _ in aprender.tokens(de)]
        if not toks or len(toks) > aprender.MAX_REGLA:
            raise ApiError("Lo que pone Whisper tiene que ser una palabra, o como mucho tres.")
        if all(aprender.fold(t) in aprender.COMUNES or aprender.fold(t) in aprender.ENLACES for t in toks):
            raise ApiError(f"«{de}» es una palabra muy corriente: cambiaría muchas frases que están bien. Pon algo más "
                           "concreto.")
        if len(aprender.letras(de)) < 3:
            raise ApiError("Lo que pone Whisper tiene que tener al menos 3 letras: algo más corto saldría en muchas "
                           "frases que están bien.")
        if not aprender.regla_valida(de, a):  # solo mayúsculas o tildes en una palabra corriente («plan» → «Plan»)
            if de.lower() != a.lower():  # cambian tildes (y quizá mayúsculas), no solo mayúsculas
                raise ApiError(f"Solo cambian las tildes de «{de}»: no hace falta en el diccionario, y cambiaría "
                               "frases que están bien.")
            raise ApiError(f"Cambiar «{de}» por «{a}» tocaría frases que están bien: si solo cambian las mayúsculas, "
                           "vale para siglas (KPI) o nombres como iPhone. Pon algo más concreto.")
        proyecto = str(b.get("proyecto") or "")
        antes = b.get("antes") if isinstance(b.get("antes"), dict) else None
        conocidos = {v["project"] for v in list_vocab()} | {""}
        if antes:
            conocidos.add(str(antes.get("proyecto") or ""))
        if not any(mismo_proyecto(proyecto, p) for p in conocidos):
            raise ApiError("Ese proyecto ya no existe. Elige otro.")
        proyecto = next((p for p in sorted(conocidos) if mismo_proyecto(proyecto, p)), proyecto)  # como se llama
        k, ka = aprender.normal(de), aprender.normal(str((antes or {}).get("de") or ""))
        pa = str((antes or {}).get("proyecto") or "")
        err = []

        def upd(d):
            ap = aprendido_de(d)
            es_la = lambda r: antes and aprender.normal(r["de"]) == ka and mismo_proyecto(r["proyecto"], pa)  # noqa
            if any(not es_la(r) and aprender.normal(r["de"]) == k and mismo_proyecto(r["proyecto"], proyecto)
                   for r in ap["reglas"]):
                err.append(f"Ya tienes «{de}» en tu diccionario para ese proyecto.")
                return
            if antes and not any(es_la(r) for r in ap["reglas"]):
                err.append("Esa entrada ya no está en tu diccionario.")
                return
            ap["reglas"] = [r for r in ap["reglas"] if not es_la(r)]
            ap["reglas"].append({"de": de, "a": a, "proyecto": proyecto, "at": time.time()})
            d["aprendido"] = ap
        self.settings.update(upd)
        if err:
            raise ApiError(err[0])
        log(f"✦ diccionario: {de} → {a}")
        return {"reglas": self._reglas_publicas(), "no": aprendido_de(self.settings.get())["no"]}

    def aprender_olvidar(self, b):
        """POST /api/aprender/olvidar {de, proyecto}: la regla deja de aplicarse en las proximas reuniones (lo ya
        cambiado se queda, con su Deshacer). {no: termino}: ese termino se puede volver a proponer."""
        k, proyecto = aprender.normal(str(b.get("de") or "")), str(b.get("proyecto") or "")
        kno = aprender.normal(str(b.get("no") or ""))  # {"no": termino}: que se pueda volver a proponer
        quitadas = []

        def upd(d):
            a = aprendido_de(d)
            if kno:
                a["no"] = [x for x in a["no"] if aprender.normal(x) != kno]
            else:
                quitadas[:] = [r for r in a["reglas"] if aprender.normal(r["de"]) == k
                               and mismo_proyecto(r["proyecto"], proyecto)]
                a["reglas"] = [r for r in a["reglas"] if r not in quitadas]
            d["aprendido"] = a
        self.settings.update(upd)
        if quitadas:
            log(f"✦ regla olvidada: {quitadas[0]['de']} → {quitadas[0]['a']}")
        return {"reglas": self._reglas_publicas(), "no": aprendido_de(self.settings.get())["no"]}

    # --- POST
    def transcribe(self, b):
        with self._move_lock():  # una reunion que se esta moviendo: se espera y se encola con su ruta nueva
            return self._transcribe(b)

    def _transcribe(self, b):
        paths = [alias_of(p) for p in (b.get("paths") or ([b["path"]] if b.get("path") else []))]
        preset = b.get("preset") or self.settings.get()["preset"]
        ids = self.jobs.submit(paths, preset, b.get("language"), resume=bool(b.get("resume")))
        # el idioma elegido es de estas reuniones: el del perfil (Ajustes) no cambia, como al grabar
        self.settings.update(lambda d: d.update(preset=preset))
        return {"added": ids}

    def cancel(self, b):
        return {"ok": self.jobs.cancel(b.get("id"))}

    def start_live(self, b):
        if b.get("preset"):
            self.jobs._check_live_preset(b["preset"])
            self.settings.update(lambda d: d.update(live_preset=b["preset"]))
        return {"id": self.jobs.start_live(b.get("path"), b.get("preset"))}

    def live_model(self, b):
        return self.jobs.set_live_preset(b.get("id"), b.get("preset"), bool(b.get("restart")))

    def live_translate(self, b):
        """Traducción del directo en la nube, por reunión (apagada por defecto): {id, on, to}. to: «es» o «en» (msg
        1152); se recuerda para esa reunión (traduccion_destino), también con la traducción apagada. live.py hace el
        resto: con otro destino, vuelve a traducir lo último al idioma nuevo."""
        job = self.jobs._live_job(b.get("id"))
        to = b.get("to")
        if to is not None and to not in live.TRANSLATE_TO:
            raise ApiError("La traducción del directo solo puede ser al español o al inglés.")
        if b.get("on"):  # sin servicio de chat (Azure OpenAI) no hay traduccion: se dice, no falla a medias
            why = engines.chat_missing(self.settings.get()["cloud"], "La traducción del directo")
            if why:
                raise ApiError(why)
        if to is not None:
            job.translate_to = to
            traduccion_destinos().set(job.path, to)
        job.translate = bool(b.get("on"))
        if isinstance(job.directo, dict):  # que la respuesta ya lo refleje (live.py lo vuelve a publicar en <1 s)
            job.directo = dict(job.directo, translate=job.translate, translate_to=live.translate_to(job))
        return job.public()

    def live_propuesta(self, b):
        """«Lo último suena a … · No» (B6): {id, rechazar: "pt"}. Ese idioma no se vuelve a proponer en esta grabación.
        («Cambiar» no pasa por aquí: es el cambio de idioma de siempre, POST /api/notes.)"""
        job = self.jobs._live_job(b.get("id"))
        idioma = str(b.get("rechazar") or "")
        if idioma not in directo_avisos.PROPONIBLES:
            raise ApiError("Idioma desconocido.")
        (job.opts or {}).setdefault("rechazos", set()).add(idioma)
        p = getattr(job, "propuesta", None)
        if p is not None:
            p.rechazar(idioma)
        if isinstance(job.directo, dict):  # que la respuesta ya lo refleje (live.py lo vuelve a publicar en <1 s)
            job.directo = dict(job.directo, propuesta=None)
        log(f"   directo {job.name}: no se propone más {idioma} en esta grabación")
        return job.public()

    def ocultar_captura(self, b):
        """POST {activado}: guarda «Ocultar Tenca al compartir pantalla» y lo aplica ya a la ventana (Ajustes › Al
        grabar; sin «Guardar», como «Arrancar con Windows»). Sin «activado», solo dice cómo está (como el GET)."""
        if isinstance(b, dict) and "activado" in b:
            on = b.get("activado") is True
            self.settings.update(lambda d: d.update(ocultar_al_compartir=on))
            if WINDOW is not None:
                WINDOW.set_capture(on)
            log(f"ajustes: ocultar {APP_NAME} al compartir pantalla {'activado' if on else 'desactivado'}")
        return captura_estado(self.settings.get())

    def clear_jobs(self, b):
        self.jobs.clear()
        return {"ok": True}

    def save_settings(self, b):
        actual = perfil_de(self.settings.get())
        perfil = (check_perfil(b["perfil"], actual["outputs"], actual)  # valida antes de guardar
                  if isinstance(b.get("perfil"), dict) else {})
        antes = procesado_de(self.settings.get())
        cur = self.settings.get()
        audio = [(b[k].strip() or defecto) if isinstance(b.get(k), str) else (cur.get(k) or defecto)
                 for k, defecto in (("rec_mic", "default"), ("rec_sys", "all"))]
        if audio == ["none", "none"]:
            raise ApiError(SIN_AUDIO)

        base, estado = b.get("rev"), {"conflicto": False}

        def upd(d):
            # Ajustes guarda cada control al cambiarlo y manda el config_rev que tenia (rev). Si no coincide, otra
            # ventana (o el asistente) ha guardado antes: lo que es una lista entera (las copias en otras carpetas, las
            # claves adicionales) no se pisa; lo demas se guarda y la respuesta lo dice (otro_cambio). Se mira aqui,
            # con el candado de Store.update: dos peticiones a la vez no pasan las dos como «sin conflicto»
            conflicto = estado["conflicto"] = base is not None and base != self.settings.rev
            pf, cl = b.get("perfil"), b.get("cloud")
            listas = (isinstance(pf, dict) and "outputs" in pf
                      or isinstance(cl, dict) and any(k in cl for k in ("extra_keys", "extra_keys_by")))
            if conflicto and listas:
                raise ApiError(AJUSTES_CAMBIADOS, 409)
            for k in ("preset", "language", "prompt"):
                if k in b:
                    d[k] = b[k] or ""
            s = b.get("summary") or {}
            if "auto" in s:
                d["summary_auto"] = bool(s["auto"])
            if "local" in s:  # consentimiento: resumir solo tambien lo transcrito en local (manda el texto a la nube)
                d["summary_auto_local"] = bool(s["local"])
            if s.get("aviso_visto") or s.get("local"):  # el aviso de Inicio, cerrado (o ya no hace falta)
                d["aviso_resumen_local"] = False
            if s.get("model"):
                d["summary_model"] = s["model"].strip()
            if s.get("lang") in engines.SUMMARY_LANGS:
                d["summary_lang"] = s["lang"]
            if "extra" in s:
                d["summary_extra"] = (s["extra"] or "").strip()[:500]
            if "meeting_detect" in b:
                d["meeting_detect"] = bool(b["meeting_detect"])
            for k in ("outlook_on", "rec_aviso"):  # reuniones: calendario de Outlook y aviso flotante
                if k in b:
                    d[k] = bool(b[k])
            if "live_auto" in b:
                d["live_auto"] = bool(b["live_auto"])
            if "carpeta_por_reunion" in b:  # una carpeta por reunion (encendida por defecto)
                d["carpeta_por_reunion"] = b["carpeta_por_reunion"] is not False
            if "rec_directo" in b:  # Ajustes › Al grabar: texto en directo (la tarjeta lo cambia por reunion)
                d["rec_directo"] = bool(b["rec_directo"])
            for k, defecto in (("rec_mic", "default"), ("rec_sys", "all")):
                if isinstance(b.get(k), str):
                    d[k] = b[k].strip()[:300] or defecto
            if "live_final_pass" in b:
                d["live_final_pass"] = bool(b["live_final_pass"])
            if b.get("live_preset") in LIVE_PRESETS:
                d["live_preset"] = b["live_preset"]
            if "threads" in b:
                d["threads"] = max(1, min(int(b["threads"] or default_threads()), os.cpu_count() or 16))
            if perfil:
                update_perfil(d, perfil)
            if "worker_keepalive_min" in b:  # 0 = como antes (5 min); tope 12 h
                d["worker_keepalive_min"] = max(0, min(int(b["worker_keepalive_min"] or 0), 720))
            if "worker_preload" in b:
                d["worker_preload"] = bool(b["worker_preload"])
            if b.get("procesado") in PROCESADOS:
                prev = procesado_de(d)
                d["procesado"] = b["procesado"]
                if d["procesado"] != "api":  # sin nube: los motores de la cola y del directo vuelven a ser locales
                    if prev == "api":  # los que se usaban con la API: si se vuelve a ella, vuelven (ida y vuelta)
                        d["motores_api"] = {k: d.get(k) for k in ("preset", "live_preset")}
                    for k, local in (("preset", "rapido"), ("live_preset", "medium")):
                        if (engines.PRESETS_BY_ID.get(d.get(k)) or {}).get("engine") == "cloud":
                            d[k] = local
                elif prev != "api" and (d["cloud"].get("api_key") or "").strip():
                    # y al reves (07/11/2026): a «API externa» con la clave ya puesta, los motores de cuando se uso la
                    # API (o la nube, como con la primera clave); si no, se elegia la API y se seguia transcribiendo con
                    # Whisper en el PC. Lo que se elija a la vez en esta misma peticion manda
                    antes_api = d.get("motores_api") if isinstance(d.get("motores_api"), dict) else {}
                    for k, validos in (("preset", engines.PRESETS_BY_ID), ("live_preset", LIVE_PRESETS)):
                        if k not in b:
                            d[k] = antes_api.get(k) if antes_api.get(k) in validos else "nube"
            ag = b.get("agente") if isinstance(b.get("agente"), dict) else None
            if ag:
                cur = d.get("agente") if isinstance(d.get("agente"), dict) else {}
                cls = (engines.AGENTES.get(ag.get("tipo")) or engines.AGENTES.get(cur.get("tipo"))
                       or engines.AGENTES["claude"])
                modelo = ag.get("modelo") if ag.get("modelo") in dict(cls.MODELOS) else cls.MODELO
                d["agente"] = {"tipo": cls.tipo, "modelo": modelo}
            c = b.get("cloud") or {}
            for k in ("provider", "model", "base_url", "azure_endpoint", "azure_deployment", "azure_api_version"):
                if k == "provider" and c.get(k) not in engines.PROVIDERS:
                    continue  # «Elige el proveedor…» sin elegir: no se guarda vacio (el motor tomaria Groq)
                if k in c:
                    d["cloud"][k] = (c[k] or "").strip()
            # 20 min de FLAC 16 kHz ~ 22 MB: por debajo del limite de 25 MB por archivo de las APIs
            for k, lo, hi in (("chunk_minutes", 2, 20), ("parallel", 1, 8)):
                if k in c:
                    d["cloud"][k] = max(lo, min(int(c[k] or lo), hi))
            if c.get("api_key") and "…" not in c["api_key"] and "•" not in c["api_key"]:
                if not d["cloud"].get("api_key") and procesado_de(d) == "api":  # primera key: la nube, predeterminada
                    d["preset"] = d["live_preset"] = "nube"
                d["cloud"]["api_key"] = c["api_key"].strip()
            if c.get("clear_key"):
                d["cloud"]["api_key"] = ""
            for k in ("live_model", "live_model_custom"):  # el del directo en Groq y el de una pasarela propia
                if k in c:
                    d["cloud"][k] = (c[k] or "").strip()[:80]
            # claves adicionales: cada lista es de su proveedor (las de Groq, en extra_keys). La interfaz manda las de
            # cada proveedor en extra_keys_by; extra_keys a secas son las del proveedor que se guarda. Las ya guardadas
            # vuelven enmascaradas y se conservan, pero solo dentro de su proveedor: nunca pasan a otro
            lists = c.get("extra_keys_by") if isinstance(c.get("extra_keys_by"), dict) else {}
            if not lists and isinstance(c.get("extra_keys"), list):
                lists = {d["cloud"].get("provider") or "groq": c["extra_keys"]}
            for prov, incoming in lists.items():
                if prov not in engines.PROVIDERS:
                    continue  # sin proveedor elegido no hay de quien: no se guardan como claves de Groq
                field = engines.extra_keys_field(prov)
                if not field or not isinstance(incoming, list):
                    continue
                old = {masked(k): k for k in d["cloud"].get(field) or []}
                new = [old.get(k.strip(), k.strip()) for k in incoming if isinstance(k, str) and k.strip()]
                d["cloud"][field] = [k for k in new if "…" not in k and "•" not in k][:10]
        self.settings.update(upd, rev=True)
        p = (getattr(self, "pruebas", None) or {}).get(b.get("prueba")) if b.get("prueba") else None
        if p is not None and p.terminada and p.firma == self._firma(self.settings.get()):
            res = p.resumen()  # la del asistente: es de lo que se acaba de guardar
            self.settings.update(lambda d: d.update(ultima_prueba=res))
        ahora = procesado_de(self.settings.get())
        if ahora != antes:
            log(f"   dónde se procesan las reuniones: {antes} → {ahora}")
        if antes == "api" and ahora != "api":
            self.jobs.nube_apagada()  # lo que estuviera en la nube no sigue
        # recien elegido un modo que transcribe en este PC: el modelo del directo, ya, con su progreso en Ajustes (solo
        # al cambiar de modo: «Guardar» con el mismo no vuelve a empezar una descarga que se cancelo)
        descarga = self._descargar_modelo_del_directo() if ahora != antes and ahora in ("local", "agente") else None
        return dict(self.config({}), descarga=descarga, otro_cambio=estado["conflicto"])

    def agente_comprobar(self, b):
        """Ajustes → «Comprobar»: el agente esta instalado, responde a --version y contesta una peticion minima con la
        cuenta del usuario. Guarda la version y la hora para ensenarlas en Ajustes."""
        st = self.settings.get()
        cls, a = agente_de(dict(st, agente=b if isinstance(b, dict) and b.get("tipo") else st.get("agente")))
        modelo = b.get("modelo") if isinstance(b, dict) and b.get("modelo") in dict(cls.MODELOS) else a["modelo"]
        ruta = cls.find()
        if not ruta:
            raise ApiError(f"No encuentro {cls.label} en este PC. Haz los pasos 1 y 2 y vuelve a pulsar «Comprobar».")
        # lo que tarda cada paso, al log (07/11/2026: en el PC de Bernardo «Comprobar» tardó minutos sin decir en que)
        origen = cls.origen(ruta)  # «Claude Code de VS Code» si es la copia de la extension (07/11/2026)
        log(f"   agente: comprobando {origen} ({modelo})…" + (f" · {ruta}" if origen != cls.label else ""))
        ag, t0 = cls(settings_dir=DATA_DIR), time.time()
        try:
            version, secs = ag.check(ruta, model=modelo)
        except engines.EngineError as e:
            log(f"   agente: {cls.label}: {e} ({self._agente_tiempos(ag, time.time() - t0)})")
            raise ApiError(explicar.explicar(e)["frase"])
        t = getattr(ag, "tiempos", None)
        if isinstance(t, dict) and t:
            log(f"   agente: {self._agente_tiempos(ag, secs)}")
        self.settings.update(lambda d: d.update(agente_version=version, agente_comprobado=time.time()))
        aviso = getattr(ag, "aviso", None)
        if aviso:
            log(f"   agente: {aviso}")
        log(f"   agente: {cls.label} {version} comprobado ({secs:.1f} s)")
        return {"ok": True, "version": version, "seconds": round(secs, 1), "ruta": ruta, "origen": origen,
                "modelo_usado": getattr(ag, "modelo_usado", None), "aviso": aviso}

    @staticmethod
    def _agente_tiempos(ag, total):
        """«--version 2.1 s · petición mínima 41.0 s · total 43.1 s» (lo que se sepa de cada paso)."""
        t = getattr(ag, "tiempos", None) or {}
        partes = [f"--version {t['version_s']} s" if "version_s" in t else None,
                  f"petición mínima {t['peticion_s']} s" if "peticion_s" in t else None, f"total {total:.1f} s"]
        return " · ".join(p for p in partes if p)

    def test_cloud(self, b):
        cloud = self.settings.get()["cloud"]
        try:
            secs = self.jobs.cloud.test(cloud)
        except engines.EngineError as e:
            prov = (engines.PROVIDERS.get(cloud.get("provider") or "groq") or {}).get("label")
            raise ApiError(explicar.explicar(e, proveedor=prov)["frase"])
        return {"ok": True, "seconds": round(secs, 2)}

    # --- «Probar» del asistente «Configurar» de la app (probar.py): prueba real con lo que se elige, sin guardar
    def _candidata(self, b):
        """(ajustes con lo elegido en el asistente encima, sin guardarlos; True si hay algo elegido). La clave nueva
        solo si es una de verdad (no la máscara que enseña Ajustes)."""
        st = copy.deepcopy(self.settings.get())
        c = (b or {}).get("candidata")
        if not isinstance(c, dict):
            return st, False
        if c.get("procesado") in PROCESADOS:
            st["procesado"] = c["procesado"]
        ag = c.get("agente")
        if isinstance(ag, dict):
            cls = engines.AGENTES.get(ag.get("tipo")) or agente_de(st)[0]
            st["agente"] = {"tipo": cls.tipo,
                            "modelo": ag.get("modelo") if ag.get("modelo") in dict(cls.MODELOS) else cls.MODELO}
        cl = c.get("cloud") if isinstance(c.get("cloud"), dict) else {}
        elegido = proveedor_elegido(self.settings.get()) or cl.get("provider") in engines.PROVIDERS
        st["cloud"] = dict(st.get("cloud") or {})
        for k in ("provider", "model", "base_url", "azure_endpoint", "azure_deployment", "azure_api_version"):
            if k == "provider" and cl.get(k) not in engines.PROVIDERS:
                continue  # como al guardar: un proveedor vacio o desconocido no se prueba como si fuera Groq
            if k in cl:
                st["cloud"][k] = (cl[k] or "").strip()
        key = (cl.get("api_key") or "").strip()
        if key and "…" not in key and "•" not in key:
            st["cloud"]["api_key"] = key
        if st.get("procesado") == "api" and not elegido:
            # el motor lo rechaza con «Elige el proveedor…» (por la marca: sin ella, unos ajustes guardados con un
            # proveedor vacio van a Groq, como siempre): nada sale hacia Groq
            st["cloud"]["provider"] = ""
            st["cloud"][engines.SIN_PROVEEDOR] = True
        return st, True

    @staticmethod
    def _prueba_motores(st):
        """(preset del directo, [modelos de Whisper que hacen falta]) de esa configuración, sin «API externa» siempre
        en el PC: el del directo y el de la cola (los dos se descargan juntos, decisión de Claudia 07/11/2026)."""
        out = []
        for key, local in (("live_preset", "medium"), ("preset", "rapido")):
            p = engines.PRESETS_BY_ID.get(st.get(key) or local)
            if not p or p["engine"] != "local":
                p = engines.PRESETS_BY_ID[local]
            out.append(p)
        return out[0], list(dict.fromkeys(p["model"] for p in out))

    def _prueba_hacer(self, st):
        """Las funciones de verdad para probar.Prueba (micro, Whisper, nube, Claude) con esos ajustes."""
        modo = procesado_de(st)
        directo, modelos_pc = self._prueba_motores(st)
        tmp = os.path.join(DATA_DIR, "tmp")
        os.makedirs(tmp, exist_ok=True)

        def modelos(report, cancelled):
            if engines.MODELS is None:
                return
            names = [m for m in modelos_pc if m in engines.WHISPER_MODELS]
            for m in names:
                if engines.MODELS.path(m) is None:
                    engines.MODELS.start(m)
            while True:
                sts = [engines.MODELS.state(m) for m in names]
                if all(x["ready"] for x in sts):
                    return
                errs = [x["error"] for x in sts if x["error"] and not x["downloading"] and not x["ready"]
                        and "cancelada" not in x["error"].lower()]
                if errs:
                    raise engines.EngineError(errs[0])
                for m, x in zip(names, sts):
                    if not x["ready"] and not x["downloading"]:
                        engines.MODELS.start(m)  # sin empezar o cancelada en Ajustes: la prueba la necesita
                total = sum(x["total_mb"] or x["size_mb"] or 0 for x in sts)
                done = sum((x["total_mb"] or x["size_mb"] or 0) if x["ready"] else (x["done_mb"] or 0) for x in sts)
                eta = max((x["eta_s"] or 0) for x in sts)
                gb = lambda mb: f"{mb / 1000:.1f}".replace(".", ",")  # noqa: E731
                report(f"Descargando · {int(100 * done / total) if total else 0} % · {gb(done)} de {gb(total)} GB"
                       + (f" · quedan unos {engines.fmt_left(eta)}" if eta else ""))
                if cancelled():
                    raise probar.Cancelada()
                time.sleep(0.5)

        def grabar(segundos, report, cancelled):
            if self.rec and self.rec.alive():
                raise engines.EngineError("Estás grabando: prueba cuando termines la grabación.")
            with self.rec_lock:
                if self.monitor:
                    self.monitor.stop(timeout=2)
                    self.monitor = None
            path = os.path.join(tmp, "prueba-micro.wav")
            try:
                os.remove(path)
            except OSError:
                pass
            report("Prepárate: en un momento empieza a grabar…")
            try:
                # con «Sin micrófono» guardado, la prueba usa el predeterminado de Windows
                mic = st.get("rec_mic") if st.get("rec_mic") not in (None, "", "none") else "default"
                r = RecorderProcess(path, mic, "none", APP_DIR, DATA_DIR)
            except Exception as e:  # sin Python para lanzarlo o sin permisos: la cadena con silencio
                return None, False, e.__class__.__name__
            fin = time.time() + 10
            while time.time() < fin and r.alive() and r.status.get("t") is None:
                if cancelled():
                    r.stop(timeout=2)
                    raise probar.Cancelada()
                time.sleep(0.1)
            if r.status.get("t") is None:
                r.stop(timeout=2)
                mic = r.status.get("mic") if isinstance(r.status.get("mic"), dict) else {}
                log(f"   probar: el micro no ha arrancado ({mic.get('error') or 'sin estado'})")
                return None, False, mic.get("error") or "el micro no ha arrancado"
            t0, fin = r.status.get("t") or 0, time.time() + segundos + 8  # un grabador colgado no para la prueba
            while r.alive() and (r.status.get("t") or 0) - t0 < segundos and time.time() < fin:
                if cancelled():
                    r.stop(timeout=2)
                    raise probar.Cancelada()
                quedan = max(1, int(segundos - ((r.status.get("t") or 0) - t0) + 0.99))
                report(f"Habla ahora: di «{probar.FRASE}» · {quedan} s")
                time.sleep(0.1)
            r.stop(timeout=5)
            mics = [m for _, m, _ in r.levels if m is not None]
            voz = sum(1 for m in mics if m > PRUEBA_VOZ_DB) >= 3  # ≥ 0,75 s con voz (niveles cada 0,25 s)
            log(f"   probar: micro {len(mics)} lecturas, máx {max(mics) if mics else '-'} dB, "
                f"voz: {'sí' if voz else 'no'}")
            return (path if os.path.isfile(path) else None), voz, None

        def cuenta(report, cancelled):
            cls, a = agente_de(st)
            ruta = cls.find()
            if not ruta:
                raise engines.EngineError(f"No encuentro {cls.label} en este PC. Sigue el paso a paso y vuelve a "
                                          "probar.")
            report(f"Comprobando {cls.label} con tu cuenta…")
            cls(settings_dir=DATA_DIR).check(ruta, model=a["modelo"], cancelled=cancelled)

        def silencio():
            import wave
            path = os.path.join(tmp, "prueba-silencio.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\0\0" * 16000)
            return path

        def transcribir(wav, report, cancelled):
            def rep(frac=None, stage=None, **k):
                report(stage)
            if modo == "api":
                import numpy as np
                y = engines.decode_16k(wav).astype(np.float32) / 32768.0
                f = os.path.join(tmp, "prueba.flac")
                engines.write_flac(f, y)
                r = self.jobs.cloud._post(st["cloud"], f, "es", None, rep, cancelled, attempts=2, max_wait=10.0)
                return (r.get("text") or "").strip()
            if getattr(self, "local_prueba", None) is None:
                self.local_prueba = engines.LocalEngine(DATA_DIR, "prueba", idle_s=60)
            segs, _, _ = self.local_prueba.transcribe(wav, directo, "es", None, st.get("threads") or default_threads(),
                                                      rep, cancelled)
            return " ".join(t for _, _, t in segs).strip()

        def resumir(texto, report, cancelled):
            def rep(stage=None, **k):
                report(stage)
            sistema = "Resume en una sola frase corta, en español, lo que dice este texto."
            if modo == "agente":
                cls, a = agente_de(st)
                out, _ = cls(settings_dir=DATA_DIR).chat(sistema, texto, 200, rep, cancelled, model=a["modelo"],
                                                         timeout=engines.AGENTE_CHECK_S)
                return out
            out, _ = engines.Summarizer(keys=self.jobs.cloud.keys)._chat(
                dict(st["cloud"], summary_model=st.get("summary_model")), sistema, texto, 200, rep, cancelled,
                timeout=60, attempts=2)
            return out

        prov = (engines.PROVIDERS.get(st["cloud"].get("provider") or "groq") or {}).get("label") or "la API"
        nombres = {"transcribe": f"con {prov}" if modo == "api" else "en este PC",
                   "resume": "Claude (tu cuenta)" if modo == "agente" else prov,
                   "proveedor": prov if modo == "api" else None}
        return {"cuenta": cuenta, "modelos": modelos, "grabar": grabar, "silencio": silencio,
                "transcribir": transcribir, "resumir": resumir}, nombres

    @staticmethod
    def _firma(st):
        """Lo que hace distinta una configuración para «Probar» (la clave, como huella): una prueba solo vale para la
        configuración que se probó (revisión del 07/11/2026: un «Listo» viejo sobrevivía a cambiar la clave)."""
        c = st.get("cloud") or {}
        key = (c.get("api_key") or "").strip()
        return (procesado_de(st), (st.get("agente") or {}).get("modelo"), c.get("provider"), c.get("model"),
                c.get("base_url"), c.get("azure_endpoint"), c.get("azure_deployment"),
                hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] if key else "")

    def probar_empezar(self, b):
        """POST /api/probar {candidata?}: empieza una prueba (una a la vez) y devuelve su id."""
        if self.rec and self.rec.alive():
            raise ApiError("Estás grabando: prueba cuando termines la grabación.")
        with self.rec_lock:
            cur = getattr(self, "prueba", None)
            if cur is not None and cur.is_alive():  # otra configuración (o la misma otra vez): la de antes se cancela
                cur.cancelar = True
            st, candidata = self._candidata(b)
            hacer, nombres = self._prueba_hacer(st)
            p = probar.Prueba(procesado_de(st), hacer, nombres, candidata=candidata, al_terminar=self._prueba_fin,
                              espera=cur if cur is not None and cur.is_alive() else None)
            p.firma = self._firma(st)
            if not hasattr(self, "pruebas"):
                self.pruebas = {}
            self.pruebas = {k: v for k, v in self.pruebas.items() if v.is_alive() or time.time() - v.empezada < 3600}
            self.pruebas[p.id] = self.prueba = p
            p.start()
        log(f"   probar: {procesado_de(st)}{' (sin guardar)' if candidata else ''}")
        return {"id": p.id}

    def _prueba_fin(self, p):
        r = p.public()
        log(f"   probar: {'bien' if r['listo'] else 'falla'} · " + " · ".join(
            f"{x['id']} {x['estado']} ({x['segundos']} s)" for x in r["pasos"])
            + (f" · {r['fallo']['tecnico'] or r['fallo']['frase']}" if r["fallo"] else ""))
        if p.nota_micro:
            log(f"   probar: micro: {p.nota_micro}")
        if not p.candidata:  # la de lo guardado (Ajustes › Probar): queda como última prueba
            res = p.resumen()
            self.settings.update(lambda d: d.update(ultima_prueba=res))

    def probar_estado(self, q):
        p = (getattr(self, "pruebas", None) or {}).get(q.get("id"))
        if p is None:
            raise ApiError("No encuentro esa prueba: vuelve a pulsar «Probar».")
        return p.public()

    def probar_cancelar(self, b):
        p = (getattr(self, "pruebas", None) or {}).get((b or {}).get("id"))
        if p is not None:
            p.cancelar = True
        return {"ok": True}

    def agente_estado(self, q):
        """Para el paso 2 del asistente: si Claude Code ya está en este PC (se mira cada pocos segundos)."""
        cls, _ = agente_de(self.settings.get())
        ruta = cls.find()
        return {"encontrado": bool(ruta), "label": cls.label, "origen": cls.origen(ruta) if ruta else None}

    def _powershell_env(self):
        """El entorno de la ventana de «Abrir PowerShell»: el de una PowerShell nueva (engines.windows_env) y, delante
        en su PATH, la carpeta del Claude Code que encuentra la app (08/11/2026). El instalador nativo no lo pone en el
        PATH («…\\.local\\bin is not in your PATH», medido en Windows) y la copia de VS Code tampoco: así, en esa
        ventana, `claude` (y su inicio de sesión en el navegador) funciona siempre. La app no inicia sesión por nadie:
        el usuario escribe claude y entra en la página de Claude."""
        env = engines.windows_env()
        cls, _ = agente_de(self.settings.get())
        ruta = cls.find()
        # y la carpeta del instalador nativo aunque aún no exista: el paso 1 instala en esa misma ventana y el 2
        # escribe claude en ella
        dirs = [os.path.dirname(os.path.abspath(ruta))] if ruta else []
        nativo = os.path.join(os.environ.get("USERPROFILE") or os.path.expanduser("~"), ".local", "bin")
        if os.path.normcase(nativo) not in map(os.path.normcase, dirs):
            dirs.append(nativo)
        env["PATH"] = os.pathsep.join(dirs + [env.get("PATH", "")])
        return env

    def abrir_powershell(self, b):
        """«Abrir PowerShell» del asistente: una ventana vacía, sin ejecutar nada (con Claude Code en su PATH)."""
        if os.name != "nt":
            raise ApiError("Este botón solo funciona en Windows: abre una terminal y sigue los pasos.")
        subprocess.Popen(["powershell.exe", "-NoExit", "-NoLogo"], cwd=os.path.expanduser("~"),
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0), env=self._powershell_env())
        return {"ok": True}

    # --- modelos de Whisper en este PC (engines.WhisperModels): estado, descarga con progreso y cancelar
    @staticmethod
    def _modelos_en_uso(st):
        """modelo -> ["directo", "cola"]: los que usan el motor del directo y el de la cola si son locales."""
        uso = {}
        for key, what, default in (("live_preset", "directo", "medium"), ("preset", "cola", "rapido")):
            p = engines.PRESETS_BY_ID.get(st.get(key) or default)
            if p and p["engine"] == "local":
                uso.setdefault(p["model"], []).append(what)
        return uso

    def modelos_state(self, q=None):
        st = self.settings.get()
        uso, out = self._modelos_en_uso(st), []
        for p in engines.PRESETS:
            if p["engine"] != "local":
                continue
            m = p["model"]
            if engines.MODELS is not None:
                s = engines.MODELS.state(m)
            else:  # sin el gestor (no deberia pasar con la app arrancada): solo si esta o no
                s = {"model": m, "ready": engines.model_cached(m), "size_mb": None, "downloading": False,
                     "done_mb": 0, "total_mb": None, "frac": None, "speed_mbs": None, "eta_s": None, "error": None}
            out.append(dict(s, preset=p["id"], label=p["label"], live_label=p["live_label"], uso=uso.get(m, [])))
        return {"modelos": out, "procesado": procesado_de(st),
                "carpeta": engines.MODELS.root if engines.MODELS is not None else None}

    def modelos_descargar(self, b):
        m = str((b or {}).get("model") or "")
        if engines.MODELS is None or m not in engines.WHISPER_MODELS:
            raise ApiError(f"No sé descargar el modelo «{m}».")
        if engines.MODELS.path(m) is None:
            engines.MODELS.start(m)
            log(f"   modelo {m}: descarga pedida desde Ajustes")
        return self.modelos_state()

    def modelos_cancelar(self, b):
        m = str((b or {}).get("model") or "")
        if engines.MODELS is not None and engines.MODELS.cancel(m):
            log(f"   modelo {m}: descarga cancelada desde Ajustes")
        return self.modelos_state()

    def _descargar_modelo_del_directo(self):
        """Al pasar a «Solo en este PC» o «Con tu cuenta de Claude»: si falta el modelo del directo, se empieza a
        descargar ya, con su progreso en Ajustes (antes se descargaba al empezar la primera reunion, sin progreso).
        Devuelve el modelo que se ha puesto a descargar, o None."""
        st = self.settings.get()
        p = engines.PRESETS_BY_ID.get(st.get("live_preset") or "medium")
        if engines.MODELS is None or not p or p["engine"] != "local" or p["model"] not in engines.WHISPER_MODELS:
            return None
        if engines.MODELS.path(p["model"]) is not None or engines.MODELS.cancelled_by_user(p["model"]):
            return None  # ya esta, o el usuario cancelo su descarga en Ajustes (la retoma con «Descargar»)
        engines.MODELS.start(p["model"])
        log(f"   modelo {p['model']}: descarga empezada al elegir dónde se procesan las reuniones")
        return p["model"]

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
        log(f"🗣 voces {'activadas' if on else 'desactivadas'}")
        return self.voices_state()

    def voices_identify(self, b):
        return {"id": self.jobs.submit_voices(b.get("path"))}

    def speakers_get(self, q):
        abs_path = safe(q.get("path"), "file")
        data = self.jobs.speakers_for(abs_path)
        job = self.jobs.active_for(rel_of(abs_path), kinds=("voces",))
        if not data:
            return {"exists": False, "job": job.public() if job else None, "enabled": self.jobs.voices_on()}
        real = [i for i in data["hablantes"].values() if not i.get("breve")]
        return {"exists": True, "enabled": self.jobs.voices_on(), "job": job.public() if job else None,
                "hablantes": data["hablantes"], "tu": data.get("tu"), "creado": data.get("creado"),
                "frases": data.get("frases", []), "who": data.get("_who", []),
                "n": len(real), "known": sum(1 for i in real if i.get("persona") and not i.get("provisional")),
                "personas": self.jobs.voices.public(), "usuario": (self.jobs.voices.user() or {}).get("id")}

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
        se escribia en la ruta vieja y se perdia (revision de la integracion del 08/11/2026)."""
        lock = getattr(self, "notes_lock", None)
        return lock if lock is not None else contextlib.nullcontext()

    def _speaker_update(self, abs_path, h, fn, creado=None, then=None):
        """Aplica fn(info, data) al hablante h y guarda el sidecar; con `then`, then(ruta) tambien dentro del candado
        (la respuesta: si se calculara fuera, un movimiento que empieza justo despues la dejaria a medias). Devuelve
        (la ruta de la reunion, la nueva si se acaba de mover; lo que devuelva then)."""
        with self._voices_lock():
            abs_path = safe(alias_of(rel_of(abs_path)), "file")
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
        abs_path = None
        if path and not h:
            safe(path, "file")  # como siempre: una reunion que ya no existe se dice
        if path and h:
            with self._voices_lock():  # si la reunion se esta moviendo (B1c), espera y lee la de la ruta nueva
                abs_path = safe(alias_of(path), "file")
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
            data = voices.read_sidecar(voices.sidecar_path(safe(rel, "file"))) or {}
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
                        abs_path = safe(alias_of(a["path"]), "file")
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
                    abs_path = safe(alias_of(rel), "file")
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
            abs_path = safe(alias_of(b.get("path")), "file")
            before = (self._speaker_check(abs_path, h, b.get("creado"))["hablantes"][h]).get("persona")
        abs_path, out = self._speaker_update(abs_path, h, lambda info, d: voices.assign(d, h, pid, uid),
                                             b.get("creado"), then=lambda p: self.speakers_get({"path": rel_of(p)}))
        # fuera del candado (puede calcular una huella en un subproceso), con la ruta de ahora
        self._not_the_user(alias_of(rel_of(abs_path)), h, before, pid)
        return out

    def speakers_join(self, b):
        """«Es el mismo que…» (msg 1242): dos hablantes de esta reunion son la misma voz (la app la partio en dos)."""
        h, con, v = b.get("h"), b.get("con"), self.jobs.voices
        with self._voices_lock():  # dentro: si la reunion se acaba de mover (B1c), su ruta nueva
            abs_path = safe(alias_of(b.get("path")), "file")
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
            abs_path = safe(alias_of(b.get("path")), "file")
            data = self.jobs.speakers_for(abs_path)
            if not data or not data.get("_who"):
                raise ApiError("Esta reunión no tiene hablantes identificados (o no tiene .srt).")
            segs = Transcripts().load(os.path.splitext(abs_path)[0])["segments"]
            out = os.path.splitext(abs_path)[0] + ".hablantes.txt"
            save_text(out, voices.export_text(segs, data["_who"], data, self.jobs.voices.names(),
                                              usuario=data.get("_usuario")), "utf-8-sig")
        self.library.at = 0
        return {"path": rel_of(out), "ext": ".hablantes.txt"}

    def export_package(self, b):
        import tempfile
        dest = os.path.join(desktop_dir(), f"{APP_NAME} - instalador.zip")
        vocab, seen, tmp = [], set(), None
        # las de carpetas vinculadas o conectadas (":id/…", ":v<id>/…"), como <raiz>/<proyecto>/Vocabulario/<tema>.txt
        for v in (list_vocab() if b.get("vocab") else []):
            arc = PKG_ROOT + "/" + (v["id"] if not v["id"].startswith(LINK)
                                    else f"{v['project']}/Vocabulario/{v['name']}.txt")
            if arc.lower() in seen:  # dos listas con el mismo proyecto y tema: una sola en el zip (la primera)
                continue
            src = v["_abs"]
            try:
                with open(src, "rb") as f:
                    raw = f.read()
                raw.decode("utf-8")
            except UnicodeDecodeError:  # cp1252 o UTF-16 (de una carpeta conectada): en el zip, en UTF-8
                tmp = tmp or tempfile.mkdtemp(prefix="tenca-listas-")
                src = os.path.join(tmp, f"{len(vocab)}.txt")
                with open(src, "w", encoding="utf-8", newline="") as f:  # sin newline="", Windows dobla los \r\n
                    f.write(_decode_list(raw))
            except OSError:
                continue
            seen.add(arc.lower())
            vocab.append((src, arc))
        try:
            n = build_package(dest, vocab)
        except OSError as e:
            raise ApiError(f"No se pudo crear el paquete: {e}")
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        if WINDOWS:
            subprocess.Popen(f'explorer /select,"{dest}"')
        return {"ok": True, "path": dest, "files": n, "size": os.path.getsize(dest), "vocab": len(vocab)}


# ----------------------------------------------------------------------------- HTTP


# ----------------------------------------------------------------------------- icono en la bandeja (bandeja.py)


# Windows apaga, reinicia o cierra la sesion con la ventana propia abierta (o oculta): window.py deja cerrar y llama
# a este mismo apagado limpio mientras Windows espera
Window.set_end_session(shutdown)


def arg(name, default=None):
    """--port 8766 / --data C:\\ruta (para probar una segunda copia sin tocar la principal)."""
    if name in sys.argv[:-1]:
        return sys.argv[sys.argv.index(name) + 1]
    return default


def main():
    global PORT, URL, DATA_DIR, SERVER, WINDOW
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    PORT = int(arg("--port", PORT))
    URL = f"http://{HOST}:{PORT}/"
    DATA_DIR = os.path.abspath(arg("--data", DATA_DIR))
    _compartir("PORT", "URL", "DATA_DIR")
    setup_logging(DATA_DIR)
    no_browser = "--no-browser" in sys.argv
    os.environ["CHULETA_DATOS"] = DATA_DIR  # el aviso flotante (proceso hijo) lee ahi sesion.token, tambien con --data
    # instancia unica, antes que el puerto: un segundo lanzamiento (doble clic, o el acceso directo otra vez porque la
    # ventana tarda) lo sabe al instante, aunque la primera copia aun no conteste
    instancia = instance_lock(DATA_DIR, PORT)
    estado = probe_running()
    if instancia is False and estado == "otra_version" and not _holder_is_port_owner():
        estado = "hermana"  # actualizacion con doble clic: la marca es de otra copia nueva, que retira la vieja
    if instancia is False and estado not in ("app", "otra_version"):  # la otra copia arranca, se cierra o tarda
        instancia, estado = wait_instance(estado)
    if estado == "otra_version":  # una version anterior seguia abierta (actualizacion en el sitio, acceso viejo)
        estado = retire_other_version()
        if estado == "grabando":
            fatal(f"Hay abierta una versión anterior de {APP_NAME} y está grabando.\n\nPara esa grabación "
                  f"(Ctrl+Alt+R o su botón Parar) y vuelve a abrir {APP_NAME}.")
        if instancia is False and estado == "libre":  # la marca era suya: el sistema la suelta al cerrarse
            instancia = instance_lock(DATA_DIR, PORT, wait=5.0)
            if instancia is False:
                log("   la versión anterior se ha cerrado pero su marca de instancia sigue: se continúa sin ella")
                instancia = None
    if instancia is False and estado != "otra_version":  # esta app ya corre o se esta abriendo en otra copia
        show_running(no_browser, estado)
        return
    srv = None if estado == "app" else reserve_port()  # el puerto, antes de App() (P1-13)
    if srv is None:
        show_running(no_browser)
        return
    try:
        start_session(DATA_DIR)
    except OSError as e:
        log_exc()
        fatal(f"Error al iniciar: no se puede escribir en {DATA_DIR} ({e}).")
    try:
        import truststore  # usa los certificados de Windows (proxies corporativos), si esta instalado
        truststore.inject_into_ssl()
    except Exception:
        pass
    # antes de cualquier ventana (la propia, el selector de carpetas o un aviso): la barra de tareas agrupa por este id
    log(f"AppUserModelID {APP_ID}: {'fijado' if set_app_id() else 'no disponible'}")
    # los modelos de Whisper los descarga el servidor (con truststore y los proxies, como Groq), con progreso real;
    # antes de App(): la precarga y /api/config ya miran ahi (engines.WhisperModels)
    engines.AgenteLocal.log = log  # tiempos y avisos de Claude Code (p. ej. un hijo que deja su salida abierta)
    engines.MODELS = engines.WhisperModels(os.path.join(DATA_DIR, "models", "whisper"), log=log,
                                           url=os.environ.get("CHULETA_PRUEBAS_URL_MODELOS") or engines.WHISPER_URL)
    if os.environ.get("CHULETA_PRUEBAS_URL_MODELOS"):
        log("PRUEBAS: los modelos de Whisper se descargan de una URL de pruebas (CHULETA_PRUEBAS_URL_MODELOS)")
    if os.environ.get("CHULETA_PRUEBAS_URL_GROQ"):  # E2E: un Groq falso en local; nunca una clave real en la CI
        base = os.environ["CHULETA_PRUEBAS_URL_GROQ"].rstrip("/")
        engines.PROVIDERS["groq"] = dict(engines.PROVIDERS["groq"], url=base + "/audio/transcriptions")
        engines.CHAT_URLS["groq"] = base + "/chat/completions"
        log("PRUEBAS: Groq va a una URL de pruebas (CHULETA_PRUEBAS_URL_GROQ)")
    try:
        Handler.app = App()
    except Exception as e:
        log_exc()
        fatal(f"Error al iniciar: {e.__class__.__name__}: {e}\n\nDetalle en {os.path.join(DATA_DIR, 'server.log')}")
    Handler.app.library.refresh()
    threading.Thread(target=Handler.app.transcripts.warm, args=(Handler.app.library,), daemon=True).start()
    # el modelo local del directo, cargado antes de que empiece la primera reunion (sin bloquear la interfaz)
    threading.Thread(target=Handler.app.jobs.preload, daemon=True, name="preload").start()
    SERVER = srv
    _compartir("SERVER")
    for line in ("=" * 64, f"  {APP_NAME.upper()}  ·  {URL}", f"  Carpeta: {ROOT}",
                 f"  Apagar: Ajustes > Apagar {APP_NAME} (con consola, también cerrándola o Ctrl+C).", "=" * 64):
        log(line)
    log(f"arranque · {URL} · carpeta {ROOT} · datos {DATA_DIR} · Python {sys.version.split()[0]} · pid "
        f"{os.getpid()}")  # el pid: con dos copias a la vez, las dos escriben en el mismo server.log
    if no_browser:  # para abrirla a mano (desarrollo): el token no va al log, que sale entero en el diagnostico
        log(f"  Interfaz: {URL}?t=<el contenido de {os.path.join(DATA_DIR, TOKEN_FILE)}>")
    ico = os.path.join(STATIC_DIR, "tenca.ico")
    # ocultarse al compartir pantalla: lo lee del ajuste al abrir la ventana (y Ajustes lo cambia al momento)
    WINDOW = None if no_browser else Window(ui_url(), DATA_DIR, ico,
                                            hide_from_capture=lambda: ocultar_al_compartir(Handler.app.settings.get()),
                                            tray=make_tray(ico))
    inicio_windows_al_dia()
    _compartir("WINDOW")
    if WINDOW is not None and WINDOW.available:
        # la ventana propia exige el hilo principal: el servidor pasa a un hilo y aqui se bloquea hasta apagar
        threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()
        if WINDOW.run():
            if WINDOW.quitting:
                return  # apagando: shutdown() termina el proceso
            log("la ventana se ha cerrado sin apagar: la app sigue en segundo plano")
        else:
            log("no se pudo abrir la ventana propia (detalle arriba): se abre en Edge")
            WINDOW = None
            _compartir("WINDOW")
            open_ui(inicio=True)
        try:
            while True:  # el servidor sigue en su hilo; con consola, Ctrl+C sale
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return
    WINDOW = None
    _compartir("WINDOW")
    if not no_browser:
        threading.Timer(0.3, open_ui, kwargs={"inicio": True}).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
