# -*- coding: utf-8 -*-
"""
Grabadora: el audio del ordenador (lo que suena por altavoces o cascos: Teams, Zoom, el
navegador…) mas el microfono, mezclados, como hace OBS.

Se ejecuta en un proceso aparte que lanza el servidor. Escribe un WAV 48 kHz estereo 16 bits
con la cabecera abierta (tamanos 0xFFFFFFFF) mientras graba, igual que OBS: asi el resto de la
app lo trata como una grabacion en curso y lo transcribe en directo. Al parar (orden "stop" o si
el servidor se cierra) corrige la cabecera y queda un WAV normal.

  python recorder.py --list                        dispositivos (JSON)
  python recorder.py --monitor --mic ID --sys ID   solo niveles, sin grabar (para probar)
  python recorder.py OUT.wav --mic ID --sys ID     grabar ("none" = sin esa fuente)

  --mic default  sigue al micro predeterminado de Windows (aunque cambie a mitad de grabacion)
  --sys all      todas las salidas a la vez (recomendado)   --sys default  la salida predeterminada

Por stdout sale una linea JSON de estado cada 0,25 s; por stdin se le manda "stop".
"""
import argparse
import errno
import json
import math
import os
import shutil
import struct
import sys
import threading
import time
import warnings
from collections import deque

import eco

SR = 48000
# margen para que lleguen los paquetes de las dos fuentes antes de mezclar: con Teams y el portatil
# cargados un paquete puede llegar tarde; con 1 s de margen casi nunca hay que rellenar con silencio
LAT = 1.0
MAX_BUF_S = 10.0    # tope de memoria por fuente (si el escritor se parase): lo mas antiguo se descarta
# Linea de tiempo comun: cada fuente coloca su audio en «posiciones» (muestras a SR desde EPOCH, el reloj del
# proceso) segun cuando llega, y el escritor toma de todas la misma ventana de posiciones. Antes cada fuente se tomaba
# por la cabeza de su cola: tras un corte (reabrir el dispositivo, PC cargado) esa fuente quedaba adelantada para
# siempre y, con altavoces, el eco de 30 ms pasaba a ser una repeticion de 330 ms (medido el 07/11/2026).
EPOCH = time.perf_counter()


def now_pos():
    return int(round((time.perf_counter() - EPOCH) * SR))


# Igualar niveles: el micro suele llegar mucho mas flojo que el audio de la reunion. Cada fuente se
# lleva poco a poco hacia el mismo volumen de voz, solo mientras hay sonido (en los silencios no se
# sube el ruido) y con limites. Rango de ganancia en dB por fuente.
TARGET_DB = -22.0
GATE_DB = -50.0
GAIN_RANGE = {"mic": (-6.0, 18.0), "sys": (-9.0, 9.0)}


class Leveler:
    """Ganancia automatica suave para una fuente (bloques de ~50 ms)."""

    def __init__(self, lo, hi):
        self.lo, self.hi = lo, hi
        self.level = None   # volumen de voz estimado (dBFS)
        self.gain = 1.0     # ganancia aplicada (lineal)

    def __call__(self, x, update=True):
        """update=False: se aplica la ganancia pero no se toca la estimacion (el micro mientras suena la reunion:
        si no, el eco de los altavoces se tomaria por su voz y se subiria hacia -22 dB)."""
        import numpy as np
        if not len(x):
            return x
        rms = float(np.sqrt(np.mean(x * x))) + 1e-12
        db = 20 * math.log10(rms)
        # hay sonido (y no es el ruido de fondo de las pausas, mucho mas flojo que la voz):
        # se actualiza la estimacion (sube rapido, baja despacio)
        if update and db > GATE_DB and (self.level is None or db > self.level - 15):
            if self.level is None:
                self.level = db
            else:
                k = 0.25 if db > self.level else 0.03
                self.level += k * (db - self.level)
        target = self.gain
        if self.level is not None:
            g_db = min(max(TARGET_DB - self.level, self.lo), self.hi)
            target = 10 ** (g_db / 20)
        # rampa dentro del bloque: sin saltos audibles
        ramp = np.linspace(self.gain, target, len(x), dtype=np.float32)[:, None]
        self.gain = target
        return x * ramp


# El micro con altavoces: entre dos frases suyas el micro solo recoge el eco de la reunion (20-25 dB por debajo). Con
# el nivelador de arriba la estimacion bajaba en ~1,7 s, tomaba el eco por su voz y lo subia hasta +18 dB: cada voz de
# los demas quedaba dos veces casi igual de fuerte (medido el 14/11/2026: eco a 0/-2,5 dB de la voz directa, la queja
# de «escucho las voces dos veces»). Este no aprende del eco: con el eco demostrado (eco.Gate, la correlacion entre
# micro y salida, que ya se mide para avisar), solo de lo que pasa del eco que cabe esperar (la salida con su retardo,
# menos la atenuacion medida del camino, mas 6 dB): su voz, tambien si hablan a la vez. Sin eco demostrado (cascos),
# de todo, como antes; mientras aun no se sabe (los primeros MIC_SIN_ECO_S con la reunion sonando), solo de lo que
# entra con la reunion callada. Sube hacia la mediana del ultimo segundo de su voz (una tos no la mueve; la primera
# vez, con 0,6 s de voz en 2 s). Baja como antes (~1,5 s), salvo con la reunion sonando y eco visto: despacio
# (MIC_BAJA_S), para que un trozo de eco mal clasificado no se la lleve. Hasta oirle, una ganancia prudente.
# No es cancelacion de eco: no toca el audio, solo decide de que bloques aprende la ganancia.
MIC_PRUDENTE_DB = 6.0   # ganancia hasta oir su voz (antes, hasta +18 dB sobre el eco de lo primero que sonase)
MIC_BAJA_S = 30.0       # constante de tiempo al bajar con la reunion sonando y eco visto (antes ~1,7 s siempre)
MIC_BAJA_SOLO_S = 1.5   # ...y en los demas casos (como antes)
MIC_SUBE_S = 0.3
MIC_VENTANA_S = 1.0     # sube hacia la mediana de su voz en este ultimo segundo...
MIC_VOZ_MIN_S = 0.4     # ...si en el hay al menos esto de voz
MIC_INICIO = (2.0, 0.6)  # la primera vez: al menos 0,6 s de su voz en los ultimos 2 s
MIC_SIN_ECO_S = 8.0     # con la reunion sonando tanto tiempo sin eco demostrado: no hay eco (cascos)
REF_CALLA_DB = -50.0    # la reunion (la salida, en crudo) calla por debajo de esto...
REF_COLA_S = 0.3        # ...desde hace esto (la cola del eco en la sala)
ECO_COPIA = 0.5         # un bloque en el que mas de la mitad no pasa del eco que cabe esperar es eco


def block_db(x):
    """dB del bloque; None si no hay bloque o no es un numero (un NaN o un inf del driver no puede envenenar nada)."""
    import numpy as np
    if x is None or not len(x):
        return None
    db = 20 * math.log10(float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) + 1e-12)
    return db if math.isfinite(db) else None


class MicLeveler(Leveler):
    """Leveler del micro que no aprende del eco (ver arriba). ref: la salida en crudo del mismo bloque; eco: el
    eco.Gate «eco de los altavoces en el micro», ya llamado con este bloque (lo pone mezclar)."""

    def __init__(self, lo, hi):
        super().__init__(lo, hi)
        self.gain = 10 ** (min(max(MIC_PRUDENTE_DB, lo), hi) / 20)
        self.t, self.dur, self.solo, self.lento = 0.0, 0.0, False, False
        self.eco, self.eco_visto, self.sonando_s = None, False, 0.0
        self.refs = deque()     # (t, dB de la salida) de los ultimos REF_COLA_S
        self.recent = deque()   # (t, duracion, dB) de los bloques de su voz de los ultimos MIC_INICIO[0] s

    def _mine(self, db, ref_db):
        """¿De este bloque se aprende?"""
        if ref_db is not None:
            self.refs.append((self.t, ref_db))
        while self.refs and self.refs[0][0] < self.t - REF_COLA_S:
            self.refs.popleft()
        ref = max((r[1] for r in self.refs), default=None)
        sounding = ref is not None and ref > REF_CALLA_DB
        if sounding:
            self.sonando_s += self.dur
        g = self.eco
        self.eco_visto = self.eco_visto or bool(g is not None and g.enabled)
        self.solo, self.lento = not sounding, sounding and self.eco_visto
        if db <= GATE_DB or (self.level is not None and db <= self.level - 15):
            return False
        if not sounding:
            return True
        if self.eco_visto and g is not None and g.lag is not None:
            return g.copia < ECO_COPIA  # pasa del eco que cabe esperar: es su voz
        return not self.eco_visto and self.sonando_s >= MIC_SIN_ECO_S  # sin eco a la vista: cascos

    def __call__(self, x, update=True, ref=None):
        import numpy as np
        if not len(x):
            return x
        self.dur = dur = len(x) / SR
        self.t += dur
        db = block_db(x)
        if db is None:  # un bloque roto (NaN): se aplica la ganancia de antes y no se aprende nada
            update = False
        if update and self._mine(db, block_db(ref)):
            self.recent.append((self.t, dur, db))
        while self.recent and self.recent[0][0] < self.t - MIC_INICIO[0]:
            self.recent.popleft()
        last = [r for r in self.recent if r[0] >= self.t - MIC_VENTANA_S]
        if update and last and last[-1][0] == self.t:
            if self.level is None:
                if sum(r[1] for r in self.recent) >= MIC_INICIO[1]:
                    self.level = float(np.median([r[2] for r in self.recent]))
            elif sum(r[1] for r in last) >= MIC_VOZ_MIN_S:
                cand = float(np.median([r[2] for r in last]))
                tau = MIC_SUBE_S if cand > self.level else MIC_BAJA_S if self.lento else MIC_BAJA_SOLO_S
                self.level += (cand - self.level) * (1 - math.exp(-dur / tau))
        target = self.gain
        if self.level is not None:
            target = 10 ** (min(max(TARGET_DB - self.level, self.lo), self.hi) / 20)
        ramp = np.linspace(self.gain, target, len(x), dtype=np.float32)[:, None]
        self.gain = target
        return x * ramp


def mezclar(parts, levelers, mic_muted=False, eco=None):
    """La mezcla de un bloque: cada fuente con su nivelador (el del micro mira la salida en crudo y `eco`, el
    eco.Gate del micro ya llamado con este bloque) y el limitador."""
    import numpy as np
    n = len(next(iter(parts.values())))
    mix = np.zeros((n, 2), np.float32)
    for k, part in parts.items():
        if k == "mic" and mic_muted:
            continue
        lev = levelers[k]
        if isinstance(lev, MicLeveler):
            lev.eco = eco
            mix += lev(part, ref=parts.get("sys"))
        else:
            mix += lev(part)
    soft_limit(mix)
    np.clip(mix, -1.0, 1.0, out=mix)
    return mix


def soft_limit(x, knee=0.8):
    """Por encima de 0,8 se curva suavemente hacia 1 en vez de recortar en seco (sin distorsion dura)."""
    import numpy as np
    a = np.abs(x)
    over = a > knee
    if over.any():
        x[over] = np.sign(x[over]) * (knee + (1 - knee) * np.tanh((a[over] - knee) / (1 - knee)))
    return x


warnings.filterwarnings("ignore")  # soundcard avisa de "discontinuidades" cada vez que hay silencio


def wav_header():
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, SR, SR * 4, 4, 16)
            + b"data" + struct.pack("<I", 0xFFFFFFFF))


def list_devices():
    import soundcard as sc
    dm, ds = sc.default_microphone(), sc.default_speaker()
    return {"mics": [{"id": m.id, "name": m.name, "default": m.id == dm.id} for m in sc.all_microphones()],
            "outputs": [{"id": s.id, "name": s.name, "default": s.id == ds.id} for s in sc.all_speakers()]}


class Source(threading.Thread):
    """Captura continua de un dispositivo a una cola en memoria (48 kHz, estereo float32).

    dev_id "default" sigue al dispositivo predeterminado de Windows aunque cambie a mitad de la
    grabacion (conectar o quitar los cascos). Con un dispositivo concreto: si se desconecta, pasa
    al predeterminado y vuelve al elegido cuando reaparece (salvo fallback=False).

    Cada bloque va a su posicion de la linea de tiempo comun (EPOCH): seguido del anterior mientras lleguen seguidos.
    Llegar tarde nunca es «antes de tiempo»: el retraso de llegada solo suma. Asi que, si en los ultimos WIN de audio
    el MENOR desfase sigue por encima del umbral, se perdio audio justo antes (o el reloj del dispositivo va mas lento):
    esos bloques se mueven a su sitio y lo que falta se toma como silencio. Un retraso solo al leer no cuenta: soundcard
    da un paquete por llamada y los atrasados llegan luego seguidos, con el desfase bajando hasta 0 dentro de esa
    ventana (si no, un bloqueo de 160 ms con paquetes de 10 ms se tomaba por un corte: refutador del 07/11/2026). Si
    llegan todos adelantados (reloj mas rapido), se mueven hacia atras. Umbral: GAP con voz; GAP_QUIET si todo lo
    mirado es silencio (RMS < -40 dBFS: la deriva se corrige asi en pasos que no se oyen, y en ese silencio)."""

    CHECK_S = 2.0  # cada cuanto se mira si Windows ha cambiado de dispositivo
    WIN = int(0.3 * SR)
    GAP, GAP_QUIET, QUIET = int(0.05 * SR), int(0.015 * SR), 10 ** (-40 / 20)  # QUIET: RMS de cada bloque

    def __init__(self, kind, dev_id, fallback=True):
        super().__init__(daemon=True)
        self.kind, self.dev_id, self.fallback = kind, dev_id, fallback
        self.buf, self.frames, self.lock = deque(), 0, threading.Lock()  # buf: [posicion, trozo, desfase al llegar]
        self.peak, self.error, self.name, self.note, self.stopping = 0.0, None, None, None, False
        self.lost = 0  # muestras que llegaron demasiado tarde y no se pudieron usar (PC muy cargado)
        self.next, self.done, self.win, self.win_len = None, None, deque(), 0  # win: (desfase, muestras, pico)
        self.since_check = 0  # la ventana se mira como mucho cada 10 ms de audio (paquetes pequeños: coste lineal)
        self.last_end = None  # llegada del bloque anterior: si llegan mucho mas deprisa que su audio, se esta vaciando
        #                       una cola atrasada (tras un bloqueo) y no se decide nada hasta que vuelvan a su ritmo
        self.gap_s = 0.0  # silencio puesto por cortes (va al log al acabar)

    def _pick(self):  # (no llamarlo _target: Thread ya usa ese nombre)
        """Dispositivo que toca usar ahora mismo (cambia si Windows cambia el predeterminado)."""
        import soundcard as sc
        devs = sc.all_microphones() if self.kind == "mic" else sc.all_speakers()
        if self.dev_id not in (None, "", "default"):
            hit = next((d for d in devs if d.id == self.dev_id), None)
            if hit or not self.fallback:
                self.note = None if hit else "desconectado"
                return hit
            self.note = "el elegido no está conectado: uso el predeterminado"
        else:
            self.note = None
        return sc.default_microphone() if self.kind == "mic" else sc.default_speaker()

    def run(self):
        import numpy as np
        import soundcard as sc
        while not self.stopping:
            try:
                target = self._pick()
                if target is None:  # dispositivo concreto que ya no esta (y sin plan B)
                    self.name, self.error = None, "desconectado"
                    time.sleep(self.CHECK_S)
                    continue
                dev = target if self.kind == "mic" else sc.get_microphone(id=str(target.id), include_loopback=True)
                self.name = target.name
                with dev.recorder(samplerate=SR, channels=1 if self.kind == "mic" else 2, blocksize=480) as r:
                    self.error, checked = None, time.time()
                    while not self.stopping:
                        d = r.record(numframes=None)
                        if time.time() - checked > self.CHECK_S:
                            checked = time.time()
                            now = self._pick()
                            if now is None or now.id != target.id:
                                break  # se han conectado/quitado los cascos: se reabre con el nuevo
                        if not len(d):
                            time.sleep(0.005)
                            continue
                        d = np.asarray(d, np.float32)
                        if d.ndim == 1:
                            d = d[:, None]
                        d = np.repeat(d, 2, axis=1) if d.shape[1] == 1 else d[:, :2]
                        self.push(d)
            except Exception as e:  # dispositivo desconectado a mitad: se reintenta
                self.error = f"{e.__class__.__name__}: {e}"[:200]
                time.sleep(1.0)

    def push(self, d, end=None):
        """Bloque recien llegado; su ultima muestra es «ahora» (end: posicion, para los tests)."""
        import numpy as np
        end = now_pos() if end is None else end
        with self.lock:
            start = end - len(d)
            if self.next is None:
                self.next = start
            err = start - self.next
            self.buf.append([self.next, d, err])
            self.frames += len(d)
            self.next += len(d)
            pk = float(np.abs(d).max()) if len(d) else 0.0
            self.peak = max(self.peak, pk)
            rms = float(np.sqrt(np.mean(np.square(d, dtype=np.float64)))) if len(d) else 0.0
            catch = self.last_end is not None and len(d) and end - self.last_end < len(d) // 4
            self.last_end = end
            self.win.append((err, len(d), rms, catch))
            self.win_len += len(d)
            while len(self.win) > 1 and self.win_len - self.win[0][1] >= self.WIN:
                self.win_len -= self.win.popleft()[1]
            self.since_check += len(d)
            if self.win_len >= self.WIN and self.since_check >= SR // 100 and not any(w[3] for w in self.win):
                self.since_check = 0
                errs = [w[0] for w in self.win]
                quiet = all(w[2] < self.QUIET for w in self.win)
                thr = self.GAP_QUIET if quiet else self.GAP
                lo, hi = min(errs), max(errs)
                shift = lo if lo >= thr else hi if hi <= -thr else 0
                if shift:
                    self._shift(shift, len(self.win) if quiet else None)
                    self.next += shift
                    self.win = deque((w[0] - shift,) + tuple(w[1:]) for w in self.win)
                    self.gap_s += shift / SR
            while self.frames > MAX_BUF_S * SR and self.buf:
                k = len(self.buf.popleft()[1])
                self.frames -= k
                self.lost += k

    def _shift(self, shift, last=None):
        """Mueve a su sitio los bloques que llegaron con el desfase `shift` (con el candado ya cogido).
        last=None (con voz: un corte de verdad): los que llegaron con ese desfase, hasta el salto. last=k (todo lo
        mirado es silencio: deriva o un corte pequeño): solo los k ultimos, los de la ventana mirada, para que el
        silencio que se mete (o lo que se quita, de su principio) caiga en ese silencio y no en la voz de antes
        (refutador del 07/11/2026: la ventana iba ~1 s por delante de la cabeza del bufer y la correccion caia en
        plena voz)."""
        if last:
            chunks = list(self.buf)[-last:]
        elif shift < 0:  # todo llega antes de lo que se creia: el anclaje iba tarde; se mueve todo, sin solapes
            chunks = list(self.buf)
        else:
            chunks = []
            for c in reversed(self.buf):
                if c[2] < shift / 2:
                    break
                chunks.insert(0, c)
        if shift < 0 and last:  # hacia atras sin pisar lo anterior: se quita lo primero de la ventana (silencio)
            drop = -shift
            for c in chunks:
                if drop <= 0:
                    break
                m = min(drop, len(c[1]))
                c[0], c[1], drop = c[0] + m, c[1][m:], drop - m
                self.frames -= m
        for c in chunks:
            c[0] += shift
            c[2] -= shift

    def take_window(self, w0, n):
        """Las n muestras de las posiciones [w0, w0 + n): silencio donde no hay audio. Lo anterior se descarta (si
        llego cuando su sitio ya estaba escrito, cuenta como perdido), y lo que un trozo posterior pisa, tambien."""
        import numpy as np
        out = np.zeros((n, 2), np.float32)
        cover = np.zeros(n, bool)
        w1 = w0 + n
        with self.lock:
            keep = deque()
            for c in self.buf:
                p, x = c[0], c[1]
                q = p + len(x)
                if self.done is not None and p < w0:
                    self.lost += min(q, w0) - p
                a, b = max(p, w0), min(q, w1)
                if a < b:
                    if self.done is not None:
                        self.lost += int(cover[a - w0:b - w0].sum())
                    out[a - w0:b - w0] = x[a - p:b - p]
                    cover[a - w0:b - w0] = True
                if q <= w1:
                    self.frames -= len(x)
                    continue
                if p < w1:
                    c[0], c[1] = w1, x[w1 - p:]
                    self.frames -= w1 - p
                keep.append(c)
            self.buf, self.done = keep, w1
        return out

    def status(self):
        with self.lock:
            p, self.peak = self.peak, 0.0
        return {"name": self.name, "db": round(20 * math.log10(p), 1) if p > 1e-5 else -100,
                "error": self.error, "note": self.note, "lost": round(self.lost / SR, 2)}


class AllOutputs:
    """Lo que suena en TODAS las salidas a la vez (cascos, altavoces…): capta la reunion salga
    por donde salga, y si se conecta otro dispositivo a mitad de la grabacion se incorpora. Una salida que es copia
    retrasada de otra (un dispositivo virtual, el «espejo» de un driver) no se suma: eco.Duplicates."""

    SCAN_S = 3.0

    def __init__(self, log=None):
        self.sources, self.scanned = {}, 0.0
        self.log = log or (lambda msg: None)
        self.dups = eco.Duplicates(log=self.log)
        self.scan()

    def scan(self):
        import soundcard as sc
        self.scanned = time.time()
        try:
            ids = {s.id for s in sc.all_speakers()}
        except Exception:
            return
        for i in ids - set(self.sources):
            s = Source("sys", i, fallback=False)  # sin plan B: si no, se duplicaria el predeterminado
            s.start()
            self.sources[i] = s
        for i in set(self.sources) - ids:
            self.sources.pop(i).stopping = True

    def take_window(self, w0, n):
        import numpy as np
        if time.time() - self.scanned > self.SCAN_S:
            self.scan()
        parts = {i: s.take_window(w0, n) for i, s in list(self.sources.items())}
        if self.dups is not None:
            try:
                return self.dups.feed(parts)
            except Exception as e:  # un fallo aqui no puede parar la grabacion: se sigue sumando todo
                self.dups = None
                self.log(f"✖ salidas duplicadas: {e.__class__.__name__}: {e}: se desactiva (se suman todas)")
        mix = np.zeros((n, 2), np.float32)
        for x in parts.values():
            mix += x
        return mix

    def status(self):
        items = list(self.sources.items())
        notes = self.dups.note({i: s.name for i, s in items}) if self.dups is not None else {}
        sts = [dict(s.status(), dup=notes.get(i)) for i, s in items]
        live = [x for x in sts if x["name"] and not x["error"]]
        loud = max((x for x in live if not x["dup"]), key=lambda x: x["db"], default=None)
        return {"name": " + ".join(x["name"] + (f" ({x['dup']})" if x["dup"] else "") for x in live) or None,
                "db": loud["db"] if loud else -100,
                "error": None if live else "ninguna salida de audio disponible",
                "note": f"suena por: {loud['name']}" if loud and loud["db"] > -60 else None,
                "lost": round(sum(x.get("lost", 0) for x in sts), 2)}

    @property
    def stopping(self):
        return False

    @stopping.setter
    def stopping(self, v):
        for s in self.sources.values():
            s.stopping = v


# Escribir no puede cortar la grabacion por un fallo pasajero (el antivirus o OneDrive con el fichero cogido, un disco
# de red que va y viene, el disco lleno mientras se libera sitio): lo que no se puede escribir se guarda en memoria y
# se reintenta en cada vuelta, hasta WRITE_RETRY_S seguidos. Solo entonces se da por perdido y se dice el motivo.
WRITE_RETRY_S = 20.0
STOP_RETRY_S = 8.0      # al parar con escrituras pendientes, se sigue intentando esto (el servidor espera 10 s)
WAV_BYTES_S = SR * 4                 # 48 kHz, estereo, 16 bits: 691 MB por hora
DISK_CHECK_S = 30.0                  # cada cuanto se mira el espacio libre mientras se graba
DISK_WARN_MIN = 15                   # aviso: quedan ~15 min de disco


class Escritor:
    """El WAV sin bufer de Python (cada write va al disco, como antes con flush): devuelve cuanto se escribio, asi un
    fallo a medias no pierde ni duplica bytes. pend: lo que falta por escribir."""

    def __init__(self, f, log=None):
        self.f, self.log = f, log or (lambda msg: None)
        self.pend, self.pend_n, self.since, self.error = deque(), 0, None, None
        self.retried_s = 0.0  # tiempo total con escrituras pendientes (va al log al acabar)

    def write(self, data):
        if data:
            self.pend.append(memoryview(data))
            self.pend_n += len(data)
        return self.flush()

    def flush(self):
        """Escribe lo pendiente; True si no queda nada. Lanza OSError si lleva mas de WRITE_RETRY_S sin poder."""
        while self.pend:
            b = self.pend[0]
            try:
                k = self.f.write(b)
            except OSError as e:
                now = time.monotonic()
                if self.since is None:
                    self.since = now
                    self.log(f"⚠ no se puede escribir la grabacion ({e.__class__.__name__}: {e}): se guarda en memoria "
                             f"y se reintenta durante {WRITE_RETRY_S:.0f} s")
                self.error = e
                if now - self.since > WRITE_RETRY_S:
                    raise
                return False
            k = len(b) if k is None else k
            self.pend_n -= k
            if k >= len(b):
                self.pend.popleft()
            else:
                self.pend[0] = b[k:]
        if self.since is not None:
            s = time.monotonic() - self.since
            self.retried_s += s
            self.log(f"✓ se vuelve a escribir la grabacion (tras {s:.1f} s con el audio en memoria)")
            self.since, self.error = None, None
        return True


def disk_free(path):
    """Bytes libres en el disco de `path` (None si no se puede saber: una ruta de red rara, por ejemplo)."""
    try:
        return shutil.disk_usage(os.path.dirname(os.path.abspath(path)) or ".").free
    except (OSError, ValueError):
        return None


def disk_minutes(free):
    return None if free is None else int(free / WAV_BYTES_S / 60)


def motivo_de(e):
    """Una frase para el usuario con el porque de un fallo de la grabadora."""
    if isinstance(e, OSError) and (e.errno == errno.ENOSPC or getattr(e, "winerror", None) in (39, 112)):
        return "el disco está lleno"
    if isinstance(e, OSError):
        return f"no se puede escribir la grabación ({e.__class__.__name__}: {e.strerror or e})"[:200]
    return f"error en la grabadora ({e.__class__.__name__}: {e})"[:200]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--monitor", action="store_true")
    ap.add_argument("--mic", default="default")
    ap.add_argument("--sys", default="default")
    a = ap.parse_args()
    out = sys.stdout
    try:
        out.reconfigure(encoding="utf-8")  # nombres de dispositivo con ® y acentos
    except (AttributeError, ValueError):
        pass

    def emit(obj):
        out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        out.flush()

    if a.list:
        emit(list_devices())
        return
    import numpy  # noqa: F401  (antes que soundcard, como antes)
    import soundcard  # noqa: F401  (al importarse reactiva sus avisos: se vuelven a silenciar)
    warnings.filterwarnings("ignore")

    sources = {}
    if a.mic != "none":
        sources["mic"] = Source("mic", a.mic)
        sources["mic"].start()

    def elog(msg):  # a recorder.log (el servidor manda ahi el stderr): que se pueda ver luego que paso
        sys.stderr.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        sys.stderr.flush()

    if a.sys == "all":
        sources["sys"] = AllOutputs(log=elog)
    elif a.sys != "none":
        sources["sys"] = Source("sys", a.sys)
        sources["sys"].start()

    stop = threading.Event()
    muted = {"mic": False}
    levelers = {k: (MicLeveler if k == "mic" else Leveler)(*GAIN_RANGE[k]) for k in sources}
    # con micro y salida: el eco de los altavoces en el micro (B) y una salida que lleva el micro (A). Solo se MIDEN y
    # se avisan (recorder.log y el medidor), sin tocar el audio: decidir por niveles cuando hablan los dos (o con musica
    # o un video sonando) se comia la voz (refutador del 07/11/2026: el 73 % de la voz del usuario bajaba > 10 dB con
    # eco a -10 dB). Alineadas las fuentes, el eco de la sala queda a ~30 ms y no se oye «doble».
    both = "mic" in sources and "sys" in sources
    echo = eco.Gate("eco de los altavoces en el micro", 0.25, 0.003, log=elog, act=False) if both else None
    mon = eco.Gate("el micro en la salida", 0.6, 0.0, log=elog, act=False) if both else None
    gates = [both]  # se apaga si fallan (la grabacion sigue igual que antes)

    def read_stdin():  # "stop" o que el servidor se cierre (fin de stdin): se para y se cierra bien
        for line in sys.stdin:
            cmd = line.strip()
            if cmd == "stop":
                break
            if cmd in ("mute", "unmute"):  # silenciar el micro a mitad de grabacion (se graba silencio)
                muted["mic"] = cmd == "mute"
        stop.set()

    threading.Thread(target=read_stdin, daemon=True).start()

    f = w = None
    disk = {"at": -1e9, "min": None, "warned": False}

    def check_disk(now):
        if not f or now - disk["at"] < DISK_CHECK_S:
            return
        disk["at"] = now
        disk["min"] = disk_minutes(disk_free(a.out))
        if disk["min"] is not None and disk["min"] <= DISK_WARN_MIN and not disk["warned"]:
            disk["warned"] = True
            elog(f"⚠ queda poco disco: unos {disk['min']} min de grabacion")

    if a.out and not a.monitor:
        try:
            f = open(a.out, "wb", buffering=0)
            w = Escritor(f, log=elog)
            w.write(wav_header())
        except OSError as e:
            emit({"fatal": motivo_de(e)})
            elog(f"✖ no se puede crear la grabacion: {e.__class__.__name__}: {e}")
            raise
    t0, written, last = time.perf_counter(), 0, 0.0
    dicho = [False]  # ya se mando una linea «fatal» con el motivo
    base = int(round((t0 - EPOCH) * SR))  # posicion de la muestra 0 del fichero: lo capturado en t0
    try:
        while True:
            now = time.perf_counter()
            final = stop.is_set()
            target = int(max(0.0, now - t0 - (0 if final else LAT)) * SR)
            n = target - written
            if n > 0:
                # la misma ventana de la linea de tiempo de todas las fuentes (se toma aunque el micro este
                # silenciado, para no acumular nada)
                parts = {k: s.take_window(base + written, n) for k, s in sources.items()}
                if gates[0] and not muted["mic"]:
                    try:  # solo miden (act=False): devuelven las partes tal cual
                        parts["sys"], _ = mon(parts["sys"], parts["mic"])
                        parts["mic"], _ = echo(parts["mic"], parts["sys"])
                    except Exception as e:  # un fallo aqui no puede parar la grabacion
                        gates[0] = False
                        elog(f"✖ eco: {e.__class__.__name__}: {e}: se deja de medir")
                # micro y reunion a un volumen parecido (el micro, sin aprender del eco que mide la puerta)
                mix = mezclar(parts, levelers, muted["mic"], echo if gates[0] else None)
                if w:
                    w.write((mix * 32767).astype("<i2").tobytes())
                written += n
            elif w and w.pend:
                w.flush()  # lo que quedo en memoria por un fallo al escribir
            check_disk(now)
            if now - last >= 0.25 or final:
                last = now
                st = {k: s.status() for k, s in sources.items()}
                for gate, k in ((echo, "mic"), (mon, "sys")):
                    if gate is None or not gate.enabled or muted["mic"]:  # con el micro silenciado no se mide
                        continue
                    ms = gate.status()["retardo_ms"] or 0
                    if gate is echo:
                        txt = f"se oye la reunión por el micro (eco de {ms} ms): con auriculares no pasa"
                    elif ms >= 120:  # > ~120 ms: vuelve por la llamada (otro participante con altavoz), no es local
                        txt = f"tu voz vuelve por la reunión ({ms} ms): es el eco de otro participante"
                    else:
                        txt = (f"esta salida lleva tu micro ({ms} ms): quita «Escuchar este dispositivo» del micro en "
                               "Sonido de Windows")
                    st[k]["note"] = f"{st[k]['note']} · {txt}" if st[k].get("note") else txt
                emit({"t": round(written / SR, 2), "mic_muted": muted["mic"], **st,
                      "eco": {"micro": echo.status(), "salida": mon.status()} if both else None,
                      # minutos de grabacion que caben en el disco (cada DISK_CHECK_S) y segundos de audio que esperan
                      # en memoria a poder escribirse (un fallo pasajero al escribir)
                      "disco_min": disk["min"], "pendiente_s": round(w.pend_n / WAV_BYTES_S, 1) if w else 0})
            if final:
                fin = time.monotonic() + STOP_RETRY_S
                while w and w.pend and time.monotonic() < fin:  # parar no tira lo que espera en memoria
                    if not w.flush():
                        time.sleep(0.2)
                break  # lo que no se haya podido escribir se dice en recorder.log (abajo)
            time.sleep(0.05)
    except BaseException as e:  # el porque, para el servidor (rec_status: «cortada») y para recorder.log
        if not isinstance(e, (KeyboardInterrupt, SystemExit)):
            dicho[0] = True
            try:
                emit({"fatal": motivo_de(e)})
            except (OSError, ValueError):
                pass
            elog(f"✖ la grabadora se para: {motivo_de(e)}")
        raise
    finally:
        for s in sources.values():
            s.stopping = True
        lost = {k: s.status().get("lost", 0) for k, s in sources.items()}
        if any(lost.values()):  # queda en recorder.log
            sys.stderr.write(f"audio perdido por llegar tarde (PC cargado): {lost}\n")
        cortes = {k: round(sum(x.gap_s for x in (s.sources.values() if isinstance(s, AllOutputs) else [s])), 2)
                  for k, s in sources.items()}
        if any(cortes.values()):
            elog(f"cortes de audio recolocados en su sitio (s): {cortes}")
        if both:
            elog(f"eco al terminar: micro {echo.status()} · salida {mon.status()}")
        if w and w.pend_n:
            perdido = w.pend_n / WAV_BYTES_S
            elog(f"✖ {perdido:.1f} s de audio no se han podido escribir")
            try:  # tambien al parar: el servidor lo dice (si no, solo quedaba en el log); si ya dijo el motivo, ese
                if not dicho[0]:
                    emit({"fatal": f"no se han podido escribir los últimos {perdido:.0f} s de la grabación "
                          f"({motivo_de(w.error) if w.error else 'error al escribir'})"})
            except (OSError, ValueError):
                pass
        if w and w.retried_s:
            elog(f"escrituras reintentadas: {w.retried_s:.1f} s con el audio en memoria")
        if f:
            try:  # cerrar la cabecera no necesita sitio nuevo; si falla, el servidor lo reintenta (close_wav)
                size = f.tell()
                f.seek(4)
                f.write(struct.pack("<I", size - 8))
                f.seek(40)
                f.write(struct.pack("<I", size - 44))
            except OSError as e:
                elog(f"✖ no se pudo cerrar la cabecera del WAV: {e.__class__.__name__}: {e}")
            finally:
                try:
                    f.close()
                except OSError:
                    pass
        try:
            emit({"done": True, "t": round(written / SR, 2)})
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
