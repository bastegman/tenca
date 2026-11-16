# -*- coding: utf-8 -*-
"""App, la parte de Ajustes: guardar los ajustes, comprobar Claude Code y la nube, «Probar» del asistente, los
modelos de Whisper de este PC y «Compartir».

Salio tal cual de server.py al partirlo (15/11/2026): App hereda de esta clase y server.py la reexporta."""
import copy
import hashlib
import os
import shutil
import subprocess
import time

import engines
import explicar
import probar
from ajustes import PROCESADOS, PRUEBA_VOZ_DB, agente_de, procesado_de, proveedor_elegido
from common import APP_NAME, WINDOWS, ApiError, default_threads, log
from diagnostico import masked
from listas import (AJUSTES_CAMBIADOS, LIVE_PRESETS, SIN_AUDIO, _decode_list, check_perfil, list_vocab, perfil_de,
                    update_perfil)
from paquete import PKG_ROOT, build_package, desktop_dir
from recording import RecorderProcess
from rutas import APP_DIR, DATA_DIR, LINK


class Ajustes:
    def save_settings(self, b):
        actual = perfil_de(self.settings.get())
        perfil = (check_perfil(b["perfil"], actual["outputs"], actual)  # valida antes de guardar
                  if isinstance(b.get("perfil"), dict) else {})
        antes = procesado_de(self.settings.get())
        cur = self.settings.get()
        audio = [(b[k].strip() or defecto) if isinstance(b.get(k), str) else (cur.get(k) or defecto)
                 for k, defecto in (("rec_mic", "default"), ("rec_sys", "all"))]
        if audio == ["none", "none"]:
            raise ApiError(SIN_AUDIO)

        base, estado = b.get("rev"), {"conflicto": False}

        def upd(d):
            # Ajustes guarda cada control al cambiarlo y manda el config_rev que tenia (rev). Si no coincide, otra
            # ventana (o el asistente) ha guardado antes: lo que es una lista entera (las copias en otras carpetas, las
            # claves adicionales) no se pisa; lo demas se guarda y la respuesta lo dice (otro_cambio). Se mira aqui,
            # con el candado de Store.update: dos peticiones a la vez no pasan las dos como «sin conflicto»
            conflicto = estado["conflicto"] = base is not None and base != self.settings.rev
            pf, cl = b.get("perfil"), b.get("cloud")
            listas = (isinstance(pf, dict) and "outputs" in pf
                      or isinstance(cl, dict) and any(k in cl for k in ("extra_keys", "extra_keys_by")))
            if conflicto and listas:
                raise ApiError(AJUSTES_CAMBIADOS, 409)
            for k in ("preset", "language", "prompt"):
                if k in b:
                    d[k] = b[k] or ""
            s = b.get("summary") or {}
            if "auto" in s:
                d["summary_auto"] = bool(s["auto"])
            if "local" in s:  # consentimiento: resumir solo tambien lo transcrito en local (manda el texto a la nube)
                d["summary_auto_local"] = bool(s["local"])
            if s.get("aviso_visto") or s.get("local"):  # el aviso de Inicio, cerrado (o ya no hace falta)
                d["aviso_resumen_local"] = False
            if s.get("model"):
                d["summary_model"] = s["model"].strip()
            if s.get("lang") in engines.SUMMARY_LANGS:
                d["summary_lang"] = s["lang"]
            if "extra" in s:
                d["summary_extra"] = (s["extra"] or "").strip()[:500]
            if "meeting_detect" in b:
                d["meeting_detect"] = bool(b["meeting_detect"])
            for k in ("outlook_on", "rec_aviso"):  # reuniones: calendario de Outlook y aviso flotante
                if k in b:
                    d[k] = bool(b[k])
            if "live_auto" in b:
                d["live_auto"] = bool(b["live_auto"])
            if "carpeta_por_reunion" in b:  # una carpeta por reunion (encendida por defecto)
                d["carpeta_por_reunion"] = b["carpeta_por_reunion"] is not False
            if "rec_directo" in b:  # Ajustes › Al grabar: texto en directo (la tarjeta lo cambia por reunion)
                d["rec_directo"] = bool(b["rec_directo"])
            for k, defecto in (("rec_mic", "default"), ("rec_sys", "all")):
                if isinstance(b.get(k), str):
                    d[k] = b[k].strip()[:300] or defecto
            if "live_final_pass" in b:
                d["live_final_pass"] = bool(b["live_final_pass"])
            if b.get("live_preset") in LIVE_PRESETS:
                d["live_preset"] = b["live_preset"]
            if "threads" in b:
                d["threads"] = max(1, min(int(b["threads"] or default_threads()), os.cpu_count() or 16))
            if perfil:
                update_perfil(d, perfil)
            if "worker_keepalive_min" in b:  # 0 = como antes (5 min); tope 12 h
                d["worker_keepalive_min"] = max(0, min(int(b["worker_keepalive_min"] or 0), 720))
            if "worker_preload" in b:
                d["worker_preload"] = bool(b["worker_preload"])
            if b.get("procesado") in PROCESADOS:
                prev = procesado_de(d)
                d["procesado"] = b["procesado"]
                if d["procesado"] != "api":  # sin nube: los motores de la cola y del directo vuelven a ser locales
                    if prev == "api":  # los que se usaban con la API: si se vuelve a ella, vuelven (ida y vuelta)
                        d["motores_api"] = {k: d.get(k) for k in ("preset", "live_preset")}
                    for k, local in (("preset", "rapido"), ("live_preset", "medium")):
                        if (engines.PRESETS_BY_ID.get(d.get(k)) or {}).get("engine") == "cloud":
                            d[k] = local
                elif prev != "api" and (d["cloud"].get("api_key") or "").strip():
                    # y al reves (07/11/2026): a «API externa» con la clave ya puesta, los motores de cuando se uso la
                    # API (o la nube, como con la primera clave); si no, se elegia la API y se seguia transcribiendo con
                    # Whisper en el PC. Lo que se elija a la vez en esta misma peticion manda
                    antes_api = d.get("motores_api") if isinstance(d.get("motores_api"), dict) else {}
                    for k, validos in (("preset", engines.PRESETS_BY_ID), ("live_preset", LIVE_PRESETS)):
                        if k not in b:
                            d[k] = antes_api.get(k) if antes_api.get(k) in validos else "nube"
            ag = b.get("agente") if isinstance(b.get("agente"), dict) else None
            if ag:
                cur = d.get("agente") if isinstance(d.get("agente"), dict) else {}
                cls = (engines.AGENTES.get(ag.get("tipo")) or engines.AGENTES.get(cur.get("tipo"))
                       or engines.AGENTES["claude"])
                modelo = ag.get("modelo") if ag.get("modelo") in dict(cls.MODELOS) else cls.MODELO
                d["agente"] = {"tipo": cls.tipo, "modelo": modelo}
            c = b.get("cloud") or {}
            for k in ("provider", "model", "base_url", "azure_endpoint", "azure_deployment", "azure_api_version"):
                if k == "provider" and c.get(k) not in engines.PROVIDERS:
                    continue  # «Elige el proveedor…» sin elegir: no se guarda vacio (el motor tomaria Groq)
                if k in c:
                    d["cloud"][k] = (c[k] or "").strip()
            # 20 min de FLAC 16 kHz ~ 22 MB: por debajo del limite de 25 MB por archivo de las APIs
            for k, lo, hi in (("chunk_minutes", 2, 20), ("parallel", 1, 8)):
                if k in c:
                    d["cloud"][k] = max(lo, min(int(c[k] or lo), hi))
            if c.get("api_key") and "…" not in c["api_key"] and "•" not in c["api_key"]:
                if not d["cloud"].get("api_key") and procesado_de(d) == "api":  # primera key: la nube, predeterminada
                    d["preset"] = d["live_preset"] = "nube"
                d["cloud"]["api_key"] = c["api_key"].strip()
            if c.get("clear_key"):
                d["cloud"]["api_key"] = ""
            for k in ("live_model", "live_model_custom"):  # el del directo en Groq y el de una pasarela propia
                if k in c:
                    d["cloud"][k] = (c[k] or "").strip()[:80]
            # claves adicionales: cada lista es de su proveedor (las de Groq, en extra_keys). La interfaz manda las de
            # cada proveedor en extra_keys_by; extra_keys a secas son las del proveedor que se guarda. Las ya guardadas
            # vuelven enmascaradas y se conservan, pero solo dentro de su proveedor: nunca pasan a otro
            lists = c.get("extra_keys_by") if isinstance(c.get("extra_keys_by"), dict) else {}
            if not lists and isinstance(c.get("extra_keys"), list):
                lists = {d["cloud"].get("provider") or "groq": c["extra_keys"]}
            for prov, incoming in lists.items():
                if prov not in engines.PROVIDERS:
                    continue  # sin proveedor elegido no hay de quien: no se guardan como claves de Groq
                field = engines.extra_keys_field(prov)
                if not field or not isinstance(incoming, list):
                    continue
                old = {masked(k): k for k in d["cloud"].get(field) or []}
                new = [old.get(k.strip(), k.strip()) for k in incoming if isinstance(k, str) and k.strip()]
                d["cloud"][field] = [k for k in new if "…" not in k and "•" not in k][:10]
        self.settings.update(upd, rev=True)
        p = (getattr(self, "pruebas", None) or {}).get(b.get("prueba")) if b.get("prueba") else None
        if p is not None and p.terminada and p.firma == self._firma(self.settings.get()):
            res = p.resumen()  # la del asistente: es de lo que se acaba de guardar
            self.settings.update(lambda d: d.update(ultima_prueba=res))
        ahora = procesado_de(self.settings.get())
        if ahora != antes:
            log(f"   dónde se procesan las reuniones: {antes} → {ahora}")
        if antes == "api" and ahora != "api":
            self.jobs.nube_apagada()  # lo que estuviera en la nube no sigue
        # recien elegido un modo que transcribe en este PC: el modelo del directo, ya, con su progreso en Ajustes (solo
        # al cambiar de modo: «Guardar» con el mismo no vuelve a empezar una descarga que se cancelo)
        descarga = self._descargar_modelo_del_directo() if ahora != antes and ahora in ("local", "agente") else None
        return dict(self.config({}), descarga=descarga, otro_cambio=estado["conflicto"])

    def agente_comprobar(self, b):
        """Ajustes → «Comprobar»: el agente esta instalado, responde a --version y contesta una peticion minima con la
        cuenta del usuario. Guarda la version y la hora para ensenarlas en Ajustes."""
        st = self.settings.get()
        cls, a = agente_de(dict(st, agente=b if isinstance(b, dict) and b.get("tipo") else st.get("agente")))
        modelo = b.get("modelo") if isinstance(b, dict) and b.get("modelo") in dict(cls.MODELOS) else a["modelo"]
        ruta = cls.find()
        if not ruta:
            raise ApiError(f"No encuentro {cls.label} en este PC. Haz los pasos 1 y 2 y vuelve a pulsar «Comprobar».")
        # lo que tarda cada paso, al log (07/11/2026: en un PC lento «Comprobar» tardó minutos sin decir en que)
        origen = cls.origen(ruta)  # «Claude Code de VS Code» si es la copia de la extension (07/11/2026)
        log(f"   agente: comprobando {origen} ({modelo})…" + (f" · {ruta}" if origen != cls.label else ""))
        ag, t0 = cls(settings_dir=DATA_DIR), time.time()
        try:
            version, secs = ag.check(ruta, model=modelo)
        except engines.EngineError as e:
            log(f"   agente: {cls.label}: {e} ({self._agente_tiempos(ag, time.time() - t0)})")
            raise ApiError(explicar.explicar(e)["frase"])
        t = getattr(ag, "tiempos", None)
        if isinstance(t, dict) and t:
            log(f"   agente: {self._agente_tiempos(ag, secs)}")
        self.settings.update(lambda d: d.update(agente_version=version, agente_comprobado=time.time()))
        aviso = getattr(ag, "aviso", None)
        if aviso:
            log(f"   agente: {aviso}")
        log(f"   agente: {cls.label} {version} comprobado ({secs:.1f} s)")
        return {"ok": True, "version": version, "seconds": round(secs, 1), "ruta": ruta, "origen": origen,
                "modelo_usado": getattr(ag, "modelo_usado", None), "aviso": aviso}

    @staticmethod
    def _agente_tiempos(ag, total):
        """«--version 2.1 s · petición mínima 41.0 s · total 43.1 s» (lo que se sepa de cada paso)."""
        t = getattr(ag, "tiempos", None) or {}
        partes = [f"--version {t['version_s']} s" if "version_s" in t else None,
                  f"petición mínima {t['peticion_s']} s" if "peticion_s" in t else None, f"total {total:.1f} s"]
        return " · ".join(p for p in partes if p)

    def test_cloud(self, b):
        cloud = self.settings.get()["cloud"]
        try:
            secs = self.jobs.cloud.test(cloud)
        except engines.EngineError as e:
            prov = (engines.PROVIDERS.get(cloud.get("provider") or "groq") or {}).get("label")
            raise ApiError(explicar.explicar(e, proveedor=prov)["frase"])
        return {"ok": True, "seconds": round(secs, 2)}

    # --- «Probar» del asistente «Configurar» de la app (probar.py): prueba real con lo que se elige, sin guardar
    def _candidata(self, b):
        """(ajustes con lo elegido en el asistente encima, sin guardarlos; True si hay algo elegido). La clave nueva
        solo si es una de verdad (no la máscara que enseña Ajustes)."""
        st = copy.deepcopy(self.settings.get())
        c = (b or {}).get("candidata")
        if not isinstance(c, dict):
            return st, False
        if c.get("procesado") in PROCESADOS:
            st["procesado"] = c["procesado"]
        ag = c.get("agente")
        if isinstance(ag, dict):
            cls = engines.AGENTES.get(ag.get("tipo")) or agente_de(st)[0]
            st["agente"] = {"tipo": cls.tipo,
                            "modelo": ag.get("modelo") if ag.get("modelo") in dict(cls.MODELOS) else cls.MODELO}
        cl = c.get("cloud") if isinstance(c.get("cloud"), dict) else {}
        elegido = proveedor_elegido(self.settings.get()) or cl.get("provider") in engines.PROVIDERS
        st["cloud"] = dict(st.get("cloud") or {})
        for k in ("provider", "model", "base_url", "azure_endpoint", "azure_deployment", "azure_api_version"):
            if k == "provider" and cl.get(k) not in engines.PROVIDERS:
                continue  # como al guardar: un proveedor vacio o desconocido no se prueba como si fuera Groq
            if k in cl:
                st["cloud"][k] = (cl[k] or "").strip()
        key = (cl.get("api_key") or "").strip()
        if key and "…" not in key and "•" not in key:
            st["cloud"]["api_key"] = key
        if st.get("procesado") == "api" and not elegido:
            # el motor lo rechaza con «Elige el proveedor…» (por la marca: sin ella, unos ajustes guardados con un
            # proveedor vacio van a Groq, como siempre): nada sale hacia Groq
            st["cloud"]["provider"] = ""
            st["cloud"][engines.SIN_PROVEEDOR] = True
        return st, True

    @staticmethod
    def _prueba_motores(st):
        """(preset del directo, [modelos de Whisper que hacen falta]) de esa configuración, sin «API externa» siempre
        en el PC: el del directo y el de la cola (los dos se descargan juntos y se prueban de una vez)."""
        out = []
        for key, local in (("live_preset", "medium"), ("preset", "rapido")):
            p = engines.PRESETS_BY_ID.get(st.get(key) or local)
            if not p or p["engine"] != "local":
                p = engines.PRESETS_BY_ID[local]
            out.append(p)
        return out[0], list(dict.fromkeys(p["model"] for p in out))

    def _prueba_hacer(self, st):
        """Las funciones de verdad para probar.Prueba (micro, Whisper, nube, Claude) con esos ajustes."""
        modo = procesado_de(st)
        directo, modelos_pc = self._prueba_motores(st)
        tmp = os.path.join(DATA_DIR, "tmp")
        os.makedirs(tmp, exist_ok=True)

        def modelos(report, cancelled):
            if engines.MODELS is None:
                return
            names = [m for m in modelos_pc if m in engines.WHISPER_MODELS]
            for m in names:
                if engines.MODELS.path(m) is None:
                    engines.MODELS.start(m)
            while True:
                sts = [engines.MODELS.state(m) for m in names]
                if all(x["ready"] for x in sts):
                    return
                errs = [x["error"] for x in sts if x["error"] and not x["downloading"] and not x["ready"]
                        and "cancelada" not in x["error"].lower()]
                if errs:
                    raise engines.EngineError(errs[0])
                for m, x in zip(names, sts):
                    if not x["ready"] and not x["downloading"]:
                        engines.MODELS.start(m)  # sin empezar o cancelada en Ajustes: la prueba la necesita
                total = sum(x["total_mb"] or x["size_mb"] or 0 for x in sts)
                done = sum((x["total_mb"] or x["size_mb"] or 0) if x["ready"] else (x["done_mb"] or 0) for x in sts)
                eta = max((x["eta_s"] or 0) for x in sts)
                gb = lambda mb: f"{mb / 1000:.1f}".replace(".", ",")  # noqa: E731
                report(f"Descargando · {int(100 * done / total) if total else 0} % · {gb(done)} de {gb(total)} GB"
                       + (f" · quedan unos {engines.fmt_left(eta)}" if eta else ""))
                if cancelled():
                    raise probar.Cancelada()
                time.sleep(0.5)

        def grabar(segundos, report, cancelled):
            if self.rec and self.rec.alive():
                raise engines.EngineError("Estás grabando: prueba cuando termines la grabación.")
            with self.rec_lock:
                if self.monitor:
                    self.monitor.stop(timeout=2)
                    self.monitor = None
            path = os.path.join(tmp, "prueba-micro.wav")
            try:
                os.remove(path)
            except OSError:
                pass
            report("Prepárate: en un momento empieza a grabar…")
            try:
                # con «Sin micrófono» guardado, la prueba usa el predeterminado de Windows
                mic = st.get("rec_mic") if st.get("rec_mic") not in (None, "", "none") else "default"
                r = RecorderProcess(path, mic, "none", APP_DIR, DATA_DIR)
            except Exception as e:  # sin Python para lanzarlo o sin permisos: la cadena con silencio
                return None, False, e.__class__.__name__
            fin = time.time() + 10
            while time.time() < fin and r.alive() and r.status.get("t") is None:
                if cancelled():
                    r.stop(timeout=2)
                    raise probar.Cancelada()
                time.sleep(0.1)
            if r.status.get("t") is None:
                r.stop(timeout=2)
                mic = r.status.get("mic") if isinstance(r.status.get("mic"), dict) else {}
                log(f"   probar: el micro no ha arrancado ({mic.get('error') or 'sin estado'})")
                return None, False, mic.get("error") or "el micro no ha arrancado"
            t0, fin = r.status.get("t") or 0, time.time() + segundos + 8  # un grabador colgado no para la prueba
            while r.alive() and (r.status.get("t") or 0) - t0 < segundos and time.time() < fin:
                if cancelled():
                    r.stop(timeout=2)
                    raise probar.Cancelada()
                quedan = max(1, int(segundos - ((r.status.get("t") or 0) - t0) + 0.99))
                report(f"Habla ahora: di «{probar.FRASE}» · {quedan} s")
                time.sleep(0.1)
            r.stop(timeout=5)
            mics = [m for _, m, _ in r.levels if m is not None]
            voz = sum(1 for m in mics if m > PRUEBA_VOZ_DB) >= 3  # ≥ 0,75 s con voz (niveles cada 0,25 s)
            log(f"   probar: micro {len(mics)} lecturas, máx {max(mics) if mics else '-'} dB, "
                f"voz: {'sí' if voz else 'no'}")
            return (path if os.path.isfile(path) else None), voz, None

        def cuenta(report, cancelled):
            cls, a = agente_de(st)
            ruta = cls.find()
            if not ruta:
                raise engines.EngineError(f"No encuentro {cls.label} en este PC. Sigue el paso a paso y vuelve a "
                                          "probar.")
            report(f"Comprobando {cls.label} con tu cuenta…")
            cls(settings_dir=DATA_DIR).check(ruta, model=a["modelo"], cancelled=cancelled)

        def silencio():
            import wave
            path = os.path.join(tmp, "prueba-silencio.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\0\0" * 16000)
            return path

        def transcribir(wav, report, cancelled):
            def rep(frac=None, stage=None, **k):
                report(stage)
            if modo == "api":
                import numpy as np
                y = engines.decode_16k(wav).astype(np.float32) / 32768.0
                f = os.path.join(tmp, "prueba.flac")
                engines.write_flac(f, y)
                r = self.jobs.cloud._post(st["cloud"], f, "es", None, rep, cancelled, attempts=2, max_wait=10.0)
                return (r.get("text") or "").strip()
            if getattr(self, "local_prueba", None) is None:
                self.local_prueba = engines.LocalEngine(DATA_DIR, "prueba", idle_s=60)
            segs, _, _ = self.local_prueba.transcribe(wav, directo, "es", None, st.get("threads") or default_threads(),
                                                      rep, cancelled)
            return " ".join(t for _, _, t in segs).strip()

        def resumir(texto, report, cancelled):
            def rep(stage=None, **k):
                report(stage)
            sistema = "Resume en una sola frase corta, en español, lo que dice este texto."
            if modo == "agente":
                cls, a = agente_de(st)
                out, _ = cls(settings_dir=DATA_DIR).chat(sistema, texto, 200, rep, cancelled, model=a["modelo"],
                                                         timeout=engines.AGENTE_CHECK_S)
                return out
            out, _ = engines.Summarizer(keys=self.jobs.cloud.keys)._chat(
                dict(st["cloud"], summary_model=st.get("summary_model")), sistema, texto, 200, rep, cancelled,
                timeout=60, attempts=2)
            return out

        prov = (engines.PROVIDERS.get(st["cloud"].get("provider") or "groq") or {}).get("label") or "la API"
        nombres = {"transcribe": f"con {prov}" if modo == "api" else "en este PC",
                   "resume": "Claude (tu cuenta)" if modo == "agente" else prov,
                   "proveedor": prov if modo == "api" else None}
        return {"cuenta": cuenta, "modelos": modelos, "grabar": grabar, "silencio": silencio,
                "transcribir": transcribir, "resumir": resumir}, nombres

    @staticmethod
    def _firma(st):
        """Lo que hace distinta una configuración para «Probar» (la clave, como huella): una prueba solo vale para la
        configuración que se probó (revisión del 07/11/2026: un «Listo» viejo sobrevivía a cambiar la clave)."""
        c = st.get("cloud") or {}
        key = (c.get("api_key") or "").strip()
        return (procesado_de(st), (st.get("agente") or {}).get("modelo"), c.get("provider"), c.get("model"),
                c.get("base_url"), c.get("azure_endpoint"), c.get("azure_deployment"),
                hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] if key else "")

    def probar_empezar(self, b):
        """POST /api/probar {candidata?}: empieza una prueba (una a la vez) y devuelve su id."""
        if self.rec and self.rec.alive():
            raise ApiError("Estás grabando: prueba cuando termines la grabación.")
        with self.rec_lock:
            cur = getattr(self, "prueba", None)
            if cur is not None and cur.is_alive():  # otra configuración (o la misma otra vez): la de antes se cancela
                cur.cancelar = True
            st, candidata = self._candidata(b)
            hacer, nombres = self._prueba_hacer(st)
            p = probar.Prueba(procesado_de(st), hacer, nombres, candidata=candidata, al_terminar=self._prueba_fin,
                              espera=cur if cur is not None and cur.is_alive() else None)
            p.firma = self._firma(st)
            if not hasattr(self, "pruebas"):
                self.pruebas = {}
            self.pruebas = {k: v for k, v in self.pruebas.items() if v.is_alive() or time.time() - v.empezada < 3600}
            self.pruebas[p.id] = self.prueba = p
            p.start()
        log(f"   probar: {procesado_de(st)}{' (sin guardar)' if candidata else ''}")
        return {"id": p.id}

    def _prueba_fin(self, p):
        r = p.public()
        log(f"   probar: {'bien' if r['listo'] else 'falla'} · " + " · ".join(
            f"{x['id']} {x['estado']} ({x['segundos']} s)" for x in r["pasos"])
            + (f" · {r['fallo']['tecnico'] or r['fallo']['frase']}" if r["fallo"] else ""))
        if p.nota_micro:
            log(f"   probar: micro: {p.nota_micro}")
        if not p.candidata:  # la de lo guardado (Ajustes › Probar): queda como última prueba
            res = p.resumen()
            self.settings.update(lambda d: d.update(ultima_prueba=res))

    def probar_estado(self, q):
        p = (getattr(self, "pruebas", None) or {}).get(q.get("id"))
        if p is None:
            raise ApiError("No encuentro esa prueba: vuelve a pulsar «Probar».")
        return p.public()

    def probar_cancelar(self, b):
        p = (getattr(self, "pruebas", None) or {}).get((b or {}).get("id"))
        if p is not None:
            p.cancelar = True
        return {"ok": True}

    def agente_estado(self, q):
        """Para el paso 2 del asistente: si Claude Code ya está en este PC (se mira cada pocos segundos)."""
        cls, _ = agente_de(self.settings.get())
        ruta = cls.find()
        return {"encontrado": bool(ruta), "label": cls.label, "origen": cls.origen(ruta) if ruta else None}

    def _powershell_env(self):
        """El entorno de la ventana de «Abrir PowerShell»: el de una PowerShell nueva (engines.windows_env) y, delante
        en su PATH, la carpeta del Claude Code que encuentra la app (08/11/2026). El instalador nativo no lo pone en el
        PATH («…\\.local\\bin is not in your PATH», medido en Windows) y la copia de VS Code tampoco: así, en esa
        ventana, `claude` (y su inicio de sesión en el navegador) funciona siempre. La app no inicia sesión por nadie:
        el usuario escribe claude y entra en la página de Claude."""
        env = engines.windows_env()
        cls, _ = agente_de(self.settings.get())
        ruta = cls.find()
        # y la carpeta del instalador nativo aunque aún no exista: el paso 1 instala en esa misma ventana y el 2
        # escribe claude en ella
        dirs = [os.path.dirname(os.path.abspath(ruta))] if ruta else []
        nativo = os.path.join(os.environ.get("USERPROFILE") or os.path.expanduser("~"), ".local", "bin")
        if os.path.normcase(nativo) not in map(os.path.normcase, dirs):
            dirs.append(nativo)
        env["PATH"] = os.pathsep.join(dirs + [env.get("PATH", "")])
        return env

    def abrir_powershell(self, b):
        """«Abrir PowerShell» del asistente: una ventana vacía, sin ejecutar nada (con Claude Code en su PATH)."""
        if os.name != "nt":
            raise ApiError("Este botón solo funciona en Windows: abre una terminal y sigue los pasos.")
        subprocess.Popen(["powershell.exe", "-NoExit", "-NoLogo"], cwd=os.path.expanduser("~"),
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0), env=self._powershell_env())
        return {"ok": True}

    # --- modelos de Whisper en este PC (engines.WhisperModels): estado, descarga con progreso y cancelar
    @staticmethod
    def _modelos_en_uso(st):
        """modelo -> ["directo", "cola"]: los que usan el motor del directo y el de la cola si son locales."""
        uso = {}
        for key, what, default in (("live_preset", "directo", "medium"), ("preset", "cola", "rapido")):
            p = engines.PRESETS_BY_ID.get(st.get(key) or default)
            if p and p["engine"] == "local":
                uso.setdefault(p["model"], []).append(what)
        return uso

    def modelos_state(self, q=None):
        st = self.settings.get()
        uso, out = self._modelos_en_uso(st), []
        for p in engines.PRESETS:
            if p["engine"] != "local":
                continue
            m = p["model"]
            if engines.MODELS is not None:
                s = engines.MODELS.state(m)
            else:  # sin el gestor (no deberia pasar con la app arrancada): solo si esta o no
                s = {"model": m, "ready": engines.model_cached(m), "size_mb": None, "downloading": False,
                     "done_mb": 0, "total_mb": None, "frac": None, "speed_mbs": None, "eta_s": None, "error": None}
            out.append(dict(s, preset=p["id"], label=p["label"], live_label=p["live_label"], uso=uso.get(m, [])))
        return {"modelos": out, "procesado": procesado_de(st),
                "carpeta": engines.MODELS.root if engines.MODELS is not None else None}

    def modelos_descargar(self, b):
        m = str((b or {}).get("model") or "")
        if engines.MODELS is None or m not in engines.WHISPER_MODELS:
            raise ApiError(f"No sé descargar el modelo «{m}».")
        if engines.MODELS.path(m) is None:
            engines.MODELS.start(m)
            log(f"   modelo {m}: descarga pedida desde Ajustes")
        return self.modelos_state()

    def modelos_cancelar(self, b):
        m = str((b or {}).get("model") or "")
        if engines.MODELS is not None and engines.MODELS.cancel(m):
            log(f"   modelo {m}: descarga cancelada desde Ajustes")
        return self.modelos_state()

    def _descargar_modelo_del_directo(self):
        """Al pasar a «Solo en este PC» o «Con tu cuenta de Claude»: si falta el modelo del directo, se empieza a
        descargar ya, con su progreso en Ajustes (antes se descargaba al empezar la primera reunion, sin progreso).
        Devuelve el modelo que se ha puesto a descargar, o None."""
        st = self.settings.get()
        p = engines.PRESETS_BY_ID.get(st.get("live_preset") or "medium")
        if engines.MODELS is None or not p or p["engine"] != "local" or p["model"] not in engines.WHISPER_MODELS:
            return None
        if engines.MODELS.path(p["model"]) is not None or engines.MODELS.cancelled_by_user(p["model"]):
            return None  # ya esta, o el usuario cancelo su descarga en Ajustes (la retoma con «Descargar»)
        engines.MODELS.start(p["model"])
        log(f"   modelo {p['model']}: descarga empezada al elegir dónde se procesan las reuniones")
        return p["model"]

    def export_package(self, b):
        import tempfile
        dest = os.path.join(desktop_dir(), f"{APP_NAME} - instalador.zip")
        vocab, seen, tmp = [], set(), None
        # las de carpetas vinculadas o conectadas (":id/…", ":v<id>/…"), como <raiz>/<proyecto>/Vocabulario/<tema>.txt
        for v in (list_vocab() if b.get("vocab") else []):
            arc = PKG_ROOT + "/" + (v["id"] if not v["id"].startswith(LINK)
                                    else f"{v['project']}/Vocabulario/{v['name']}.txt")
            if arc.lower() in seen:  # dos listas con el mismo proyecto y tema: una sola en el zip (la primera)
                continue
            src = v["_abs"]
            try:
                with open(src, "rb") as f:
                    raw = f.read()
                raw.decode("utf-8")
            except UnicodeDecodeError:  # cp1252 o UTF-16 (de una carpeta conectada): en el zip, en UTF-8
                tmp = tmp or tempfile.mkdtemp(prefix="tenca-listas-")
                src = os.path.join(tmp, f"{len(vocab)}.txt")
                with open(src, "w", encoding="utf-8", newline="") as f:  # sin newline="", Windows dobla los \r\n
                    f.write(_decode_list(raw))
            except OSError:
                continue
            seen.add(arc.lower())
            vocab.append((src, arc))
        try:
            n = build_package(dest, vocab)
        except OSError as e:
            raise ApiError(f"No se pudo crear el paquete: {e}")
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        if WINDOWS:
            subprocess.Popen(f'explorer /select,"{dest}"')
        return {"ok": True, "path": dest, "files": n, "size": os.path.getsize(dest), "vocab": len(vocab)}
