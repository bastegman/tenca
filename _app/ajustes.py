# -*- coding: utf-8 -*-
"""Ajustes: el fichero settings.json (Store: guardado atomico, claves cifradas, migraciones al leerlo), los valores por
defecto (DEFAULT_SETTINGS), el procesado y el proveedor elegidos, y lo que cambia en los ajustes al mover o renombrar
una carpeta o una reunion (settings_moved) o al cambiar el nombre de la app (migrate_app_name).

Salio tal cual de server.py al partirlo (15/11/2026); server.py lo reexporta."""
import copy
import json
import math
import os
import re
import shutil
import threading
import time

import engines
from common import ApiError, default_threads, fold, is_protected, log, protect, unprotect
from rutas import AUDIO_EXT, is_link_root, root_name, safe


class Store:
    """JSON en %LOCALAPPDATA%\\AudiosApp (fuera de OneDrive: la API key no se sincroniza).
    Los SECRETS se guardan cifrados con DPAPI (common.protect); en memoria y en get() van en claro. Un fichero
    antiguo con la clave en claro se lee igual y queda cifrado en el primer guardado (migracion transparente).
    Si no se puede descifrar (otro usuario de Windows, otro PC), la clave queda vacia y se avisa en el log.

    Un settings.json ilegible (JSON mal formado o cortado, o que no es un objeto) no se ignora en silencio, porque el
    primer guardado lo pisaria con los valores por defecto (API key, carpetas vinculadas y destinos vacios): se copia
    tal cual como settings.json.roto-<fecha>, se rescata de el lo que mas cuesta volver a poner (todos los SECRETS y
    RESCUE), se anota en el log y `broken` se lo cuenta a la interfaz. Si ni siquiera se puede hacer la copia, en toda
    la sesion no se escribe encima del original (`frozen`): los cambios se quedan en memoria."""

    # un texto o una lista de textos; las claves adicionales van por proveedor (engines.EXTRA_KEYS_FIELDS)
    SECRETS = (("cloud", "api_key"), ("cloud", "extra_keys"), ("cloud", "extra_keys_openai"),
               ("cloud", "extra_keys_azure"), ("cloud", "extra_keys_custom"))

    def __init__(self, path, defaults):
        self.path, self.lock = path, threading.Lock()
        self.data = copy.deepcopy(defaults)
        self.broken, self.frozen = None, False  # settings.json ilegible: {"copia", "error", "recuperado"}
        for k, v in self._load().items():
            if isinstance(v, dict) and isinstance(self.data.get(k), dict):
                self.data[k].update(v)
            else:
                self.data[k] = v
        # un valor de tipo o de valor raro (escrito a mano, versiones mezcladas): el de por defecto, con su linea en el
        # log (punto 5 del review del 14/11/2026). `corregidos`: las claves, para el diagnostico
        self.corregidos = normalizar_ajustes(self.data, defaults)
        self.lost = []  # secretos que no se pudieron descifrar (la interfaz pide pegarlos otra vez)
        # config_rev: sube con cada cambio de Ajustes (update(..., rev=True)). Empieza en la hora de arranque, asi
        # que tras reiniciar no coincide con la que tuviera una ventana abierta antes
        self.rev = int(time.time() * 1000)
        for keys in self.SECRETS:
            d = self.data
            for k in keys[:-1]:
                d = d.get(k) if isinstance(d, dict) else None
            if not isinstance(d, dict):
                continue
            v = d.get(keys[-1])
            items = v if isinstance(v, list) else [v]
            out = []
            for item in items:
                if not is_protected(item):
                    out.append(item)
                    continue
                try:
                    out.append(unprotect(item).decode("utf-8"))
                except (OSError, ValueError) as e:
                    out.append("")
                    if ".".join(keys) not in self.lost:
                        self.lost.append(".".join(keys))
                    log(f"   no se pudo descifrar {'.'.join(keys)} de settings.json ({e}): vuelve a pegarla en Ajustes")
            d[keys[-1]] = [x for x in out if x] if isinstance(v, list) else out[0]

    def _load(self):
        """Lo guardado ({} si no hay fichero). Con BOM tambien (el Bloc de notas lo pone). Si el fichero existe pero no
        se puede leer o no es un objeto JSON, lo que _broken haya podido rescatar."""
        try:
            with open(self.path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            return {}
        except OSError as e:  # existe pero no se deja leer: tampoco se pisa a ciegas
            return self._broken(None, e)
        try:
            saved = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(saved, dict):
                raise ValueError(f"no es un objeto JSON ({type(saved).__name__})")
            bad = [k for k, v in saved.items() if isinstance(self.data.get(k), dict) and not isinstance(v, dict)]
            if bad:  # p. ej. "cloud": null: con eso la app no arranca; lo demas si vale
                raise ValueError("secciones que no son un objeto: " + ", ".join(bad))
            return saved
        except ValueError as e:  # UnicodeDecodeError y JSONDecodeError incluidos
            return self._broken(raw, e)

    def _broken(self, raw, err):
        """settings.json ilegible: copia del fichero tal cual, rescate (los SECRETS y RESCUE), log y aviso para la
        interfaz."""
        why = f"{err.__class__.__name__}: {err}"[:200]
        copia = f"{self.path}.roto-{time.strftime('%Y-%m-%d_%H-%M-%S')}"
        n = 1
        while os.path.exists(copia):
            copia, n = f"{self.path}.roto-{time.strftime('%Y-%m-%d_%H-%M-%S')}-{n}", n + 1
        try:
            shutil.copy2(self.path, copia)
        except OSError as e:
            log(f"   no se pudo copiar settings.json ({e.__class__.__name__}): no se escribirá encima en esta sesión")
            copia, self.frozen = None, True
        saved = self._rescue(raw) if raw else {}

        def dig(keys):
            d = saved
            for k in keys:
                d = d.get(k) if isinstance(d, dict) else None
            return d

        def cuantos(n, uno, varios):
            return [f"{n} {uno if n == 1 else varios}"] if n else []
        extra = sum(len(dig(k) or []) for k in self.SECRETS if k != ("cloud", "api_key"))  # de todos los proveedores
        recuperado = ((["la API key"] if dig(("cloud", "api_key")) else [])
                      + cuantos(extra, "clave adicional", "claves adicionales")
                      + cuantos(len(dig(("links",)) or []), "carpeta vinculada", "carpetas vinculadas")
                      + cuantos(len(dig(("vocab_links",)) or []), "carpeta de listas", "carpetas de listas")
                      + cuantos(len(dig(("perfil", "outputs")) or []), "destino de salida", "destinos de salida")
                      + cuantos(len(dig(("project_folders",)) or {}) + len(dig(("workstream_folders",)) or {}),
                                "carpeta principal asignada", "carpetas principales asignadas")
                      + (["la carpeta por reunión apagada"] if dig(("carpeta_por_reunion",)) is False else []))
        self.broken = {"copia": os.path.basename(copia) if copia else None, "error": why, "recuperado": recuperado}
        log(f"   settings.json no se puede leer ({why})"
            + (f": copia en {os.path.basename(copia)}" if copia else "")
            + f"; recuperado: {', '.join(recuperado) or 'nada'}; lo demás vuelve a los valores por defecto")
        return saved

    # de un settings.json roto, ademas de los SECRETS. Las carpetas principales asignadas (y si la carpeta por reunion
    # esta apagada, con su marca de migracion) tambien: no hay donde volver a ponerlas en la interfaz (revision de la
    # integracion del 08/11/2026)
    RESCUE = (("links",), ("vocab_links",), ("perfil", "outputs"), ("project_folders",), ("workstream_folders",),
              ("carpeta_por_reunion",), ("carpetas_v",), ("nombre_v",), ("aprendido",))

    @classmethod
    def _rescue(cls, raw):
        """De un settings.json roto, los SECRETS (todos: tambien los que se anadan) y lo de RESCUE que se pueda leer
        entero: cada valor se decodifica con raw_decode desde su clave, asi que vale aunque el fichero este cortado o
        mal escrito en otra parte."""
        text = raw.decode("utf-8", errors="replace").lstrip("\ufeff")
        dec, out = json.JSONDecoder(), {}
        for keys in cls.SECRETS + cls.RESCUE:
            for m in re.finditer('"' + re.escape(keys[-1]) + r'"\s*:\s*', text):
                try:
                    v, _ = dec.raw_decode(text, m.end())
                except ValueError:
                    continue
                if keys == ("cloud", "api_key"):
                    ok = isinstance(v, str) and bool(v.strip())
                elif keys in cls.SECRETS:  # las claves adicionales (las de cada proveedor): listas de textos
                    ok = isinstance(v, list) and bool(v) and all(isinstance(x, str) for x in v)
                elif keys in (("project_folders",), ("workstream_folders",)):  # {proyecto o id de lista: carpeta}
                    ok = isinstance(v, dict) and bool(v) and all(isinstance(a, str) and isinstance(b, str)
                                                                 for a, b in v.items())
                elif keys == ("carpeta_por_reunion",):  # encendida es lo de por defecto
                    ok = v is False
                elif keys == ("carpetas_v",):  # la migracion ya hecha no se repite
                    ok = isinstance(v, int) and not isinstance(v, bool) and v >= 1
                elif keys == ("nombre_v",):  # la del nombre de la app (migrate_app_name), tampoco
                    ok = isinstance(v, str) and bool(v)
                elif keys == ("aprendido",):  # lo que aprendiste a corregir: no hay de donde volver a sacarlo
                    ok = isinstance(v, dict) and any(aprendido_de({"aprendido": v}).values())
                else:  # links, vocab_links y outputs: listas de {"path": …}
                    ok = isinstance(v, list) and bool(v) and all(isinstance(x, dict) and isinstance(x.get("path"), str)
                                                                 for x in v)
                if ok:
                    d = out
                    for k in keys[:-1]:
                        d = d.setdefault(k, {})
                    d[keys[-1]] = v
                    break
        return out

    def get(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def update(self, fn, rev=False):
        with self.lock:
            fn(self.data)
            if rev:
                self.rev += 1
            out = copy.deepcopy(self.data)
            for keys in self.SECRETS:
                d = out
                for k in keys[:-1]:
                    d = d.get(k) if isinstance(d, dict) else None
                if isinstance(d, dict) and isinstance(d.get(keys[-1]), str) and d[keys[-1]]:
                    d[keys[-1]] = protect(d[keys[-1]])
                elif isinstance(d, dict) and isinstance(d.get(keys[-1]), list):
                    d[keys[-1]] = [protect(x) for x in d[keys[-1]] if isinstance(x, str) and x]
            if self.frozen:  # settings.json ilegible y sin copia: escribir encima perderia lo que tenga
                return
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())  # en disco antes de sustituir: un corte de luz no deja un settings.json vacio
            os.replace(tmp, self.path)


DEFAULT_SETTINGS = {
    "preset": "rapido",
    "language": "",  # "" = detectar; "es-en" = espanol + ingles (spanglish): idioma por bloque, solo entre esos dos
    "threads": default_threads(),
    "prompt": "",
    "live_auto": True,       # transcribir solo en cuanto OBS empieza a grabar
    "live_preset": "medium",  # "medium" (preciso), "rapido" (small) o "nube"
    "live_final_pass": True,  # directo en la nube: al terminar, repasar la grabacion entera (mejor texto)
    # grabadora propia: "default" sigue al micro de Windows (cascos incluidos); "all" graba todas las
    # salidas a la vez, asi da igual por donde saque Teams el audio. "none" = sin esa fuente
    "rec_mic": "default",
    "rec_sys": "all",
    "rec_vocab": "",          # ultimo workstream elegido al grabar (<proyecto>/Vocabulario/<workstream>.txt)
    "links": [],              # carpetas vinculadas: [{"name", "path"}] (fuera de Audios, p. ej. la del proyecto)
    "vocab_links": [],        # carpetas de listas conectadas: [{"name", "path"}] (se leen en vivo; VOCAB_LINKS)
    "rec_folder": "",         # ultima carpeta donde se grabo (para las reuniones sin tema)
    # carpeta principal asignada (rec_folder_origin): proyecto -> carpeta y, para un workstream que quiera la suya, id
    # de su lista -> carpeta. Sin asignar, la del proyecto en el arbol (la que tiene su Vocabulario). Hasta el
    # 07/11/2026 project_folders era «la ultima elegida» para ese proyecto: lo que habia se queda como asignacion
    "project_folders": {},
    "workstream_folders": {},
    # una carpeta por reunion, «AAAA-MM-DD Nombre», dentro de la principal: el audio y sus textos van dentro
    "carpeta_por_reunion": True,
    # plan de grabacion (plan.py, 07/11/2026): idioma por proyecto y por workstream (id de su lista), y si al grabar se
    # transcribe en directo por defecto (Ajustes › Al grabar; «Solo grabar» en la tarjeta vale para esa reunion)
    "project_langs": {},
    "workstream_langs": {},
    "rec_directo": True,
    "favorites": [],          # [{"kind": "a"|"f", "path": ...}] (estilo Notion)
    "folder_icons": {},       # carpeta -> emoji
    "meeting_detect": True,   # avisar si Teams/Zoom/… usa el micro y no se esta grabando
    # que la ventana no salga al compartir pantalla ni en capturas (window.py); se aplica al momento
    "ocultar_al_compartir": True,
    "outlook_on": False,      # leer la cita en curso de Outlook clasico (COM, pywin32): nombre, tema y asistentes
    # si Outlook deja leer los asistentes sin preguntar ("si"), pregunta ("pregunta": solos no se leen, solo con
    # «Probar») o no deja ("deniega"); "" = sin comprobar. Lo apunta calendario.py (Object Model Guard)
    "outlook_asistentes": "",
    "rec_aviso": True,        # aviso flotante «Grabar / ✕» al detectar una llamada (calendario.py + aviso.py)
    "summary_auto": True,     # resumen con IA al terminar de transcribir una reunion reciente
    # ...tambien si se transcribio en local (consentimiento: el resumen manda el texto al proveedor de la nube). Sin
    # el, lo transcrito en local no sale del PC por el resumen automatico (Jobs.auto_summary)
    "summary_auto_local": False,
    # aviso unico al actualizar a la version que deja de subir lo local (App._aviso_resumen_local): None = sin decidir,
    # True = pendiente (la interfaz lo ensena en Inicio), False = visto o no aplica
    "aviso_resumen_local": None,
    "summary_model": "openai/gpt-oss-120b",
    "summary_lang": "es",     # es / en / same (el de la reunion)
    "summary_extra": "",     # instrucciones extra para el resumen
    # donde se procesan las reuniones: "local" (nada sale del PC), "agente" (resumen con la cuenta del usuario a
    # traves de Claude Code) o "api" (nube: transcripcion y resumen con API key). None = sin decidir: al arrancar
    # pasa a "api" si hay API key (instalaciones anteriores) y si no a "local" (App.__init__, procesado_de)
    "procesado": None,
    # el agente del PC: {"tipo": "claude", "modelo": "sonnet"} en cuanto se guarda desde Ajustes; None por defecto (asi
    # un "agente": null escrito a mano no da el fichero por roto: agente_de lo resuelve)
    "agente": None,
    # aprender de tus correcciones (aprender.py): reglas «lo que escribio Whisper → lo correcto» que aceptaste, por
    # proyecto, y lo que dijiste que no (aprendido_de lo valida)
    "aprendido": {"reglas": [], "no": [], "no_reglas": []},
    "voices_enabled": False,  # quien habla (diarizacion local + huellas de voz): opcional, apagado por defecto
    # perfil de reunion por defecto (Ajustes › Al grabar, y «Usarlo en: todas» en la tarjeta de grabar).
    # None en folder/vocab = automatico, como siempre: la ultima carpeta usada (o la del proyecto del tema) y el tema
    # que sugiera la reunion en curso o el ultimo elegido. El idioma por defecto sigue siendo "language".
    "perfil": {"folder": None,    # carpeta de destino fija (ruta interna, "" = la raiz) o None
               "vocab": None,     # tema fijo (id <proyecto>/Vocabulario/<tema>.txt, "" = sin vocabulario) o None
               "context": "",     # contexto libre: de que van las reuniones y quien participa (Whisper y resumen)
               # destinos de salida: cada reunion como documento en estas carpetas (salidas.py), al terminar la
               # transcripcion y el resumen y al guardar cambios: [{"path", "format": md|txt|docx, "by_project"}]
               "outputs": []},
    # Whisper en local: minutos que el modelo sigue cargado tras la ultima reunion (0 = 5 min, como antes) y si se
    # precarga al arrancar el que usara el directo (solo si ya esta descargado; medido: 2-6 s de arranque en frio)
    "worker_keepalive_min": 30,
    "worker_preload": True,
    # nube: whisper-large-v3 (el mas preciso; -turbo es mas rapido y algo menos fino)
    "cloud": {"provider": "groq", "api_key": "", "model": "whisper-large-v3", "base_url": "",
              "azure_endpoint": "", "azure_deployment": "", "azure_api_version": "2024-06-01",
              "chunk_minutes": 10, "parallel": 3,
              # directo en la nube: modelo rapido por trozo y claves de otras cuentas de Groq (rotacion de cupo)
              "live_model": engines.LIVE_MODEL_DEFAULT, "extra_keys": [],
              # «Otra (compatible)»: modelo del directo propio ("" = el de la cola) y sus claves adicionales; las
              # claves adicionales de cada proveedor solo se usan con el (engines.EXTRA_KEYS_FIELDS)
              "live_model_custom": "", "extra_keys_openai": [], "extra_keys_azure": [], "extra_keys_custom": []},
    "speed": {},
}

PROCESADOS = ("local", "agente", "api")
PRUEBA_VOZ_DB = -35.0  # «Probar»: pico del micro (dB, cada 0,25 s) que cuenta como voz; supuesto, sin medir
NUBE_OFF = ("La transcripción en la nube está desactivada: en Ajustes → «Dónde se procesan tus reuniones» elige "
            "«API externa».")


# ----------------------------------------------------------------------------- ajustes a prueba de valores raros
# Punto 5 del review del 14/11/2026: un valor de otro tipo en settings.json (escrito a mano, una version mezclada)
# dejaba la app sin arrancar o «cargando» (fuzz de 583 casos: 21 sin arrancar y 58 con /api/config, Hoy, el diagnostico
# o la lista de modelos rotos; el 10/11/2026, un proveedor antiguo la dejo sin transcripcion). Al cargar, cada clave de
# DEFAULT_SETTINGS se compara con su tipo y, si tiene una lista cerrada o un rango, con ellos; si no cuadra, el valor
# por defecto y una linea en el log. En las listas y los mapas solo se quitan los elementos que no valen. Lo que no
# esta en DEFAULT_SETTINGS (marcas de migracion, lo que guarda la app aparte) no se toca, y el proveedor de la nube
# solo se mira por su tipo: su valor lo arregla reparar_proveedor (fix-groq), que dice en el log a que pasa.
_MAL = object()  # «no vale»: vuelve el de por defecto


def _entero(lo, hi):
    def f(v):
        if isinstance(v, float) and v.is_integer():  # 30.0 escrito a mano es 30
            v = int(v)
        return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else _MAL
    return f


def _uno_de(validos):
    return lambda v: v if isinstance(v, str) and v in validos() else _MAL


def _texto(v):
    return v if isinstance(v, str) else _MAL


def _texto_lleno(v):
    return v if isinstance(v, str) and v.strip() else _MAL


def _o_nada(f):
    return lambda v: None if v is None else f(v)


def _lista_de(elemento):
    """Una lista: los elementos que no valen, fuera (el resto se queda)."""
    return lambda v: [x for x in v if elemento(x)] if isinstance(v, list) else _MAL


def _mapa_de(valor):
    """Un mapa de textos: las entradas que no valen, fuera."""
    return lambda v: ({a: b for a, b in v.items() if isinstance(a, str) and valor(b)} if isinstance(v, dict)
                      else _MAL)


def _con_ruta(x):  # carpetas vinculadas, de listas y favoritos: {"path": …, "name"?: …}
    return (isinstance(x, dict) and isinstance(x.get("path"), str) and bool(x["path"].strip())
            and isinstance(x.get("name", ""), (str, type(None))))


def _es_texto(x):
    return isinstance(x, str)


def _velocidad(x):  # «speed»: veces tiempo real medidas por modelo
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _presets():
    return engines.PRESETS_BY_ID


# por clave (las de un nivel por debajo, con su seccion): lo que vale. Sin entrada, el tipo de su valor por defecto
VALIDOS = {
    ("preset",): _uno_de(_presets),
    ("live_preset",): _uno_de(_presets),
    ("threads",): _entero(1, 1024),
    ("rec_mic",): _texto_lleno,
    ("rec_sys",): _texto_lleno,
    ("links",): _lista_de(_con_ruta),
    ("vocab_links",): _lista_de(_con_ruta),
    ("favorites",): _lista_de(_con_ruta),
    ("project_folders",): _mapa_de(_es_texto),
    ("workstream_folders",): _mapa_de(_es_texto),
    ("project_langs",): _mapa_de(_es_texto),
    ("workstream_langs",): _mapa_de(_es_texto),
    ("folder_icons",): _mapa_de(_es_texto),
    ("outlook_asistentes",): _uno_de(lambda: ("", "si", "pregunta", "deniega")),
    ("aviso_resumen_local",): _o_nada(lambda v: v if isinstance(v, bool) else _MAL),
    ("summary_lang",): _uno_de(lambda: engines.SUMMARY_LANGS),
    ("procesado",): _o_nada(_uno_de(lambda: PROCESADOS)),
    ("agente",): _o_nada(lambda v: v if isinstance(v, dict) else _MAL),  # lo de dentro lo resuelve agente_de
    ("worker_keepalive_min",): _entero(0, 720),
    ("perfil", "folder"): _o_nada(_texto),
    ("perfil", "vocab"): _o_nada(_texto),
    ("cloud", "provider"): _texto,  # el valor, reparar_proveedor
    ("cloud", "chunk_minutes"): _entero(2, 20),
    ("cloud", "parallel"): _entero(1, 8),
    ("cloud", "extra_keys"): _lista_de(_es_texto),
    ("cloud", "extra_keys_openai"): _lista_de(_es_texto),
    ("cloud", "extra_keys_azure"): _lista_de(_es_texto),
    ("cloud", "extra_keys_custom"): _lista_de(_es_texto),
    ("speed",): _mapa_de(_velocidad),
}


def _por_tipo(defecto):
    """Sin entrada en VALIDOS: del mismo tipo que el de por defecto (un entero no vale por un si/no ni al reves).
    Con None por defecto, cualquier cosa (la resuelve quien la lee)."""
    if defecto is None:
        return lambda v: v
    if isinstance(defecto, bool):
        return lambda v: v if isinstance(v, bool) else _MAL
    if isinstance(defecto, (int, float)):
        return _entero(-2**63, 2**63) if isinstance(defecto, int) else (
            lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else _MAL)
    return lambda v: v if isinstance(v, type(defecto)) else _MAL


def normalizar_ajustes(d, defaults):
    """Al cargar settings.json (Store): cada clave de `defaults`, y un nivel por debajo en sus secciones, con el tipo
    y los valores que valen (VALIDOS o el tipo del de por defecto). Lo que no cuadra pasa al de por defecto; de una
    lista o un mapa solo se quita lo que no vale. Cada cambio deja una linea en el log, con la clave y sin el valor
    (puede ser una ruta o una clave). Cambia `d` y devuelve las claves corregidas («cloud.parallel»)."""
    out = []

    def una(keys, cont, defecto):
        k = keys[-1]
        if k not in cont:
            return
        v = cont[k]
        if isinstance(defecto, dict) and len(keys) == 1:
            if not isinstance(v, dict):
                cont[k] = copy.deepcopy(defecto)
                out.append(k)
                log(f"   ajuste {k} no válido: vuelve al valor por defecto")
                return
            for k2, d2 in defecto.items():
                una(keys + (k2,), v, d2)
            if keys not in VALIDOS:
                return
        f = VALIDOS.get(keys) or _por_tipo(defecto)
        try:
            nuevo = f(v)
        except Exception:  # noqa: BLE001  un validador nunca impide arrancar
            nuevo = _MAL
        nombre = ".".join(keys)
        if nuevo is _MAL:
            cont[k] = copy.deepcopy(defecto)
            out.append(nombre)
            log(f"   ajuste {nombre} no válido: vuelve al valor por defecto")
        elif isinstance(nuevo, (list, dict)) and len(nuevo) != len(v):
            cont[k] = nuevo
            out.append(nombre)
            log(f"   ajuste {nombre}: {len(v) - len(nuevo)} elementos no válidos, fuera")
        elif nuevo is not v and not isinstance(nuevo, (list, dict)):  # 30.0 escrito a mano: 30, sin mas
            cont[k] = nuevo
    for k, defecto in defaults.items():
        una((k,), d, defecto)
    return out


def procesado_de(st):
    """Donde se procesan las reuniones. Sin decidir (ajustes de una version anterior): "api" si hay API key, si no
    "local"."""
    p = st.get("procesado")
    if p in PROCESADOS:
        return p
    return "api" if ((st.get("cloud") or {}).get("api_key") or "").strip() else "local"


def proveedor_elegido(st):
    """Si ya hay un proveedor de la nube elegido: «API externa» elegida, o una clave, una direccion propia o una de
    Azure guardadas. En una instalacion nueva no lo hay, y la interfaz no da ninguno por elegido: a quien instala
    por primera vez no se le sugiere un servicio concreto. cloud.provider sigue siendo "groq" por defecto en los
    ajustes, para no cambiar nada a quien ya lo usa sin haberlo guardado nunca."""
    c = st.get("cloud") or {}
    # un proveedor que no es el de por defecto solo puede venir de una eleccion (aunque aun no tenga clave)
    return (procesado_de(st) == "api" or c.get("provider") not in ("groq", "", None)
            or any(str(c.get(k) or "").strip() for k in ("api_key", "base_url", "azure_endpoint"))
            or any(c.get(f) for f in engines.EXTRA_KEYS_FIELDS.values()))


def reparar_proveedor(d):
    """Al arrancar (fix-groq, 10/11/2026): unos ajustes con un proveedor de la nube que no existe (vacio o de una
    version anterior) pasan a Groq, que es lo que hacia el motor hasta fe29522 y otra vez desde fix-groq; asi Ajustes
    lo ensena elegido y «Guardar» no lo cambia. Con 0d5169b, en ese caso el desplegable no marcaba ninguno, el navegador
    marcaba el primero (Azure OpenAI) y un «Guardar» lo dejaba en Azure sin endpoint: si quedo asi (Azure sin endpoint
    ni deployment, y una clave de Groq, «gsk_…»), vuelve a Groq. Una instalacion nueva no cambia (ya lleva "groq" y
    nada elegido). Devuelve lo que se ha cambiado (una frase para el log) o None."""
    c = d.get("cloud")
    if not isinstance(c, dict):
        return None
    p = c.get("provider")
    if p not in engines.PROVIDERS:
        c["provider"] = "groq"
        return f"«{p if isinstance(p, str) else type(p).__name__}» no es ninguno de los que hay: pasa a Groq"
    clave = c.get("api_key") if isinstance(c.get("api_key"), str) else ""
    if (p == "azure" and not str(c.get("azure_endpoint") or "").strip()
            and not str(c.get("azure_deployment") or "").strip() and clave.strip().startswith("gsk_")):
        c["provider"] = "groq"
        return "Azure OpenAI sin endpoint ni deployment y con una clave de Groq: pasa a Groq"
    return None


def agente_de(st):
    """(clase del agente, {tipo, modelo}): el configurado, o Claude Code si no se reconoce."""
    a = st.get("agente") if isinstance(st.get("agente"), dict) else {}
    cls = engines.AGENTES.get(a.get("tipo")) or engines.AGENTES["claude"]
    modelo = a.get("modelo") if a.get("modelo") in dict(cls.MODELOS) else cls.MODELO
    return cls, {"tipo": cls.tipo, "modelo": modelo}


def moved_rel(p, old, new, folder=False):
    """La ruta interna `p` despues de que `old` pase a ser `new` (con folder, tambien lo que hay dentro); None si no le
    afecta."""
    if not isinstance(p, str) or not old:
        return None
    if p == old:
        return new
    if folder and p.startswith(old + "/"):
        return new + p[len(old):]
    return None


MEETING_LIKE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ _-]")


def _una_reunion_de_ese_dia(rel):
    """La carpeta `rel` («AAAA-MM-DD …») es la de una sola reunion de ese dia: dentro hay un solo audio y su nombre
    empieza por esa fecha. Una carpeta de proyecto que empieza por una fecha («2025-09-01 Proyecto X») tiene otras."""
    try:
        names = os.listdir(safe(rel, "dir"))
    except (ApiError, OSError):
        return False
    audios = [n for n in names if os.path.splitext(n)[1].lower() in AUDIO_EXT]
    return len(audios) == 1 and audios[0][:10] == rel.rpartition("/")[2][:10]


def migrate_main_folders(d):
    """Una vez (carpetas_v), al llegar la carpeta por reunion: la carpeta de cada proyecto que apuntaba la app era
    «la ultima usada», y la de quien ya se hacia una carpeta por reunion a mano es la de una reunion
    («AAAA-MM-DD …»): las nuevas acabarian dentro de ella. Esa pasa a ser su carpeta padre; lo demas se queda como
    asignacion. Igual con la ultima carpeta (rec_folder). Devuelve [(proyecto o «la última», vieja, nueva)]."""
    out = []

    def fix(rel, who):
        if isinstance(rel, str) and MEETING_LIKE_RE.match(rel.rpartition("/")[2]) and not is_link_root(rel) \
                and _una_reunion_de_ese_dia(rel):
            new = rel.rpartition("/")[0]
            out.append((who, rel, new))
            return new
        return rel
    d["project_folders"] = {k: fix(v, k) for k, v in (d.get("project_folders") or {}).items()}
    if d.get("rec_folder"):
        d["rec_folder"] = fix(d["rec_folder"], "la última")
    d["carpetas_v"] = 1
    return out


def migrate_app_name(d, raiz=None):
    """Lo que los ajustes guardan por el nombre del proyecto (la carpeta asignada, project_folders, y el idioma,
    project_langs) lleva, para las listas de la raiz, el nombre con el que lo buscaba la version anterior: el que dejo
    aqui la ultima vez (nombre_v; «Tenca» con la de 4553b9c, que lo llamaba como la app) o, sin marca, «Chuleta»
    (APP_FORMER_NAMES[0], el nombre de la app en todas las de antes). Ahora se llama como su carpeta en el disco
    (root_name, `raiz`): «Chuleta» en una instalacion encima de Chuleta, «Tenca» en una nueva. Pasa a ese nombre si aun
    no tiene uno, para que la carpeta y el idioma asignados sigan valiendo. Uno mas antiguo («Notas») no: ya no se usaba
    y puede ser un proyecto de verdad. Una vez por nombre: si despues se quita la asignacion de la raiz, no vuelve.
    Devuelve [(clave, nombre anterior)]."""
    from common import APP_FORMER_NAMES
    raiz = raiz or root_name()
    out, antes = [], d.get("nombre_v")
    if antes == raiz:
        return out
    antes = antes if isinstance(antes, str) and antes else APP_FORMER_NAMES[0]
    for key in ("project_folders", "project_langs"):
        m = d.get(key)
        if not isinstance(m, dict) or raiz in m:
            continue
        # el de ahora con otras mayusculas (una carpeta «tenca»: las busquedas son exactas) o, si no, el de antes
        k = next((k for k in m if fold(k) == fold(raiz)), None)
        if k is None and fold(antes) != fold(raiz):
            k = next((k for k in m if fold(k) == fold(antes)), None)
        if k is not None:
            m = dict(m)
            m[raiz] = m.pop(k)
            d[key] = m
            out.append((key, k))
    d["nombre_v"] = raiz
    return out


def settings_moved(d, old, new, folder=False, project=None):
    """Los ajustes que guardan rutas internas siguen a una carpeta o a un tema renombrados: carpeta y tema fijos del
    perfil, ultima carpeta y ultimo tema al grabar y la carpeta de cada proyecto. Si no, Ctrl+Alt+R se quedaba sin
    vocabulario y grabando en la raiz. project=(nombre viejo, nuevo): el proyecto tambien cambia de nombre."""
    perfil = dict(d.get("perfil") or {})
    for k in ("folder", "vocab"):
        m = moved_rel(perfil.get(k), old, new, folder)
        if m is not None:
            perfil[k] = m
    if d.get("perfil") is not None or perfil:
        d["perfil"] = perfil
    for k in ("rec_folder", "rec_vocab"):
        m = moved_rel(d.get(k), old, new, folder)
        if m is not None:
            d[k] = m
    pf = {}
    for name, rel in (d.get("project_folders") or {}).items():
        m = moved_rel(rel, old, new, folder)
        pf[name] = rel if m is None else m
    if project and project[0] in pf and project[1] not in pf:
        pf[project[1]] = pf.pop(project[0])
    d["project_folders"] = pf
    if d.get("workstream_folders"):  # id de la lista -> carpeta: los dos son rutas internas
        wf = {}
        for vid, rel in d["workstream_folders"].items():
            k, m = moved_rel(vid, old, new, folder), moved_rel(rel, old, new, folder)
            wf[vid if k is None else k] = rel if m is None else m
        d["workstream_folders"] = wf
    if project and isinstance(d.get("aprendido"), dict):  # las reglas aprendidas son de un proyecto, por su nombre
        for r in d["aprendido"].get("reglas") or []:
            if isinstance(r, dict) and isinstance(r.get("proyecto"), str) and fold(r["proyecto"]) == fold(project[0]):
                r["proyecto"] = project[1]
    _idiomas_moved(d, old, new, folder, project)


def _idiomas_moved(d, old, new, folder, project):
    """Plan de grabacion: el idioma de cada workstream (por el id de su lista) y de cada proyecto (por su nombre)
    siguen a un tema o a una carpeta renombrados."""
    if d.get("workstream_langs"):
        wl = {}
        for vid, lang in d["workstream_langs"].items():
            m = moved_rel(vid, old, new, folder)
            wl[vid if m is None else m] = lang
        d["workstream_langs"] = wl
    pl = d.get("project_langs")
    if pl and project and project[0] in pl and project[1] not in pl:
        pl = dict(pl)
        pl[project[1]] = pl.pop(project[0])
        d["project_langs"] = pl


def aprendido_de(st):
    """El ajuste "aprendido", validado (uno escrito a mano o de otra version no rompe nada: lo que no vale se
    ignora)."""
    a = st.get("aprendido") if isinstance(st.get("aprendido"), dict) else {}
    txt = lambda x: isinstance(x, str) and bool(x.strip())  # noqa: E731
    reglas = [dict(r, proyecto=r.get("proyecto") if isinstance(r.get("proyecto"), str) else "")
              for r in a.get("reglas") or [] if isinstance(r, dict) and txt(r.get("de")) and txt(r.get("a"))]
    no = [x for x in a.get("no") or [] if txt(x)]  # como se escribió (se comparan con aprender.normal)
    no_reglas = [list(x) for x in a.get("no_reglas") or [] if isinstance(x, list) and len(x) == 2 and all(map(txt, x))]
    return {"reglas": reglas, "no": no, "no_reglas": no_reglas}
