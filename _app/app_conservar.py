# -*- coding: utf-8 -*-
"""App, la parte de qué pasa con el audio de cada reunión (punto 17 del review del 14/11/2026, conservar.py): el
vigilante que, al arrancar y cada pocos minutos, manda a la papelera el audio de las reuniones con «Guardar 15 días»
(pasado el plazo) o «Borrar al terminar», cuando ya están transcritas y resumidas. Nunca mientras se graba.

La política se elige con la tarjeta de grabar (rec_start la escribe en el .md) o en la página de la reunión (notes_save
con la propiedad «audio», que despierta al vigilante)."""
import os
import threading
import time

import conservar
from carpetas import to_recycle_bin
from common import ApiError, log, log_exc
from rutas import rel_of, safe

TEXTO_POL = {"15-dias": f"{conservar.DIAS} días", "borrar": "al terminar"}
RESPIRO_S = 20  # tras despertarlo: que el resumen recién escrito y lo que encadena ya estén en su sitio


class Conservar:
    def _audio_evento(self):
        ev = self.__dict__.get("_audio_ev")
        if ev is None:
            ev = self.__dict__.setdefault("_audio_ev", threading.Event())
        return ev

    def audio_despertar(self):
        """Que el vigilante mire ya (una política cambiada, una reunión que acaba de quedar libre)."""
        self._audio_evento().set()

    def audio_vigia(self):
        """Hilo del arranque (main): el primer repaso a los PRIMERO_S y después cada BARRIDO_S o cuando lo despierten
        (con RESPIRO_S de margen)."""
        ev = self._audio_evento()
        ev.wait(conservar.PRIMERO_S)
        while not getattr(getattr(self, "jobs", None), "closing", False):
            despierto = ev.is_set()
            ev.clear()
            if despierto:
                time.sleep(RESPIRO_S)
            try:
                self.audio_repaso()
            except Exception:
                log_exc()
            ev.wait(conservar.BARRIDO_S)

    def _audio_grabando(self):
        """Algo se está grabando (la grabadora de la app, OBS o un directo): entonces no se borra nada."""
        r = getattr(self, "rec", None)
        if r is not None and r.alive():
            return True
        jobs = getattr(self, "jobs", None)
        if jobs is not None:
            with jobs.lock:
                if any(j.live and j.status in ("queued", "running") for j in jobs.list):
                    return True
        return any(a.get("recording") for a in list(self.library.flat))

    def audio_estado(self, a):
        """(toca, motivo): si ya toca mandar el audio de esa reunión a la papelera y, si no, por qué (ESPERAS)."""
        pol = conservar.politica_de(a.get("props"))
        toca, motivo = conservar.debe_borrar(a, pol)
        if not toca:
            return False, motivo
        if self._ocupada(a["path"]) or self._movs().get(a["path"]):
            return False, "ocupada"
        return True, None

    def audio_repaso(self):
        """Un repaso de la biblioteca: los audios que ya tocan, a la papelera. Devuelve cuántos."""
        self.library.refresh(max_age=5)  # antes de mirar si se graba: con la biblioteca de ahora
        if self._audio_grabando():
            return 0
        n = 0
        for a in list(self.library.flat):
            if self._audio_grabando():  # ha empezado una grabación: lo demás, en el siguiente repaso
                break
            try:
                abs_audio = safe(a["path"], "reunion")
            except ApiError:
                continue
            if not a.get("sin_audio") and os.path.isfile(conservar.marca_de(abs_audio)):
                self._audio_marca_vieja(a, abs_audio)
                continue
            if not self.audio_estado(a)[0]:
                continue
            n += self._audio_borrar(a["path"])
        if n:
            self.library.at = 0
        return n

    def _audio_marca_vieja(self, a, abs_audio):
        """Marca junto a un audio que sí está: a medio borrar (se quita) o sacado de la papelera (se guarda)."""
        with self._move_lock():
            r = conservar.resolver_marca(abs_audio)
            if r == "recuperado":
                try:
                    self.notes_save({"path": a["path"], "props": {conservar.CLAVE: conservar.POR_DEFECTO}})
                except Exception:
                    log_exc()
                log(f"🔊 {a['path']}: el audio ha vuelto de la papelera; se guarda")
            elif r:
                log(f"   {a['path']}: el borrado del audio no terminó; se vuelve a mirar")
        self.library.at = 0

    def _audio_borrar(self, rel):
        """El de una reunión, con el candado de mover y borrar y comprobado todo otra vez dentro. 1 si se ha mandado
        a la papelera."""
        with self._move_lock():
            self.library.refresh(max_age=0)
            a = self.library.find(rel)
            if not a or self._audio_grabando():
                return 0
            toca, _ = self.audio_estado(a)
            if not toca:
                return 0
            try:
                abs_audio = safe(rel, "file")
            except ApiError:
                return 0
            pol = conservar.politica_de(a.get("props"))
            try:
                marca = conservar.borrar_audio(abs_audio, a, pol, to_recycle_bin)
            except ApiError as e:
                if e.msg in conservar.ESPERAS:  # una unidad sin papelera o un audio que no cabe: se dice en su página
                    if self.library.audio_no.get(rel) != e.msg:
                        log(f"   {rel}: el audio no se manda a la papelera ({e.msg})")
                    self.library.audio_no[rel] = e.msg
                    self.library.at = 0
                else:
                    log(f"   {rel}: el audio no se ha podido mandar a la papelera ({e.msg}); se reintenta")
                return 0
            except OSError as e:
                log(f"   {rel}: el audio no se ha podido mandar a la papelera ({e.__class__.__name__}); se reintenta")
                return 0
            self.library.audio_no.pop(rel, None)
            mb = (marca.get("bytes") or 0) / 1e6
            log(f"🗑 audio a la papelera ({TEXTO_POL.get(pol, pol)}, {mb:.0f} MB): {rel_of(abs_audio)}")
            self.library.at = 0
            return 1
