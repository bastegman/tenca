# -*- coding: utf-8 -*-
"""Quien habla: diarizacion en local (sherpa-onnx: segmentacion pyannote 3.0 + huellas CAM++ de 3D-Speaker, chino e
ingles; hasta el 15/11/2026, TitaNet small), memoria de personas entre reuniones y casado con las frases del .srt.

  - Funcion OPCIONAL (apagada por defecto). Si esta apagada no se importa sherpa_onnx, no se descarga nada y no se
    crea ningun sidecar. Los modelos (~35 MB) se bajan a DATA_DIR/models/voces/ al activarla.
  - Por reunion: <audio>.hablantes.json junto al audio (tramos + hablantes h1, h2… + casado con el .srt), SIN vectores.
  - Global: DATA_DIR/voces.json cifrado con DPAPI (fuera de Windows, en claro): personas con hasta 5 huellas
    (vectores de 192) cada una. Una huella solo se guarda cuando el usuario pulsa «Recordar» con consentimiento.
  - «Tu» no usa biometria: sale de la actividad del microfono durante la grabacion propia (micro{paso, activo}).
  - La diarizacion corre en un SUBPROCESO de prioridad baja (modo CLI de este mismo fichero), que escribe el sidecar
    y devuelve las huellas por stdout en JSON (como worker.py):

        python voices.py --diarize AUDIO --out SIDECAR [--niveles FICHERO] [--data DATA_DIR] [--threads 2]
                         [--participantes N]
        python voices.py --fingerprint h2 --diarize AUDIO --out SIDECAR     (recalcular la huella de un hablante)

Umbrales (spike del 30/10/2026): coseno >= 0,55 y segundo candidato a >= 0,10 → misma persona; 0,40-0,55 (o dos
candidatos a < 0,10) → provisional, se pide confirmar; < 0,40 → persona nueva.
"""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tarfile
import threading
import time
import unicodedata
import uuid

import numpy as np

import pistas
from common import log, protect, read_text, unprotect, write_bytes

# Modelo de huellas desde el 15/11/2026 (review del 14/11, punto 36): CAM++ zh_en de 3D-Speaker (Apache 2.0). Medido
# con scripts/banco_voces.py + scripts/eval_voces.py: series del AMI de 2 h, reuniones montadas de 6-8 voces en español
# (OpenSLR 71), todo tambien por Opus a 24 kbps como Teams, y reuniones de 2 h con alguien que habla 27-60 s. Contra
# TitaNet small: DER medio 0,190 frente a 0,200 en las 12 de prueba, empate en limpio y mejor con Teams (0,198 frente a
# 0,227 con 8 voces); quien habla poco sale en su grupo con los dos y con CAM++ cae menos de su voz en el de otro
# (0-21 % frente a 0-32 %); la misma persona en otra reunion, con coseno >= 0,79 (TitaNet: 0,62). 830 MB de pico con
# 2 h 13 (TitaNet: 918). Las huellas guardadas con el anterior se rehacen (Voices._migrar, rehacer_huellas).
MODEL_ID = "pyannote-seg-3.0 + 3dspeaker_campplus_sv_zh_en_16k-common_advanced"
DIM = 192
UMBRAL, DUDOSO, MARGEN = 0.55, 0.40, 0.10
MAX_HUELLAS = 5          # por persona: al anadir la sexta se descarta la mas antigua
MIN_HUELLA_S = 20.0      # voz acumulada para proponer guardar una huella
MIN_MATCH_S = 5.0        # voz acumulada para intentar emparejar (entre 5 y 20 s: provisional)
BREVE_S = 10.0           # clusteres con menos voz: «(breve)», no generan persona
BLOQUE_S = 1800.0        # audio mas largo se diariza por bloques de <= 30 min enlazados por huella
ENLACE = 0.55            # coseno para que un grupo de un bloque sea alguien de los anteriores (el de UMBRAL hasta el
#                          15/11/2026; aparte para poder medirlo con scripts/eval_voces.py --enlace)
HUELLA_MAX_S = 90.0      # voz que se concatena para una huella (los tramos mas largos primero)
# Lo mas que se le pasa de una vez al modelo de huellas (TitaNet): su memoria crece con la duracion del trozo y una
# huella mas larga es la media de las de sus ventanas, ponderada por su duracion. Medido el 09/11/2026 (sherpa-onnx
# 2.0.1, un proceso por medida): 10 s, +84 MB; 30 s, +265 MB; 90 s, +808 MB. Con tres hablantes de 90 s la
# diarizacion de 11 min llegaba a 1,96 GB y en el portatil de Bernardo, con Teams y Outlook abiertos, se quedo sin
# memoria («bad allocation» en /decoder/_pooling/Concat_4, el nodo que crece con el tiempo); por encima de ~123 s el
# modelo ni funciona. Por ventanas de 30 s: coseno 0,999-1,0 con la huella de 90 s de golpe (tests/test_fix_memoria.py)
HUELLA_VENTANA_S = 30.0
# Calibrado el 08/11/2026 con scripts/eval_voces.py: reuniones reales de 4 personas del AMI Meeting Corpus (CC BY 4.0;
# 3 para elegir y 4 de prueba) y 4 reuniones montadas con voces chilenas de OpenSLR 71 (CC BY-SA 4.0). Hasta entonces,
# 0,5 sin unir nada al final: 36 hablantes visibles para 12 personas (DER 0,517) y 48 para 16 en las de prueba (0,494).
# Con esto, 15/12 (0,228), 18/16 (0,248) y 16/16 en las chilenas, sin juntar a dos personas.
UMBRAL_GRUPOS = 0.6      # corte del clustering de sherpa-onnx (distancia coseno; mayor = menos grupos; con 0,7 ya
#                          juntaba a dos hombres chilenos)
# Al terminar se unen los grupos que son la misma voz (consolidate): sherpa-onnx parte a cada persona en varios.
# Con CAM++ (15/11/2026), FUSION y FUSION_GRANDES elegidos con las series del AMI de `dev` (ES2011, y ES2011 con una
# persona más que habla poco, 50 y 44 s; scripts/eval_voces.py sobre scripts/banco_voces.py) y comprobados con el
# resto del banco (12 reuniones de 14 min a 2 h 30): con 0,70/0,75 (lo de TitaNet), 24 hablantes visibles para 14 en
# dev y 79 para 64 en el resto; con 0,60/0,70, 16/14 y 74/64, el DER igual o mejor (dev 0,227 -> 0,224; resto 0,190)
# y la misma pureza, y quien habla poco sigue en su grupo en las 6 reuniones con ella. Con TitaNet, 0,65 ya juntaba a
# dos personas.
FUSION = 0.60            # coseno entre las huellas de dos grupos para unirlos
TOPE_MIN = 0.65          # con el numero de participantes como pista: nunca se une por debajo de este coseno (con
#                          0,3 una pista demasiado baja juntaba a personas distintas; con 0,65, da igual si se queda
#                          corta). Con CAM++ y FUSION 0,60 la pista ya no une nada mas (lo une todo el paso 1): en dev,
#                          0,50-0,65 dan lo mismo, y por debajo de 0,62 (el coseno maximo medido entre dos personas
#                          distintas con CAM++) una pista corta podria juntar a dos. Se deja en 0,65
FUSION_GRANDES = 0.70    # el de FUSION cuando los dos grupos tienen >= GRANDE_S: sus dos huellas son fiables (con
GRANDE_S = 30.0          # TitaNet, dos hombres distintos de 57 y 70 s llegaban a 0,72; ese caso no se ha medido con
#                          CAM++)
ABSORBE_MIN = 0.55       # coseno minimo para que un grupo breve pase al visible mas parecido (con 0,30, el 28 % de
#                          la voz absorbida iba a otra persona: mejor «(breve)» que el nombre de otro)
HOLGURA = 1              # tope de hablantes = participantes nombrados + HOLGURA (el usuario no suele apuntarse)
SR = 16000
MIC_PASO, MIC_DB, MIC_SOBRE_SYS_DB = 0.25, -40.0, 6.0
# La grabadora estampa cada nivel con lo que YA ha escrito del WAV, pero el pico es de lo que acaba de llegar del
# dispositivo, que se escribe LAT (1 s) despues (recorder.py): la linea de tiempo del micro va adelantada respecto al
# audio. Se corrige al leerla; una linea de tiempo ya alineada lleva "desfase": 0. Medido: tests/test_fix_voces.py.
MIC_DESFASE_S = 0.875
MIC_NO_ES_TU = 0.2       # grabacion propia: un hablante que casi nunca coincide con el micro activo no es el usuario
SIDECAR_EXT = ".hablantes.json"
IDLE_PRIORITY = getattr(__import__("subprocess"), "IDLE_PRIORITY_CLASS", 0)

RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
# sha256 calculados el 30/10/2026 sobre los ficheros descargados de esos URLs; el de TitaNet coincide con el
# checksum.txt del release speaker-recongition-models (el release de segmentacion no publica checksum.txt).
MODELS = (
    {"name": "segmentation.onnx", "size": 6958444, "member": "sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
     "url": RELEASES + "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
     "sha256": "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488"},
    # sha256 del 15/11/2026, el mismo que el checksum.txt del release (y el tamaño)
    {"name": "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx", "size": 28281164,
     "url": RELEASES + "speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
     "sha256": "aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2"},
)


class VoicesError(Exception):
    pass


# ----------------------------------------------------------------------------- disponibilidad y modelos

def enabled(settings):
    return bool((settings or {}).get("voices_enabled"))


_AVAILABLE = {}


def available():
    """(True, None) si sherpa_onnx importa en ESTE Python; si no, (False, motivo). Se prueba en un subproceso (asi
    el servidor no carga las DLL de onnxruntime) y se recuerda. No se llama si la opcion esta apagada."""
    if not _AVAILABLE:
        import subprocess
        try:
            r = subprocess.run([sys.executable, "-c", "import sherpa_onnx; print(sherpa_onnx.__version__)"],
                               capture_output=True, timeout=120,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0:
                _AVAILABLE.update(ok=True, why=None)
            else:  # ImportError o un fallo de DLL en Windows: la ultima linea del error
                err = r.stderr.decode("utf-8", "replace").strip().splitlines()
                _AVAILABLE.update(ok=False, why=(err[-1] if err else f"salida {r.returncode}")[:300])
        except (OSError, subprocess.SubprocessError) as e:
            _AVAILABLE.update(ok=False, why=f"{e.__class__.__name__}: {e}")
    return _AVAILABLE["ok"], _AVAILABLE["why"]


def hilos(n=None):
    """Hilos de onnxruntime para «Quién habla»: la mitad de los del PC, entre 2 y 4 (hasta el 09/11/2026, siempre 2).
    Medido ese día (10 min de reunión, Mac de 8 núcleos y 16 hilos): 1 hilo, 34,5 s; 2, 23,3 s; 4, 18,3 s; 8, 23,0 s.
    Con 4 o menos hilos en el PC se queda en 2, como antes. El proceso sigue en prioridad baja."""
    n = os.cpu_count() if n is None else n
    return min(4, max(2, (n or 2) // 2))


def models_dir(data_dir):
    return os.path.join(data_dir, "models", "voces")


def model_paths(data_dir):
    d = models_dir(data_dir)
    return {"seg": os.path.join(d, MODELS[0]["name"]), "emb": os.path.join(d, MODELS[1]["name"])}


def models_ready(data_dir):
    return all(os.path.isfile(p) and os.path.getsize(p) > 0 for p in model_paths(data_dir).values())


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_models(data_dir, progress=None, cancelled=None):
    """Descarga (una vez) los dos modelos a DATA_DIR/models/voces/ comprobando el sha256 anclado arriba."""
    progress = progress or (lambda **k: None)
    cancelled = cancelled or (lambda: False)
    d = models_dir(data_dir)
    os.makedirs(d, exist_ok=True)
    total = sum(m["size"] for m in MODELS)
    done = 0
    for m in MODELS:
        target = os.path.join(d, m["name"])
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            done += m["size"]
            continue
        import requests
        part = os.path.join(d, os.path.basename(m["url"]) + ".part")
        progress(stage=f"Descargando modelo de voces ({m['name']})…", frac=done / total)
        with requests.get(m["url"], stream=True, timeout=60) as r:
            if r.status_code != 200:
                raise VoicesError(f"No se pudo descargar {m['url']} (HTTP {r.status_code})")
            got = 0
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    if cancelled():
                        f.close()
                        os.remove(part)
                        raise VoicesError("cancelado")
                    f.write(chunk)
                    got += len(chunk)
                    progress(frac=(done + min(got, m["size"])) / total)
        digest = _sha256(part)
        if digest != m["sha256"]:
            os.remove(part)
            raise VoicesError(f"El modelo descargado no coincide con el esperado (sha256 {digest[:12]}…): "
                              "no se usa. Prueba otra vez mas tarde.")
        if m.get("member"):  # tarball: solo el model.onnx fp32 (el int8 anade clusteres espurios)
            with tarfile.open(part, "r:bz2") as t:
                member = t.getmember(m["member"])
                with t.extractfile(member) as src, open(target + ".part", "wb") as dst:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        dst.write(chunk)
            os.replace(target + ".part", target)
            os.remove(part)
        else:
            os.replace(part, target)
        done += m["size"]
        progress(frac=done / total)
    progress(stage="Modelos de voces listos", frac=1.0)
    return model_paths(data_dir)


# ----------------------------------------------------------------------------- audio

def load_audio_16k(path, cancelled=None, en_disco=None, canal=None):
    """int16 16 kHz mono. Con PyAV (engines.decode_16k) cualquier formato; sin el, solo WAV PCM 16 bit (desarrollo).
    en_disco: un archivo nuevo donde dejar el audio decodificado; se devuelve leído de ahí con np.memmap (solo lectura),
    así que no ocupa memoria del proceso por mucho que dure: el sistema trae del disco lo que se va leyendo y lo puede
    soltar cuando quiera (en Windows no cuenta como memoria comprometida, la que se acaba en un «bad allocation»).
    Un WAV de la grabadora en dos pistas (pistas.es_doble): la suma (la mezcla de siempre), o con `canal` 0 el micro,
    1 lo que suena y "ambos" los dos, (n, 2). En cualquier otro audio `canal` no cambia nada."""
    doble = pistas.es_doble(path)
    ancho = 2 if canal == "ambos" and doble else 1
    try:
        import engines
        if en_disco is None:
            return engines.decode_16k(path, cancelled=cancelled or (lambda: False), canal=canal)
        with open(en_disco, "wb") as f:
            n = engines.decode_16k(path, cancelled=cancelled or (lambda: False), destino=f, canal=canal)
        if not n:
            return np.zeros((0, 2) if ancho == 2 else 0, np.int16)
        return np.memmap(en_disco, dtype="<i2", mode="r", shape=(n, 2) if ancho == 2 else (n,))
    except ImportError:
        pass
    import wave
    with wave.open(path, "rb") as w:
        sr, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise VoicesError("Sin PyAV solo se leen WAV de 16 bit")
    x = np.frombuffer(raw, dtype="<i2")
    if doble and ch == 2:
        x2 = x.reshape(-1, 2)
        if canal == "ambos":
            return np.ascontiguousarray(np.stack([_a_16k(x2[:, 0], sr), _a_16k(x2[:, 1], sr)], axis=1))
        x = pistas.a_mono_i16(x2) if canal is None else x2[:, int(canal)]
    elif ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return _a_16k(x, sr)


def _a_16k(x, sr):
    """Sin PyAV (desarrollo): a 16 kHz con un filtro tosco (vale para diarizar), int16."""
    if sr != SR:
        if sr % SR == 0:  # 48 kHz → 16 kHz: media de cada 3 muestras
            k = sr // SR
            x = x[:len(x) // k * k].reshape(-1, k).mean(axis=1)
        else:
            x = np.interp(np.arange(0, len(x), sr / SR), np.arange(len(x)), x)
    return np.ascontiguousarray(np.asarray(x).astype(np.int16))


def _f32(samples16):
    """float32 en [-1, 1) con una sola copia (antes, dos: asarray y la division)."""
    return np.multiply(samples16, np.float32(1 / 32768.0), dtype=np.float32)


# ----------------------------------------------------------------------------- diarizacion y huellas (sherpa-onnx)

def _sherpa():
    try:
        import sherpa_onnx
    except Exception as e:  # ImportError o un fallo de DLL en Windows
        raise VoicesError(f"sherpa-onnx no esta disponible: {e.__class__.__name__}: {e}")
    return sherpa_onnx


def _diarizer(so, paths, threads=2, shift=0.5, umbral=UMBRAL_GRUPOS):
    cfg = so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=paths["seg"], window_shift_ratio=shift),
            num_threads=threads),
        embedding=so.SpeakerEmbeddingExtractorConfig(model=paths["emb"], num_threads=threads),
        clustering=so.FastClusteringConfig(num_clusters=-1, threshold=float(umbral)),
        min_duration_on=0.3, min_duration_off=0.5)
    if not cfg.validate():
        raise VoicesError("Configuracion de diarizacion no valida (¿faltan los modelos en models/voces?)")
    return so.OfflineSpeakerDiarization(cfg)


def _extractor(so, paths, threads=2):
    cfg = so.SpeakerEmbeddingExtractorConfig(model=paths["emb"], num_threads=threads)
    if not cfg.validate():
        raise VoicesError("Modelo de huellas de voz no valido")
    return so.SpeakerEmbeddingExtractor(cfg)


def _embed1(ex, samples_f32):
    s = ex.create_stream()
    s.accept_waveform(SR, samples_f32)
    s.input_finished()
    if not ex.is_ready(s):
        return None
    return _unit(np.asarray(ex.compute(s), dtype=np.float32))


def embed(ex, samples_f32, ventana_s=HUELLA_VENTANA_S):
    """Vector L2-normalizado (192) de un trozo de voz float32 a 16 kHz, o None si es demasiado corto. Al modelo nunca
    le llegan mas de ventana_s de una vez: un trozo mas largo se parte en ventanas iguales (ninguna de menos de la
    mitad) y su huella es la media de las de cada una, ponderada por su duracion (HUELLA_VENTANA_S)."""
    n = len(samples_f32)
    if n < SR:  # menos de 1 s: no es fiable
        return None
    cada = max(SR, int(ventana_s * SR))
    k = -(-n // cada)
    if k == 1:
        return _embed1(ex, samples_f32)
    acc, cortes = None, [i * n // k for i in range(k + 1)]
    for a, b in zip(cortes, cortes[1:]):
        v = _embed1(ex, np.ascontiguousarray(samples_f32[a:b]))
        if v is not None:
            acc = v * (b - a) if acc is None else acc + v * (b - a)
    return _unit(acc)


def tramos_huella(tramos, max_s=HUELLA_MAX_S):
    """Lo que entra en la huella de un hablante: sus tramos mas largos, en ese orden, hasta max_s de voz. [(a, b)]."""
    out, acc = [], 0.0
    for a, b in sorted(tramos, key=lambda t: -(t[1] - t[0])):
        if acc >= max_s:
            break
        take = min(b - a, max_s - acc)
        out.append((a, a + take))
        acc += take
    return out


def fingerprint(ex, samples16, tramos, max_s=HUELLA_MAX_S):
    """Huella de un hablante: sus tramos mas largos concatenados (hasta max_s) → embed. (vector|None, segundos)."""
    parts = [samples16[int(a * SR):int(b * SR)] for a, b in tramos_huella(tramos, max_s)]
    if not parts:
        return None, 0.0
    return embed(ex, _f32(np.concatenate(parts))), sum(b - a for a, b in tramos)


def audio_huella(path, tramos, duracion, max_s=HUELLA_MAX_S, en_disco=None, canal=None):
    """El audio de la huella de un hablante (tramos_huella, concatenado) sin decodificar la reunion entera: solo esos
    tramos (engines.decode_rangos_16k, que comprueba con `duracion`, la del sidecar, que los tiempos del archivo son
    los de la diarizacion). Si el formato no deja saltar (mp3, aac, amr…) o algo no cuadra, entera como antes
    (load_audio_16k, con en_disco). `canal`: el de un WAV en dos pistas (0 el micro, 1 lo que suena; None la suma).
    Devuelve (int16, si ha sido por tramos)."""
    elegidos = tramos_huella(tramos, max_s)
    if not elegidos:
        return np.zeros(0, np.int16), True
    try:
        import engines
        partes = engines.decode_rangos_16k(path, elegidos, duracion, canal=canal)
        return np.concatenate(partes), True
    except ImportError:  # sin PyAV (desarrollo): entera, por el camino de siempre
        pass
    except Exception as e:  # SinSalto u otra cosa: entera
        log(f"   huella: el audio se lee entero ({e.__class__.__name__}: {e})")
    samples = load_audio_16k(path, en_disco=en_disco, canal=canal)
    try:
        return np.concatenate([np.asarray(samples[int(a * SR):int(b * SR)]) for a, b in elegidos]), False
    finally:
        del samples


def cosine(a, b):
    a, b = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    return float(np.dot(a, b) / (na * nb)) if na > 0 and nb > 0 else 0.0


def _unit(v):
    if v is None:
        return None
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else None


def _intervals(tramos):
    """Tramos ordenados y sin solapes (al unir dos grupos pueden pisarse); los que solo se tocan siguen aparte."""
    out = []
    for a, b in sorted(tramos):
        if out and a < out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def consolidate(res, samples16=None, ex=None, fusion=FUSION, absorbe_min=ABSORBE_MIN, participantes=None,
                tope_min=TOPE_MIN, fusion_grandes=None, grande_s=None):
    """Une los grupos de una diarizacion que son la misma voz; devuelve el mismo formato, renumerado (h1 = el que mas
    habla). sherpa-onnx parte a cada persona en varios grupos: en reuniones reales de 4 personas salian 7-11 hablantes
    visibles y 43-59 grupos (scripts/eval_voces.py). Pasos, decididos con la media de las huellas de cada grupo
    ponderada por sus segundos:
    1) mientras dos grupos tengan huellas con coseno >= fusion (>= fusion_grandes si los dos tienen >= grande_s
       segundos: sus huellas son fiables), se unen los mas parecidos;
    2) si se sabe cuantos participantes hay, mientras haya mas hablantes visibles (>= BREVE_S) que participantes +
       HOLGURA se une el par visible mas parecido, nunca por debajo de tope_min (ni, si los dos son grandes, de lo que
       piden dos grandes): es una pista, no se fuerza;
    3) cada grupo breve pasa al visible mas parecido si el coseno es >= absorbe_min; si no, sigue breve.
    Con ex y samples16, la huella de cada grupo que ha cambiado se recalcula como las demas (fingerprint)."""
    hue = res.get("huellas") or {}
    by_h = {}
    for t in res.get("tramos") or []:
        by_h.setdefault(t["h"], []).append((float(t["inicio"]), float(t["fin"])))
    groups = []
    for h, tr in by_h.items():
        tr = _intervals(tr)
        v = _unit(hue.get(h))
        secs = sum(b - a for a, b in tr)
        groups.append({"tramos": tr, "fp": tr, "vec": v, "secs": secs, "orig": v, "cambia": False})

    def visible(g):
        return g["secs"] >= BREVE_S

    def join(a, b, absorbe=False):
        """b pasa a a. Al absorber un breve, ni su huella ni sus tramos cuentan para la huella de a (pocos segundos y
        quiza de otra voz): solo para quien habla."""
        if b["secs"] > a["secs"] and not absorbe:
            a, b = b, a
        if not absorbe:
            if a["vec"] is not None and b["vec"] is not None:
                a["vec"] = _unit(a["vec"] * max(a["secs"], 1.0) + b["vec"] * max(b["secs"], 1.0))
            elif a["vec"] is None:
                a["vec"] = b["vec"]
            a["fp"] = _intervals(a["fp"] + b["fp"])
            a["cambia"] = True
        a["tramos"] = _intervals(a["tramos"] + b["tramos"])
        a["secs"] = sum(x1 - x0 for x0, x1 in a["tramos"])
        groups[:] = [g for g in groups if g is not b]

    fusion_grandes = FUSION_GRANDES if fusion_grandes is None else fusion_grandes
    grande_s = GRANDE_S if grande_s is None else grande_s

    def need(a, b):  # dos grupos grandes: sus dos huellas son fiables y han de parecerse mas para ser la misma voz
        return max(fusion, fusion_grandes) if min(a["secs"], b["secs"]) >= grande_s else fusion

    def best_pair(cands, umbral=None):
        """El par mas parecido entre los que llegan a su umbral (con umbral=None, el mas parecido sin mas)."""
        best = (-2.0, None, None)
        for i, a in enumerate(cands):
            for b in cands[i + 1:]:
                c = cosine(a["vec"], b["vec"])
                if c > best[0] and (umbral is None or c >= umbral(a, b)):
                    best = (c, a, b)
        return best

    while True:
        c, a, b = best_pair([g for g in groups if g["vec"] is not None], need)
        if a is None:
            break
        join(a, b)
    if participantes and int(participantes) > 0:
        while True:
            vis = [g for g in groups if g["vec"] is not None and visible(g)]
            if len(vis) <= int(participantes) + HOLGURA:
                break
            c, a, b = best_pair(vis, lambda x, y: need(x, y) if min(x["secs"], y["secs"]) >= grande_s else tope_min)
            if a is None:
                break
            join(a, b)
    vis = [g for g in groups if g["vec"] is not None and visible(g)]
    for g in [g for g in groups if not visible(g) and g["vec"] is not None]:
        if not vis:
            break
        c, dst = max(((cosine(g["vec"], d["vec"]), d) for d in vis), key=lambda x: x[0])
        if c >= absorbe_min:
            join(dst, g, absorbe=True)
    groups.sort(key=lambda g: -g["secs"])
    tramos, hablantes, huellas = [], {}, {}
    for k, g in enumerate(groups, 1):
        h = f"h{k}"
        if g["cambia"] and ex is not None and samples16 is not None:
            v, _ = fingerprint(ex, samples16, g["fp"])
            g["vec"] = _unit(v) if v is not None else g["vec"]
        elif not g["cambia"]:
            g["vec"] = g["orig"]
        tramos += [{"inicio": round(a, 2), "fin": round(b, 2), "h": h} for a, b in g["tramos"]]
        hablantes[h] = {"persona": None, "score": None, "segundos": round(g["secs"], 1), "provisional": False,
                        "breve": g["secs"] < BREVE_S}
        if g["vec"] is not None:
            huellas[h] = [round(float(x), 5) for x in g["vec"]]
    tramos.sort(key=lambda t: (t["inicio"], t["fin"]))
    out = dict(res)
    out.update({"tramos": tramos, "hablantes": hablantes, "huellas": huellas})
    return out


SUFIJOS_NOMBRE = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "mba"}  # «Smith, John, Jr.»: no es otra persona


def count_participants(text, sugeridos=False):
    """Cuantas personas nombra el campo «Participantes». Texto libre («Ana, Luis y Pedro»): se parte por comas, «;»,
    lineas, «/» e «y/and/&» (en mayusculas tambien); ante la duda cuenta de mas, que une menos voces. sugeridos=True:
    el texto es el que escribio la app con los de Outlook o Teams (calendario.join_participants), un trozo por
    destinatario: con «; » si algun nombre lleva coma («Apellido, Nombre»), si no con «, ». Cada destinatario cuenta
    una persona, o una por cada nombre detras de la primera coma (separados por comas, salvo sufijos como «Jr.», o
    por «y/and/&» o «/»: «Pérez, Ana y Luis» son dos; el «y» de «Ortega y Gasset, José» no separa). Lo que va entre
    parentesis («(Madrid)», «(Ventas)») no cuenta, y una lista de distribucion cuenta una. Dos destinatarios que solo
    se distinguen por el parentesis (homonimos) son dos. En un trozo sin coma, «y» separa («José Ortega y Gasset»
    cuenta dos: de mas) (revision de la integracion del 08/11/2026)."""
    text, sep = str(text or ""), r"\s+(?:y|e|and|&)\s+|/"
    if not sugeridos:
        parts = re.split(r"[,;\n/]+|\s+(?:y|e|and|&)\s+", text, flags=re.I)
        return len({p.strip().casefold() for p in parts if p.strip()})
    total = 0
    for trozo in re.split(r"[;\n]+" if ";" in text else r"[,\n]+", text):
        t = re.sub(r"\([^)]*\)", " ", trozo).strip() or trozo.strip()
        if not t:
            continue
        if "," in t:
            ape, nom = t.split(",", 1)
            nombres = []
            for x in (x.strip() for x in nom.split(",")):
                if x and not (nombres and x.casefold().strip(".") in SUFIJOS_NOMBRE):
                    nombres += [n.strip() for n in re.split(sep, x, flags=re.I) if n.strip()]
            total += len({n.casefold() for n in nombres}) or 1
        else:
            total += len({n.strip().casefold() for n in re.split(sep, t, flags=re.I) if n.strip()}) or 1
    return total


def mixed(huellas, umbral=DUDOSO):
    """Alguna huella de la persona no se parece a ninguna de las otras (coseno < umbral, lo que el emparejado
    llamaria «otra persona»): puede llevar la voz de alguien mas (p. ej. por el fallo de antes del 07/11/2026)."""
    vs = [h["vector"] for h in huellas or [] if h.get("vector")]
    if len(vs) < 2:
        return False
    return any(max(cosine(a, b) for j, b in enumerate(vs) if j != i) < umbral for i, a in enumerate(vs))


def diarize(samples16, data_dir, threads=2, progress=None, cancelled=None, shift=0.5, umbral_grupos=UMBRAL_GRUPOS,
            consolidar=True, participantes=None, enlace=None):
    """Devuelve {"tramos": [{inicio, fin, h}], "hablantes": {h: {segundos, breve, …}}, "huellas": {h: [floats]},
    "duracion_s"}. Audio > 30 min: por bloques, enlazando los clusteres de cada bloque por huella (coseno >= enlace;
    None = ENLACE, leido al llamar)."""
    enlace = ENLACE if enlace is None else float(enlace)
    progress = progress or (lambda **k: None)
    cancelled = cancelled or (lambda: False)
    so = _sherpa()
    paths = model_paths(data_dir)
    if not models_ready(data_dir):
        raise VoicesError("Faltan los modelos de voces (activa la opcion en Ajustes para descargarlos)")
    total_s = len(samples16) / SR
    n_blocks = max(1, int(math.ceil(total_s / BLOQUE_S)))
    block = int(math.ceil(len(samples16) / n_blocks))
    speakers = []  # globales: {"vec", "vecs": [(vec, s)], "segundos", "tramos": [(a, b)]}
    for bi in range(n_blocks):
        off = bi * block
        part = samples16[off:off + block]
        if not len(part):
            continue
        t_off = off / SR

        def cb(done, n, _bi=bi):
            where = f"bloque {_bi + 1}/{n_blocks}" if n_blocks > 1 else "escuchando"
            progress(stage=f"Identificando voces… ({where})", frac=0.05 + 0.85 * (_bi + done / max(1, n)) / n_blocks)
            return 1 if cancelled() else 0

        # una sesion de onnxruntime por bloque y por fase, que se suelta al terminarla: cada una guarda para si la
        # memoria que ha llegado a usar (su arena) y, juntas, se sumaban (fix-subir-memoria, 09/11/2026)
        sd = _diarizer(so, paths, threads, shift, umbral_grupos)
        if sd.sample_rate != SR:
            raise VoicesError(f"El modelo espera {sd.sample_rate} Hz")
        local = {}
        for s in sd.process(_f32(part), cb).sort_by_start_time():
            local.setdefault(int(s.speaker), []).append((round(s.start + t_off, 3), round(s.end + t_off, 3)))
        del sd
        if cancelled():
            raise VoicesError("cancelado")
        ex = _extractor(so, paths, threads)
        for spk in sorted(local):
            if cancelled():
                raise VoicesError("cancelado")
            vec, secs = fingerprint(ex, samples16, local[spk])
            target = None
            if bi and vec is not None:  # bloque 2 en adelante: ¿es alguien del bloque anterior?
                best = max(((cosine(vec, g["vec"]), g) for g in speakers if g["vec"] is not None),
                           key=lambda x: x[0], default=(0.0, None))
                if best[0] >= enlace:
                    target = best[1]
            if target is None:
                target = {"vec": None, "vecs": [], "segundos": 0.0, "tramos": []}
                speakers.append(target)
            target["tramos"] += local[spk]
            target["segundos"] += secs
            if vec is not None:
                target["vecs"].append((vec, secs))
                mean = sum(v * max(s, 1.0) for v, s in target["vecs"])
                n = float(np.linalg.norm(mean))
                target["vec"] = mean / n if n > 0 else None
        del ex
    order = sorted(range(len(speakers)), key=lambda i: -speakers[i]["segundos"])
    tramos, hablantes, huellas = [], {}, {}
    for k, i in enumerate(order, 1):
        h, g = f"h{k}", speakers[i]
        tramos += [{"inicio": round(a, 2), "fin": round(b, 2), "h": h} for a, b in g["tramos"]]
        hablantes[h] = {"persona": None, "score": None, "segundos": round(g["segundos"], 1),
                        "provisional": False, "breve": g["segundos"] < BREVE_S}
        if g["vec"] is not None:
            huellas[h] = [round(float(x), 5) for x in g["vec"]]
    tramos.sort(key=lambda t: (t["inicio"], t["fin"]))
    out = {"tramos": tramos, "hablantes": hablantes, "huellas": huellas, "duracion_s": round(total_s, 1)}
    if consolidar:
        progress(stage="Uniendo voces parecidas…", frac=0.92)
        out = consolidate(out, samples16, _extractor(so, paths, threads), participantes=participantes)
    progress(stage="Voces identificadas", frac=0.95)
    return out


# ----------------------------------------------------------------------------- dos pistas (grabacion propia)

# Desde el 15/11/2026 la grabadora guarda el micro a la izquierda y lo que suena a la derecha (pistas.py), y manda su
# propia linea de «Tú» (recorder.PasosTu: «1» donde el micro lleva su voz y no el eco; «desfase» 0). Con las dos cosas,
# «Quién habla» se hace por separado: los demas, en el canal de lo que suena (sin su voz ni el eco), y lo que el micro
# recoge y no es eco, en el del micro (su voz y, en una sala, quien este a su lado). Asi «Tú» es un grupo propio con
# los tramos de su micro, no el grupo de la mezcla que mas coincide con unos niveles con 0,875 s de desfase. Sin esas
# dos cosas, o en una reunion presencial (lo que suena casi callado), la suma, como siempre.
PISTA_VOZ_MIN_S = 60.0   # con menos voz en el canal de lo que suena: presencial, se diariza la suma
PISTA_VOZ_DB = -45.0     # un paso de 0,25 s de ese canal tiene voz por encima de esto (RMS, ya nivelado a -22 dB)
PISTA_ECO = 0.6          # un grupo del micro que suena con los demas el 60 % de su tiempo y se parece a uno: su eco
PISTA_VUELTA = 0.6       # un grupo de los demas que coincide con tu micro el 60 % de su tiempo y se parece a ti: tu voz
#                          que vuelve por la llamada (otro participante con altavoz)
PISTA_HUECO_S = 0.3      # silencio entre los tramos del micro al juntarlos para diarizarlos
# Lo que suena tambien se diariza junto (solo sus tramos con sonido, por encima de PISTA_JUNTAR_DB, con las pausas de
# menos de PISTA_PAUSA_S dentro): el segmentador cuesta por segundo de audio, y en ese canal hay silencio cada vez que
# hablas tu. Sin esto, «Quién habla» tardaba un 16 % mas que con la mezcla (voces-memoria en Windows, 55 min, run
# 43761758493: 52 s frente a 45); medido en la simulacion, ademas, los demas salen algo mejor (DER 0,208 -> 0,175).
PISTA_JUNTAR_DB = -55.0  # muy por debajo de una voz (el nivelador la deja hacia -22 dB): no se pierde a nadie flojo
PISTA_PAUSA_S = 1.0
PISTA_MICRO_SOBRE_DB = 6.0   # del micro se diariza lo que pasa de su ruido de fondo esto (pasos_micro)...
PISTA_MICRO_MIN_DB = -55.0   # ...y de esto
PISTA_COPIA_DB = 6.0     # ...y esos dos solo si la copia va al menos 6 dB por debajo del otro canal en esos tramos
#                          (el original suena mas: es el mismo criterio que pistas.ganancias_eco al reproducir)


def micro_de_grabadora(micro):
    """¿La linea de tiempo del micro es la de la grabadora (desde el 15/11/2026), alineada con el archivo?"""
    if not isinstance(micro, dict) or not isinstance(micro.get("activo"), str) or "1" not in micro["activo"]:
        return False
    try:
        return float(micro.get("desfase", MIC_DESFASE_S)) == 0.0 and micro.get("fuente") == "grabadora"
    except (TypeError, ValueError):
        return False


def niveles_db(x16, paso=pistas.PASO, trozo_s=60.0):
    """RMS por paso en dB (respecto al fondo de escala de int16; -120 en silencio digital). Por trozos (x16 puede ser
    un memmap de horas)."""
    n = int(round(paso * SR))
    k = len(x16) // n
    out = np.full(k, -120.0)
    step = max(1, int(trozo_s / paso))
    for i in range(0, k, step):
        j = min(k, i + step)
        fr = np.asarray(x16[i * n:j * n], np.float32).reshape(j - i, n)
        out[i:j] = 10 * np.log10(np.mean(fr * fr, axis=1) / 32768.0 ** 2 + 1e-12)
    return out


def actividad(x16, paso=pistas.PASO, umbral_db=PISTA_VOZ_DB, trozo_s=60.0):
    """bool por paso: RMS por encima de umbral_db."""
    return niveles_db(x16, paso, trozo_s) > umbral_db


def pasos_micro(dl, dr, bits=None, bloque_s=30.0, paso=pistas.PASO):
    """bool por paso: lo que el micro recoge que no es eco, para diarizarlo (no para decidir quién es «Tú»: eso lo hace
    la línea de la grabadora). Pasa de su ruido de fondo (el percentil 10 de cada bloque_s) PISTA_MICRO_SOBRE_DB y de
    PISTA_MICRO_MIN_DB, salvo que lo que ha sonado en el PC en los últimos 0,75 s pase de -45 dB y el micro vaya 6 dB
    por debajo (el criterio de pistas.ganancias_eco: es eco). Más los pasos «1» de la línea (bits): su voz hablando a la
    vez que ellos, que por nivel no se distingue. Así entra también quien está en la sala lejos del micro, que la
    línea no marca (revisión 1, P1-1: 18 dB por debajo de tu voz, 2 % de sus pasos «1» y 65 de 72 s sin hablante)."""
    k = len(dl)
    out = np.zeros(k, bool)
    b = max(1, int(bloque_s / paso))
    for i in range(0, k, b):
        suelo = float(np.percentile(dl[i:i + b], 10))
        out[i:i + b] = dl[i:i + b] > max(suelo + PISTA_MICRO_SOBRE_DB, PISTA_MICRO_MIN_DB)
    ref = dr.copy()
    for d in (1, 2):  # lo que ha sonado en los ultimos 0,75 s (la cola del eco)
        ref[d:] = np.maximum(ref[d:], dr[:-d])
    eco = (ref > pistas.ECO_SOBRE_DB) & (dl < ref + pistas.ECO_MARGEN_DB)
    out &= ~eco
    if bits is not None and len(bits):
        m = min(k, len(bits))
        out[:m] |= bits[:m]
    return out


def compactar(x16, tramos, hueco_s=PISTA_HUECO_S, en_disco=None):
    """Los tramos [(a, b)] de x16, uno tras otro con hueco_s de silencio entre ellos: (int16, mapa). mapa: [(inicio en
    lo compactado, inicio real, duracion)] en segundos. en_disco: un archivo nuevo donde dejarlo (se devuelve como
    np.memmap, como load_audio_16k: quien presenta una reunion de 3 h habla mas de 1 h)."""
    mapa, t, n = [], 0.0, 0
    gap = np.zeros(int(hueco_s * SR), np.int16)
    partes = [] if en_disco is None else None
    f = open(en_disco, "wb") if en_disco is not None else None
    try:
        for a, b in tramos:
            i, j = max(0, int(round(a * SR))), min(len(x16), int(round(b * SR)))
            if j <= i:
                continue
            trozos = ([gap] if n else []) + [np.asarray(x16[i:j], np.int16)]
            if n:
                t += len(gap) / SR
            mapa.append((t, i / SR, (j - i) / SR))
            t += (j - i) / SR
            for y in trozos:
                n += len(y)
                if f is not None:
                    f.write(np.ascontiguousarray(y, "<i2").tobytes())
                else:
                    partes.append(y)
    finally:
        if f is not None:
            f.close()
    if en_disco is not None:
        return (np.memmap(en_disco, dtype="<i2", mode="r", shape=(n,)) if n else np.zeros(0, np.int16)), mapa
    return (np.concatenate(partes) if partes else np.zeros(0, np.int16)), mapa


def descompactar(tramos, mapa):
    """Tramos [{inicio, fin, h}] de lo compactado → tiempos reales (un tramo que cruza un hueco se parte)."""
    out = []
    for t in tramos:
        a, b = float(t["inicio"]), float(t["fin"])
        for c0, r0, d in mapa:
            lo, hi = max(a, c0), min(b, c0 + d)
            if hi > lo:
                out.append({"inicio": round(r0 + lo - c0, 2), "fin": round(r0 + hi - c0, 2), "h": t["h"]})
    return out


def _fraccion(tramos, bits, paso=pistas.PASO):
    """Fraccion del tiempo de los tramos [(a, b)] con bits[i] verdadero (bits: bool por paso)."""
    tot = on = 0
    for a, b in tramos:
        for i in range(max(0, int(math.floor(a / paso))), int(math.ceil(b / paso))):
            tot += 1
            on += bool(i < len(bits) and bits[i])
    return on / tot if tot else 0.0


def nivel_db(x16, tramos):
    """RMS en dB (respecto a int16 a fondo de escala) de x16 en esos tramos [(a, b)]; -120 si no hay nada."""
    acc = n = 0
    for a, b in tramos:
        i, j = max(0, int(a * SR)), min(len(x16), int(b * SR))
        for k in range(i, j, SR * 60):
            y = np.asarray(x16[k:min(j, k + SR * 60)], np.float64)
            acc += float(np.sum(y * y))
            n += len(y)
    return 10 * math.log10(acc / n / 32768.0 ** 2 + 1e-12) if n else -120.0


def diarize_pistas(stereo16, micro, data_dir, threads=2, progress=None, cancelled=None, participantes=None,
                   tmp_micro=None, tmp_demas=None):
    """«Quién habla» de un WAV en dos pistas (stereo16: (n, 2) int16 16 kHz, p. ej. un memmap; micro: la linea de la
    grabadora). None si no vale (micro_de_grabadora, o PISTA_VOZ_MIN_S): quien llama diariza la suma. Si vale, el
    formato de diarize con, en cada hablante, "pista": "micro" o "sistema":
      - los demas: diarize del canal derecho, solo sus tramos con sonido, juntados (PISTA_JUNTAR_DB);
      - el micro: diarize solo de lo que recoge y no es eco (pasos_micro), juntado (compactar);
      - un grupo del micro que suena con los demas (PISTA_ECO), se parece a uno de ellos (>= ENLACE) y en sus tramos va
        PISTA_COPIA_DB por debajo de lo que suena es su eco y se quita; uno de los demas que coincide con el micro
        (PISTA_VUELTA), se parece a uno del micro y va por debajo de el es tu voz que vuelve por la llamada y se quita.
        El nivel decide cual es el original (sin el, tu voz y su vuelta se quitaban las dos).
    «Tú» lo decide despues mark_user con la linea de la grabadora, entre los grupos del micro (fuera_de_tu): tu grupo
    coincide con ella y quien este a tu lado casi nada."""
    progress = progress or (lambda **k: None)
    cancelled = cancelled or (lambda: False)
    if not micro_de_grabadora(micro) or stereo16.ndim != 2 or stereo16.shape[1] != 2:
        return None
    izq, der = stereo16[:, 0], stereo16[:, 1]
    dr = niveles_db(der)
    act_der = dr > PISTA_VOZ_DB
    if act_der.sum() * pistas.PASO < PISTA_VOZ_MIN_S:
        log(f"🗣 dos pistas: lo que suena tiene {act_der.sum() * pistas.PASO:.0f} s de voz: se identifica la suma "
            "(reunion presencial)")
        return None

    def prog(a, b):
        return lambda stage=None, frac=None: progress(stage=stage, frac=None if frac is None else a + (b - a) * frac)

    total = len(stereo16) / SR
    sonando = dr > PISTA_JUNTAR_DB
    suyos = pistas.tramos_de_bits("".join("1" if b else "0" for b in sonando), hueco_s=PISTA_PAUSA_S, min_s=0.1,
                                  margen_s=0.25)
    comp_s, mapa_s = compactar(der, suyos, en_disco=tmp_demas)  # tmp_demas: lo borra quien llama
    try:
        rs = diarize(comp_s, data_dir, threads, prog(0.0, 0.8), cancelled, participantes=participantes)
    finally:
        del comp_s
    res_s = dict(rs, tramos=descompactar(rs["tramos"], mapa_s))
    mic_bits = np.frombuffer(micro["activo"].encode("ascii"), np.uint8) == ord("1")
    del_micro = pasos_micro(niveles_db(izq), dr, mic_bits)
    mios = pistas.tramos_de_bits("".join("1" if b else "0" for b in del_micro))
    voz_mic = sum(b - a for a, b in mios)
    res_m = {"tramos": [], "hablantes": {}, "huellas": {}}
    if voz_mic >= MIN_MATCH_S:
        comp, mapa = compactar(izq, mios, en_disco=tmp_micro)  # tmp_micro: lo borra quien llama
        try:
            rm = diarize(comp, data_dir, threads, prog(0.8, 0.95), cancelled)
        finally:
            del comp  # suelta el memmap (en Windows, uno mapeado no se puede borrar)
        res_m = dict(rm, tramos=descompactar(rm["tramos"], mapa))
    grupos = []  # (pista, tramos [(a, b)], huella | None)
    for pista, r in (("micro", res_m), ("sistema", res_s)):
        by_h = {}
        for t in r["tramos"]:
            by_h.setdefault(t["h"], []).append((float(t["inicio"]), float(t["fin"])))
        for h, tr in by_h.items():
            grupos.append({"pista": pista, "tramos": _intervals(tr), "vec": _unit(r["huellas"].get(h)), "h": h})
    del res_m, res_s
    quitar = []
    for g in grupos:
        otros = [o for o in grupos if o["pista"] != g["pista"] and o["vec"] is not None]
        parecido = max((cosine(g["vec"], o["vec"]) for o in otros), default=0.0) if g["vec"] is not None else 0.0
        if parecido < ENLACE:
            continue
        if g["pista"] == "micro":
            if _fraccion(g["tramos"], act_der) >= PISTA_ECO \
                    and nivel_db(izq, g["tramos"]) < nivel_db(der, g["tramos"]) - PISTA_COPIA_DB:
                quitar.append(g)
        elif _fraccion(g["tramos"], mic_bits) >= PISTA_VUELTA \
                and nivel_db(der, g["tramos"]) < nivel_db(izq, g["tramos"]) - PISTA_COPIA_DB:
            quitar.append(g)
    for g in quitar:
        log(f"🗣 dos pistas: {sum(b - a for a, b in g['tramos']):.0f} s del canal "
            f"{'del micro' if g['pista'] == 'micro' else 'de lo que suena'} quitados: "
            f"{'eco de los demas' if g['pista'] == 'micro' else 'tu voz que vuelve por la llamada'}")
    grupos = [g for g in grupos if not any(g is q for q in quitar)]
    for g in grupos:
        g["secs"] = sum(b - a for a, b in g["tramos"])
    grupos.sort(key=lambda g: -g["secs"])
    tramos, hablantes, huellas = [], {}, {}
    for k, g in enumerate(grupos, 1):
        h = f"h{k}"
        tramos += [{"inicio": round(a, 2), "fin": round(b, 2), "h": h} for a, b in g["tramos"]]
        hablantes[h] = {"persona": None, "score": None, "segundos": round(g["secs"], 1), "provisional": False,
                        "breve": g["secs"] < BREVE_S, "pista": g["pista"]}
        if g["vec"] is not None:
            huellas[h] = [round(float(x), 5) for x in g["vec"]]
    tramos.sort(key=lambda t: (t["inicio"], t["fin"]))
    progress(stage="Voces identificadas", frac=0.95)
    return {"tramos": tramos, "hablantes": hablantes, "huellas": huellas, "duracion_s": round(total, 1),
            "pistas": True}


def fuera_de_tu(data):
    """Los hablantes que no compiten por «Tú»: los breves y, en dos pistas, los del canal de lo que suena (no pueden
    ser el usuario, y un grupo suyo que coincida con falsos «1» de la linea le quitaba «Tú» a toda la reunion: revision
    1, P1-2 y P2-6)."""
    hablantes = (data or {}).get("hablantes") or {}
    out = {h for h, i in hablantes.items() if i.get("breve")}
    if (data or {}).get("pistas"):
        out |= {h for h, i in hablantes.items() if i.get("pista") == "sistema"}
    return out


def canal_de(data, h):
    """El canal de la huella de h en un WAV en dos pistas: 0 (micro), 1 (lo que suena) o None (la suma: un hablante de
    antes o unido de los dos canales)."""
    p = ((data or {}).get("hablantes") or {}).get(h, {}).get("pista")
    return 0 if p == "micro" else 1 if p == "sistema" else None


# ----------------------------------------------------------------------------- emparejado, casado y «Tu» (numpy puro)

def match(vector, personas, umbral=UMBRAL, dudoso=DUDOSO, margen=MARGEN):
    """Coseno maximo contra todas las huellas de cada persona.
    >= umbral y 2.º candidato a >= margen → persona; dudoso..umbral, o dos candidatos a < margen → provisional (¿X?);
    < dudoso → None (persona nueva)."""
    scores = []
    for p in personas or []:
        best = max((cosine(vector, h["vector"]) for h in p.get("huellas") or [] if h.get("vector")), default=None)
        if best is not None:
            scores.append((best, p["id"]))
    scores.sort(key=lambda x: -x[0])
    out = {"persona": None, "score": None, "provisional": False, "segunda": None, "score2": None}
    if not scores:
        return out
    s1, id1 = scores[0]
    s2, id2 = scores[1] if len(scores) > 1 else (None, None)
    close = s2 is not None and s1 - s2 < margen
    out.update(score=round(s1, 3), score2=round(s2, 3) if s2 is not None else None)
    if s1 >= umbral and not close:
        out.update(persona=id1)
    elif s1 >= dudoso:
        out.update(persona=id1, provisional=True, segunda=id2 if close else None)
    return out


def _bounds(f):
    if isinstance(f, dict):
        return f.get("start"), f.get("end")
    return f[0], f[1]


def assign_srt(frases, tramos):
    """Hablante de cada frase por solape: mayoritario si cubre >= 50 %; «varios» si dos cubren >= 35 % cada uno;
    None si ningun tramo la toca. Devuelve una lista alineada con frases."""
    tr = sorted(((float(t["inicio"]), float(t["fin"]), t["h"]) for t in tramos), key=lambda x: x[0])
    max_end = []
    m = -1e18
    for a, b, _ in tr:
        m = max(m, b)
        max_end.append(m)
    out = [None] * len(frases)
    order = sorted(range(len(frases)), key=lambda i: (_bounds(frases[i])[0] is None, _bounds(frases[i])[0] or 0))
    j0 = 0
    for i in order:
        a, b = _bounds(frases[i])
        if a is None or b is None or b <= a:
            continue
        a, b = float(a), float(b)
        while j0 < len(tr) and max_end[j0] <= a:
            j0 += 1
        cover, j = {}, j0
        while j < len(tr) and tr[j][0] < b:
            ov = min(b, tr[j][1]) - max(a, tr[j][0])
            if ov > 0:
                cover[tr[j][2]] = cover.get(tr[j][2], 0.0) + ov
            j += 1
        if not cover:
            continue
        ranked = sorted(cover.items(), key=lambda kv: -kv[1])
        dur = b - a
        if ranked[0][1] / dur >= 0.5:
            out[i] = ranked[0][0]
        elif len(ranked) > 1 and ranked[0][1] / dur >= 0.35 and ranked[1][1] / dur >= 0.35:
            out[i] = "varios"
        else:
            out[i] = ranked[0][0]
    return out


def mic_timeline(levels, paso=MIC_PASO):
    """Serie del micro de la grabadora propia → {"paso", "activo": "0110…"} (1 = micro activo en ese paso).
    levels: iterable de (t, mic_db, sys_db|None). Activo si mic > -40 dB y mic > sys + 6 dB. Sin biometria."""
    bits = bytearray()
    for t, mic, sysdb in levels:
        if mic is None or t is None:
            continue
        i = int(float(t) / paso)
        if i >= len(bits):
            bits.extend(b"0" * (i + 1 - len(bits)))
        if mic > MIC_DB and (sysdb is None or mic > sysdb + MIC_SOBRE_SYS_DB):
            bits[i] = ord("1")
    return {"paso": paso, "activo": bits.decode("ascii")}


def close_gaps(bits):
    """Un paso suelto sin actividad entre dos activos cuenta como activo: los niveles llegan cada ~0,28 s y los pasos
    son de 0,25 s, asi que ~1 de cada 6 pasos se queda sin nivel (medido: 9 de 49) aunque se este hablando."""
    if "101" not in bits:
        return bits
    b = bytearray(bits, "ascii")
    for i in range(1, len(b) - 1):
        if b[i] == 48 and b[i - 1] == 49 and b[i + 1] == 49:
            b[i] = 49
    return b.decode("ascii")


def mic_ratios(tramos, micro, skip=()):
    """Fraccion de la voz de cada hablante con el micro activo, {h: 0..1}; {} sin linea de tiempo. Lo que el desfase
    deja fuera (antes del primer paso o justo despues del ultimo) no cuenta; mas alla del final cuenta como inactivo."""
    if not micro or not isinstance(micro.get("activo"), str) or not micro["activo"]:
        return {}
    act = close_gaps(micro["activo"])
    try:  # un sidecar tocado a mano o danado no impide identificar
        paso, shift = float(micro.get("paso") or MIC_PASO), float(micro.get("desfase", MIC_DESFASE_S) or 0.0)
    except (TypeError, ValueError):
        paso, shift = MIC_PASO, MIC_DESFASE_S
    paso = paso if math.isfinite(paso) and paso > 0 else MIC_PASO
    shift = min(max(0.0, shift), 10.0) if math.isfinite(shift) else MIC_DESFASE_S
    unknown_end = len(act) + int(math.ceil(shift / paso))
    tot, on = {}, {}
    for t in tramos:
        h = t["h"]
        if h in skip:
            continue
        a, b = float(t["inicio"]) - shift, float(t["fin"]) - shift
        for i in range(max(0, int(math.floor(a / paso))), int(math.ceil(b / paso))):
            if len(act) <= i < unknown_end:
                continue
            tot[h] = tot.get(h, 0) + 1
            if i < len(act) and act[i] == "1":
                on[h] = on.get(h, 0) + 1
    return {h: on.get(h, 0) / n for h, n in tot.items() if n}


def pick_user(ratios):
    """El hablante que coincide >= 60 % con el micro activo (y el siguiente <= 30 %), o None."""
    ranked = sorted(((r, h) for h, r in ratios.items()), reverse=True)
    if ranked and ranked[0][0] >= 0.6 and (len(ranked) < 2 or ranked[1][0] <= 0.3):
        return ranked[0][1]
    return None


def mark_user(tramos, micro, skip=()):
    """Hablante que coincide >= 60 % con el micro activo (y el siguiente <= 30 %), o None."""
    return pick_user(mic_ratios(tramos, micro, skip))


# ----------------------------------------------------------------------------- voces.json (personas y huellas)

def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def name_key(nombre):
    """Clave para comparar nombres de personas: sin tildes, sin mayusculas y con los espacios normalizados."""
    s = unicodedata.normalize("NFKD", " ".join(str(nombre or "").split()))
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


_CANDADOS, _CANDADOS_LOCK = {}, threading.Lock()


def _candado(path):
    """Un candado por fichero para todo el proceso: dos Voices() a la vez sobre el mismo voces.json (el servidor tiene
    hilos) no escriben a la vez ni migran dos veces."""
    key = os.path.normcase(os.path.abspath(path))
    with _CANDADOS_LOCK:
        return _CANDADOS.setdefault(key, threading.RLock())


class Voices:
    """DATA_DIR/voces.json: personas con sus huellas. Cifrado con DPAPI (ambito usuario) en Windows; escritura
    atomica con copia .bak (que se elimina al olvidar o fusionar: ninguna copia con huellas sobrevive a un borrado).
    Las huellas solo entran por remember(), que exige el consentimiento."""

    EMPTY = {"version": 1, "modelo": MODEL_ID, "dim": DIM, "umbral": UMBRAL, "contador": 0, "personas": []}

    def __init__(self, path):
        self.path, self.lock, self.error = path, _candado(path), None
        self.data = json.loads(json.dumps(self.EMPTY))
        with self.lock:
            self._load(path)

    def _load(self, path):
        if not os.path.isfile(path):
            # sin voces.json pero con su copia: una escritura que se corto entre las dos (antes del 15/11/2026 la copia
            # se hacia moviendo el fichero): se recupera de la copia. No si se aparto por ilegible (entonces se empieza
            # de cero, como siempre)
            if not os.path.isfile(path + ".bak") or os.path.exists(path + ".ilegible"):
                return
            log("   voces.json no está; se recupera de voces.json.bak")
            path = path + ".bak"
        try:
            raw = read_text(path).strip()
            text = unprotect(raw).decode("utf-8") if raw.startswith(("dpapi:", "plain:")) else raw
            saved = json.loads(text)
            if not isinstance(saved, dict) or not isinstance(saved.get("personas"), list):
                raise ValueError("formato desconocido")
            self.data.update(saved)
        except Exception as e:  # no se pisa: se aparta y se empieza de cero
            self.error = f"{e.__class__.__name__}: {e}"
            if path != self.path:  # la copia tampoco se lee: se deja donde esta (no pisa un .ilegible)
                log(f"   voces.json.bak no se pudo leer ({self.error}); se empieza de cero")
                return
            log(f"   voces.json no se pudo leer ({self.error}); se aparta como voces.json.ilegible")
            try:
                os.replace(path, self.path + ".ilegible")
            except OSError:
                pass
            return
        self._migrar(saved.get("modelo"), recuperado=path != self.path)

    def _migrar(self, anterior, recuperado=False):
        """Huellas de otro modelo (hasta el 15/11/2026, TitaNet small): sus vectores no se pueden comparar con los del
        de ahora (los dos tienen DIM valores, asi que compararlos no fallaria: daria parecidos sin sentido). Cada
        huella lleva su "modelo" desde ese dia; las que no lo llevan o llevan otro (un voces.json de antes, o una que
        guardo una version anterior de la app sobre uno ya migrado) pasan a «rehacer» (reunion, fecha y segundos, sin
        el vector), para recalcularlas desde el audio de esas reuniones (rehacer_huellas). Cada persona conserva id,
        nombre, «Soy yo» y consentimiento. Mientras, no se la reconoce por la voz: mejor que reconocer mal. La copia
        .bak que deja save() es la de antes."""
        n = 0
        for p in self.data["personas"]:
            if not isinstance(p, dict):
                continue
            todas = p.get("huellas") if isinstance(p.get("huellas"), list) else []
            viejas = [h for h in todas if not isinstance(h, dict) or h.get("modelo") != MODEL_ID]
            if not viejas:
                continue
            pend = p.setdefault("rehacer", [])
            for h in viejas:
                if isinstance(h, dict):
                    pend.append({"reunion": h.get("reunion") or "", "fecha": h.get("fecha"),
                                 "segundos": h.get("segundos")})
            n += len(viejas)
            p["huellas"] = [h for h in todas if h not in viejas]
        cambia = n or recuperado or self.data.get("modelo") != MODEL_ID
        if anterior and anterior != MODEL_ID:
            self.data["modelo_anterior"] = anterior
        self.data.update(modelo=MODEL_ID, dim=DIM)
        if not cambia:
            return
        try:
            self.save()
        except OSError as e:  # se queda migrado en memoria; se escribe en el siguiente cambio
            log(f"   voces.json: no se pudo escribir ({e.__class__.__name__})")
        if n:
            log(f"   voces.json: modelo de huellas {MODEL_ID}; {n} huellas por rehacer desde sus reuniones")

    def pendientes(self):
        """[(pid, {"reunion", "fecha", "segundos"})]: las huellas por rehacer con el modelo de ahora."""
        with self.lock:
            return [(p["id"], dict(x)) for p in self.data["personas"] for x in p.get("rehacer") or []]

    def rehecha(self, pid, reunion, vector=None, segundos=None):
        """La huella de `reunion` ya rehecha (vector) o que no se puede rehacer (vector None: la reunion o su audio
        ya no estan, o era la misma reunion otra vez): sale de «rehacer». Con vector, entra con su fecha de entonces,
        sin pedir de nuevo el consentimiento (se dio al guardarla). Devuelve cuantas huellas tiene, o None si la
        persona ya no existe."""
        if vector is not None:  # antes de tocar nada: una huella que no vale no quita la pendiente
            vector = [round(float(x), 5) for x in vector]
            if len(vector) != DIM:
                raise ValueError(f"La huella no tiene {DIM} valores")
        with self.lock:
            p = self.find(pid)
            if not p:
                return None
            pend = p.get("rehacer") or []
            item = next((x for x in pend if x.get("reunion") == reunion), None)
            if item is None:
                return len(p.get("huellas") or [])
            pend.remove(item)
            if not pend:
                p.pop("rehacer", None)
            if vector is not None:
                p.setdefault("huellas", []).append({"vector": vector, "fecha": item.get("fecha") or _now(),
                                                    "reunion": reunion, "segundos": round(float(
                                                        segundos if segundos is not None else
                                                        item.get("segundos") or 0), 1), "modelo": MODEL_ID})
                p["huellas"].sort(key=lambda h: h.get("fecha") or "")
                del p["huellas"][:-MAX_HUELLAS]
            self.save()
            return len(p.get("huellas") or [])

    def save(self, backup=True):
        """backup=False al olvidar o fusionar: el .bak (y un .ilegible apartado) guardarian huellas ya borradas.
        Un .tmp propio por escritura y la copia .bak copiando (no moviendo): voces.json nunca falta, ni con dos
        escrituras a la vez (antes, dos Voices() a la vez podian dejar solo el .bak; revision 2 de motor-lote1)."""
        with self.lock:
            body = protect(json.dumps(self.data, ensure_ascii=False).encode("utf-8"))
            tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(body)
                if backup and os.path.isfile(self.path):
                    try:
                        shutil.copyfile(self.path, self.path + ".bak")
                    except OSError:
                        pass
                os.replace(tmp, self.path)
            finally:  # si no se pudo poner en su sitio, no se queda una copia con huellas por ahi
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            if not backup:  # olvidar: ninguna copia con las huellas borradas (tampoco un .tmp de un fallo de antes)
                import glob
                for extra in [self.path + ".bak", self.path + ".ilegible", self.path + ".tmp"] + \
                        glob.glob(glob.escape(self.path) + ".*.tmp"):
                    try:
                        os.remove(extra)
                    except OSError:
                        pass

    # --- lectura
    def personas(self):
        with self.lock:
            return [json.loads(json.dumps(p)) for p in self.data["personas"]]

    def public(self):
        """Para la interfaz: sin vectores."""
        out = []
        for p in self.personas():
            fechas = [h.get("fecha") or "" for h in p.get("huellas") or []]
            out.append({"id": p["id"], "nombre": p["nombre"], "creada": p.get("creada"),
                        "es_usuario": bool(p.get("es_usuario")), "consentimiento": p.get("consentimiento"),
                        "huellas": len(p.get("huellas") or []), "ultima": max(fechas) if fechas else None,
                        "mezcla": mixed(p.get("huellas")), "por_rehacer": len(p.get("rehacer") or []),
                        "reuniones": sorted({h.get("reunion") for h in p.get("huellas") or [] if h.get("reunion")})})
        return out

    def names(self):
        with self.lock:
            return {p["id"]: p["nombre"] for p in self.data["personas"]}

    def find(self, pid):
        with self.lock:
            return next((p for p in self.data["personas"] if p["id"] == pid), None)

    def user(self):
        with self.lock:
            return next((p for p in self.data["personas"] if p.get("es_usuario")), None)

    def by_name(self, nombre):
        """La persona que ya se llama asi (sin distinguir mayusculas, tildes ni espacios de mas), o None. Asi elegir
        «Ana» de la lista asigna la Ana que ya existe en vez de crear otra. Nunca devuelve la del usuario: un compañero
        puede llamarse como el, y antes del 07/11/2026 la persona del usuario pudo quedarse con el nombre de otra
        (el fallo del msg 1170); el usuario se marca con «Soy yo»."""
        key = name_key(nombre)
        if not key:
            return None
        with self.lock:
            hit = next((p for p in self.data["personas"]
                        if not p.get("es_usuario") and name_key(p.get("nombre")) == key), None)
            return json.loads(json.dumps(hit)) if hit else None

    # --- escritura
    def create(self, nombre=None, es_usuario=False, consentimiento=None):
        with self.lock:
            self.data["contador"] = int(self.data.get("contador") or 0) + 1
            nombre = " ".join((nombre or "").split()) or f"Persona {self.data['contador']}"
            p = {"id": f"p_{time.strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}", "nombre": nombre, "creada": _now(),
                 "es_usuario": bool(es_usuario), "consentimiento": consentimiento, "huellas": []}
            if es_usuario:
                for q in self.data["personas"]:
                    q["es_usuario"] = False
            self.data["personas"].append(p)
            self.save()
            return json.loads(json.dumps(p))

    def remember(self, pid, vector, segundos, reunion, consentimiento):
        """Anade una huella (vector de DIM) a la persona. Sin consentimiento no se guarda nada."""
        if not consentimiento:
            raise ValueError("Falta el consentimiento para guardar la huella de voz")
        vector = [round(float(x), 5) for x in vector]
        if len(vector) != int(self.data.get("dim") or DIM):
            raise ValueError(f"La huella no tiene {DIM} valores")
        with self.lock:
            p = self.find(pid)
            if not p:
                raise KeyError(pid)
            p.setdefault("huellas", []).append({"vector": vector, "fecha": _now(), "reunion": reunion or "",
                                                "segundos": round(float(segundos or 0), 1), "modelo": MODEL_ID})
            p["huellas"].sort(key=lambda h: h.get("fecha") or "")
            del p["huellas"][:-MAX_HUELLAS]
            if not p.get("consentimiento"):
                p["consentimiento"] = consentimiento if isinstance(consentimiento, str) else _now()
            self.save()
            return len(p["huellas"])

    def drop_similar(self, pid, vector, umbral=0.8):
        """Quita de la persona las huellas casi iguales a `vector` (coseno >= umbral: la misma voz por el mismo canal da
        0,97-0,99 y dos personas distintas rara vez pasan de 0,8) y devuelve cuantas. Para la del
        usuario cuando dice que una voz que la app tomaba por la suya es de otra persona: esa huella es la de esa
        persona (antes del 07/11/2026 se guardaba asi por error). Sin copia .bak de lo quitado."""
        with self.lock:
            p = self.find(pid)
            if not p or vector is None:
                return 0
            keep = [h for h in p.get("huellas") or [] if not h.get("vector") or cosine(h["vector"], vector) < umbral]
            n = len(p.get("huellas") or []) - len(keep)
            if n:
                p["huellas"] = keep
                self.save(backup=False)
            return n

    def rename(self, pid, nombre):
        nombre = " ".join((nombre or "").split())
        if not nombre:
            raise ValueError("El nombre no puede estar vacio")
        with self.lock:
            p = self.find(pid)
            if not p:
                raise KeyError(pid)
            p["nombre"] = nombre[:80]
            self.save()

    def set_user(self, pid, flag=True):
        with self.lock:
            p = self.find(pid)
            if not p:
                raise KeyError(pid)
            if flag:
                for q in self.data["personas"]:
                    q["es_usuario"] = False
            p["es_usuario"] = bool(flag)
            self.save()

    def merge(self, keep, other):
        """Dos «Personas» eran la misma: las huellas de other pasan a keep (las 5 mas recientes) y other desaparece.
        Devuelve el id eliminado (para reescribir los sidecars que lo citen)."""
        if keep == other:
            return None
        with self.lock:
            a, b = self.find(keep), self.find(other)
            if not a or not b:
                raise KeyError(other if a else keep)
            if a.get("es_usuario") or b.get("es_usuario"):  # dentro de otra la haria «Tu» con sus huellas; al reves,
                raise ValueError("Tu persona no se puede fundir con otra")  # tu persona se quedaria con otra voz
            both = (a.get("huellas") or []) + (b.get("huellas") or [])
            a["huellas"] = sorted(both, key=lambda h: h.get("fecha") or "")
            del a["huellas"][:-MAX_HUELLAS]
            if a.get("rehacer") or b.get("rehacer"):  # las por rehacer de las dos (migracion de modelo)
                a["rehacer"] = (a.get("rehacer") or []) + (b.get("rehacer") or [])
            a["es_usuario"] = bool(a.get("es_usuario") or b.get("es_usuario"))
            a["consentimiento"] = a.get("consentimiento") or b.get("consentimiento")
            self.data["personas"] = [p for p in self.data["personas"] if p["id"] != other]
            self.save(backup=False)
            return other

    def forget(self, pid):
        with self.lock:
            before = len(self.data["personas"])
            self.data["personas"] = [p for p in self.data["personas"] if p["id"] != pid]
            if len(self.data["personas"]) == before:
                raise KeyError(pid)
            self.save(backup=False)

    def forget_all(self):
        with self.lock:
            self.data["personas"] = []
            self.save(backup=False)


# las de server.AUDIO_EXT (el servidor no se importa desde aqui: este fichero corre como subproceso)
AUDIO_EXT = (".wav", ".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".aac", ".wma", ".opus", ".mkv", ".mov", ".webm", ".oga",
             ".amr", ".3gp", ".3gpp", ".3ga", ".caf", ".qta", ".aif", ".aiff", ".waptt")
SOLO_EN_LINEA = 0x400000 | 0x1000  # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | OFFLINE (OneDrive «solo en línea»)


def buscar_por_nombre(raices, extensiones=AUDIO_EXT):
    """buscar(nombre) para rehacer_huellas: las rutas absolutas de los audios con ese nombre de fichero bajo las raices
    (las carpetas de reuniones), con un solo recorrido, que se hace la primera vez que se pide. Sin carpetas ocultas
    ni la de la app (_app), y sin los ficheros de OneDrive que solo estan en linea (leerlos los descargaria). Si el
    servidor ya tiene su lista de reuniones, mejor un buscar() sobre esa lista."""
    indice, hecho = {}, []

    def construir():
        for raiz in raices or []:
            for d, subs, files in os.walk(raiz):
                subs[:] = [x for x in subs if not x.startswith(".") and x != "_app"]
                for f in files:
                    if not f.lower().endswith(tuple(extensiones)):
                        continue
                    full = os.path.join(d, f)
                    try:
                        if getattr(os.stat(full), "st_file_attributes", 0) & SOLO_EN_LINEA:
                            continue
                    except OSError:
                        continue
                    indice.setdefault(f, []).append(full)

    def buscar(nombre):
        if not hecho:
            hecho.append(True)
            construir()
        return list(indice.get(nombre) or [])
    return buscar


def hablante_de(data, pid, segundos=None):
    """El hablante del sidecar que el usuario puso a mano como esa persona (el de «Recordar»), no uno que la app solo
    propuso (por voz, por el micro o provisional). Si hay varios, el de segundos mas cercanos a los de la huella."""
    hablantes = (data or {}).get("hablantes") if isinstance(data, dict) else None
    if not isinstance(hablantes, dict):
        return None
    cands = [(k, i) for k, i in hablantes.items() if isinstance(i, dict) and i.get("persona") == pid
             and i.get("origen") == "manual" and not i.get("provisional")]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0][0]

    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0
    return min(cands, key=lambda c: abs(num(c[1].get("segundos")) - num(segundos)))[0]


def rehacer_huellas(v, buscar, calcular, cancelled=None):
    """Rehace con el modelo de ahora las huellas que dejo la migracion (Voices._migrar).
    buscar(reunion) -> rutas de audio con ese nombre (la reunion se guardaba por su nombre de fichero);
    calcular(audio, h, sidecar) -> (vector, segundos) de ese hablante (en la app, la huella de «Recordar», en un
    subproceso); sidecar es lo leido al elegir h: quien llama puede comprobar que su "creado" no ha cambiado (si se
    volvio a identificar, h ya es otra voz) y devolver (None, None).
    El hablante es el que su sidecar asigna a mano a esa persona (hablante_de). Una que no se encuentra se queda por
    rehacer (no se pierde nada: se vuelve a buscar la proxima vez); una que falla al calcular, tambien. La misma reunion
    dos veces da una sola huella. Devuelve {"rehechas": n, "sin_audio": n, "fallidas": n}; no lanza."""
    cancelled = cancelled or (lambda: False)
    out = {"rehechas": 0, "sin_audio": 0, "fallidas": 0}
    hechas = set()
    try:
        for pid, item in v.pendientes():
            if cancelled():
                break
            reunion = item.get("reunion") or ""
            if (pid, reunion) in hechas:  # la misma reunion otra vez: ya tiene esa huella
                v.rehecha(pid, reunion, None)
                continue
            estado = "sin_audio"
            for audio in (buscar(reunion) if reunion else []) or []:
                data = read_sidecar(sidecar_path(audio)) or {}
                h = hablante_de(data, pid, item.get("segundos"))
                if h is None:
                    continue
                try:
                    vec, secs = calcular(audio, h, data)
                    if vec is not None:
                        vec = [float(x) for x in vec]
                        secs = float(secs) if secs is not None else None
                        if len(vec) != DIM:
                            raise ValueError(f"la huella no tiene {DIM} valores")
                except Exception as e:  # sin memoria, cancelada, sin el audio: se queda por rehacer
                    log(f"   huella por rehacer ({reunion}, {h}): {e.__class__.__name__}")
                    estado = "fallidas"
                    break
                if vec is not None and v.rehecha(pid, reunion, vec, secs) is not None:  # OSError: fuera
                    estado = "rehechas"
                    hechas.add((pid, reunion))
                    break
            out[estado] += 1
    except OSError as e:  # no se puede escribir voces.json: se para y se sigue la proxima vez
        log(f"   huellas por rehacer: no se pudo escribir voces.json ({e.__class__.__name__})")
    if out["rehechas"] or out["fallidas"]:
        log(f"   huellas rehechas con el modelo nuevo: {out}")
    return out


# ----------------------------------------------------------------------------- sidecar <audio>.hablantes.json

def sidecar_path(audio_abs):
    return os.path.splitext(audio_abs)[0] + SIDECAR_EXT


def read_sidecar(path):
    try:
        data = json.loads(read_text(path))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _strip_vectors(obj):
    if isinstance(obj, dict):
        return {k: _strip_vectors(v) for k, v in obj.items() if k not in ("huellas", "vector", "vectores", "huella")}
    if isinstance(obj, list):
        return [_strip_vectors(x) for x in obj]
    return obj


def write_sidecar(path, data):
    """Junto al audio (OneDrive incluido): se escribe en el sitio, como el .txt y el .srt, y NUNCA con vectores."""
    data = _strip_vectors(data)
    data.setdefault("version", 1)
    write_bytes(path, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return data


def write_micro(path, micro):
    """Al parar la grabadora propia: guarda la linea de tiempo del micro (sin tramos; no es biometria)."""
    data = read_sidecar(path) or {"version": 1}
    data["micro"] = micro
    return write_sidecar(path, data)


def identify(data, huellas, voices, micro=None):
    """Completa hablantes{persona, score, provisional, origen} contra voces.json y «tu» por el micro. Modifica data.
    origen: "huella" (emparejada con voces.json), "micro" (el usuario, por el micro) o "manual" (assign()).

    Cuando el micro y la huella no cuadran no se decide solo, se pregunta (provisional): el micro dice «tu» y la
    huella dice otra persona; o la huella dice «usuario» y ese hablante casi nunca habla por el micro."""
    personas = voices.personas() if voices else []
    u = voices.user() if voices else None
    uid = u["id"] if u else None
    u_mixed = bool(u and mixed(u.get("huellas")))  # puede llevar la voz de otra persona: no se decide por huella
    hablantes = data.setdefault("hablantes", {})
    for h, info in hablantes.items():
        info.setdefault("persona", None)
        info.setdefault("score", None)
        info.setdefault("provisional", False)
        secs = float(info.get("segundos") or 0)
        if info.get("breve") or secs < MIN_MATCH_S or h not in huellas:
            continue
        m = match(huellas[h], personas)
        info.update(persona=m["persona"], score=m["score"], provisional=bool(m["provisional"]) or secs < MIN_HUELLA_S,
                    segunda=m["segunda"], origen="huella" if m["persona"] else None)
    micro = micro or data.get("micro")
    if micro:
        data["micro"] = micro
    breve = {h for h, i in hablantes.items() if i.get("breve")}
    ratios = mic_ratios(data.get("tramos", []), micro, skip=breve) if micro else {}
    if micro:
        fuera = fuera_de_tu(data)
        data["tu"] = pick_user({h: r for h, r in ratios.items() if h not in fuera})
    tu = data.get("tu")
    for h, info in hablantes.items():
        if info.get("origen") != "huella" or not info.get("persona"):
            continue
        if info["persona"] == uid:  # salvo que el micro lo confirme (h == tu)
            if h != tu and (u_mixed or (h in ratios and ratios[h] <= MIC_NO_ES_TU)):
                info["provisional"] = True
        elif h == tu:
            info["provisional"] = True
    if tu and uid:
        info = hablantes.get(tu)
        if info and not info.get("persona"):  # el micro ya dice que eres tu: no hace falta huella
            info.update(persona=uid, provisional=False, origen="micro")
    return data


def assign(data, h, pid, usuario=None):
    """El usuario dice quien es el hablante h en esta reunion (pid, o None = nadie). Modifica data y mantiene «tu»:
    si pid es el usuario, h pasa a ser «tu» y deja de serlo quien lo era solo por el micro; si no, h deja de ser
    «tu». Nunca toca voces.json."""
    hablantes = data.get("hablantes") or {}
    info = hablantes.get(h)
    if info is None:
        raise KeyError(h)
    old_tu = data.get("tu")
    info.update(persona=pid, provisional=False, origen="manual")
    if pid and pid == usuario:
        data["tu"] = h
        for k, other in hablantes.items():  # sin «origen» (sidecar de antes del 07/11/2026): el que era «tu»
            by_micro = other.get("origen") == "micro" or (not other.get("origen") and k == old_tu)
            if k != h and other.get("persona") == usuario and by_micro:
                other.update(persona=None, provisional=False, origen=None)
    elif data.get("tu") == h:
        data["tu"] = None
    return data


def join_speakers(data, h, con, usuario=None, names=None):
    """«Es el mismo que…» (msg 1242): el hablante h de esta reunion es la misma voz que `con`, que la app partio en
    dos. Los tramos de h pasan a `con`, que conserva quien es (o toma lo que se sabia de h si `con` no lo sabia), y h
    desaparece. Si ya se sabe que son dos personas distintas (dos personas, o tu y otra), ValueError: eso no es unir,
    sino corregir a uno de los dos. `names`: ids de las personas que siguen en voces.json. Modifica data."""
    hablantes = data.get("hablantes") or {}
    if h == con or h not in hablantes or con not in hablantes:
        raise KeyError(con if h in hablantes else h)
    names = names or {}

    def who(x):  # quien es, si ya esta decidido (lo provisional y lo que dice el micro, no)
        info = hablantes[x]
        pid = info.get("persona")
        if not pid or pid not in names or info.get("provisional") or info.get("origen") == "micro":
            return None
        return "tu" if pid == usuario else pid

    wh, wc = who(h), who(con)
    if wh and wc and wh != wc:
        raise ValueError("Ya son dos personas distintas: corrige el nombre de uno de los dos.")
    if wh and not wc:
        for k in ("persona", "score", "provisional", "origen"):
            if k in hablantes[h]:
                hablantes[con][k] = hablantes[h][k]
    ident = wh or wc
    if ident and ident != "tu":  # `con` es otra persona: ninguno de los dos eres tu
        if data.get("tu") in (h, con):
            data["tu"] = None
    elif data.get("tu") == h:
        data["tu"] = con
    mine = [(float(t["inicio"]), float(t["fin"])) for t in data.get("tramos") or [] if t.get("h") in (h, con)]
    rest = [t for t in data.get("tramos") or [] if t.get("h") not in (h, con)]
    joined = _intervals(mine)
    data["tramos"] = sorted(rest + [{"inicio": round(a, 2), "fin": round(b, 2), "h": con} for a, b in joined],
                            key=lambda t: (t["inicio"], t["fin"]))
    secs = sum(b - a for a, b in joined)
    hablantes[con].update(segundos=round(secs, 1), breve=secs < BREVE_S)
    if hablantes[h].get("pista") != hablantes[con].get("pista"):  # dos pistas: uno de cada canal, su huella de la suma
        hablantes[con].pop("pista", None)
    del hablantes[h]
    for f in data.get("frases") or []:
        if isinstance(f, dict) and f.get("h") == h:
            f["h"] = con
    return data


def carry_manual(old, new, usuario=None, known=None):
    """Volver a identificar no borra lo que el usuario ya dijo: cada hablante nuevo hereda la decision manual del
    hablante antiguo con el que mas coincide en el tiempo (si cubre al menos la mitad de su voz), salvo que esa
    persona ya no exista (`known`: ids que siguen en voces.json). Modifica new."""
    olds = {h: i for h, i in (old.get("hablantes") or {}).items() if i.get("origen") == "manual"}
    if not olds or not new.get("tramos"):
        return new
    step = 0.1  # mascaras de 0,1 s: lineal en la duracion (dos horas son 72.000 casillas por hablante)
    end = min(max(float(t["fin"]) for t in (old.get("tramos") or []) + new["tramos"]), 12 * 3600.0)
    n = int(math.ceil(end / step)) + 1

    def mask(tramos, h):
        m = np.zeros(n, bool)
        for t in tramos:
            if t.get("h") == h:
                m[max(0, int(float(t["inicio"]) / step)):max(0, int(math.ceil(float(t["fin"]) / step)))] = True
        return m
    old_masks = {k: mask(old.get("tramos") or [], k) for k in olds}
    for h in list(new.get("hablantes") or {}):
        mine = mask(new["tramos"], h)
        total = int(mine.sum())
        if not total:
            continue
        best, cover = max(((k, int((mine & m).sum())) for k, m in old_masks.items()), key=lambda x: x[1])
        if cover / total >= 0.5:
            pid = olds[best].get("persona")
            if pid and known is not None and pid not in known:
                continue
            assign(new, h, pid, usuario)
    return new


def refresh_frases(data, segs):
    """Casado con el .srt (clave: inicio de la frase, como seg_key en server.py). Barato: se rehace al leer."""
    who = assign_srt(segs, data.get("tramos", []))
    data["frases"] = [{"inicio": round(float(s["start"]), 2), "h": w} for s, w in zip(segs, who)
                      if s.get("start") is not None]
    return who


def label(h, data, names, for_llm=False, usuario=None):
    """Etiqueta de un hablante. Manda la persona asignada (a mano, por huella o por el micro): su nombre, o «Tú» si es
    la del usuario (`usuario` = su id; en el resumen, su nombre o «Yo»), con «¿…?» si es provisional. Sin persona,
    «Tú» si el micro lo dice; si no, «(breve)» o «Persona N». También «varios» y None."""
    if h is None:
        return None
    if h == "varios":
        return "varios"
    info = (data.get("hablantes") or {}).get(h) or {}
    pid = info.get("persona")
    name = names.get(pid) if pid else None  # None tambien si la persona se olvido
    doubt = bool(info.get("provisional"))
    if name is not None and (name or pid == usuario):
        if pid == usuario:
            shown = (name or "Yo") if for_llm else "Tú"
        else:
            shown = name
        return f"¿{shown}?" if doubt else shown
    if data.get("tu") == h:
        return "Yo" if for_llm else "Tú"
    if info.get("breve"):
        return "(breve)"
    n = h[1:] if h.startswith("h") and h[1:].isdigit() else h
    return f"Persona {n}"


def is_user(h, data, names, usuario=None):
    """El hablante h es el usuario en esta reunion: su persona es la del usuario o, sin persona, el micro lo dice."""
    info = (data.get("hablantes") or {}).get(h) or {}
    pid = info.get("persona")
    if pid and pid in names:
        return pid == usuario
    return data.get("tu") == h


def export_text(segs, who, data, names, usuario=None):
    """<audio>.hablantes.txt: un parrafo por turno, con la etiqueta delante y el minuto del primer segmento."""
    out, cur, cur_h, t0 = [], [], object(), None
    for s, w in zip(segs, who):
        if w != cur_h and cur:
            out.append((cur_h, t0, " ".join(cur)))
            cur = []
        if not cur:
            cur_h, t0 = w, s.get("start")
        cur.append(s["text"])
    if cur:
        out.append((cur_h, t0, " ".join(cur)))
    lines = []
    for h, t, text in out:
        stamp = "" if t is None else f"[{int(t) // 60}:{int(t) % 60:02d}] "
        who_ = label(h, data, names, usuario=usuario)
        lines.append(f"{stamp}{who_ + ': ' if who_ else ''}{text}")
    return "\n\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- CLI (subproceso)

TMP_VIEJO_S = 6 * 3600  # un voces-*.pcm de hace más de esto ya no es de ningún subproceso vivo


def _suma_en_disco(stereo16, ruta, trozo=16000 * 60):
    """La suma de las dos pistas (pistas.a_mono_i16) de un memmap (n, 2) a otro archivo, por trozos: memmap (n,)."""
    n = len(stereo16)
    if not n:
        return np.zeros(0, np.int16)
    with open(ruta, "wb") as f:
        for i in range(0, n, trozo):
            f.write(pistas.a_mono_i16(np.asarray(stereo16[i:i + trozo])).astype("<i2").tobytes())
    return np.memmap(ruta, dtype="<i2", mode="r", shape=(n,))


def _borrar_tmp(path):
    """Borra el audio decodificado de esta ejecución y los que hayan quedado de otras de hace más de TMP_VIEJO_S (un
    subproceso que se cancela muere sin llegar aquí). En Windows, un archivo que sigue mapeado no se puede borrar: si
    algo (una excepción con sus marcos) aún lo tiene, se recoge la basura y se prueba otra vez; si tampoco, lo borra
    CloudEngine.purge_tmp al arrancar o cerrar la app."""
    for intento in range(2):
        try:
            os.remove(path)
            break
        except FileNotFoundError:
            break
        except OSError:
            if intento == 0:
                import gc
                gc.collect()
    d = os.path.dirname(path)
    try:
        viejos = [e.path for e in os.scandir(d) if e.name.startswith("voces-") and e.name.endswith(".pcm")
                  and e.path != path and time.time() - e.stat().st_mtime > TMP_VIEJO_S]
    except OSError:
        return
    for v in viejos:
        try:
            os.remove(v)
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Diarizacion en local (subproceso de la app)")
    ap.add_argument("--diarize", required=True, metavar="AUDIO")
    ap.add_argument("--out", required=True, metavar="SIDECAR")
    ap.add_argument("--niveles", metavar="FICHERO", help="JSON {paso, activo} de la linea de tiempo del micro")
    ap.add_argument("--data", default=os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                                                   "AudiosApp"))
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--fingerprint", metavar="H", help="solo recalcular la huella de ese hablante del sidecar")
    ap.add_argument("--participantes", type=int, default=0, help="personas nombradas en la reunion (pista)")
    a = ap.parse_args(argv)
    proto = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)  # cualquier print de las librerias nativas va a stderr y no rompe el protocolo

    def send(obj):
        proto.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        proto.flush()

    def progress(stage=None, frac=None):
        msg = {"type": "progress"}
        if stage is not None:
            msg["stage"] = stage
        if frac is not None:
            msg["frac"] = round(float(frac), 4)
        send(msg)

    # el audio decodificado, en un archivo de DATA_DIR/tmp (el servidor vacía esa carpeta al arrancar y al cerrarse)
    tmp = os.path.join(a.data, "tmp", f"voces-{os.getpid()}-{uuid.uuid4().hex[:8]}.pcm")
    tmp_suma = tmp[:-4] + "-suma.pcm"  # dos pistas en una reunion presencial: la suma de las dos
    tmp_micro = tmp[:-4] + "-micro.pcm"  # dos pistas: los tramos del micro, juntados
    tmp_demas = tmp[:-4] + "-demas.pcm"  # y los de lo que suena
    samples = None
    try:
        if not models_ready(a.data):  # p. ej. tras actualizar con otro modelo de huellas: «Recordar» antes que nada
            ensure_models(a.data, progress)
        progress(stage="Leyendo audio…", frac=0.01)
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        if a.fingerprint:
            # «Recordar» desde la etiqueta, con el usuario esperando: solo los tramos de su huella, no la reunion
            # entera (fix-lento, 10/11/2026: en una de 3 h en m4a eran ~40 s en un Mac rapido y minutos en un portatil)
            data = read_sidecar(a.out) or {}
            tramos = [(float(t["inicio"]), float(t["fin"])) for t in data.get("tramos", [])
                      if t.get("h") == a.fingerprint]
            if not tramos:
                raise VoicesError(f"El sidecar no tiene tramos de {a.fingerprint}")
            canal = canal_de(data, a.fingerprint) if pistas.es_doble(a.diarize) else None
            voz, _ = audio_huella(a.diarize, tramos, data.get("duracion_s"), en_disco=tmp, canal=canal)
            so = _sherpa()
            vec = embed(_extractor(so, model_paths(a.data), a.threads), _f32(voz)) if len(voz) else None
            if vec is None:
                raise VoicesError("Voz insuficiente para una huella")
            send({"type": "result", "huellas": {a.fingerprint: [round(float(x), 5) for x in vec]},
                  "segundos": sum(b - a_ for a_, b in tramos)})
            return 0
        micro = None
        if a.niveles:
            try:
                micro = json.loads(read_text(a.niveles))
            except (OSError, ValueError):
                micro = None
        old = read_sidecar(a.out) or {}
        micro = micro or old.get("micro")
        res = None
        if pistas.es_doble(a.diarize) and micro_de_grabadora(micro):
            samples = load_audio_16k(a.diarize, en_disco=tmp, canal="ambos")
            res = diarize_pistas(samples, micro, a.data, a.threads, progress, participantes=a.participantes or None,
                                 tmp_micro=tmp_micro, tmp_demas=tmp_demas)
            if res is None:  # presencial: la suma, sin decodificar otra vez
                samples = _suma_en_disco(samples, tmp_suma)
        else:
            samples = load_audio_16k(a.diarize, en_disco=tmp)
        if res is None:
            res = diarize(samples, a.data, a.threads, progress, participantes=a.participantes or None)
        data = {"version": 1, "modelo": MODEL_ID, "creado": _now(), "duracion_s": res["duracion_s"],
                "tramos": res["tramos"], "hablantes": res["hablantes"], "tu": None, "frases": [],
                "micro": micro}
        if res.get("pistas"):
            data["pistas"] = True
        data["tu"] = mark_user(data["tramos"], data["micro"], skip=fuera_de_tu(data)) if data["micro"] else None
        write_sidecar(a.out, data)
        send({"type": "result", "sidecar": a.out, "huellas": res["huellas"], "hablantes": len(res["hablantes"]),
              "duracion_s": res["duracion_s"]})
        return 0
    except Exception as e:
        send({"type": "error", "error": str(e) or e.__class__.__name__})
        return 1
    finally:
        del samples  # suelta el memmap: en Windows un archivo mapeado no se puede borrar
        _borrar_tmp(tmp)
        _borrar_tmp(tmp_suma)
        _borrar_tmp(tmp_micro)
        _borrar_tmp(tmp_demas)


if __name__ == "__main__":
    sys.exit(main())
