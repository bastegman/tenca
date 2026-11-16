# -*- coding: utf-8 -*-
"""Banco de la transcripción en la nube de punta a punta: peticiones por hora de audio, 429 y tiempo hasta el texto.

Arranca la app de verdad (`_app/server.py`, en una carpeta temporal con su copia de `_app`) contra un proveedor
simulado en local (la URL de pruebas de siempre, CHULETA_PRUEBAS_URL_GROQ) que limita a --rpm peticiones por minuto
y por clave: pasada la cuenta responde 429 con Retry-After (los segundos hasta que se libra un hueco), y apunta cada
petición que llega antes de lo que pidió un Retry-After anterior («a destiempo»). Cada respuesta tarda --latencia s
y lleva un segmento cada 5 s de la parte subida (la duración la lee de la cabecera del FLAC).

Las reuniones son WAV de --minutos con ráfagas de ruido de 8 s y 2 s de silencio (para que haya donde cortar) y
idioma «es-en» en su .md (--idioma), que es lo que cambia el tamaño de las partes; --trozo es cloud.chunk_minutes.

Escenarios:
  solo       una reunión con «Transcribir» en la nube
  retomado   al arrancar se retoma el repaso final pendiente de A (repasos_pendientes.json) y a los pocos segundos
             el usuario pide transcribir B: cuánto tarda el texto de B
  compartido como «solo», con otro consumidor de la misma clave gastando --otro peticiones/min (otro PC, el directo):
             cuántos 429 y cuántas peticiones a destiempo

Uso: python scripts/nube_trozos_bench.py --app RUTA/_app --escenario solo [--minutos 60] [--rpm 20] [--out f.json]
Necesita numpy y PyAV en el Python que lo ejecuta (es el que arranca la app). Sin red: todo es local.
"""
import argparse
import http.client
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLAVE = "gsk_banco_de_pruebas_0000000000"


def flac_segundos(body):
    """Duración de un FLAC por su STREAMINFO (o None)."""
    i = body.find(b"fLaC")
    if i < 0 or len(body) < i + 4 + 4 + 18:
        return None
    si = body[i + 8:i + 8 + 34]
    rate = (si[10] << 12) | (si[11] << 4) | (si[12] >> 4)
    total = ((si[13] & 0x0F) << 32) | struct.unpack(">I", si[14:18])[0]
    return total / rate if rate else None


class Proveedor:
    """El proveedor simulado: --rpm por clave en una ventana deslizante de 60 s."""

    def __init__(self, rpm, latencia, otro_rpm=0.0):
        self.rpm, self.latencia, self.lock = rpm, latencia, threading.Lock()
        self.ventana = []           # momentos de las peticiones aceptadas (las de la app y las del otro)
        self.prohibido_hasta = 0.0  # lo que pidio el ultimo Retry-After
        self.log = []               # (t, codigo, a_destiempo, segundos de audio)
        p = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                code, ra = p.admitir(time.time(), app=True, segundos=flac_segundos(body))
                if code == 200:
                    time.sleep(p.latencia)
                    dur = flac_segundos(body) or 5.0
                    segs = [{"id": k, "start": float(t), "end": float(min(dur, t + 4.5)), "text": f"frase {k}",
                             "avg_logprob": -0.1, "no_speech_prob": 0.01, "compression_ratio": 1.2}
                            for k, t in enumerate(range(0, max(1, int(dur)), 5))]
                    out = {"text": " ".join(s["text"] for s in segs), "language": "spanish", "duration": dur,
                           "segments": segs}
                else:
                    out = {"error": {"message": "Rate limit reached for requests", "type": "requests",
                                     "code": "rate_limit_exceeded"}}
                data = json.dumps(out).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if ra:
                    self.send_header("retry-after", str(ra))
                self.end_headers()
                self.wfile.write(data)

        class Srv(ThreadingHTTPServer):
            daemon_threads = True

        self.httpd = Srv(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}/openai/v1"
        if otro_rpm:
            threading.Thread(target=self.otro, args=(otro_rpm,), daemon=True).start()

    def admitir(self, t, app, segundos=None):
        with self.lock:
            self.ventana = [x for x in self.ventana if x > t - 60.0]
            destiempo = app and t < self.prohibido_hasta
            if len(self.ventana) >= self.rpm:
                ra = max(1, math.ceil(self.ventana[0] + 60.0 - t))
                if app:  # solo lo que se le ha dicho a la app cuenta para «a destiempo» (lo del otro no lo ve)
                    self.prohibido_hasta = max(self.prohibido_hasta, t + ra)
                    self.log.append((t, 429, destiempo, segundos))
                return 429, ra
            self.ventana.append(t)
            if app:
                self.log.append((t, 200, destiempo, segundos))
            return 200, None

    def otro(self, rpm):
        while True:
            self.admitir(time.time(), app=False)
            time.sleep(60.0 / rpm)


def wav_reunion(path, minutos, sr=16000):
    """Mono 16 bit: 8 s de ruido suave y 2 s de silencio, en bucle."""
    import numpy as np
    rng = np.random.default_rng(1)
    ciclo = np.concatenate([(rng.standard_normal(8 * sr) * 3000).astype(np.int16), np.zeros(2 * sr, np.int16)])
    n = int(minutos * 60 * sr)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        hecho = 0
        while hecho < n:
            trozo = ciclo[:min(len(ciclo), n - hecho)]
            w.writeframes(trozo.tobytes())
            hecho += len(trozo)


def puerto_libre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class App:
    def __init__(self, app_dir, minutos, nombres, pendiente=None, idioma="es-en", trozo=10):
        self.tmp = tempfile.mkdtemp(prefix="nube-trozos-")
        self.root = os.path.join(self.tmp, "Tenca")
        shutil.copytree(app_dir, os.path.join(self.root, "_app"), ignore=shutil.ignore_patterns("__pycache__"))
        self.data = os.path.join(self.tmp, "datos")
        os.makedirs(self.data)
        for n in nombres:
            wav_reunion(os.path.join(self.root, n + ".wav"), minutos)
            with open(os.path.join(self.root, n + ".md"), "w", encoding="utf-8") as f:
                f.write(f"---\nidioma: {idioma}\n---\n")
        st = {"procesado": "api", "preset": "nube", "live_preset": "nube", "summary_auto": False,
              "voices_enabled": False, "worker_preload": False,
              "cloud": {"provider": "groq", "api_key": CLAVE, "chunk_minutes": trozo, "parallel": 3}}
        with open(os.path.join(self.data, "settings.json"), "w", encoding="utf-8") as f:
            json.dump(st, f)
        if pendiente:
            with open(os.path.join(self.data, "repasos_pendientes.json"), "w", encoding="utf-8") as f:
                json.dump({pendiente + ".wav": {"local_part": False}}, f)
        self.port = puerto_libre()

    def arrancar(self, url):
        env = dict(os.environ, CHULETA_PRUEBAS_URL_GROQ=url, PYTHONUNBUFFERED="1")
        self.log = open(os.path.join(self.tmp, "consola.log"), "wb")
        self.proc = subprocess.Popen([sys.executable, os.path.join(self.root, "_app", "server.py"), "--port",
                                      str(self.port), "--data", self.data, "--no-browser"], env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        fin = time.time() + 60
        while time.time() < fin:
            try:
                self.get("/api/ping")
                return
            except OSError:
                time.sleep(0.3)
        raise RuntimeError("la app no arranca")

    def _h(self, post=False):
        with open(os.path.join(self.data, "sesion.token"), encoding="utf-8") as f:
            h = {"X-Chuleta-Token": f.read().strip()}
        if post:
            h.update({"X-Audios": "1", "Content-Type": "application/json"})
        return h

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path, headers=self._h() if os.path.exists(os.path.join(self.data, "sesion.token")) else {})
        r = c.getresponse()
        return json.loads(r.read() or b"{}")

    def post(self, path, body):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        c.request("POST", path, body=json.dumps(body), headers=self._h(True))
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")

    def trabajos(self):
        return self.get("/api/jobs").get("jobs") or []

    def parar(self):
        try:
            self.proc.kill()
            self.proc.wait(10)
        except Exception:
            pass
        self.log.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def esperar(app, cond, tope, paso=0.5):
    fin = time.time() + tope
    while time.time() < fin:
        js = app.trabajos()
        r = cond(js)
        if r:
            return r, js
        time.sleep(paso)
    return None, app.trabajos()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True)
    ap.add_argument("--escenario", choices=("solo", "retomado", "compartido"), default="solo")
    ap.add_argument("--minutos", type=float, default=60.0)
    ap.add_argument("--rpm", type=int, default=20)
    ap.add_argument("--latencia", type=float, default=0.5)
    ap.add_argument("--otro", type=float, default=8.0, help="peticiones/min del otro consumidor (compartido)")
    ap.add_argument("--tope", type=float, default=1800.0)
    ap.add_argument("--idioma", default="es-en", help="el de la reunion (su .md)")
    ap.add_argument("--trozo", type=int, default=10, help="cloud.chunk_minutes (2-20)")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    prov = Proveedor(a.rpm, a.latencia, a.otro if a.escenario == "compartido" else 0.0)
    nombres = ["A", "B"] if a.escenario == "retomado" else ["B"]
    app = App(os.path.abspath(a.app), a.minutos, nombres, pendiente="A" if a.escenario == "retomado" else None,
              idioma=a.idioma, trozo=a.trozo)
    res = {"escenario": a.escenario, "minutos": a.minutos, "rpm": a.rpm, "app": a.app, "idioma": a.idioma,
           "trozo_min": a.trozo}
    try:
        app.arrancar(prov.base)
        t0 = time.time()
        if a.escenario == "retomado":  # el repaso de A ya en marcha; entonces el usuario pide B
            ok, _ = esperar(app, lambda js: any(j.get("final_pass") and j["status"] == "running" for j in js), 60)
            res["a_en_marcha_s"] = round(time.time() - t0, 1) if ok else None
            time.sleep(2.0)
        t_pide = time.time()
        code, r = app.post("/api/transcribe", {"path": "B.wav", "preset": "nube"})
        res["pedir_b"] = code
        bid = (r.get("added") or [None])[0]
        empezado = []

        def b_hecho(js):
            b = next((j for j in js if j.get("id") == bid), None)
            if b and b["status"] == "running" and not empezado:
                empezado.append(time.time())
            return b if b and b["status"] in ("done", "error", "cancelled") else None
        b, js = esperar(app, b_hecho, a.tope)
        res["b_estado"] = b and b["status"]
        res["b_error"] = b and b.get("error")
        res["b_empieza_s"] = round(empezado[0] - t_pide, 1) if empezado else None
        res["b_texto_s"] = round(time.time() - t_pide, 1) if b else None
        if a.escenario == "retomado":
            ok, js = esperar(app, lambda js: all(j["status"] in ("done", "error", "cancelled") for j in js
                                                 if j.get("final_pass")), a.tope)
            res["a_texto_s"] = round(time.time() - t0, 1)
        with prov.lock:
            log = list(prov.log)
        horas = a.minutos / 60.0 * len(nombres)
        res.update(peticiones=len(log), ok=sum(1 for x in log if x[1] == 200), r429=sum(1 for x in log if x[1] == 429),
                   a_destiempo=sum(1 for x in log if x[2]),
                   peticiones_por_hora=round(len(log) / horas, 1),
                   partes_s=sorted({round(x[3]) for x in log if x[1] == 200 and x[3]})[:8])
    finally:
        app.parar()
        prov.httpd.shutdown()
    print(json.dumps(res, ensure_ascii=False), flush=True)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
