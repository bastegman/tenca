# -*- coding: utf-8 -*-
"""Grabadora de mentira para tests/test_grabadora.py y tests/test_ui_grabadora.py (no es un test): se copia encima de
_app/recorder.py y hace lo que diga el fichero JSON de FAKE_REC_CTL, una entrada por arranque:

  {"modos": ["muere", "vive"], "muere_s": 1.0, "runs": "/ruta/arranques.txt"}

  vive   escribe un WAV con la cabecera abierta, manda su estado cada 0,25 s y para con «stop» (cierra la cabecera)
  muere  igual, pero a los muere_s se muere de golpe con un traceback en stderr (como un OSError que nadie captura)
  fatal  igual, pero dice el motivo («fatal»: el disco esta lleno) antes de terminar, como recorder.py
  nace   no llega a grabar: se muere al arrancar

Fuera de FAKE_REC_CTL (--list, --monitor) hace lo minimo para que la app no se queje."""
import json
import os
import struct
import sys
import threading
import time


def main():
    args = sys.argv[1:]
    out = sys.stdout
    if "--list" in args:
        out.write(json.dumps({"mics": [{"id": "m", "name": "Micro", "default": True}],
                              "outputs": [{"id": "s", "name": "Altavoces", "default": True}]}) + "\n")
        return 0
    ctl = json.load(open(os.environ["FAKE_REC_CTL"], encoding="utf-8"))
    with open(ctl["runs"], "a", encoding="utf-8") as f:
        f.write((args[0] if args and not args[0].startswith("--") else "--monitor") + "\n")
    with open(ctl["runs"], encoding="utf-8") as f:
        n = len(f.read().splitlines())
    modos = ctl.get("modos") or ["vive"]
    modo = modos[min(n - 1, len(modos) - 1)]
    path = args[0] if args and not args[0].startswith("--") else None
    if modo == "nace":
        sys.stderr.write("Traceback (most recent call last):\nRuntimeError: no hay dispositivos\n")
        return 1
    stop, muted = threading.Event(), [False]

    def leer():
        for line in sys.stdin:
            if line.strip() in ("mute", "unmute"):
                muted[0] = line.strip() == "mute"
            if ctl.get("ordenes"):  # lo que le manda el servidor (mute, unmute, stop), con la grabacion
                with open(ctl["ordenes"], "a", encoding="utf-8") as fo:
                    fo.write(f"{os.path.basename(path or '')}\t{line.strip()}\n")
            if line.strip() == "stop":
                break
        stop.set()
    threading.Thread(target=leer, daemon=True).start()
    f = None
    if path:
        f = open(path, "wb")
        f.write(b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" + b"fmt "
                + struct.pack("<IHHIIHH", 16, 1, 2, 48000, 192000, 4, 16) + b"data" + struct.pack("<I", 0xFFFFFFFF))
        f.flush()
    t0, t = time.time(), 0.0
    while not stop.is_set():
        t = round(time.time() - t0, 2)
        if f:
            f.write(b"\0" * 48000)  # 0,25 s de silencio
            f.flush()
        out.write(json.dumps({"t": t, "mic": {"db": -20, "name": "Micro"}, "sys": {"db": -60, "name": "Altavoces"},
                              "mic_muted": muted[0], "disco_min": ctl.get("disco_min")}) + "\n")
        out.flush()
        if modo in ("muere", "fatal") and t >= ctl.get("muere_s", 1.0):
            if modo == "fatal":
                out.write(json.dumps({"fatal": "el disco está lleno"}) + "\n")
                out.flush()
                sys.stderr.write(time.strftime("%H:%M:%S") + " ✖ la grabadora se para: el disco está lleno\n")
                sys.stderr.flush()
            else:
                sys.stderr.write("Traceback (most recent call last):\nOSError: [Errno 5] Input/output error\n")
                sys.stderr.flush()
            os._exit(1)  # de golpe: sin cerrar la cabecera
        time.sleep(0.25)
    if f:
        size = f.tell()
        f.seek(4)
        f.write(struct.pack("<I", size - 8))
        f.seek(40)
        f.write(struct.pack("<I", size - 44))
        f.close()
    out.write(json.dumps({"done": True, "t": t}) + "\n")
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
