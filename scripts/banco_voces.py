# -*- coding: utf-8 -*-
"""Banco de reuniones para medir «Quién habla» con scripts/eval_voces.py más cerca del caso real: reuniones de horas,
con más personas, en español y con el audio comprimido de Teams. Solo con audio público y licencias abiertas; los audios
no van al repositorio.

    python scripts/banco_voces.py serie  --out SERIE.wav  A.wav A.rttm B.wav B.rttm …    (encadena reuniones)
    python scripts/banco_voces.py montar --out REUNION.wav [--minutos 20] [--semilla 1] VOZ_DIR VOZ_DIR …
    python scripts/banco_voces.py teams  ENTRADA.wav SALIDA.wav [--kbps 24]
    python scripts/banco_voces.py insertar --out R.wav --quien X [--segundos 60] BASE.wav BASE.rttm OTRA.wav OTRA.rttm

- `serie`: varias reuniones del mismo grupo una detrás de otra (p. ej. las cuatro de una serie del AMI Meeting Corpus,
  CC BY 4.0, con las mismas cuatro personas: ~2 h), con 2 s de silencio entre ellas, y su RTTM con los tiempos
  desplazados. Así se prueba la ruta de bloques de 30 min enlazados por huella (voices.BLOQUE_S) con 1-3 h.
- `montar`: una reunión de varias voces a partir de frases sueltas de cada persona (una carpeta de WAV por persona;
  p. ej. OpenSLR 71, voces chilenas, CC BY-SA 4.0). Turnos de 1-3 frases, con quién habla después al azar (unos hablan
  más que otros), pausas de 0,2-0,9 s y algún solape corto. La RTTM marca solo la voz de cada frase (energía por tramas
  de 10 ms), no el turno entero con sus silencios: así el DER vale.
- `insertar`: la reunión base con alguien más que habla poco (p. ej. 1 min en una serie de 2 h), con intervenciones
  suyas sacadas de otra reunión: ¿sale como persona aparte o se le atribuye a otro?
- `teams`: pasa el audio por Opus a 24 kbps (lo que usa Teams con poca red), con PyAV, y lo deja otra vez en WAV de
  16 kHz.
"""
import argparse
import os
import random
import sys
import wave

import numpy as np

SR = 16000
STEP = 0.01
SUELO_DB = 20.0  # potencia por trama (muestras int16) por debajo de la cual nunca hay voz: un RMS de 10 (-70 dBFS)


def read_wav(path):
    """int16 16 kHz mono (WAV PCM 16 bit; si no, con PyAV a través de la app)."""
    try:
        with wave.open(path, "rb") as w:
            if w.getsampwidth() == 2 and w.getframerate() == SR and w.getnchannels() == 1:
                return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").copy()
    except wave.Error:
        pass
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_app"))
    import engines
    return np.asarray(engines.decode_16k(path), dtype=np.int16)


def write_wav(path, x):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.asarray(x, dtype="<i2").tobytes())


def read_rttm(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.split()
            if len(p) >= 8 and p[0] == "SPEAKER":
                out.append((float(p[3]), float(p[4]), p[7]))
    return out


def write_rttm(path, name, spans):
    with open(path, "w", encoding="utf-8") as f:
        for a, d, who in sorted(spans):
            f.write(f"SPEAKER {name} 1 {a:.3f} {d:.3f} <NA> <NA> {who} <NA> <NA>\n")


# ----------------------------------------------------------------------------- serie

def serie(pares, out, hueco_s=2.0):
    """Encadena (wav, rttm) en un WAV y su RTTM. Devuelve la duración en segundos."""
    partes, spans, t = [], [], 0.0
    gap = np.zeros(int(hueco_s * SR), np.int16)
    for wav, rttm in pares:
        x = read_wav(wav)
        spans += [(a + t, d, who) for a, d, who in read_rttm(rttm)]
        partes += [x, gap]
        t += (len(x) + len(gap)) / SR
    write_wav(out, np.concatenate(partes[:-1]))
    write_rttm(os.path.splitext(out)[0] + ".rttm", os.path.splitext(os.path.basename(out))[0], spans)
    return t - hueco_s


# ----------------------------------------------------------------------------- montar

def voz(x, umbral_db=-35.0, puente_s=0.25, min_s=0.1):
    """Tramos con voz de una frase [(a, b)] en segundos: tramas de 10 ms por encima de umbral_db respecto a la más
    fuerte, con los huecos cortos (< puente_s) unidos y sin trozos de menos de min_s."""
    n = int(STEP * SR)
    k = len(x) // n
    if not k:
        return []
    fr = x[:k * n].astype(np.float32).reshape(k, n)
    db = 10 * np.log10(np.mean(fr * fr, axis=1) + 1e-9)
    on = db > max(db.max() + umbral_db, SUELO_DB)
    tramos, i = [], 0
    while i < k:
        if on[i]:
            j = i
            while j < k and on[j]:
                j += 1
            tramos.append([i * STEP, j * STEP])
            i = j
        else:
            i += 1
    unidos = []
    for a, b in tramos:
        if unidos and a - unidos[-1][1] < puente_s:
            unidos[-1][1] = b
        else:
            unidos.append([a, b])
    return [(a, b) for a, b in unidos if b - a >= min_s]


def recortar(x):
    """La frase sin el silencio del principio y del final (deja 0,1 s)."""
    t = voz(x)
    if not t:
        return None
    a, b = max(0, int((t[0][0] - 0.1) * SR)), min(len(x), int((t[-1][1] + 0.1) * SR))
    return x[a:b]


def frases_de(carpeta):
    fs = sorted(f for f in os.listdir(carpeta) if f.lower().endswith(".wav"))
    out = []
    for f in fs:
        x = recortar(read_wav(os.path.join(carpeta, f)))
        if x is not None and len(x) > 0.5 * SR:
            out.append(x)
    return out


def montar(carpetas, out, minutos=20.0, semilla=1, solape=0.12, nivel_db=6.0):
    """Reunión montada con las voces de cada carpeta. Cada persona, con un nivel algo distinto (±nivel_db/2), como en
    una llamada. Devuelve (duración, segundos de voz por persona)."""
    rnd = random.Random(semilla)
    voces = {os.path.basename(os.path.normpath(c)): frases_de(c) for c in carpetas}
    voces = {k: v for k, v in voces.items() if v}
    if len(voces) < 2:
        raise SystemExit("Hacen falta al menos dos carpetas con frases")
    pesos = {k: rnd.uniform(0.5, 1.5) for k in voces}  # unos hablan más que otros
    gana = {k: 10 ** (rnd.uniform(-nivel_db / 2, nivel_db / 2) / 20) for k in voces}
    pos = {k: 0 for k in voces}
    total = int(minutos * 60 * SR)
    buf = np.zeros(total + 60 * SR, np.float32)
    spans, t, antes, hablado = [], int(0.5 * SR), None, {k: 0.0 for k in voces}
    while t < total:
        libres = [k for k in voces if k != antes and pos[k] < len(voces[k])]
        if not libres:
            break
        quien = rnd.choices(libres, [pesos[k] for k in libres])[0]
        for _ in range(rnd.choice((1, 1, 2, 2, 3))):
            if pos[quien] >= len(voces[quien]) or t >= total:
                break
            x = voces[quien][pos[quien]].astype(np.float32) * gana[quien]
            pos[quien] += 1
            buf[t:t + len(x)] += x
            spans += [(t / SR + a, b - a, quien) for a, b in voz(x)]
            hablado[quien] += len(x) / SR
            t += len(x) + int(rnd.uniform(0.05, 0.3) * SR)
        # siguiente turno: pausa, o a veces se pisan (empieza antes de que acabe el anterior)
        t += int(-rnd.uniform(0.3, 1.0) * SR) if rnd.random() < solape else int(rnd.uniform(0.2, 0.9) * SR)
        antes = quien
    fin = max(int(spans[-1][0] * SR + spans[-1][1] * SR) + SR // 2, t) if spans else t
    y = buf[:fin]
    pico = float(np.max(np.abs(y))) or 1.0
    if pico > 32000:  # sin saturar (la RTTM no cambia)
        y *= 32000 / pico
    write_wav(out, np.clip(y, -32768, 32767).astype(np.int16))
    write_rttm(os.path.splitext(out)[0] + ".rttm", os.path.splitext(os.path.basename(out))[0], spans)
    return len(y) / SR, hablado


# ----------------------------------------------------------------------------- insertar (alguien que habla poco)

def insertar(base_wav, base_rttm, fuente_wav, fuente_rttm, quien, out, segundos=60.0, trozos=6, min_s=4.0):
    """La reunión base con una persona más que habla poco: `trozos` intervenciones suyas de la fuente (sola, sin
    solape, de >= min_s), recortadas hasta sumar ~`segundos`, repartidas por la reunión. Cada una entra en un silencio
    de la base (entre dos turnos), con 0,3 s antes y después: lo de detrás se retrasa y su RTTM se desplaza igual.
    Devuelve (duración, segundos suyos)."""
    x, ref = read_wav(base_wav), sorted(read_rttm(base_rttm))
    src, sref = read_wav(fuente_wav), read_rttm(fuente_rttm)
    otros = [(a, a + d) for a, d, k in sref if k != quien]
    solos = sorted(((a, a + d) for a, d, k in sref if k == quien and d >= min_s
                    and not any(o0 < a + d and o1 > a for o0, o1 in otros)), key=lambda t: -(t[1] - t[0]))
    if not solos:
        raise SystemExit(f"{quien} no tiene intervenciones solas de >= {min_s} s en la fuente")
    largo = max(min_s, segundos / max(1, trozos))
    clips = [(a, min(b, a + largo)) for a, b in sorted(solos[:trozos], key=lambda t: t[0])]
    dur = len(x) / SR
    fin, huecos = 0.0, []  # silencios de la base (>= 0,6 s sin nadie): su punto medio
    for a, d, _ in ref:
        if a - fin >= 0.6:
            huecos.append((fin + a) / 2)
        fin = max(fin, a + d)
    if dur - fin >= 0.6:
        huecos.append((fin + dur) / 2)
    pausa = np.zeros(int(0.3 * SR), np.int16)
    partes, spans, desde, extra, ins = [], [], 0, 0.0, []
    for k, (a, b) in enumerate(clips):
        objetivo = dur * (k + 1) / (len(clips) + 1)
        # nunca en medio de alguien: sin silencio libre, al final
        t = min((h for h in huecos if h * SR > desde), key=lambda h: abs(h - objetivo), default=dur)
        i = max(desde, int(t * SR))
        clip = src[int(a * SR):int(b * SR)]
        partes += [x[desde:i], pausa, clip, pausa]
        add = (2 * len(pausa) + len(clip)) / SR
        spans.append((i / SR + extra + 0.3, len(clip) / SR, quien))
        ins.append((i / SR, add))
        extra += add
        desde = i
    partes.append(x[desde:])
    for a, d, k in ref:
        spans.append((a + sum(add for t, add in ins if t <= a), d, k))
    y = np.concatenate(partes)
    write_wav(out, y)
    write_rttm(os.path.splitext(out)[0] + ".rttm", os.path.splitext(os.path.basename(out))[0], spans)
    return len(y) / SR, sum(b - a for a, b in clips)


# ----------------------------------------------------------------------------- teams

def teams(entrada, salida, kbps=24):
    """Opus a kbps (libopus de PyAV, 16 kHz mono, tramas de 20 ms) y de vuelta a WAV de 16 kHz."""
    import av
    x = read_wav(entrada)
    tmp = salida + ".opus.ogg"
    with av.open(tmp, "w", format="ogg") as oc:
        st = oc.add_stream("libopus", rate=SR)
        st.bit_rate = int(kbps * 1000)
        st.layout = "mono"
        n = 320 * 50  # 1 s por paquete de entrada; el codificador lo parte en sus tramas
        for i in range(0, len(x), n):
            fr = av.AudioFrame.from_ndarray(x[i:i + n].reshape(1, -1), format="s16", layout="mono")
            fr.sample_rate = SR
            for p in st.encode(fr):
                oc.mux(p)
        for p in st.encode(None):
            oc.mux(p)
    try:
        y = []
        with av.open(tmp) as ic:
            rs = av.AudioResampler(format="s16", layout="mono", rate=SR)
            for fr in ic.decode(audio=0):
                for r in rs.resample(fr):
                    y.append(r.to_ndarray().reshape(-1))
            for r in rs.resample(None):
                y.append(r.to_ndarray().reshape(-1))
        y = np.concatenate(y) if y else np.zeros(0, np.int16)
        bytes_opus = os.path.getsize(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    write_wav(salida, y[:len(x)])
    r = os.path.splitext(entrada)[0] + ".rttm"
    if os.path.isfile(r):  # la misma RTTM (el codec no mueve el audio más de unos ms)
        with open(r, encoding="utf-8") as f, open(os.path.splitext(salida)[0] + ".rttm", "w", encoding="utf-8") as g:
            name = os.path.splitext(os.path.basename(salida))[0]
            for line in f:
                p = line.split()
                if len(p) >= 8 and p[0] == "SPEAKER":
                    p[1] = name
                    g.write(" ".join(p) + "\n")
    return bytes_opus * 8 / max(1e-9, len(x) / SR) / 1000  # kbps reales


def main(argv=None):
    ap = argparse.ArgumentParser(description="Banco de reuniones para scripts/eval_voces.py")
    sub = ap.add_subparsers(dest="op", required=True)
    s = sub.add_parser("serie")
    s.add_argument("--out", required=True)
    s.add_argument("pares", nargs="+")
    m = sub.add_parser("montar")
    m.add_argument("--out", required=True)
    m.add_argument("--minutos", type=float, default=20.0)
    m.add_argument("--semilla", type=int, default=1)
    m.add_argument("voces", nargs="+")
    i = sub.add_parser("insertar")
    i.add_argument("--out", required=True)
    i.add_argument("--quien", required=True, help="la persona de la fuente que se mete")
    i.add_argument("--segundos", type=float, default=60.0)
    i.add_argument("--trozos", type=int, default=6)
    i.add_argument("base_wav")
    i.add_argument("base_rttm")
    i.add_argument("fuente_wav")
    i.add_argument("fuente_rttm")
    t = sub.add_parser("teams")
    t.add_argument("entrada")
    t.add_argument("salida")
    t.add_argument("--kbps", type=float, default=24)
    a = ap.parse_args(argv)
    if a.op == "serie":
        if len(a.pares) % 2:
            raise SystemExit("Pares WAV RTTM")
        d = serie(list(zip(a.pares[::2], a.pares[1::2])), a.out)
        print(f"{a.out}: {d / 60:.1f} min")
    elif a.op == "montar":
        d, h = montar(a.voces, a.out, a.minutos, a.semilla)
        print(f"{a.out}: {d / 60:.1f} min, {len(h)} voces: " + ", ".join(f"{k} {v:.0f} s" for k, v in h.items()))
    elif a.op == "insertar":
        d, h = insertar(a.base_wav, a.base_rttm, a.fuente_wav, a.fuente_rttm, a.quien, a.out, a.segundos, a.trozos)
        print(f"{a.out}: {d / 60:.1f} min, {a.quien} habla {h:.0f} s")
    else:
        print(f"{a.salida}: Opus a {teams(a.entrada, a.salida, a.kbps):.1f} kbps reales")


if __name__ == "__main__":
    main()
