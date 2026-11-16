/* Hoy (rediseño B7): la pantalla de inicio, qué grabar ahora y qué tienes pendiente.
   - Arriba, la reunión de ahora (la ventana de reunión de Teams o la cita de Outlook en curso) con su proyecto y su
     idioma, cambiables, y «Grabar», que empieza ya con el plan del servidor: /api/hoy lo trae calculado con
     plan.py, la misma regla que la tarjeta de Grabar, Ctrl+Alt+R y el aviso de Teams, y al cambiar un chip se pide
     otra vez a /api/grabar/plan. Lo que se ve es el cuerpo exacto de /api/rec/start. Si la reunión se repite, lo que
     quedó abierto la última vez.
   - «Para revisar»: las reuniones de los últimos 30 días que piden algo (error, sin transcribir, voces sin nombre,
     sin proyecto), cada una con su botón. «Esta semana», por día.
   - «Mis pendientes» (review del 14/11/2026, punto 28): las tareas sin hacer de los resúmenes de todas las reuniones
     (/api/pendientes), primero las tuyas, con su casilla (marca la línea del .resumen.md, con «Deshacer» un momento),
     la reunión y el minuto.
   - La agenda de Outlook del próximo mes (un mes al menos), que el servidor lee en segundo plano y guarda
     en memoria: Hoy no espera a Outlook. Sin Outlook, la pantalla es la misma sin agenda, con una línea que dice por qué.
   - Sin reuniones: «Graba tu primera reunión», con las tres formas de grabar y la de subir un audio.
   - Al parar una grabación, si ya ha empezado otra reunión: «Ya ha empezado «X» · 10:00–11:00 · Grabar ·
     Ahora no», encima de la tarjeta en Hoy y, en las demás páginas, junto a los avisos (renderSiguiente, desde
     pollRec con /api/rec/status). La decide el servidor (_siguiente: la reunión de ahora sin la que se grababa) y
     «Grabar» graba esa con su plan, solo si sigue siendo la propuesta. */
// orden de arranque: este fichero se evalúa justo después de reunion.js (la cadena, en main.js)
import './reunion.js';
import {
  $, $$, aDate, allAudios, api, APP_NAME, avisoResumenLocalHtml, closeModal, esc, estDuration, fmtClock, fmtDur, go,
  I, isLive, jobFor, LANGS, lastTxJobFor, liveAudios, liveBanner, main, MES, nfc, openModal, openRec, openSettings,
  pad, pollJobs, pollRec, refreshTree, renderView, S, statusOf, submitTx, toast,
} from './app.js';
import { iconOf, saveProps, titleOf, vocabOptions, welcomeCard, wsPatch } from './notas.js';
import { avisoProbarHtml, cfgAccionHtml } from './configurar.js';

const HOY_REVISAR_DIAS = 30;  // «Para revisar»: lo de los últimos 30 días (lo de antes, en Reuniones)
const HOY_REVISAR_MAX = 8;
const HOY_AGENDA_DIAS_VISTA = 7;  // la agenda enseña una semana; el resto del mes, con «Ver el resto del mes»
const HOY_CADA_MS = 30000;  // /api/hoy mientras Hoy está a la vista: la reunión de ahora cambia sola
const HOY_DIAS_C = ['dom', 'lun', 'mar', 'mié', 'jue', 'vie', 'sáb'];
const HOY_DIAS_L = ['domingo', 'lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado'];
const HOY_MESES_L = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre',
  'noviembre', 'diciembre'];
// lo que se ha pedido al servidor y lo que has cambiado en la tarjeta de ahora (pedido: {vocab?, cambios: {idioma?}})
const HOY = { datos: null, at: 0, cargando: null, pedido: null, plan: null, seq: 0, pendiente: null, todo: false,
  leyendo: 0, error: null };
// «Mis pendientes»: lo último de /api/pendientes; filtro ('tuyas'/'todas', null = tuyas si hay alguna); hechas: las que
// se acaban de marcar (clave -> temporizador), que siguen a la vista con «Deshacer»; enviando: las que van al servidor
const HOY_PEND_MAX = 12;
const HOY_PEND_DESHACER_MS = 6000;
const PEND = { datos: null, at: 0, cargando: null, filtro: null, todo: false, hechas: new Map(), enviando: new Set(),
  error: null, seq: 0 };

/* ---------------------------------------------------------------- fechas */
const hoyIniDia = d => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const hoyFechaDe = a => aDate(a) || new Date((a.mtime || 0) * 1000);
/* el título en Hoy: el suyo, el del archivo o, sin ninguno, «21:15 · sin nombre» (no la fecha larga: ya va al lado) */
const hoyTitulo = a => titleOf(a) || (a.label ? nfc(a.label) : `${pad(hoyFechaDe(a).getHours())}:${pad(hoyFechaDe(a).getMinutes())} · sin nombre`);
const hoyHora = iso => (iso || '').slice(11, 16);
function hoyDiaTxt(d, now, conMes) {
  const dd = Math.round((hoyIniDia(d) - hoyIniDia(now)) / 864e5);  // redondeado: los días de 23 y 25 h del cambio de hora
  if (dd === 0) return 'Hoy';
  if (dd === -1) return 'Ayer';
  if (dd === 1) return 'Mañana';
  const s = `${HOY_DIAS_C[d.getDay()]} ${d.getDate()}${conMes ? ' ' + MES[d.getMonth()] : ''}`;
  return s[0].toUpperCase() + s.slice(1);
}
function hoyFechaLarga(now) { return `${HOY_DIAS_L[now.getDay()]} ${now.getDate()} de ${HOY_MESES_L[now.getMonth()]}`; }
function hoyFaltan(min) {
  if (min < 1) return 'ya';
  if (min < 60) return `en ${Math.round(min)} min`;
  const h = Math.floor(min / 60), m = Math.round(min % 60);
  return `en ${h} h${m ? ` ${m} min` : ''}`;
}

/* ---------------------------------------------------------------- lo que pide algo de ti (Para revisar) */
const hoySumJob = path => { for (let i = S.jobs.length - 1; i >= 0; i--) { const j = S.jobs[i]; if (j.path === path && j.kind === 'summary') return j; } return null; };
const hoySumError = path => hoySumJob(path)?.status === 'error';
/* «No es de ningún proyecto» (review del 14/11/2026, punto 38): una respuesta, como elegir uno, que quita la fila. Va al
   servidor como propiedad de la reunión (sin_proyecto, en su .md), que es lo que manda. En localStorage solo queda lo
   que el servidor aún no ha guardado (falló al mandarlo, o se contestó con una versión que no lo guardaba): se le
   manda al abrir Hoy y se borra de aquí (hoyMigrarSinProy). */
const HOY_SIN_PROY = 'tenca.sinProyecto';
function hoySinProySet() {
  if (!HOY.sinProy) { try { HOY.sinProy = new Set(JSON.parse(localStorage.getItem(HOY_SIN_PROY) || '[]')); } catch { HOY.sinProy = new Set(); } }
  return HOY.sinProy;
}
const hoySinProyecto = a => !!(a.props || {}).sin_proyecto || hoySinProySet().has(a.path);
function hoyApuntarSinProy(path, si) {
  const s = hoySinProySet();
  if (si) s.add(path); else s.delete(path);
  try { localStorage.setItem(HOY_SIN_PROY, JSON.stringify([...s].slice(-500))); } catch { /* sin almacenamiento */ }
}
/* lo apuntado aquí, al servidor, una vez por apertura: si la reunión ya lo tiene o ya tiene proyecto, solo se borra de
   aquí; si no está en la lista (se movió o se borró), se queda */
async function hoyMigrarSinProy(audios) {
  const s = hoySinProySet();
  if (HOY.sinProyMigrado || !s.size || !audios.length) return;
  HOY.sinProyMigrado = true;
  for (const path of [...s]) {
    const a = audios.find(x => x.path === path);
    if (!a) continue;
    if (!(a.props || {}).sin_proyecto && !(a.props || {}).workstream) {
      try {
        const r = await api('/api/notes', { path, props: { sin_proyecto: 'sí' } });
        if (!(r.props || {}).sin_proyecto) continue;  // un servidor que no lo guarda: sigue valiendo lo de aquí
        a.props = r.props; a.notes_mtime = r.mtime;
      } catch { continue; }  // se intenta la próxima vez
    }
    hoyApuntarSinProy(path, false);
  }
}
/* Lo que pide una reunión, en orden: lo que no deja leerla primero (error, a medias, sin transcribir), luego las voces,
   el resumen fallido y el proyecto. El botón es el del primero. null si no pide nada o si está en marcha (la cola y el
   directo ya lo dicen). */
function hoyRevision(a) {
  if (isLive(a) || a.recording || jobFor(a.path)) return null;
  const st = statusOf(a), motivos = [];
  let accion = null, fallo = null;  // fallo: el trabajo que falló, para decir por qué en la fila y su botón (punto 38)
  const anota = (texto, aviso, k, label) => { motivos.push({ texto, aviso }); if (!accion && k) accion = { k, label }; };
  if (st === 'error') { anota('No se pudo transcribir', true, 'transcribir', 'Reintentar'); fallo = lastTxJobFor(a.path); }
  else if (st === 'partial') anota(`La transcripción se cortó en ${fmtClock(a.tx_end)}`, true, 'continuar', 'Continuar');
  else if (st === 'pending') anota(`Grabada · sin transcribir (${fmtDur(estDuration(a))})`, false, 'transcribir', 'Transcribir');
  const sinNombre = (a.speakers && a.speakers.sin_nombre) || 0;
  if (sinNombre) anota(`${sinNombre} ${sinNombre === 1 ? 'voz' : 'voces'} sin nombre`, true, 'voces', 'Ponles nombre');
  if (!a.has_summary && hoySumError(a.path)) { anota('No se pudo resumir', true, 'resumir', 'Reintentar el resumen'); fallo ||= hoySumJob(a.path); }
  if ((S.vocab || []).length && !(a.props || {}).workstream && !hoySinProyecto(a)) anota('¿De qué proyecto es?', false, 'proyecto', 'Elegir proyecto');
  if (!motivos.length) return null;
  if (a.has_summary) motivos.unshift({ texto: 'Resumen listo', aviso: false });
  return { a, motivos, accion, fallo };
}
function hoyRevisar(audios, now) {
  const desde = hoyIniDia(now).getTime() - (HOY_REVISAR_DIAS - 1) * 864e5;
  return audios.filter(a => hoyFechaDe(a).getTime() >= desde).map(hoyRevision).filter(Boolean)
    .sort((x, y) => hoyFechaDe(y.a) - hoyFechaDe(x.a));
}
/* Esta semana (de lunes a hoy), por día, de la más reciente; sin las que ya salen en Para revisar */
function hoySemana(audios, now, fuera) {
  const lunes = hoyIniDia(now).getTime() - ((now.getDay() + 6) % 7) * 864e5, dias = new Map();
  audios.filter(a => !fuera.has(a.path) && hoyFechaDe(a).getTime() >= lunes)
    .sort((x, y) => hoyFechaDe(y) - hoyFechaDe(x))
    .forEach(a => { const k = hoyIniDia(hoyFechaDe(a)).getTime(); if (!dias.has(k)) dias.set(k, []); dias.get(k).push(a); });
  return [...dias.entries()].map(([k, l]) => ({ dia: hoyDiaTxt(new Date(k), now), audios: l }));
}
/* La agenda por día desde ahora (las citas que ya han terminado no salen); `ahora`: la de la tarjeta de arriba */
function hoyAgendaDias(citas, now, ahora) {
  const dias = new Map();
  (citas || []).forEach(c => {
    const s = new Date(c.start), e = new Date(c.end);
    if (isNaN(s) || isNaN(e) || e <= now) return;
    const k = hoyIniDia(s).getTime();
    if (!dias.has(k)) dias.set(k, []);
    dias.get(k).push(Object.assign({}, c, { ahora: !!(ahora && ahora.inicio === c.start && (ahora.asunto ?? ahora.titulo) === c.subject) }));
  });
  return [...dias.entries()].sort((x, y) => x[0] - y[0]).map(([k, l]) => ({ k, dia: hoyDiaTxt(new Date(k), now, true), citas: l }));
}

/* ---------------------------------------------------------------- cargar */
/* En una llamada, o grabando, el servidor no relee Outlook (lo está usando la reunión: _agenda_paso). «Actualizar» lo
   dice («se leerá al colgar») en vez de quedarse 45 s en «Leyendo de Outlook…» y volver a lo de antes sin decir nada
   (review del 14/11/2026, punto 38). 'llamada', 'grabando' o null. Si /api/hoy lo trae (agenda.ocupado), manda eso. */
function hoyAgendaOcupada() {
  const g = HOY.datos?.agenda;
  if (g && 'ocupado' in g) return g.ocupado || null;
  if (S.rec?.recording) return 'grabando';
  return S.rec?.meeting || HOY.datos?.ahora?.teams ? 'llamada' : null;
}
const hoyViejo = () => Date.now() - Math.max(HOY.at, HOY.intento || 0) > HOY_CADA_MS;  // (un fallo no se reintenta en bucle)
/* refrescar: que el servidor relea la agenda ya («Actualizar», «Volver a leer»); otraVez: si hay otra petición en
   camino (que puede traer lo de antes de un cambio), una más al terminar; abrir: al llegar a Hoy, que el servidor relea
   la agenda si tiene más de 2 min (el sondeo de cada 30 s no la relee: cada lectura ocupa Outlook) */
function hoyCargar(refrescar, otraVez, abrir) {
  if (HOY.cargando) { if (otraVez) HOY.otraVez = refrescar ? 'r' : (HOY.otraVez || true); return HOY.cargando; }
  HOY.intento = Date.now();
  if (refrescar) HOY.hasta = { t: Date.now() + 45000, comprobada: HOY.datos?.agenda?.comprobada || null };
  const q = refrescar ? '?refrescar=1' : abrir ? '?abrir=1' : '';
  HOY.cargando = api('/api/hoy' + q).then(d => {
    const antes = hoyPlan()?.nombre?.valor;  // la que se ve (con un chip cambiado, la de su plan)
    HOY.datos = d; HOY.at = Date.now(); HOY.error = null;
    // otra reunión: lo que cambiaste era para la anterior, y la tarjeta se repinta aunque tengas el foco en ella (si
    // no, enseñaría la anterior y «Grabar» grabaría la nueva)
    // (HOY.seq sube: un plan de la anterior que llegue tarde ya no se pinta; revisión 2, P1)
    if (antes !== d.plan?.nombre?.valor) { HOY.pedido = null; HOY.plan = null; HOY.forzarAhora = true; HOY.seq++; }
    if (!HOY.pedido) HOY.plan = null;
    // «Actualizar» en una llamada: al colgar, se pide la lectura que entonces no se pudo hacer
    if (HOY.alColgar && !hoyAgendaOcupada()) { HOY.alColgar = null; HOY.otraVez = 'r'; }
    // la agenda se está leyendo, o la has pedido otra vez y aún no ha cambiado: se vuelve a preguntar enseguida
    clearTimeout(HOY.leerTimer);
    const esperando = HOY.hasta && Date.now() < HOY.hasta.t && (d.agenda?.comprobada || null) === HOY.hasta.comprobada;
    if (!esperando) HOY.hasta = null;
    if ((d.agenda?.estado === 'leyendo' && HOY.leyendo < 20) || esperando) {
      HOY.leyendo++;
      HOY.leerTimer = setTimeout(() => hoyCargar(), esperando ? 2000 : 3000);
    } else HOY.leyendo = 0;
  }).catch(err => { HOY.error = err.message; }).finally(() => {
    HOY.cargando = null;
    const otra = HOY.otraVez;
    HOY.otraVez = false;
    if (otra) hoyCargar(otra === 'r');
    hoyPintarVivo();
    if (S.route?.kind === 'c') renderView();  // el calendario, con las citas
  });
  return HOY.cargando;
}
/* la tarjeta de ahora y la agenda, en su sitio, sin repintar la página. La tarjeta no se repinta mientras eliges un
   chip, salvo que la reunión haya cambiado; la agenda (solo botones) siempre. El foco vuelve a lo que tenías */
function hoyRepintar(el, html) {
  const f = document.activeElement, dentro = f && el.contains(f);
  const sel = !dentro ? null : f.dataset.hoy ? `[data-hoy="${f.dataset.hoy}"]`
    : f.dataset.act ? `[data-act="${f.dataset.act}"]${f.dataset.inicio ? `[data-inicio="${CSS.escape(f.dataset.inicio)}"]` : ''}` : null;
  el.innerHTML = html;
  if (sel) $(sel, el)?.focus();
}
function hoyPintarVivo(forzar) {
  if (S.route?.kind !== 'home') return;
  const ah = $('#hoyAhora'), ag = $('#hoyAgenda');
  if (ah && (forzar || HOY.forzarAhora || !ah.contains(document.activeElement))) hoyRepintar(ah, hoyAhoraHtml());
  HOY.forzarAhora = false;
  if (ag) hoyRepintar(ag, hoyAgendaHtml());
}
setInterval(() => {
  if (S.route?.kind === 'home' && !document.hidden && !S.off && $('#modal')?.classList.contains('hidden')) {
    hoyCargar();
    if (Date.now() - PEND.at > HOY_CADA_MS) hoyPendCargar();
  }
}, HOY_CADA_MS);

/* ---------------------------------------------------------------- la reunión de ahora */
const hoyPlan = () => HOY.plan || HOY.datos?.plan || null;
function hoyLinea(p, clave) { return (p?.lineas || []).find(l => l.clave === clave); }
function hoyKicker(info, now) {
  const horas = info.inicio && info.fin ? ` · ${hoyHora(info.inicio)}–${hoyHora(info.fin)}` : '';
  if (info.teams) return `Ahora en Teams${horas}`;
  if (info.en_curso) return `Ahora${horas}`;
  const min = info.inicio ? (new Date(info.inicio) - now) / 60000 : 0;
  return `A las ${hoyHora(info.inicio)} · ${hoyFaltan(min)}`;
}
function hoyUltimaHtml(u) {
  if (!u) return '';
  const t = u.tareas == null ? (u.resumen ? '' : 'sin resumen') : u.tareas === 0 ? 'sin tareas abiertas'
    : `<b>${u.tareas} ${u.tareas === 1 ? 'tarea abierta' : 'tareas abiertas'}</b>`;
  return `<div class="ha-ultima">${I.refresh}<span>La última vez (${esc(u.fecha_txt || '')})${t ? `: ${t}` : ''} · <a href="#" data-act="hoy-ultima" data-path="${esc(u.path)}">${u.resumen ? 'ver resumen' : 'ver la reunión'}</a></span></div>`;
}
function hoyAhoraHtml() {
  if (S.rec?.recording) return '';  // grabando: arriba está el directo (liveBanner)
  const d = HOY.datos, p = hoyPlan(), now = new Date();
  if (!d) return `<section class="card hoy-ahora vacio" aria-busy="true"><div class="ha-txt"><div class="ha-kicker">Ahora</div><div class="muted">${HOY.error ? esc(HOY.error) : 'Mirando si estás en una reunión…'}</div></div></section>`;
  const info = d.ahora;
  if ((!info || !p) && !allAudios().length) return '';  // sin reunión ni reuniones: lo dice «Graba tu primera reunión»
  if (!info || !p) {  // sin reunión: «Grabar» abre la tarjeta para ponerle nombre
    HOY.visto = null;  // un «Grabar» pulsado antes, con un plan en camino, ya no graba la que se veía (hoyGrabar)
    const prox = hoyAgendaDias(d.agenda?.citas, now).flatMap(x => x.citas)[0];
    return `<section class="card hoy-ahora vacio"><div class="ha-ico">${I.mic}</div><div class="ha-txt">
      <div class="ha-kicker">Ahora</div><div class="ha-titulo sin">No estás en ninguna reunión</div>
      ${prox ? `<div class="ha-prox">Próxima: <b>${esc(prox.subject || '(sin asunto)')}</b> · ${esc(hoyDiaTxt(new Date(prox.start), now))} a las ${hoyHora(prox.start)}</div>` : ''}</div>
      <button class="btn ha-grabar" data-act="hoy-tarjeta" title="Abre la tarjeta de grabar para ponerle nombre y proyecto"><i class="recdot"></i>Grabar…</button></section>`;
  }
  const idi = hoyLinea(p, 'idioma'), proy = p.proyecto || {};
  const vocab = (S.vocab || []).length ? `<label class="ha-chip" title="Proyecto › workstream: su carpeta, su idioma y su vocabulario">${I.folder}<select data-hoy="vocab" aria-label="Proyecto">${hoyProyOpciones(proy.vocab || '')}</select></label>` : '';
  const lang = idi ? `<label class="ha-chip" title="Idioma de la reunión">${I.globe}<select data-hoy="idioma" aria-label="Idioma">${LANGS.map(([v, l]) => `<option value="${v}"${v === (idi.code || '') ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select></label>` : '';
  const motivo = [proy.vocab ? proy.motivo : '', idi && idi.origen !== 'ajustes' ? `idioma ${idi.origen_txt}` : ''].filter(Boolean).join(' · ');
  const nombre = p.nombre?.valor || info.titulo;
  HOY.visto = p;  // «Grabar» manda este, el que se está viendo
  return `<section class="card hoy-ahora"><div class="ha-ico">${info.teams ? I.phone : I.calendar}</div><div class="ha-txt">
    <div class="ha-kicker">${esc(hoyKicker(info, now))}</div>
    <h2 class="ha-titulo">${esc(nombre)}</h2>
    <div class="ha-chips">${vocab}${lang}${motivo ? `<span class="ha-motivo">${esc(motivo)}</span>` : ''}</div>
    ${hoyUltimaHtml(d.ultima)}
    </div><button class="btn primary ha-grabar" data-act="hoy-grabar" title="${esc(p.resumen || '')}"><i class="recdot"></i>Grabar</button></section>`;
}
/* el chip del proyecto: «Proyecto › Workstream» en cada opción (cerrado, un <select> solo enseña la opción, sin su grupo) */
function hoyProyOpciones(sel) {
  return `<option value=""${sel ? '' : ' selected'}>Sin proyecto</option>` + (S.vocab || []).map(v =>
    `<option value="${esc(v.id)}"${v.id === sel ? ' selected' : ''}>${esc(v.project)} › ${esc(v.name)}</option>`).join('');
}
/* un chip cambiado: el plan otra vez, con lo cambiado para esta reunión (como «Solo esta reunión» en la tarjeta) */
function hoyCambio(el) {
  const p = hoyPlan();
  if (!p) return;
  const pedido = HOY.pedido || { cambios: {} };
  if (el.dataset.hoy === 'vocab') pedido.vocab = el.value;
  else if (el.dataset.hoy === 'idioma') pedido.cambios.idioma = el.value;
  HOY.pedido = pedido;
  const seq = ++HOY.seq;
  HOY.pendiente = api('/api/grabar/plan', pedido).then(plan => {
    // solo la respuesta a lo último que cambiaste; si entretanto el sondeo trajo otra reunión (HOY.seq), se descarta
    if (seq !== HOY.seq || HOY.pedido !== pedido) return;
    // el servidor ya está en otra reunión (o en ninguna) y el sondeo aún no lo ha traído: lo cambiado era para la que
    // se veía. Ni se pinta este plan sobre la tarjeta de la anterior (revisiones 3 y 4: título de una con la hora de
    // otra, o «Grabar» sin nombre) ni se queda el chip cambiado sin aplicarse: se trae la de ahora y se repinta entera.
    // «Grabar» espera a esto (HOY.pendiente) y, si la reunión ha cambiado, no graba (hoyGrabar).
    if ((plan.nombre?.valor || '') !== (HOY.datos?.plan?.nombre?.valor || '')) {
      HOY.pedido = null; HOY.plan = null; HOY.visto = null; HOY.forzarAhora = true; HOY.at = 0;
      toast('La reunión de ahora ha cambiado: revisa la tarjeta antes de grabar');
      return hoyCargar(false, true);
    }
    HOY.plan = plan;
    const ah = $('#hoyAhora');
    if (ah) { ah.innerHTML = hoyAhoraHtml(); $(`[data-hoy="${el.dataset.hoy}"]`, ah)?.focus(); }
  }).catch(err => toast(esc(err.message), 'err'));
}
async function hoyGrabar(b) {
  if (S.rec?.recording) return toast('Ya hay una grabación en marcha.', 'err');
  b.disabled = true;
  try {
    const vista = HOY.visto?.nombre?.valor;  // la reunión que se veía al pulsar
    await HOY.pendiente;  // lo último que has cambiado: se graba con lo que se está viendo
    const p = HOY.visto;
    if (vista && p?.nombre?.valor !== vista) throw new Error('La reunión de ahora ha cambiado: mírala y vuelve a pulsar «Grabar».');
    if (!p) throw new Error('Aún no se sabe cómo grabar: espera un segundo y vuelve a pulsar.');
    S.recLocal = true;
    const r = await api('/api/rec/start', { ...p.rec, resumen: p.resumen });
    toast(`● Grabando · ${esc(p.resumen)}${r.aviso ? ` · ${esc(r.aviso)}` : ''}`);
    HOY.pedido = null; HOY.plan = null; HOY.visto = null; HOY.at = 0;
    await refreshTree(true);
    pollRec(); pollJobs(true);
    go('a', r.path);
  } catch (err) { S.recLocal = false; toast(esc(err.message), 'err'); b.disabled = false; }
}

/* ---------------------------------------------------------------- «Ya ha empezado «X»» al parar */
// la propuesta que viene en /api/rec/status (S.rec.siguiente) mientras no se graba; «Ahora no» la quita aquí al momento
let hoySigNo = null;
function hoySiguienteTxt(g) {
  const horas = g.inicio && g.fin ? ` · ${hoyHora(g.inicio)}–${hoyHora(g.fin)}` : g.teams ? ' en Teams' : '';
  return `Ya ha empezado «${g.titulo}»${horas}`;
}
function hoySiguienteHtml(g) {
  return `<div class="hoy-siguiente" role="status" data-siguiente="${esc(g.titulo)}"><span class="hs-ico">${I.calendar}</span>`
    + `<span class="hs-txt">${esc(hoySiguienteTxt(g))}</span>`
    + `<button class="btn sm primary" type="button" data-sig-act="grabar" title="Empieza a grabar esta reunión con su proyecto y su idioma"><i class="recdot"></i>Grabar</button>`
    + '<button class="btn sm ghost" type="button" data-sig-act="no" title="No volver a proponerla hasta la próxima grabación">Ahora no</button></div>';
}
function renderSiguiente() {
  const dada = !S.rec?.recording && S.rec?.siguiente || null;
  if (!dada || dada.titulo !== hoySigNo) hoySigNo = null;  // «Ahora no» vale hasta que el servidor deje de darla
  const g = dada && dada.titulo !== hoySigNo ? dada : null;
  const enHoy = S.route?.kind === 'home' && $('#hoySiguiente');
  const sig = g ? JSON.stringify([g.titulo, g.inicio, g.fin, g.teams]) : '';
  for (const [el, toca] of [[$('#hoySiguiente'), enHoy], [$('#sigToast'), !enHoy]]) {
    if (!el) continue;
    const html = toca && g ? hoySiguienteHtml(g) : '';
    if (el.dataset.sig === (html ? sig : '')) continue;
    el.dataset.sig = html ? sig : '';
    el.innerHTML = html;
  }
  if (!enHoy && g && !$('#sigToast')) {
    const el = document.createElement('div');
    el.id = 'sigToast';
    el.className = 'toast sig-toast';
    el.dataset.sig = sig;
    el.innerHTML = hoySiguienteHtml(g);
    $('#toasts')?.prepend(el);
  } else if ((!g || enHoy) && $('#sigToast')) $('#sigToast').remove();
}
async function hoySiguienteAccion(b) {
  const caja = b.closest('[data-siguiente]'), titulo = caja?.dataset.siguiente;
  if (!titulo) return;
  if (b.dataset.sigAct === 'no') {
    hoySigNo = titulo;
    renderSiguiente();
    try { await api('/api/rec/siguiente', { descartar: true }); pollRec(); } catch (err) { hoySigNo = null; toast(esc(err.message), 'err'); }
    return;
  }
  if (S.rec?.recording) return toast('Ya hay una grabación en marcha.', 'err');
  $$('[data-sig-act]').forEach(x => { x.disabled = true; });
  try {
    S.recLocal = true;
    const r = await api('/api/rec/siguiente', { grabar: titulo });
    toast(`● Grabando · ${esc(r.plan?.resumen || titulo)}${r.aviso ? ` · ${esc(r.aviso)}` : ''}`);
    HOY.pedido = null; HOY.plan = null; HOY.visto = null; HOY.at = 0;
    await refreshTree(true);
    pollRec(); pollJobs(true);
    if (r.path) go('a', r.path);
  } catch (err) {
    S.recLocal = false;
    toast(esc(err.message), 'err');
    $$('[data-sig-act]').forEach(x => { x.disabled = false; });
    pollRec();
  }
}
addEventListener('click', e => {
  const b = e.target.closest?.('[data-sig-act]');
  if (!b) return;
  e.preventDefault();
  e.stopPropagation();  // dentro de un toast: el clic no lo cierra ni abre otra cosa
  hoySiguienteAccion(b);
}, true);

/* ---------------------------------------------------------------- Para revisar y Esta semana */
function hoyProyTxt(a) {
  const p = a.props || {};
  return p.workstream ? [p.proyecto, p.workstream].filter(Boolean).join(' › ') : '';
}
/* por qué falló, en la fila (antes solo en la Cola): la frase de explicar.py, con el texto técnico al pasar por encima */
function hoyFalloHtml(j) {
  const frase = j && (j.aviso?.frase || j.error);
  return frase ? `<div class="hr-motivo" title="${esc(j.error || '')}">${esc(frase)}</div>` : '';
}
function hoyRevisarHtml(lista, now) {
  if (!lista.length) return '';
  const filas = lista.slice(0, HOY_REVISAR_MAX).map(r => {
    const a = r.a, proy = hoyProyTxt(a);
    return `<div class="hr-fila" data-go="a" data-path="${esc(a.path)}"><span class="hr-dia">${esc(hoyDiaTxt(hoyFechaDe(a), now))}</span>
      <div class="hr-t"><div class="hr-n">${iconOf(a) ? iconOf(a) + ' ' : ''}${esc(hoyTitulo(a))}${proy ? `<span class="hr-proy">${esc(proy)}</span>` : !(S.vocab || []).length ? '' : hoySinProyecto(a) ? '<span class="hr-proy">Ningún proyecto</span>' : '<span class="hr-proy sin">Sin proyecto</span>'}</div>
      <div class="hr-sub">${r.motivos.map(m => m.aviso ? `<b class="aviso">${esc(m.texto)}</b>` : esc(m.texto)).join(' · ')}</div>${hoyFalloHtml(r.fallo)}</div>
      <div class="hr-acts">${r.fallo ? cfgAccionHtml(r.fallo) : ''}<button class="btn sm" data-act="hoy-accion" data-k="${r.accion.k}" data-path="${esc(a.path)}">${esc(r.accion.label)}</button></div></div>`;
  }).join('');
  const mas = lista.length - HOY_REVISAR_MAX;
  return `<section class="hoy-revisar"><h2>Para revisar <span class="count">${lista.length}</span></h2><div class="card hr-lista">${filas}</div>
    ${mas > 0 ? `<a class="hr-mas" href="#/r/">y ${mas} más · ver en Reuniones</a>` : ''}</section>`;
}
function hoyChip(a) {
  const j = jobFor(a.path);
  if (isLive(a) || a.recording) return '<span class="pill rec"><i class="recdot"></i>Grabando</span>';
  if (j) return `<span class="pill run">${j.kind === 'voces' ? 'Voces' : 'Transcribiendo'}${j.status === 'running' ? ` ${Math.round(j.progress * 100)} %` : ''}</span>`;
  if (a.has_summary) return `<a class="pill ok" href="#" data-act="hoy-resumen" data-path="${esc(a.path)}">${I.check}Resumen</a>`;
  if (a.txt || a.srt) return '<span class="pill">Transcrita</span>';
  return '<span class="pill pending">Sin transcribir</span>';
}
function hoySemanaHtml(dias) {
  if (!dias.length) return '';
  const filas = dias.map(d => d.audios.map((a, i) => {
    const p = a.props || {}, sub = [hoyProyTxt(a), fmtDur(estDuration(a)), p.participantes].filter(Boolean).join(' · ');
    return `<div class="hs-fila" data-go="a" data-path="${esc(a.path)}"><span class="hr-dia">${i ? '' : esc(d.dia)}</span>
      <div class="hr-t"><div class="hr-n">${iconOf(a) ? iconOf(a) + ' ' : ''}${esc(hoyTitulo(a))}</div><div class="hr-sub">${esc(sub)}</div></div>${hoyChip(a)}</div>`;
  }).join('')).join('');
  return `<section class="hoy-semana"><h2>Esta semana</h2><div class="card hr-lista">${filas}</div></section>`;
}
function hoyVacioHtml() {
  const hk = S.config?.hotkey?.ok, aviso = S.config?.meeting_detect !== false;
  return `<section class="card hoy-vacio"><h2>Graba tu primera reunión</h2>
    <ol class="hv-formas">
      <li><button class="btn primary" data-act="hoy-tarjeta"><i class="recdot"></i>Grabar</button><span>con el botón, aquí o arriba</span></li>
      <li><span class="hv-kbd"><kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>R</kbd></span><span>desde cualquier programa${hk ? '' : ` (con ${esc(APP_NAME)} en primer plano: ${esc(S.config?.hotkey?.error || 'el atajo global no está disponible')})`}</span></li>
      <li><span class="hv-ico">${I.phone}</span><span>al entrar en una llamada de Teams, ${esc(APP_NAME)} te avisa con un botón para grabar${aviso ? '' : ' (el aviso está apagado en Ajustes › Al grabar)'}</span></li>
    </ol>
    <div class="hv-subir">${I.upload}<span>o arrastra aquí un audio que ya tengas, o <a href="#" data-act="hoy-subir">súbelo</a></span></div></section>`;
}

/* ---------------------------------------------------------------- Mis pendientes (punto 28) */
/* Las tareas abiertas de los resúmenes («- [ ] Responsable: tarea — fecha [mm:ss]»), de la reunión más reciente a la
   más antigua. Lo que manda es el .resumen.md: marcar una cambia su línea a «- [x]» (POST /api/pendientes/hecha), y
   «La última vez» y la vista del resumen cuentan lo mismo. Mientras queda alguna recién marcada (con «Deshacer») no se
   relee: la lista nueva ya no la traería y se iría sin dar tiempo a deshacerla. */
/* _k: quién es la tarea (para no perder una recién marcada al releer); _id: un número para la fila en la página (en
   data-k y data-hoy: la casilla y su «Deshacer» comparten data-hoy, así el foco pasa de una a otro al repintar) */
const hoyPendClave = t => `${t.path}\u0001${t.i}\u0001${t.texto}`;
function hoyPendCargar(forzar) {
  if (PEND.cargando || (!forzar && PEND.hechas.size)) return PEND.cargando;
  PEND.at = Date.now();
  PEND.cargando = api('/api/pendientes').then(d => {
    const nuevas = d.tareas || [], viejas = PEND.datos?.tareas || [];
    nuevas.forEach(t => { t._k = hoyPendClave(t); t._id = ++PEND.seq; });
    // las recién marcadas siguen en su sitio hasta que venza su «Deshacer»: tras la que iba delante (al final, una
    // segunda marcada se salía de las 12 primeras y perdía su «Deshacer»; revisión 1)
    viejas.forEach((t, n) => {
      if (!PEND.hechas.has(t._k) || nuevas.some(x => x._k === t._k)) return;
      let pos = -1;
      for (let m = n - 1; m >= 0 && pos < 0; m--) pos = nuevas.findIndex(x => x._k === viejas[m]._k);
      nuevas.splice(pos + 1, 0, t);
    });
    d.tareas = nuevas;
    PEND.datos = d; PEND.error = null;
  }).catch(err => { PEND.error = err.message; }).finally(() => { PEND.cargando = null; hoyPendPintar(); });
  return PEND.cargando;
}
function hoyPendPintar() {
  const el = S.route?.kind === 'home' && $('#hoyPend');
  if (el) hoyRepintar(el, hoyPendHtml());
}
function hoyPendFilaHtml(t) {
  const a = S.audios.get(t.path), titulo = a ? hoyTitulo(a) : (t.titulo || 'Reunión');
  const k = t._id, cuando = [titulo, t.fecha_txt].filter(Boolean).join(' · ');
  if (PEND.hechas.has(t._k)) {
    return `<div class="mp-fila hecha" data-k="${k}"><span class="mp-box on" aria-hidden="true">${I.check}</span>
      <div class="mp-t"><div class="mp-n">${t.responsable ? `${esc(t.responsable)}: ` : ''}${esc(t.tarea)}</div><div class="mp-sub">Hecha · se ha marcado en el resumen</div></div>
      <button class="btn sm ghost" type="button" data-act="hoy-pend-deshacer" data-k="${k}" data-hoy="pend-${k}">Deshacer</button></div>`;
  }
  const minuto = t.segundo != null && !t.sin_audio
    ? ` · <a href="#" class="mp-min" data-act="hoy-pend-min" data-k="${k}" title="Abre la reunión en ese minuto">[${fmtClock(t.segundo)}]</a>` : '';
  return `<div class="mp-fila${t.tuya ? ' tuya' : ''}" data-k="${k}">
    <button class="mp-box" type="button" data-act="hoy-pend-hecha" data-k="${k}" data-hoy="pend-${k}" title="Marcar como hecha" aria-label="Marcar como hecha: ${esc(t.tarea)}"></button>
    <div class="mp-t"><div class="mp-n">${t.responsable ? `<b>${esc(t.responsable)}</b>: ` : ''}${esc(t.tarea)}</div>
      <div class="mp-sub"><a href="#" data-act="hoy-pend-ir" data-k="${k}" title="Abre el resumen de la reunión">${esc(cuando)}</a>${minuto}</div></div></div>`;
}
function hoyPendHtml() {
  const d = PEND.datos;
  if (!d) return PEND.error ? `<div class="mp-nada">Mis pendientes: ${esc(PEND.error)}</div>` : '';
  const todas = d.tareas || [], abiertas = todas.filter(t => !PEND.hechas.has(t._k));
  // los resúmenes que OneDrive tiene «solo en línea» no se abren (se descargarían): se dice cuántos
  const sinLeer = d.sin_leer ? ` · ${d.sin_leer === 1 ? '1 resumen está solo en OneDrive: ábrelo' : `${d.sin_leer} resúmenes están solo en OneDrive: ábrelos`} para ver sus tareas` : '';
  if (!todas.length) return `<div class="mp-nada">${I.check}<span>Mis pendientes: ${d.sin_leer ? 'ninguna tarea abierta en lo que se puede leer' : 'ninguna tarea abierta en los resúmenes de tus reuniones'}${esc(sinLeer)}</span></div>`;
  // «Tuyas» si hay alguna (también recién marcada, con su «Deshacer»); si no, todas y sin los dos botones
  const tuyas = abiertas.filter(t => t.tuya).length, hayTuyas = todas.some(t => t.tuya);
  const filtro = hayTuyas ? (PEND.filtro || 'tuyas') : 'todas';
  const lista = filtro === 'tuyas' ? todas.filter(t => t.tuya) : todas;
  const vista = PEND.todo ? lista : lista.slice(0, HOY_PEND_MAX);
  const fil = hayTuyas ? `<div class="mp-filtro" role="group" aria-label="Qué tareas">
      <button type="button" class="btn sm${filtro === 'tuyas' ? ' on' : ' ghost'}" data-act="hoy-pend-filtro" data-f="tuyas" aria-pressed="${filtro === 'tuyas'}">Tuyas (${tuyas})</button>
      <button type="button" class="btn sm${filtro === 'todas' ? ' on' : ' ghost'}" data-act="hoy-pend-filtro" data-f="todas" aria-pressed="${filtro === 'todas'}">Todas (${abiertas.length})</button></div>` : '';
  const filas = vista.map(hoyPendFilaHtml).join('');
  const mas = lista.length - vista.length;
  return `<section class="hoy-pendientes"><div class="mp-h"><h2>Mis pendientes <span class="count">${abiertas.length}</span></h2>${fil}</div>
    <div class="card hr-lista mp-lista">${filas}</div>${sinLeer ? `<div class="muted mp-sin-leer">${esc(sinLeer.slice(3))}</div>` : ''}
    ${mas > 0 ? `<button class="btn ghost sm mp-mas" type="button" data-act="hoy-pend-todas">Ver todas (${lista.length})</button>` : ''}</section>`;
}
const hoyPendDe = k => (PEND.datos?.tareas || []).find(t => String(t._id) === k);
async function hoyPendMarcar(t, hecha) {
  if (!t || PEND.enviando.has(t._k)) return;
  PEND.enviando.add(t._k);
  try {
    const r = await api('/api/pendientes/hecha', { path: t.path, i: t.i, texto: t.texto, hecha });
    clearTimeout(PEND.hechas.get(t._k));
    PEND.hechas.delete(t._k);
    t.i = r.i ?? t.i;  // donde estaba de verdad (si el resumen se había editado); _k no cambia: es la de esta fila
    if (hecha) {
      PEND.hechas.set(t._k, setTimeout(() => {
        PEND.hechas.delete(t._k);
        if (PEND.datos) PEND.datos.tareas = PEND.datos.tareas.filter(x => x !== t);
        hoyPendPintar();
        hoyPendCargar(true);
      }, HOY_PEND_DESHACER_MS));
    }
    if (S.sum && S.sum.path === t.path) S.sum = null;  // la vista del resumen lo vuelve a leer
    if (HOY.datos?.ultima?.path === t.path) hoyCargar(false, true);  // «La última vez: N tareas abiertas», al momento
    hoyPendPintar();
  } catch (err) {
    toast(esc(err.message), 'err');
    hoyPendCargar(true);
  } finally { PEND.enviando.delete(t._k); }
}

/* ---------------------------------------------------------------- la agenda de Outlook */
function hoyAgendaHtml() {
  const d = HOY.datos, g = d?.agenda;
  if (!g) return '';
  const now = new Date(), dias = hoyAgendaDias(g.citas, now, d.ahora);
  const limite = hoyIniDia(now).getTime() + HOY_AGENDA_DIAS_VISTA * 864e5;
  const vista = HOY.todo ? dias : dias.filter(x => x.k < limite);
  const resto = dias.filter(x => x.k >= limite).reduce((n, x) => n + x.citas.length, 0);
  const activo = g.estado !== 'apagado' && !['no_windows', 'sin_outlook', 'outlook_nuevo'].includes(g.estado);
  const accion = g.accion ? `<button class="btn sm" data-act="hoy-agenda-${esc(g.accion.id)}">${esc(g.accion.texto)}</button>` : '';
  const linea = g.estado !== 'ok' ? `<div class="ag-estado${g.estado === 'leyendo' ? ' leyendo' : ''}">${g.estado === 'leyendo' ? '' : I.info}<span>${esc(g.frase)}</span>${accion}</div>` : '';
  const filas = vista.map(x => `<div class="ag-dia"><div class="ag-dia-h">${esc(x.dia)}</div>${x.citas.map(c => {
    const tags = [c.ahora ? '<span class="ag-tag ahora">Ahora</span>' : '', c.is_teams ? '<span class="ag-tag">Teams</span>' : '',
      c.recurring ? `<span class="ag-tag" title="Periódica">${I.refresh}</span>` : ''].join('');
    const lugar = c.location && !c.is_teams ? `<div class="ag-lugar">${esc(c.location)}</div>` : '';
    const prep = c.preparada ? `<div class="ag-prep-txt">${I.check}${esc(hoyPreparadaTxt(c.preparada))}</div>` : '';
    // «Preparar» en las que aún no han empezado (la de ahora se cambia en la tarjeta de arriba)
    const boton = c.ahora || new Date(c.start) <= now || !(c.subject || '').trim() ? ''
      : `<button class="btn ghost sm ag-prep${c.preparada ? ' on' : ''}" data-act="hoy-preparar" data-asunto="${esc(c.subject)}" data-inicio="${esc(c.start)}" data-fin="${esc(c.end)}" title="Elige ya su proyecto, su idioma y su contexto: se usarán al grabarla">${c.preparada ? 'Cambiar' : 'Preparar'}</button>`;
    return `<div class="ag-cita${c.ahora ? ' ahora' : ''}"><span class="ag-h">${hoyHora(c.start)}<small>${hoyHora(c.end)}</small></span>
      <div class="ag-t"><div class="ag-n">${esc(c.subject || '(sin asunto)')}${tags}</div>${lugar}${prep}</div>${boton}</div>`;
  }).join('')}</div>`).join('');
  const nada = g.estado === 'ok' && !dias.length ? `<div class="ag-nada">No tienes más reuniones en los próximos ${g.dias} días.</div>` : '';
  const colgar = HOY.alColgar && hoyAgendaOcupada();
  const pie = g.leida ? `<div class="ag-pie">${HOY.hasta ? 'Leyendo de Outlook…' : `Leída de Outlook a las ${hoyHora(g.leida)}`}${g.cortada ? ' · solo las primeras citas: tu agenda tiene más' : ''}${colgar ? ` · <b class="ag-colgar">${colgar === 'grabando' ? 'Mientras se graba no se lee Outlook: se leerá al terminar' : 'En una llamada no se lee Outlook: se leerá al colgar'}</b>` : ''}
    <button class="btn ghost sm" data-act="hoy-agenda-leer" title="Volver a leer la agenda de Outlook">${I.refresh}Actualizar</button></div>` : '';
  return `<h2>Agenda${activo ? ` <span class="count">próximos ${g.dias} días</span>` : ''}</h2>${linea}
    ${filas || nada ? `<div class="card ag-lista">${filas}${nada}</div>` : ''}
    ${resto && !HOY.todo ? `<button class="btn ghost sm ag-mas" data-act="hoy-agenda-todo">Ver el resto del mes (${resto} ${resto === 1 ? 'reunión' : 'reuniones'})</button>` : ''}${pie}`;
}

/* Calendario (notas.js, viewCalendar): las citas de Outlook que aún no han pasado, en su día, junto a las grabadas.
   Es donde se buscan «las siguientes reuniones». Se ven distintas y no se abren (no son reuniones
   grabadas). */
function hoyCalDia(key) {
  if (!HOY.cargando && (!HOY.datos || hoyViejo())) hoyCargar();
  const now = new Date(), citas = (HOY.datos?.agenda?.citas || []).filter(c => c.start.slice(0, 10) === key && new Date(c.end) > now);
  return citas.slice(0, 3).map(c => `<div class="cev outlook" title="${esc(c.subject || '(sin asunto)')} · ${hoyHora(c.start)}–${hoyHora(c.end)} · de tu Outlook">`
    + `<span class="ct">${hoyHora(c.start)}</span>${esc(c.subject || '(sin asunto)')}</div>`).join('')
    + (citas.length > 3 ? `<div class="cmore outlook">+${citas.length - 3} en Outlook</div>` : '');
}

/* ---------------------------------------------------------------- la página */
function viewHoy() {
  const all = allAudios(), now = new Date();
  if (!HOY.abierta) { HOY.abierta = true; hoyCargar(false, true, true); } else if (!HOY.datos || hoyViejo()) hoyCargar();
  if (all.length && (!PEND.datos || Date.now() - PEND.at > HOY_CADA_MS)) hoyPendCargar();
  hoyMigrarSinProy(all);
  const revisar = all.length ? hoyRevisar(all, now) : [];
  const fuera = new Set(revisar.slice(0, HOY_REVISAR_MAX).map(r => r.a.path));
  main.innerHTML = `<div class="page hoy">
    ${liveAudios().map(liveBanner).join('')}
    ${avisoProbarHtml()}
    ${avisoResumenLocalHtml()}
    <h1>Hoy <span class="sub">· ${esc(hoyFechaLarga(now))}</span></h1>
    ${welcomeCard(all)}
    <div id="hoySiguiente" class="hoy-siguiente-box"></div>
    <div class="hoy-grid">
      <div class="hoy-ahora-box" id="hoyAhora">${hoyAhoraHtml()}</div>
      <section class="hoy-agenda" id="hoyAgenda">${hoyAgendaHtml()}</section>
      <div class="hoy-rev">${all.length ? hoyRevisarHtml(revisar, now) : hoyVacioHtml()}</div>
      <div class="hoy-pend" id="hoyPend">${all.length ? hoyPendHtml() : ''}</div>
      <div class="hoy-sem">${all.length ? hoySemanaHtml(hoySemana(all, now, fuera)) : ''}</div>
    </div></div>`;
  renderSiguiente();  // «Ya ha empezado «X»» encima de la tarjeta, sin esperar al siguiente sondeo
}

/* ---------------------------------------------------------------- preparar una reunión de la agenda */
function hoyPreparadaTxt(p) {
  const v = 'vocab' in p ? (p.vocab ? (S.vocab || []).find(x => x.id === p.vocab) : null) : undefined;
  const lang = 'idioma' in p ? (LANGS.find(([c]) => c === p.idioma) || [p.idioma, p.idioma])[1].replace(/ \(.*\)$/, '') : '';
  return ['Preparada', v === undefined ? '' : v ? `${v.project} › ${v.name}` : 'sin proyecto', lang,
    p.contexto ? 'con contexto' : ''].filter(Boolean).join(' · ');
}
/* «Preparar»: proyecto, idioma y contexto de una cita que aún no ha empezado. El servidor lo guarda y plan.py lo usa
   al grabarla (de 10 min antes a 5 después), con «Grabar», la tarjeta, Ctrl+Alt+R o el aviso de Teams */
function hoyPreparar(c) {
  const p = c.preparada || {}, ini = new Date(c.start);
  const vocab = `<option value="__auto"${'vocab' in p ? '' : ' selected'}>Lo de siempre (la última vez o el título)</option>`
    + `<option value=""${p.vocab === '' ? ' selected' : ''}>Sin proyecto</option>`
    + (S.vocab || []).map(v => `<option value="${esc(v.id)}"${p.vocab === v.id ? ' selected' : ''}>${esc(v.project)} › ${esc(v.name)}</option>`).join('');
  const langs = `<option value="__auto"${'idioma' in p ? '' : ' selected'}>El del proyecto o el de Ajustes</option>`
    + LANGS.map(([v, l]) => `<option value="${v}"${'idioma' in p && p.idioma === v ? ' selected' : ''}>${esc(l)}</option>`).join('');
  openModal(`<h3>Preparar «${esc(c.subject)}»</h3>
    <div class="help" style="margin:0 0 12px">${esc(hoyDiaTxt(ini, new Date(), true))} a las ${hoyHora(c.start)}. Al grabarla (con «Grabar», Ctrl+Alt+R o el aviso de Teams) se usará esto.</div>
    <label class="hp-l">Proyecto<select id="hpVocab" class="input">${vocab}</select></label>
    <label class="hp-l">Idioma<select id="hpIdioma" class="input">${langs}</select></label>
    <label class="hp-l">Contexto <span class="muted">(opcional: de qué va y quién participa; ayuda a la transcripción y al resumen)</span>
      <textarea id="hpCtx" class="input" rows="3" maxlength="500">${esc(p.contexto || '')}</textarea></label>
    <div class="btns">${c.preparada ? '<button class="btn ghost danger" data-m="quitar" type="button">Quitar</button><span class="hp-hueco"></span>' : ''}
      <button class="btn" data-m="no" type="button">Cancelar</button><button class="btn primary" data-m="ok" type="button">Guardar</button></div>`, d => {
    d.addEventListener('click', async e => {
      const b = e.target.closest('[data-m]');
      if (!b) return;
      if (b.dataset.m === 'no') return closeModal(false);
      const auto = x => (x === '__auto' ? null : x);
      const cuerpo = { asunto: c.subject, inicio: c.start, fin: c.end, quitar: b.dataset.m === 'quitar',
        vocab: auto($('#hpVocab', d).value), idioma: auto($('#hpIdioma', d).value), contexto: $('#hpCtx', d).value };
      b.disabled = true;
      try {
        const r = await api('/api/hoy/preparar', cuerpo);
        closeModal(true);
        toast(r.preparada ? `Preparada: al grabar «${esc(c.subject)}» se usará esto` : `«${esc(c.subject)}» sin preparar: se grabará como siempre`);
        HOY.at = 0;
        hoyCargar(false, true);
      } catch (err) { toast(esc(err.message), 'err'); b.disabled = false; }
    });
  });
}

/* ---------------------------------------------------------------- acciones */
/* «¿De qué proyecto es?»: uno de la lista o «No es de ningún proyecto», que también es una respuesta y quita la fila
   (antes «Sin proyecto» cerraba sin guardar y la fila volvía; review del 14/11/2026, punto 38) */
function hoyElegirProyecto(a) {
  const opciones = vocabOptions('', 'Elige un proyecto…').replace('<option value="">', '<option value="" disabled selected>');
  openModal(`<h3>¿De qué proyecto es?</h3><div class="help" style="margin:0 0 10px">${esc(hoyTitulo(a))}: su carpeta y su vocabulario serán los del proyecto.</div>
    <select id="hoyProy" class="input" aria-label="Proyecto">${opciones}</select>
    <div class="btns"><button class="btn ghost" data-m="ninguno" type="button">No es de ningún proyecto</button><span class="hp-hueco"></span>
      <button class="btn" data-m="no" type="button">Cancelar</button><button class="btn primary" data-m="ok" type="button" disabled>Guardar</button></div>`, d => {
    const sel = $('#hoyProy', d), ok = $('[data-m="ok"]', d);
    sel.addEventListener('change', () => { ok.disabled = !sel.value; });
    d.addEventListener('click', async e => {
      const b = e.target.closest('[data-m]');
      if (!b) return;
      if (b.dataset.m === 'no') return closeModal(false);
      if (b.dataset.m === 'ninguno') {
        b.disabled = true;
        hoyApuntarSinProy(a.path, true);  // la fila se va ya; si el servidor lo guarda, se borra de aquí
        const r = await saveProps(a, { sin_proyecto: 'sí' });  // (si falla, ya lo ha dicho; aquí queda apuntado)
        if ((r?.props || {}).sin_proyecto) hoyApuntarSinProy(a.path, false);
        closeModal(true);
        toast(`«${esc(hoyTitulo(a))}» no es de ningún proyecto: ya no se pregunta`);
        if (S.route?.kind === 'home') viewHoy();
        return;
      }
      const v = (S.vocab || []).find(x => x.id === sel.value);
      if (!v) return sel.focus();
      b.disabled = true;
      const r = await saveProps(a, Object.assign(wsPatch(v), hoySinProyecto(a) ? { sin_proyecto: '' } : {}));
      if (r) hoyApuntarSinProy(a.path, false);
      closeModal(!!r);
      if (r) { toast(`🗂 ${esc(v.project)} › ${esc(v.name)}`); refreshTree(true); }
    });
  });
}
async function hoyAccion(k, a) {
  if (k === 'transcribir' || k === 'continuar') return submitTx([a.path], S.config.preset in S.presetsById ? S.config.preset : 'rapido', null, k === 'continuar');
  if (k === 'voces') { S.tab = 'tx'; S.irQuien = a.path; return go('a', a.path); }  // al bloque «Quién habla» (B8)
  if (k === 'proyecto') return hoyElegirProyecto(a);
  if (k === 'resumir') {
    try { await api('/api/summarize', { path: a.path }); toast('Resumiendo…'); pollJobs(true); } catch (err) { toast(esc(err.message), 'err'); }
  }
}
main.addEventListener('click', e => {
  const b = e.target.closest('[data-act^="hoy-"]');
  if (!b || !main.contains(b)) return;
  e.preventDefault();
  e.stopPropagation();  // dentro de una fila con data-go: el botón no abre la reunión
  const act = b.dataset.act, a = b.dataset.path ? S.audios.get(b.dataset.path) : null;
  if (act === 'hoy-grabar') hoyGrabar(b);
  else if (act === 'hoy-tarjeta') openRec();
  else if (act === 'hoy-subir') $('#btnSubir')?.click();
  else if (act === 'hoy-ultima' || act === 'hoy-resumen') { S.tab = 'sum'; go('a', b.dataset.path); }
  else if (act === 'hoy-accion' && a) hoyAccion(b.dataset.k, a);
  else if (act === 'hoy-agenda-leer') {
    HOY.leyendo = 0;
    // en una llamada, con una agenda ya leída (sin ninguna, el servidor la lee igual): se dice y se lee al colgar
    HOY.alColgar = HOY.datos?.agenda?.leida ? hoyAgendaOcupada() : null;
    hoyCargar(!HOY.alColgar, true);
    hoyPintarVivo();
  }
  else if (act === 'hoy-agenda-ajustes') openSettings({ sec: 'calendario' });
  else if (act === 'hoy-agenda-todo') { HOY.todo = true; hoyPintarVivo(true); }
  else if (act === 'hoy-preparar') {
    const c = (HOY.datos?.agenda?.citas || []).find(x => x.start === b.dataset.inicio && x.subject === b.dataset.asunto);
    if (c) hoyPreparar(c);
  }
  else if (act === 'hoy-pend-hecha' || act === 'hoy-pend-deshacer') hoyPendMarcar(hoyPendDe(b.dataset.k), act === 'hoy-pend-hecha');
  else if (act === 'hoy-pend-filtro') { PEND.filtro = b.dataset.f; PEND.todo = false; hoyPendPintar(); }
  else if (act === 'hoy-pend-todas') { PEND.todo = true; hoyPendPintar(); }
  else if (act === 'hoy-pend-ir' || act === 'hoy-pend-min') {
    const t = hoyPendDe(b.dataset.k);
    if (!t) return;
    if (act === 'hoy-pend-min') { S.tab = 'tx'; go('a', t.path, { t: String(t.segundo) }); }  // al minuto, en la transcripción
    else { S.tab = 'sum'; go('a', t.path); }
  }
}, true);
main.addEventListener('change', e => { if (e.target.dataset?.hoy) hoyCambio(e.target); });
addEventListener('hashchange', () => { if (S.route?.kind !== 'home') HOY.abierta = false; });

export { HOY, hoyAccion, hoyCalDia, hoyCargar, hoyElegirProyecto, PEND, renderSiguiente, viewHoy };
