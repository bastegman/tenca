# -*- coding: utf-8 -*-
"""App, la parte de los trabajos: transcribir, cancelar, el directo (modelo, traduccion, propuesta), ocultar al
compartir y limpiar la lista.

Salio tal cual de server.py al partirlo (15/11/2026): App hereda de esta clase y server.py la reexporta."""
import directo_avisos
import engines
import live
from api import WINDOW, captura_estado
from biblioteca import traduccion_destinos
from common import APP_NAME, ApiError, log
from rutas import alias_of


class Directo:
    # --- POST
    def transcribe(self, b):
        with self._move_lock():  # una reunion que se esta moviendo: se espera y se encola con su ruta nueva
            return self._transcribe(b)

    def _transcribe(self, b):
        paths = [alias_of(p) for p in (b.get("paths") or ([b["path"]] if b.get("path") else []))]
        preset = b.get("preset") or self.settings.get()["preset"]
        ids = self.jobs.submit(paths, preset, b.get("language"), resume=bool(b.get("resume")))
        # el idioma elegido es de estas reuniones: el del perfil (Ajustes) no cambia, como al grabar
        self.settings.update(lambda d: d.update(preset=preset))
        return {"added": ids}

    def cancel(self, b):
        return {"ok": self.jobs.cancel(b.get("id"))}

    def start_live(self, b):
        if b.get("preset"):
            self.jobs._check_live_preset(b["preset"])
            self.settings.update(lambda d: d.update(live_preset=b["preset"]))
        return {"id": self.jobs.start_live(b.get("path"), b.get("preset"))}

    def live_model(self, b):
        return self.jobs.set_live_preset(b.get("id"), b.get("preset"), bool(b.get("restart")))

    def live_translate(self, b):
        """Traducción del directo en la nube, por reunión (apagada por defecto): {id, on, to}. to: «es» o «en» (msg
        1152); se recuerda para esa reunión (traduccion_destino), también con la traducción apagada. live.py hace el
        resto: con otro destino, vuelve a traducir lo último al idioma nuevo."""
        job = self.jobs._live_job(b.get("id"))
        to = b.get("to")
        if to is not None and to not in live.TRANSLATE_TO:
            raise ApiError("La traducción del directo solo puede ser al español o al inglés.")
        if b.get("on"):  # sin servicio de chat (Azure OpenAI) no hay traduccion: se dice, no falla a medias
            why = engines.chat_missing(self.settings.get()["cloud"], "La traducción del directo")
            if why:
                raise ApiError(why)
        if to is not None:
            job.translate_to = to
            traduccion_destinos().set(job.path, to)
        job.translate = bool(b.get("on"))
        if isinstance(job.directo, dict):  # que la respuesta ya lo refleje (live.py lo vuelve a publicar en <1 s)
            job.directo = dict(job.directo, translate=job.translate, translate_to=live.translate_to(job))
        return job.public()

    def live_propuesta(self, b):
        """«Lo último suena a … · No» (B6): {id, rechazar: "pt"}. Ese idioma no se vuelve a proponer en esta grabación.
        («Cambiar» no pasa por aquí: es el cambio de idioma de siempre, POST /api/notes.)"""
        job = self.jobs._live_job(b.get("id"))
        idioma = str(b.get("rechazar") or "")
        if idioma not in directo_avisos.PROPONIBLES:
            raise ApiError("Idioma desconocido.")
        (job.opts or {}).setdefault("rechazos", set()).add(idioma)
        p = getattr(job, "propuesta", None)
        if p is not None:
            p.rechazar(idioma)
        if isinstance(job.directo, dict):  # que la respuesta ya lo refleje (live.py lo vuelve a publicar en <1 s)
            job.directo = dict(job.directo, propuesta=None)
        log(f"   directo {job.name}: no se propone más {idioma} en esta grabación")
        return job.public()

    def ocultar_captura(self, b):
        """POST {activado}: guarda «Ocultar Tenca al compartir pantalla» y lo aplica ya a la ventana (Ajustes › Al
        grabar; sin «Guardar», como «Arrancar con Windows»). Sin «activado», solo dice cómo está (como el GET)."""
        if isinstance(b, dict) and "activado" in b:
            on = b.get("activado") is True
            self.settings.update(lambda d: d.update(ocultar_al_compartir=on))
            if WINDOW is not None:
                WINDOW.set_capture(on)
            log(f"ajustes: ocultar {APP_NAME} al compartir pantalla {'activado' if on else 'desactivado'}")
        return captura_estado(self.settings.get())

    def clear_jobs(self, b):
        self.jobs.clear()
        return {"ok": True}
