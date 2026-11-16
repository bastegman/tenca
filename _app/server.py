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
import errno  # noqa: F401 (fachada: los tests usan server.errno)
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
import fallos
import live  # noqa: F401 (fachada: los tests usan server.live)
import movil
import voices  # noqa: F401 (fachada: los tests usan server.voices)
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
    apagando, arranca, ask_yes_no, esperar_salida, make_tray, schedule_shutdown, shutdown, tell, tray_abrir,
    tray_balloon, tray_estado, tray_grabar, tray_salir)
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
from app_voces import (  # noqa: F401 (fachada: server.X sigue valiendo)
    Voces)
from app_conservar import Conservar  # punto 17 del review: qué pasa con el audio de cada reunión
from window import Window, set_app_id
# fachada: lo que server.py importaba antes de partirse (15/11/2026) y ya no usa aqui sigue siendo server.X (tests y
# ramas que lo parchean o lo leen)
import contextlib  # noqa: F401,E402
import copy  # noqa: F401,E402
import hashlib  # noqa: F401,E402
import hmac  # noqa: F401,E402
import itertools  # noqa: F401,E402
import json  # noqa: F401,E402
import queue  # noqa: F401,E402
import re  # noqa: F401,E402
import secrets  # noqa: F401,E402
import struct  # noqa: F401,E402
import unicodedata  # noqa: F401,E402
import urllib.error  # noqa: F401,E402
import urllib.request  # noqa: F401,E402
import uuid  # noqa: F401,E402
from collections import Counter  # noqa: F401,E402
from datetime import datetime, timedelta  # noqa: F401,E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: F401,E402
from urllib.parse import parse_qs, urlparse  # noqa: F401,E402

import aprender  # noqa: F401,E402
import explicar  # noqa: F401,E402
import plan as grabar_plan  # noqa: F401,E402
import probar  # noqa: F401,E402
import salidas  # noqa: F401,E402
import window as window_mod  # noqa: F401,E402
from bandeja import Bandeja  # noqa: F401,E402
from common import local_opener, read_token  # noqa: F401,E402
from recording import disco_al_empezar, teams_mic_estado  # noqa: F401,E402


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
PARTES = ("ajustes", "apagado", "api", "app_ajustes", "app_carpetas", "app_conservar", "app_directo",
          "app_grabar", "app_hoy", "app_listas", "app_reuniones", "app_textos", "app_voces",
          "aprendido", "biblioteca", "carpetas", "conservar", "diagnostico", "inicio_windows", "instancia",
          "listas", "paquete", "rutas", "trabajos", "voces_directo",
          # nuevos, no salieron de server.py (lote 2a del motor, 15/11/2026): informe usa DATA_DIR (_compartir)
          "despierto", "fallos", "informe",
          # nuevo, no salió de server.py: «Mis pendientes» (punto 28 del review, 15/11/2026)
          "pendientes")
COMPARTIDAS = ("PORT", "URL", "DATA_DIR", "SERVER", "WINDOW", "COMPLETE")  # COMPLETE: lo crea App()


def _partes():
    """Los modulos de PARTES, tal como los importa server.py (por su nombre: sys.modules[n] es el mismo que lee
    `from n import …` arriba). Sin comparar carpetas: con un enlace en la ruta, __file__ de server.py y el de sus
    partes no coinciden (/var y /private/var) y un modulo se quedaria fuera sin decir nada."""
    return [m for m in (sys.modules.get(n) for n in PARTES) if m is not None]


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


# la app (python server.py) no lo necesita: nadie le asigna nada desde fuera; y solo si este modulo es el registrado
# con su nombre (no al cargarlo con spec_from_file_location sin registrarlo)
if __name__ != "__main__" and getattr(sys.modules.get(__name__), "__dict__", None) is globals():
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


class App(Grabar, Reuniones, Listas, Carpetas, Hoy, Textos, Directo, Ajustes, Voces, Conservar):
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
        # cambiar la carpeta en pleno directo (B1c): se mueve cuando la reunion queda libre (al acabar su ultimo
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
        # «Ya ha empezado «X» · Grabar · Ahora no» al parar una grabación (B7): lo que se grababa (para no
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
        """Al arrancar: reparar_proveedor. Se mira sobre una copia y solo se escribe si hay algo que
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
        valer). Solo en una red privada de Windows (decisión 3 de movil.py)."""
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
    arranca()  # nada se esta apagando (apagado.py)
    PORT = int(arg("--port", PORT))
    URL = f"http://{HOST}:{PORT}/"
    DATA_DIR = os.path.abspath(arg("--data", DATA_DIR))
    _compartir("PORT", "URL", "DATA_DIR")
    setup_logging(DATA_DIR)
    fallos.ganchos()  # lo que se escape de un hilo o del principal, a server.log (punto 33)
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
    fallos.instalar(DATA_DIR)  # ya es LA app: faulthandler a fallos-graves.log (punto 33)
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
    # los modelos de Whisper los descarga el servidor (con truststore y los proxies, como la nube), con progreso real;
    # antes de App(): la precarga y /api/config ya miran ahi (engines.WhisperModels)
    engines.AgenteLocal.log = log  # tiempos y avisos de Claude Code (p. ej. un hijo que deja su salida abierta)
    engines.MODELS = engines.WhisperModels(os.path.join(DATA_DIR, "models", "whisper"), log=log,
                                           url=os.environ.get("CHULETA_PRUEBAS_URL_MODELOS") or engines.WHISPER_URL)
    if os.environ.get("CHULETA_PRUEBAS_URL_MODELOS"):
        log("PRUEBAS: los modelos de Whisper se descargan de una URL de pruebas (CHULETA_PRUEBAS_URL_MODELOS)")
    if os.environ.get("CHULETA_PRUEBAS_URL_GROQ"):  # E2E: un proveedor falso en local; nunca una clave real en la CI
        base = os.environ["CHULETA_PRUEBAS_URL_GROQ"].rstrip("/")
        engines.PROVIDERS["groq"] = dict(engines.PROVIDERS["groq"], url=base + "/audio/transcriptions")
        engines.CHAT_URLS["groq"] = base + "/chat/completions"
        log("PRUEBAS: el proveedor de fábrica va a una URL de pruebas (CHULETA_PRUEBAS_URL_GROQ)")
    try:
        Handler.app = App()
    except Exception as e:
        log_exc()
        fatal(f"Error al iniciar: {e.__class__.__name__}: {e}\n\nDetalle en {os.path.join(DATA_DIR, 'server.log')}")
    Handler.app.library.refresh()
    threading.Thread(target=Handler.app.transcripts.warm, args=(Handler.app.library,), daemon=True).start()
    # el modelo local del directo, cargado antes de que empiece la primera reunion (sin bloquear la interfaz)
    threading.Thread(target=Handler.app.jobs.preload, daemon=True, name="preload").start()
    # las huellas de voz de un modelo anterior (al actualizar), rehechas con el de ahora desde sus reuniones, en segundo
    # plano y sin grabar a la vez (app_voces.Voces.rehacer_huellas)
    threading.Thread(target=Handler.app.rehacer_huellas_al_arrancar, daemon=True, name="rehacer-huellas").start()
    # el audio de las reuniones con «Guardar 15 días» o «Borrar al terminar», a la papelera cuando toque (punto 17)
    threading.Thread(target=Handler.app.audio_vigia, daemon=True, name="audio-conservar").start()
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
    _compartir("WINDOW")
    inicio_windows_al_dia()
    if WINDOW is not None and WINDOW.available:
        # la ventana propia exige el hilo principal: el servidor pasa a un hilo y aqui se bloquea hasta apagar
        threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()
        if WINDOW.run():
            if WINDOW.quitting:  # apagando: shutdown() termina el proceso; este hilo no sale antes (apagado.py)
                if apagando():
                    esperar_salida()
                return
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
    else:
        if apagando():  # el servidor ha parado porque se apaga: lo termina shutdown() (o este hilo, si tarda)
            esperar_salida()


if __name__ == "__main__":
    main()
