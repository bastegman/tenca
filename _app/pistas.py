# -*- coding: utf-8 -*-
"""Grabación propia en dos pistas (punto 20 del review del 14/11/2026).

El WAV de la grabadora (recorder.py --pistas) tiene el mismo formato y tamaño que antes (48 kHz, estéreo, 16 bits),
pero los dos canales ya no llevan la misma mezcla:

  - canal izquierdo: el micro (tu voz), nivelado como antes (recorder.MicLeveler);
  - canal derecho: lo que suena en el PC (los demás), nivelado como antes y en mono.

Su suma es la mezcla de siempre: lo que lee el directo, la cola y la nube (a_mono). La marca va en la propia cabecera
(un trozo LIST/INFO con un comentario, MARCA): viaja con el archivo, no hay que leer nada más para saberlo y la ve
también el reproductor (pistas.js, con una petición Range de la cabecera). Los WAV antiguos y los audios subidos no la
llevan y se leen como siempre.

Después de la reunión: «Tú» sale del micro (la decisión de la grabadora bloque a bloque, recorder.PasosTu, ya alineada
con el archivo) y «Quién habla» separa por un lado a los demás (canal derecho) y por otro lo que el micro recoge y no es
eco (voices.diarize_pistas). El reproductor mezcla los dos canales con Web Audio, con «Tú» y «Los demás» por separado y
el eco del micro quitado al reproducir; «Exportar audio mezclado» hace lo mismo a un WAV normal (exportar_mezcla).
numpy puro y sin dependencias de la app: lo importan la grabadora (proceso aparte), el directo y el motor.
"""
import os
import struct

SR = 48000
OPEN = 0xFFFFFFFF
MARCA = b"tenca:pistas=micro,sistema"
# lo que se ve en las propiedades del archivo en Windows («Comentarios»): ASCII, por si lo lee otro programa
COMENTARIO = (b"Tenca, dos pistas: izquierda = tu micro, derecha = el audio del PC (los demas). Para oirlo mezclado, "
              b"Exportar audio mezclado en Tenca. " + MARCA)
CABECERA_MAX = 4096  # la cabecera entera cabe aqui (live.wav_info lee lo mismo)


def cabecera(sr=SR):
    """Cabecera del WAV de dos pistas, abierta (tamaños 0xFFFFFFFF) como la de siempre: RIFF, fmt (PCM, 2 canales,
    16 bits), LIST/INFO con el comentario y la marca, y data. El audio empieza en len(cabecera())."""
    txt = COMENTARIO + b"\0"
    icmt = b"ICMT" + struct.pack("<I", len(txt)) + txt + (b"\0" if len(txt) % 2 else b"")
    info = b"INFO" + icmt
    return (b"RIFF" + struct.pack("<I", OPEN) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, sr, sr * 4, 4, 16)
            + b"LIST" + struct.pack("<I", len(info)) + info
            + b"data" + struct.pack("<I", OPEN))


def marca_en(head):
    """¿Esta cabecera (los primeros bytes de un WAV) es de una grabación en dos pistas? Recorre los trozos hasta «data»:
    la marca tiene que estar en un LIST/INFO antes del audio y el formato ser PCM de 16 bits en estéreo."""
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return False
    off, fmt_ok, marca = 12, False, False
    while off + 8 <= len(head):
        cid, n = head[off:off + 4], struct.unpack("<I", head[off + 4:off + 8])[0]
        if cid == b"data":
            return fmt_ok and marca
        if n == OPEN:
            return False
        body = head[off + 8:off + 8 + n]
        if cid == b"fmt " and len(body) >= 16:
            tag, ch, _, _, _, bits = struct.unpack("<HHIIHH", body[:16])
            fmt_ok = tag == 1 and ch == 2 and bits == 16
        elif cid == b"LIST" and body[:4] == b"INFO" and MARCA in body:
            marca = True
        off += 8 + n + (n & 1)
    return False


def es_doble(path):
    """¿El archivo es un WAV de la grabadora en dos pistas? Solo lee la cabecera (False si no se puede leer)."""
    if not str(path).lower().endswith(".wav"):
        return False
    try:
        with open(path, "rb") as f:
            return marca_en(f.read(CABECERA_MAX))
    except OSError:
        return False


def soft_limit(x, knee=0.8):
    """Por encima de 0,8 se curva suavemente hacia 1 en vez de recortar en seco (sin distorsion dura). En el sitio."""
    import numpy as np
    a = np.abs(x)
    over = a > knee
    if over.any():
        x[over] = np.sign(x[over]) * (knee + (1 - knee) * np.tanh((a[over] - knee) / (1 - knee)))
    return x


def soft_unlimit(y, knee=0.8):
    """La inversa de soft_limit (lo que habia antes del limitador), en una copia float32. Lo que el limitador dejo en
    1 (o el redondeo a 16 bits, casi en 1) no tiene vuelta exacta: se toma como 1 + 1,45 (atanh de 1 - 1e-6)."""
    import numpy as np
    y = np.array(y, np.float32)
    a = np.abs(y)
    over = a > knee
    if over.any():
        z = np.minimum((a[over] - knee) / (1 - knee), 1 - 1e-6)
        y[over] = np.sign(y[over]) * (knee + (1 - knee) * np.arctanh(z))
    return y


def a_mono(x):
    """(n, 2) float de un WAV de dos pistas → (n,) float: la suma, que es la mezcla de antes (micro + lo que suena),
    con el mismo limitador. La media de los dos canales (lo de cualquier otro estéreo) la dejaría 6 dB más baja. Cada
    canal se grabó con su limitador: se deshace antes de sumar, para que el resultado sea el limitador sobre la suma,
    como antes, y no el limitador dos veces (medido el 15/11/2026: hasta 0,048 de diferencia en los picos
    de una sala)."""
    import numpy as np
    y = soft_unlimit(x[:, 0]) + soft_unlimit(x[:, 1])
    soft_limit(y)
    np.clip(y, -1.0, 1.0, out=y)
    return y


def a_mono_i16(x):
    """Lo mismo con int16 (n, 2) → int16 (n,) (la lectura a 16 kHz de engines.decode_16k)."""
    import numpy as np
    y = a_mono(np.asarray(x, np.float32) / 32768.0)
    return np.round(y * 32767).astype(np.int16)


# ----------------------------------------------------------------------------- «Tú» desde el micro

PASO = 0.25  # la linea de tiempo del micro: un bit por paso (como voices.MIC_PASO)


def tramos_de_bits(bits, paso=PASO, hueco_s=0.5, min_s=0.3, margen_s=0.1):
    """Los tramos [(a, b)] en segundos en los que la línea de tiempo del micro dice «es tu voz» («1»): huecos de
    menos de hueco_s unidos, sin trozos de menos de min_s y con margen_s a cada lado (el bit se decide por paso de
    0,25 s)."""
    out = []
    i, n = 0, len(bits or "")
    while i < n:
        if bits[i] != "1":
            i += 1
            continue
        j = i
        while j < n and bits[j] == "1":
            j += 1
        a, b = i * paso, j * paso
        if out and a - out[-1][1] <= hueco_s:
            out[-1][1] = b
        else:
            out.append([a, b])
        i = j
    res = []
    for a, b in out:
        if b - a >= min_s:
            a, b = max(0.0, a - margen_s), b + margen_s
            if res and a <= res[-1][1]:
                res[-1] = (res[-1][0], b)
            else:
                res.append((a, b))
    return res


# ----------------------------------------------------------------------------- quitar el eco al reproducir o exportar

# El micro con altavoces lleva el eco de la reunión (con el nivelador del 14/11, 12-17 dB por debajo de la voz de los
# demás). Al reproducir y al exportar, el canal del micro se baja ECO_DB mientras no pasa de ECO_MARGEN_DB por debajo de
# lo más fuerte que ha sonado en el PC en los últimos ECO_COLA_S (la cola del eco en la sala y el retardo de unos
# altavoces Bluetooth, como eco.MAX_LAG_S), si eso pasa de ECO_SOBRE_DB: es eco, no tu voz. Tu voz, sola o a la vez que
# ellos, pasa (con el nivelador está a su nivel). Baja y sube en unos 10-20 ms: entre dos palabras de ellos sigue bajado
# por la cola, y cuando hablas tú se abre al momento. pistas-worklet.js (el reproductor) hace lo mismo con estos
# números. No se graba: se puede quitar.
ECO_DB = -18.0
ECO_SOBRE_DB = -45.0
ECO_MARGEN_DB = -6.0
ECO_VENTANA_S = 0.02    # nivel de cada canal: RMS de 20 ms
ECO_COLA_S = 0.6        # lo que suena se mira en las ultimas 0,6 s (30 ventanas)
ECO_ATAQUE_S = 0.01     # baja en ~10 ms
ECO_SUELTA_S = 0.02     # y sube en ~20 ms (el reproductor retrasa el audio una ventana: decide a tiempo, como aqui)


def ganancias_eco(x, sr=SR, estado=None):
    """Ganancia del canal del micro por muestra (float32, 1 = sin tocar) para el bloque x (n, 2) float de un WAV de dos
    pistas. `estado` (un dict) guarda entre bloques la ganancia, lo pendiente y los niveles recientes de lo que suena
    (lo usa exportar_mezcla: por bloques da lo mismo que de una vez)."""
    import numpy as np
    st = estado if estado is not None else {}
    w = max(1, int(round(ECO_VENTANA_S * sr)))
    k = max(1, int(round(ECO_COLA_S / ECO_VENTANA_S)))
    x = np.asarray(x, np.float32)
    if not len(x):
        return np.ones(0, np.float32)
    # las muestras que no llenaron la ultima ventana se guardan para el bloque siguiente (resto)
    resto = st.get("resto")
    xx = x if resto is None or not len(resto) else np.concatenate([resto, x])
    m = len(xx) // w
    st["resto"] = xx[m * w:].copy()
    out_n = len(x)
    if not m:  # aun no hay ventana completa: la ganancia de ahora
        return np.full(out_n, st.get("g_prev", 1.0), np.float32)
    v = xx[:m * w].astype(np.float64).reshape(m, w, 2)
    dl = 10 * np.log10(np.mean(v[:, :, 0] ** 2, axis=1) + 1e-12)
    dr = 10 * np.log10(np.mean(v[:, :, 1] ** 2, axis=1) + 1e-12)
    hist = st.get("dr", np.full(k - 1, -120.0))
    todo = np.concatenate([hist, dr])
    ref = np.lib.stride_tricks.sliding_window_view(todo, k).max(axis=1)[-m:]
    st["dr"] = todo[-(k - 1):] if k > 1 else np.zeros(0)
    eco = (ref > ECO_SOBRE_DB) & (dl < ref + ECO_MARGEN_DB)
    objetivo = np.where(eco, 10 ** (ECO_DB / 20), 1.0)
    g = float(st.get("g", 1.0))
    ka, ks = 1 - np.exp(-ECO_VENTANA_S / ECO_ATAQUE_S), 1 - np.exp(-ECO_VENTANA_S / ECO_SUELTA_S)
    por_ventana = np.empty(m, np.float32)
    for i in range(m):
        g += (objetivo[i] - g) * (ka if objetivo[i] < g else ks)
        por_ventana[i] = g
    st["g"] = g
    # de ventana a muestra: de la ganancia de la anterior a la de esta (sin escalones audibles)
    antes = np.concatenate([[st.get("g_prev", 1.0)], por_ventana[:-1]])
    st["g_prev"] = float(por_ventana[-1])
    frac = (np.arange(w, dtype=np.float32) + 1) / w
    curva = (antes[:, None] + (por_ventana - antes)[:, None] * frac[None, :]).reshape(-1)
    # la curva es de las m ventanas completas (desde lo que quedo del bloque anterior): se le quita ese principio y lo
    # que falta al final (el resto, aun sin decidir) va con la ultima ganancia
    previo = 0 if resto is None else len(resto)
    curva = curva[previo:]
    if len(curva) < out_n:
        curva = np.concatenate([curva, np.full(out_n - len(curva), curva[-1] if len(curva) else g, np.float32)])
    return curva[:out_n].astype(np.float32)


def mezclar_bloque(x, tu=1.0, demas=1.0, eco=True, estado=None, sr=SR):
    """(n, 2) float de un WAV de dos pistas → (n,) float mezclado: el micro × tu (y sin su eco si eco), lo que suena
    × demas, sumados y con el limitador de la grabadora (como a_mono: con tu = demas = 1 y sin quitar el eco, es lo
    mismo). Para exportar; pistas-worklet.js hace lo mismo al reproducir."""
    import numpy as np
    x = np.asarray(x, np.float32)
    gl = np.float32(tu) * (ganancias_eco(x, sr, estado) if eco else np.float32(1.0))
    y = soft_unlimit(x[:, 0]) * gl + soft_unlimit(x[:, 1]) * np.float32(demas)
    soft_limit(y)
    np.clip(y, -1.0, 1.0, out=y)
    return y


def exportar_mezcla(src, dst, tu=1.0, demas=1.0, eco=True, cancelled=None, bloque_s=10.0):
    """Escribe en dst un WAV normal (48 kHz, mono, 16 bits) con la mezcla de un WAV de dos pistas (mezclar_bloque).
    Por bloques: la memoria no depende de lo que dure. Lo escribe a dst + «.part» y lo renombra al terminar. Devuelve
    los segundos escritos. ValueError si src no es de dos pistas."""
    import numpy as np
    cancelled = cancelled or (lambda: False)
    with open(src, "rb") as f:
        head = f.read(CABECERA_MAX)
    if not marca_en(head):
        raise ValueError("Este audio no está grabado en dos pistas: ya se oye mezclado")
    off, data_n = _data_de(head)
    size = os.path.getsize(src)
    total = size - off if data_n is None else min(data_n, size - off)
    total -= total % 4
    tmp = dst + ".part"
    estado, escritas = {}, 0
    try:
        with open(src, "rb") as f, open(tmp, "wb") as out:
            out.write(_cabecera_mono(0))
            f.seek(off)
            quedan, paso = total, int(bloque_s * SR) * 4
            while quedan > 0:
                if cancelled():
                    raise InterruptedError("cancelado")
                raw = f.read(min(paso, quedan))
                if not raw:
                    break
                raw = raw[:len(raw) - len(raw) % 4]
                quedan -= len(raw)
                x = np.frombuffer(raw, "<i2").reshape(-1, 2).astype(np.float32) / 32768.0
                y = mezclar_bloque(x, tu, demas, eco, estado)
                out.write(np.round(y * 32767).astype("<i2").tobytes())
                escritas += len(y)
            out.seek(0)
            out.write(_cabecera_mono(escritas))
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return escritas / SR


def _data_de(head):
    """(offset del audio, bytes de audio o None si la cabecera sigue abierta)."""
    off = 12
    while off + 8 <= len(head):
        cid, n = head[off:off + 4], struct.unpack("<I", head[off + 4:off + 8])[0]
        if cid == b"data":
            return off + 8, (None if n in (OPEN, 0) else n)
        off += 8 + n + (n & 1)
    raise ValueError("WAV sin audio")


def _cabecera_mono(muestras, sr=SR):
    n = muestras * 2
    return (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
            + b"data" + struct.pack("<I", n))


def nombre_mezcla(src, carpeta):
    """Ruta libre en `carpeta` para la mezcla de src: «<nombre> (mezclado).wav», «… (mezclado 2).wav»…"""
    base = os.path.splitext(os.path.basename(src))[0] + " (mezclado)"
    cand, k = os.path.join(carpeta, base + ".wav"), 2
    while os.path.exists(cand) or os.path.exists(cand + ".part"):
        cand = os.path.join(carpeta, f"{os.path.splitext(os.path.basename(src))[0]} (mezclado {k}).wav")
        k += 1
    return cand


def exportar_peticion(abs_path, carpeta, body, grabando=None):
    """«Exportar audio mezclado» (POST /api/audio/mezclado {path, tu, demas, eco}): la mezcla de abs_path en `carpeta`
    (el Escritorio). grabando: la ruta que se está grabando ahora (no se exporta a medias). ValueError con el motivo,
    en una frase para el usuario. Devuelve {"destino", "nombre", "segundos"}."""
    if not es_doble(abs_path):
        raise ValueError("Este audio no está grabado en dos pistas: ya se oye mezclado.")
    if grabando and os.path.normcase(os.path.abspath(grabando)) == os.path.normcase(os.path.abspath(abs_path)):
        raise ValueError("Esta reunión se está grabando: para la grabación y vuelve a probar.")

    def num(k):
        try:
            v = float((body or {}).get(k, 1.0))
        except (TypeError, ValueError):
            v = 1.0
        return min(2.0, max(0.0, v)) if v == v else 1.0
    if not os.path.isdir(carpeta):
        raise ValueError(f"No encuentro la carpeta donde guardarlo ({carpeta}).")
    dst = nombre_mezcla(abs_path, carpeta)
    try:
        segundos = exportar_mezcla(abs_path, dst, num("tu"), num("demas"), bool((body or {}).get("eco", True)))
    except OSError as e:
        raise ValueError(f"No se pudo guardar el audio mezclado ({e.strerror or e}).")
    return {"destino": dst, "nombre": os.path.basename(dst), "segundos": round(segundos, 1)}
