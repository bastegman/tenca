# -*- coding: utf-8 -*-
"""HTTP: el servidor local (Server) y sus rutas (Handler), con las comprobaciones de Host, Origin, Sec-Fetch-Site,
X-Audios y el token de sesion, la CSP y lo que Ajustes ensena de la ventana («Ocultar al compartir pantalla»).

Salio tal cual de server.py al partirlo (15/11/2026); server.py lo reexporta. PORT y WINDOW los fija main() al
arrancar: server.py los copia aqui (server.COMPARTIDAS)."""
import hmac
import json
import os
import re
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import live
import window as window_mod
from common import APP_NAME, ApiError, TOKEN_HEADER, log, log_exc
from inicio_windows import inicio_windows_leer, inicio_windows_poner
from rutas import AUDIO_EXT, MIME, STATIC_DIR, VERSION, alias_of, safe

SERVER = None  # el servidor HTTP; lo pone main()
WINDOW = None  # la ventana propia (None si se abre en Edge); la pone main()


HOST, PORT = "127.0.0.1", 8765
URL = f"http://{HOST}:{PORT}/"


# P0-2: la interfaz no lleva <script> en linea ni manejadores on…= (todo va en los .js), asi que un script colado en el
# HTML no llega a ejecutarse; los style="…" de las plantillas si hacen falta ('unsafe-inline' solo en estilos)
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "media-src 'self'; connect-src 'self'; frame-ancestors 'none'")
SLOW_REQUEST_S = 2.0


def slow_request(method, path, dt):
    """Una peticion de la API que tarda mas de SLOW_REQUEST_S queda en el log con su duracion: asi se ve que la
    interfaz esperaba al servidor, y en que (02/11/2026: /api/config tardo 8 min por una importacion y nada lo
    decia)."""
    if dt >= SLOW_REQUEST_S:
        log(f"   petición lenta: {method} {path} {dt:.1f} s")


DRAIN_MAX = 1 << 20  # P0-1: un cuerpo sin leer de hasta 1 MiB se lee y se descarta antes de responder (el error llega
# entero y la conexion sigue); uno mayor (una subida rechazada) o sin Content-Length cierra la conexion


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"{APP_NAME}/1.0"
    # cada conexion es un hilo de ThreadingHTTPServer: una keep-alive ociosa (o que deja de leer o de mandar a medias)
    # se cierra a los 60 s, y un cliente que deja conexiones abiertas ya no acumula hilos. El plazo es por operacion
    # del socket, no lo que tarda la app en contestar (el selector de carpetas espera al usuario)
    timeout = 60
    app = None
    token = None  # P1-02: el de esta sesion (lo pone main()); sin el, /api/* y /media responden 403
    body_read = False  # P0-1: el cuerpo de la peticion en curso se ha leido entero
    denied_logged = set()  # el primer 403 sin token de un navegador y el de un programa van al log, los demas no

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _origin_ok(self):
        """P0-1: un POST que llega de una pagina web solo vale si es la propia interfaz. El navegador manda Origin en
        todo POST y Sec-Fetch-Site (same-origin, same-site, cross-site o none); los programas (el acceso directo, el
        aviso flotante, el instalador, las pruebas) no mandan ninguna de las dos."""
        origin = self.headers.get("Origin")
        if origin is not None and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return False
        return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")

    def _cookie_name(self):
        return f"audios_sesion_{PORT}"  # con el puerto: las cookies no distinguen puertos (otra copia con --port)

    def _token_matches(self, given):
        tok = self.token
        return bool(tok and given) and hmac.compare_digest(str(given).encode("utf-8", "replace"), tok.encode("utf-8"))

    def _token_ok(self):
        """P1-02: la cabecera X-Chuleta-Token (programas) o la cookie de sesion (la interfaz, que la recibe al abrir
        /?t=<token>)."""
        if self._token_matches(self.headers.get(TOKEN_HEADER)):
            return True
        for raw in self.headers.get_all("Cookie") or []:
            for part in raw.split(";"):
                k, _, v = part.strip().partition("=")
                if k == self._cookie_name() and self._token_matches(v.strip()):
                    return True
        return False

    def _denied(self):
        quien = "navegador" if self.headers.get("Sec-Fetch-Mode") else "programa"
        if quien not in Handler.denied_logged:
            Handler.denied_logged.add(quien)
            log(f"   403 sin token de sesión ({quien}): {self.command} {urlparse(self.path).path} (solo el primero)")
        self._json({"error": f"Sesión no válida: vuelve a abrir {APP_NAME} con su acceso directo."}, 403)

    def _session_start(self, t):
        """GET /?t=<token> (la ventana propia y Edge abren esa direccion): si es el token de esta sesion, la cookie
        (HttpOnly, SameSite=Strict) y a "/", para que el token no se quede en la direccion; si no, a "/" sin cookie (la
        interfaz dira que hay que abrir la app con su acceso directo)."""
        extra = {"Location": "/"}
        if self._token_matches(t):
            extra["Set-Cookie"] = f"{self._cookie_name()}={self.token}; Path=/; HttpOnly; SameSite=Strict"
            log("   sesión: cookie entregada a la interfaz")
        else:
            log("   sesión: /?t= con un token que no es el de esta sesión (sin cookie)")
        self._send(303, b"", "text/plain; charset=utf-8", extra)

    def _close_if_unread(self):
        """P0-1: si la peticion traia un cuerpo que no se ha leido (un 403, un error antes de leerlo), se lee y se
        descarta (hasta DRAIN_MAX) o la conexion se cierra tras la respuesta. Con keep-alive, esos bytes se tomarian por
        la peticion siguiente, con sus propias cabeceras: asi una web podia colar un POST con Host y X-Audios dentro del
        cuerpo de otro."""
        te, cl = self.headers.get("Transfer-Encoding"), (self.headers.get("Content-Length") or "0").strip()
        if self.body_read or (not te and cl == "0"):
            return
        if not te and cl.isdigit() and int(cl) <= DRAIN_MAX:
            left = int(cl)
            try:
                self.connection.settimeout(5)
                while left > 0:
                    chunk = self.rfile.read(min(1 << 16, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                self.connection.settimeout(self.timeout)  # el de siempre (sin plazo, la conexion ociosa no se cierra)
            except OSError:
                left = -1
            if left == 0:
                self.body_read = True
                return
        self.send_header("Connection", "close")

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self._close_if_unread()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        self.body_read = False
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if "path" in q:  # una reunion que la app ha movido en esta sesion: a su ruta nueva
            q["path"] = alias_of(q["path"])
        if u.path in ("", "/") and "t" in q:
            return self._session_start(q["t"])
        if (u.path.startswith("/api/") or u.path == "/media") and not self._token_ok():
            return self._denied()
        a = self.app
        routes = {"/api/ping": lambda q: {"app": "audios", "version": VERSION, "pid": os.getpid()},
                  "/api/config": a.config, "/api/tree": a.tree,
                  "/api/transcript": a.transcript, "/api/doc": a.doc, "/api/search": a.search,
                  "/api/jobs": a.jobs_list, "/api/rec/devices": a.rec_devices, "/api/rec/status": a.rec_status,
                  "/api/notes": a.notes_get, "/api/tags": a.tags, "/api/summary": a.summary_get,
                  "/api/vocab": a.vocab, "/api/voces": a.voices_state, "/api/hablantes": a.speakers_get,
                  "/api/reunion/actual": a.reunion_actual, "/api/calendario/estado": a.calendario_estado,
                  "/api/hoy": a.hoy,
                  "/api/perfil": a.perfil_get,
                  "/api/diagnostico": a.diagnostico, "/api/modelos": a.modelos_state, "/api/probar": a.probar_estado,
                  "/api/agente/estado": a.agente_estado,
                  "/api/aprender": a.aprender_get, "/api/aprender/reglas": a.aprender_reglas,
                  "/api/movil/red": a.movil_red, "/api/movil/estado": a.movil_estado,
                  "/api/inicio-windows": lambda q: {"activado": inicio_windows_leer()},
                  "/api/ocultar-captura": lambda q: captura_estado(a.settings.get())}
        try:
            if u.path in routes:
                t0 = time.time()
                try:
                    return self._json(routes[u.path](q))
                finally:
                    slow_request("GET", u.path, time.time() - t0)
            if u.path == "/media":
                return self._media(safe(q.get("path"), "file"))
            return self._static(u.path)
        except ApiError as e:
            self._json({"error": e.msg}, e.code)
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception as e:
            log_exc()
            self._json({"error": str(e)}, 500)

    do_HEAD = do_GET

    def do_POST(self):
        self.body_read = False
        if not self._host_ok() or not self._origin_ok() or self.headers.get("X-Audios") != "1":
            return self._send(403, b"forbidden", "text/plain")
        if not self._token_ok():
            return self._denied()
        a = self.app
        routes = {"/api/transcribe": a.transcribe, "/api/cancel": a.cancel, "/api/jobs/clear": a.clear_jobs,
                  "/api/move": a.move, "/api/mkdir": a.mkdir, "/api/rename-folder": a.rename_folder,
                  "/api/reveal": a.reveal, "/api/open": a.open_file, "/api/settings": a.save_settings,
                  "/api/cloud/test": a.test_cloud, "/api/live": a.start_live, "/api/live/model": a.live_model,
                  "/api/agente/comprobar": a.agente_comprobar,
                  "/api/modelos/descargar": a.modelos_descargar, "/api/modelos/cancelar": a.modelos_cancelar,
                  "/api/probar": a.probar_empezar, "/api/probar/cancelar": a.probar_cancelar,
                  "/api/abrir-powershell": a.abrir_powershell,
                  "/api/live/traducir": a.live_translate, "/api/live/propuesta": a.live_propuesta,
                  "/api/rec/seguir": a.rec_seguir,
                  "/api/rec/siguiente": a.rec_siguiente,
                  "/api/rec/monitor": a.rec_monitor, "/api/rec/start": a.rec_start, "/api/rec/stop": a.rec_stop,
                  "/api/rec/toggle": a.rec_toggle, "/api/rec/mic": a.rec_mic, "/api/notes": a.notes_save,
                  "/api/grabar/plan": a.grabar_plan_api, "/api/grabar/recordar": a.grabar_recordar,
                  "/api/hoy/preparar": a.hoy_preparar,
                  "/api/summarize": a.summarize,
                  "/api/favorite": a.favorite, "/api/folder-icon": a.folder_icon, "/api/delete": a.delete,
                  "/api/transcript/save": a.save_transcript, "/api/transcript/restore": a.restore_transcript,
                  "/api/aprender/anadir": a.aprender_anadir, "/api/aprender/no": a.aprender_no,
                  "/api/aprender/deshacer": a.aprender_deshacer, "/api/aprender/olvidar": a.aprender_olvidar,
                  "/api/aprender/regla": a.aprender_regla,
                  "/api/export": a.export_package, "/api/link/pick": a.link_pick, "/api/link/add": a.link_add,
                  "/api/link/remove": a.link_remove, "/api/vocab/save": a.vocab_save,
                  "/api/vocab/delete": a.vocab_delete, "/api/vocab/check": a.vocab_check,
                  "/api/vocab/import": a.vocab_import, "/api/perfil": a.perfil_save,
                  "/api/proyecto/carpeta": a.carpeta_principal, "/api/reunion/destino": a.destino,
                  "/api/vocab/link/add": a.vocab_link_add, "/api/vocab/link/remove": a.vocab_link_remove,
                  "/api/voces/activar": a.voices_toggle, "/api/voces/identificar": a.voices_identify,
                  "/api/voces/persona": a.voices_person, "/api/voces/fusionar": a.voices_merge,
                  "/api/voces/olvidar": a.voices_forget, "/api/voces/reasignar": a.voices_reassign,
                  "/api/hablantes/unir": a.speakers_join,
                  "/api/voces/exportar": a.voices_export,
                  "/api/audio/mezclado": a.audio_mezclado,
                  "/api/apagar": a.apagar, "/api/mostrar": a.mostrar, "/api/ocultar": a.ocultar,
                  "/api/acerca/abrir": a.acerca_abrir, "/api/calendario/activar": a.calendario_activar,
                  "/api/movil/abrir": a.movil_abrir, "/api/movil/datos": a.movil_datos,
                  "/api/movil/cerrar": a.movil_cerrar,
                  "/api/inicio-windows": lambda b: {"activado": inicio_windows_poner(b.get("activado") is True)},
                  "/api/ocultar-captura": a.ocultar_captura}
        try:
            u = urlparse(self.path)
            if self.headers.get("Transfer-Encoding"):  # sin Content-Length no se sabe donde acaba el cuerpo (P0-1)
                raise ApiError("Cuerpo no válido: falta Content-Length", 411)
            if u.path == "/api/import":  # el cuerpo es el archivo tal cual (puede ser de 1 GB): se copia a trozos
                r = a.import_file(self, {k: v[0] for k, v in parse_qs(u.query).items()})
                self.body_read = True  # import_file lo ha leido entero (si se corta, lanza ApiError)
                return self._json(r)
            if u.path == "/api/subir":  # igual, pero subir marca body_read en cuanto lo ha leído entero: un audio
                # que no se puede leer se contesta al momento, sin esperar a vaciar un cuerpo que ya no queda
                q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
                return self._json(a.subir(self, q))
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0:
                raise ApiError("Cuerpo no válido")
            raw = self.rfile.read(n) if n else b""
            self.body_read = len(raw) == n
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ApiError("Cuerpo no válido: se esperaba un objeto JSON")
            if isinstance(body.get("path"), str):  # una reunion movida en esta sesion (unas notas que se guardan justo
                body["path"] = alias_of(body["path"])  # al moverla): a su ruta nueva
            route = routes.get(u.path)
            if not route:
                raise ApiError("No encontrado", 404)
            t0 = time.time()
            try:
                self._json(route(body))
            finally:
                slow_request('POST', u.path, time.time() - t0)
        except ApiError as e:
            self._json({"error": e.msg}, e.code)
        except Exception as e:
            log_exc()
            self._json({"error": str(e)}, 500)

    def _static(self, path):
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        p = os.path.normpath(os.path.join(STATIC_DIR, name))
        if not p.startswith(STATIC_DIR + os.sep) or not os.path.isfile(p):
            return self._send(404, b"not found", "text/plain")
        with open(p, "rb") as f:
            body = f.read()
        if name == "index.html":  # <title> y marca: el nombre de la app vive en common.APP_NAME
            body = body.replace(b"{{APP_NAME}}", APP_NAME.encode("utf-8"))
        self._send(200, body, MIME.get(os.path.splitext(p)[1].lower(), "application/octet-stream"))

    def _media(self, p):
        """Audio con soporte de Range: permite saltar a cualquier minuto al instante. Solo audio y video: un .html, un
        .js o un .svg de una carpeta vinculada, servido desde este origen, se saltaria la CSP (P0-2)."""
        if os.path.splitext(p)[1].lower() not in AUDIO_EXT:
            raise ApiError("Solo se sirven audios", 404)
        size = os.path.getsize(p)
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self._close_if_unread()
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", MIME.get(os.path.splitext(p)[1].lower(), "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        # P2-13: el audio de las reuniones no se queda en la cache de WebView2 (su perfil es persistente, en DATA_DIR)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self._close_if_unread()
        self.end_headers()
        if self.command == "HEAD":
            return
        # Grabacion de OBS en curso: la cabecera dice 0xFFFFFFFF; se sirve con el tamano de ahora
        # para que el navegador sepa cuanto dura lo grabado y se pueda escuchar
        patch = b""
        info = live.wav_info(p) if p.lower().endswith(".wav") else None
        if info and info["data_size"] is None and start < info["data_off"]:
            with open(p, "rb") as f:
                patch = bytearray(f.read(info["data_off"]))
            struct.pack_into("<I", patch, 4, size - 8)
            struct.pack_into("<I", patch, info["data_off"] - 4, size - info["data_off"])
        remaining = end - start + 1
        try:
            with open(p, "rb") as f:
                f.seek(start)
                pos = start
                while remaining > 0:
                    chunk = f.read(min(512 * 1024, remaining))
                    if not chunk:
                        break
                    if pos < len(patch):
                        k = min(len(patch) - pos, len(chunk))
                        chunk = patch[pos:pos + k] + chunk[k:]
                    self.wfile.write(chunk)
                    pos += len(chunk)
                    remaining -= len(chunk)
        except (ConnectionError, OSError):
            pass  # el navegador corta la conexion al saltar a otro punto: normal


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # en Windows SO_REUSEADDR permitiria dos servidores en el mismo puerto

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):  # el navegador cierra conexiones: normal
            super().handle_error(request, client_address)


def ocultar_al_compartir(st):
    """El ajuste «Ocultar Tenca al compartir pantalla» (por defecto, sí)."""
    return (st or {}).get("ocultar_al_compartir", True) is not False


def captura_estado(st):
    """Para Ajustes: {activado (el ajuste), soportado, motivo (por qué no se puede), aplicado (lo que tiene la ventana
    ahora), error}. Sin la ventana propia (se abrió en Edge, o con --no-browser) no se puede: esa ventana no es de este
    proceso."""
    ok, motivo = window_mod.capture_support()
    out = {"activado": ocultar_al_compartir(st), "soportado": ok, "motivo": motivo, "aplicado": None, "error": None}
    if ok and WINDOW is None:
        out.update(soportado=False, motivo=f"Solo con la ventana propia de {APP_NAME}: ahora se ha abierto en el "
                   "navegador y esa ventana no es suya.")
    elif WINDOW is not None:
        out.update(aplicado=WINDOW.capture_applied, error=WINDOW.capture_error)
    return out
