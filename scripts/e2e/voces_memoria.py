# -*- coding: utf-8 -*-
"""«Quién habla» con una reunión larga: pico de memoria del subproceso de voces, con la app de este commit y, si se da,
con la de antes (fix-subir-memoria, 09/11/2026: con un audio de 55 min, la de `fe29522` llegaba a ~2,3 GB y en un
portátil acabó en «bad allocation»).

    python scripts/e2e/voces_memoria.py --salida DIR [--minutos 55] [--antes DIR_APP] [--tope MB]

1. Sintetiza turnos con las voces del sistema (SAPI en Windows; `say` en macOS) y los repite hasta --minutos (WAV
   16 kHz mono).
2. Baja los modelos de voces con voices.ensure_models (sha256 anclado) a DIR/datos.
3. Ejecuta `voices.py --diarize` con cada app en un proceso nuevo que, al acabar, mide su propio pico: en Windows,
   PeakPagefileUsage (memoria comprometida, la que se acaba en un «bad allocation») y PeakWorkingSetSize; en macOS,
   ri_lifetime_max_phys_footprint y ru_maxrss.
   La misma reunión en dos pistas (la primera voz al micro, las demás a lo que suena, con la línea de «Tú»):
   tampoco puede pasar de --tope, y tiene que salir «Tú» del micro y los demás de lo que suena.
4. La huella de «Recordar» (`voices.py --fingerprint h1`, fix-lento, 10/11/2026) con la reunión en m4a, con cada app:
   la de ahora solo lee los tramos de la huella y tiene que tardar menos que la de antes, con la misma huella
   (coseno >= 0,99). La de antes decodificaba la reunión entera (40 s con 3 h en un Mac; minutos en un portátil).
5. Tras actualizar (con --antes): la app de antes guarda una persona con su huella de «Recordar» de esa reunión en su
   voces.json; la de ahora arranca de verdad (server.py) con ese voces.json y, sin tocar nada, la huella se rehace
   con el modelo de ahora desde el audio de la reunión (app_voces.rehacer_huellas): la misma que da «Recordar» con
   la de ahora (coseno >= 0,99), con el nombre, «Soy yo» y el consentimiento de antes.
6. Escribe DIR/voces_memoria.json y sale con 1 si la de ahora no baja de la de antes, pasa de --tope MB o no rehace
   la huella guardada.
Necesita sherpa-onnx, numpy, PyAV y requests (lo que instala INSTALAR.bat para «Quién habla»).
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRASES = (
    "Good morning everyone, let's start with the status of the platform migration and the milestones for this quarter.",
    "Thanks. On my side, the data team has finished moving the reports and we are validating them with the business.",
    "I am worried about the second phase, because we depend on the vendor delivering the integrations on time.",
    "I spoke with them yesterday and they promise a first version on the fifteenth, but without the load tests.",
    "Then I suggest we run the load tests ourselves in parallel, so that we do not lose two weeks waiting.",
    "Agreed, but we will need a staging environment with anonymised data, and that takes time to prepare.",
)

# en el proceso hijo: voices.main y, al acabar, su propio pico de memoria
HIJO = r'''
import ctypes, json, os, sys
app = sys.argv[1]
sys.path.insert(0, app)
os.chdir(app)
import voices
rc = voices.main(sys.argv[3:])
out = {"rc": rc}
if sys.platform == "win32":
    class PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
            (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                           "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                           "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
    c = PMC()
    c.cb = ctypes.sizeof(c)
    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    ok = k32.K32GetProcessMemoryInfo(ctypes.c_void_p(k32.GetCurrentProcess()), ctypes.byref(c), c.cb)
    out.update(ok=bool(ok), comprometida_mb=round(c.PeakPagefileUsage / 2**20), working_set_mb=round(
        c.PeakWorkingSetSize / 2**20))
elif sys.platform == "darwin":
    import resource
    buf = (ctypes.c_uint64 * 64)()
    ctypes.CDLL("/usr/lib/libproc.dylib").proc_pid_rusage(os.getpid(), 4, ctypes.byref(buf))
    out.update(comprometida_mb=round(list(buf)[2:][28] / 2**20),
               working_set_mb=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20))
with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump(out, f)
'''


def sintetizar(dir_, minutos):
    """WAV de --minutos con turnos de las voces del sistema que haya (al menos una)."""
    turnos = []
    if sys.platform == "win32":
        ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }")
        voces = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                               timeout=120).stdout.split("\n")
        voces = [v.strip() for v in voces if v.strip()][:3]
        print("voces:", voces, flush=True)
        q = lambda t: t.replace("'", "''")  # noqa: E731 - comillas simples de PowerShell («let's»: run 42691646093)
        for i, frase in enumerate(FRASES):
            v, p = voces[i % len(voces)], os.path.join(dir_, f"t{i}.wav")
            ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  f"$s.SelectVoice('{q(v)}'); $f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
                  "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
                  "[System.Speech.AudioFormat.AudioChannel]::Mono);"
                  f" $s.SetOutputToWaveFile('{q(p)}', $f); $s.Speak('{q(frase)}'); $s.Dispose()")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, timeout=120)
            turnos.append(p)
    else:
        voces = [v for v in ("Samantha", "Daniel", "Karen", "Alex") if subprocess.run(
            ["say", "-v", v, ""], capture_output=True).returncode == 0][:3] or [None]
        for i, frase in enumerate(FRASES):
            v, p = voces[i % len(voces)], os.path.join(dir_, f"t{i}.wav")
            subprocess.run(["say", "-o", p + ".aiff"] + (["-v", v] if v else []) + [frase], check=True, timeout=120)
            subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", p + ".aiff", p], check=True,
                           timeout=60)
            turnos.append(p)
    pcm = []
    for p in turnos:
        with wave.open(p, "rb") as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2), p
            pcm.append(w.readframes(w.getnframes()) + b"\0\0" * 4800)  # 0,3 s entre turnos
    ronda = b"".join(pcm)
    n = int(minutos * 60 * 16000 * 2)
    audio = (ronda * (n // len(ronda) + 1))[:n]
    path = os.path.join(dir_, "reunion.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(audio)
    sintetizar.turnos, sintetizar.voces = pcm, len(voces)  # para dos_pistas
    return path


def dos_pistas(dir_, minutos):
    """La misma reunión grabada en dos pistas (pistas.py, desde el 15/11/2026): los turnos de la primera voz al micro
    (canal izquierdo) y los demás a lo que suena (derecho), y en su sidecar la línea de «Tú» de la grabadora (la
    energía de la izquierda por pasos de 0,25 s, «desfase» 0). Devuelve la ruta del WAV."""
    import numpy as np
    sys.path.insert(0, os.path.join(REPO, "_app"))
    import pistas
    izq, der = [], []  # la primera voz «eres tú»
    for i, t in enumerate(sintetizar.turnos):
        x = np.frombuffer(t, "<i2")
        z = np.zeros_like(x)
        izq.append(x if i % sintetizar.voces == 0 else z)
        der.append(z if i % sintetizar.voces == 0 else x)
    ronda = np.stack([np.concatenate(izq), np.concatenate(der)], axis=1)
    n = int(minutos * 60 * 16000)
    x = np.concatenate([ronda] * (n // len(ronda) + 1))[:n]
    path = os.path.join(dir_, "pistas.wav")
    head = bytearray(pistas.cabecera(16000))
    pcm = np.ascontiguousarray(x, "<i2").tobytes()
    head[4:8] = (len(head) - 8 + len(pcm)).to_bytes(4, "little")
    head[-4:] = len(pcm).to_bytes(4, "little")
    with open(path, "wb") as f:
        f.write(bytes(head) + pcm)
    paso = 4000
    e = np.sqrt(np.mean(x[:len(x) // paso * paso, 0].astype(np.float64).reshape(-1, paso) ** 2, axis=1))
    bits = "".join("1" if v > 300 else "0" for v in e)
    with open(os.path.join(dir_, "pistas.hablantes.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "micro": {"paso": 0.25, "activo": bits, "desfase": 0.0, "fuente": "grabadora"}}, f)
    return path


def a_m4a(wav, destino):
    """El WAV sintetizado, en m4a (AAC, como las notas de voz del móvil): decodificarlo entero cuesta de verdad."""
    import av
    with av.open(wav) as src, av.open(destino, "w", format="mp4") as dst:
        s = dst.add_stream("aac", rate=16000)
        s.layout = "mono"
        for fr in src.decode(audio=0):
            fr.pts = None
            for pk in s.encode(fr):
                dst.mux(pk)
        for pk in s.encode(None):
            dst.mux(pk)
    return destino


def huella(app, audio, datos, side, etiqueta):
    """voices.py --fingerprint h1 (el subproceso de «Recordar» desde la etiqueta): segundos y la huella."""
    t0 = time.time()
    r = subprocess.run([sys.executable, os.path.join(app, "voices.py"), "--diarize", audio, "--out", side,
                        "--fingerprint", "h1", "--data", datos, "--threads", "2"], capture_output=True, timeout=1800,
                       cwd=app)
    proto = [json.loads(line) for line in r.stdout.decode("utf-8", "replace").splitlines() if line.startswith("{")]
    res = next((m for m in proto if m.get("type") == "result"), {})
    out = {"segundos": round(time.time() - t0, 2), "vector": (res.get("huellas") or {}).get("h1"),
           "error": next((m["error"] for m in proto if m.get("type") == "error"), None)}
    if not out["vector"]:
        sys.stderr.write(r.stderr.decode("utf-8", "replace")[-3000:])
    print(etiqueta, "huella", {k: v for k, v in out.items() if k != "vector"}, flush=True)
    return out


# con la app de antes: una persona con su huella de «Recordar» (y la del usuario, sin huella), como en una instalación
# de antes del cambio de modelo de huellas
VOCES_DE_ANTES = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import voices
v = voices.Voices(sys.argv[2])
yo = v.create("Yo", es_usuario=True, consentimiento="2026-11-01T10:00:00")
p = v.create("Persona Prueba", consentimiento="2026-11-01T10:00:00")
v.remember(p["id"], json.loads(sys.argv[3]), 60.0, sys.argv[4], "2026-11-01T10:00:00")
print(json.dumps({"persona": p["id"], "yo": yo["id"], "modelo": voices.MODEL_ID}))
'''


def tras_actualizar(antes, m4a, side, datos, trabajo, vec_antes, vec_ahora, espera=900):
    """El paso 5: el voces.json de la de antes, la de ahora arrancada con él y la huella rehecha sola."""
    import numpy as np
    sys.path.insert(0, os.path.join(REPO, "_app"))
    import voices
    raiz, datos2 = os.path.join(trabajo, "Tenca"), os.path.join(trabajo, "datos_actualizado")
    shutil.copytree(os.path.join(REPO, "_app"), os.path.join(raiz, "_app"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(os.path.join(datos, "models"), os.path.join(datos2, "models"))  # los modelos ya bajados
    nombre = "2026-11-02 Proyecto.m4a"
    shutil.copy(m4a, os.path.join(raiz, nombre))
    voces_json = os.path.join(datos2, "voces.json")
    r = subprocess.run([sys.executable, "-c", VOCES_DE_ANTES, os.path.abspath(antes), voces_json,
                        json.dumps(vec_antes), nombre], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return {"error": f"la de antes no ha guardado la persona: {r.stderr[-800:]}"}
    ids = json.loads(r.stdout.strip().splitlines()[-1])
    with open(side, encoding="utf-8") as f:  # el sidecar de la reunión, con h1 puesto a mano (el de «Recordar»)
        data = json.load(f)
    data["hablantes"]["h1"].update(persona=ids["persona"], origen="manual", provisional=False)
    with open(os.path.join(raiz, "2026-11-02 Proyecto.hablantes.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)
    with open(os.path.join(datos2, "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"voices_enabled": True}, f)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    salida = open(os.path.join(trabajo, "server_actualizado.out"), "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, os.path.join(raiz, "_app", "server.py"), "--port", str(port), "--data",
                             datos2, "--no-browser"], cwd=raiz, stdout=salida, stderr=subprocess.STDOUT,
                            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    t0, out = time.time(), {"modelo_antes": ids["modelo"], "modelo_ahora": voices.MODEL_ID}
    try:
        persona = None
        while time.time() - t0 < espera and proc.poll() is None:
            time.sleep(3)
            try:
                with open(os.path.join(datos2, "sesion.token"), encoding="utf-8") as f:
                    token = f.read().strip()
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/voces", headers={"X-Chuleta-Token": token})
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as resp:
                    estado = json.load(resp)
            except (OSError, ValueError):
                continue
            persona = next((p for p in estado.get("personas") or [] if p["id"] == ids["persona"]), None)
            if persona and persona["huellas"] >= 1 and not persona["por_rehacer"]:
                break
        out.update(segundos=round(time.time() - t0, 1), persona=persona, server_vivo=proc.poll() is None)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(20)
            except subprocess.TimeoutExpired:
                proc.kill()
        salida.close()
    v = voices.Voices(voces_json)  # lo que quedó escrito (DPAPI: el mismo usuario)
    p = v.find(ids["persona"]) or {}
    yo = v.find(ids["yo"]) or {}
    huellas = p.get("huellas") or []
    out.update(nombre=p.get("nombre"), consentimiento=p.get("consentimiento"), yo_es_usuario=yo.get("es_usuario"),
               huellas=len(huellas), por_rehacer=len(p.get("rehacer") or []),
               modelo_huella=huellas[0].get("modelo") if huellas else None)
    if huellas and vec_ahora:
        a, b = np.asarray(huellas[0]["vector"]), np.asarray(vec_ahora)
        out["coseno_con_recordar_de_ahora"] = round(float(a @ b / np.linalg.norm(a) / np.linalg.norm(b)), 5)
    try:
        with open(os.path.join(datos2, "server.log"), encoding="utf-8", errors="replace") as f:
            out["log"] = [line.rstrip() for line in f if "huella" in line or "voces.json" in line][-12:]
    except OSError:
        out["log"] = []
    print("tras actualizar", {k: v for k, v in out.items() if k != "log"}, flush=True)
    for line in out["log"]:
        print("   ", line, flush=True)
    return out


def medir(app, audio, datos, salida, etiqueta, sidecar=None):
    res, side = os.path.join(salida, f"{etiqueta}.json"), os.path.join(salida, f"{etiqueta}.hablantes.json")
    if sidecar:  # el de la grabadora (la línea de «Tú»), como lo deja la app junto al audio
        shutil.copy(sidecar, side)
    t0 = time.time()
    r = subprocess.run([sys.executable, "-c", HIJO, app, res, "--diarize", audio, "--out", side, "--data", datos,
                        "--threads", "2"], capture_output=True, timeout=3600)
    if not os.path.isfile(res):  # el hijo no llegó al final: lo que dijo, para leerlo en la CI
        sys.stderr.write(r.stderr.decode("utf-8", "replace")[-4000:])
        raise SystemExit(f"{etiqueta}: el proceso de voces terminó con {r.returncode} sin resultado")
    with open(res, encoding="utf-8") as f:
        out = json.load(f)
    with open(side, encoding="utf-8") as f:
        d = json.load(f)
    out.update(segundos=round(time.time() - t0), hablantes=sum(1 for h in d["hablantes"].values() if not h["breve"]),
               pistas=bool(d.get("pistas")),
               por_pista={p: sum(1 for h in d["hablantes"].values() if not h["breve"] and h.get("pista") == p)
                          for p in ("micro", "sistema")})
    proto = [json.loads(line) for line in r.stdout.decode("utf-8", "replace").splitlines() if line.startswith("{")]
    out["error"] = next((m["error"] for m in proto if m.get("type") == "error"), None)
    print(etiqueta, out, flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--salida", required=True)
    ap.add_argument("--minutos", type=float, default=55)
    ap.add_argument("--antes", help="carpeta _app de la versión de antes (p. ej. la de fe29522)")
    ap.add_argument("--tope", type=float, default=None, help="MB de memoria comprometida que la de ahora no pasa")
    a = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):  # las líneas de server.log llevan iconos (🗣): en Windows, cp1252
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    a.salida = os.path.abspath(a.salida)  # el hijo cambia de carpeta: todas las rutas, absolutas (run 42692402938)
    os.makedirs(a.salida, exist_ok=True)
    # el audio (~105 MB con 55 min) y los modelos, fuera de --salida: si algo falla a medias, el artefacto de la CI
    # (if: always()) no se los lleva (revisión 2)
    trabajo = tempfile.mkdtemp(prefix="voces-memoria-")
    try:
        return medir_todo(a, trabajo)
    finally:
        shutil.rmtree(trabajo, ignore_errors=True)


def medir_todo(a, trabajo):
    audio = sintetizar(trabajo, a.minutos)
    datos = os.path.join(trabajo, "datos")
    sys.path.insert(0, os.path.join(REPO, "_app"))
    import voices
    voices.ensure_models(datos)
    modelo_antes = voices.MODEL_ID
    if a.antes:  # la de antes, con sus modelos (desde el 15/11/2026 el de huellas es otro: CAM++ en vez de TitaNet)
        r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import voices; "
                            "voices.ensure_models(sys.argv[2]); print(voices.MODEL_ID)", os.path.abspath(a.antes),
                            datos], capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise SystemExit(f"no se pudieron bajar los modelos de la de antes: {r.stderr[-500:]}")
        modelo_antes = r.stdout.strip().splitlines()[-1]
    out = {"minutos": a.minutos, "ahora": medir(os.path.join(REPO, "_app"), audio, datos, a.salida, "ahora")}
    # la misma reunión en dos pistas (punto 20 del review del 14/11/2026): no puede gastar más memoria
    dual = dos_pistas(trabajo, a.minutos)
    out["ahora_pistas"] = medir(os.path.join(REPO, "_app"), dual, datos, a.salida, "ahora_pistas",
                                sidecar=os.path.join(trabajo, "pistas.hablantes.json"))
    out["modelo_ahora"], out["modelo_antes"] = voices.MODEL_ID, modelo_antes
    if a.antes:
        out["antes"] = medir(os.path.abspath(a.antes), audio, datos, a.salida, "antes")
    m4a = a_m4a(audio, os.path.join(trabajo, "reunion.m4a"))
    side = os.path.join(trabajo, "reunion.hablantes.json")  # junto al m4a: el sidecar de la diarizacion de ahora
    shutil.copy(os.path.join(a.salida, "ahora.hablantes.json"), side)
    out["huella_ahora"] = huella(os.path.join(REPO, "_app"), m4a, datos, side, "ahora")
    if a.antes:
        out["huella_antes"] = huella(os.path.abspath(a.antes), m4a, datos, side, "antes")
        v1, v0 = out["huella_ahora"]["vector"], out["huella_antes"]["vector"]
        if v1 and v0:
            import numpy as np
            v1, v0 = np.asarray(v1), np.asarray(v0)
            out["huella_coseno"] = round(float(v1 @ v0 / np.linalg.norm(v1) / np.linalg.norm(v0)), 5)
    if a.antes and out["huella_antes"]["vector"]:
        out["tras_actualizar"] = tras_actualizar(a.antes, m4a, side, datos, trabajo, out["huella_antes"]["vector"],
                                                 out["huella_ahora"]["vector"])
    with open(os.path.join(a.salida, "voces_memoria.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    mal = []
    if out["ahora"]["rc"] != 0 or out["ahora"]["error"]:
        mal.append(f"la de ahora ha fallado: {out['ahora']['error']}")
    if a.antes and out["ahora"]["comprometida_mb"] >= out["antes"]["comprometida_mb"]:
        mal.append("la de ahora no gasta menos que la de antes")
    if a.tope and out["ahora"]["comprometida_mb"] > a.tope:
        mal.append(f"la de ahora pasa de {a.tope:.0f} MB")
    dp = out["ahora_pistas"]
    if dp["rc"] != 0 or dp["error"] or not dp["pistas"]:
        mal.append(f"en dos pistas ha fallado o no se ha diarizado por pistas: {dp['error']} {dp['pistas']}")
    elif dp["por_pista"]["micro"] != 1 or dp["por_pista"]["sistema"] < 1:
        mal.append(f"en dos pistas, hablantes por canal raros: {dp['por_pista']}")
    if a.tope and dp["comprometida_mb"] > a.tope:
        mal.append(f"en dos pistas pasa de {a.tope:.0f} MB")
    print("dos pistas:", dp["comprometida_mb"], "MB y", dp["segundos"], "s; la mezcla:",
          out["ahora"]["comprometida_mb"], "MB y", out["ahora"]["segundos"], "s", flush=True)
    h1, h0 = out["huella_ahora"], out.get("huella_antes")
    if not h1["vector"]:
        mal.append(f"la huella de ahora ha fallado: {h1['error']}")
    elif h0 and h0["vector"]:
        print("huella: ahora", h1["segundos"], "s, antes", h0["segundos"], "s, coseno", out["huella_coseno"],
              flush=True)
        if h1["segundos"] >= h0["segundos"]:
            mal.append("la huella de ahora no tarda menos que la de antes")
        # la misma huella solo con el mismo modelo (con otro, el coseno no dice nada)
        if out["modelo_ahora"] == out["modelo_antes"] and out["huella_coseno"] < 0.99:
            mal.append(f"la huella de ahora no es la de antes (coseno {out['huella_coseno']})")
        if len(h1["vector"]) != voices.DIM:
            mal.append(f"la huella de ahora no tiene {voices.DIM} valores")
    ta = out.get("tras_actualizar")
    if a.antes and not ta:
        mal.append("no se ha probado la actualización: la huella de la de antes ha fallado")
    elif ta:
        if ta.get("error"):
            mal.append(f"tras actualizar: {ta['error']}")
        elif (ta.get("huellas"), ta.get("por_rehacer"), ta.get("modelo_huella")) != (1, 0, voices.MODEL_ID):
            mal.append(f"tras actualizar la huella no se ha rehecho: {ta.get('huellas')} huellas, "
                       f"{ta.get('por_rehacer')} por rehacer, modelo {ta.get('modelo_huella')}")
        elif (ta.get("coseno_con_recordar_de_ahora") or 0) < 0.99:
            mal.append(f"tras actualizar la huella no es la de «Recordar» ({ta.get('coseno_con_recordar_de_ahora')})")
        quien = (ta.get("nombre"), ta.get("yo_es_usuario"), bool(ta.get("consentimiento")))
        if quien != ("Persona Prueba", True, True):
            mal.append(f"tras actualizar se ha perdido algo de la persona: {ta.get('nombre')}, "
                       f"{ta.get('yo_es_usuario')}, {ta.get('consentimiento')}")
    for m in mal:
        print("ERROR:", m, flush=True)
    return 1 if mal else 0


if __name__ == "__main__":
    sys.exit(main())
