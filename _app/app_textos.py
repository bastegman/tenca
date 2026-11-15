# -*- coding: utf-8 -*-
"""App, la parte de editar transcripciones (guardar, restaurar, lo editado en el directo) y de aprender de tus
correcciones.

Salio tal cual de server.py al partirlo (15/11/2026): App hereda de esta clase y server.py la reexporta."""
import os
import shutil
import time

import aprender
import engines
from ajustes import aprendido_de
from aprendido import (anadir_a_lista, deshacer_aprendido, guardar_registro, lista_de_reunion, mismo_proyecto,
                       original_de, proyecto_de_reunion, registro_aprendido)
from biblioteca import TX_SAVE_LOCK, apply_edits, seg_key, temas, write_outputs
from carpetas import backup_dir, copia_original
from common import ApiError, fold, log, log_exc, save_text, write_bytes
from listas import find_vocab, list_vocab, md_props, vocab_fit, vocab_info
from rutas import VLINK, VOCAB_LINKS, alias_of, rel_of, safe


class Textos:
    # --- edicion de transcripciones
    _tx_save_lock = TX_SAVE_LOCK  # biblioteca: el mismo que usa aprendido.aplicar_aprendido

    def save_transcript(self, b):
        """Guarda lo corregido a mano. La primera vez copia lo que escribio Whisper (para poder volver). De uno en uno:
        al cerrar la pagina el mismo texto llega dos veces a la vez (keepalive y el fetch normal), y el segundo podia
        escribir lo corregido antes de que el primero copiara el original."""
        with self._tx_save_lock:  # dentro: si la reunion se acaba de mover, su ruta nueva (_move_now lo coge)
            return self._save_transcript(dict(b, path=alias_of(b.get("path"))))

    def _save_transcript(self, b):
        abs_path = safe(b.get("path"), "file")
        rel, base = rel_of(abs_path), os.path.splitext(abs_path)[0]
        segs = []
        for s in b.get("segments") or []:
            text = " ".join(str(s.get("text") or "").split())
            if text:
                segs.append((s.get("start"), s.get("end"), text))
        job = self.jobs.active_for(rel)
        if job and job.live:
            gen = b.get("gen")
            return self._save_live_edits(job, segs, base, gen if isinstance(gen, int) and not isinstance(gen, bool)
                                         else None)
        if job:
            raise ApiError("Se está haciendo el repaso final (1–2 min por hora de reunión); "
                           "podrás editar en cuanto termine."
                           if job.final_pass else "Se está transcribiendo; podrás editar cuando termine.")
        if not segs:
            raise ApiError("La transcripción no puede quedar vacía.")
        copia_original(rel, base)
        if os.path.isfile(base + ".srt") and all(a is not None and e is not None for a, e, _ in segs):
            write_outputs(abs_path, [(float(a), float(e), t) for a, e, t in segs])
        else:  # transcripcion solo en .txt (sin tiempos)
            save_text(base + ".txt", "\n".join(t for _, _, t in segs) + "\n", "utf-8-sig")
        self.library.at = 0
        self.jobs.schedule_outputs(rel)  # destinos de salida: el documento con la transcripcion corregida
        mt = max((os.path.getmtime(base + e) for e in (".txt", ".srt") if os.path.isfile(base + e)), default=0)
        return {"ok": True, "tx_mtime": mt, "count": len(segs)}

    def _save_live_edits(self, job, segs, base, gen=None):
        """Corregir mientras se transcribe en directo: se guardan las diferencias con lo que dio Whisper
        y se aplican al escribir cada trozo nuevo, asi el directo no pisa lo corregido. gen: la generación del texto
        que tenía el editor (/api/transcript → text_gen). Si desde entonces el directo ha rehecho texto ya escrito
        (cambio de idioma), lo que va desde ahí (rehecho_desde) no se toca: ni se corrige con el texto de antes ni se
        da por borrado (antes salía como «frase borrada» y se perdía lo rehecho)."""
        if not job.out:
            raise ApiError("El directo aún está empezando; prueba en unos segundos.")
        sent = {seg_key(a): t for a, _, t in segs if a is not None}
        last = max(sent) if sent else -1.0  # lo que llego despues aun no lo veias: no se toca
        with job.edit_lock:
            corte = None
            if gen is not None and gen != getattr(job, "text_gen", 0):
                # lo rehecho despues de la version del editor; ninguno (era la de otro directo de la reunion, que
                # empezo con el texto del archivo): como siempre
                corte = min((t for g, t in getattr(job, "rewrites", []) if g > gen), default=None)
            for a, _, t in job.raw:
                k = seg_key(a)
                if corte is not None and a >= corte - 1e-6:
                    continue  # rehecho despues de que el editor cargara su texto
                if k in sent:
                    if sent[k] != " ".join(t.split()):
                        job.edits[k] = sent[k]
                    else:
                        job.edits.pop(k, None)
                elif k <= last:
                    job.edits[k] = ""  # la dejaste vacia: frase borrada
            job.out.write(apply_edits(job.raw, job.edits))
            gen_ahora = getattr(job, "text_gen", 0)
        self.library.at = 0
        mt = max((os.path.getmtime(base + e) for e in (".txt", ".srt") if os.path.isfile(base + e)), default=0)
        return {"ok": True, "tx_mtime": mt, "count": len(segs), "live": True, "text_gen": gen_ahora,
                "rehecho_desde": corte}

    def restore_transcript(self, b):
        """«Restaurar el original». Como save_transcript, con el candado que coge _move_now: si la reunion se esta
        moviendo (B1c), espera y restaura en la ruta nueva; si no, dejaba el original en la carpeta vieja, borraba la
        copia y respondia «ok» (revision de la integracion del 08/11/2026)."""
        with self._tx_save_lock:
            return self._restore_transcript(dict(b, path=alias_of(b.get("path"))))

    def _restore_transcript(self, b):
        abs_path = safe(b.get("path"), "file")
        rel, base = rel_of(abs_path), os.path.splitext(abs_path)[0]
        bdir = backup_dir(rel)
        if not os.path.isdir(bdir):
            raise ApiError("No hay copia del original de esta transcripción.")
        if self.jobs.active_for(rel):
            raise ApiError("Se está transcribiendo; espera a que termine.")
        for ext in (".srt", ".txt"):
            src = os.path.join(bdir, "original" + ext)
            if os.path.isfile(src):
                with open(src, "rb") as f:
                    write_bytes(base + ext, f.read())
        shutil.rmtree(bdir, ignore_errors=True)
        self.library.at = 0
        self.jobs.schedule_outputs(rel)  # destinos de salida: el documento vuelve a la transcripcion original
        log(f"↺ {rel}: transcripción original restaurada")
        return {"ok": True}

    # --- aprender de tus correcciones (aprender.py)
    def _propuestas(self, abs_path):
        """Lo que se puede aprender de lo que corregiste en esta reunion (aprender.propuestas), con las listas donde ya
        esta cada termino, la lista de la reunion (la que recibe Whisper; "" = el vocabulario general) y todas las
        listas para elegir otra. Nada mientras se transcribe ni sin copia del original."""
        rel = rel_of(abs_path)
        vacio = {"propuestas": [], "lista": "", "proyecto": "", "listas": []}
        if self.jobs.active_for(rel):
            return vacio
        orig = original_de(rel)
        data = self.transcripts.load(os.path.splitext(abs_path)[0]) if orig else None
        if not orig or not data:
            return vacio
        apr = aprendido_de(self.settings.get())
        reg = registro_aprendido(rel)
        hechos = {(aprender.normal(c["de"]), aprender.normal(c["a"])) for c in reg["cambios"]}
        no = {aprender.normal(x) for x in apr["no"]} | set(reg["respondidas"])
        ps = aprender.propuestas(orig, data["segments"], hechos, no, [tuple(x) for x in apr["no_reglas"]])
        if not ps:
            return vacio
        props = md_props(abs_path)
        v = lista_de_reunion(abs_path, props)
        listas = self._vocab_terms()
        general = {aprender.normal(t) for t in engines.split_terms(self.settings.get().get("prompt") or "")}
        for p in ps:
            k = aprender.normal(p["termino"])
            p["en"] = [l["id"] for l in listas if k in {aprender.normal(t) for t in l["terms"]}]
            p["en_general"] = k in general
            for x in p["antes"]:  # la regla que ya tienes, en que proyectos (la interfaz no la ofrece otra vez alli)
                kd = aprender.normal(x["texto"])
                x["reglas_en"] = [r["proyecto"] for r in apr["reglas"]
                                  if aprender.normal(r["de"]) == kd and aprender.normal(r["a"]) == k]
        # el proyecto de la reunion (aunque su lista no este): si la propuesta va al vocabulario general, la casilla
        # de la regla viene sin marcar, porque valdria para las reuniones sin proyecto y no para las de este
        out = {"propuestas": ps, "lista": v["id"] if v else "", "proyecto": v["project"] if v else "",
               "proyecto_reunion": proyecto_de_reunion(abs_path, props),
               "listas": [{"id": l["id"], "project": l["project"], "name": l["name"],
                           "conectada": bool(l.get("conectada"))} for l in listas]}
        guardada = temas().get(rel) or ""
        lid = guardada[len(VLINK):].split("/")[0] if guardada.startswith(VLINK) else ""
        sin_carpeta = [k for k, l in list(VOCAB_LINKS.items()) if not os.path.isdir(l["path"])
                       and (k == lid or fold(l["name"]) == fold(props.get("proyecto") or ""))]
        if not v and props.get("workstream") and sin_carpeta:
            # su lista esta en una carpeta conectada que ahora no se encuentra (VPN, red, disco): no se elige otra por
            # el; si nunca tuvo lista o se borro, como siempre: el vocabulario general, que es el que uso Whisper
            out["lista"] = None
            out["falta"] = {"proyecto": props.get("proyecto") or "", "workstream": props["workstream"]}
        return out

    def aprender_get(self, q):
        """GET /api/aprender?path=: las propuestas de esa reunion (ver _propuestas)."""
        return self._propuestas(safe(q.get("path"), "file"))

    def aprender_anadir(self, b):
        """POST /api/aprender/anadir {path, termino, lista ("" = vocabulario general), reglas: [lo que escribio
        Whisper]}: tu sí. El termino va al final de la lista elegida (tambien en una carpeta conectada, sin tocarle nada
        mas) y, de las reglas seguras de esa propuesta, las que marcaste se aplican desde la siguiente transcripcion del
        proyecto de esa lista; las que desmarcaste no se vuelven a proponer. Solo vale lo que se esta proponiendo ahora
        en esa reunion: nada se aprende que no hayas visto."""
        abs_path = safe(b.get("path"), "file")
        termino = aprender.termino_valido(b.get("termino"))
        p = next((x for x in self._propuestas(abs_path)["propuestas"]
                  if termino and aprender.normal(x["termino"]) == aprender.normal(termino)), None)
        if not p:
            raise ApiError("Esa propuesta ya no está: puede que hayas vuelto a cambiar el texto. Recarga la reunión.")
        termino = p["termino"]
        quiere = {aprender.normal(x) for x in b.get("reglas") or [] if isinstance(x, str)}
        seguras = [x for x in p["antes"] if x["regla"]]
        reglas = [x for x in seguras if aprender.normal(x["texto"]) in quiere]
        if len(reglas) != len(quiere):
            raise ApiError("Esa corrección ya no se puede aprender. Recarga la reunión.")
        lista = str(b.get("lista") or "")
        v = find_vocab(None, None, lista) if lista else None
        if lista and not v:
            vid = lista[len(VLINK):].split("/")[0] if lista.startswith(VLINK) else ""
            if vid in VOCAB_LINKS and not os.path.isdir(VOCAB_LINKS[vid]["path"]):
                raise ApiError(f"No encuentro tu carpeta de listas «{VOCAB_LINKS[vid]['name']}»: ¿sin conexión o con "
                               "el disco desconectado? Vuelve a probar cuando esté, o elige otra lista.")
            raise ApiError("Esa lista ya no existe. Elige otra.")
        lang = self.settings.get().get("language") or ""
        if v:
            ya = anadir_a_lista(v, termino)
            try:
                cabe = termino not in vocab_info(v, lang)["dropped"]
            except OSError:
                cabe = True
        else:
            general = self.settings.get().get("prompt") or ""
            ya = aprender.normal(termino) in {aprender.normal(t) for t in engines.split_terms(general)}
            if not ya:
                general = aprender.anadir_texto(general, termino)
                self.settings.update(lambda d: d.update(prompt=general))
            cabe = termino not in vocab_fit(general, lang)["dropped"]
        proyecto, ahora = v["project"] if v else "", time.time()
        try:  # desde esta reunion ya no se vuelve a proponer (aunque haya ido a otra lista que la de la reunion)
            with self._tx_save_lock:
                reg = registro_aprendido(rel_of(abs_path))
                k = aprender.normal(termino)
                if k not in reg["respondidas"] and os.path.isdir(backup_dir(rel_of(abs_path))):
                    reg["respondidas"].append(k)
                    guardar_registro(rel_of(abs_path), reg)
        except OSError:
            log_exc()
        if reglas:
            def upd(d):
                a = aprendido_de(d)
                for x in reglas:  # una por lo que escribio Whisper y proyecto: la nueva sustituye a la vieja
                    kd = aprender.normal(x["texto"])
                    a["reglas"] = [r for r in a["reglas"] if not (aprender.normal(r["de"]) == kd
                                                                  and mismo_proyecto(r["proyecto"], proyecto))]
                    a["reglas"].append({"de": x["texto"], "a": termino, "proyecto": proyecto, "at": ahora})
                d["aprendido"] = a
            # la casilla desmarcada no se apunta como un «no» para siempre (no se veria en ningun sitio): en esta
            # reunion ya no se pregunta (respondidas) y, si lo vuelves a corregir en otra, se vuelve a ofrecer
            self.settings.update(upd)
        self.library.at = 0
        n = len(reglas)
        log(f"✦ aprendido: {termino} en {(v['project'] + ' · ' + v['name']) if v else 'el vocabulario general'} "
            f"({n} regla{'s' if n != 1 else ''})")
        return {"ya_estaba": ya, "cabe": cabe, "reglas": n,
                "lista": {k: v[k] for k in ("id", "project", "name")} if v else None,
                "prompt": None if v else self.settings.get().get("prompt") or ""}

    def aprender_no(self, b):
        """POST /api/aprender/no {termino}: «No»: ese termino no se vuelve a proponer (en ninguna reunion)."""
        termino = aprender.termino_valido(b.get("termino"))
        if not termino:
            raise ApiError("Falta el término.")
        k = aprender.normal(termino)

        def upd(d):
            a = aprendido_de(d)
            if k not in {aprender.normal(x) for x in a["no"]}:
                a["no"].append(termino)
            d["aprendido"] = a
        self.settings.update(upd)
        return {"ok": True}

    def aprender_deshacer(self, b):
        """POST /api/aprender/deshacer {path, de, a}: «Deshacer» de lo que cambio Tenca en esa reunion
        (deshacer_aprendido). Con el candado de las correcciones a mano, como Restaurar original."""
        with self._tx_save_lock:
            abs_path = safe(b.get("path"), "file")
            rel = rel_of(abs_path)
            if self.jobs.active_for(rel):
                raise ApiError("Se está transcribiendo; espera a que termine.")
            de = str(b.get("de") or "")
            hechos, saltados = deshacer_aprendido(rel, de, str(b.get("a") or ""))
        self.library.at = 0
        if hechos:
            self.jobs.schedule_outputs(rel)  # destinos de salida: el documento vuelve a lo que escribio Whisper
        # la regla sigue para las proximas reuniones: la interfaz ofrece olvidarla (su proyecto, para aprender_olvidar)
        proyecto = proyecto_de_reunion(abs_path)
        r = next((r for r in aprendido_de(self.settings.get())["reglas"] if aprender.normal(r["de"]) ==
                  aprender.normal(de) and mismo_proyecto(r["proyecto"], proyecto)), None)
        return {"deshechos": hechos, "no_deshechos": saltados, "regla": {"proyecto": r["proyecto"]} if r else None}

    def _reglas_publicas(self):
        return [{k: r[k] for k in ("de", "a", "proyecto")} for r in aprendido_de(self.settings.get())["reglas"]]

    def aprender_reglas(self, q):
        """GET /api/aprender/reglas: las reglas aprendidas y lo que dijiste que no se propusiera (Ajustes ›
        Proyectos)."""
        return {"reglas": self._reglas_publicas(), "no": aprendido_de(self.settings.get())["no"]}

    def aprender_regla(self, b):
        """POST /api/aprender/regla {antes: {de, proyecto} | null, de, a, proyecto}: cambiar una entrada del diccionario
        personal desde Ajustes (lo que escribio Whisper, lo correcto o el proyecto). Lo ya cambiado en reuniones
        anteriores no se toca: cada una sigue con su «Deshacer»."""
        de, a = aprender.termino_valido(b.get("de")), aprender.termino_valido(b.get("a"))
        if not de or not a:
            raise ApiError("Escribe lo que pone Whisper y lo correcto (sin comas ni punto y coma).")
        if de == a:
            raise ApiError("Lo que pone Whisper y lo correcto son iguales: no habría nada que cambiar.")
        toks = [t for t, _, _ in aprender.tokens(de)]
        if not toks or len(toks) > aprender.MAX_REGLA:
            raise ApiError("Lo que pone Whisper tiene que ser una palabra, o como mucho tres.")
        if all(aprender.fold(t) in aprender.COMUNES or aprender.fold(t) in aprender.ENLACES for t in toks):
            raise ApiError(f"«{de}» es una palabra muy corriente: cambiaría muchas frases que están bien. Pon algo más "
                           "concreto.")
        if len(aprender.letras(de)) < 3:
            raise ApiError("Lo que pone Whisper tiene que tener al menos 3 letras: algo más corto saldría en muchas "
                           "frases que están bien.")
        if not aprender.regla_valida(de, a):  # solo mayúsculas o tildes en una palabra corriente («plan» → «Plan»)
            if de.lower() != a.lower():  # cambian tildes (y quizá mayúsculas), no solo mayúsculas
                raise ApiError(f"Solo cambian las tildes de «{de}»: no hace falta en el diccionario, y cambiaría "
                               "frases que están bien.")
            raise ApiError(f"Cambiar «{de}» por «{a}» tocaría frases que están bien: si solo cambian las mayúsculas, "
                           "vale para siglas (KPI) o nombres como iPhone. Pon algo más concreto.")
        proyecto = str(b.get("proyecto") or "")
        antes = b.get("antes") if isinstance(b.get("antes"), dict) else None
        conocidos = {v["project"] for v in list_vocab()} | {""}
        if antes:
            conocidos.add(str(antes.get("proyecto") or ""))
        if not any(mismo_proyecto(proyecto, p) for p in conocidos):
            raise ApiError("Ese proyecto ya no existe. Elige otro.")
        proyecto = next((p for p in sorted(conocidos) if mismo_proyecto(proyecto, p)), proyecto)  # como se llama
        k, ka = aprender.normal(de), aprender.normal(str((antes or {}).get("de") or ""))
        pa = str((antes or {}).get("proyecto") or "")
        err = []

        def upd(d):
            ap = aprendido_de(d)
            es_la = lambda r: antes and aprender.normal(r["de"]) == ka and mismo_proyecto(r["proyecto"], pa)  # noqa
            if any(not es_la(r) and aprender.normal(r["de"]) == k and mismo_proyecto(r["proyecto"], proyecto)
                   for r in ap["reglas"]):
                err.append(f"Ya tienes «{de}» en tu diccionario para ese proyecto.")
                return
            if antes and not any(es_la(r) for r in ap["reglas"]):
                err.append("Esa entrada ya no está en tu diccionario.")
                return
            ap["reglas"] = [r for r in ap["reglas"] if not es_la(r)]
            ap["reglas"].append({"de": de, "a": a, "proyecto": proyecto, "at": time.time()})
            d["aprendido"] = ap
        self.settings.update(upd)
        if err:
            raise ApiError(err[0])
        log(f"✦ diccionario: {de} → {a}")
        return {"reglas": self._reglas_publicas(), "no": aprendido_de(self.settings.get())["no"]}

    def aprender_olvidar(self, b):
        """POST /api/aprender/olvidar {de, proyecto}: la regla deja de aplicarse en las proximas reuniones (lo ya
        cambiado se queda, con su Deshacer). {no: termino}: ese termino se puede volver a proponer."""
        k, proyecto = aprender.normal(str(b.get("de") or "")), str(b.get("proyecto") or "")
        kno = aprender.normal(str(b.get("no") or ""))  # {"no": termino}: que se pueda volver a proponer
        quitadas = []

        def upd(d):
            a = aprendido_de(d)
            if kno:
                a["no"] = [x for x in a["no"] if aprender.normal(x) != kno]
            else:
                quitadas[:] = [r for r in a["reglas"] if aprender.normal(r["de"]) == k
                               and mismo_proyecto(r["proyecto"], proyecto)]
                a["reglas"] = [r for r in a["reglas"] if r not in quitadas]
            d["aprendido"] = a
        self.settings.update(upd)
        if quitadas:
            log(f"✦ regla olvidada: {quitadas[0]['de']} → {quitadas[0]['a']}")
        return {"reglas": self._reglas_publicas(), "no": aprendido_de(self.settings.get())["no"]}
