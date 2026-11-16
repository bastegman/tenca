# -*- coding: utf-8 -*-
"""Diagnostico: claves enmascaradas y las lineas de server.log que se pueden ensenar (plantillas DIAG_LINES).

Salio tal cual de server.py al partirlo (15/11/2026); server.py lo reexporta."""
import re

import engines
import voices
from common import APP_NAME


def masked(key):
    return (key[:4] + "…" + key[-4:]) if key and len(key) > 12 else ("••••" if key else "")


KEY_LIKE = re.compile(r"\b(?:gsk|sk)[-_][A-Za-z0-9_-]{12,}")  # Groq (gsk_...) y OpenAI (sk-...)


def scrub(text, keys=()):
    """Enmascara claves en un texto: las conocidas (la API key y las adicionales) y cualquier token con pinta de
    API key. Para el diagnostico que se copia desde Acerca de."""
    for k in sorted({k for k in keys if k}, key=len, reverse=True):
        text = text.replace(k, masked(k))
    return KEY_LIKE.sub(lambda m: masked(m.group(0)), text)


# ----------------------------------------------------------------------------- diagnostico: lista blanca
# «Copiar diagnostico» se pega donde se pide ayuda, a veces en un sitio publico. Lleva solo lo que se sabe tecnico:
# version, Python y sistema, motor y modelo, contadores y, de las ultimas lineas de server.log, lo fijo de los mensajes
# que escribe la propia app (DIAG_LINES) con sus cifras, modelos y codigos de error. Todo lo demas (nombres de
# reuniones, carpetas, proyectos, temas y personas, rutas, el texto de un error, lo que no se reconoce) va como
# «<texto>». No depende de lo que la app sepa de la biblioteca: lo ya borrado, renombrado u olvidado tampoco sale.
DIAG_FREE = "<texto>"
# Cada plantilla es un mensaje de log() con sus huecos. {t}: texto libre (siempre «<texto>»); {b}: lo mismo, dentro de
# unos corchetes; {e}: un error («<texto>» y sus codigos); {n}: una cifra; {m}: un modelo o una velocidad conocidos
# (DIAG_MODELS); {c}: una excepcion conocida (diag_exceptions); {k}: nombres de ajustes; {l}: un idioma; {a}: la app de
# la llamada; {u}: la URL local; {v}: una version; {y}: el .py de una traza (solo su nombre); {f}: la funcion de una
# traza. ⟦…⟧ es opcional y lo demas, literal. Gana la primera que encaja (las concretas van antes que las generales);
# sin plantilla, la sangria, el icono y «<texto>» (diag_line).
DIAG_LINES = (
    # trabajos de la cola, del directo, del resumen y de las voces: «<icono> <reunion>  <que>»
    "▶ {t}  [{m}]",
    "● Grabando: {t}⟦ [{b} · {b}]⟧⟦ (solo grabar, sin transcribir)⟧",
    "■ Grabación parada: {t} ({n} min)",
    "● {t}  transcripción en directo⟦ (automática)⟧",
    "↻ {t}  repaso final de la grabación entera en la nube",
    "✔ {t}  directo terminado ({n} min grabados)",
    "■ {t}  directo detenido",
    "✨ {t}  resumen⟦ (automático)⟧ en cola",
    "✔ {t}  resumen listo ({n} s, {m})",
    "✖ {t}  resumen: {e}",
    "🗣 {t}  voces⟦ (automático)⟧ en cola",
    "✔ {t}  voces: {n} persona⟦s⟧ · {n} reconocida⟦s⟧ ({n} s)",
    "■ {t}  voces canceladas",
    "✖ {t}  voces: {e}",
    "✔ {t}  {n} min⟦  ({n}x tiempo real)⟧",
    "■ {t}  cancelado",
    "✖ modelos de voces: {e}",
    "✖ {t}  {e}",
    "   {t}: sin repaso final (tiene correcciones a mano)",
    "   directo {t}: vocabulario, idioma y contexto actualizados",
    "   directo {t}: rehacer con {m}",
    "   directo {t}: modelo {m}",
    "   {t}: transcrita en local, sin resumen automático (no sale del PC; se puede resumir a mano)",
    "   continuando desde {n} min",
    "   en lo que faltaba no hay voz: se da por completa",
    "   se mantiene el texto del directo",
    "   no se pudo escribir la transcripción todavía ({c})",
    "   voces: el PC tiene poca memoria libre; se espera (como mucho {n} min)",  # antes de la de {e}, que se la comeria
    "   voces: {e}",
    "   {t}: documento escrito en {n} destino(s)⟦ · sin tocar: {t}⟧",
    # repaso final pendiente al apagar (fix-directo) y destinos de salida al apagar o borrar (fix-perfil)
    "   {t}: el repaso final se quedó pendiente al apagar; se retoma",
    "   {t}: el repaso final pendiente ya no se puede hacer",
    "   repaso final pendiente de {t}: la reunión ya no está ahí, se olvida",
    "   el repaso final queda pendiente para el próximo arranque",
    "   {t}: el directo no dejó texto ({e}); lo transcribe el repaso final",
    "   destinos de salida: no ha dado tiempo a escribir {t} al apagar",
    "   no se pudieron mandar a la papelera los documentos de {t}: {e}",
    # directo (live.py)
    "   directo: idioma {l} desde el siguiente trozo",
    "   idioma: {l}⟦ ({n}%)⟧",
    "   rehaciendo desde el principio con {m}",
    "   se salta un trozo que Whisper no consigue transcribir",
    "   directo reanudado: vuelve a haber cupo",
    "   trozo sin transcribir ({e}); se sigue con el siguiente",
    "   traducción del directo: {e}",
    "   trozo de {n} s ({n} s de voz, {n} frases): texto a los {n} s{t}",
    "   {e} ({n} s de audio, intento {n})",
    "   directo: no se pudieron releer los ajustes de la nube ({c})",
    # agente de IA del PC (Claude Code)
    "   agente: {m} {v} comprobado ({n} s)",
    "   agente: {m}: {e}",
    # biblioteca, carpetas, vocabulario y voces
    "⤓ importado: {t}",
    "🗑 vocabulario a la papelera: {t}",
    "🗑 {t} → papelera ({n} archivos⟦ y {n} documentos de los destinos⟧)",
    # qué pasa con el audio de cada reunión (punto 17, app_conservar.py y conservar.py)
    "🗑 audio a la papelera (15 días, {n} MB): {t}",
    "🗑 audio a la papelera (al terminar, {n} MB): {t}",
    "   {t}: el audio no se manda a la papelera (sin_papelera)",
    "   {t}: el audio no se manda a la papelera (grande)",
    "   {t}: el audio no se ha podido mandar a la papelera ({c}); se reintenta",
    "   {t}: el audio no se ha podido mandar a la papelera ({e}); se reintenta",
    "   {t}: el borrado del audio no terminó; se vuelve a mirar",
    "🔊 {t}: el audio ha vuelto de la papelera; se guarda",
    "   {t}: el audio ha vuelto de la papelera, pero no se ha podido apuntar «Guardar» ({c}); se reintenta",
    "   audio: no se pudo comprobar la papelera ({c})",
    # quién habla mientras se graba con el directo en la nube (punto 56, voces_directo.py)
    "🗣 quién habla en directo: {n} min identificados ({n} personas, {n} s)",
    "🗣 quién habla en directo: cortado ({n} min; la grabación ha terminado o el directo ya no va en la nube)",
    "🗣 quién habla en directo: no se ha podido ({c}); se hará al terminar",
    "   tema de {n} reuniones: siguen a la carpeta renombrada {t}",
    "↪ {t}  →  {t}",
    "   no se pudo quitar la carpeta vacía {t}",
    "📁 {t}: se moverá a {t} al terminar",
    "📁 {t}: ya no se mueve (deshecho)",
    "   movimiento pendiente de {t}: la reunión ya no está ahí, se olvida",
    "   movimiento pendiente de {t}: la carpeta {t} ya no existe, se queda donde está",
    "   movimiento pendiente de {t}: {e} Se reintenta en {n} s",
    "   carpeta principal de {t}: era la de una reunión ({t}); pasa a {t}",
    "   {k}: el proyecto de la raíz se llamaba «{t}»; pasa a «{t}», el nombre de su carpeta",  # migrate_app_name
    "   carpeta principal de {t}⟦ (workstream)⟧: {t}",
    "↺ {t}: transcripción original restaurada",
    "✦ {t}: {n} cambio⟦s⟧ con lo aprendido de tus correcciones",
    "↺ {t}: {n} cambio⟦s⟧ aprendido⟦s⟧ deshecho⟦s⟧",
    "✦ aprendido: {t} en {t} ({n} regla⟦s⟧)",
    "✦ regla olvidada: {t}",
    "✦ diccionario: {t}",
    "   directo: la nube no está lista ({e}); se transcribe en este PC con {m}",
    "📱 recibiendo del móvil en la wifi ({n}) durante {n} min",
    "📱 un móvil ha abierto el código",
    "📱 ya no se reciben audios del móvil",
    "📱 no se ha guardado un audio del móvil ({t})",
    "   móvil: no se han podido leer las redes ({e})",
    "🔗 carpeta vinculada: {t}",
    "🔗 carpeta desvinculada (no se ha tocado nada): {t}",
    "🔤 vocabulario guardado: {t} ({n} términos)",
    "🔤 vocabulario importado de {t}: {n} listas copiadas, {n} omitidas ({t})",
    "   perfil de reunión guardado: {k}",
    "🗣 huella guardada: {t} ({n} huella⟦s⟧)",
    "🗣 personas fusionadas ({n} reuniones actualizadas)",
    "🗣 todas las huellas olvidadas",
    "🗣 persona olvidada",
    "🗣 voces activadas",
    "🗣 voces desactivadas",
    "🎧 audio mezclado exportado ({n} min)",
    "   modelos de voces descargados",
    "   voces.json no se pudo leer ({e}); se aparta como voces.json.ilegible",
    # el cambio de modelo de huellas (voices.Voices._migrar, rehacer_huellas) y rehacerlas al arrancar (app_voces)
    "   voces.json no está; se recupera de voces.json.bak",
    "   voces.json.bak no se pudo leer ({e}); se empieza de cero",
    "   voces.json: no se pudo escribir ({c})",
    "   voces.json: modelo de huellas {m}; {n} huellas por rehacer desde sus reuniones",
    "   huella por rehacer ({t}, {t}): {c}",
    "   huellas por rehacer: no se pudo escribir voces.json ({c})",
    "   huellas rehechas con el modelo nuevo: {'rehechas': {n}, 'sin_audio': {n}, 'fallidas': {n}}",
    "🗣 {n} huella⟦s⟧ de voz por rehacer con el modelo nuevo: se rehacen en segundo plano",
    "   huellas por rehacer: se espera a que termine lo que está en marcha ({k})",
    "   huellas por rehacer: el PC va justo ({k}); se espera",
    "   huellas por rehacer: el PC lleva 1 h sin memoria libre; se deja para el próximo arranque",
    "   huellas por rehacer: faltan los modelos de voces; se descargan primero",
    "   huellas por rehacer: sin los modelos de voces ({e}); se intentará la próxima vez",
    "   huellas por rehacer: la librería de voces no carga en este PC ({e})",
    "🗣 huellas de voz con el modelo nuevo: {n} rehecha⟦s⟧, {n} sin su reunión, {n} por reintentar",
    # arranque, ajustes, grabadora, llamadas y apagado
    "arranque · {u} · carpeta {t} · datos {t} · Python {v}⟦ · pid {n}⟧",
    "=" * 64,  # el recuadro de main() con la URL, la carpeta y como apagar
    f"  {APP_NAME.upper()}  ·  {{u}}",
    "  Carpeta: {t}",
    f"  Apagar: Ajustes > Apagar {APP_NAME} (con consola, también cerrándola o Ctrl+C).",
    "   sin DPAPI fuera de Windows: los secretos se guardan en claro (plain:base64)",
    "modelo {m} precargado para el directo en {n} s",
    "modelo {m} precargado para la cola en {n} s",
    "no se pudo precargar {m}: {e}",
    "no se precarga {m}: el PC tiene {n} GB de memoria libre (se carga al empezar la reunión)",
    "   borrados los trozos de audio temporales que dejó la sesión anterior",
    "   no se pudo descifrar {k} de settings.json ({e}): vuelve a pegarla en Ajustes",
    # ajustes a prueba de valores raros (ajustes.normalizar_ajustes, punto 5 del review del 14/11/2026)
    "   ajuste {k} no válido: vuelve al valor por defecto",
    "   ajuste {k}: {n} elementos no válidos, fuera",
    # el PC despierto (despierto.py, punto 30), los hilos y los fallos graves (fallos.py, punto 33) y «Buscar versión
    # nueva» (paquete.py, punto 60)
    "   PC despierto: pedido a Windows (grabando)",
    "   PC despierto: pedido a Windows (transcribiendo)",
    "   PC despierto: pedido a Windows (grabando y transcribiendo)",
    "   PC despierto: ya no hace falta, se suelta",
    "   PC despierto: Windows no lo acepta (error {n})",
    "✖ hilo {t}: se ha parado por un error ({c})",
    "   ✖ se ha parado {t}: hasta reiniciar no funcionará",
    "✖ error sin atender ({c})",
    "   la sesión anterior dejó un volcado de faulthandler ({n} líneas): fallos-graves.anterior.log",
    "   la sesión anterior terminó sin apagar",
    "   sin volcado de fallos graves ({c})",
    "   versión nueva: consultada ({s})",
    "   versión nueva: no se pudo consultar ({e})",
    "   no se pudo copiar settings.json ({c}): no se escribirá encima en esta sesión",
    "   settings.json no se puede leer ({e})⟦: copia en {t}⟧; recuperado: {t}; lo demás vuelve a los valores por "
    "defecto",
    "   aviso pendiente: el resumen automático ya no sube a la nube lo transcrito en local",
    "   no se pudo guardar {t} ({c})",
    "   micro silenciado",
    "   micro reactivado",
    "   reunión detectada ({a}): aviso flotante abierto",
    "   reunión detectada ({a}): aviso enviado",
    "   Outlook: GetDateFormatEx/GetTimeFormatEx han fallado (error {n}): el filtro va con el formato de EE. UU.",
    # calendario (fix-calendario): estados, pasos de «Probar» y entorno, sin asuntos ni nombres (la genérica, la
    # última de las suyas: si no, se come las de «probar»)
    "   calendario: probar · {s} · {s} ({n} ms)",
    "   calendario: entorno · Python {n} bits · Office {s} · directiva {s} · fecha {s} · horas {s} · asistentes {s}",
    "   calendario: asistentes {s}",
    "   calendario: activado desde Ajustes",
    "   calendario: apagado desde Ajustes",
    "   calendario: el formato de fecha {s} no le vale a Outlook ({e})",
    "   calendario: con el formato de fecha {s}, Outlook da {n} citas fuera de la hora pedida",
    "   calendario: agenda · {n} citas⟦ (cortada)⟧ ({n} ms)",
    "   hoy: reunión preparada ({n} cosas)",
    "   hoy: reunión sin preparar ({n} cosas)",
    # «Ya ha empezado «X»» al parar una grabación (hoy): sin el asunto
    "   siguiente reunión: propuesta (empezó hace {n} min)",
    "   siguiente reunión: propuesta (en Teams)",
    "   siguiente reunión: aviso flotante abierto",
    "   siguiente reunión: «Ahora no»",
    "   siguiente reunión: grabar desde la app",
    "   siguiente reunión: grabar desde el aviso",
    "   siguiente reunión: ya no está en curso",
    # fin de la llamada (directo-avisos): «paro en 30 s · No terminar»
    "   la llamada de Teams ha terminado: se para la grabación en {n} s si no se pide seguir",
    "   la llamada sigue (o no se puede saber): no se para la grabación",
    "   «No terminar»: la grabación sigue aunque la llamada haya terminado",
    "■ {t}: parada al terminar la llamada de Teams (sin «No terminar» en {n} s)",
    "   calendario: agenda · {s} ({n} ms)",
    "   calendario: {s} ({n} ms)",
    "apagar pedido desde la interfaz ({n} trabajos en cola o en marcha)",
    "apagar pedido desde la bandeja ({n} trabajos en cola o en marcha)",
    # icono en la bandeja (bandeja.py) y arrancar con Windows
    "icono en la bandeja (hwnd {n})",
    "icono en la bandeja (hwnd None)",
    "   sin icono en la bandeja ({c}): cerrar la ventana la deja en segundo plano como antes",
    "icono de la bandeja retirado",
    "   icono de la bandeja: el hilo de la ventana no contesta; se quita desde aquí",
    "   primer cierre con la X: aviso en el icono de la bandeja",
    "   salir desde la bandeja: está grabando, no se sale",
    "   aviso: {t}",
    "   salir desde la bandeja: cancelado (trabajos en marcha)",
    "arrancar con Windows: activado",
    "arrancar con Windows: desactivado",
    "arrancar con Windows: acceso directo actualizado",
    "arrancar con Windows: sigue activado, ahora como «{t}»",  # la copia con el nombre anterior de la app
    "arrancar con Windows: quitado el acceso directo antiguo «{t}»",
    "arrancar con Windows: desactivado en Aplicaciones de inicio, como lo tenía «{t}»",  # _startup_aprobado_copiar
    "arrancar con Windows: activado en Aplicaciones de inicio, como lo tenía «{t}»",
    "   arrancar con Windows: {e}",
    "ventana oculta: la app sigue en segundo plano (el acceso directo la vuelve a abrir)",
    "apagando ({t})",
    "apagado",
    "la app ya estaba abierta: se ensena su ventana",
    # doble apertura al arrancar (fix-doble-apertura): la segunda copia, la marca de instancia y la ventana en camino
    "la app ya estaba abierta: se enseña su ventana · pid {n}",
    "otra copia tiene la marca y no contesta (arrancando): si se está abriendo, su ventana saldrá sola · pid {n}",
    "otra copia tiene la marca y no contesta (libre): si se está abriendo, su ventana saldrá sola · pid {n}",
    "otra copia tiene la marca y no contesta (otro): si se está abriendo, su ventana saldrá sola · pid {n}",
    "otra copia tiene la marca y no contesta (hermana): si se está abriendo, su ventana saldrá sola · pid {n}",
    "   aún se está abriendo: su ventana saldrá sola",
    "   la otra copia: pid {n}, desde hace {n} s",
    "   no contesta a /api/mostrar: se abre en Edge desde aquí",
    "   /api/mostrar: {e}",
    "   Edge se ha abierto hace {n} s: no se abre otro",
    "   marca de instancia: no se puede abrir ({e})",
    "   marca de instancia: no se puede bloquear ({e})",
    "   marca de instancia: sin carpeta de datos ({e})",
    "   la versión anterior se ha cerrado pero su marca de instancia sigue: se continúa sin ella",
    "   se ha vuelto a abrir mientras arrancaba: la ventana se trae al frente",
    "la ventana propia no se ha enseñado en {n} s: se abre en Edge",
    "ventana abierta (WebView2; hwnd {n})",
    "ventana abierta (WebView2; hwnd None)",
    "AppUserModelID {t}: fijado",
    "AppUserModelID {t}: no disponible",
    "la ventana se ha cerrado sin apagar: la app sigue en segundo plano",
    "no se pudo abrir la ventana propia (detalle arriba): se abre en Edge",
    "la ventana propia necesita WebView2 y pywebview usaría «{t}» (¿falta el runtime de WebView2?): se abre en Edge",
    "Windows pide cerrar la ventana (WindowsShutDown): se deja cerrar",  # fix-windows: apagar o cerrar sesion
    "Windows pide cerrar la ventana (TaskManagerClosing): se deja cerrar",
    "Windows cierra la sesión o se apaga: apagado limpio",
    "Windows ha pedido cerrar: apagado limpio",
    # sesion de la API (fix-seguridad): el metodo y la ruta pedidos sin token, como texto
    "   403 sin token de sesión (navegador): {t} (solo el primero)",
    "   403 sin token de sesión (programa): {t} (solo el primero)",
    "   sesión: cookie entregada a la interfaz",
    "   sesión: /?t= con un token que no es el de esta sesión (sin cookie)",
    "puerto {n} ocupado ({t}): {e}",
    "  Interfaz: {u}?t=<el contenido de {t}>",
    "(no hay server.log)",
    # trazas (log_exc): el .py, la linea y la funcion; de la excepcion, su tipo y sus codigos
    "Traceback (most recent call last):",
    '  File "{y}", line {n}, in {f}',
    "During handling of the above exception, another exception occurred:",
    "The above exception was the direct cause of the following exception:",
    "{c}: {e}",
    "{c}",
)
DIAG_MODELS = frozenset(
    {x for p in engines.PRESETS for x in (p["id"], p["label"], p.get("live_label"), p.get("model")) if x}
    | {voices.MODEL_ID}  # el modelo de huellas de «Quién habla» (la linea del cambio de modelo)
    | set(engines.SUMMARY_MODELS) | {m for v in engines.PROVIDERS.values() for m in v.get("models") or []}
    | {engines.LIVE_MODEL_DEFAULT, getattr(engines.Summarizer, "TRANSLATE_MODEL", None), "gpt-4o-mini"}
    | {c.label for c in engines.AGENTES.values()} - {None})
# codigos de error que se pueden ensenar: HTTP de la nube (engines: «HTTP 401: …»), errno y WinError, HRESULT de Windows
# (COM de Outlook, WebView2), los codigos de error de las APIs compatibles con OpenAI y, de las palabras (exc), solo las
# excepciones conocidas (diag_codes)
DIAG_CODES = re.compile(
    r"\bHTTP ?[1-5]\d\d\b|\[(?:Errno|WinError) -?\d+\]|\b0x[0-9A-Fa-f]{8}\b|(?<!\d)-2147\d{6}\b"
    r"|\b(?:rate_limit_exceeded|invalid_api_key|insufficient_quota|model_not_found|context_length_exceeded"
    r"|request_too_large|model_decommissioned|invalid_request_error|authentication_error|permission_denied)\b"
    r"|(?<![\w.])(?P<exc>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)")
# lo que puede ser cada hueco (el resto: .+?); {b}, {u} y {v} no pasan de sus separadores, para no probar en balde
_DIAG_INNER = {"n": r"\d+(?:[.,]\d+)?", "c": r"[A-Za-z_][A-Za-z0-9_.]*", "e": r".*?", "b": r"[^\[\]]+?", "u": r"\S+",
               "v": r"\S+"}
_DIAG_HEAD = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [A-Z] ")  # «AAAA-MM-DD HH:MM:SS I » de server.log
# una linea mas larga (el HTML de un error, p. ej.) no se compara con las plantillas: con varios huecos de texto libre,
# ver si encaja costaria segundos y bloquearia «Copiar diagnostico». Sale como una sin plantilla (diag_line)
DIAG_MAX = 2000


def _diag_rx(template):
    """La plantilla como expresion regular: los literales, escapados; cada hueco, un grupo con su tipo delante."""
    out = []
    for i, tok in enumerate(re.split(r"(\{[a-z]\}|⟦|⟧)", template)):
        if tok == "⟦":
            out.append("(?:")
        elif tok == "⟧":
            out.append(")?")
        elif re.fullmatch(r"\{[a-z]\}", tok):
            out.append(f"(?P<{tok[1]}{i}>{_DIAG_INNER.get(tok[1], '.+?')})")
        else:
            out.append(re.escape(tok))
    return re.compile("".join(out))


_DIAG_RX = tuple(_diag_rx(t) for t in DIAG_LINES)


def diag_exceptions():
    """Las excepciones que el diagnostico ensena, lista cerrada: las clases de excepcion que existen en este proceso
    (las de Python, las de la app y las de las librerias cargadas; las de requests aunque esta sesion aun no haya usado
    la nube), por su nombre y con su modulo, como las escribe una traza. Lo que solo tiene su forma (un trozo de un
    nombre de carpeta o de archivo, «EarlyWarning» o «Omega.FusionError») no esta."""
    try:
        import requests.exceptions  # noqa: F401
    except ImportError:
        pass
    seen, todo = {BaseException}, [BaseException]
    while todo:
        for sub in type.__subclasses__(todo.pop()):
            if sub not in seen:
                seen.add(sub)
                todo.append(sub)
    names = set()
    for cls in seen:
        names |= {cls.__name__, cls.__qualname__}
        if cls.__module__ not in ("builtins", "__main__"):
            names.add(f"{cls.__module__}.{cls.__qualname__}")
    return names


def diag_codes(text):
    """« (HTTP 401, PermissionError)»: los codigos de error de un texto libre (sin repetir, 4 como mucho; nada si no
    hay). De las palabras, solo las excepciones conocidas (diag_exceptions), entera o la ultima parte de un nombre con
    puntos; no un nombre en minusculas sin modulo («error») ni Error, Exception o Warning a secas."""
    known, codes = diag_exceptions(), []
    for m in DIAG_CODES.finditer(text or ""):
        code = m.group(0)
        if m.group("exc") is not None:
            code = next((c for c in (code, code.rsplit(".", 1)[-1]) if c in known and ("." in c or c[0].isupper())
                         and c not in ("Error", "Exception", "Warning")), None)
        if code and code not in codes:
            codes.append(code)
            if len(codes) == 4:
                break
    return f" ({', '.join(codes)})" if codes else ""


def _diag_meeting_app(v):
    from recording import MEETING_APPS
    return v in {label for _, label in MEETING_APPS}


# lo que un hueco tiene que ser para ensenarse tal cual (los que no estan, {t}, nunca)
DIAG_CHECKS = {
    "m": lambda v: v in DIAG_MODELS,
    "c": lambda v: v in diag_exceptions(),
    "k": lambda v: all(re.fullmatch(r"[a-z_.]{2,40}", x) for x in v.split(", ")),
    "s": lambda v: all(re.fullmatch(r"[a-z][a-z0-9_]{1,40}", x) for x in v.split(", ")),  # códigos (calendario.py)
    "l": lambda v: bool(re.fullmatch(r"auto|detectar|[a-z]{2}(?:-[a-z]{2}){0,2}", v)),  # hasta la de tres (es-en-pt)
    "a": _diag_meeting_app,
    "u": lambda v: bool(re.fullmatch(r"http://127\.0\.0\.1:\d{2,5}/", v)),
    "v": lambda v: bool(re.fullmatch(r"\d+(?:\.\d+){1,3}[a-z0-9]{0,6}", v)),
    "y": lambda v: bool(re.fullmatch(r"[A-Za-z0-9_.-]+\.pyw?", v)),
    "f": lambda v: bool(re.fullmatch(r"[A-Za-z_<][A-Za-z0-9_<>.]*", v)),
}


def _diag_value(kind, v):
    if kind == "n":
        return v
    if kind == "e":
        return DIAG_FREE + diag_codes(v) if v.strip() else v
    if kind == "y":  # de la ruta de una traza, solo el nombre del .py
        v = re.split(r"[\\/]", v)[-1]
    ok = DIAG_CHECKS.get(kind)
    return v if ok and ok(v) else DIAG_FREE


def diag_line(line):
    """Una linea de server.log para el diagnostico: la fecha y el nivel, y del mensaje lo de su plantilla
    (DIAG_LINES), con cada hueco segun su tipo. Sin plantilla (o con mas de DIAG_MAX caracteres): la sangria, el icono
    del principio (●, ✖, 🗣…) y «<texto>» con los codigos de error que lleve."""
    head = _DIAG_HEAD.match(line)
    head = head.group(0) if head else ""
    msg = line[len(head):]
    for rx in _DIAG_RX if len(msg) <= DIAG_MAX else ():
        m = rx.fullmatch(msg)
        if m:
            out, pos = [], 0
            for name in sorted((g for g in rx.groupindex if m.start(g) >= 0), key=m.start):
                out += [msg[pos:m.start(name)], _diag_value(name[0], m.group(name))]
                pos = m.end(name)
            return head + "".join(out) + msg[pos:]
    body = msg.lstrip(" ")
    indent, first = msg[:len(msg) - len(body)], body.split(" ", 1)[0]
    icon = first + " " if first and len(first) <= 2 and not re.search(r"\w", first) else ""
    rest = body[len(icon):]
    if not rest.strip():
        return head + indent + body.strip()
    return head + indent + icon + DIAG_FREE + diag_codes(rest)
