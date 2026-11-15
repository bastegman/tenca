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
import errno  # noqa: F401 (fachada: los tests usan server.errno)
import json
import logging  # noqa: F401 (fachada: los tests usan server.logging)
import os
import platform  # noqa: F401 (fachada: los tests usan server.platform)
import shutil  # noqa: F401 (fachada: los tests usan server.shutil)
import subprocess  # noqa: F401 (fachada: los tests usan server.subprocess)
import sys
import threading
import time
import webbrowser  # noqa: F401 (fachada: los tests usan server.webbrowser)

import calendario
import directo_avisos
import engines
import live  # noqa: F401 (fachada: los tests usan server.live)
import movil
import voices
from common import (APP_ID, APP_NAME, ApiError, BAD_NAME, LOGGER, SRT_TIME, WINDOWS,  # noqa: F401 (re-exportados)
                    alert, clean_name, default_threads, fmt_srt, fold, is_protected, log, log_exc, natural_key,
                    parse_srt, protect, read_text, save_text, setup_logging, unprotect, wav_duration, write_bytes)
from common import TOKEN_FILE, TOKEN_HEADER  # noqa: F401 (fachada: server.TOKEN_HEADER en los tests)
from recording import (Cortes, GlobalHotkey, cue, notify)
from recording import RecorderProcess  # noqa: F401 (fachada: los tests usan server.RecorderProcess)
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
from app_textos import (  # noqa: F401 (fachada: server.X sigue valiendo)
    Textos)
from app_directo import (  # noqa: F401 (fachada: server.X sigue valiendo)
    Directo)
from app_ajustes import (  # noqa: F401 (fachada: server.X sigue valiendo)
    Ajustes)
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
PARTES = ("ajustes", "apagado", "api", "app_ajustes", "app_carpetas", "app_directo",
          "app_grabar", "app_hoy", "app_listas", "app_reuniones", "app_textos", "aprendido",
          "biblioteca", "carpetas", "diagnostico", "inicio_windows", "instancia", "listas",
          "paquete", "rutas", "trabajos")
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


class App(Grabar, Reuniones, Listas, Carpetas, Hoy, Textos, Directo, Ajustes):
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
